import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from daily_review.core import Policy, Store, make_event
from daily_review.report import build_report, response_text, chunks, parse_report
from daily_review.cli import due_days, pending_days


def report_body(summary="完成一项工作", ai_usage="暂无"):
    return ("## 📌 今日关键战报\n\n" + summary + "\n\n"
            "## 💡 经验与教训沉淀\n\n暂无可沉淀事项。\n\n"
            "## 🚀 工作流提效方案（SOP / 工具 / 流程重构）\n\n暂无额外优化建议。\n\n"
            "## 🤖 AI 使用方式总结与提升\n\n" + ai_usage)


class ReportTests(unittest.TestCase):
    def test_disabled_browser_does_not_call_collector(self):
        from unittest.mock import patch
        from daily_review.cli import collect
        with tempfile.TemporaryDirectory() as t:
            store=Store(Path(t)/'db.sqlite');self.addCleanup(store.close)
            cfg={'timezone':'Asia/Shanghai','blacklist':{},'sources':{'codex':{},'lark':{},'git':{},'browser':{'enabled':False}}}
            with patch('daily_review.cli.browser_events',side_effect=AssertionError('Browser must stay disabled')):
                status=collect(cfg,store,'2026-11-10',['browser'])
            self.assertEqual(status[0]['status'],'disabled')

    def test_disabled_browser_excludes_stored_events_from_report(self):
        with tempfile.TemporaryDirectory() as t:
            store=Store(Path(t)/'db.sqlite')
            self.addCleanup(store.close)
            store.put(make_event('browser','old','2026-11-10T01:00:00+08:00','old browser','BROWSER_OMIT_ME'))
            store.put(make_event('manual','keep','2026-11-10T01:00:00+08:00','work','WORK_KEEP_ME'))
            store.run_status('2026-11-10','browser','partial',1,'old access failure')
            seen=[]
            def model(prompt):
                seen.append(prompt)
                return json.dumps({'report_markdown':report_body(),'actions':[],'lessons':[],'growth_focus':''})
            cfg={'timezone':'Asia/Shanghai','model':{},'sources':{'browser':{'enabled':False}}}
            result=build_report(store,cfg,Policy({}),'2026-11-10',Path(t)/'reports',model)
            self.assertTrue(result['ok'])
            self.assertNotIn('BROWSER_OMIT_ME',seen[0])
            self.assertIn('WORK_KEEP_ME',seen[0])
            self.assertIn('browser: disabled',Path(result['sources_path']).read_text())

    def test_response_sse_and_json(self):
        payload = {"output": [{"type": "message", "content": [{"type": "output_text", "text": "result"}]}]}
        self.assertEqual(response_text(json.dumps(payload)), "result")
        sse = 'data: ' + json.dumps({"type": "response.completed", "response": payload}) + '\n\ndata: [DONE]\n'
        self.assertEqual(response_text(sse), "result")

    def test_report_filters_old_events_and_actions_are_durable(self):
        with tempfile.TemporaryDirectory() as t:
            s = Store(Path(t) / "db.sqlite")
            s.put(make_event("codex", "allowed", "2026-11-10T01:00:00+08:00", "work", "good", scope_id="a"))
            s.put(make_event("codex", "denied", "2026-11-10T01:00:00+08:00", "private", "SECRET", scope_id="b"))
            seen = []
            def model(prompt):
                seen.append(prompt)
                return json.dumps({"report_markdown": report_body("完成了一项工作 [codex:allowed]"), "actions": [
                    {"title": "验收示例", "trigger": "下次需求", "step": "先写示例", "success": "减少返工",
                     "review_date": "2026-11-13", "evidence_ids": ["codex:allowed"]}], "lessons": ["先澄清验收"], "growth_focus": "需求澄清"})
            cfg = {"timezone": "Asia/Shanghai", "model": {"chunk_chars": 20000}}
            p = Policy({"codex_ids": ["b"]})
            build_report(s, cfg, p, "2026-11-10", Path(t), model)
            build_report(s, cfg, p, "2026-11-10", Path(t), model)
            self.assertNotIn("SECRET", "".join(seen))
            self.assertEqual(len(s.actions(p)), 1)
            s.close()
            s = Store(Path(t) / "db.sqlite")
            self.assertEqual(len(s.actions(p)), 1)
            self.assertEqual(s.actions(Policy({"codex_ids": ["a", "b"]})), [])
            s.close()

    def test_model_failure_writes_fact_draft_and_not_success(self):
        with tempfile.TemporaryDirectory() as t:
            s = Store(Path(t) / "db.sqlite")
            s.put(make_event("git", "a", "2026-11-10T01:00:00+08:00", "Repo", "Committed"))
            def fail(prompt):
                raise RuntimeError("Unavailable")
            result = build_report(s, {"timezone": "Asia/Shanghai", "model": {}}, Policy({}), "2026-11-10", Path(t), fail)
            self.assertFalse(result["ok"])
            self.assertIn("未完成 AI 分析", Path(result["path"]).read_text())
            self.assertIsNone(s.db.execute("SELECT data FROM reports").fetchone())
            s.close()

    def test_chunks_cover_oversized_event(self):
        e = {"id": "e", "body": "x" * 12000}
        parts = chunks([e], 4000)
        self.assertGreater(len(parts), 2)
        self.assertEqual(sum(len(x["body"]) for part in parts for x in part), 12000)

    def test_due_days_respects_timezone_and_catchup(self):
        cfg = {"timezone": "Asia/Shanghai", "schedule": {"hour": 10, "minute": 0, "catchup_days": 3}}
        now = datetime.fromisoformat("2026-11-10T02:05:00+00:00")
        self.assertEqual(due_days(cfg, now, "2026-11-08"), ["2026-11-09"])
        before = datetime.fromisoformat("2026-11-10T01:05:00+00:00")
        self.assertEqual(due_days(cfg, before, "2026-11-08"), [])
        self.assertEqual(due_days(cfg, now, "2026-11-06"), ["2026-11-07", "2026-11-08", "2026-11-09"])

    def test_first_run_respects_start_date(self):
        cfg = {"timezone": "Asia/Shanghai", "schedule": {"hour": 10, "minute": 0, "catchup_days": 3, "start_date": "2026-11-09"}}
        before = datetime.fromisoformat("2026-11-10T01:59:59+00:00")
        due = datetime.fromisoformat("2026-11-10T02:00:00+00:00")
        self.assertEqual(due_days(cfg, before, None), [])
        self.assertEqual(due_days(cfg, due, None), ["2026-11-09"])

    def test_long_outage_does_not_skip_old_dates(self):
        cfg = {"timezone": "Asia/Shanghai", "schedule": {"hour": 10, "minute": 0, "catchup_days": 3}}
        now = datetime.fromisoformat("2026-11-10T14:05:00+00:00")
        self.assertEqual(due_days(cfg, now, "2026-11-01"), ["2026-11-02", "2026-11-03", "2026-11-04"])

    def test_failed_sources_remain_visible_in_report(self):
        with tempfile.TemporaryDirectory() as t:
            s = Store(Path(t)/'db.sqlite')
            s.run_status('2026-11-10', 'browser', 'error', 0, 'permission')
            def model(prompt): return json.dumps({'report_markdown': report_body('无完整证据'), 'actions': [], 'lessons': [], 'growth_focus': ''})
            r = build_report(s, {'timezone': 'Asia/Shanghai', 'model': {}}, Policy({}), '2026-11-10', Path(t), model)
            self.assertNotIn('browser: error', Path(r['path']).read_text())
            self.assertIn('browser: error', Path(r['sources_path']).read_text())
            s.close()

    def test_large_event_full_text_is_preserved_despite_legacy_cap(self):
        with tempfile.TemporaryDirectory() as t:
            s=Store(Path(t)/'db.sqlite')
            s.put(make_event('codex','huge','2026-11-10T01:00:00+08:00','large','a'*5000))
            seen=[]
            def model(prompt):
                seen.append(prompt)
                return json.dumps({'report_markdown':report_body('有限分析'),'actions':[],'lessons':[],'growth_focus':''})
            r=build_report(s,{'timezone':'Asia/Shanghai','model':{'max_event_chars':1000}},Policy({}),'2026-11-10',Path(t),model)
            self.assertNotIn('中段省略',seen[0])
            self.assertIn('a'*5000,seen[0])
            self.assertNotIn('1 条超长记录',Path(r['path']).read_text())
            self.assertNotIn('1 条超长记录',Path(r['sources_path']).read_text())
            self.assertEqual(len(s.events('2026-11-10','Asia/Shanghai',Policy({}))[0]['body']),5000)
            s.close()

    def test_same_evidence_reuses_report_without_model_call(self):
        with tempfile.TemporaryDirectory() as t:
            s=Store(Path(t)/'db.sqlite');calls=[]
            s.put(make_event('manual','fact','2026-11-10T01:00:00+08:00','work','fact'))
            def model(prompt):
                calls.append(prompt)
                return json.dumps({'report_markdown':report_body('报告'),'actions':[],'lessons':[],'growth_focus':''})
            cfg={'timezone':'Asia/Shanghai','model':{}}
            build_report(s,cfg,Policy({}),'2026-11-10',Path(t),model)
            result=build_report(s,cfg,Policy({}),'2026-11-10',Path(t),model)
            self.assertEqual(len(calls),1);self.assertTrue(result['reused']);s.close()

    def test_old_failures_do_not_starve_new_daily_run(self):
        self.assertEqual(pending_days(['2026-11-10'],['2026-11-01','2026-11-02','2026-11-03'],3),
                         ['2026-11-10','2026-11-01','2026-11-02'])

    def test_action_feedback_invalidates_report_cache(self):
        with tempfile.TemporaryDirectory() as t:
            s=Store(Path(t)/'db.sqlite');p=Policy({});calls=[]
            s.put(make_event('manual','fact','2026-11-10T01:00:00+08:00','work','fact'))
            action={'title':'experiment','trigger':'next task','step':'check','success':'less rework','review_date':'2026-11-11','evidence_ids':['manual:x']}
            s.db.execute('INSERT INTO actions(id,day,fingerprint,data) VALUES(?,?,?,?)',('a','2026-11-09',p.fingerprint,json.dumps(action)));s.db.commit()
            def model(prompt):
                calls.append(prompt);return json.dumps({'report_markdown':report_body('报告'),'actions':[],'lessons':[],'growth_focus':''})
            cfg={'timezone':'Asia/Shanghai','model':{}}
            build_report(s,cfg,p,'2026-11-10',Path(t),model)
            s.db.execute("UPDATE actions SET status='done',note='有效'");s.db.commit()
            build_report(s,cfg,p,'2026-11-10',Path(t),model)
            self.assertEqual(len(calls),2);self.assertIn('有效',calls[-1]);s.close()


    def test_report_body_has_four_modules_without_source_dump(self):
        with tempfile.TemporaryDirectory() as t:
            s=Store(Path(t)/'db.sqlite')
            s.put(make_event('codex','trace-id','2026-11-10T01:00:00+08:00','work','RAW_DEBUG_LOG'))
            s.run_status('2026-11-10','codex','ok',1)
            def model(prompt):
                return json.dumps({'report_markdown':report_body().replace('\n\n','\n'),'actions':[],'lessons':[],'growth_focus':''})
            r=build_report(s,{'timezone':'Asia/Shanghai','model':{}},Policy({}),'2026-11-10',Path(t),model)
            body=Path(r['path']).read_text()
            self.assertEqual(body.count('\n## '),4)
            for section in ['📌 今日关键战报','💡 经验与教训沉淀','🚀 工作流提效方案（SOP / 工具 / 流程重构）','🤖 AI 使用方式总结与提升']:
                self.assertIn('## '+section+'\n\n',body)
            self.assertNotIn('## 来源索引',body)
            self.assertNotIn('codex:trace-id',body)
            self.assertIn('codex:trace-id',Path(r['sources_path']).read_text())
            s.close()

    def test_no_events_returns_short_result_without_model_or_advice(self):
        with tempfile.TemporaryDirectory() as t:
            s=Store(Path(t)/'db.sqlite')
            def model(prompt): self.fail('Empty day must not call the model')
            r=build_report(s,{'timezone':'Asia/Shanghai','model':{}},Policy({}),'2026-11-10',Path(t),model)
            body=Path(r['path']).read_text()
            self.assertTrue(r['ok']);self.assertLess(len(body),200)
            self.assertIn('没有可复盘的记录',body)
            self.assertNotIn('建议',body);self.assertEqual(s.actions(Policy({})),[]);s.close()

    def test_model_failure_does_not_dump_raw_records_into_report(self):
        with tempfile.TemporaryDirectory() as t:
            s=Store(Path(t)/'db.sqlite')
            s.put(make_event('codex','raw','2026-11-10T01:00:00+08:00','task','RAW_LOG_'*3000))
            def model(prompt): raise RuntimeError('Unavailable')
            r=build_report(s,{'timezone':'Asia/Shanghai','model':{}},Policy({}),'2026-11-10',Path(t),model)
            body=Path(r['path']).read_text()
            self.assertFalse(r['ok']);self.assertLess(len(body),500)
            self.assertNotIn('RAW_LOG_',body);s.close()

    def test_report_accepts_content_based_headings_and_order(self):
        for body in ('## 今日成果\n\n完成文案修正。',
                     '已完成文案修正，页面显示已确认。',
                     '### AI协作\n\n暂无\n\n### 工作结果\n\n完成文案修正。',
                     report_body().split('## 🤖 AI 使用方式总结与提升')[0],
                     report_body().replace('## 📌 今日关键战报','## 📌 工作结果')+'\n\n## 待确认事项\n\n暂无'):
            with self.subTest(body=body):
                result=parse_report(json.dumps({'report_markdown':body,'actions':[],'lessons':[]}),set())
                self.assertEqual(result['report_markdown'],body.strip())

    def test_ai_usage_accepts_summary_or_explicit_empty(self):
        for content in ('暂无', '你已将问题链接直接交给Codex读取；下次可复用同样的输入方式。'):
            with self.subTest(content=content):
                body=report_body(ai_usage=content)
                result=parse_report(json.dumps({'report_markdown':body,'lessons':[],'actions':[]}),set())
                self.assertTrue(result['report_markdown'].endswith(content))

    def test_flexible_structure_keeps_json_and_action_evidence_checks(self):
        for body in (None, '', '   '):
            with self.subTest(body=body),self.assertRaises(ValueError):
                parse_report(json.dumps({'report_markdown':body,'actions':[]}),set())
        with self.assertRaises(ValueError):
            parse_report('正文不是JSON',set())
        action={'title':'核验','trigger':'下次任务','step':'执行验证','success':'符合预期',
                'review_date':'2026-11-18','evidence_ids':['manual:missing']}
        with self.assertRaisesRegex(ValueError,'unsupported evidence'):
            parse_report(json.dumps({'report_markdown':'## 自定义成果\n完成工作。','actions':[action]}),set())
        with self.assertRaisesRegex(ValueError,'cannot invent'):
            parse_report(json.dumps({'report_markdown':'暂无可复盘的工作事项。','lessons':['无依据经验']}),set())

    def test_ai_usage_fact_is_audited_and_empty_ai_usage_is_not_a_claim(self):
        from daily_review.report import CLAIM_AUDIT
        for ai_usage in ('暂无', '用户已改用问题链接向Codex提交任务。'):
            with self.subTest(ai_usage=ai_usage),tempfile.TemporaryDirectory() as t:
                store=Store(Path(t)/'db.sqlite');self.addCleanup(store.close)
                quote='我已将Bug链接直接发给Codex，不再复制清单。'
                store.put(make_event('manual','ai-use','2026-11-10T10:00:00+08:00','用户反馈',quote))
                body=report_body('用户已改用问题链接向Codex提交任务。',ai_usage)
                calls=[]
                def model(prompt):
                    calls.append(prompt)
                    if prompt.startswith(CLAIM_AUDIT):
                        self.assertIn(ai_usage,prompt)
                        return json.dumps({'passed':True,'reason':'用户原话支持',
                            'claims':[{'text':'用户已改用问题链接向Codex提交任务。',
                                       'evidence_ids':['manual:ai-use'],'quote':quote}]})
                    return json.dumps({'report_markdown':body,'lessons':[],'actions':[]})
                result=build_report(store,{'timezone':'Asia/Shanghai','model':{'audit_reports':True}},
                                    Policy({}),'2026-11-10',Path(t)/'reports',model)
                self.assertTrue(result['ok'],result)
                self.assertEqual(len(calls),2)
                self.assertTrue(Path(result['path']).read_text().split('[查看来源')[0].strip().endswith(ai_usage))

    def test_all_empty_module_boundaries_pass_audit_without_invented_claims(self):
        from daily_review.report import CLAIM_AUDIT
        with tempfile.TemporaryDirectory() as t:
            store=Store(Path(t)/'db.sqlite');self.addCleanup(store.close)
            store.put(make_event('manual','noise','2026-11-10T10:00:00+08:00','记录','无关通知'))
            body='### AI协作\n\n暂无\n\n# 工作结果\n\n暂无'
            def model(prompt):
                return json.dumps({'passed':True,'reason':'没有事实断言','claims':[]} if prompt.startswith(CLAIM_AUDIT)
                                  else {'report_markdown':body,'lessons':[],'actions':[]})
            result=build_report(store,{'timezone':'Asia/Shanghai','model':{'audit_reports':True}},
                                Policy({}),'2026-11-10',Path(t)/'reports',model)
            self.assertTrue(result['ok'],result)

    def test_legacy_ai_lessons_do_not_reenter_personal_history(self):
        from daily_review.report import PERSONAL_LESSONS_VERSION
        with tempfile.TemporaryDirectory() as t:
            s=Store(Path(t)/'db.sqlite');self.addCleanup(s.close);p=Policy({});seen=[]
            s.put(make_event('manual','work','2026-11-10T01:00:00+08:00','work','已完成工作'))
            for day,data in [('2026-11-08',{'lessons':['OLD_AI_METHOD_ONLY']}),
                             ('2026-11-09',{'lessons':['PERSONAL_BEHAVIOR_EVIDENCE'],'personal_lessons_version':PERSONAL_LESSONS_VERSION})]:
                s.db.execute('INSERT INTO reports VALUES(?,?,?)',(day,p.fingerprint,json.dumps(data)))
            s.db.commit()
            def model(prompt):
                seen.append(prompt);return json.dumps({'report_markdown':report_body(),'actions':[],'lessons':[],'growth_focus':''})
            result=build_report(s,{'timezone':'Asia/Shanghai','model':{}},p,'2026-11-10',Path(t),model)
            self.assertTrue(result['ok']);self.assertNotIn('OLD_AI_METHOD_ONLY',seen[0]);self.assertIn('PERSONAL_BEHAVIOR_EVIDENCE',seen[0])
            saved=json.loads(s.db.execute('SELECT data FROM reports WHERE day=?',('2026-11-10',)).fetchone()[0])
            self.assertEqual(saved['personal_lessons_version'],PERSONAL_LESSONS_VERSION)


if __name__ == "__main__":
    unittest.main()
