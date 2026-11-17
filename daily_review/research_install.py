"""Install research separately from the deadline-driven daily publisher."""
import os
import plistlib
import subprocess
from pathlib import Path

LABEL='local.daily-review-research'


def definition(config):
    root=Path(config['_root'])
    return {'Label':LABEL,'ProgramArguments':['/bin/bash',str(root/'run.sh'),'research-work'],
            'WorkingDirectory':str(root),'StartInterval':600,'RunAtLoad':True,'ProcessType':'Background',
            'StandardOutPath':str(root/'runtime/research.log'),
            'StandardErrorPath':str(root/'runtime/research-error.log'),
            'EnvironmentVariables':{'TZ':config['timezone'],'PATH':os.environ.get('PATH','/usr/bin:/bin')}}


def install(config):
    root=Path(config['_root']);target=Path.home()/'Library/LaunchAgents'/(LABEL+'.plist')
    target.parent.mkdir(parents=True,exist_ok=True)
    data=plistlib.dumps(definition(config));target.write_bytes(data);os.chmod(target,0o600)
    (root/'research-launchd.plist').write_bytes(data)
    domain='gui/'+str(os.getuid())
    subprocess.run(['launchctl','bootout',domain+'/'+LABEL],capture_output=True)
    result=subprocess.run(['launchctl','bootstrap',domain,str(target)],capture_output=True)
    if result.returncode: raise RuntimeError('Research launchctl bootstrap failed')
    return {'installed':True,'label':LABEL,'plist':str(target)}
