"""Low-frequency alerts for persistent blockers, checked by an independent worker."""
import json
import os
import plistlib
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

SCRIPT = '''on run argv
    display notification (item 1 of argv) with title "每日复盘运行受阻"
end run'''
COOLDOWN = 6 * 3600


def record_failure(store, stage, failed, now=None):
    now = time.time() if now is None else now
    key = 'blocking_phase:' + stage
    previous = store.get(key, {})
    if failed:
        store.set(key, {'first_at': previous.get('first_at', now),
                        'attempts': previous.get('attempts', 0) + 1, 'last_at': now})
    elif previous:
        store.db.execute('DELETE FROM state WHERE key=?', (key,)); store.db.commit()


def record_research_result(store, result, now=None):
    # A failure in one opportunity must not count as a retry of another.
    record_failure(store, 'research', False, now=now)
    if result.get('id') and (result.get('error') or result.get('processed')):
        record_failure(store, 'research:'+result['id'], bool(result.get('error')), now=now)
    if result.get('discovery_error') or 'discovered_facts' in result:
        record_failure(store, 'research:discovery', bool(result.get('discovery_error')), now=now)


def check(config, store, policy, runner=None, now=None):
    now = time.time() if now is None else now
    if not config.get('collection_enabled') or not config.get('desktop', {}).get('notify', True):
        return {'skipped': 'disabled'}
    yesterday = (datetime.fromtimestamp(now, ZoneInfo(config['timezone'])).date() - timedelta(days=1)).isoformat()
    blockers = set()
    from .archive import selected_jobs, coverage
    archive_enabled = (config.get('sources', {}).get('codex', {}).get('mode') == 'archive'
                       and config['sources']['codex'].get('enabled', True)
                       and policy.allows({'source': 'codex'}))
    days = set(store.get('report_retry_dates', []))
    if archive_enabled:
        from .archive import init
        init(store)
        for (day,) in store.db.execute("SELECT DISTINCT day FROM codex_jobs WHERE status!='done' AND day<=?", (yesterday,)).fetchall():
            for key, _, _, status, _ in selected_jobs(store, day, policy):
                row = store.db.execute('SELECT attempts,created_at,started_at,error FROM codex_jobs WHERE id=?', (key,)).fetchone()
                if status in ('failed', 'running') and row[3] and row[0] >= 3 and now - row[1] >= 900:
                    blockers.add('Codex 归档反复校验或请求失败')
                if status == 'running' and row[2] and now - row[2] >= 1800:
                    blockers.add('Codex 归档执行长时间未结束')
        for day in days:
            state = coverage(store, day, policy)
            progress_key = 'blocking_progress:' + day
            progress = {'done': state['count'], 'pending': state['pending_jobs'],
                        'cursor': state['cursor_pending'], 'errors': state['relevant_scan_errors']}
            previous = store.get(progress_key, {})
            if state['status'] == 'ok':
                store.set(progress_key, {})
            elif progress != previous.get('value'):
                store.set(progress_key, {'value': progress, 'at': now})
            elif now - previous.get('at', now) >= 1800:
                blockers.add('日报等待归档超过 30 分钟且无进展')
            scan = store.get('codex_archive_scan', {})
            if state['status'] != 'ok' and now - scan.get('at', now) >= 1800:
                blockers.add('归档后台超过 30 分钟未更新')
    for key, raw in store.db.execute("SELECT key,value FROM state WHERE key LIKE 'blocking_phase:%'").fetchall():
        phase = json.loads(raw)
        if phase.get('attempts', 0)<3 or now-phase['first_at']<1800: continue
        reason='后台任务连续执行失败' if key.startswith('blocking_phase:runtime:') else '日报分析连续重试失败'
        if key=='blocking_phase:research': reason='研究任务连续重试失败'
        if key=='blocking_phase:research:discovery': reason='研究机会发现连续重试失败'
        if key.startswith('blocking_phase:research:') and not key.endswith(':discovery'):
            from .research import list_items, _permitted
            ident=key.split(':',2)[2]
            item=next((x for x in list_items(store) if x['id']==ident),None)
            if not item or item['status'] not in ('pending','researching') or not _permitted(store,config,policy,item):
                continue
            reason='研究「'+item['title']+'」连续重试失败：'+item.get('last_error','未知错误')[:160]
        if key.startswith('blocking_phase:report:') and store.db.execute(
                'SELECT 1 FROM reports WHERE day=?', (key.rsplit(':', 1)[1],)).fetchone():
            continue
        blockers.add(reason)
    from .comparison import source_signature
    signature = source_signature(config, policy)
    for path in (Path(config['_root'])/'runtime/comparison-status').glob('????-??-??.json'):
        status = json.loads(path.read_text())
        if status.get('source_signature') != signature: continue
        if status.get('stage') != 'preparing_input': continue
        if store.db.execute('SELECT 1 FROM reports WHERE day=?', (status['date'],)).fetchone(): continue
        audit = Path(config['_root'])/'reports'/(status['date']+'.input-audit.json')
        last_progress = max(status.get('at', now), audit.stat().st_mtime if audit.exists() else 0)
        if now - last_progress >= 1800:
            blockers.add('日报生成超过 30 分钟无进展')
    state = store.get('blocking_notification', {})
    if not blockers:
        state.update(active=False, submitted=False, reasons=[])
        store.set('blocking_notification', state)
        return {'blocked': False}
    if state.get('active') and state.get('submitted'):
        return {'blocked': True, 'notification': 'already_submitted', 'reasons': sorted(blockers)}
    state.update(active=True, reasons=sorted(blockers))
    if now < state.get('last_attempt_at', -COOLDOWN) + COOLDOWN:
        store.set('blocking_notification', state)
        return {'blocked': True, 'notification': 'cooldown', 'reasons': sorted(blockers)}
    message = ('主人，复盘任务持续受阻：' + '；'.join(sorted(blockers)) +
               '。本轮自动重试尚未恢复，请检查程序运行状态。同一故障不重复提醒。')
    # Persist before invoking the OS: a restart cannot cause rapid resubmission.
    state['last_attempt_at'] = now
    store.set('blocking_notification', state)
    try:
        result = (runner or subprocess.run)(['/usr/bin/osascript', '-e', SCRIPT, message],
                                            capture_output=True, text=True, timeout=15)
        state['submitted'] = result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        state['submitted'] = False
    store.set('blocking_notification', state)
    return {'blocked': True, 'notification': 'submitted' if state['submitted'] else 'failed',
            'reasons': sorted(blockers)}


def install(config):
    label = 'local.daily-review-health'
    root = Path(config['_root'])
    value = {'Label':label, 'ProgramArguments':['/bin/bash',str(root/'run.sh'),'blocking-check'],
        'WorkingDirectory':str(root), 'StartInterval':300, 'RunAtLoad':True,
        'ProcessType':'Background', 'StandardOutPath':str(root/'runtime/health.log'),
        'StandardErrorPath':str(root/'runtime/health-error.log'),
        'EnvironmentVariables':{'PATH':os.environ.get('PATH','/usr/bin:/bin'), 'TZ':config['timezone']}}
    path = Path.home()/'Library/LaunchAgents'/(label+'.plist')
    path.write_bytes(plistlib.dumps(value)); os.chmod(path,0o600)
    domain = 'gui/'+str(os.getuid())
    subprocess.run(['launchctl','bootout',domain+'/'+label],capture_output=True)
    result = subprocess.run(['launchctl','bootstrap',domain,str(path)],capture_output=True)
    if result.returncode: raise RuntimeError('Health monitor bootstrap failed: '+str(result.returncode))
    return {'ok':True,'label':label,'interval_seconds':300}
