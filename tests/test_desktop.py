import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from daily_review.core import Policy, Store
from daily_review import cli
try:
    from daily_review import desktop
except ImportError:
    desktop = None


class DesktopTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(desktop, 'Report desktop delivery is not implemented')
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / '报告 with "quotes" $(touch nope)' / '2026-11-09.md'
        self.path.parent.mkdir(); self.path.write_text('# Report\nFinished work\n')
        self.store = Store(self.root / 'events.sqlite'); self.addCleanup(self.store.close)
        self.policy = Policy({})
        self.calls = []

    def runner(self, args, **kwargs):
        self.calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, '', '')

    def deliver(self, result=None, **kwargs):
        return desktop.deliver(self.store, result or {'ok':True,'path':str(self.path)}, self.policy,
                               runner=self.runner, now=1000, **kwargs)

    def test_saved_report_notifies_and_opens_typora_without_shell_interpolation(self):
        self.deliver()
        self.assertEqual(len(self.calls),2)
        notification, opening = self.calls
        self.assertEqual(notification[0][0], '/usr/bin/osascript')
        self.assertIn('2026-11-09',notification[0])
        self.assertEqual(opening[0], ['/usr/bin/open','-a','Typora',str(self.path.resolve())])
        self.assertNotIn('shell',opening[1])
        self.assertNotIn('Finished work',' '.join(notification[0]))
        self.assertFalse((self.root/'nope').exists())

    def test_failures_pending_and_collection_only_never_deliver(self):
        for result in [{'ok':False,'path':str(self.path)},
                       {'ok':False,'waiting_for_archive':True,'path':str(self.path)},
                       {'ok':True,'collected_only':True},
                       {'ok':True,'path':str(self.path.with_name('2026-11-09.pending.md'))}]:
            self.deliver(result)
        self.assertEqual(self.calls,[])

    def test_updated_report_deduplicates_across_restart_and_policy_changes(self):
        self.deliver()
        self.path.write_text('# Report\nUpdated completed work\n')
        other = Store(self.root/'events.sqlite')
        try:
            desktop.deliver(other,{'ok':True,'path':str(self.path),'reused':True},self.policy,runner=self.runner,now=2000)
            desktop.deliver(other,{'ok':True,'path':str(self.path)},Policy({'names':['Private']}),runner=self.runner,now=3000)
        finally: other.close()
        self.assertEqual(len(self.calls),2)

    def test_updated_report_retries_only_failed_channel(self):
        def fail_open(args, **kwargs):
            self.calls.append((args,kwargs))
            return subprocess.CompletedProcess(args, 1 if args[0].endswith('open') else 0,'','')
        result={'ok':True,'path':str(self.path)}
        desktop.deliver(self.store,result,self.policy,runner=fail_open,now=1000)
        self.path.write_text('Updated successful report')
        desktop.deliver(self.store,result,self.policy,runner=self.runner,now=1010)
        self.assertEqual(len(self.calls),2)
        desktop.retry_pending(self.store,self.policy,runner=self.runner,now=1300)
        self.assertEqual([a[0] for a,k in self.calls],['/usr/bin/osascript','/usr/bin/open','/usr/bin/open'])

    def test_failed_notification_retries_without_reopening_successful_typora(self):
        def fail_notification(args, **kwargs):
            self.calls.append((args,kwargs))
            return subprocess.CompletedProcess(args, 1 if args[0].endswith('osascript') else 0,'','error')
        first=desktop.deliver(self.store,{'ok':True,'path':str(self.path)},self.policy,runner=fail_notification,now=1000)
        self.assertEqual(len(self.calls),2)
        self.assertEqual(first['notification'],'failed')
        self.assertEqual(first['typora'],'submitted')
        desktop.retry_pending(self.store,self.policy,runner=self.runner,now=1010)
        self.assertEqual(len(self.calls),2)
        desktop.retry_pending(self.store,self.policy,runner=self.runner,now=1400)
        self.assertEqual([a[0] for a,k in self.calls],['/usr/bin/osascript','/usr/bin/open','/usr/bin/osascript'])

    def test_open_timeout_does_not_undo_report_or_repeat_notification(self):
        def timeout_open(args, **kwargs):
            self.calls.append((args,kwargs))
            if args[0].endswith('open'): raise subprocess.TimeoutExpired(args,15)
            return subprocess.CompletedProcess(args,0,'','')
        result={'ok':True,'path':str(self.path)}
        first=desktop.deliver(self.store,result,self.policy,runner=timeout_open,now=1000)
        self.assertTrue(result['ok']); self.assertEqual(first['typora'],'failed')
        desktop.retry_pending(self.store,self.policy,runner=self.runner,now=1400)
        self.assertEqual([a[0] for a,k in self.calls],['/usr/bin/osascript','/usr/bin/open','/usr/bin/open'])

    def test_frequent_polling_does_not_postpone_failed_channel_retry(self):
        def failed(args,**kwargs): return subprocess.CompletedProcess(args,1,'','failed')
        desktop.deliver(self.store,{'ok':True,'path':str(self.path)},self.policy,runner=failed,now=1000)
        for now in (1010,1100,1299):
            desktop.retry_pending(self.store,self.policy,runner=self.runner,now=now)
        self.assertEqual(self.calls,[])
        desktop.retry_pending(self.store,self.policy,runner=self.runner,now=1300)
        self.assertEqual(len(self.calls),2)

    def test_changed_file_or_policy_cancels_pending_delivery(self):
        def failed(args,**kwargs):return subprocess.CompletedProcess(args,1,'','failed')
        desktop.deliver(self.store,{'ok':True,'path':str(self.path)},self.policy,runner=failed,now=1000)
        self.path.write_text('**未完成 AI 分析。**')
        desktop.retry_pending(self.store,self.policy,runner=self.runner,now=1400)
        self.assertEqual(self.calls,[])
        self.path.write_text('New valid report')
        desktop.deliver(self.store,{'ok':True,'path':str(self.path)},self.policy,runner=failed,now=1500)
        desktop.retry_pending(self.store,Policy({'codex_ids':['newly-denied']}),runner=self.runner,now=2000)
        self.assertEqual(self.calls,[])

    def test_notification_and_typora_can_be_disabled_individually(self):
        self.deliver(settings={'notify':False,'open_typora':True})
        self.assertEqual([a[0] for a,k in self.calls],['/usr/bin/open'])

    def test_cli_manual_report_is_silent_unless_notify_is_explicit(self):
        config={'timezone':'Asia/Shanghai','collection_enabled':True,'blacklist':{},
                'schedule':{'hour':9,'minute':30,'start_date':'9999-01-01'},
                'sources':{'lark':{},'codex':{}},'model':{}}
        path=self.root/'config.json';path.write_text(json.dumps(config))
        with patch('sys.argv',['daily-review','--config',str(path),'report','--date','2026-11-09']), \
             patch('daily_review.cli.build_report',return_value={'ok':True,'path':str(self.path)}), \
             patch('daily_review.desktop.subprocess.run',side_effect=self.runner):
            self.assertEqual(cli.main(),0)
        self.assertEqual(self.calls,[])
        with patch('sys.argv',['daily-review','--config',str(path),'report','--date','2026-11-09','--notify']), \
             patch('daily_review.cli.build_report',return_value={'ok':True,'path':str(self.path)}), \
             patch('daily_review.desktop.subprocess.run',side_effect=self.runner):
            self.assertEqual(cli.main(),0)
        self.assertEqual(len(self.calls),2)


if __name__=='__main__': unittest.main()
