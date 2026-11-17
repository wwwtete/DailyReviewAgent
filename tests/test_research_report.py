import json
import tempfile
import unittest
from pathlib import Path
from daily_review.core import Store,Policy,make_event
from daily_review.report import build_report
from daily_review import research


def body(text='昨天暂无工作记录'):
    return ('## 📌 今日关键战报\n\n'+text+'\n\n## 💡 经验与教训沉淀\n\n暂无。\n\n'
            '## 🚀 工作流提效方案（SOP / 工具 / 流程重构）\n\n暂无。\n\n'
            '## 🤖 AI 使用方式总结与提升\n\n暂无')


class ResearchReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.store=Store(self.root/'db');self.addCleanup(self.store.close)
        self.cfg={'_root':str(self.root),'timezone':'Asia/Shanghai','model':{},'sources':{'browser':{'enabled':False}}}
        self.policy=Policy({})
        self.old=make_event('git','old','2026-11-09T12:00:00+08:00','旧任务','OLD_TASK_FACT',path='/repo/app')
        self.store.put(self.old)

    def result(self):
        key=research.enqueue(self.store,self.cfg,self.policy,{'kind':'efficiency','title':'自动导航',
            'task_key':'nav','goal':'减少重复说明','evidence_ids':['git:old']},[self.old],'2026-11-10')
        research.finish(self.store,self.cfg,self.policy,key,{'level':'B','summary':'自动推导路径模板',
            'scenario':'开发验收','root_cause':'重复提供路径','steps':['AI读导航','AI执行并验收'],
            'carrier':'Prompt','owners':{'build':'AI','execute':'AI','verify':'AI'},'user_cost':'一次性确认',
            'benefit':'减少重复说明，未量化','limitations':'仅草案审查，未验证设备',
            'artifact':{'name':'prompt.md','content':'输入任务；AI查导航；执行目标路径并断言；失败停止保留证据。'}},
            {'passed':True,'reason':'文档适配通过，未实际设备验证'})
        return key

    def test_empty_day_can_publish_mature_historical_result(self):
        key=self.result();seen=[]
        def model(prompt):
            seen.append(prompt)
            return json.dumps({'report_markdown':body(),'lessons':[],'actions':[],'research_used_ids':[key]})
        r=build_report(self.store,self.cfg,self.policy,'2026-11-11',self.root/'reports',model)
        self.assertTrue(r['ok']);self.assertEqual(len(seen),1)
        self.assertIn('OLD_TASK_FACT',seen[0])
        self.assertIn('mature_research',seen[0])
        self.assertEqual(research.list_items(self.store)[0]['published_day'],'2026-11-11')

    def test_pending_research_does_not_block_daily_report(self):
        research.enqueue(self.store,self.cfg,self.policy,{'kind':'efficiency','title':'慢研究',
            'task_key':'slow','goal':'x','evidence_ids':['git:old']},[self.old],'2026-11-10')
        e=make_event('git','now','2026-11-11T12:00:00+08:00','工作','TODAY_FACT',path='/repo/app');self.store.put(e)
        seen=[]
        def model(prompt):
            seen.append(prompt);return json.dumps({'report_markdown':body('昨天完成开发'),'lessons':[],'actions':[]})
        r=build_report(self.store,self.cfg,self.policy,'2026-11-11',self.root/'reports',model)
        self.assertTrue(r['ok']);self.assertEqual(len(seen),1)
        self.assertNotIn('慢研究',seen[0])

    def test_mature_research_changes_invalidate_report_cache(self):
        key=self.result()
        self.store.put(make_event('manual','now','2026-11-11T12:00:00+08:00','工作','当前工作'))
        calls=[]
        def model(prompt):
            calls.append(prompt);return json.dumps({'report_markdown':body(),'lessons':[],'actions':[]})
        one=build_report(self.store,self.cfg,self.policy,'2026-11-11',self.root/'reports',model)
        two=build_report(self.store,self.cfg,self.policy,'2026-11-11',self.root/'reports',model)
        self.assertTrue(two.get('reused'))
        research.feedback(self.store,key,'stopped','已经采用，无需再推荐')
        three=build_report(self.store,self.cfg,self.policy,'2026-11-11',self.root/'reports',model)
        self.assertFalse(three.get('reused',False));self.assertEqual(len(calls),2)

    def test_research_output_cannot_claim_unprovided_result(self):
        self.store.put(make_event('manual','now','2026-11-11T12:00:00+08:00','工作','当前工作'))
        def model(prompt): return json.dumps({'report_markdown':body(),'lessons':[],'actions':[],'research_used_ids':['invented']})
        r=build_report(self.store,self.cfg,self.policy,'2026-11-11',self.root/'reports',model)
        self.assertFalse(r['ok'])

    def test_publishing_result_keeps_identical_report_cache_valid(self):
        key=self.result();calls=[]
        def model(prompt):
            calls.append(prompt);return json.dumps({'report_markdown':body(),'lessons':[],'actions':[],'research_used_ids':[key]})
        build_report(self.store,self.cfg,self.policy,'2026-11-11',self.root/'reports',model)
        two=build_report(self.store,self.cfg,self.policy,'2026-11-11',self.root/'reports',model)
        self.assertTrue(two.get('reused'));self.assertEqual(len(calls),1)

    def test_historical_evidence_can_be_requested_without_becoming_daily_fact(self):
        key=self.result();calls=[]
        def model(prompt):
            calls.append(prompt)
            if len(calls)==1:return json.dumps({'request_evidence':['git:old']})
            self.assertIn('OLD_TASK_FACT',prompt)
            return json.dumps({'report_markdown':body(),'lessons':[],'actions':[],'research_used_ids':[key]})
        result=build_report(self.store,self.cfg,self.policy,'2026-11-11',self.root/'reports',model)
        self.assertTrue(result['ok']);self.assertEqual(result['events'],0);self.assertEqual(len(calls),2)
