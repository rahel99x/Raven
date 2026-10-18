"""Real lock contention and identity-preserving diagnostics for automatic traces."""
from __future__ import annotations

import fcntl
import json
import os
import struct
import sys
import time
from pathlib import Path

import pytest

from raven.launch_autotrace import _RunBudget, record_gap
from raven.trace_recorder import Recorder


def budget_file(tmp_path):
    folder = tmp_path / '.raven-runtime'
    folder.mkdir()
    path = folder / 'trace-budget'
    path.write_bytes(struct.pack('!QQ',0,0))
    path.chmod(0o600)
    return path


def lock(path):
    descriptor = os.open(path,os.O_RDWR)
    fcntl.flock(descriptor,fcntl.LOCK_EX | fcntl.LOCK_NB)
    return descriptor


def test_busy_budget_constructor_has_finite_wait_and_closes_descriptor(tmp_path):
    path = budget_file(tmp_path)
    holder = lock(path)
    budget = _RunBudget.__new__(_RunBudget)
    started = time.monotonic()
    try:
        with pytest.raises(TimeoutError,match='application continues'):
            budget.__init__(tmp_path,{})
        assert budget.fd is None
        assert .15 <= time.monotonic()-started < 2
        assert struct.unpack('!QQ',path.read_bytes()) == (0,0)
    finally:
        os.close(holder)


def test_busy_byte_reservation_is_bounded_and_never_mutates_shared_counter(tmp_path):
    path = budget_file(tmp_path)
    budget = _RunBudget(tmp_path,{})
    holder = lock(path)
    started = time.monotonic()
    try:
        with pytest.raises(TimeoutError,match='budget is busy'):
            budget.reserve(1024)
        assert .15 <= time.monotonic()-started < 2
        assert struct.unpack('!QQ',path.read_bytes()) == (0,1)
    finally:
        os.close(holder)
    try:
        assert budget.reserve(1024)
        assert struct.unpack('!QQ',path.read_bytes()) == (1024,1)
    finally:
        budget.close()


def test_budget_contention_stops_optional_recorder_without_application_exception(tmp_path):
    path = budget_file(tmp_path)
    budget = _RunBudget(tmp_path,{})
    recorder = Recorder(tmp_path,job_id='local',attempt='one',journal_format='blocks',
                        background_flush=False,reserve_bytes=budget.reserve)
    holder = lock(path)
    try:
        recorder.emit('science',{'name':'solve'})
        recorder.flush()
        assert recorder.state == 'ERROR'
        assert any('TimeoutError' in item for item in recorder.warnings)
        assert recorder.committed_seq == 0
    finally:
        os.close(holder)
        recorder.close()
        budget.close()


def test_gap_receipts_preserve_distinct_pids_ranks_and_observed_parent_identity(tmp_path):
    reason = 'Automatic trace process budget exhausted'
    for process in ({'pid':100,'rank':0,'launcher':'python'},
                    {'pid':101,'rank':1,'launcher':'python'},
                    {'pid':100,'rank':0,'launcher':'python'},
                    {'parent_pid':100,'launcher':'python','role':'child'},
                    {'parent_pid':101,'launcher':'python','role':'child'}):
        record_gap(tmp_path,reason,process=process)
    entries = json.loads((tmp_path / 'trace/index.json').read_text())['sources']
    assert len(entries) == 4
    assert entries[0]['count'] == 2
    assert {(row['process'].get('pid'),row['process'].get('rank')) for row in entries[:2]} == {(100,0),(101,1)}
    assert all('pid' not in row['process'] for row in entries[2:])


def test_explicit_unknown_gap_does_not_acquire_observers_pid(tmp_path):
    record_gap(tmp_path,'No application attachment was observed',process={})
    row = json.loads((tmp_path / 'trace/index.json').read_text())['sources'][0]
    assert row['process'] == {}


def launch_run(tmp_path, *, trace=False):
    from raven.launch import resolve_plan
    from raven.launch_runtime import _initialize_run, materialize
    script = tmp_path / 'job.sh'
    script.write_text('#!/bin/sh\ntrue\n')
    trace_flags = ['--trace'] if trace else []
    if trace and sys.version_info < (3,12):
        trace_flags += ['--trace-adapter','python-legacy-profile']
    plan = materialize(resolve_plan([str(script),'--project',str(tmp_path),'--local', *trace_flags],
                                    cwd=tmp_path,environ={}))
    run = _initialize_run(plan,{})
    return plan,run


def test_verified_run_binds_complete_frozen_plan_before_recording_writes(tmp_path):
    from raven.launch_autotrace import _Attachment, verified_run
    plan,run = launch_run(tmp_path,trace=True)
    assert verified_run(run,plan)[0] == run
    _other_plan,other = launch_run(tmp_path,trace=True)
    before = {str(path.relative_to(other)):path.read_bytes() for path in other.rglob('*') if path.is_file()}
    with pytest.raises(ValueError,match='frozen launch plan'):
        verified_run(other,plan)
    attachment = _Attachment(other,plan,chained=False,chain_error='')
    with pytest.raises(ValueError,match='frozen launch plan'):
        attachment.start()
    assert attachment.budget is None and attachment.recorder is None
    assert before == {str(path.relative_to(other)):path.read_bytes() for path in other.rglob('*') if path.is_file()}


def test_wrong_attempt_environment_emits_stderr_without_mutating_foreign_run(tmp_path,monkeypatch,capsys):
    from raven import launch_autotrace as auto
    plan,_run = launch_run(tmp_path,trace=True)
    _other_plan,other = launch_run(tmp_path,trace=True)
    before = {str(path.relative_to(other)):path.read_bytes() for path in other.rglob('*') if path.is_file()}
    monkeypatch.setattr(auto,'_ACTIVE',None)
    monkeypatch.delenv('RAVEN_TRACE_SKIP_AUTO',raising=False)
    monkeypatch.setenv('RAVEN_RUN_DIR',str(other))
    monkeypatch.setenv('RAVEN_RUN_ID',other.name)
    monkeypatch.setenv('RAVEN_LAUNCH_PLAN',str(Path(plan['runtime_bundle']).with_name('plan.json')))
    assert auto.auto_attach() is None
    assert 'frozen launch plan' in capsys.readouterr().err
    assert before == {str(path.relative_to(other)):path.read_bytes() for path in other.rglob('*') if path.is_file()}


def test_unreadable_plan_cannot_create_diagnostics_in_unverified_directory(tmp_path,monkeypatch,capsys):
    from raven import launch_autotrace as auto
    marker = tmp_path/'keep'; marker.write_text('unchanged')
    monkeypatch.setattr(auto,'_ACTIVE',None)
    monkeypatch.delenv('RAVEN_TRACE_SKIP_AUTO',raising=False)
    monkeypatch.setenv('RAVEN_RUN_DIR',str(tmp_path))
    monkeypatch.setenv('RAVEN_LAUNCH_PLAN',str(tmp_path/'missing-plan.json'))
    assert auto.auto_attach() is None
    assert 'attachment unavailable' in capsys.readouterr().err
    assert list(tmp_path.iterdir()) == [marker] and marker.read_text() == 'unchanged'


def test_no_trace_nested_startup_removes_only_verified_prior_raven_site(tmp_path):
    from raven.launch_bootstrap import prepare
    first,old_run = launch_run(tmp_path,trace=True)
    previous = {'PYTHONPATH':'/user/modules:/site/modules'}
    prepare(old_run,first,previous)
    old_site = previous['RAVEN_TRACE_SITE']
    second,new_run = launch_run(tmp_path)
    inherited = {'PYTHONPATH':old_site+':/user/modules:/site/modules','RAVEN_TRACE_SITE':old_site}
    prepare(new_run,second,inherited)
    assert 'RAVEN_TRACE_SITE' not in inherited
    assert inherited['PYTHONPATH'].split(os.pathsep) == ['/user/modules','/site/modules',second['runtime_bundle']]
    assert Path(old_site,'sitecustomize.py').exists()


def test_unverified_prior_site_does_not_remove_user_import_path(tmp_path):
    from raven.launch_bootstrap import prepare
    plan,run = launch_run(tmp_path)
    user_site = str(tmp_path/'user-site')
    inherited = {'PYTHONPATH':user_site+':/keep','RAVEN_TRACE_SITE':user_site}
    prepare(run,plan,inherited)
    assert 'RAVEN_TRACE_SITE' not in inherited
    assert inherited['PYTHONPATH'].split(os.pathsep)[:2] == [user_site,'/keep']


def test_capture_start_failure_keeps_partial_receipt_and_application_control(tmp_path,monkeypatch):
    from raven import launch_autotrace as auto
    from raven.launch_bootstrap import prepare
    from raven.trace_protocol import TraceReader, discover_trace
    from raven.trace_recorder import PythonCapture
    plan,run = launch_run(tmp_path,trace=True)
    env = {}
    prepare(run,plan,env)
    monkeypatch.setattr(auto,'_ACTIVE',None)
    monkeypatch.delenv('RAVEN_TRACE_SKIP_AUTO',raising=False)
    monkeypatch.setenv('RAVEN_RUN_DIR',str(run))
    monkeypatch.setenv('RAVEN_RUN_ID',run.name)
    monkeypatch.setenv('RAVEN_LAUNCH_PLAN',str(Path(plan['runtime_bundle']).with_name('plan.json')))
    def fail_start(self):
        raise ValueError('Injected monitoring registration failure')
    monkeypatch.setattr(PythonCapture,'start',fail_start)
    assert auto.auto_attach() is None
    paths = discover_trace(run,'local')
    assert len(paths) == 1
    snapshot = TraceReader(paths[0]).refresh()
    assert snapshot['manifest']['state'] == 'PARTIAL'
    assert not snapshot['complete']
    assert any('did not start completely' in warning for warning in snapshot['warnings'])
    rows = json.loads((run/'trace/index.json').read_text())['sources']
    assert any('monitoring registration failure' in row.get('reason','') for row in rows)


@pytest.mark.parametrize('prior_trace',[False,True])
@pytest.mark.parametrize('next_trace',[False,True])
def test_nested_launch_replaces_verified_prior_runtime_only(tmp_path,prior_trace,next_trace):
    from raven.launch_bootstrap import prepare
    first,old_run = launch_run(tmp_path,trace=prior_trace)
    inherited = {'PYTHONPATH':'/user/modules:/site/modules'}
    prepare(old_run,first,inherited)
    previous = {'RAVEN_RUN_DIR':str(old_run),'RAVEN_LAUNCH_PLAN':str(Path(first['runtime_bundle']).with_name('plan.json')),
                'RAVEN_RUNTIME_BUNDLE':first['runtime_bundle']}
    second,new_run = launch_run(tmp_path,trace=next_trace)
    prepare(new_run,second,inherited,previous=previous)
    paths = inherited['PYTHONPATH'].split(os.pathsep)
    assert first['runtime_bundle'] not in paths
    assert paths[-1] == second['runtime_bundle']
    assert paths[-3:-1] == ['/user/modules','/site/modules']
    assert Path(first['runtime_bundle']).is_file()
    assert (inherited.get('RAVEN_TRACE_SITE') is not None) == next_trace


@pytest.mark.parametrize('invalid',['wrong-run','wrong-path','public-file'])
def test_unverified_inherited_archive_keeps_user_precedence(tmp_path,invalid):
    from raven.launch_bootstrap import prepare
    first,old_run = launch_run(tmp_path)
    second,new_run = launch_run(tmp_path)
    previous = {'RAVEN_RUN_DIR':str(old_run),'RAVEN_LAUNCH_PLAN':str(Path(first['runtime_bundle']).with_name('plan.json')),
                'RAVEN_RUNTIME_BUNDLE':first['runtime_bundle']}
    if invalid == 'wrong-run':
        previous['RAVEN_RUN_DIR'] = str(new_run)
    elif invalid == 'wrong-path':
        previous['RAVEN_RUNTIME_BUNDLE'] = '/user/custom-runtime.pyz'
    else:
        Path(first['runtime_bundle']).chmod(0o644)
    old_archive = previous['RAVEN_RUNTIME_BUNDLE']
    inherited = {'PYTHONPATH':old_archive+':/keep'}
    prepare(new_run,second,inherited,previous=previous)
    assert inherited['PYTHONPATH'].split(os.pathsep) == [old_archive,'/keep',second['runtime_bundle']]
