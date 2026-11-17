"""Read-only source adapters. Resource discovery precedes body access."""
import hashlib
import errno
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .core import day_window, make_event, safe_url, stamp


class GitCoverageGap(RuntimeError):
    def __init__(self, gaps):
        self.gaps = list(dict.fromkeys(gaps))
        super().__init__('Git coverage gaps: ' + '; '.join(self.gaps))


class GitTransientFailure(RuntimeError):
    pass


def read_db(path):
    c = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA query_only=ON")
    return c


@contextmanager
def browser_db(path):
    """Query live DB when possible; recover a stable private copy if exclusively locked."""
    c = None
    temporary = None
    try:
        try:
            c = read_db(path)
            c.execute("SELECT count(*) FROM sqlite_master").fetchone()
        except sqlite3.OperationalError as error:
            if c:
                c.close(); c = None
            if "locked" not in str(error):
                raise
            temporary = tempfile.TemporaryDirectory(prefix="daily-review-browser-")
            target = Path(temporary.name) / "History"
            files = [Path(str(path) + suffix) for suffix in ("", "-wal", "-journal")]
            before = {str(p): (p.stat().st_size, p.stat().st_mtime_ns) for p in files if p.exists()}
            for p in files:
                if str(p) in before:
                    suffix = str(p)[len(str(path)):]
                    shutil.copyfile(p, str(target) + suffix)
                    os.chmod(str(target) + suffix, 0o600)
            after = {str(p): (p.stat().st_size, p.stat().st_mtime_ns) for p in files if p.exists()}
            if before != after:
                raise RuntimeError("Browser changed during snapshot; retry later")
            # Read/write only the disposable copy so SQLite can recover the copied journal.
            c = sqlite3.connect(target)
            c.row_factory = sqlite3.Row
            if c.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise RuntimeError("Browser snapshot integrity check failed")
            c.execute("PRAGMA query_only=ON")
        yield c
    finally:
        if c:
            c.close()
        if temporary:
            temporary.cleanup()


def codex_threads(home):
    dbs = sorted(Path(home).expanduser().glob("state_*.sqlite"),
                 key=lambda p: int(p.stem.split("_")[-1]), reverse=True)
    if not dbs:
        raise RuntimeError("Codex state database unavailable")
    c = read_db(dbs[0])
    try:
        columns = {r[1] for r in c.execute("PRAGMA table_info(threads)")}
        fields = [x for x in ("id", "rollout_path", "cwd", "title", "updated_at", "project_id") if x in columns]
        return [dict(r) for r in c.execute("SELECT " + ",".join(fields) + " FROM threads")]
    finally:
        c.close()


def codex_events(config, day, tz, policy):
    a, b = day_window(day, tz)
    errors = 0
    for row in codex_threads(config.get("home", "~/.codex")):
        meta = {"source": "codex", "scope_id": row["id"], "title": row.get("title", ""),
                "path": row.get("cwd", ""), "project_id": row.get("project_id", "")}
        if not policy.allows(meta) or float(row.get("updated_at", 0)) < a.timestamp():
            continue
        if not policy.allows({**meta, "path": row["rollout_path"]}):
            continue
        try:
            with Path(row["rollout_path"]).open() as f:
                for n, line in enumerate(f):
                    try:
                        r = json.loads(line)
                        if not r.get("timestamp") or not a <= stamp(r["timestamp"]) < b:
                            continue
                        p = r.get("payload", {})
                        # response_item is canonical; event_msg duplicates user/assistant text.
                        if r.get("type") != "response_item":
                            continue
                        if p.get("type") == "message" and p.get("role") in ("user", "assistant"):
                            text = "\n".join(x.get("text", "") for x in p.get("content", []) if isinstance(x, dict))
                            body = p["role"] + ": " + text
                        elif p.get("type") in ("function_call_output", "custom_tool_call_output"):
                            body = "tool evidence: " + str(p.get("output", ""))
                        else:
                            continue
                        yield make_event("codex", row["id"] + ":" + str(n), r["timestamp"], meta["title"], body,
                                         **{k: v for k, v in meta.items() if k not in ("source", "title")},
                                         url="codex://threads/" + row["id"])
                    except (ValueError, TypeError):
                        errors += 1
        except OSError:
            errors += 1
    if errors:
        raise RuntimeError("Codex unreadable or malformed records: " + str(errors))


def discover_browsers():
    support = Path.home() / "Library/Application Support"
    profiles = []
    for label, folder in (("Chrome", "Google/Chrome"), ("Edge", "Microsoft Edge"),
                          ("Brave", "BraveSoftware/Brave-Browser"), ("Chromium", "Chromium"),
                          ("Arc", "Arc/User Data")):
        root = support / folder
        if root.is_dir():
            for p in root.iterdir():
                if p.is_dir() and (p / "History").exists():
                    profiles.append({"kind": "chromium", "path": str(p / "History"), "name": label + "/" + p.name})
    root = support / "Firefox/Profiles"
    if root.is_dir():
        for p in root.iterdir():
            if (p / "places.sqlite").exists():
                profiles.append({"kind": "firefox", "path": str(p / "places.sqlite"), "name": "Firefox/" + p.name})
    safari = Path.home() / "Library/Safari/History.db"
    if safari.exists():
        profiles.append({"kind": "safari", "path": str(safari), "name": "Safari"})
    return profiles


def browser_events(config, day, tz, policy):
    a, b = day_window(day, tz)
    profiles = list(config.get("profiles", []))
    if config.get("discover", True):
        profiles += discover_browsers()
    errors, seen = 0, set()
    for profile in profiles:
        path = str(Path(profile["path"]).expanduser().resolve())
        meta = {"source": "browser", "path": path, "profile": profile["name"]}
        if path in seen or not policy.allows(meta):
            continue
        seen.add(path)
        db_context = None
        try:
            db_context = browser_db(path)
            c = db_context.__enter__()
            kind = profile["kind"]
            if kind == "chromium":
                offset, scale = 11644473600, 1000000
                query = "SELECT v.id,v.visit_time AS ts,u.url,u.id AS page FROM visits v JOIN urls u ON u.id=v.url WHERE v.visit_time>=? AND v.visit_time<?"
                title_sql = "SELECT title FROM urls WHERE id=?"
            elif kind == "firefox":
                offset, scale = 0, 1000000
                query = "SELECT v.id,v.visit_date AS ts,p.url,p.id AS page FROM moz_historyvisits v JOIN moz_places p ON p.id=v.place_id WHERE v.visit_date>=? AND v.visit_date<?"
                title_sql = "SELECT title FROM moz_places WHERE id=?"
            elif kind == "safari":
                offset, scale = -978307200, 1
                query = "SELECT v.id,v.visit_time AS ts,i.url,v.id AS page FROM history_visits v JOIN history_items i ON i.id=v.history_item WHERE v.visit_time>=? AND v.visit_time<?"
                title_sql = "SELECT title FROM history_visits WHERE id=?"
            else:
                raise ValueError("Unknown browser kind")
            for r in c.execute(query, ((a.timestamp() + offset) * scale, (b.timestamp() + offset) * scale)):
                if not policy.allows({**meta, "url": r["url"]}):
                    continue
                url = safe_url(r["url"])
                if not url:
                    continue
                title = (c.execute(title_sql, (r["page"],)).fetchone()[0] or url)
                if not policy.allows({**meta, "url": r["url"], "title": title}):
                    continue
                ts = datetime.fromtimestamp(r["ts"] / scale - offset, timezone.utc)
                ident = hashlib.sha256(path.encode()).hexdigest()[:12] + ":" + str(r["id"])
                yield make_event("browser", ident, ts, title, "浏览访问记录；不代表阅读时长或工作成果。", url=url,
                                 **{k: v for k, v in meta.items() if k != "source"})
        except (OSError, sqlite3.Error, ValueError, RuntimeError):
            errors += 1
        finally:
            if db_context:
                db_context.__exit__(None, None, None)
    if errors:
        raise RuntimeError("Browser profiles unreadable: " + str(errors) + "; check macOS file access")


def command(args, timeout=90):
    env = {**os.environ, "LARKSUITE_CLI_NO_UPDATE_NOTIFIER": "1", "LARKSUITE_CLI_NO_SKILLS_NOTIFIER": "1",
           "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0", "TZ": "Asia/Shanghai"}
    r = subprocess.run(args, text=True, capture_output=True, timeout=timeout, env=env)
    if r.returncode:
        # Raw stderr can contain sensitive source data or credentials.
        raise RuntimeError(Path(args[0]).name + " failed (exit " + str(r.returncode) + ")")
    return r.stdout


def _git_location(args):
    try:
        return str(Path(args[args.index('-C') + 1]).resolve())
    except (ValueError, IndexError):
        return str(args[0])


def _git_failure(args, error):
    location = _git_location(args)
    if isinstance(error, subprocess.TimeoutExpired):
        return 'transient', location + ': timeout'
    if isinstance(error, OSError):
        if error.errno in (errno.EACCES, errno.EPERM):
            return 'permanent', location + ': permission_denied'
        if error.errno in (errno.ENOENT, errno.ENOTDIR):
            return 'permanent', location + ': path_unavailable'
        return 'transient', location + ': io_error'
    message = ((getattr(error, 'stderr', '') or '') + ' ' +
               (getattr(error, 'stdout', '') or '')).casefold()
    permanent = {
        'permission_denied': ('permission denied', 'operation not permitted'),
        'path_unavailable': ('no such file or directory', 'cannot chdir', 'does not exist'),
        'not_a_repository': ('not a git repository',),
        'repository_unreadable': ('bad object', 'unknown revision', 'ambiguous argument',
                                  'object file', 'unable to read'),
    }
    transient = ('index.lock', 'another git process', 'temporarily unavailable',
                 'resource busy', 'timed out', 'could not resolve host',
                 'connection reset', 'connection refused', 'network is unreachable')
    for category, markers in permanent.items():
        if any(marker in message for marker in markers):
            return 'permanent', location + ': ' + category
    if any(marker in message for marker in transient):
        return 'transient', location + ': temporary_failure'
    return 'unknown', location + ': stable_read_failure'


def _git_read(args, timeout=90, retries=2, missing=None):
    """Read Git metadata, retrying transient/unknown failures in this collection."""
    last = None
    for attempt in range(retries + 1):
        try:
            result = subprocess.run(args, text=True, capture_output=True, timeout=timeout,
                                    env={**os.environ, 'GIT_OPTIONAL_LOCKS': '0',
                                         'GIT_TERMINAL_PROMPT': '0', 'TZ': 'Asia/Shanghai'})
            if result.returncode == 0:
                return result.stdout
            if missing and missing(result):
                return None
            last = subprocess.CalledProcessError(result.returncode, args, result.stdout, result.stderr)
        except (OSError, subprocess.TimeoutExpired) as error:
            last = error
        kind, detail = _git_failure(args, last)
        if kind == 'permanent':
            raise GitCoverageGap([detail])
        if attempt == retries:
            if kind == 'unknown':
                raise GitCoverageGap([detail])
            raise GitTransientFailure('Git temporary read failure after %d attempts: %s' %
                                      (retries + 1, detail))


def git_repositories(config, policy):
    roots = [Path(x).expanduser() for x in config.get("roots", [str(Path.home())])]
    if config.get("discover_from_codex", True):
        try:
            threads = codex_threads(config.get("codex_home", "~/.codex"))
        except (OSError, sqlite3.Error, RuntimeError):
            threads = []
        for t in threads:
            if policy.allows({"source": "git", "path": t.get("cwd", ""), "title": Path(t.get("cwd", "")).name}):
                roots.append(Path(t["cwd"]))
    seen, repos = set(), set()
    failures = {'permanent': [], 'transient': []}
    prune = {".git", "node_modules", ".venv", "venv", ".Trash", "Library", ".cache", ".npm", ".gradle", "Pods"}
    def walk_error(error):
        path = str(Path(error.filename or '').expanduser())
        if error.errno in (errno.EACCES, errno.EPERM):
            failures['permanent'].append(path + ': permission_denied')
        elif error.errno in (errno.ENOENT, errno.ENOTDIR):
            failures['permanent'].append(path + ': path_unavailable')
        else:
            failures['transient'].append(path + ': io_error')
    for root in roots:
        if not policy.allows({"source": "git", "path": str(root), "title": root.name}):
            continue
        try:
            if not root.is_dir():
                failures['permanent'].append(str(root) + ': path_unavailable')
                continue
        except OSError as error:
            walk_error(error); continue
        for folder, dirs, files in os.walk(root, followlinks=False, onerror=walk_error):
            path = Path(folder).resolve()
            if path in seen or not policy.allows({"source": "git", "path": str(path), "title": path.name}):
                dirs[:] = []
                continue
            seen.add(path)
            if ".git" in dirs or ".git" in files:
                repos.add(path)
            dirs[:] = [d for d in dirs if d not in prune and not Path(folder, d).is_symlink() and
                       policy.allows({"source": "git", "path": str(path / d), "title": d})]
    failures = {key: list(dict.fromkeys(value)) for key, value in failures.items()}
    return sorted(repos), failures


def git_events(config, day, tz, policy):
    a, b = day_window(day, tz)
    repos, discovery = git_repositories(config, policy)
    gaps = list(discovery['permanent'])
    transient = len(discovery['transient'])
    now = datetime.now(timezone.utc)
    for repo in repos:
        base = [config.get("binary", "git"), "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null", "-C", str(repo)]
        try:
            # Local configuration is metadata; no network or repository code is executed.
            remote = (_git_read(base + ["config", "--get", "remote.origin.url"],
                                missing=lambda result: result.returncode == 1 and
                                not result.stdout.strip() and not result.stderr.strip()) or '').strip()
            meta = {"source": "git", "path": str(repo), "title": repo.name, "remote": remote}
            if not policy.allows(meta):
                continue
            head = _git_read(base + ["rev-parse", "--verify", "HEAD"],
                             missing=lambda result: result.returncode == 128 and
                             result.stderr.strip().casefold() == 'fatal: needed a single revision')
            if head is not None:
                # since-as-filter avoids dropping commits with non-monotonic committer dates.
                hashes = _git_read(base + ["log", "--all", "--since-as-filter=" + a.isoformat(),
                                           "--until=" + b.isoformat(), "--format=%H"]).splitlines()
                for sha in dict.fromkeys(hashes):
                    files = _git_read(base + ["diff-tree", "--root", "--no-commit-id", "--name-only", "-r", sha]).strip()
                    # Skip the entire commit before reading its message if any changed path is denied.
                    if any(not policy.allows({"source": "git", "path": str(repo / f), "title": f}) for f in files.splitlines()):
                        continue
                    raw = _git_read(base + ["show", "-s", "--format=%cI%x00%an%x00%ae%x00%s", sha]).strip().split("\0", 3)
                    if len(raw) != 4 or not a <= stamp(raw[0]) < b:
                        continue
                    body = "提交：" + raw[3] + "\n作者：" + raw[1] + " <" + raw[2] + ">\n变更文件：\n" + files
                    yield make_event("git", hashlib.sha256(str(repo).encode()).hexdigest()[:12] + ":" + sha,
                                     raw[0], repo.name, body, path=str(repo), remote=safe_url(remote) or remote,
                                     scope_id=sha, author=raw[1])
            if a <= now < b:
                status = _git_read(base + ["status", "--porcelain=v1", "--untracked-files=normal"]).strip()
                status = "\n".join(line for line in status.splitlines() if all(
                    policy.allows({"source": "git", "path": str(repo / f.strip('"')), "title": f})
                    for f in line[3:].split(" -> ")))
                if status:
                    yield make_event("git", str(repo) + ":status:" + day, now, repo.name,
                                     "当前未提交状态快照；不能确定修改发生时间或贡献归属：\n" + status,
                                     path=str(repo), remote=safe_url(remote) or remote)
        except GitCoverageGap as error:
            gaps.extend(error.gaps)
        except GitTransientFailure:
            transient += 1
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
            kind, detail = _git_failure(base, error)
            if kind == 'permanent': gaps.append(detail)
            else: transient += 1
    if transient:
        suffix = '; coverage gaps: ' + '; '.join(dict.fromkeys(gaps)) if gaps else ''
        raise GitTransientFailure('Git temporary failures after retries: ' + str(transient) + suffix)
    if gaps:
        raise GitCoverageGap(gaps)


def lark_call(binary, args):
    data = json.loads(command([binary, *args, "--as", "user", "--format", "json"]))
    if data.get("ok") is not True:
        raise RuntimeError("Lark returned unsuccessful envelope")
    return data.get("data", {})


def lark_stamp(value, tz):
    if isinstance(value, dict):
        value = value.get("timestamp") or value.get("date")
    try:
        return stamp(value)
    except ValueError:
        dt = datetime.fromisoformat(str(value))
        if dt.tzinfo is None:
            return dt.replace(tzinfo=ZoneInfo(tz))
        raise


def pages(cli, args, keys):
    seen = set()
    token = None
    while True:
        data = cli(args + (["--page-token", token] if token else []))
        items = data if isinstance(data, list) else next((data[k] for k in keys if isinstance(data.get(k), list)), None)
        if items is None:
            raise RuntimeError("Unrecognized Lark list response")
        yield from items
        if isinstance(data, list) or not data.get("has_more"):
            return
        token = data.get("page_token") or data.get("next_page_token")
        if not token or token in seen:
            raise RuntimeError("Incomplete pagination")
        seen.add(token)


def lark_events(config, day, tz, policy, cli=None):
    cli = cli or (lambda args: lark_call(config.get("binary", "lark-cli"), args))
    a, b = day_window(day, tz)
    errors = []
    if config.get("messages", True) and policy.allows({"source": "lark_messages"}):
        try:
            for chat in pages(cli, ["im", "+chat-list", "--types", "p2p,group", "--page-size", "100"], ("chats", "items")):
                cid = chat["chat_id"]
                meta = {"source": "lark_messages", "scope_id": cid, "title": chat.get("name", cid)}
                if not policy.allows(meta):
                    continue
                try:
                    args = ["im", "+chat-messages-list", "--chat-id", cid, "--start", a.isoformat(),
                            "--end", b.isoformat(), "--order", "asc", "--no-reactions"]
                    for msg in pages(cli, args, ("messages", "items")):
                        at = msg.get("create_time") or msg.get("created_at")
                        if not at or not a <= lark_stamp(at, tz) < b:
                            continue
                        body = msg.get("body", msg.get("content", msg.get("text", "")))
                        if isinstance(body, dict):
                            body = body.get("content", json.dumps(body, ensure_ascii=False))
                        sender = msg.get("sender", {})
                        yield make_event("lark_messages", msg["message_id"], lark_stamp(at, tz), meta["title"], str(body),
                                         scope_id=cid, author=sender,
                                         **{k:msg[k] for k in ('message_type','msg_type','root_id','parent_id','thread_id') if msg.get(k)},
                                         url=msg.get("message_app_link", "https://applink.feishu.cn/client/chat/open?openChatId=" + cid))
                except (RuntimeError, ValueError, KeyError, OSError, subprocess.TimeoutExpired):
                    errors.append("chat")
        except (RuntimeError, ValueError, KeyError, OSError, subprocess.TimeoutExpired):
            errors.append("chat-list")
    if config.get("tasks", True) and policy.allows({"source": "lark_tasks"}):
        try:
            now = datetime.now(timezone.utc)
            for task in pages(cli, ["task", "+get-my-tasks"], ("tasks", "items")):
                tid = task.get("guid") or task.get("task_guid") or task.get("id")
                meta = {"source": "lark_tasks", "scope_id": tid, "title": task.get("summary", "任务")}
                if not tid or not policy.allows(meta):
                    continue
                completed = task.get("completed_at")
                occurred = completed if completed and str(completed) != "0" else task.get("created_at")
                if occurred and a <= lark_stamp(occurred, tz) < b:
                    at, label = occurred, "任务完成时间记录" if completed and str(completed) != "0" else "任务创建时间记录"
                elif a <= now < b:
                    at, label = now, "当前任务状态快照，不代表今天完成"
                else:
                    continue
                yield make_event("lark_tasks", str(tid) + ":" + day, lark_stamp(at, tz), meta["title"],
                                 label + "\n" + json.dumps(task, ensure_ascii=False), scope_id=tid, url=task.get("url", ""))
        except (RuntimeError, ValueError, KeyError, OSError, subprocess.TimeoutExpired):
            errors.append("tasks")
    if config.get("calendar", True) and policy.allows({"source": "lark_calendar"}):
        try:
            data = cli(["calendar", "+agenda", "--start", a.isoformat(), "--end", b.isoformat()])
            events = data if isinstance(data, list) else data.get("events", data.get("items"))
            if events is None or (isinstance(data, dict) and data.get("has_more")):
                raise RuntimeError("Calendar list shape/pagination incomplete")
            for item in events:
                ident = item.get("event_id") or item.get("id")
                meta = {"source": "lark_calendar", "scope_id": ident, "title": item.get("summary", "日程")}
                if policy.allows(meta):
                    at = lark_stamp(item.get("start_time", a), tz)
                    yield make_event("lark_calendar", str(ident) + ":" + day, max(a, at), meta["title"],
                                     "计划日程，未证明实际参加：\n" + json.dumps(item, ensure_ascii=False), scope_id=ident)
        except (RuntimeError, ValueError, KeyError, OSError, subprocess.TimeoutExpired):
            errors.append("calendar")
    if config.get("meetings", True) and policy.allows({"source": "lark_meetings"}):
        try:
            args = ["vc", "+search", "--start", a.isoformat(), "--end", (b - timedelta(seconds=1)).isoformat(), "--page-size", "30"]
            for item in pages(cli, args, ("items", "meetings")):
                ident = item.get("meeting_id") or item.get("id")
                title = item.get("topic", item.get("title", "会议"))
                if policy.allows({"source": "lark_meetings", "scope_id": ident, "title": title}):
                    at = item.get("start_time") or a
                    yield make_event("lark_meetings", str(ident), max(a, lark_stamp(at, tz)), title,
                                     "会议元数据，不含逐字稿：\n" + json.dumps(item, ensure_ascii=False), scope_id=ident)
        except (RuntimeError, ValueError, KeyError, OSError, subprocess.TimeoutExpired):
            errors.append("meetings")
    if errors:
        raise RuntimeError("Lark incomplete sections: " + ",".join(errors))
