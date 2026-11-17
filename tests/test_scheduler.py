import contextlib
import io
import json
import subprocess
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from daily_review import archive, cli, desktop
from daily_review.core import Store, make_event


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = {'timezone':'Asia/Shanghai','collection_enabled':True,'blacklist':{},
                       'schedule':{'hour':9,'minute':30,'start_date':'2026-11-09'},
                       'sources':{'codex':{'enabled':False,'mode':'archive'},
                                  'lark':{'enabled':False},'git':{},'browser':{'enabled':False}},
                       'model':{}}
        self.path = self.root/'config.json'; self.path.write_text(json.dumps(self.config))
        self.store = Store(self.root/'runtime/events.sqlite'); self.addCleanup(self.store.close)
        archive.init(self.store)
        self.day = '2026-11-09'
        self.event = make_event('git','commit','2026-11-09T10:00:00+08:00','Change','Verified change')
        self.calls = []; self.model_calls = []

    def dirty(self):
        self.store.db.execute('INSERT OR REPLACE INTO codex_dirty_days VALUES(?,?)',(self.day,38))
        self.store.db.commit()

    def collect_git(self, *args):
        yield self.event
        raise RuntimeError('Git discovery/read failures: 43')

    def model(self, config, prompt):
        if prompt.startswith('你是日报证据审查者'):
            return json.dumps({'passed':True,'reason':'空报告无事实断言','claims':[]})
        self.model_calls.append(prompt)
        return json.dumps({'report_markdown':'暂无可复盘的工作事项。','actions':[],'lessons':[]})

    def runner(self, args, **kwargs):
        self.calls.append(args)
        return subprocess.CompletedProcess(args,0,'','')

    def invoke(self, at, *args, model=None):
        fixed = datetime.fromisoformat(at)
        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return fixed.astimezone(tz) if tz else fixed
        output = io.StringIO()
        with patch('sys.argv',['daily-review','--config',str(self.path),*args]), \
             patch('daily_review.cli.datetime',Clock), \
             patch('daily_review.cli.git_events',side_effect=self.collect_git), \
             patch('daily_review.report.model_call',side_effect=model or self.model), \
             patch('daily_review.desktop.subprocess.run',side_effect=self.runner), \
             patch('daily_review.desktop.time.time',return_value=fixed.timestamp()), \
             contextlib.redirect_stdout(output):
            code = cli.main()
        return code, json.loads(output.getvalue())

    def test_before_cutoff_blocks_dirty_backlog_and_pending_desktop_retry(self):
        self.store.set('last_completed_day','2026-11-10'); self.dirty()
        path = self.root/'reports'/(self.day+'.md'); path.parent.mkdir(); path.write_text('Report')
        desktop.deliver(self.store,{'ok':True,'path':str(path)},cli.effective_policy(self.config),
                        runner=lambda *a,**k: subprocess.CompletedProcess(a,1,'',''),now=0)
        for at in ('2026-11-11T08:00:00+08:00','2026-11-11T09:29:59+08:00'):
            code, result = self.invoke(at,'tick')
            self.assertEqual(result.get('skipped'),'before_schedule')
        self.assertEqual(self.model_calls,[]); self.assertEqual(self.calls,[])
        self.assertEqual(archive.dirty_days(self.store),{self.day:38})
        self.assertEqual(self.store.statuses(self.day),[])

    def test_generated_report_is_not_scheduled_for_old_date_recollection(self):
        self.dirty()
        code, first = self.invoke('2026-11-10T09:30:00+08:00','tick')
        self.assertEqual(code,0)
        self.assertTrue(first['runs'][0]['ok']); self.assertFalse(first['runs'][0]['complete'])
        self.assertEqual(len(self.calls),2)
        self.assertEqual(archive.dirty_days(self.store),{})
        self.assertEqual(self.store.get('retry_dates'),[])
        code, second = self.invoke('2026-11-10T09:35:00+08:00','tick')
        self.assertEqual(second['runs'],[])
        self.event['body'] = 'Additional verification changes report input'
        code, third = self.invoke('2026-11-10T15:30:00+08:00','tick')
        self.assertEqual(third['runs'],[])
        self.assertEqual(len(self.model_calls),1); self.assertEqual(len(self.calls),2)

    def test_failed_report_preserves_dirty_revision_and_honors_backoff(self):
        self.store.set('last_completed_day',self.day); self.dirty()
        def failed(*args): raise RuntimeError('Model unavailable')
        code, result = self.invoke('2026-11-10T10:00:00+08:00','tick',model=failed)
        self.assertFalse(result['runs'][0]['ok'])
        self.assertEqual(archive.dirty_days(self.store),{self.day:38})
        code, result = self.invoke('2026-11-10T10:05:00+08:00','tick')
        self.assertEqual(result.get('skipped'),'retry_backoff')
        self.assertEqual(self.calls,[])

    def test_old_retry_backoff_does_not_delay_new_daily_report(self):
        self.store.set('last_completed_day',self.day); self.dirty()
        self.store.set('retry_dates',[self.day])
        self.store.set('next_retry',datetime.fromisoformat('2026-11-11T20:00:00+08:00').timestamp())
        code, result = self.invoke('2026-11-11T09:30:00+08:00','tick')
        self.assertEqual([Path(r['path']).stem for r in result['runs']],['2026-11-10'])
        self.assertEqual(archive.dirty_days(self.store),{self.day:38})
        self.assertEqual(len(self.calls),2)

    def test_due_daily_report_is_delivered_before_another_due_day_runs(self):
        self.config['schedule']['start_date'] = '2026-11-09'
        self.path.write_text(json.dumps(self.config))
        sequence = []

        def run_day(config, store, day, *args, **kwargs):
            sequence.append('run:%s:desktop_calls=%d' % (day,len(self.calls)))
            path = self.root/'reports'/(day+'.md')
            path.parent.mkdir(exist_ok=True); path.write_text('Report ' + day)
            return {'ok':True,'complete':True,'path':str(path)}

        self.store.set('last_completed_day','2026-11-08')
        with patch('daily_review.cli.run_day',side_effect=run_day):
            code, result = self.invoke('2026-11-11T09:30:00+08:00','tick')
        self.assertEqual(code,0)
        self.assertEqual(sequence,['run:2026-11-09:desktop_calls=0',
                                   'run:2026-11-10:desktop_calls=2'])
        self.assertEqual(result['runs'][0]['desktop'],
                         {'notification':'submitted','typora':'submitted'})

    def test_due_daily_report_discards_finalized_old_retry(self):
        sequence = []

        def run_day(config, store, day, *args, **kwargs):
            sequence.append(day)
            path = self.root/'reports'/(day+'.md')
            path.parent.mkdir(exist_ok=True); path.write_text('Report ' + day)
            return {'ok':True,'complete':True,'path':str(path)}

        self.store.set('last_completed_day',self.day)
        self.store.set('retry_dates',[self.day])
        self.store.set('next_retry',0)
        with patch('daily_review.cli.run_day',side_effect=run_day):
            code, result = self.invoke('2026-11-11T09:30:00+08:00','tick')
        self.assertEqual(code,0)
        self.assertEqual(sequence,['2026-11-10'])
        self.assertEqual(self.store.get('last_completed_day'),'2026-11-10')
        self.assertEqual(self.store.get('retry_dates'),[])
        self.assertEqual(result['runs'][0]['desktop'],
                         {'notification':'submitted','typora':'submitted'})

    def test_tick_recovers_report_delivery_and_does_not_run_old_retry(self):
        report_day = '2026-11-10'
        path = self.root/'reports'/(report_day+'.md')
        path.parent.mkdir(); path.write_text('Recovered report')
        policy = cli.effective_policy(self.config)
        self.store.db.execute('INSERT INTO reports VALUES(?,?,?)',
                              (report_day,policy.fingerprint,'{}'))
        self.store.db.commit()
        self.store.set('last_completed_day',report_day)
        self.store.set('retry_dates',[self.day])
        self.store.set('next_retry',0)
        observed = []

        def slow_retry(*args, **kwargs):
            observed.append(len(self.calls))
            return {'ok':False,'waiting_for_archive':True}

        with patch('daily_review.cli.run_day',side_effect=slow_retry):
            code, result = self.invoke('2026-11-11T09:30:00+08:00','tick')
        self.assertEqual(code,0)
        self.assertEqual(observed,[])
        self.assertEqual(result['runs'],[])
        self.assertEqual(self.store.get('retry_dates'),[])
        self.assertEqual(result['recovered_delivery'],
                         {'date':report_day,'notification':'submitted','typora':'submitted'})

    def test_limited_git_coverage_is_terminal_and_visible(self):
        from daily_review.collectors import GitCoverageGap
        def limited(*args):
            yield self.event
            raise GitCoverageGap(['/private/repo: permission_denied'])
        config = cli.load_config(self.path)
        with patch('daily_review.cli.git_events',side_effect=limited), \
             patch('daily_review.report.model_call',side_effect=self.model):
            result = cli.run_day(config,self.store,self.day)
        git = next(x for x in result['sources'] if x['source']=='git')
        self.assertEqual(git['status'],'limited')
        self.assertIn('permission_denied',git['detail'])
        self.assertTrue(result['ok'])
        self.assertFalse(result['complete'])

    def test_limited_git_report_makes_tick_successful_without_retry(self):
        from daily_review.collectors import GitCoverageGap
        def limited(*args):
            raise GitCoverageGap(['/private/repo: permission_denied'])
            yield
        with patch('daily_review.cli.git_events',side_effect=limited):
            code,result=self.invoke('2026-11-10T09:30:00+08:00','tick')
        self.assertEqual(code,0)
        self.assertTrue(result['ok'])
        self.assertFalse(result['complete'])
        self.assertEqual(self.store.get('retry_dates'),[])

    def test_archive_wait_prefetches_slow_sources_once_then_reuses_them(self):
        self.config['sources']['codex'] = {'enabled':True,'mode':'archive'}
        self.path.write_text(json.dumps(self.config))
        config = cli.load_config(self.path)
        partial = {'source':'codex','status':'partial','count':0,'detail':'pending=1'}
        ready = {'source':'codex','status':'ok','count':1,'detail':'pending=0'}
        git_calls = []

        def collect_git(*args):
            git_calls.append(1)
            yield self.event

        with patch('daily_review.archive.coverage',side_effect=[partial,partial,partial,ready,ready,ready]), \
             patch('daily_review.cli.git_events',side_effect=collect_git), \
             patch('daily_review.report.model_call',side_effect=self.model):
            first = cli.run_day(config,self.store,self.day,prefetch=True)
            second = cli.run_day(config,self.store,self.day,prefetch=True)
        self.assertTrue(first['waiting_for_archive'])
        self.assertTrue(second['ok'])
        self.assertEqual(len(git_calls),1)
        self.assertIsNone(self.store.get('report_prefetch:'+self.day))

    def test_idle_tick_retries_failed_delivery_only_after_cutoff(self):
        self.store.set('last_completed_day',self.day)
        path = self.root/'reports'/(self.day+'.md'); path.parent.mkdir(); path.write_text('Report')
        desktop.deliver(self.store,{'ok':True,'path':str(path)},cli.effective_policy(self.config),
                        runner=lambda *a,**k: subprocess.CompletedProcess(a,1,'',''),now=0)
        code, result = self.invoke('2026-11-10T09:29:59+08:00','tick')
        self.assertEqual(self.calls,[])
        code, result = self.invoke('2026-11-10T09:30:00+08:00','tick')
        self.assertEqual(result['runs'],[])
        self.assertEqual(len(self.calls),2)
        self.invoke('2026-11-10T09:35:00+08:00','tick')
        self.assertEqual(len(self.calls),2)

    def test_manual_run_and_report_are_silent_and_acknowledge_archive(self):
        for command in ('run','report'):
            self.dirty()
            code, result = self.invoke('2026-11-10T08:00:00+08:00',command,'--date',self.day)
            self.assertTrue(result['ok'])
            self.assertEqual(archive.dirty_days(self.store),{})
        self.assertEqual(self.calls,[])
