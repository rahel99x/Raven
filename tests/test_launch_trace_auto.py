"""Real portable local launches: startup, descendants, scope, budgets and gaps."""
import json
import os
import shlex
import sys
from pathlib import Path

import pytest

from raven.launch import resolve_plan
from raven.launch_runtime import execute_plan, materialize, supervise
from raven.trace_protocol import TraceReader


def launch(tmp_path, source, *options, env=None, settings=None):
    script = tmp_path / 'job.sh'
    script.write_text('#!/bin/sh\nset -eu\n' + source + '\n')
    args = [str(script), '--project', str(tmp_path), '--local', *options]
    if '--trace' in options and sys.version_info < (3, 12) and '--trace-adapter' not in options:
        args += ['--trace-adapter', 'python-legacy-profile']
    environment = dict(os.environ)
    for key in list(environment):
        if key.startswith('RAVEN_'):
            environment.pop(key)
    environment.update(env or {})
    # An application venv without Raven installed must use the bundled client.
    environment.pop('PYTHONPATH', None)
    environment.update(env or {})
    plan = resolve_plan(args, cwd=tmp_path, environ=environment)
    plan['settings'].update(settings or {})
    result = execute_plan(plan, environ=environment)
    return Path(result['run_dir']), result


def entries(run):
    return json.loads((run / 'trace/index.json').read_text())['sources']


def recordings(run):
    found = []
    for entry in entries(run):
        if entry.get('schema') != 'raven.trace/v1':
            continue
        reader = TraceReader(run / entry['path'])
        snapshot = reader.refresh()
        while not snapshot['caught_up']:
            snapshot = reader.refresh()
        assert snapshot['status'] != 'INVALID', snapshot['warnings']
        found.append((entry, snapshot))
    return found


def python(command):
    return shlex.join([sys.executable, *command])


@pytest.mark.parametrize('form', ['script', 'module', 'command', 'shebang', 'function'])
def test_unedited_python_entrypoints_attach_and_preserve_exit(tmp_path, form):
    (tmp_path / 'application.py').write_text('def work():\n    return 17\nassert work() == 17\n')
    if form == 'module':
        command = python(['-m', 'application'])
    elif form == 'command':
        command = python(['-c', 'def work(): return 17\nassert work()==17'])
    elif form == 'shebang':
        path = tmp_path / 'entry'
        path.write_text('#!' + sys.executable + '\n' + (tmp_path / 'application.py').read_text())
        path.chmod(0o700)
        command = './entry'
    elif form == 'function':
        command = 'run_work() { ' + python(['application.py']) + '; }; run_work'
    else:
        command = python(['application.py'])
    run, result = launch(tmp_path, command, '--trace')
    assert result['exit_code'] == 0
    items = recordings(run)
    assert len(items) == 1, entries(run)
    entry, snapshot = items[0]
    assert entry['status'] == 'complete', entry
    assert snapshot['identity']['attempt'] == run.name
    assert snapshot['identity']['parent_pid'] > 0
    assert any(event.get('data', {}).get('function') == 'work' for event in snapshot['events']), snapshot


def test_no_trace_exposes_metrics_client_without_startup_hook(tmp_path):
    command = python(['-c', "import os,raven; raven.metric('loss',.5); assert not os.environ.get('RAVEN_TRACE_SITE')"])
    run, result = launch(tmp_path, command, '--no-trace')
    assert result['exit_code'] == 0
    assert not (run / 'trace').exists()
    rows = [json.loads(line) for line in (run / 'metrics.jsonl').read_text().splitlines()]
    assert rows[-1]['metrics']['loss'] == .5


def test_chains_existing_sitecustomize_and_retains_path_order(tmp_path):
    site = tmp_path / 'site'; site.mkdir()
    (site / 'sitecustomize.py').write_text("import os; os.environ['SITE_MARKER']='yes'\n")
    run, result = launch(tmp_path, python(['-c', "import os; assert os.environ['SITE_MARKER']=='yes'"]), '--trace', env={'PYTHONPATH': str(site)})
    assert result['exit_code'] == 0
    entry, _snapshot = recordings(run)[0]
    manifest = json.loads((run / entry['path']).read_text())
    assert manifest['coverage']['chained'] is True
    assert json.loads((run / 'run.json').read_text())['metadata']['automation']['python_startup']['original_pythonpath'] == str(site)


@pytest.mark.parametrize('method', ['fork', 'spawn', 'forkserver'])
def test_multiprocessing_children_have_independent_complete_journals(tmp_path, method):
    (tmp_path / 'application.py').write_text('''import multiprocessing as mp
import os
def work():
    assert os.getpid() > 0
if __name__ == '__main__':
    p = mp.get_context(''' + repr(method) + ''').Process(target=work)
    p.start(); p.join(15)
    assert p.exitcode == 0
''')
    run, result = launch(tmp_path, python(['application.py']), '--trace')
    assert result['exit_code'] == 0
    items = recordings(run)
    assert len(items) >= 2, entries(run)
    workers = [(e,s) for e,s in items if any(x.get('data',{}).get('function') == 'work' for x in s['events'])]
    assert len(workers) == 1, entries(run)
    assert workers[0][0]['status'] == 'complete', workers[0][0]
    assert workers[0][1]['identity']['role'] == 'helper'
    assert len({s['identity']['pid'] for _,s in items}) == len(items)


def test_subprocess_pool_threads_async_and_observed_bypass_diagnostics(tmp_path):
    (tmp_path / 'application.py').write_text('''import asyncio, concurrent.futures, subprocess, sys, threading, os
def work(): return 42
async def awork(): return work()
if __name__ == '__main__':
    with concurrent.futures.ProcessPoolExecutor(2) as pool:
        assert list(pool.map(work_arg, [1,2])) if False else pool.submit(work).result() == 42
    t=threading.Thread(target=work); t.start(); t.join()
    assert asyncio.run(awork()) == 42
    subprocess.run([sys.executable,'-c','def child():return 1\\nchild()'],check=True)
    for option in ['-I','-S','-E']:
        subprocess.run([sys.executable,option,'-c','pass'],check=True)
    subprocess.run([sys.executable,'-c','pass'],env=dict(os.environ,PYTHONPATH=''),check=True)
''')
    run, result = launch(tmp_path, python(['application.py']), '--trace')
    assert result['exit_code'] == 0
    items = recordings(run)
    assert len(items) >= 3, entries(run)
    assert all(e['status'] == 'complete' for e,_ in items)
    assert any(s['identity']['role']=='helper' for _,s in items)
    reasons = ' '.join(e.get('reason','') for e in entries(run))
    assert 'disables environment/site' in reasons and 'replaces PYTHONPATH' in reasons


@pytest.mark.parametrize('env,expected', [({'RANK':'2','LOCAL_RANK':'1','WORLD_SIZE':'4'},'torchrun'), ({'SLURM_PROCID':'2','SLURM_LOCALID':'1','SLURM_NTASKS':'4'},'srun')])
def test_rank_environment_receipts_are_explicit_fixtures(tmp_path, env, expected):
    run, result = launch(tmp_path, python(['-c', 'def work():return 1\nwork()']), '--trace', env=env)
    assert result['exit_code'] == 0
    identity = recordings(run)[0][1]['identity']
    assert (identity['rank'],identity['local_rank'],identity['world_size'],identity['launcher']) == (2,1,4,expected)


def test_explicit_hook_records_once_and_explicit_only_mode(tmp_path):
    (tmp_path / 'application.py').write_text('def work():return 1\nwork()\n')
    command = '. "$RAVEN_SHELL_HELPER"\nraven_exec ' + python(['application.py'])
    run, result = launch(tmp_path, command, '--trace', env={'RAVEN_TRACE_AUTO':'0'})
    assert result['exit_code'] == 0
    assert len(recordings(run)) == 1


def test_default_explicit_hook_suppresses_automatic_duplicate(tmp_path):
    (tmp_path / 'application.py').write_text('def work():return 1\nwork()\n')
    run, result = launch(tmp_path, '. "$RAVEN_SHELL_HELPER"\nraven_exec ' + python(['application.py']), '--trace')
    assert result['exit_code'] == 0
    assert len(recordings(run)) == 1


def test_process_cap_and_overwritten_shell_path_report_truthful_gap(tmp_path):
    command = python(['-c','pass'])
    run, result = launch(tmp_path, command+'\n'+command, '--trace', settings={'trace_processes':1})
    assert result['exit_code']==0 and len(recordings(run))==1
    assert any('process budget exhausted' in e.get('reason','') for e in entries(run))
    other=tmp_path/'other'; other.mkdir()
    run, result=launch(other, 'unset PYTHONPATH\n'+command, '--trace')
    assert result['exit_code']==0
    assert not recordings(run)
    gap=entries(run)[0]
    assert gap['status']=='unverified' and 'pid' not in gap['process']
    assert 'module command has been inferred' in gap['reason']


def test_tampered_startup_digest_fails_open_without_hook(tmp_path):
    script=tmp_path/'job.sh'; script.write_text('#!/bin/sh\n'+python(['-c','pass'])+'\n')
    plan=materialize(resolve_plan([str(script),'--project',str(tmp_path),'--local','--trace'],cwd=tmp_path,environ={}))
    plan['sitecustomize_sha256']='0'*64
    code,run=supervise(plan)
    assert code==0 and not (run/'.raven-runtime/site/sitecustomize.py').exists()
    warnings=json.loads((run/'run.json').read_text())['metadata']['automation']['warnings']
    assert any('digest' in w for w in warnings)


@pytest.mark.parametrize('explicit', [False, True])
def test_vendored_client_does_not_shadow_owned_trace_runtime(tmp_path, explicit):
    from raven.research_reporting import vendor_client
    vendor_client(tmp_path)
    (tmp_path/'application.py').write_text("import raven\nassert raven.__file__.endswith('raven.py')\n@raven.measure('loss')\ndef work():return .2\nwork()\n")
    command=python(['application.py'])
    if explicit:
        command='. "$RAVEN_SHELL_HELPER"\nraven_exec '+command
    run,result=launch(tmp_path,command,'--trace',env={'PYTHONPATH':str(tmp_path)})
    assert result['exit_code']==0
    assert len(recordings(run))==1
    assert json.loads((run/'metrics.jsonl').read_text().splitlines()[-1])['metrics']['loss']==.2


@pytest.mark.parametrize('explicit', [False, True])
def test_recording_caps_preserve_program_and_partial_identity(tmp_path, explicit):
    (tmp_path/'application.py').write_text('def work():return 1\nfor _ in range(3000): work()\n')
    command=python(['application.py'])
    if explicit:
        command='. "$RAVEN_SHELL_HELPER"\nraven_exec '+command
    run,result=launch(tmp_path,command,'--trace','--trace-run-bytes','4096')
    assert result['exit_code']==0
    entry,snapshot=recordings(run)[0]
    assert entry['status']=='partial'
    assert snapshot['identity']['parent_pid']>0
    manifest=json.loads((run/entry['path']).read_text())
    assert manifest['state']=='LIMIT'
    assert sum(p.stat().st_size for p in run.rglob('events.jsonl'))<=4096


def test_observed_native_executable_gap_has_no_invented_child_pid(tmp_path):
    run,result=launch(tmp_path,python(['-c',"import subprocess;subprocess.run(['/bin/true'],check=True)"]),'--trace')
    assert result['exit_code']==0
    native=[e for e in entries(run) if e.get('process',{}).get('launcher')=='true']
    assert len(native)==1 and native[0]['status']=='unverified'
    assert 'pid' not in native[0]['process'] and 'parent_pid' in native[0]['process']


@pytest.mark.parametrize('environment', [{'PYTHONNOUSERSITE':'1'}, {'PYTHONSAFEPATH':'1'}])
def test_user_site_and_safe_path_flags_preserve_automatic_attachment(tmp_path, environment):
    run,result=launch(tmp_path,python(['-c','def work():return 1\nwork()']),'--trace',env=environment)
    assert result['exit_code']==0 and len(recordings(run))==1


def test_unsupported_adapter_is_recorded_without_replacing_application_outcome(tmp_path):
    run,result=launch(tmp_path,python(['-c','raise SystemExit(7)']),'--trace','--trace-adapter','native-cooperative')
    assert result['exit_code']==7 and not recordings(run)
    assert any('native/cooperative integration' in e.get('reason','') for e in entries(run))
