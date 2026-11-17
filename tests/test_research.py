import json
import tempfile
import unittest
from pathlib import Path
from daily_review.core import Store, Policy, make_event
from daily_review import history, research


class ResearchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = Store(self.root/'events.sqlite'); self.addCleanup(self.store.close)
        self.config = {'_root': str(self.root), 'timezone':'Asia/Shanghai',
                       'sources':{'browser':{'enabled':False}}, 'model':{}}
        self.policy = Policy({})

    def event(self, ident, day, path='/repo/app', body='工作事实', source='git'):
        e = make_event(source, ident, day+'T12:00:00+08:00', ident, body, path=path, scope_id=ident)
        self.store.put(e); return e

    def test_recent_and_related_history_apply_window_identity_and_policy(self):
        a=self.event('recent','2026-11-11')
        self.event('old-related','2026-10-20')
        self.event('too-old','2026-10-01')
        self.event('unrelated','2026-10-21','/repo/other')
        self.event('disabled','2026-11-10',source='browser')
        self.event('blocked','2026-11-10','/repo/private')
        p=Policy({'paths':['/repo/private']})
        recent=history.recent(self.store,self.config,p,'2026-11-11',7)
        self.assertEqual([e['id'] for e in recent],['git:recent'])
        related=history.related(self.store,self.config,p,'2026-11-11',[a])
        self.assertEqual({e['id'] for e in related},{'git:recent','git:old-related'})

    def test_opportunity_is_durable_deduplicated_and_pending_not_publishable(self):
        a=self.event('one','2026-11-10')
        item={'kind':'efficiency','title':'自动验收入口','task_key':'navigation-test',
              'goal':'去掉用户重复描述路径','evidence_ids':[a['id']]}
        key=research.enqueue(self.store,self.config,self.policy,item,[a],'2026-11-11')
        self.assertEqual(key,research.enqueue(self.store,self.config,self.policy,item,[a],'2026-11-12'))
        self.assertEqual(len(research.list_items(self.store)),1)
        self.assertEqual(research.ready(self.store,self.config,self.policy,'2026-11-12'),[])

    def test_result_gate_checks_artifact_review_and_hash_then_rechecks_blacklist(self):
        a=self.event('one','2026-11-10')
        key=research.enqueue(self.store,self.config,self.policy,{'kind':'capability','title':'任务路径发现',
            'task_key':'nav','goal':'自动找到入口','evidence_ids':[a['id']]},[a],'2026-11-11')
        result={'level':'B','summary':'交付适配项目的提示模板','scenario':'开发完成后的设备验证',
            'root_cause':'用户重复提供路径','steps':['AI读取导航','AI执行并验证'],
            'carrier':'Prompt','owners':{'build':'AI','execute':'AI','verify':'AI'},
            'user_cost':'一次性检查','benefit':'减少重复说明，未量化','limitations':'尚未在设备上执行',
            'artifact':{'name':'verification-prompt.md','content':'''# Prompt

输入：任务。
AI读取导航，执行并保留证据。失败停止。'''}}
        with self.assertRaises(ValueError):
            research.finish(self.store,self.config,self.policy,key,result,{'passed':False,'reason':'没有验收条件'})
        research.finish(self.store,self.config,self.policy,key,result,{'passed':True,'reason':'Prompt结构及任务适配审查通过；未验证设备执行'})
        items=research.ready(self.store,self.config,self.policy,'2026-11-12'); self.assertEqual(len(items),1)
        self.assertEqual(research.ready(self.store,self.config,Policy({'paths':['/repo/app']}),'2026-11-12'),[])
        Path(items[0]['artifact_path']).write_text('tampered')
        self.assertEqual(research.ready(self.store,self.config,self.policy,'2026-11-12'),[])

    def test_tools_block_forbidden_paths_and_disabled_source_lookup(self):
        self.event('disabled','2026-11-10',source='browser')
        with self.assertRaises(ValueError):
            history.lookup(self.store,self.config,self.policy,'browser:disabled')
        denied=self.root/'private'; denied.mkdir(); (denied/'x.md').write_text('secret')
        with self.assertRaises(ValueError):
            research.read_project(denied/'x.md',[str(denied)],Policy({'paths':[str(denied)]}))

    def test_unverified_script_cannot_be_published_as_level_b(self):
        a=self.event('one','2026-11-10')
        key=research.enqueue(self.store,self.config,self.policy,{'kind':'efficiency','title':'脚本','task_key':'s','goal':'自动化','evidence_ids':[a['id']]},[a],'2026-11-11')
        result={'level':'B','summary':'x','scenario':'x','root_cause':'x','steps':['x'],
            'carrier':'python','owners':{'build':'AI','execute':'AI','verify':'AI'},'user_cost':'0',
            'benefit':'未实测','limitations':'仅编译','artifact':{'name':'a.py','content':'print(1)'}}
        with self.assertRaises(ValueError):
            research.finish(self.store,self.config,self.policy,key,result,{'passed':True,'reason':'编译成功'})

    def test_published_result_does_not_repeat_but_feedback_remains(self):
        a=self.event('one','2026-11-10')
        key=research.enqueue(self.store,self.config,self.policy,{'kind':'efficiency','title':'复用','task_key':'reuse','goal':'x','evidence_ids':[a['id']]},[a],'2026-11-11')
        result={'level':'A','summary':'x','scenario':'x','root_cause':'假设','steps':['AI先核实环境'],
            'carrier':'建议新增Prompt','owners':{'build':'AI','execute':'AI','verify':'AI'},
            'user_cost':'一次授权','benefit':'预期收益未测量','limitations':'缺少真实项目文件'}
        research.finish(self.store,self.config,self.policy,key,result,{'passed':True,'reason':'方案及阻碍审查'})
        research.publish(self.store,'2026-11-12',[key])
        self.assertEqual(research.ready(self.store,self.config,self.policy,'2026-11-13'),[])
        research.feedback(self.store,key,'stopped','不适用')
        self.assertEqual(research.list_items(self.store)[0]['feedback'],'不适用')

    def test_newly_denied_derived_content_cannot_be_published(self):
        a=self.event('one','2026-11-10')
        key=research.enqueue(self.store,self.config,self.policy,{'kind':'efficiency','title':'例子','task_key':'derived','goal':'x','evidence_ids':[a['id']]},[a],'2026-11-11')
        result={'level':'B','summary':'讨论PRIVATE_TOPIC','scenario':'x','root_cause':'x','steps':['x'],
            'carrier':'Prompt','owners':{'build':'AI','execute':'AI','verify':'AI'},'user_cost':'一次','benefit':'未测','limitations':'草案',
            'artifact':{'name':'a.md','content':'PRIVATE_TOPIC相关草案'}}
        research.finish(self.store,self.config,self.policy,key,result,{'passed':True,'reason':'审查通过'})
        self.assertEqual(research.ready(self.store,self.config,Policy({'names':['PRIVATE_TOPIC']}),'2026-11-12'),[])

    def test_changed_source_invalidates_prepared_result(self):
        a=self.event('one','2026-11-10')
        key=research.enqueue(self.store,self.config,self.policy,{'kind':'efficiency','title':'方法','task_key':'rev','goal':'x','evidence_ids':[a['id']]},[a],'2026-11-11')
        research.finish(self.store,self.config,self.policy,key,{'level':'A','summary':'x','scenario':'x','root_cause':'x','steps':['x'],
            'carrier':'方案','owners':{'build':'AI','execute':'AI','verify':'AI'},'user_cost':'一次','benefit':'未测','limitations':'缺环境'},
            {'passed':True,'reason':'方案审查'})
        self.event('one','2026-11-10',body='后续已纠正此结论')
        self.assertEqual(research.ready(self.store,self.config,self.policy,'2026-11-12'),[])

    def test_fetched_or_read_context_is_rechecked_for_policy_changes(self):
        a=self.event('one','2026-11-10')
        key=research.enqueue(self.store,self.config,self.policy,{'kind':'efficiency','title':'方案','task_key':'ctx','goal':'x','evidence_ids':[a['id']]},[a],'2026-11-11')
        item=research.list_items(self.store)[0]
        item['title']='PRIVATE_CONTEXT'
        self.assertFalse(research._permitted(self.store,self.config,Policy({'names':['PRIVATE_CONTEXT']}),item))

    def test_same_evidence_and_goal_kind_does_not_create_duplicate_opportunity(self):
        a=self.event('one','2026-11-10')
        first=research.enqueue(self.store,self.config,self.policy,{'kind':'efficiency','title':'原标题',
            'task_key':'nav-path','goal':'减少路径说明','evidence_ids':[a['id']]},[a],'2026-11-11')
        second=research.enqueue(self.store,self.config,self.policy,{'kind':'efficiency','title':'改写标题',
            'task_key':'different-wording','goal':'减少路径说明','evidence_ids':[a['id']]},[a],'2026-11-12')
        self.assertEqual(first,second);self.assertEqual(len(research.list_items(self.store)),1)

    def test_stale_ready_result_returns_to_pending_for_research(self):
        a=self.event('one','2026-11-10')
        key=research.enqueue(self.store,self.config,self.policy,{'kind':'efficiency','title':'方法','task_key':'stale',
            'goal':'x','evidence_ids':[a['id']]},[a],'2026-11-11')
        research.finish(self.store,self.config,self.policy,key,{'level':'A','summary':'x','scenario':'x','root_cause':'x','steps':['x'],
            'carrier':'方案','owners':{'build':'AI','execute':'AI','verify':'AI'},'user_cost':'一次','benefit':'未测','limitations':'环境不足'},
            {'passed':True,'reason':'方案审查'})
        self.event('one','2026-11-10',body='后续纠正')
        research.refresh(self.store,self.config,self.policy)
        self.assertEqual(research.list_items(self.store)[0]['status'],'pending')
