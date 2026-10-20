from pathlib import Path
import hashlib,json,os,subprocess,tempfile,venv
wheel=Path('/workspace/review-final-dist-f63d82e/raven-7.0.0-py3-none-any.whl')
report={'schema':'raven.installed-review-smoke/v1','code_revision':'f63d82ecd40c8b3ad4bdb42c5f92a492d9b0cdc0','wheel_sha256':hashlib.sha256(wheel.read_bytes()).hexdigest(),'cwd':'Temporary directory outside the source checkout','checks':[]}
with tempfile.TemporaryDirectory(prefix='raven-final-installed-') as root:
    root=Path(root); envdir=root/'venv'; venv.EnvBuilder(with_pip=True).create(envdir)
    subprocess.run([str(envdir/'bin/python'),'-m','pip','install','--no-index','--no-deps',str(wheel)],check=True)
    env=os.environ.copy(); env.pop('PYTHONPATH',None); env.update(HOME=str(root/'home'),XDG_CONFIG_HOME=str(root/'config'),XDG_STATE_HOME=str(root/'state'),XDG_CACHE_HOME=str(root/'cache'),TERM='xterm-256color')
    for key in tuple(env):
        if key.startswith('RAVEN_'): env.pop(key)
    commands=[[x,'--version'] for x in ('raven','ravenl','ravenh')]
    commands += [[x,action,'--help'] for x in ('raven','ravenl','ravenh') for action in ('update','revert','trust')]
    commands += [['raven','trace','cleanup-runtime','--help'],['raven','trace','capabilities','--json'],['ravenl','--site','desktop','doctor'],['ravenh','--site','desktop','--plan'],['raven','--fake','--no-state','--no-plugins','--record',str(root/'commands.jsonl'),'--json']]
    for command in commands:
        p=subprocess.run([str(envdir/'bin'/command[0]),*command[1:]],cwd=root,env=env,capture_output=True,text=True,timeout=60)
        row={'command':command,'exit_code':p.returncode,'status':'passed' if p.returncode==0 else 'failed'}
        if p.returncode: row['stderr']=p.stderr[-4000:]
        if command[-1]=='--json' and p.returncode==0:
            value=json.loads(p.stdout); row['valid_json']=True
            if '--record' in command:
                assert value.get('jobs'); assert (root/'commands.jsonl').stat().st_size>0
                row['recording_created']=True
        report['checks'].append(row)
    report['status']='qualified-local' if all(x['status']=='passed' for x in report['checks']) else 'failed'
Path('/workspace/review-final-installed-smoke-f63d82e.json').write_text(json.dumps(report,indent=2)+'\n')
assert report['status']=='qualified-local', report
print(len(report['checks']),'installed commands passed')
