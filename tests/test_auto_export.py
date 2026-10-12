"""Completion exports exercise real streaming/publication and recovery locks."""
from concurrent.futures import ThreadPoolExecutor
import copy
import csv
import json
from pathlib import Path
import threading

import pytest

from raven import auto_export as A, data_export as E, reporting as R


@pytest.fixture
def run(tmp_path):
    root = tmp_path / "lab project α"
    root.mkdir()
    (root / "batch.sh").write_text("#!/bin/bash\ntrue\n")
    return R.begin_run(root, "run-one", name="encoder seed 3", script="batch.sh")


def read(path):
    return json.loads(Path(path).read_text())


def finish(run, status="COMPLETED"):
    R.write_metric(run, {"loss": 0.1234567890123456}, step=5)
    return R.finish_run(run, state=status, exit_code=0 if status == "COMPLETED" else 7,
                        runtime_seconds=2.125)


def request(**extra):
    return dict(include=["application", "summary"], **extra)


@pytest.mark.parametrize("outcome", ["COMPLETED", "FAILED", "CANCELLED", "INTERRUPTED"])
def test_completion_uses_retained_data_and_preserves_application_outcome(run, outcome):
    state = A.capture(run, request())
    assert A.process_pending(run.parent.parent) == []
    assert not (run.parent.parent / "exports").exists()
    finish(run, outcome)
    summary_before = (run / "summary.json").read_bytes()
    result, = A.process_pending(run.parent.parent)
    assert result["status"] == "complete"
    assert (run / "summary.json").read_bytes() == summary_before
    output = Path(result["outputs"][0])
    with (output / "application-metrics.csv").open() as stream:
        row, = list(csv.DictReader(stream))
    assert float(row["value"]) == 0.1234567890123456
    assert row["run_id"] == run.name
    assert read(output / "summary.json")["state"] == outcome
    assert A.process_pending(run.parent.parent) == []
    assert A.capture(run, request())["key"] == state["key"]
    assert A.read_status(run)[0]["outputs"] == result["outputs"]
    assert len(list((run.parent.parent / "exports").rglob("manifest.json"))) == 1


def test_captured_preferences_do_not_follow_mutation(run):
    preferences = request(destination={"mode": "relative", "path": "paper/data α"})
    A.capture(run, preferences)
    preferences["destination"]["path"] = "later preference"
    finish(run)
    result, = A.process_pending(run.parent.parent)
    assert "/paper/data α/" in result["outputs"][0]
    assert not (run.parent.parent / "later preference").exists()


def test_two_workers_cannot_publish_same_request_twice(run, monkeypatch):
    A.capture(run, request())
    finish(run)
    entered, release = threading.Event(), threading.Event()
    actual = E.export_data
    def held(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return actual(*args, **kwargs)
    monkeypatch.setattr(E, "export_data", held)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(A.process_pending, run.parent.parent)
        assert entered.wait(5)
        second = pool.submit(A.process_pending, run.parent.parent)
        assert second.result(timeout=5) == []
        release.set()
        assert first.result(timeout=5)[0]["status"] == "complete"
    assert len(list((run.parent.parent / "exports").rglob("manifest.json"))) == 1


def test_recover_after_published_bundle_before_receipt(run, monkeypatch):
    A.capture(run, request())
    finish(run)
    actual = E.export_data
    class SupervisorLost(BaseException):
        pass
    def lost(*args, **kwargs):
        actual(*args, **kwargs)
        raise SupervisorLost()
    monkeypatch.setattr(E, "export_data", lost)
    with pytest.raises(SupervisorLost):
        A.process_pending(run.parent.parent)
    first_bundle, = list((run.parent.parent / "exports").rglob("manifest.json"))
    before = first_bundle.read_bytes()
    monkeypatch.setattr(E, "export_data", actual)
    recovered, = A.process_pending(run.parent.parent)
    assert recovered["status"] == "complete"
    assert first_bundle.read_bytes() == before
    assert len(list((run.parent.parent / "exports").rglob("manifest.json"))) == 1


def test_retry_bounded_and_explicit_retry_preserves_plan(run, monkeypatch):
    A.capture(run, request())
    finish(run)
    actual = E.export_data
    plans = []
    def full(plan, **kwargs):
        plans.append(copy.deepcopy(plan))
        raise OSError("No space left on device")
    monkeypatch.setattr(E, "export_data", full)
    clock = [100000000000.]
    monkeypatch.setattr(A.time, "time", lambda: clock[0])
    for expected in range(1, 4):
        result, = A.process_pending(run.parent.parent)
        assert result["tries"] == expected and result["status"] == "failed"
        clock[0] += 1000
    assert A.process_pending(run.parent.parent) == []
    assert plans[0] == plans[1] == plans[2]
    assert read(run / "summary.json")["exit_code"] == 0
    monkeypatch.setattr(E, "export_data", actual)
    assert A.process_pending(run.parent.parent, retry=True)[0]["status"] == "complete"


def scheduler_run(run):
    inventory = read(run / "run.json")
    inventory.update(job_id="1200_3", attempt=2, submit=1720000000.25)
    inventory["metadata"] = {"raven_launch": {"cluster": "lab", "submission_token": "a" * 32}}
    R._atomic_json(run / "run.json", inventory, replace=True)
    return {"state": "CANCELLED", "job_id": "1200_3", "cluster": "lab", "attempt": 2,
            "submit_time": 1720000000., "observed_at": 1720000010., "source": "test accounting"}


@pytest.mark.parametrize("field,value", [("job_id", "1200_4"), ("cluster", "other"),
                                         ("attempt", 1), ("submit_time", 1720100000.),
                                         ("submit_time", None)])
def test_stale_accounting_does_not_complete_missing_cleanup(run, field, value):
    evidence = scheduler_run(run)
    A.capture(run, request())
    evidence[field] = value
    assert A.process_pending(run.parent.parent, terminal_evidence={run.name: evidence}) == []
    assert not (run / "scheduler-outcome.json").exists()
    assert not (run.parent.parent / "exports").exists()


def test_missing_cleanup_uses_verified_accounting_without_fabricating_summary(run):
    evidence = scheduler_run(run)
    A.capture(run, request())
    result, = A.process_pending(run.parent.parent, terminal_evidence={run.name: evidence})
    assert result["status"] in {"complete", "partial"}
    assert read(run / "run.json")["state"] == "RUNNING"
    assert not (run / "summary.json").exists()
    assert read(run / "scheduler-outcome.json")["state"] == "CANCELLED"


def test_reused_run_directory_never_exports_another_attempt(run):
    A.capture(run, request())
    finish(run)
    inventory = read(run / "run.json")
    inventory["attempt"] += 1
    R._atomic_json(run / "run.json", inventory, replace=True)
    result, = A.process_pending(run.parent.parent)
    assert result["status"] == "failed" and "identity changed" in result["reason"]
    assert not (run.parent.parent / "exports").exists()


def test_capture_rejects_another_subject_and_symlink_queue(run, tmp_path):
    with pytest.raises(ValueError, match="another project"):
        A.capture(run, request(project=str(tmp_path)))
    with pytest.raises(ValueError, match="separately"):
        A.capture(run, request(runs=["other"]))
    queue = run.parent.parent / ".raven" / "auto-exports"
    import shutil
    shutil.rmtree(queue)
    outside = tmp_path / "outside"
    outside.mkdir()
    queue.symlink_to(outside, target_is_directory=True)
    with pytest.raises(OSError):
        A.capture(run, request())
    assert list(outside.iterdir()) == []


def test_readonly_empty_status_never_creates_state(run):
    root = run.parent.parent
    before = sorted(str(p.relative_to(root)) for p in root.rglob("*"))
    assert A.read_status(run) == A.pending_status(root) == A.process_pending(root) == []
    assert sorted(str(p.relative_to(root)) for p in root.rglob("*")) == before


def test_launch_index_recovers_prestart_request_without_resubmitting(run):
    root = run.parent.parent
    inventory = read(run / "run.json")
    inventory.update(state="SUBMITTED", job_id="222", submit=1720000000.25)
    inventory.pop("start")
    inventory["metadata"] = {"raven_launch": {"cluster": "lab", "submission_token": "b" * 32}}
    R._atomic_json(run / "run.json", inventory, replace=True)
    A.capture_launch(root, "b" * 32, request())
    launches = root / ".raven/launches"
    launches.mkdir()
    R._atomic_json(launches / ("b" * 32 + ".json"), {"schema": "raven.launch/v1", "token": "b" * 32,
                     "status": "submitted", "run_id": run.name, "job_id": "222"})
    evidence = {"state": "CANCELLED", "job_id": "222", "cluster": "lab", "attempt": 1,
                "submit_time": 1720000000., "observed_at": 1720000010.}
    result, = A.process_pending(root, terminal_evidence={run.name: evidence})
    assert result["status"] in {"complete", "partial"}
    assert not (run / "summary.json").exists()
    assert A.process_pending(root, terminal_evidence={run.name: evidence}) == []


def test_interrupted_capture_remains_visible_and_idempotent_capture_repairs_run_index(run, monkeypatch):
    actual = A._write
    def interrupted(parent, name, value, **kwargs):
        if name == 'auto-export.json':
            raise OSError('interrupted after durable pending publication')
        return actual(parent, name, value, **kwargs)
    monkeypatch.setattr(A, '_write', interrupted)
    with pytest.raises(OSError, match='interrupted'):
        A.capture(run, request())
    assert not (run / 'auto-export.json').exists()
    pending, = A.pending_status(run.parent.parent)
    assert A.read_status(run)[0]['key'] == pending['key']
    monkeypatch.setattr(A, '_write', actual)
    recovered = A.capture(run, request())
    assert recovered['key'] == pending['key']
    assert read(run / 'auto-export.json')['keys'] == [pending['key']]
    finish(run)
    assert A.process_pending(run.parent.parent)[0]['status'] == 'complete'


def test_processing_repairs_index_after_interrupted_capture_without_recapture(run, monkeypatch):
    actual = A._write
    def interrupted(parent, name, value, **kwargs):
        if name == 'auto-export.json':
            raise OSError('interrupted')
        return actual(parent, name, value, **kwargs)
    monkeypatch.setattr(A, '_write', interrupted)
    with pytest.raises(OSError):
        A.capture(run, request())
    monkeypatch.setattr(A, '_write', actual)
    finish(run)
    done, = A.process_pending(run.parent.parent)
    assert A.read_status(run)[0]['key'] == done['key']
    assert read(run / 'auto-export.json')['keys'] == [done['key']]


def test_round_robin_deadline_does_not_starve_completed_request_behind_active_runs(run, monkeypatch):
    root = run.parent.parent
    runs = [run] + [R.begin_run(root, 'running-' + str(i), name='work', script='batch.sh') for i in range(5)]
    states = [A.capture(path, request()) for path in runs]
    last = max(states, key=lambda value: value['key'])
    finish(root / 'runs' / last['identity']['run_id'])
    now = [0.]
    actual = A._inventory
    def costly_active_read(root_fd, run_id):
        value = actual(root_fd, run_id)
        if value['state'] == 'RUNNING':
            now[0] += .6
        return value
    monkeypatch.setattr(A.time, 'monotonic', lambda: now[0])
    monkeypatch.setattr(A, '_inventory', costly_active_read)
    completed = []
    for _ in range(4):
        completed.extend(A.process_pending(root, deadline_seconds=.9))
    assert any(value['key'] == last['key'] and value['status'] == 'complete' for value in completed)


def test_unknown_acceptance_launch_remains_visible_without_submission_or_mutation(run):
    root = run.parent.parent
    A.capture_launch(root, 'c' * 32, request())
    before = sorted(str(path.relative_to(root)) for path in root.rglob('*'))
    candidate, = A.launch_candidates(root)
    assert candidate['token'] == 'c' * 32 and candidate['identity'] is None
    assert candidate['status'] == 'pending' and 'unresolved' in candidate['reason']
    assert before == sorted(str(path.relative_to(root)) for path in root.rglob('*'))


def test_launch_round_robin_advances_past_unresolved_prefix(run, monkeypatch):
    root = run.parent.parent
    for token in ('1' * 32, '2' * 32, '3' * 32):
        A.capture_launch(root, token, request())
    now, examined = [0.], []
    actual = A._launch_inventory
    def slow(project, root_fd, folders, name):
        examined.append(name)
        now[0] += .6
        return actual(project, root_fd, folders, name)
    monkeypatch.setattr(A.time, 'monotonic', lambda: now[0])
    monkeypatch.setattr(A, '_launch_inventory', slow)
    A.process_pending(root, deadline_seconds=.9)
    A.process_pending(root, deadline_seconds=.9)
    A.process_pending(root, deadline_seconds=.9)
    assert '3' * 32 + '.json' in examined


def test_dashboard_reconciles_prestart_array_parent_from_launch_candidates(run, monkeypatch):
    from types import SimpleNamespace
    from raven import auto_export_ui as UI
    from raven.remote import LocalFiles
    root = run.parent.parent
    token = 'd' * 32
    inventory = read(run / 'run.json')
    inventory.update(state='SUBMITTED', job_id='222', submit=1720000000.25)
    inventory.pop('start')
    inventory['metadata'] = {'raven_launch': {'cluster': 'lab', 'submission_token': token, 'array_id': '222'}}
    R._atomic_json(run / 'run.json', inventory, replace=True)
    A.capture_launch(root, token, request())
    launches = root / '.raven/launches'
    launches.mkdir()
    R._atomic_json(launches / (token + '.json'), {'schema': 'raven.launch/v1', 'token': token, 'run_id': run.name})
    assert A.pending_status(root) == []
    job = SimpleNamespace(id='222', state='CANCELLED', cluster='lab')
    details = {'Restarts': '0', 'SubmitTime': '2024-07-03T09:46:40+00:00'}
    hub = SimpleNamespace(files=LocalFiles(), pending=None, future=None)
    def start(worker, completion):
        hub.work, hub.complete = worker, completion
        return True
    hub.start_task = start
    messages = []
    app = SimpleNamespace(interactive=True, research=hub, project_state={'root': str(root)},
                          store=SimpleNamespace(record_context=lambda jid: (job, details)),
                          say=lambda message, **kwargs: messages.append(message))
    observed = [100.]
    monkeypatch.setattr(UI.time, "monotonic", lambda: observed[0])
    assert not UI.tick(app)  # Explicit project work owns the initial interval.
    observed[0] += UI.RECOVERY_INTERVAL
    assert UI.tick(app)
    result = hub.work()
    hub.complete(result)
    assert result['results'][0]['status'] in {'complete', 'partial'}
    assert result['launches'] == []
    assert read(run / 'scheduler-outcome.json')['state'] == 'CANCELLED'
    assert not (run / 'summary.json').exists()


def test_recovery_callback_cannot_publish_after_leaving_supported_project(run, monkeypatch):
    from types import SimpleNamespace
    from raven import auto_export_ui as UI
    from raven.remote import LocalFiles
    hub = SimpleNamespace(files=LocalFiles(), pending=None, future=None)
    def start(worker, completion):
        hub.complete = completion
        return True
    hub.start_task = start
    messages = []
    app = SimpleNamespace(interactive=True, research=hub, project_state={'root': str(run.parent.parent)}, store=None,
                          say=lambda message, **kwargs: messages.append(message))
    observed = [100.]
    monkeypatch.setattr(UI.time, "monotonic", lambda: observed[0])
    assert not UI.tick(app)  # Explicit project work owns the initial interval.
    observed[0] += UI.RECOVERY_INTERVAL
    assert UI.tick(app)
    old_generation = app.auto_export_state['generation']
    app.project_state = {}
    assert not UI.tick(app)
    assert app.auto_export_state['generation'] > old_generation
    hub.complete(RuntimeError('old project error'))
    assert not messages and app.auto_export_state['error'] == ''
    assert not app.auto_export_state['pending']


def test_verified_completed_array_parent_retires_launch_without_duplicate_parent_export(run):
    root = run.parent.parent
    token = 'e' * 32
    inventory = read(run / 'run.json')
    inventory.update(state='SUBMITTED', job_id='222', submit=1720000000.25)
    inventory.pop('start')
    inventory['metadata'] = {'raven_launch': {'cluster': 'lab', 'submission_token': token, 'array_id': '222'}}
    R._atomic_json(run / 'run.json', inventory, replace=True)
    A.capture_launch(root, token, request())
    launches = root / '.raven/launches'
    launches.mkdir()
    R._atomic_json(launches / (token + '.json'), {'schema': 'raven.launch/v1', 'token': token, 'run_id': run.name})
    terminal = {'state': 'COMPLETED', 'job_id': '222', 'cluster': 'lab', 'attempt': 1, 'submit_time': 1720000000.}
    assert A.process_pending(root, terminal_evidence={run.name: terminal}) == []
    assert A.launch_candidates(root) == [] and A.pending_status(root) == []
    assert not (root / 'exports').exists()


def test_reused_attempt_does_not_repair_foreign_pending_index(run):
    pending = A.capture(run, request())
    (run / 'auto-export.json').unlink()
    inventory = read(run / 'run.json')
    inventory['attempt'] += 1
    R._atomic_json(run / 'run.json', inventory, replace=True)
    result, = A.process_pending(run.parent.parent)
    assert result['key'] == pending['key'] and result['status'] == 'failed'
    assert not (run / 'auto-export.json').exists()


def test_unresolved_launches_cannot_consume_entire_pending_export_slice(run, monkeypatch):
    root = run.parent.parent
    for token in ('1' * 32, '2' * 32, '3' * 32):
        A.capture_launch(root, token, request())
    A.capture(run, request())
    finish(run)
    now = [0.]
    actual = A._launch_inventory
    def slow(project, root_fd, folders, name):
        now[0] += .3
        return actual(project, root_fd, folders, name)
    monkeypatch.setattr(A.time, 'monotonic', lambda: now[0])
    monkeypatch.setattr(A, '_launch_inventory', slow)
    completed, = A.process_pending(root, deadline_seconds=1.)
    assert completed['status'] == 'complete'


def test_cancellation_retry_exhaustion_is_visible_and_never_changes_workload(run, monkeypatch):
    A.capture(run, request())
    finish(run)
    before = (run / 'summary.json').read_bytes()
    now = [100000000000.]
    monkeypatch.setattr(A.time, 'time', lambda: now[0])
    monkeypatch.setattr(E, 'export_data', lambda *a, **k: {'status': 'cancelled', 'outputs': []})
    for attempt in range(3):
        value, = A.process_pending(run.parent.parent)
        assert value['tries'] == attempt + 1
        now[0] += 1000
    assert value['status'] == 'failed' and 'exhausted' in value['reason']
    assert '--reconcile --retry' in value['reason']
    assert A.process_pending(run.parent.parent) == []
    assert (run / 'summary.json').read_bytes() == before


def test_noninteractive_observation_does_not_start_incidental_export_work(run, monkeypatch):
    from types import SimpleNamespace
    from raven import auto_export_ui as UI
    from raven.remote import LocalFiles
    queued = []
    hub = SimpleNamespace(files=LocalFiles(), pending=None, future=None,
                          start_task=lambda *args: queued.append(args) or True)
    app = SimpleNamespace(interactive=False, research=hub,
                          project_state={'root': str(run.parent.parent)}, store=None)
    before = list(run.parent.parent.rglob('*'))
    monkeypatch.setattr(UI.auto_export, 'pending_status', lambda root: pytest.fail('noninteractive tick inspected exports'))
    for _ in range(3):
        assert not UI.tick(app)
    assert queued == []
    assert list(run.parent.parent.rglob('*')) == before


def test_periodic_recovery_defers_to_initial_project_work_and_active_dialogs(run, monkeypatch):
    from types import SimpleNamespace
    from raven import auto_export_ui as UI
    from raven.remote import LocalFiles
    observed, queued = [0.], []
    monkeypatch.setattr(UI.time, 'monotonic', lambda: observed[0])
    hub = SimpleNamespace(files=LocalFiles(), pending=None, future=None,
                          start_task=lambda *args: queued.append(args) or True)
    app = SimpleNamespace(interactive=True, mode='main', research=hub,
                          project_state={'root': str(run.parent.parent)}, store=None)
    assert not UI.tick(app)
    observed[0] = UI.RECOVERY_INTERVAL - .01
    assert not UI.tick(app)
    assert queued == []
    observed[0] = UI.RECOVERY_INTERVAL
    app.mode = 'project_runs'
    assert not UI.tick(app)
    app.mode = 'main'
    app.project_state['busy'] = True
    assert not UI.tick(app)
    app.project_state['busy'] = False
    assert UI.tick(app)
    assert len(queued) == 1


def test_switching_to_noninteractive_cancels_pending_periodic_recovery(run, monkeypatch):
    from types import SimpleNamespace
    from raven import auto_export_ui as UI
    from raven.remote import LocalFiles
    observed = [100.]
    monkeypatch.setattr(UI.time, 'monotonic', lambda: observed[0])
    hub = SimpleNamespace(files=LocalFiles(), pending=None, future=None,
                          start_task=lambda *args: True)
    app = SimpleNamespace(interactive=True, research=hub,
                          project_state={'root': str(run.parent.parent)}, store=None)
    assert not UI.tick(app)
    observed[0] += UI.RECOVERY_INTERVAL
    assert UI.tick(app)
    cancellation = app.auto_export_state['cancel']
    app.interactive = False
    assert not UI.tick(app)
    assert cancellation.is_set()
    assert not app.auto_export_state['pending']
