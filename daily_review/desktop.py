"""Persistent macOS report delivery; callers hold the CLI run lock."""
import hashlib
import re
import subprocess
import time
from pathlib import Path

NOTIFICATION_SCRIPT = '''on run argv
    display notification ("主人，" & item 1 of argv & " 的工作复盘报告已生成，请查看。") with title "每日复盘已完成"
end run'''
PREFIX = 'desktop_delivery:'


def _submit(store, key, state, settings, runner, now):
    statuses = {}
    attempted = False
    commands = {
        'notification': ['/usr/bin/osascript', '-e', NOTIFICATION_SCRIPT, Path(state['path']).stem],
        'typora': ['/usr/bin/open', '-a', 'Typora', state['path']],
    }
    for channel, setting in (('notification', 'notify'), ('typora', 'open_typora')):
        if not settings.get(setting, True):
            statuses[channel] = 'disabled'
            continue
        if state.get(channel) == 'submitted':
            statuses[channel] = 'already_submitted'
            continue
        if now < state.get('retry_at', 0):
            statuses[channel] = 'retry_pending'
            continue
        attempted = True
        try:
            result = runner(commands[channel], capture_output=True, text=True, timeout=15)
            if result.returncode:
                raise OSError('Command exit ' + str(result.returncode))
            state[channel] = 'submitted'
            state.pop(channel + '_error', None)
        except (OSError, subprocess.SubprocessError) as error:
            state[channel] = 'failed'
            state[channel + '_error'] = type(error).__name__
        statuses[channel] = state[channel]
        # Persist each success independently: a failure in the other channel cannot repeat it.
        store.set(key, state)
    if attempted:
        state['retry_at'] = now + 300
    store.set(key, state)
    return statuses


def deliver(store, result, policy, settings=None, runner=None, now=None):
    if not result.get('ok') or result.get('collected_only') or result.get('waiting_for_archive'):
        return {'skipped': 'report_not_ready'}
    path = Path(result.get('path', '')).resolve()
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}\.md', path.name):
        return {'skipped': 'not_a_daily_report'}
    try:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as error:
        return {'error': type(error).__name__}
    key = PREFIX + hashlib.sha256(str(path).encode()).hexdigest()
    state = store.get(key, {})
    # Delivery belongs to the daily path, not its wording or current policy.
    # Keep successful channels, including states saved by older installations.
    state.update(path=str(path), digest=digest, fingerprint=policy.fingerprint)
    state.pop('cancelled', None)
    return _submit(store, key, state, settings or {}, runner or subprocess.run, time.time() if now is None else now)


def retry_pending(store, policy, settings=None, runner=None, now=None):
    results = []
    for (key,) in store.db.execute('SELECT key FROM state WHERE key LIKE ?', (PREFIX + '%',)).fetchall():
        state = store.get(key)
        if state.get('cancelled') or all(state.get(c) == 'submitted' for c in ('notification', 'typora')):
            continue
        try:
            valid = state['fingerprint'] == policy.fingerprint and hashlib.sha256(Path(state['path']).read_bytes()).hexdigest() == state['digest']
        except OSError:
            valid = False
        if not valid:
            state['cancelled'] = True; store.set(key, state)
            continue
        results.append(_submit(store, key, state, settings or {}, runner or subprocess.run, time.time() if now is None else now))
    return results
