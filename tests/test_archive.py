import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from daily_review.core import Policy, Store, make_event
from daily_review import report
try:
    from daily_review import archive
except ImportError:
    archive = None


class ArchiveTests(unittest.TestCase):
    def test_rotated_session_recovers_legacy_cursor_and_all_files_once(self):
        archive.init(self.store)
        self.message('old pending work', role='user')
        raw = self.log.read_bytes(); st = self.log.stat()
        record = archive.record_from_line(json.loads(raw), 't1', 0, len(raw), '', 1)
        meta = {'source':'codex','scope_id':'t1','title':'Work','path':'/work','transcript_path':str(self.log)}
        state = {'offset':len(raw),'line':1,'turn':'','pending':[record],
                 'identity':[st.st_dev,st.st_ino],'meta':meta}
        self.store.db.execute('INSERT INTO codex_cursors VALUES(?,?)', ('t1',json.dumps(state)))
        self.store.db.commit()
        self.message('old final verification'); self.finish()
        archived = self.home/'archived_sessions'; archived.mkdir()
        self.log.rename(archived/'rollout-t1-old.jsonl')
        self.message('new corrected result'); self.finish()
        first = self.run_worker()
        self.assertEqual(first['scan_errors'],0)
        body = ' '.join(e['body'] for e in self.events())
        for text in ('old pending work','old final verification','new corrected result'):
            self.assertIn(text,body)
        jobs = self.store.db.execute('SELECT count(*) FROM codex_jobs').fetchone()[0]
        self.run_worker()
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM codex_jobs').fetchone()[0],jobs)
        self.assertEqual(archive.coverage(self.store,'2026-11-09',Policy({}))['status'],'ok')
        for event in self.events():
            evidence = archive.evidence(self.store,event['id'],Policy({}))
            self.assertTrue(Path(evidence['transcript_path']).exists())

    def test_unseen_files_same_session_same_offset_are_all_archived(self):
        self.message('first file'); self.finish()
        archived = self.home/'archived_sessions'; archived.mkdir()
        self.log.rename(archived/'rollout-t1-first.jsonl')
        self.message('second file'); self.finish()
        self.run_worker()
        self.assertEqual(len(self.events()),2)
        refs = [archive.evidence(self.store,e['id'],Policy({}))['records'][0]['id'] for e in self.events()]
        self.assertEqual(len(set(refs)),2)

    def test_reused_path_does_not_steal_new_file_evidence(self):
        self.log=self.home/'a.jsonl'
        db=sqlite3.connect(self.home/'state_5.sqlite')
        db.execute('UPDATE threads SET rollout_path=?',(str(self.log),));db.commit();db.close()
        self.message('OLD_FILE');self.finish();self.run_worker()
        archived=self.home/'archived_sessions';archived.mkdir()
        old=archived/'rollout-t1-old.jsonl';self.log.rename(old)
        self.message('NEW_FILE');self.finish();self.run_worker()
        for event in self.events():
            ref=archive.evidence(self.store,event['id'],Policy({}))
            expected=old if 'OLD_FILE' in event['body'] else self.log
            self.assertEqual(Path(ref['transcript_path']),expected)

    def test_missing_transcript_reports_actionable_failure(self):
        self.log.unlink()
        result = self.run_worker()
        self.assertFalse(result['ok'])
        failure = self.store.get('codex_archive_scan')['errors'][0]
        self.assertTrue(failure['detail'])
        self.assertEqual(failure['path'],str(self.log))

    def test_renamed_pending_chat_blacklist_no_longer_blocks_coverage(self):
        from datetime import datetime, timezone
        at=datetime.now(timezone.utc).isoformat()
        self.message('pending private work',at)
        self.run_worker()
        db=sqlite3.connect(self.home/'state_5.sqlite')
        db.execute("UPDATE threads SET title='Private'"); db.commit();db.close()
        policy=Policy({'names':['Private']})
        self.run_worker(policy)
        day=datetime.now(archive.ZoneInfo('Asia/Shanghai')).date().isoformat()
        self.assertEqual(archive.coverage(self.store,day,policy)['status'],'ok')

    def test_matching_filename_other_session_header_is_not_read_as_this_session(self):
        archived=self.home/'archived_sessions';archived.mkdir()
        other=archived/'rollout-t1-other.jsonl'
        other.write_text(json.dumps({'type':'session_meta','payload':{'id':'other'}})+chr(10)+
                         json.dumps({'type':'response_item','timestamp':'2026-11-09T04:00:00Z',
                         'payload':{'type':'message','role':'user','content':[{'text':'WRONG_SESSION'}]}})+chr(10))
        self.message('real work');self.finish();self.run_worker()
        self.assertNotIn('WRONG_SESSION',''.join(self.prompts))

    def test_archive_wait_does_not_recollect_all_sources(self):
        from daily_review.cli import run_day
        self.message('pending');self.finish()
        archive.scan(self.cfg,self.store,Policy({}))
        self.cfg['sources']['lark']={}
        with patch('daily_review.cli.collect',side_effect=AssertionError('unnecessary recollection')):
            result=run_day(self.cfg,self.store,'2026-11-09')
        self.assertTrue(result['waiting_for_archive'])

    def setUp(self):
        self.assertIsNotNone(archive, 'Codex incremental archive is not implemented')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.log = self.root / 'rollout.jsonl'
        self.log.touch()
        self.home = self.root / 'codex'; self.home.mkdir()
        db = sqlite3.connect(self.home / 'state_5.sqlite')
        db.execute('CREATE TABLE threads(id,rollout_path,cwd,title,updated_at,project_id)')
        db.execute('INSERT INTO threads VALUES(?,?,?,?,?,?)', ('t1', str(self.log), '/work', 'Work', 9999999999, 'p1'))
        db.commit(); db.close()
        self.cfg = {'_root': str(self.root), 'collection_enabled': True, 'timezone': 'Asia/Shanghai',
                    'schedule': {'start_date': '2026-11-09'}, 'blacklist': {},
                    'sources': {'codex': {'enabled': True, 'home': str(self.home), 'mode': 'archive'}},
                    'model': {'chunk_chars': 12000}, 'archive': {'chunk_chars': 4000, 'max_jobs': 100}}
        self.store = Store(self.root / 'runtime/events.sqlite')
        self.addCleanup(self.store.close)
        self.prompts = []

    def write(self, kind, payload, at='2026-11-09T04:00:00Z'):
        with self.log.open('a') as f:
            f.write(json.dumps({'type': kind, 'payload': payload, 'timestamp': at}) + '\n')

    def message(self, body, at='2026-11-09T04:00:00Z', role='assistant'):
        self.write('response_item', {'type': 'message', 'role': role, 'phase': 'final_answer',
                                    'content': [{'type': 'output_text', 'text': body}]}, at)

    def finish(self, at='2026-11-09T04:00:00Z'):
        self.write('event_msg', {'type': 'task_complete', 'turn_id': 'turn1'}, at)

    def model(self, prompt):
        self.prompts.append(prompt)
        records = json.loads(prompt.split('\nINPUT_JSON\n')[1])['records']
        facts = []
        for r in records:
            facts.append({'kind': 'output', 'text': r['body'][:100], 'status': 'reported_complete',
                          'evidence': [{'id': r['id'], 'quote': r['body'][:100]}]})
        return json.dumps({'facts': facts, 'experience_candidates': []})

    def run_worker(self, policy=None, model=None):
        return archive.work(self.cfg, self.store, policy or Policy({}), model or self.model, force=True)

    def events(self, day='2026-11-09', policy=None):
        return archive.events(self.store, day, 'Asia/Shanghai', policy or Policy({}))

    def test_duplicate_scan_and_hook_do_not_repeat_facts(self):
        self.message('Fixed rotation; visual verification pending.'); self.finish()
        self.run_worker(); self.run_worker()
        archive.enqueue_hook(self.cfg, {'session_id': 't1', 'hook_event_name': 'Stop', 'turn_id': 'turn1'})
        self.run_worker()
        self.assertEqual(len(self.events()), 1)
        self.assertEqual(len(self.prompts), 1)

    def test_hooks_are_append_only_and_audited_when_consumed(self):
        self.message('First result'); self.finish()
        archive.enqueue_hook(self.cfg, {'session_id':'t1','hook_event_name':'Stop','turn_id':'turn1'})
        archive.enqueue_hook(self.cfg, {'session_id':'t1','hook_event_name':'SessionEnd','turn_id':'turn1'})
        queue = self.root/'runtime/codex-hook-queue'
        self.assertEqual(len(list(queue.glob('*.json'))),2)
        self.run_worker()
        rows=self.store.db.execute(
            'SELECT event,status,received_at,consumed_at FROM codex_hook_events ORDER BY event').fetchall()
        self.assertEqual([r[0] for r in rows],['SessionEnd','Stop'])
        self.assertTrue(all(r[1]=='consumed' and r[2] and r[3] for r in rows))

    def test_hook_after_turn_was_already_scanned_is_consumed_from_job_boundary(self):
        self.message('Completed before hook'); self.finish(); self.run_worker()
        archive.enqueue_hook(self.cfg,{'session_id':'t1','hook_event_name':'Stop','turn_id':'turn1'})
        self.run_worker()
        row=self.store.db.execute('SELECT status,consumed_at FROM codex_hook_events').fetchone()
        self.assertEqual(row[0],'consumed')
        self.assertGreater(row[1],0)

    def test_jobs_record_queue_start_completion_and_turn_boundary(self):
        self.message('Timed result'); self.finish(); self.run_worker()
        row=self.store.db.execute(
            'SELECT created_at,started_at,completed_at,turn_id,trigger_event FROM codex_jobs').fetchone()
        self.assertTrue(row[0] <= row[1] <= row[2])
        self.assertEqual(row[3],'turn1')
        self.assertEqual(row[4],'task_complete')

    def test_worker_drains_multiple_batches_in_one_wakeup(self):
        self.cfg['archive'].update({'max_jobs':1,'max_run_jobs':3,'max_run_seconds':60})
        for n in range(3):
            turn='turn'+str(n)
            self.write('event_msg',{'type':'task_started','turn_id':turn})
            self.message('Result '+str(n))
            self.write('event_msg',{'type':'task_complete','turn_id':turn})
        result=self.run_worker()
        self.assertEqual(result['done'],3)
        self.assertEqual(result['pending'],0)

    def test_scan_error_only_blocks_its_affected_day(self):
        import time
        archive.init(self.store)
        self.store.set('codex_archive_timezone','Asia/Shanghai')
        self.store.set('codex_archive_scan',{'at':time.time(),'errors':[
            {'session':'old','path':'old','error':'MissingTranscript','detail':'missing',
             'days':['2026-11-10']}]})
        current=archive.coverage(self.store,
            archive.datetime.now(archive.ZoneInfo('Asia/Shanghai')).date().isoformat(),Policy({}))
        old=archive.coverage(self.store,'2026-11-10',Policy({}))
        self.assertEqual(current['status'],'ok')
        self.assertEqual(old['status'],'partial')
        self.assertIn('relevant_scan_errors=1',old['detail'])

    def test_global_scan_error_blocks_every_target_day(self):
        import time
        archive.init(self.store)
        self.store.set('codex_archive_timezone','Asia/Shanghai')
        self.store.set('codex_archive_scan',{'at':time.time(),'errors':[
            {'session':'','path':'codex','error':'OperationalError','detail':'unavailable',
             'scope':'global','days':[]}]})
        self.assertEqual(archive.coverage(self.store,'2026-11-10',Policy({}))['status'],'partial')

    def test_missing_transcript_uses_pending_cursor_day_without_jobs(self):
        archive.init(self.store)
        self.message('Pending evidence', '2026-11-11T04:00:00Z', role='user')
        raw = self.log.read_bytes(); st = self.log.stat()
        record = archive.record_from_line(json.loads(raw), 't1', 0, len(raw), 'turn-pending', 1)
        meta = {'source':'codex','scope_id':'t1','title':'Work','path':'/work',
                'transcript_path':str(self.log)}
        state = {'offset':len(raw),'line':1,'turn':'turn-pending','pending':[record],
                 'identity':[st.st_dev,st.st_ino],'meta':meta,'record_prefix':'t1:missing'}
        self.store.db.execute('INSERT INTO codex_file_cursors VALUES(?,?,?)',
                              ('t1','missing-file',json.dumps(state)))
        self.store.db.commit(); self.log.unlink()

        self.run_worker()

        old = archive.coverage(self.store, '2026-11-10', Policy({}))
        affected = archive.coverage(self.store, '2026-11-11', Policy({}))
        self.assertEqual(old['relevant_scan_errors'], 0)
        self.assertGreater(affected['relevant_scan_errors'], 0)
        self.assertEqual(affected['status'], 'partial')

    def test_cross_midnight_deltas_do_not_rewrite_yesterday(self):
        self.message('Validation pending', '2026-11-09T15:59:00Z')
        self.message('Validation completed', '2026-11-09T16:01:00Z'); self.finish('2026-11-09T16:02:00Z')
        self.run_worker()
        self.assertIn('pending', self.events()[0]['body'])
        self.assertNotIn('completed', self.events()[0]['body'])
        self.assertIn('completed', self.events('2026-11-10')[0]['body'])

    def test_long_tool_result_middle_is_seen_and_citable(self):
        self.write('response_item', {'type': 'function_call_output', 'output': 'x'*9000+'ROOT_CAUSE_MIDDLE'+'y'*9000})
        self.finish(); self.run_worker()
        self.assertIn('ROOT_CAUSE_MIDDLE', ''.join(self.prompts))
        self.assertGreater(len(self.prompts), 3)

    def test_retry_after_model_failure_does_not_lose_checkpoint(self):
        self.message('A verified change'); self.finish()
        def fail(prompt): raise RuntimeError('Model unavailable')
        result = self.run_worker(model=fail)
        self.assertGreater(result['failed'], 0)
        self.assertEqual(self.events(), [])
        self.run_worker()
        self.assertEqual(len(self.events()), 1)

    def test_blacklist_before_open_and_filters_old_archives(self):
        self.message('Private outcome'); self.finish(); self.run_worker()
        self.assertEqual(self.events(policy=Policy({'codex_ids': ['t1']})), [])
        self.log.unlink()
        result = self.run_worker(Policy({'paths': ['/work']}))
        self.assertEqual(result['failed'], 0)
        self.assertEqual(archive.enqueue_hook(self.cfg, {'session_id': 't1', 'hook_event_name': 'Stop'}, Policy({'codex_ids': ['t1']}))['queued'], False)

    def test_new_blacklist_blocks_old_evidence_even_if_summary_omits_name(self):
        self.message('SECRET_NAME work completed'); self.finish(); self.run_worker()
        self.assertEqual(self.events(policy=Policy({'names': ['SECRET_NAME']})), [])

    def test_archive_rejects_advice_and_fabricated_citations(self):
        records = [{'id': 'e1', 'kind': 'assistant', 'body': 'fixed issue'}]
        invalid = [
            {'facts': [], 'experience_candidates': [], 'report_markdown': '## 今日关键战报'},
            {'facts': [{'kind': 'output', 'text': 'fixed', 'status': 'reported_complete', 'evidence': [{'id': 'e1', 'quote': 'fake quote'}]}], 'experience_candidates': []},
            {'facts': [], 'experience_candidates': [{'observation': '建议你每天休息', 'fact_indexes': [], 'evidence': []}]},
            {'facts': [{'kind': 'output', 'text': 'fixed', 'status': 'verified', 'evidence': [{'id': 'e1', 'quote': 'fixed issue'}]}], 'experience_candidates': []}
        ]
        for obj in invalid:
            with self.subTest(obj=obj), self.assertRaises(ValueError):
                archive.validate(json.dumps(obj), records)

    def test_no_work_creates_no_fake_lesson_or_daily_event(self):
        self.message('Thank you'); self.finish()
        self.run_worker(model=lambda p: '{"facts":[],"experience_candidates":[]}')
        self.assertEqual(self.events(), [])
        self.assertEqual(archive.coverage(self.store, '2026-11-09', Policy({}))['status'], 'ok')

    def test_partial_json_line_is_retried_after_completion(self):
        self.message('First fact'); self.finish()
        with self.log.open('a') as f: f.write('{"type":"response_item"')
        self.run_worker()
        with self.log.open('a') as f: f.write(',"timestamp":"2026-11-09T06:00:00Z","payload":{"type":"message","role":"assistant","content":[{"text":"Second fact"}]}}\n')
        self.finish(); self.run_worker()
        self.assertEqual(len(self.events()), 2)

    def test_interruption_can_archive_pending_work(self):
        self.message('Change implemented, tests not run yet')
        self.write('event_msg', {'type': 'turn_aborted', 'turn_id': 'turn1'})
        self.run_worker()
        self.assertIn('tests not run', self.events()[0]['body'])

    def test_hook_before_transcript_flush_does_not_lose_later_records(self):
        from datetime import datetime, timezone
        at = datetime.now(timezone.utc).isoformat()
        archive.enqueue_hook(self.cfg, {'session_id': 't1', 'hook_event_name': 'Stop'})
        self.run_worker()
        self.message('Late buffered final output', at)
        self.run_worker()
        day = datetime.now(archive.ZoneInfo('Asia/Shanghai')).date().isoformat()
        self.assertEqual(len(self.events(day)), 1)

    def test_late_hook_without_turn_is_consumed_without_flushing_new_turn(self):
        from datetime import datetime, timedelta, timezone
        self.write('event_msg', {'type':'task_started','turn_id':'old-turn'})
        self.message('Old completed work');
        self.write('event_msg', {'type':'task_complete','turn_id':'old-turn'})
        self.run_worker()
        jobs_before = self.store.db.execute('SELECT count(*) FROM codex_jobs').fetchone()[0]

        archive.enqueue_hook(self.cfg,
                             {'session_id':'t1','hook_event_name':'SessionEnd'})
        future = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
        self.write('event_msg', {'type':'task_started','turn_id':'new-turn'}, future)
        self.message('New turn remains pending', future)
        self.run_worker()

        status = self.store.db.execute(
            "SELECT status FROM codex_hook_events WHERE event='SessionEnd'").fetchone()[0]
        self.assertEqual(status, 'consumed')
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM codex_jobs').fetchone()[0],
                         jobs_before)
        cursor = self.store.db.execute(
            'SELECT data FROM codex_file_cursors WHERE session=?', ('t1',)).fetchone()[0]
        self.assertEqual([r['body'] for r in json.loads(cursor)['pending']],
                         ['New turn remains pending'])

    def test_pending_current_turn_is_not_reported_as_complete(self):
        from datetime import datetime, timezone
        at = datetime.now(timezone.utc).isoformat()
        self.message('Still running', at)
        self.run_worker()
        day = datetime.now(archive.ZoneInfo('Asia/Shanghai')).date().isoformat()
        self.assertEqual(archive.coverage(self.store, day, Policy({}))['status'], 'partial')

    def test_existing_archive_is_blocked_after_chat_rename(self):
        self.message('finished task'); self.finish(); self.run_worker()
        db = sqlite3.connect(self.home/'state_5.sqlite')
        db.execute("UPDATE threads SET title='Private'"); db.commit(); db.close()
        policy = Policy({'names':['Private']})
        self.run_worker(policy)
        self.assertEqual(self.events(policy=policy), [])

    def test_evidence_retrieval_returns_exact_fragment_and_respects_new_deny(self):
        self.message('Concrete fact with proof'); self.finish(); self.run_worker()
        ident = self.events()[0]['id']
        self.assertEqual(archive.evidence(self.store, ident, Policy({}))['records'][0]['body'], 'Concrete fact with proof')
        with self.assertRaises(ValueError):
            archive.evidence(self.store, ident, Policy({'codex_ids':['t1']}))

    def test_stale_scan_is_not_complete(self):
        self.store.set('codex_archive_scan', {'at': 0, 'errors': []})
        self.assertNotEqual(archive.coverage(self.store, '2026-11-09', Policy({}))['status'], 'ok')

    def test_late_archive_marks_existing_report_dirty(self):
        self.store.db.execute("INSERT INTO reports VALUES(?,?,?)", ('2026-11-09', 'p', '{}'))
        self.store.db.commit()
        self.message('Late verification'); self.finish(); self.run_worker()
        self.assertIn('2026-11-09', archive.dirty_days(self.store))

    def test_supported_quote_does_not_allow_advice_in_experience_candidate(self):
        records = [{'id':'e1','kind':'tool','body':'12 tests passed'}]
        fact = {'kind':'verification','text':'12 tests passed','status':'verified', 'evidence':[{'id':'e1','quote':'12 tests passed'}]}
        for text in ['建议每天休息十分钟', '## 📌 今日关键战报', '以后应该自动运行所有测试']:
            with self.subTest(text=text), self.assertRaises(ValueError):
                archive.validate(json.dumps({'facts':[fact], 'experience_candidates':[{'observation':text,'fact_indexes':[0],'evidence':fact['evidence']}]}),records)

    def test_personal_candidate_requires_direct_user_and_user_supported_fact(self):
        records = [{'id':'u','kind':'user','body':'先保留失败关闭页面的既有逻辑，只调整动画'},
                   {'id':'a','kind':'assistant','body':'已完成实现并通过检查'}]
        facts = [{'kind':'decision','text':'用户限定修改范围','status':'observed',
                  'evidence':[{'id':'u','quote':records[0]['body']}]},
                 {'kind':'output','text':'助手称完成','status':'reported_complete',
                  'evidence':[{'id':'a','quote':records[1]['body']}]}]
        candidate = {'observation':'用户在本次变更中明确了既有失败行为和修改边界。',
                     'fact_indexes':[0],'evidence':facts[0]['evidence']}
        self.assertEqual(archive.validate(json.dumps({'facts':facts,'experience_candidates':[candidate]}),records)['experience_candidates'],[candidate])
        for c in [{**candidate,'evidence':facts[1]['evidence']}, {**candidate,'fact_indexes':[1]}]:
            with self.subTest(candidate=c), self.assertRaisesRegex(ValueError,'direct user evidence'):
                archive.validate(json.dumps({'facts':facts,'experience_candidates':[c]}),records)

    def test_injected_agent_rules_do_not_prove_personal_behavior(self):
        records = [{'id':'u','kind':'user','body':'# AGENTS.md instructions\n先验证后提交'}]
        fact = {'kind':'decision','text':'规范要求先验证','status':'observed',
                'evidence':[{'id':'u','quote':'先验证后提交'}]}
        with self.assertRaisesRegex(ValueError,'direct user evidence'):
            archive.validate(json.dumps({'facts':[fact],'experience_candidates':[
                {'observation':'用户先验证后提交','fact_indexes':[0],'evidence':fact['evidence']}]}),records)

    def test_legacy_archive_filters_ai_candidates_and_preserves_user_quotes(self):
        self.message('用户明确保留失败关闭页面的逻辑',role='user')
        self.message('助手完成动画修改');self.finish();self.run_worker()
        key,raw=self.store.db.execute('SELECT id,result FROM codex_jobs').fetchone()
        data=json.loads(raw);data['experience_candidates']=[
            {'observation':'自动实现与测试形成闭环','fact_indexes':[1],'evidence':data['facts'][1]['evidence']}]
        self.store.db.execute('UPDATE codex_jobs SET result=? WHERE id=?',(json.dumps(data),key));self.store.db.commit()
        bodies=[json.loads(e['body']) for e in self.events()]
        user=next(b for b in bodies if b['evidence_roles']==['user'])
        self.assertEqual(user['user_evidence'],['用户明确保留失败关闭页面的逻辑'])
        self.assertTrue(all(not b['experience_candidates'] for b in bodies))

    def test_daily_model_can_request_only_allowed_archive_evidence(self):
        self.message('Detailed verification context'); self.finish(); self.run_worker()
        event_id = self.events()[0]['id']; seen=[]
        def daily(prompt):
            seen.append(prompt)
            if len(seen)==1: return json.dumps({'request_evidence':[event_id]})
            return json.dumps({'report_markdown':'暂无可复盘的工作事项。','actions':[],'lessons':[],'growth_focus':''})
        result = report.build_report(self.store,self.cfg,Policy({}),'2026-11-09',self.root/'reports',daily)
        self.assertTrue(result['ok'])
        self.assertEqual(len(seen),2)
        self.assertIn('补充原文证据',seen[1])
        self.assertIn('Detailed verification context',seen[1])

    def test_daily_short_id_evidence_is_resolved_and_action_stores_full_id(self):
        self.message('Detailed verified context'); self.finish(); self.run_worker()
        ident=self.events()[0]['id']; seen=[]
        def daily(prompt):
            seen.append(prompt)
            if len(seen)==1: return json.dumps({'request_evidence':['C1']})
            body='\n\n'.join('## '+heading+'\n\n具体事实与行动' for heading in report.SECTIONS)
            return json.dumps({'report_markdown':body,'lessons':[],'growth_focus':'','actions':[
                {'title':'验收','trigger':'交付','step':'验证','success':'通过','review_date':'2026-11-11','evidence_ids':['C1']}]})
        result=report.build_report(self.store,self.cfg,Policy({}),'2026-11-09',self.root/'reports',daily)
        self.assertTrue(result['ok'])
        self.assertIn('Detailed verified context',seen[1])
        self.assertEqual(self.store.actions(Policy({}))[0]['evidence_ids'],[ident])

    def test_binary_tool_images_are_not_sent_as_text_but_text_evidence_survives(self):
        self.write('response_item', {'type':'custom_tool_call_output','output':[{'type':'input_text','text':'Visible UI: camera preview distorted'}, {'type':'input_image','image_url':'data:image/png;base64,'+'Z'*100000}]})
        self.finish(); self.run_worker()
        self.assertIn('camera preview distorted',''.join(self.prompts))
        self.assertNotIn('Z'*1000,''.join(self.prompts))
        self.assertEqual(len(self.prompts),1)

    def test_active_turn_content_is_not_archived_just_because_prior_hook_signal_remains(self):
        from datetime import datetime, timezone
        at=datetime.now(timezone.utc).isoformat()
        archive.enqueue_hook(self.cfg, {'session_id':'t1','hook_event_name':'Stop','turn_id':'old-turn'})
        self.write('event_msg', {'type':'task_started','turn_id':'new-turn'}, at)
        self.message('Active new task', at)
        self.run_worker()
        day=datetime.now(archive.ZoneInfo('Asia/Shanghai')).date().isoformat()
        self.assertEqual(self.events(day), [])

    def test_daily_evidence_request_rejects_unrelated_or_denied_ids(self):
        self.message('Work'); self.finish(); self.run_worker()
        result=report.build_report(self.store,self.cfg,Policy({}),'2026-11-09',self.root/'reports',lambda p:'{"request_evidence":["/etc/passwd"]}')
        self.assertFalse(result['ok'])

    def test_finishing_report_does_not_erase_newer_archive_revision(self):
        self.store.db.execute('INSERT INTO reports VALUES(?,?,?)', ('2026-11-09','p','{}'));self.store.db.commit()
        self.message('First result'); self.finish(); self.run_worker()
        first=archive.dirty_days(self.store)['2026-11-09']
        self.message('Later result'); self.finish(); self.run_worker()
        archive.acknowledge(self.store, '2026-11-09', first)
        self.assertIn('2026-11-09',archive.dirty_days(self.store))
        archive.acknowledge(self.store,'2026-11-09',archive.dirty_days(self.store)['2026-11-09'])
        self.assertEqual(archive.dirty_days(self.store),{})

    def test_report_acknowledges_consumed_revision_even_with_partial_git(self):
        self.message('Verified result'); self.finish(); self.run_worker()
        self.store.run_status('2026-11-09','git','partial',1,'Discovery failures')
        for attempt in range(2):
            self.store.db.execute('INSERT INTO codex_dirty_days VALUES(?,?)',('2026-11-09',38))
            self.store.db.commit()
            def daily(prompt):
                self.assertEqual(attempt,0,'Cached report must not call model')
                return json.dumps({'report_markdown':'暂无可复盘的工作事项。','actions':[],'lessons':[]})
            result=report.build_report(self.store,self.cfg,Policy({}),'2026-11-09',self.root/'reports',daily)
            self.assertTrue(result['ok'])
            self.assertEqual(archive.dirty_days(self.store),{})
        self.assertTrue(result['reused'])

    def test_report_failure_and_archive_wait_preserve_revision(self):
        self.message('Verified result'); self.finish(); self.run_worker()
        self.store.db.execute('INSERT INTO codex_dirty_days VALUES(?,?)',('2026-11-09',38));self.store.db.commit()
        def failed(prompt): raise RuntimeError('Unavailable')
        result=report.build_report(self.store,self.cfg,Policy({}),'2026-11-09',self.root/'reports',failed)
        self.assertFalse(result['ok']); self.assertEqual(archive.dirty_days(self.store),{'2026-11-09':38})
        self.message('Not yet archived'); self.finish(); archive.scan(self.cfg,self.store,Policy({}))
        result=report.build_report(self.store,self.cfg,Policy({}),'2026-11-09',self.root/'reports',failed)
        self.assertTrue(result['waiting_for_archive'])
        self.assertEqual(archive.dirty_days(self.store),{'2026-11-09':38})

    def test_report_preserves_revision_arriving_during_model_call(self):
        self.message('Verified result'); self.finish(); self.run_worker()
        self.store.db.execute('INSERT INTO codex_dirty_days VALUES(?,?)',('2026-11-09',38));self.store.db.commit()
        def daily(prompt):
            other=Store(self.root/'runtime/events.sqlite')
            try:
                other.db.execute('UPDATE codex_dirty_days SET revision=39'); other.db.commit()
            finally: other.close()
            return json.dumps({'report_markdown':'暂无可复盘的工作事项。','actions':[],'lessons':[]})
        result=report.build_report(self.store,self.cfg,Policy({}),'2026-11-09',self.root/'reports',daily)
        self.assertTrue(result['ok']); self.assertEqual(archive.dirty_days(self.store),{'2026-11-09':39})

    def test_archived_facts_are_not_lossily_summarized_again(self):
        for n in range(5):
            self.message('Unique completed fact '+str(n)+' '+('detail '*40)); self.finish()
        self.run_worker(); self.cfg['model']['chunk_chars']=2000
        seen=[]
        def daily(prompt):
            seen.append(prompt)
            return json.dumps({'report_markdown':'暂无可复盘的工作事项。','actions':[],'lessons':[],'growth_focus':''})
        result=report.build_report(self.store,self.cfg,Policy({}),'2026-11-09',self.root/'reports',daily)
        self.assertTrue(result['ok'])
        self.assertEqual(len(seen),1)
        for n in range(5): self.assertIn('Unique completed fact '+str(n),seen[0])

    def test_new_blacklist_during_worker_prevents_next_model_call(self):
        self.message('First work'); self.finish(); self.message('Second work'); self.finish()
        self.cfg['sources']['lark']={}
        config_path=self.root/'config.json'; self.cfg['_path']=str(config_path)
        config_path.write_text(json.dumps(self.cfg))
        calls=[]
        def model(prompt):
            calls.append(prompt)
            changed=dict(self.cfg);changed['blacklist']={'codex_ids':['t1']}
            config_path.write_text(json.dumps(changed))
            return self.model(prompt)
        self.run_worker(model=model)
        self.assertEqual(len(calls),1)

    def test_incomplete_archive_does_not_publish_misleading_daily_report(self):
        self.message('Old result'); self.finish(); self.run_worker()
        self.message('Correction still waiting'); self.finish()
        archive.scan(self.cfg,self.store,Policy({}))
        def daily(prompt): self.fail('Incomplete archive must not generate confident conclusions')
        result=report.build_report(self.store,self.cfg,Policy({}),'2026-11-09',self.root/'reports',daily)
        self.assertFalse(result['ok'])
        self.assertTrue(result['waiting_for_archive'])
        self.assertIsNone(self.store.db.execute('SELECT data FROM reports').fetchone())

    def test_disabled_codex_does_not_block_other_sources_report(self):
        self.cfg['sources']['codex']['enabled']=False
        self.store.put(make_event('manual','one','2026-11-09T05:00:00Z','Work','Completed document'))
        def model(prompt):return json.dumps({'report_markdown':'暂无可复盘的工作事项。','actions':[],'lessons':[],'growth_focus':''})
        result=report.build_report(self.store,self.cfg,Policy({'sources':['codex']}),'2026-11-09',self.root/'reports',model)
        self.assertTrue(result['ok'])

    def test_report_uses_archive_and_never_old_raw_codex(self):
        self.message('Archived work result'); self.finish(); self.run_worker()
        self.store.put(make_event('codex','old','2026-11-09T05:00:00Z','Work','RAW_NOISY_LOG'))
        seen=[]
        def daily(prompt):
            seen.append(prompt)
            return json.dumps({'report_markdown':'暂无可复盘的工作事项。','actions':[],'lessons':[],'growth_focus':''})
        report.build_report(self.store,self.cfg,Policy({}),'2026-11-09',self.root/'reports',daily)
        self.assertNotIn('RAW_NOISY_LOG',''.join(seen))
        self.assertIn('Archived work result',''.join(seen))


if __name__ == '__main__': unittest.main()
