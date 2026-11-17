import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from daily_review import archive, blocking
from daily_review.comparison import set_status
from daily_review.core import Policy, Store


class BlockingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)/'events.sqlite'
        self.store = Store(self.path); self.addCleanup(self.store.close)
        self.config = {'_root':self.tmp.name, 'collection_enabled': True, 'timezone': 'Asia/Shanghai', 'sources': {}}
        self.calls = []; self.now = 1800000000

    def runner(self, args, **kwargs):
        self.calls.append(args)
        return subprocess.CompletedProcess(args, 0, '', '')

    def check(self, now=None, **kwargs):
        return blocking.check(self.config, self.store, Policy({}), runner=self.runner,
                              now=self.now if now is None else now, **kwargs)

    def fail(self, stage='report:2026-11-16'):
        for at in (self.now-2000, self.now-1000, self.now):
            blocking.record_failure(self.store, stage, True, now=at)

    def test_transient_failure_and_normal_wait_are_silent(self):
        blocking.record_failure(self.store, 'report:day', True, now=self.now-4000)
        self.assertFalse(self.check()['blocked']); self.assertEqual(self.calls, [])

    def test_persistent_failure_only_notifies_once_across_restarts(self):
        self.fail(); self.assertEqual(self.check()['notification'], 'submitted')
        for offset in (5, 300, 3600, 86400): self.check(self.now+offset)
        other = Store(self.path)
        try: blocking.check(self.config, other, Policy({}), runner=self.runner, now=self.now+90000)
        finally: other.close()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][0], '/usr/bin/osascript')

    def test_multiple_failures_aggregate_and_recovery_has_no_notification(self):
        self.fail(); self.fail('research')
        self.assertEqual(len(self.check()['reasons']), 2); self.assertEqual(len(self.calls), 1)
        for stage in ('report:2026-11-16', 'research'):
            blocking.record_failure(self.store, stage, False, now=self.now+1)
        self.assertFalse(self.check(self.now+2)['blocked']); self.assertEqual(len(self.calls), 1)
        self.fail(); self.assertEqual(self.check(self.now+3)['notification'], 'cooldown')
        self.assertEqual(self.check(self.now+21600)['notification'], 'submitted')
        self.assertEqual(len(self.calls), 2)

    def test_failed_submission_is_rate_limited(self):
        self.fail()
        def failed(args, **kwargs): self.calls.append(args); raise OSError('unavailable')
        first = blocking.check(self.config, self.store, Policy({}), runner=failed, now=self.now)
        self.assertEqual(first['notification'], 'failed')
        self.check(self.now+300); self.assertEqual(len(self.calls), 1)

    def test_no_progress_wait_is_silent_until_threshold(self):
        self.config['sources'] = {'codex': {'mode':'archive'}}
        archive.init(self.store)
        meta = {'source':'codex','scope_id':'s','path':'/work','transcript_path':'/x'}
        records = [{'id':'s:f:1','kind':'tool','body':'work','at':'2026-11-16T00:00:00Z'}]
        self.store.db.execute('INSERT INTO codex_jobs(id,session,day,meta,records,created_at) VALUES(?,?,?,?,?,?)',
            ('j','s','2026-11-16',json.dumps(meta),json.dumps(records),self.now-3000)); self.store.db.commit()
        self.store.set('report_retry_dates', ['2026-11-16'])
        for offset in (0, 300, 1799):
            self.store.set('codex_archive_scan', {'at':self.now+offset,'errors':[]})
            self.assertFalse(self.check(self.now+offset)['blocked'])
        self.store.set('codex_archive_scan', {'at':self.now+1800,'errors':[]})
        self.assertEqual(self.check(self.now+1800)['notification'], 'submitted')
        self.assertEqual(len(self.calls), 1)

    def test_disabled_notifications_are_silent(self):
        self.fail(); self.config['desktop'] = {'notify':False}
        self.check(); self.assertEqual(self.calls, [])

    def test_research_failures_are_scoped_to_the_same_item(self):
        for index, item in enumerate(('item-a', 'item-b', 'item-c')):
            blocking.record_research_result(self.store, {'id':item,'error':'invalid JSON'},
                                            now=self.now - 2000 + index)
        self.assertFalse(self.check()['blocked'])
        self.assertIsNone(self.store.get('blocking_phase:research'))
        for item in ('item-a', 'item-b', 'item-c'):
            self.assertEqual(self.store.get('blocking_phase:research:'+item)['attempts'],1)

    def test_one_research_success_does_not_clear_other_item_failure(self):
        blocking.record_research_result(self.store, {'id':'failed','error':'invalid JSON'}, now=self.now)
        blocking.record_research_result(self.store, {'id':'success','processed':1}, now=self.now+1)
        self.assertEqual(self.store.get('blocking_phase:research:failed')['attempts'],1)
        self.assertIsNone(self.store.get('blocking_phase:research:success'))

    def test_persistent_research_alert_identifies_item_and_stops_after_recovery(self):
        from daily_review import research
        from daily_review.core import make_event
        self.config['sources']={}
        event=make_event('manual','research-seed','2026-11-16T10:00:00+08:00','工作','事实')
        self.store.put(event)
        key=research.enqueue(self.store,self.config,Policy({}),{
            'kind':'efficiency','title':'自动核对搜索接口','task_key':'search','goal':'减少重复',
            'evidence_ids':[event['id']]},[event],'2026-11-16')
        value=json.loads(self.store.db.execute('SELECT data FROM research_items WHERE id=?',(key,)).fetchone()[0])
        value['last_error']='JSONDecodeError: Extra data'
        self.store.db.execute('UPDATE research_items SET data=? WHERE id=?',(json.dumps(value),key));self.store.db.commit()
        for at in (self.now-2000,self.now-1000,self.now):
            blocking.record_research_result(self.store,{'id':key,'error':'invalid JSON'},now=at)
        result=self.check()
        self.assertTrue(result['blocked'])
        self.assertIn('自动核对搜索接口',self.calls[0][-1])
        self.assertIn('JSONDecodeError',self.calls[0][-1])
        blocking.record_research_result(self.store,{'id':key,'processed':1},now=self.now+1)
        self.assertFalse(self.check(self.now+2)['blocked'])
        self.assertEqual(len(self.calls),1)

    def test_failed_archive_is_deduplicated_while_retry_is_running_and_blacklist_applies(self):
        self.config['sources'] = {'codex': {'mode':'archive'}}
        archive.init(self.store)
        meta = {'source':'codex','scope_id':'private-session','path':'/work','transcript_path':'/x'}
        records = [{'id':'s:f:1','kind':'tool','body':'work','at':'2026-11-16T00:00:00Z'}]
        self.store.db.execute('INSERT INTO codex_jobs(id,session,day,meta,records,status,attempts,created_at,error) VALUES(?,?,?,?,?,?,?,?,?)',
            ('j','private-session','2026-11-16',json.dumps(meta),json.dumps(records),'failed',3,self.now-1000,'invalid quote'))
        self.store.db.commit()
        denied = blocking.check(self.config, self.store, Policy({'codex_ids':['private-session']}), runner=self.runner, now=self.now)
        self.assertFalse(denied['blocked']); self.assertEqual(self.calls, [])
        self.assertEqual(self.check()['notification'], 'submitted')
        self.store.db.execute("UPDATE codex_jobs SET status='running',started_at=?",(self.now+1,));self.store.db.commit()
        self.assertEqual(self.check(self.now+2)['notification'], 'already_submitted')
        self.assertEqual(len(self.calls), 1)

    def test_stalled_report_can_be_detected_without_taking_report_lock(self):
        self.config['_root'] = self.tmp.name
        set_status(self.config, Policy({}), '2026-11-16', 'preparing_input', at=self.now-1801)
        self.assertEqual(self.check()['notification'], 'submitted')

    def test_single_failed_report_waiting_for_backoff_is_not_a_stalled_process(self):
        set_status(self.config, Policy({}), '2026-11-16', 'analysis_failed', at=self.now-1801)
        blocking.record_failure(self.store, 'report:2026-11-16', True, now=self.now-1801)
        self.assertFalse(self.check()['blocked']); self.assertEqual(self.calls, [])

    def test_old_policy_stall_is_not_notified(self):
        set_status(self.config, Policy({'names':['private']}), '2026-11-16', 'preparing_input', at=self.now-1801)
        self.assertFalse(self.check()['blocked']); self.assertEqual(self.calls, [])

    def test_monitor_uses_its_own_lock_when_report_lock_is_held(self):
        import fcntl
        from daily_review import cli
        self.config.update(blacklist={},sources={'lark':{}},schedule={'hour':8,'minute':0})
        path = Path(self.tmp.name)/'config.json';path.write_text(json.dumps(self.config))
        runtime = Path(self.tmp.name)/'runtime';runtime.mkdir(exist_ok=True)
        with (runtime/'run.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch('sys.argv',['daily-review','--config',str(path),'blocking-check']), \
                 patch('daily_review.blocking.check',return_value={'blocked':False}) as checker:
                self.assertEqual(cli.main(),0)
                self.assertEqual(checker.call_count,1)


if __name__ == '__main__': unittest.main()
