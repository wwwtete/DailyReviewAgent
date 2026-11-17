import json
import tempfile
import unittest
from pathlib import Path
try:
    from daily_review.archive_install import configure_hooks
except ImportError:
    configure_hooks = None


class InstallTests(unittest.TestCase):
    def test_merges_and_is_idempotent_without_overwriting_existing_hooks(self):
        self.assertIsNotNone(configure_hooks)
        with tempfile.TemporaryDirectory() as t:
            root = Path(t); home = root/'codex'; home.mkdir()
            p = home/'hooks.json'
            existing = {'hooks': {'Stop': [{'hooks': [{'type':'command','command':'existing-notification'}]}]}}
            p.write_text(json.dumps(existing))
            cfg = {'_root':str(root), 'sources':{'codex':{'home':str(home)}}}
            configure_hooks(cfg); configure_hooks(cfg)
            actual = json.loads(p.read_text())
            self.assertEqual(actual['hooks']['Stop'][0],existing['hooks']['Stop'][0])
            self.assertEqual(len(actual['hooks']['Stop']),2)
            for name in ('Stop','SessionEnd','Interrupt'):
                handler = actual['hooks'][name][-1]['hooks'][0]
                self.assertEqual(handler['timeout'],3)
                self.assertIn('archive-hook',handler['command'])
