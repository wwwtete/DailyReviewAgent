"""Install a short command hook and an independent launchd archive worker."""
import json
import os
import plistlib
import shlex
import subprocess
from pathlib import Path

from .report import write_private

LABEL = 'local.daily-review-archive'
MARKER = 'DailyReviewAgent fact archive'


def configure_hooks(config):
    root = Path(config['_root'])
    home = Path(config['sources']['codex'].get('home', '~/.codex')).expanduser()
    home.mkdir(parents=True, exist_ok=True)
    path = home / 'hooks.json'
    existing = path.read_text() if path.exists() else '{"hooks":{}}'
    data = json.loads(existing)
    backup = root / 'runtime/hooks.before-archive.json'
    backup.parent.mkdir(parents=True, exist_ok=True)
    if not backup.exists(): write_private(backup, existing)
    command = '/bin/bash ' + shlex.quote(str(root / 'run.sh')) + ' archive-hook'
    for event in ('Stop', 'SessionEnd', 'Interrupt'):
        groups = data.setdefault('hooks', {}).setdefault(event, [])
        # Own only our exact marked handler; leave all user/plugin hooks intact.
        groups[:] = [g for g in groups if not (len(g.get('hooks', [])) == 1 and g['hooks'][0].get('statusMessage') == MARKER)]
        groups.append({'hooks': [{'type': 'command', 'command': command, 'timeout': 3, 'statusMessage': MARKER}]})
    write_private(path, json.dumps(data, ensure_ascii=False, indent=2) + '\n')
    return str(path)


def install(config):
    root = Path(config['_root'])
    hooks = configure_hooks(config)
    queue = root / 'runtime/codex-hook-queue'; queue.mkdir(exist_ok=True)
    plist = {'Label': LABEL, 'ProgramArguments': ['/bin/bash', str(root/'run.sh'), 'archive-work'],
             'WorkingDirectory': str(root), 'StartInterval': 60, 'RunAtLoad': True,
             'WatchPaths': [str(queue)], 'ThrottleInterval': 30, 'ProcessType': 'Background',
             'StandardOutPath': str(root/'runtime/archive.log'),
             'StandardErrorPath': str(root/'runtime/archive-error.log'),
             'EnvironmentVariables': {'TZ': config['timezone']}}
    target = Path.home()/'Library/LaunchAgents'/(LABEL+'.plist')
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(plistlib.dumps(plist)); os.chmod(target, 0o600)
    (root/'archive-launchd.plist').write_bytes(plistlib.dumps(plist))
    domain = 'gui/' + str(os.getuid())
    subprocess.run(['launchctl', 'bootout', domain+'/'+LABEL], capture_output=True)
    result = subprocess.run(['launchctl', 'bootstrap', domain, str(target)], capture_output=True)
    if result.returncode: raise RuntimeError('Archive launchctl bootstrap failed')
    return {'installed': True, 'label': LABEL, 'hooks': hooks,
            'hook_trust': 'Review exact new hook definitions in Codex Settings > Hooks; fallback scan already runs'}
