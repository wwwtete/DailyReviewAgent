import json
import tempfile
import unittest
from pathlib import Path
from daily_review.core import Store, Policy, make_event
from daily_review import research
from daily_review.report import build_report


class FidelityIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.store=Store(self.root/'db');self.addCleanup(self.store.close)
        self.cfg={'_root':str(self.root),'timezone':'Asia/Shanghai','model':{},'sources':{}}
        self.e=make_event('lark_messages','forward','2026-11-15T10:00:00+08:00','群',
                         '甲'*6500+'纠正：仅为AI建议，尚未实施。'+'乙'*6500,author={'name':'forwarder'})
        self.store.put(self.e)

    def test_daily_can_page_today_lark_beyond_old_cutoff(self):
        prompts=[]
        def model(prompt):
            prompts.append(prompt)
            if len(prompts)==1:return json.dumps({'request_evidence':[self.e['id']]})
            data=json.loads(prompt.split('补充原文证据：\n')[1].split('\n取证轮次：')[0])
            if len(prompts)==2:
                return json.dumps({'request_evidence':[{'id':self.e['id'],'cursor':data[-1]['next_cursor']}]})
            self.assertIn('纠正：仅为AI建议',data[-1]['text'])
            self.assertFalse(data[-1]['historical_not_daily_fact'])
            return json.dumps({'report_markdown':'暂无可复盘的工作事项。','actions':[],'lessons':[]})
        result=build_report(self.store,self.cfg,Policy({}),'2026-11-15',self.root/'reports',model)
        self.assertTrue(result['ok'],result);self.assertEqual(len(prompts),3)

    def test_daily_rejects_unsupported_completion_before_publish(self):
        cfg={**self.cfg,'model':{'audit_reports':True}}
        outputs=iter([{'report_markdown':'暂无可复盘的工作事项。','actions':[],'lessons':[]},
                      {'passed':False,'reason':'计划不能证明完成','claims':[]},
                      {'report_markdown':'暂无可复盘的工作事项。','actions':[],'lessons':[]},
                      {'passed':False,'reason':'计划不能证明完成','claims':[]}])
        result=build_report(self.store,cfg,Policy({}),'2026-11-15',self.root/'reports',lambda p:json.dumps(next(outputs)))
        self.assertFalse(result['ok']);self.assertIn('claim audit',result['detail'])
        self.assertIsNone(self.store.db.execute('SELECT data FROM reports').fetchone())

    def test_legacy_seen_does_not_hide_unread_tail_and_resumes_by_part(self):
        research.init(self.store)
        self.store.db.execute('INSERT INTO research_seen VALUES(?,?)',(self.e['id'],research.digest(self.e)))
        self.store.db.commit();seen=[]
        def model(prompt):
            data=json.loads(prompt.split('INPUT_JSON\n',1)[1]);seen.extend(data['parts'])
            return json.dumps({'opportunities':[], 'reviewed_part_ids':[p['part_id'] for p in data['parts']]})
        for _ in range(4):research.work(self.cfg,self.store,Policy({}),'2026-11-15',model)
        self.assertTrue(any('纠正：仅为AI建议' in p['body'] for p in seen))
        self.assertEqual(''.join(p['body'] for p in seen),self.e['body'])
        self.assertEqual(len({p['part_id'] for p in seen}),len(seen))

    def test_discovery_without_complete_fragment_ack_not_marked_seen(self):
        research.work(self.cfg,self.store,Policy({}),'2026-11-15',lambda p:json.dumps({'opportunities':[]}))
        self.assertIsNone(self.store.db.execute('SELECT signature FROM research_seen WHERE id=?',(self.e['id'],)).fetchone())

    def test_claim_audit_can_repair_nonliteral_quote_once(self):
        from daily_review.report import SECTIONS
        cfg={**self.cfg,'model':{'audit_reports':True}}
        body='\n\n'.join('## '+section+'\n\n'+('仅为AI建议，尚未实施。' if i==0 else '暂无。') for i,section in enumerate(SECTIONS))
        count=[]
        def model(prompt):
            count.append(prompt)
            if len(count)==1:return json.dumps({'report_markdown':body,'actions':[],'lessons':[]})
            return json.dumps({'passed':True,'reason':'逐项检查通过','claims':[{'text':'仅为AI建议，尚未实施。',
                'evidence_ids':[self.e['id']],'quote':'仅为…未实施' if len(count)==2 else '仅为AI建议，尚未实施。'}]})
        result=build_report(self.store,cfg,Policy({}),'2026-11-15',self.root/'reports',model)
        self.assertTrue(result['ok'],result);self.assertEqual(len(count),3)

    def test_nonempty_report_cannot_pass_empty_claim_audit(self):
        from daily_review.report import SECTIONS
        cfg={**self.cfg,'model':{'audit_reports':True}}
        body='\n\n'.join('## '+section+'\n\n已完成上线。' for section in SECTIONS)
        def model(prompt):
            if prompt.startswith('你是日报证据审查者'):return json.dumps({'passed':True,'reason':'空列表','claims':[]})
            return json.dumps({'report_markdown':body,'actions':[],'lessons':[]})
        result=build_report(self.store,cfg,Policy({}),'2026-11-15',self.root/'reports',model)
        self.assertFalse(result['ok'])

    def test_failed_semantic_audit_regenerates_once_then_reaudits(self):
        cfg={**self.cfg,'model':{'audit_reports':True}}
        outputs=iter([{'report_markdown':'暂无可复盘的工作事项。','actions':[],'lessons':[]},
                      {'passed':False,'reason':'团队贡献不能归为本人','claims':[]},
                      {'report_markdown':'暂无可复盘的工作事项。','actions':[],'lessons':[]},
                      {'passed':True,'reason':'重生成后无个人成果断言','claims':[]}])
        result=build_report(self.store,cfg,Policy({}),'2026-11-15',self.root/'reports',lambda p:json.dumps(next(outputs)))
        self.assertTrue(result['ok'],result)

    def test_unread_research_tail_survives_rolling_horizon(self):
        def model(prompt):
            data=json.loads(prompt.split('INPUT_JSON\n',1)[1])
            return json.dumps({'opportunities':[],'reviewed_part_ids':[p['part_id'] for p in data['parts']]})
        # Force a one-part batch by making metadata large, without discarding it.
        e={**self.e,'author':{'name':'x'*16000},'body':'甲'*6000+'TAIL_CORRECTION'}
        self.store.put(e)
        first=research.work(self.cfg,self.store,Policy({}),'2026-11-15',model)
        seen=[]
        def later(prompt):
            data=json.loads(prompt.split('INPUT_JSON\n',1)[1]);seen.extend(data['parts'])
            return json.dumps({'opportunities':[],'reviewed_part_ids':[p['part_id'] for p in data['parts']]})
        research.work(self.cfg,self.store,Policy({}),'2026-12-01',later)
        self.assertTrue(any('TAIL_CORRECTION' in p['body'] for p in seen))

    def test_audit_exhaustion_never_publishes_unreviewed_revision(self):
        cfg={**self.cfg,'model':{'audit_reports':True}}
        count=[]
        def model(prompt):
            count.append(prompt)
            if not prompt.startswith('你是日报证据审查者'):
                return json.dumps({'report_markdown':'暂无可复盘的工作事项。','actions':[],'lessons':[]})
            if len(count)<5:
                return json.dumps({'passed':True,'reason':'fake quote','claims':[{'text':'暂无可复盘的工作事项。',
                                    'evidence_ids':[self.e['id']],'quote':'NOT_IN_SOURCE'}]})
            return json.dumps({'passed':False,'reason':'语义仍有错误','claims':[]})
        result=build_report(self.store,cfg,Policy({}),'2026-11-15',self.root/'reports',model)
        self.assertFalse(result['ok'])

    def test_codex_original_quote_is_checked_as_decoded_evidence(self):
        from daily_review.report import SECTIONS
        e={**self.e,'id':'codex:archived-replay','source':'codex','scope_id':'session','archive':True,
           'body':json.dumps({'fact':{'kind':'user_feedback','status':'observed',
             'text':'用户纠正AI方案尚未验证','evidence':[{'id':'u','quote':'我说“尚未实施”\n请先验证'}]},
             'experience_candidates':[],'evidence_roles':['user'],'user_evidence':['我说“尚未实施”\n请先验证']},ensure_ascii=False)}
        self.store.put(e)
        body='\n\n'.join('## '+section+'\n\n'+('用户纠正AI方案尚未验证。' if i==0 else '暂无。') for i,section in enumerate(SECTIONS))
        def model(prompt):
            if prompt.startswith('你是日报证据审查者'):
                return json.dumps({'passed':True,'reason':'用户反馈有原话','claims':[{'text':'用户纠正AI方案尚未验证。',
                    'evidence_ids':[e['id']],'quote':'我说“尚未实施”\n请先验证'}]})
            return json.dumps({'report_markdown':body,'actions':[],'lessons':[]})
        result=build_report(self.store,{**self.cfg,'model':{'audit_reports':True}},Policy({}),'2026-11-15',self.root/'reports',model)
        self.assertTrue(result['ok'],result)
