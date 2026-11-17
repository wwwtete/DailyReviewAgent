import fnmatch
import hashlib
import json
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo


def stamp(value):
    if isinstance(value, dict):
        value = value.get("timestamp") or value.get("date")
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
        n = float(value)
        if n > 1e12:
            n /= 1000
        return datetime.fromtimestamp(n, timezone.utc)
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("Timestamp must include timezone")
    return dt


def day_window(day, tz):
    start = datetime.fromisoformat(day).replace(tzinfo=ZoneInfo(tz))
    return start, start + timedelta(days=1)


def safe_url(url):
    try:
        p = urlsplit(url)
        if p.scheme not in ("http", "https"):
            return ""
        host = p.hostname or ""
        if p.port:
            host += ":" + str(p.port)
        return urlunsplit((p.scheme, host, p.path, "", ""))
    except ValueError:
        return ""


class Policy:
    KEYS = {"sources", "paths", "names", "codex_ids", "project_ids", "lark_chat_ids",
            "lark_ids", "domains", "urls", "git_remotes", "browser_profiles"}

    def __init__(self, rules):
        unknown = set(rules) - self.KEYS
        if unknown:
            raise ValueError("Unknown blacklist fields: " + ", ".join(sorted(unknown)))
        for key, value in rules.items():
            if not isinstance(value, list) or any(not isinstance(x, str) or not x for x in value):
                raise ValueError("Blacklist values must be lists of nonempty strings: " + key)
        self.rules = rules
        self.fingerprint = hashlib.sha256(json.dumps(rules, sort_keys=True).encode()).hexdigest()

    def matches(self, key, value):
        return any(fnmatch.fnmatchcase(str(value or "").casefold(), p.casefold())
                   for p in self.rules.get(key, []))

    def allows(self, meta):
        source = meta.get("source", "")
        if self.matches("sources", source) or self.matches("sources", source.split("_")[0]) or self.matches("names", meta.get("title")):
            return False
        for key, field in (("project_ids", "project_id"), ("git_remotes", "remote"),
                           ("browser_profiles", "profile")):
            if self.matches(key, meta.get(field)):
                return False
        if source == "codex" and self.matches("codex_ids", meta.get("scope_id")):
            return False
        if source.startswith("lark") and (self.matches("lark_chat_ids", meta.get("scope_id")) or
                                           self.matches("lark_ids", meta.get("scope_id"))):
            return False
        if meta.get("path"):
            path = str(Path(meta["path"]).expanduser().resolve())
            for pattern in self.rules.get("paths", []):
                p = str(Path(pattern).expanduser().resolve())
                if fnmatch.fnmatchcase(path, p) or path == p or path.startswith(p.rstrip("/") + "/"):
                    return False
        url = meta.get("url", "")
        if self.matches("urls", url):
            return False
        try:
            host = (urlsplit(url).hostname or "").lower().rstrip(".")
        except ValueError:
            return False
        for domain in self.rules.get("domains", []):
            d = domain.lower().rstrip(".").removeprefix("*.")
            if host and (host == d or host.endswith("." + d)):
                return False
        body = str(meta.get("body", ""))
        if body:
            # Conservative cross-source reference filter; semantic aliases need explicit rules.
            for key in ("codex_ids", "project_ids", "lark_chat_ids", "lark_ids"):
                if any(p in body for p in self.rules.get(key, []) if not any(c in p for c in "*?[")):
                    return False
            for pattern in self.rules.get("names", []):
                if fnmatch.fnmatchcase(body.casefold(), "*" + pattern.casefold() + "*"):
                    return False
            for pattern in self.rules.get("paths", []):
                literal = str(Path(pattern).expanduser())
                if not any(c in literal for c in "*?[") and re.search(re.escape(literal.rstrip("/")) + r"(?:/|\s|[\"'<>]|$)", body):
                    return False
            for linked in re.findall(r"https?://[^\s\"'<>]+", body):
                if not self.allows({"url": linked}):
                    return False
        return True


def make_event(source, ident, occurred_at, title, body, **meta):
    return {"id": source + ":" + str(ident), "source": source,
            "at": stamp(occurred_at).astimezone(timezone.utc).isoformat(),
            "title": str(title), "body": str(body), **meta}


class Store:
    def __init__(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        os.chmod(path, 0o600)
        self.db.executescript('''
          PRAGMA journal_mode=WAL;
          CREATE TABLE IF NOT EXISTS events (id TEXT PRIMARY KEY, at TEXT NOT NULL, data TEXT NOT NULL);
          CREATE INDEX IF NOT EXISTS events_at ON events(at);
          CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS runs (day TEXT, source TEXT, at TEXT, status TEXT, count INTEGER, detail TEXT,
            PRIMARY KEY(day, source));
          CREATE TABLE IF NOT EXISTS reports (day TEXT PRIMARY KEY, fingerprint TEXT, data TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS actions (id TEXT PRIMARY KEY, day TEXT, fingerprint TEXT, data TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'proposed', note TEXT NOT NULL DEFAULT '');
        ''')

    def put(self, event):
        self.db.execute("INSERT INTO events VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET at=excluded.at,data=excluded.data",
                        (event["id"], event["at"], json.dumps(event, ensure_ascii=False)))
        self.db.commit()

    def events(self, day, tz, policy):
        a, b = day_window(day, tz)
        rows = self.db.execute("SELECT data FROM events WHERE at>=? AND at<? ORDER BY at,id",
                               (a.astimezone(timezone.utc).isoformat(), b.astimezone(timezone.utc).isoformat()))
        return [e for (raw,) in rows if policy.allows(e := json.loads(raw))]

    def get(self, key, default=None):
        row = self.db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO state VALUES (?,?)", (key, json.dumps(value)))
        self.db.commit()

    def run_status(self, day, source, status, count, detail=""):
        self.db.execute("INSERT OR REPLACE INTO runs VALUES(?,?,?,?,?,?)",
                        (day, source, datetime.now(timezone.utc).isoformat(), status, count, detail))
        self.db.commit()

    def statuses(self, day):
        return [dict(zip(("source", "status", "count", "detail"), r)) for r in
                self.db.execute("SELECT source,status,count,detail FROM runs WHERE day=? ORDER BY source", (day,))]

    def reconcile(self, day, tz, source, present_ids):
        a, b = day_window(day, tz)
        present = set(present_ids)
        rows = self.db.execute("SELECT id FROM events WHERE at>=? AND at<? AND (json_extract(data,'$.source')=? OR json_extract(data,'$.source') LIKE ?)",
                               (a.astimezone(timezone.utc).isoformat(), b.astimezone(timezone.utc).isoformat(), source, source + "_%")).fetchall()
        self.db.executemany("DELETE FROM events WHERE id=?", [(r[0],) for r in rows if r[0] not in present])
        self.db.commit()

    def actions(self, policy, through_day="9999-12-31"):
        return [{**json.loads(row[1]), "id": row[0], "status": row[2], "note": row[3]} for row in
                self.db.execute("SELECT id,data,status,note FROM actions WHERE fingerprint=? AND day<=? ORDER BY day",
                                (policy.fingerprint, through_day))]

    def close(self):
        self.db.close()
