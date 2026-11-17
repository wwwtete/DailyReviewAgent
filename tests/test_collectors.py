import json
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from datetime import datetime, timezone
from pathlib import Path

from daily_review.core import Policy
from daily_review.collectors import (GitCoverageGap, _git_read, codex_events, browser_events,
                                     git_events, lark_events, git_repositories)


class CollectorTests(unittest.TestCase):
    def test_codex_denied_rollout_is_not_opened(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            c = sqlite3.connect(root / "state_5.sqlite")
            c.execute("CREATE TABLE threads(id,rollout_path,cwd,title,updated_at,project_id)")
            c.execute("INSERT INTO threads VALUES(?,?,?,?,?,?)", ("deny", "/does/not/exist", "/private", "secret", 9999999999, None))
            c.commit(); c.close()
            self.assertEqual(list(codex_events({"home": t}, "2026-11-10", "Asia/Shanghai",
                                              Policy({"codex_ids": ["deny"]}))), [])

    def test_codex_collects_only_target_day_and_no_duplicate_user_echo(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t); log = root / "rollout.jsonl"
            lines = [
                {"timestamp": "2026-11-09T17:00:00Z", "type": "event_msg", "payload": {"type": "user_message", "message": "do it"}},
                {"timestamp": "2026-11-09T17:00:00Z", "type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "do it"}]}},
                {"timestamp": "2026-11-10T02:00:00Z", "type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "done"}]}},
                {"timestamp": "2026-11-10T17:00:00Z", "type": "event_msg", "payload": {"type": "agent_message", "message": "tomorrow"}},
            ]
            log.write_text("\n".join(json.dumps(x) for x in lines))
            c = sqlite3.connect(root / "state_5.sqlite")
            c.execute("CREATE TABLE threads(id,rollout_path,cwd,title,updated_at,project_id)")
            c.execute("INSERT INTO threads VALUES(?,?,?,?,?,?)", ("ok", str(log), "/work", "task", 9999999999, None))
            c.commit(); c.close()
            events = list(codex_events({"home": t}, "2026-11-10", "Asia/Shanghai", Policy({})))
            self.assertEqual([x["body"] for x in events], ["user: do it", "assistant: done"])

    def test_browser_blacklist_and_half_open_day(self):
        with tempfile.TemporaryDirectory() as t:
            db = Path(t) / "History"
            c = sqlite3.connect(db)
            c.executescript("CREATE TABLE urls(id INTEGER,url TEXT,title TEXT); CREATE TABLE visits(id INTEGER,url INTEGER,visit_time INTEGER);")
            for i, url in enumerate(["https://blocked.example/a", "https://good.example/a?token=secret", "https://good.example/tomorrow"], 1):
                c.execute("INSERT INTO urls VALUES(?,?,?)", (i, url, "Title"))
                when = "2026-11-09T17:00:00+00:00" if i < 3 else "2026-11-10T16:00:00+00:00"
                n = int((datetime.fromisoformat(when).timestamp() + 11644473600) * 1e6)
                c.execute("INSERT INTO visits VALUES(?,?,?)", (i, i, n))
            c.commit(); c.close()
            config = {"profiles": [{"kind": "chromium", "path": str(db), "name": "fixture"}], "discover": False}
            result = list(browser_events(config, "2026-11-10", "Asia/Shanghai", Policy({"domains": ["blocked.example"]})))
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0]["url"], "https://good.example/a")
            self.assertNotIn("secret", json.dumps(result))

    def test_git_commit_and_denied_repo(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            def git(*args):
                return subprocess.run(["git", "-C", t, *args], check=True, capture_output=True, text=True).stdout
            git("init"); git("config", "user.name", "Test"); git("config", "user.email", "test@example.com")
            (root / "a.txt").write_text("a")
            git("add", "."); git("commit", "-m", "A concrete delivery")
            day = datetime.now(timezone.utc).date().isoformat()
            cfg = {"roots": [t], "discover_from_codex": False}
            found = list(git_events(cfg, day, "UTC", Policy({})))
            self.assertTrue(any("A concrete delivery" in e["body"] for e in found))
            self.assertEqual(list(git_events(cfg, day, "UTC", Policy({"paths": [t]}))), [])

    def test_lark_denied_chat_no_body_call_and_pagination(self):
        calls = []
        def cli(args):
            calls.append(args)
            if args[:2] == ["im", "+chat-list"]:
                if "--page-token" in args:
                    return {"chats": [{"chat_id": "ok", "name": "Work"}], "has_more": False}
                return {"chats": [{"chat_id": "deny", "name": "Private"}], "has_more": True, "page_token": "p2"}
            return {"messages": [{"message_id": "m1", "create_time": "1794272400000", "body": {"content": "hello"}}], "has_more": False}
        cfg = {"messages": True, "tasks": False, "calendar": False, "meetings": False}
        result = list(lark_events(cfg, "2026-11-10", "Asia/Shanghai", Policy({"lark_chat_ids": ["deny"]}), cli))
        self.assertEqual(len(result), 1)
        self.assertFalse(any("deny" in call for call in calls if call[:2] != ["im", "+chat-list"]))

    def test_lark_broken_pagination_is_error(self):
        def cli(args):
            return {"chats": [], "has_more": True}
        with self.assertRaises(RuntimeError):
            list(lark_events({"messages": True, "tasks": False, "calendar": False, "meetings": False},
                             "2026-11-10", "Asia/Shanghai", Policy({}), cli))

    def test_browser_exclusive_lock_uses_private_snapshot(self):
        with tempfile.TemporaryDirectory() as t:
            db = Path(t) / "History"
            c = sqlite3.connect(db)
            c.executescript("CREATE TABLE urls(id,url,title); CREATE TABLE visits(id,url,visit_time);")
            c.execute("INSERT INTO urls VALUES(1,'https://example.com','Read me')")
            n = int((datetime.fromisoformat('2026-11-09T17:00:00+00:00').timestamp() + 11644473600) * 1e6)
            c.execute("INSERT INTO visits VALUES(1,1,?)", (n,)); c.commit()
            c.execute("PRAGMA locking_mode=EXCLUSIVE"); c.execute("BEGIN EXCLUSIVE")
            try:
                cfg = {"discover": False, "profiles": [{"kind": "chromium", "path": str(db), "name": "locked"}]}
                result = list(browser_events(cfg, "2026-11-10", "Asia/Shanghai", Policy({})))
                self.assertEqual(len(result), 1)
            finally:
                c.rollback(); c.close()

    def test_unknown_lark_shape_does_not_look_empty(self):
        def cli(args): return {"unexpected": [], "has_more": False}
        with self.assertRaises(RuntimeError):
            list(lark_events({"messages": True, "tasks": False, "calendar": False, "meetings": False},
                             "2026-11-10", "Asia/Shanghai", Policy({}), cli))

    def test_lark_local_timestamp_has_configured_timezone(self):
        def cli(args):
            if args[:2] == ["im", "+chat-list"]:
                return {"chats": [{"chat_id": "c", "name": "work"}], "has_more": False}
            return {"messages": [{"message_id": "m", "create_time": "2026-11-10 01:15", "content": "fact"}], "has_more": False}
        result = list(lark_events({"messages": True, "tasks": False, "calendar": False, "meetings": False},
                                 "2026-11-10", "Asia/Shanghai", Policy({}), cli))
        self.assertEqual(result[0]["at"], "2026-11-09T17:15:00+00:00")

    def test_git_discovery_independent_of_codex(self):
        with tempfile.TemporaryDirectory() as t:
            subprocess.run(['git', '-C', t, 'init'], check=True, capture_output=True)
            repos, errors = git_repositories({'roots': [t], 'codex_home': t + '/missing'}, Policy({}))
            self.assertEqual(repos, [Path(t).resolve()])
            self.assertEqual(errors, {'permanent': [], 'transient': []})

    def test_git_permission_failure_is_explicit_terminal_coverage_gap(self):
        with tempfile.TemporaryDirectory() as t:
            error = PermissionError(13, 'denied', str(Path(t)/'private'))
            def walk(root, **kwargs):
                kwargs['onerror'](error)
                return []
            with patch('daily_review.collectors.os.walk',side_effect=walk):
                with self.assertRaises(GitCoverageGap) as caught:
                    list(git_events({'roots':[t],'discover_from_codex':False},
                                    '2026-11-10','UTC',Policy({})))
            self.assertIn('permission_denied',str(caught.exception))
            self.assertIn(str(Path(t)/'private'),str(caught.exception))

    def test_git_transient_command_failure_retries_in_current_collection(self):
        timeout = subprocess.TimeoutExpired(['git'], 1)
        success = subprocess.CompletedProcess(['git'],0,'ok','')
        with patch('daily_review.collectors.subprocess.run',side_effect=[timeout,success]) as run:
            self.assertEqual(_git_read(['git','status'],timeout=1,retries=2),'ok')
        self.assertEqual(run.call_count,2)

    def test_git_rev_parse_permission_failure_is_a_coverage_gap(self):
        repo = Path('/work/private-repo')
        no_remote = subprocess.CompletedProcess(['git'], 1, '', '')
        denied = subprocess.CompletedProcess(['git'], 128, '', 'fatal: Permission denied')
        with patch('daily_review.collectors.git_repositories',
                   return_value=([repo], {'permanent': [], 'transient': []})), \
             patch('daily_review.collectors.subprocess.run', side_effect=[no_remote, denied]):
            with self.assertRaises(GitCoverageGap) as caught:
                list(git_events({'discover_from_codex': False}, '2026-11-10', 'UTC', Policy({})))
        self.assertIn(str(repo), str(caught.exception))
        self.assertIn('permission_denied', str(caught.exception))

    def test_git_remote_permission_failure_is_a_coverage_gap(self):
        repo = Path('/work/private-repo')
        denied = subprocess.CompletedProcess(['git'], 128, '', 'fatal: Operation not permitted')
        with patch('daily_review.collectors.git_repositories',
                   return_value=([repo], {'permanent': [], 'transient': []})), \
             patch('daily_review.collectors.subprocess.run', return_value=denied):
            with self.assertRaises(GitCoverageGap) as caught:
                list(git_events({'discover_from_codex': False}, '2026-11-10', 'UTC', Policy({})))
        self.assertIn(str(repo), str(caught.exception))
        self.assertIn('permission_denied', str(caught.exception))

    def test_calendar_all_day_date(self):
        def cli(args): return [{'event_id': 'e', 'summary': 'All day', 'start_time': {'date': '2026-11-10'}}]
        events = list(lark_events({'messages': False, 'tasks': False, 'calendar': True, 'meetings': False},
                                 '2026-11-10', 'Asia/Shanghai', Policy({}), cli))
        self.assertEqual(events[0]['at'], '2026-11-09T16:00:00+00:00')

    def test_git_denied_subdirectory_commit_body_not_collected(self):
        with tempfile.TemporaryDirectory() as t:
            def git(*args): return subprocess.run(['git', '-C', t, *args], check=True, capture_output=True)
            git('init'); git('config', 'user.name', 'Test'); git('config', 'user.email', 't@example.com')
            p = Path(t) / 'private'; p.mkdir(); (p/'secret.txt').write_text('secret')
            git('add', '.'); git('commit', '-m', 'PRIVATE DESCRIPTION')
            result = list(git_events({'roots': [t], 'discover_from_codex': False}, datetime.now(timezone.utc).date().isoformat(), 'UTC', Policy({'paths': [str(p)]})))
            self.assertFalse(any('PRIVATE DESCRIPTION' in e['body'] for e in result))


if __name__ == "__main__":
    unittest.main()
