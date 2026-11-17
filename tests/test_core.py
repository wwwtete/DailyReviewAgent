import tempfile
import unittest
from pathlib import Path

from daily_review.core import Policy, Store, day_window, make_event


class CoreTests(unittest.TestCase):
    def test_default_allow_and_deny_before_body(self):
        p = Policy({"paths": ["/projects/private"], "names": ["*机密*"],
                    "codex_ids": ["blocked"], "domains": ["private.example"]})
        self.assertTrue(p.allows({"source": "codex", "scope_id": "ok"}))
        self.assertFalse(p.allows({"source": "codex", "scope_id": "blocked"}))
        self.assertFalse(p.allows({"path": "/projects/private/sub"}))
        self.assertTrue(p.allows({"path": "/projects/private-other"}))
        self.assertFalse(p.allows({"title": "机密项目"}))
        self.assertFalse(p.allows({"url": "https://sub.private.example/a"}))
        self.assertTrue(p.allows({"url": "https://private.example.evil/a"}))

    def test_timezone_day_is_half_open(self):
        a, b = day_window("2026-11-10", "Asia/Shanghai")
        self.assertEqual(a.isoformat(), "2026-11-10T00:00:00+08:00")
        self.assertEqual((b - a).total_seconds(), 86400)

    def test_idempotence_and_retroactive_filter(self):
        with tempfile.TemporaryDirectory() as t:
            s = Store(Path(t) / "db.sqlite")
            e = make_event("codex", "e1", "2026-11-10T02:00:00+08:00", "hello", "fact", scope_id="x")
            s.put(e); s.put(e)
            self.assertEqual(len(s.events("2026-11-10", "Asia/Shanghai", Policy({}))), 1)
            self.assertEqual(s.events("2026-11-10", "Asia/Shanghai", Policy({"codex_ids": ["x"]})), [])
            s.close()

    def test_unknown_policy_field_fails_closed(self):
        with self.assertRaises(ValueError):
            Policy({"codex_id": ["typo"]})

    def test_disabled_source_family_and_body_references(self):
        self.assertFalse(Policy({"sources": ["lark"]}).allows({"source": "lark_messages"}))
        self.assertFalse(Policy({"lark_chat_ids": ["oc_private"]}).allows({"source": "codex", "body": "message from oc_private"}))
        self.assertFalse(Policy({"domains": ["private.example"]}).allows({"source": "lark_messages", "body": "https://private.example/a"}))

    def test_reconcile_removes_disappeared_snapshot(self):
        with tempfile.TemporaryDirectory() as t:
            s = Store(Path(t) / "db.sqlite")
            e = make_event("git", "status", "2026-11-10T01:00:00+08:00", "repo", "dirty")
            s.put(e)
            s.reconcile("2026-11-10", "Asia/Shanghai", "git", [])
            self.assertEqual(s.events("2026-11-10", "Asia/Shanghai", Policy({})), [])
            s.close()

    def test_past_report_cannot_see_future_actions(self):
        with tempfile.TemporaryDirectory() as t:
            s=Store(Path(t)/'db.sqlite');p=Policy({})
            s.db.execute('INSERT INTO actions(id,day,fingerprint,data) VALUES(?,?,?,?)',('future','2026-11-10',p.fingerprint,'{"title":"future"}'));s.db.commit()
            self.assertEqual(s.actions(p, '2026-11-09'),[])
            self.assertEqual(len(s.actions(p, '2026-11-10')),1);s.close()


if __name__ == "__main__":
    unittest.main()
