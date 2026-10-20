"""Equivalent warm local update fixtures, never a live Slurm benchmark."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import resource

ROOT = Path('/workspace/Raven')
sys.path.insert(0, str(ROOT))
BASE = Path('/workspace/review-install-perf')

def git(root, *args):
    return subprocess.check_output(['git', '-c', 'user.name=Review', '-c', 'user.email=review@example.invalid', '-C', str(root), *args], stderr=subprocess.DEVNULL).decode().strip()

def prepare():
    BASE.mkdir(mode=0o700, exist_ok=True)
    seed = BASE / 'seed'; seed.mkdir()
    git(seed, 'init', '-b', 'main')
    for index in range(2672):
        path = seed / ('layer%02d' % (index % 32)) / ('branch%02d' % (index % 8)) / ('input%04d.py' % index)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('VALUE = ' + str(index) + '\n')
    git(seed, 'add', '.'); git(seed, 'commit', '-m', 'baseline')
    for mode in ('baseline', 'current'):
        git(BASE, 'clone', str(seed), str(BASE / mode))
    (seed / 'layer00/branch00/input0000.py').write_text('VALUE = 999\n')
    git(seed, 'add', '.'); git(seed, 'commit', '-m', 'upstream')
    for name in ('update_source', 'config_trust'):
        (BASE / ('baseline_' + name + '.py')).write_bytes(subprocess.check_output(['git', 'show', 'b98742f:raven/' + name + '.py'], cwd=ROOT))

def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module

if sys.argv[1] == 'prepare':
    prepare()
else:
    mode = sys.argv[1]
    flavor = mode.split('-')[0]
    if flavor == 'current':
        from raven import update_source
        module = update_source
    else:
        import raven
        trust = load('raven._baseline_config_trust', BASE / 'baseline_config_trust.py')
        raven.config_trust = trust
        sys.modules['raven.config_trust'] = trust
        module = load('raven._baseline_update_source', BASE / 'baseline_update_source.py')
    source = BASE / mode
    if not source.exists():
        git(BASE, 'clone', str(BASE / 'seed'), str(source))
        git(source, 'reset', '--hard', 'HEAD~1')
    # Equal warm-byte-cache setup; collect cold cache claims nowhere.
    for path in source.rglob('*.py'):
        path.read_bytes()
    start = time.perf_counter()
    self_before = resource.getrusage(resource.RUSAGE_SELF)
    child_before = resource.getrusage(resource.RUSAGE_CHILDREN)
    result = module.update_source(source, backup_root=BASE / (mode + '-backups'))
    elapsed = time.perf_counter() - start
    self_after = resource.getrusage(resource.RUSAGE_SELF)
    child_after = resource.getrusage(resource.RUSAGE_CHILDREN)
    def delta(field):
        return (getattr(self_after, field) - getattr(self_before, field)
                + getattr(child_after, field) - getattr(child_before, field))
    print(json.dumps({'mode':mode, 'seconds':elapsed, 'files':2672,
                      'process_max_rss_kib':self_after.ru_maxrss, 'child_max_rss_kib':child_after.ru_maxrss,
                      'cpu_user_seconds':delta('ru_utime'), 'cpu_system_seconds':delta('ru_stime'),
                      'input_blocks_512_bytes':delta('ru_inblock'), 'output_blocks_512_bytes':delta('ru_oublock'),
                      'changed':result['changed'], 'head':git(source, 'rev-parse', 'HEAD')}))
