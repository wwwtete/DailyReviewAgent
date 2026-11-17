import json
import tempfile
import unittest
from pathlib import Path
from daily_review.core import Store,Policy,make_event
from daily_review import research


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.store=Store(self.root/'db');self.addCleanup(self.store.close)
        self.cfg={'_root':str(self.root),'timezone':'Asia/Shanghai','model':{},'collection_enabled':True,'sources':{}}
        self.e=make_event('git','seed','2026-11-11T12:00:00+08:00','Android工作','用户重复说明进入测试页路径',path=str(self.root))
        self.store.put(self.e)

    def test_worker_reads_source_prepares_and_reviews_actual_artifact(self):
        (self.root/'README.md').write_text('入口：连接 IM，连接成功后进入 MeetingCall。')
        seen=[]
        responses=[{'opportunities':[{'kind':'efficiency','title':'自动发现验收导航','task_key':'navigation',
                     'goal':'省掉重复口述路径','evidence_ids':['git:seed']}]},
                   {'tool':'read_file','args':{'path':str(self.root/'README.md')}},
                   {'result':{'level':'B','summary':'从源码入口自动执行验收','scenario':'开发完成',
                    'root_cause':'用户重复说明路径','steps':['AI查README','AI等连接成功后导航并验收'],
                    'carrier':'Prompt','owners':{'build':'AI','execute':'AI','verify':'AI'},'user_cost':'一次性设置',
                    'benefit':'减少重复说明，未实测','limitations':'未执行设备验收',
                    'artifact':{'name':'flow.md','content':'连接IM→等待成功→MeetingCall；AI执行并断言，失败停止。'}}},
                   {'passed':True,'reason':'事实/项目适配与Prompt结构审查通过；未实测设备'}]
        def model(prompt):
            seen.append(prompt);value=responses.pop(0)
            if 'opportunities' in value:
                data=json.loads(prompt.split('INPUT_JSON'+chr(10),1)[1])
                value['reviewed_part_ids']=[p['part_id'] for p in data['parts']]
            return json.dumps(value)
        result=research.work(self.cfg,self.store,Policy({}),'2026-11-11',model)
        self.assertEqual(result['status'],'ready')
        self.assertIn('连接 IM',seen[2]);self.assertFalse(responses)
        items=research.ready(self.store,self.cfg,Policy({}),'2026-11-12')
        self.assertTrue(Path(items[0]['artifact_path']).exists())
        audit=Path(items[0]['artifact_path']).parent/'research-audit.json'
        self.assertEqual(json.loads(audit.read_text())['tools'][0]['tool'],'read_file')
        again=research.work(self.cfg,self.store,Policy({}),'2026-11-11',lambda p: self.fail('unchanged facts must not be rediscovered'))
        self.assertEqual(again['processed'],0)

    def test_failure_retries_without_erasing_opportunity(self):
        key=research.enqueue(self.store,self.cfg,Policy({}),{'kind':'efficiency','title':'自动化','task_key':'x',
            'goal':'减少重复','evidence_ids':['git:seed']},[self.e],'2026-11-11')
        self.store.db.execute('INSERT INTO research_seen VALUES(?,?)',(self.e['id'],research.seen_signature(self.e)));self.store.db.commit()
        def fail(prompt): raise RuntimeError('network down')
        result=research.work(self.cfg,self.store,Policy({}),'2026-11-11',fail)
        self.assertGreater(result['retry_in'],0)
        item=research.list_items(self.store)[0]
        self.assertEqual(item['id'],key);self.assertEqual(item['status'],'pending');self.assertEqual(item['attempts'],1)

    def test_malformed_json_gets_one_protocol_repair(self):
        from daily_review.research import _json_model
        calls=[]
        def model(prompt):
            calls.append(prompt)
            return '{"stop":"暂不具备条件"}\n{"extra":true}' if len(calls)==1 else '{"stop":"暂不具备条件"}'
        value=_json_model(model,'研究请求')
        self.assertEqual(json.loads(value), {'stop':'暂不具备条件'})
        self.assertEqual(len(calls),2)

    def test_repeated_malformed_json_stops_and_preserves_failure_response(self):
        from daily_review.research import _json_model
        calls=[]
        raw='{"tool":"read_file"}{"tool":"read_file"}'
        def model(prompt): calls.append(prompt);return raw
        with self.assertRaises(json.JSONDecodeError):
            _json_model(model,'研究请求',str(self.root))
        self.assertEqual(len(calls),2)
        logs=list((self.root/'runtime/research-failures').glob('*.json'))
        self.assertEqual(len(logs),1)
        self.assertEqual(json.loads(logs[0].read_text())['response'],raw)

    def test_research_context_budget_is_checked_after_lossless_packing(self):
        from daily_review.research import _bounded_events
        from daily_review import source_facts
        events=[make_event('manual',str(i),'2026-11-11T12:00:00+08:00','事实','x'*1800,
                           path=str(self.root)) for i in range(180)]
        bounded=_bounded_events([events[0]],events,120000)
        packed=source_facts.pack(source_facts.fragments(bounded,8666))
        self.assertLessEqual(len(source_facts.encode(packed).encode('utf-8')),120000)
        self.assertEqual(bounded[0]['id'],events[0]['id'])
        self.assertLess(len(bounded),len(events))

    def test_large_direct_evidence_is_never_discarded_to_fit_budget(self):
        from daily_review.research import _bounded_events
        seed={**self.e,'body':'必须保留的直接证据。'*15000}
        self.assertEqual(_bounded_events([seed],[seed,self.e],1000),[seed])

    def test_launchd_research_uses_independent_process_and_lock(self):
        from unittest.mock import patch
        from daily_review.research_install import definition
        plist=definition(self.cfg)
        self.assertEqual(plist['ProgramArguments'],['/bin/bash',str(self.root/'run.sh'),'research-work'])
        self.assertEqual(plist['StartInterval'],600)
        self.assertNotIn('tick',plist['ProgramArguments'])

    def test_model_client_allows_host_research_protocol_without_direct_tools(self):
        from unittest.mock import patch
        from daily_review.report import model_call
        import io
        payloads=[]
        def send(request,timeout):
            payloads.append(json.loads(request.data))
            return io.BytesIO(json.dumps({'output':[{'type':'message','content':[{'type':'output_text','text':'{}'}]}]}).encode())
        with patch('daily_review.report.model_settings',return_value=('https://example.com','key','test-model')), patch('urllib.request.urlopen',side_effect=send):
            model_call({},'prompt',instructions='Request host tools through the documented JSON protocol.')
        self.assertEqual(payloads[0]['instructions'],'Request host tools through the documented JSON protocol.')

    def test_discovery_failure_does_not_starve_existing_research(self):
        key=research.enqueue(self.store,self.cfg,Policy({}),{'kind':'efficiency','title':'已有任务','task_key':'existing',
            'goal':'减少重复','evidence_ids':['git:seed']},[self.e],'2026-11-11')
        def model(prompt):
            if 'opportunities' in prompt: raise RuntimeError('discovery temporarily unavailable')
            return json.dumps({'stop':'研究确认已经采用'})
        result=research.work(self.cfg,self.store,Policy({}),'2026-11-11',model)
        self.assertEqual(research.list_items(self.store)[0]['status'],'stopped')
        self.assertIn('discovery_error',result)

    def test_checkpoint_preserves_tool_results_between_failed_attempts(self):
        (self.root/'README.md').write_text('连接IM后进入MeetingCall')
        key=research.enqueue(self.store,self.cfg,Policy({}),{'kind':'efficiency','title':'路径','task_key':'resume',
            'goal':'自动导航','evidence_ids':['git:seed']},[self.e],'2026-11-11')
        self.store.db.execute('INSERT INTO research_seen VALUES(?,?)',(self.e['id'],research.seen_signature(self.e)));self.store.db.commit()
        calls=[]
        def first(prompt):
            calls.append(prompt)
            if len(calls)==1:return json.dumps({'tool':'read_file','args':{'path':str(self.root/'README.md')}})
            raise RuntimeError('interrupted model request')
        research.work(self.cfg,self.store,Policy({}),'2026-11-11',first)
        self.store.db.execute('UPDATE research_items SET retry_at=0');self.store.db.commit()
        seen=[]
        def second(prompt): seen.append(prompt);return json.dumps({'stop':'已读取记录，无新收益'})
        research.work(self.cfg,self.store,Policy({}),'2026-11-11',second)
        self.assertIn('连接IM后进入MeetingCall',seen[0])

    def test_rejected_draft_is_saved_with_actionable_review_feedback(self):
        key=research.enqueue(self.store,self.cfg,Policy({}),{'kind':'efficiency','title':'验收','task_key':'review',
            'goal':'自动验收','evidence_ids':['git:seed']},[self.e],'2026-11-11')
        self.store.db.execute('INSERT INTO research_seen VALUES(?,?)',(self.e['id'],research.seen_signature(self.e)));self.store.db.commit()
        output=iter([{'result':{'level':'B','summary':'不完整草案'}},{'passed':False,'reason':'缺少执行责任与失败停止'}])
        result=research.work(self.cfg,self.store,Policy({}),'2026-11-11',lambda p:json.dumps(next(output)))
        item=research.list_items(self.store)[0]
        self.assertIn('缺少执行责任与失败停止',item['last_error'])
        self.assertEqual(item['checkpoint']['draft']['summary'],'不完整草案')
        self.store.db.execute('UPDATE research_items SET retry_at=0');self.store.db.commit()
        seen=[]
        def model(prompt):seen.append(prompt);return json.dumps({'stop':'无收益，停止'})
        research.work(self.cfg,self.store,Policy({}),'2026-11-11',model)
        self.assertIn('缺少执行责任与失败停止',seen[0]);self.assertIn('不完整草案',seen[0])
