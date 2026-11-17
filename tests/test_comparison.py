import contextlib
import io
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from daily_review import cli
from daily_review.comparison import read_input, snapshot, set_status, pending, acknowledge
from daily_review.core import Policy, Store, make_event
from daily_review.report import build_report


class ComparisonTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = {'_root': str(self.root), 'timezone': 'Asia/Shanghai',
                       'collection_enabled': True, 'blacklist': {}, 'sources': {'lark': {}}, 'model': {}}
        self.policy = cli.effective_policy(self.config); self.day = '2026-11-15'

    def test_missing_pending_and_ready_input(self):
        self.assertFalse(read_input(self.config, self.policy, self.day)['ok'])
        snapshot(self.config, self.policy, self.day, 'facts only')
        self.assertFalse(read_input(self.config, self.policy, self.day)['ok'])
        snapshot(self.config, self.policy, self.day, 'facts only', ready=True)
        result = read_input(self.config, self.policy, self.day)
        self.assertTrue(result['ok']); self.assertEqual(result['prompt'], 'facts only')
        self.assertEqual(result['output_mode'], 'chat_markdown_only')

    def test_late_ready_date_remains_pending_until_explicit_receipt(self):
        set_status(self.config, self.policy, self.day, 'waiting_for_archive', pending_jobs=1)
        value = read_input(self.config, self.policy, self.day)
        self.assertEqual(value['upstream']['stage'], 'waiting_for_archive')
        self.assertIn(self.day, [x['date'] for x in pending(self.config, self.policy)['dates']])
        snapshot(self.config, self.policy, self.day, 'facts', ready=True)
        self.assertTrue(next(x for x in pending(self.config, self.policy)['dates'] if x['date']==self.day)['ready'])
        with self.assertRaises(ValueError): acknowledge(self.config, self.policy, self.day, 'wrong')
        acknowledge(self.config, self.policy, self.day, read_input(self.config, self.policy, self.day)['input_sha256'])
        self.assertNotIn(self.day, [x['date'] for x in pending(self.config, self.policy)['dates']])

    def test_saved_old_prompt_uses_latest_presentation_without_changing_evidence(self):
        from daily_review.report import SECTIONS
        prompt='旧模板：🌿 生活与精力管理建议；按重要/紧急分类。\n工作事实：FACT_KEEP'
        snapshot(self.config,self.policy,self.day,prompt,ready=True)
        path=self.root/'runtime/comparison-inputs'/(self.day+'.json')
        before=path.read_bytes()
        value=read_input(self.config,self.policy,self.day)
        self.assertTrue(value['ok'])
        self.assertEqual(value['prompt'],prompt)
        self.assertEqual(path.read_bytes(),before)
        for section in SECTIONS:self.assertIn(section,value['presentation'])
        self.assertIn('即使保存的prompt仍含旧模板',value['presentation'])
        self.assertIn('无内容正文写“暂无”',value['presentation'])
        self.assertIn('标题可按内容增减、改名或调整顺序',value['presentation'])
        self.assertIn('SOP四字段仅作建议',value['presentation'])
        self.assertIn('若无计划或工时依据，直接陈述事实即可',value['presentation'])
        self.assertIn('不设置生活模块',value['presentation'])
        self.assertIn('300–2000',value['presentation'])
        self.assertIn('3400',value['presentation'])
        self.assertNotIn('不作重要/紧急分类、量化完成率或时间利用率评估',value['presentation'])

    def test_status_never_exposes_old_policy_details(self):
        set_status(self.config, self.policy, self.day, 'private-stage', pending_jobs=1)
        value = read_input(self.config, Policy({'names':['private']}), self.day)
        self.assertNotIn('upstream', value)

    def test_policy_and_disabled_source_changes_block_old_input(self):
        snapshot(self.config, self.policy, self.day, 'PRIVATE_FACT', ready=True)
        for cfg, policy in ((self.config, Policy({'names': ['private']})),
                            ({**self.config, 'sources': {'git': {'enabled': False}}}, self.policy),
                            ({**self.config, 'collection_enabled': False}, self.policy)):
            result = read_input(cfg, policy, self.day)
            self.assertFalse(result['ok']); self.assertNotIn('PRIVATE_FACT', json.dumps(result))

    def test_api_input_matches_and_reading_does_not_change_database(self):
        store = Store(self.root / 'runtime/events.sqlite'); self.addCleanup(store.close)
        store.put(make_event('manual', 'x', self.day+'T10:00:00+08:00', 'Work', 'FACT'))
        prompts = []
        def model(prompt):
            prompts.append(prompt)
            return json.dumps({'report_markdown': '暂无可复盘的工作事项。', 'lessons': [], 'actions': []})
        result = build_report(store, self.config, self.policy, self.day, self.root/'reports', model)
        self.assertTrue(result['ok'])
        before = list(store.db.iterdump())
        value = read_input(self.config, self.policy, self.day)
        self.assertEqual(value['prompt'], prompts[-1])
        self.assertNotIn('暂无可复盘的工作事项。', value['prompt'].split('当天事件：')[-1])
        self.assertEqual(list(store.db.iterdump()), before)

    def test_comparison_failure_does_not_break_api_report(self):
        store = Store(self.root / 'runtime/events.sqlite'); self.addCleanup(store.close)
        store.put(make_event('manual', 'x', self.day+'T10:00:00+08:00', 'Work', 'FACT'))
        with patch('daily_review.comparison.snapshot', side_effect=OSError('disk')), contextlib.redirect_stderr(io.StringIO()):
            result = build_report(store, self.config, self.policy, self.day, self.root/'reports',
                                  lambda p: json.dumps({'report_markdown': '暂无可复盘的工作事项。', 'lessons': [], 'actions': []}))
        self.assertTrue(result['ok'])

    def test_rejected_api_draft_never_enters_comparison_input(self):
        store = Store(self.root / 'runtime/events.sqlite'); self.addCleanup(store.close)
        store.put(make_event('manual', 'x', self.day+'T10:00:00+08:00', 'Work', 'FACT'))
        config = {**self.config, 'model': {'audit_reports': True}}
        outputs = iter([{'report_markdown': '暂无可复盘的工作事项。', 'lessons': [], 'actions': []},
                        {'passed': False, 'reason': 'API_REJECTED_DRAFT_MARKER', 'claims': []},
                        {'report_markdown': '暂无可复盘的工作事项。', 'lessons': [], 'actions': []},
                        {'passed': True, 'reason': '通过', 'claims': []}])
        result = build_report(store, config, self.policy, self.day, self.root/'reports',
                              lambda prompt: json.dumps(next(outputs)))
        self.assertTrue(result['ok'], result)
        value = read_input(config, self.policy, self.day)
        self.assertNotIn('API_REJECTED_DRAFT_MARKER', value['prompt'])
        self.assertNotIn('修订要求', value['prompt'])

    def test_cli_default_date_is_yesterday_without_report_or_database_write(self):
        config = self.root/'config.json'; config.write_text(json.dumps(self.config))
        snapshot(self.config, self.policy, self.day, 'facts', ready=True)
        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime.fromisoformat('2026-11-16T08:00:00+08:00').astimezone(tz)
        output = io.StringIO()
        with patch('sys.argv', ['daily-review', '--config', str(config), 'comparison-input']), \
             patch('daily_review.cli.datetime', Clock), contextlib.redirect_stdout(output):
            self.assertEqual(cli.main(), 0)
        self.assertEqual(json.loads(output.getvalue())['date'], self.day)
        self.assertFalse((self.root/'reports').exists())
        self.assertFalse((self.root/'runtime/events.sqlite').exists())

    def test_eight_am_schedule_uses_beijing_not_host_timezone(self):
        cfg = {'timezone': 'Asia/Shanghai', 'schedule': {'hour': 8, 'minute': 0, 'start_date': self.day}}
        before = datetime.fromisoformat('2026-11-15T23:59:59+00:00')
        due = datetime.fromisoformat('2026-11-16T00:00:00+00:00')
        self.assertFalse(cli.schedule_reached(cfg, before))
        self.assertEqual(cli.due_days(cfg, before, '2026-11-14'), [])
        self.assertTrue(cli.schedule_reached(cfg, due))
        self.assertEqual(cli.due_days(cfg, due, '2026-11-14'), [self.day])
