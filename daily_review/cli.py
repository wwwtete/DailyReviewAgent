import argparse
import fcntl
import hashlib
import json
import os
import plistlib
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .collectors import (GitCoverageGap, browser_events, codex_events, discover_browsers,
                         git_events, lark_events)
from .core import Policy, Store, make_event
from .report import build_report, model_call, model_settings, write_private

ROOT = Path(__file__).resolve().parent.parent
LABEL = "local.daily-review-agent"


def load_config(path):
    path = Path(path).resolve()
    cfg = json.loads(path.read_text())
    Policy(cfg.get("blacklist", {}))
    ZoneInfo(cfg["timezone"])
    cfg["_path"] = str(path)
    cfg["_root"] = str(path.parent)
    return cfg


def due_days(config, now, last_day):
    zone = ZoneInfo(config["timezone"])
    local = now.astimezone(zone)
    schedule = config["schedule"]
    cutoff = local.replace(hour=schedule["hour"], minute=schedule["minute"], second=0, microsecond=0)
    end = local.date() - timedelta(days=1 if local >= cutoff else 2)
    if not last_day:
        start_date = schedule.get("start_date", end.isoformat())
        if end.isoformat() < start_date:
            return []
        last_day = (datetime.fromisoformat(start_date).date() - timedelta(days=1)).isoformat()
    start = datetime.fromisoformat(last_day).date() + timedelta(days=1)
    result = []
    while start <= end and len(result) < schedule.get("catchup_days", 3):
        result.append(start.isoformat()); start += timedelta(days=1)
    return result


def schedule_reached(config, now):
    local = now.astimezone(ZoneInfo(config['timezone']))
    return (local.hour, local.minute) >= (config['schedule']['hour'], config['schedule']['minute'])


def effective_policy(config):
    rules = {k: list(v) for k, v in config["blacklist"].items()}
    disabled = [k for k, v in config["sources"].items() if not v.get("enabled", True)]
    disabled += ["lark_" + k for k in ("messages", "tasks", "calendar", "meetings") if not config["sources"]["lark"].get(k, True)]
    rules["sources"] = rules.get("sources", []) + disabled
    # A Codex project ID has a concrete working path: carry its deny into Git discovery.
    if rules.get("project_ids"):
        from .collectors import codex_threads
        base = Policy(rules)
        try:
            for t in codex_threads(config["sources"]["codex"].get("home", "~/.codex")):
                if base.matches("project_ids", t.get("project_id")) and t.get("cwd"):
                    rules.setdefault("paths", []).append(t["cwd"])
        except Exception:
            raise RuntimeError("Cannot resolve denied project IDs; collection stopped")
    return Policy(rules)


def pending_days(regular, pending, limit):
    return list(dict.fromkeys(regular + sorted(pending)))[:limit]


def finalized_report(config, store, day, policy):
    path = Path(config['_root']) / 'reports' / (day + '.md')
    return path.exists() and store.db.execute(
        'SELECT 1 FROM reports WHERE day=?', (day,)).fetchone() is not None


def discard_finalized_retries(config, store, policy, days):
    finalized = {day for day in days if finalized_report(config, store, day, policy)}
    retry = set(store.get('retry_dates', [])) - finalized
    store.set('retry_dates', sorted(retry))
    if not retry:
        store.set('next_retry', 0)
    if finalized:
        store.db.executemany('DELETE FROM codex_dirty_days WHERE day=?',
                             [(day,) for day in finalized])
        store.db.commit()
    return retry


def disable_old_date_retries(store):
    """Old source retries are suspended; missing reports still come from due_days()."""
    old = set(store.get('retry_dates', []))
    active = set(store.get('report_retry_dates', []))
    store.set('retry_dates', sorted(old & active))
    if not old & active:
        store.set('next_retry', 0)


def _prefetch_key(day):
    return "report_prefetch:" + day


def _prefetch_signature(config, policy):
    settings = {"timezone": config["timezone"], "sources": config["sources"],
                "policy": policy.fingerprint}
    return hashlib.sha256(json.dumps(settings, sort_keys=True).encode()).hexdigest()


def _clear_prefetch(store, day):
    store.db.execute("DELETE FROM state WHERE key=?", (_prefetch_key(day),))
    store.db.commit()


def recover_scheduled_delivery(config, store, policy, now):
    local = now.astimezone(ZoneInfo(config['timezone']))
    day = (local.date() - timedelta(days=1)).isoformat()
    report = store.db.execute("SELECT 1 FROM reports WHERE day=? AND fingerprint=?",
                              (day, policy.fingerprint)).fetchone()
    path = Path(config['_root']) / 'reports' / (day + '.md')
    if not report or not path.exists():
        return None
    from .desktop import deliver
    return {'date': day, **deliver(store, {'ok': True, 'path': str(path)}, policy,
                                    config.get('desktop', {}))}


def collect(config, store, day, sources=None):
    policy = effective_policy(config)
    adapters = {"codex": codex_events, "lark": lark_events, "git": git_events, "browser": browser_events}
    for source, adapter in adapters.items():
        if sources is not None and source not in sources:
            continue
        source_config = config["sources"][source]
        if not source_config.get("enabled", True) or not policy.allows({"source": source}):
            store.run_status(day, source, "disabled", 0)
            continue
        if source == "codex" and source_config.get("mode") == "archive":
            from .archive import coverage
            try:
                state = coverage(store, day, policy)
                store.run_status(day, source, state["status"], state["count"], state["detail"])
            except Exception as error:
                store.run_status(day, source, "error", 0, type(error).__name__)
            continue
        count = 0
        ids = []
        store.run_status(day, source, "running", 0)
        try:
            for event in adapter(source_config, day, config["timezone"], policy):
                if policy.allows(event):
                    store.put(event); count += 1; ids.append(event["id"])
                    if count % 100 == 0:
                        store.run_status(day, source, "running", count)
            store.reconcile(day, config["timezone"], source, ids)
            store.run_status(day, source, "ok", count)
        except GitCoverageGap as error:
            store.reconcile(day, config["timezone"], source, ids)
            store.run_status(day, source, "limited", count, str(error))
        except Exception as error:
            # Exceptions from subprocesses intentionally carry no raw output.
            detail = str(error) if isinstance(error, RuntimeError) else type(error).__name__
            store.run_status(day, source, "partial" if count else "error", count, detail)
    return store.statuses(day)


def run_day(config, store, day, no_ai=False, sources=None, prefetch=False):
    policy = effective_policy(config)
    use_prefetch = prefetch and not no_ai and sources is None
    signature = _prefetch_signature(config, policy)
    cached = store.get(_prefetch_key(day), {}) if use_prefetch else {}
    attempted = set(cached.get('sources', [])) if cached.get('signature') == signature else set()
    archive_enabled = (config['sources'].get('codex', {}).get('mode') == 'archive'
                       and config['sources']['codex'].get('enabled', True)
                       and policy.allows({'source': 'codex'}))
    if archive_enabled:
        from .archive import coverage
        state = coverage(store, day, policy)
        store.run_status(day, 'codex', state['status'], state['count'], state['detail'])
        if not no_ai and state['status'] != 'ok':
            if not use_prefetch:
                return build_report(store, config, policy, day, Path(config['_root']) / 'reports')
            # Lark and Git are slow. Collect them while the independent Codex
            # worker drains its queue, then reuse that snapshot on the retry.
            remaining = [name for name in ('lark', 'git', 'browser') if name not in attempted]
            if remaining:
                collect(config, store, day, remaining)
                attempted.update(remaining)
                if use_prefetch:
                    store.set(_prefetch_key(day), {'signature': signature, 'sources': sorted(attempted)})
            state = coverage(store, day, policy)
            store.run_status(day, 'codex', state['status'], state['count'], state['detail'])
            if state['status'] != 'ok':
                return build_report(store, config, policy, day, Path(config['_root']) / 'reports')
    selected = sources
    if use_prefetch:
        selected = [name for name in ('codex', 'lark', 'git', 'browser') if name not in attempted]
    statuses = collect(config, store, day, selected)
    if use_prefetch and selected:
        attempted.update(selected)
        store.set(_prefetch_key(day), {'signature': signature, 'sources': sorted(attempted)})
    output = Path(config["_root"]) / "reports"
    if no_ai:
        return {"ok": all(x["status"] in ("ok", "disabled") for x in statuses), "collected_only": True, "date": day, "sources": statuses}
    result = build_report(store, config, policy, day, output)
    result["complete"] = all(x["status"] in ("ok", "disabled") for x in statuses)
    result["sources"] = statuses
    if result.get('ok') and use_prefetch:
        _clear_prefetch(store, day)
    return result


def doctor(config, probe_model=False):
    result = {"collection_enabled": config.get("collection_enabled", False), "timezone": config["timezone"],
              "schedule": config["schedule"], "python": sys.version.split()[0],
              "git": shutil.which(config["sources"]["git"].get("binary", "git")) is not None,
              "lark_cli": shutil.which(config["sources"]["lark"].get("binary", "lark-cli")) is not None,
              "browser_profiles": [{"name": p["name"], "kind": p["kind"]} for p in discover_browsers()] if config["sources"].get("browser", {}).get("enabled", True) else []}
    try:
        _, _, name = model_settings(config)
        result["model_configured"] = True
        result["model"] = name
    except Exception as error:
        result["model_configured"] = False
        result["model_error"] = type(error).__name__
    if probe_model:
        try:
            answer = model_call(config, "这是合成数据连通性测试，不读取任何用户数据。请只回复 OK。")
            result["model_probe"] = "ok" if "OK" in answer else "unexpected_response"
        except Exception as error:
            result["model_probe"] = str(error) if isinstance(error, RuntimeError) else type(error).__name__
    return result


def launchd_install(config):
    root = Path(config["_root"])
    script = root / "run.sh"
    logdir = root / "runtime"
    logdir.mkdir(exist_ok=True)
    target = Path.home() / "Library/LaunchAgents" / (LABEL + ".plist")
    target.parent.mkdir(parents=True, exist_ok=True)
    plist = {"Label": LABEL, "ProgramArguments": ["/bin/bash", str(script), "tick"],
             "WorkingDirectory": str(root), "StartInterval": 300, "RunAtLoad": True,
             "ProcessType": "Background", "StandardOutPath": str(logdir / "scheduler.log"),
             "StandardErrorPath": str(logdir / "scheduler-error.log"),
             "EnvironmentVariables": {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "TZ": config["timezone"]}}
    generated = root / "launchd.plist"
    generated.write_bytes(plistlib.dumps(plist))
    target.write_bytes(plistlib.dumps(plist))
    os.chmod(target, 0o600)
    domain = "gui/" + str(os.getuid())
    subprocess.run(["launchctl", "bootout", domain + "/" + LABEL], capture_output=True)
    r = subprocess.run(["launchctl", "bootstrap", domain, str(target)], capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError("launchctl bootstrap failed: " + str(r.returncode))
    return {"installed": True, "plist": str(target), "label": LABEL,
            "collection_enabled": config.get("collection_enabled", False)}


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description="独立每日复盘智能体")
    parser.add_argument("--config", default=str(ROOT / "config.json"))
    subs = parser.add_subparsers(dest="cmd", required=True)
    p = subs.add_parser("doctor"); p.add_argument("--probe-model", action="store_true")
    for name in ("run", "report"):
        p = subs.add_parser(name); p.add_argument("--date"); p.add_argument("--no-ai", action="store_true"); p.add_argument("--sources")
        p.add_argument("--notify", action="store_true", help="生成后请求桌面提醒和打开；已成功的动作不会重复")
    subs.add_parser("tick")
    subs.add_parser("status")
    subs.add_parser("background-check")
    p = subs.add_parser('comparison-input', help='只读输出API的生成材料；不含API报告或凭据')
    p.add_argument('--date', help='默认北京时间昨天')
    p.add_argument('--wait-seconds', type=int, default=0, help='等待同日材料就绪，最多1200秒')
    subs.add_parser('comparison-pending', help='只读列出未完成的对比日期及就绪状态')
    p = subs.add_parser('comparison-ack', help='聊天报告生成后记录输入版本回执')
    p.add_argument('--date', required=True)
    p.add_argument('--input-sha256', required=True)
    subs.add_parser("install-schedule")
    subs.add_parser("uninstall-schedule")
    subs.add_parser("archive-hook")
    p = subs.add_parser("archive-work"); p.add_argument("--force", action="store_true")
    p = subs.add_parser("research-work"); p.add_argument("--date", help="研究事实截至日期，默认北京时间昨天")
    subs.add_parser("research-status")
    subs.add_parser("install-research")
    subs.add_parser('install-blocking')
    subs.add_parser('blocking-check')
    p = subs.add_parser("research-feedback"); p.add_argument("id"); p.add_argument("status", choices=["ready", "done", "stopped"]); p.add_argument("--note", default="")
    subs.add_parser("archive-status")
    subs.add_parser("install-archive")
    p = subs.add_parser("archive-evidence"); p.add_argument("id")
    p = subs.add_parser("note"); p.add_argument("text"); p.add_argument("--date")
    p = subs.add_parser("action"); p.add_argument("id"); p.add_argument("status", choices=["proposed", "active", "done", "stopped"]); p.add_argument("--note", default="")
    subs.add_parser("actions")
    args = parser.parse_args()
    config = load_config(args.config)
    root = Path(config["_root"])
    runtime = root / "runtime"; runtime.mkdir(exist_ok=True)
    if args.cmd == "archive-hook":
        from .archive import enqueue_hook
        enqueue_hook(config, json.load(sys.stdin), effective_policy(config))
        print("{}")  # Stop requires JSON; never inject context or request a continuation.
        return 0
    if args.cmd == "doctor":
        result = doctor(config, args.probe_model)
    elif args.cmd == 'comparison-input':
        from .comparison import read_input
        if not 0 <= args.wait_seconds <= 1200:
            raise ValueError('wait-seconds must be between 0 and 1200')
        day = args.date or (datetime.now(ZoneInfo(config['timezone'])).date() - timedelta(days=1)).isoformat()
        datetime.strptime(day, '%Y-%m-%d')
        deadline = time.monotonic() + args.wait_seconds
        while True:
            current = load_config(args.config)
            result = read_input(current, effective_policy(current), day)
            if result['ok'] or result['reason'] != 'waiting_for_api_input' or time.monotonic() >= deadline:
                break
            time.sleep(min(5, max(0, deadline - time.monotonic())))
    elif args.cmd == 'comparison-pending':
        from .comparison import pending
        result = pending(config, effective_policy(config))
    elif args.cmd == 'comparison-ack':
        from .comparison import acknowledge
        datetime.strptime(args.date, '%Y-%m-%d')
        result = acknowledge(config, effective_policy(config), args.date, args.input_sha256)
    elif args.cmd == "install-schedule":
        result = launchd_install(config)
    elif args.cmd == "install-research":
        from .research_install import install
        result = install(config)
    elif args.cmd == 'install-blocking':
        from .blocking import install
        result = install(config)
    elif args.cmd == "install-archive":
        from .archive_install import install
        result = install(config)
    elif args.cmd == "uninstall-schedule":
        subprocess.run(["launchctl", "bootout", "gui/" + str(os.getuid()) + "/" + LABEL], capture_output=True)
        p = Path.home() / "Library/LaunchAgents" / (LABEL + ".plist")
        if p.exists(): p.unlink()
        result = {"uninstalled": True}
    else:
        lock_name = ('blocking.lock' if args.cmd == 'blocking-check' else
                     "archive.lock" if args.cmd == "archive-work" else "research.lock" if args.cmd == "research-work" else "run.lock")
        with (runtime / lock_name).open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                print(json.dumps({"skipped": "another run is active"})); return 0
            store = Store(runtime / "events.sqlite")
            try:
                now = datetime.now(ZoneInfo(config["timezone"]))
                day = getattr(args, "date", None) or now.date().isoformat()
                policy = effective_policy(config)
                if args.cmd == 'blocking-check':
                    from .blocking import check
                    result = check(config, store, policy)
                elif args.cmd == "research-work":
                    from .research import work
                    through = args.date or (now.date() - timedelta(days=1)).isoformat()
                    result = work(config, store, policy, through)
                    from .blocking import record_failure
                    failed = bool(result.get('error') or result.get('discovery_error'))
                    if failed or result.get('processed') or result.get('discovered_facts'):
                        record_failure(store, 'research', failed)
                elif args.cmd == "research-status":
                    from .research import list_items, _permitted
                    items = [x for x in list_items(store) if _permitted(store, config, policy, x)]
                    result = [{k:x.get(k) for k in ('id','kind','title','status','attempts','retry_at','published_day','feedback','artifact_path','last_error')} for x in items]
                elif args.cmd == "research-feedback":
                    from .research import feedback, list_items, _permitted
                    if not any(x['id'] == args.id and _permitted(store, config, policy, x) for x in list_items(store)):
                        raise ValueError('Research not found under current policy')
                    feedback(store, args.id, args.status, args.note)
                    result = {"updated": args.id, "status": args.status}
                elif args.cmd == "archive-work":
                    from .archive import work
                    result = work(config, store, policy, force=args.force)
                elif args.cmd == "archive-status":
                    from .archive import init, coverage
                    init(store)
                    result = {"today": coverage(store, day, policy), "last_scan": store.get("codex_archive_scan"),
                              "jobs": dict(store.db.execute("SELECT status,count(*) FROM codex_jobs GROUP BY status")),
                              "last_worker": store.get("codex_archive_worker")}
                elif args.cmd == "archive-evidence":
                    from .archive import evidence
                    result = evidence(store, args.id, policy)
                elif args.cmd == "status":
                    result = {"last_completed_day": store.get("last_completed_day"), "collection_enabled": config.get("collection_enabled", False),
                              "today": store.statuses(day), "events": store.db.execute("SELECT count(*) FROM events").fetchone()[0]}
                elif args.cmd == "background-check":
                    result = doctor(config, True)
                    from .collectors import codex_threads, browser_db, lark_call
                    try:
                        result["codex_metadata"] = len(codex_threads(config["sources"]["codex"].get("home", "~/.codex")))
                    except Exception as error:
                        result["codex_metadata"] = type(error).__name__
                    result["browser_access"] = {}
                    for profile in (discover_browsers() if config["sources"].get("browser", {}).get("enabled", True) else []):
                        try:
                            with browser_db(profile["path"]) as connection:
                                connection.execute("SELECT count(*) FROM sqlite_master").fetchone()
                            result["browser_access"][profile["name"]] = "ok"
                        except Exception as error:
                            result["browser_access"][profile["name"]] = type(error).__name__
                    try:
                        data = lark_call(config["sources"]["lark"].get("binary", "lark-cli"), ["im", "+chat-list", "--types", "p2p,group", "--page-size", "1"])
                        result["lark_access"] = "ok" if isinstance(data.get("chats"), list) else "unexpected_shape"
                    except Exception as error:
                        result["lark_access"] = type(error).__name__
                    write_private(root / "background-check.json", json.dumps(result, ensure_ascii=False, indent=2))
                elif args.cmd == "actions":
                    result = store.actions(policy)
                elif args.cmd == "action":
                    cursor = store.db.execute("UPDATE actions SET status=?,note=? WHERE id=? AND fingerprint=?",
                                              (args.status, args.note, args.id, policy.fingerprint))
                    if not cursor.rowcount: raise ValueError("Action not found under current policy")
                    store.db.commit(); result = {"updated": args.id, "status": args.status}
                elif args.cmd == "note":
                    at = now if day == now.date().isoformat() else datetime.fromisoformat(day + "T12:00:00").replace(tzinfo=ZoneInfo(config["timezone"]))
                    event = make_event("manual", now.isoformat(), at, "主人补充", args.text)
                    store.put(event); result = {"recorded": event["id"]}
                elif not config.get("collection_enabled", False):
                    result = {"skipped": "collection_disabled_pending_blacklist", "config": config["_path"]}
                elif args.cmd == "report":
                    result = build_report(store, config, policy, day, root / "reports")
                elif args.cmd == "run":
                    result = run_day(config, store, day, args.no_ai, args.sources.split(",") if args.sources else None)
                elif not schedule_reached(config, now):
                    result = {"skipped": "before_schedule"}
                else:
                    recovered_delivery = recover_scheduled_delivery(config, store, policy, now)
                    # Throttle retry after persistent authentication/network failures.
                    regular = due_days(config, now, store.get("last_completed_day"))
                    from .archive import dirty_days
                    dirty = dirty_days(store)
                    retry_dates = store.get("retry_dates", [])
                    disable_old_date_retries(store)
                    retry_dates = []
                    discard_finalized_retries(config, store, policy, set(retry_dates) | set(dirty))
                    retry_dates = store.get("retry_dates", [])
                    next_retry = store.get("next_retry", 0)
                    dirty = {day: revision for day, revision in dirty.items()
                             if not finalized_report(config, store, day, policy)}
                    if now.timestamp() < next_retry:
                        regular = [d for d in regular if d not in retry_dates]
                        dirty = {d: rev for d, rev in dirty.items() if d not in retry_dates}
                    if now.timestamp() < next_retry and not regular and not dirty:
                        result = {"skipped": "retry_backoff"}
                    else:
                        results = []
                        pending = list(dirty)
                        # A newly due report is the user-facing deadline. Old
                        # partial-source retries wait for the next tick so they
                        # cannot delay today's report or its desktop delivery.
                        targets = (regular if regular else pending_days([], pending, config["schedule"].get("catchup_days", 3)))
                        for target in targets:
                            r = run_day(config, store, target, prefetch=True); results.append(r)
                            from .blocking import record_failure
                            if not r.get('waiting_for_archive'):
                                record_failure(store, 'report:' + target, not r['ok'])
                            if not r["ok"]:
                                retry = set(store.get("retry_dates", [])); retry.add(target)
                                store.set("retry_dates", sorted(retry))
                                report_retry = set(store.get('report_retry_dates', [])); report_retry.add(target)
                                store.set('report_retry_dates', sorted(report_retry))
                                store.set("next_retry", now.timestamp() + (300 if r.get("waiting_for_archive") else 3600)); break
                            if target in regular:
                                store.set("last_completed_day", target)
                            retry = set(store.get("retry_dates", []))
                            if r.get("ok") and r.get("path"):
                                retry.discard(target)
                                report_retry = set(store.get('report_retry_dates', [])); report_retry.discard(target)
                                store.set('report_retry_dates', sorted(report_retry))
                                from .archive import acknowledge
                                revision = dirty.get(target)
                                if revision is not None:
                                    acknowledge(store, target, revision)
                            elif r.get("complete"):
                                retry.discard(target)
                            else:
                                retry.add(target)
                            store.set("retry_dates", sorted(retry))
                            if r.get("ok") and r.get("path"):
                                # Deliver each completed report before starting
                                # another potentially slow date.
                                from .desktop import deliver
                                current = load_config(args.config)
                                current_policy = effective_policy(current)
                                if current.get("collection_enabled", False) and current_policy.fingerprint == policy.fingerprint:
                                    r["desktop"] = deliver(store, r, policy, current.get("desktop", {}))
                        if results and store.get("retry_dates", []) and all(r["ok"] for r in results):
                            store.set("next_retry", now.timestamp() + 21600)
                        result = {"ok": all(r["ok"] for r in results), "runs": results,
                                  "complete": all(r.get("complete", True) for r in results)}
                    if recovered_delivery:
                        result["recovered_delivery"] = recovered_delivery
                automatic_delivery = args.cmd == "tick" and schedule_reached(config, datetime.now(ZoneInfo(config['timezone'])))
                if (automatic_delivery or getattr(args, 'notify', False)) and config.get("collection_enabled", False):
                    from .desktop import deliver, retry_pending
                    # Use the current blacklist if configuration changed during model generation.
                    current = load_config(args.config)
                    current_policy = effective_policy(current)
                    if current.get("collection_enabled", False) and current_policy.fingerprint == policy.fingerprint:
                        settings = current.get("desktop", {})
                        if args.cmd != "tick":
                            for item in result.get("runs", [result]):
                                if item.get("ok") and item.get("path"):
                                    item["desktop"] = deliver(store, item, policy, settings)
                        if args.cmd == "tick":
                            result["desktop_retries"] = retry_pending(store, policy, settings)
                if args.cmd in ('tick', 'archive-work', 'research-work'):
                    from .blocking import record_failure
                    record_failure(store, 'runtime:' + args.cmd, False)
            except (ValueError, RuntimeError, OSError, TypeError, KeyError):
                if args.cmd in ('tick', 'archive-work', 'research-work'):
                    from .blocking import record_failure
                    record_failure(store, 'runtime:' + args.cmd, True)
                raise
            finally:
                store.close()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if isinstance(result, dict) and result.get("ok") is False else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, RuntimeError, OSError) as error:
        print(json.dumps({"error": type(error).__name__, "detail": str(error)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(1)
