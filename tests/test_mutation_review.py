"""Adversarial mutation cases name exact targets and preserve uncertain outcomes."""
from dataclasses import replace
import json
from pathlib import Path
import os
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from raven.actions import Actions
from raven.config import Config
from raven.controller import App
from raven.deps import DepGraph
from raven.model import Job, Store
from raven import ops_cluster as C, ops_science as S
from raven.resubmit import build, flags_of, output_pattern, parse_submitted, script_of
from raven.slurm import FakeBackend, Slurm
from raven.slurm import Backend
from test_ops_cluster import BackendFixture, job
from raven import operations as O


@pytest.fixture
def app(tmp_path):
    slurm = Slurm(FakeBackend("alex"), "alex")
    store = Store(str(tmp_path), persist=False)
    store.apply_jobs(slurm.jobs())
    value = App(store, None, Actions(slurm, store), Config(), "alex", ascii_=True, interactive=False)
    value.visible_ids = [record.id for record in store.jobs]
    value.selected_id = value.visible_ids[0]
    slurm.b.calls.clear()
    yield value
    if value.research is not None:
        value.research.close()


@pytest.mark.parametrize("command", ["cancel", "hold", "release", "requeue", "top", "resubmit", "chain cancel", "chain hold", "chain release", "cancel marked", "hold marked", "resubmit marked"])
def test_noninteractive_mutations_never_guess_a_target(app, command):
    app.run_command(command)
    assert not app.command_ok and app.mode != "confirm"
    assert app.actions.slurm.b.calls == []


def test_interactive_empty_marks_do_not_fall_back_to_selection(app):
    app.interactive = True
    app.run_command("cancel marked")
    assert not app.command_ok and app.mode != "confirm"
    assert app.actions.slurm.b.calls == []


def test_unknown_member_does_not_silently_shrink_a_destructive_scope(app):
    app.run_command("cancel " + app.selected_id + " 99999999999")
    assert not app.command_ok and app.mode != "confirm"
    assert app.actions.slurm.b.calls == []


def test_singleton_display_edges_use_numeric_order_and_are_never_destructive():
    records = [Job("9999", "experiment", "cpu", state="RUNNING", user="alice"),
               Job("10000", "experiment", "cpu", state="PENDING", dependency="singleton", user="alice")]
    graph = DepGraph(records)
    assert graph.edges == [("9999", "singleton", "10000")]
    assert graph.downstream("10000") == []
    assert graph.downstream("9999", include_inferred=False) == []
    array = DepGraph([Job("500_9", "x", "cpu", "RUNNING", user="a"), Job("500_10", "x", "cpu", "PENDING", dependency="singleton", user="a")])
    assert array.edges == [("500_9", "singleton", "500_10")]


def test_cancel_strips_only_display_throttle_and_top_sends_one_ordered_argument():
    calls = []
    backend = SimpleNamespace(call=lambda argv, timeout: (calls.append(argv) or True, ""))
    scheduler = Slurm(backend, "alice")
    scheduler.cancel(["7000_[3-10%2]", "42"])
    scheduler.top(["43", "42"])
    assert calls == [["scancel", "7000_[3-10]", "42"], ["scontrol", "top", "43,42"]]


def test_per_job_results_do_not_mislabel_successful_partial_batch(app, monkeypatch):
    jobs = [Job(str(i), "x", "cpu", state="PENDING", reason="JobHeldUser") for i in (1, 2, 3)]
    calls = []
    def release(ids):
        calls.append(ids)
        return ids != ["2"], "admin hold" if ids == ["2"] else ""
    monkeypatch.setattr(app.actions.slurm, "release", release)
    ok, _ = app.actions.run("release", jobs)
    assert not ok and calls == [["1"], ["2"], ["3"]]
    outcomes = [event for event in app.store.events if event.get("action") == "release"]
    assert [event["ok"] for event in outcomes] == [True, False, True]
    assert app.actions.applicable("release", replace(jobs[0], reason="JobHeldAdmin"))[0] is False


def test_array_clone_and_log_pattern_preserve_the_user_named_scope():
    clone = build("12345_7", {"submit_line": "sbatch --array=0-99 --wait --parsable --clusters=elsewhere sweep.sh"}, {})
    assert flags_of(clone.argv)["array"] == "7"
    assert all(flag not in clone.argv for flag in ("--wait", "--parsable", "--clusters=elsewhere"))
    rebuilt = build("12345_7", {}, {"Command": "sweep.sh"})
    assert flags_of(rebuilt.argv)["array"] == "7"
    assert output_pattern("/scratch/sweep/logs/sweep-12345.out", "12345_7", "sweep") == "/scratch/sweep/logs/%x-%j.out"


def test_submission_receipt_ignores_site_banners_and_refuses_conflicting_ids():
    assert parse_submitted("Charging 8 core-hours to project 1002\nSubmitted batch job 4242\n") == "4242"
    assert parse_submitted("Submitted batch job 4242\nSubmitted batch job 4243") is None
    assert parse_submitted("prefix Submitted batch job 4242") is None


def test_sacct_submitline_quotes_are_literal_while_recorded_argv_is_exact():
    clone = build("42", {"submit_line": "sbatch train.sh input'quoted file.csv"}, {})
    assert script_of(clone.argv) == ["train.sh", "input'quoted", "file.csv"]
    assert any("does not preserve argument boundaries" in note for note in clone.notes)
    clone = build("42", {"submit_argv": ["sbatch", "train.sh", "my file.csv"]}, {})
    assert script_of(clone.argv) == ["train.sh", "my file.csv"]



@pytest.mark.parametrize("info", [
    {"submit_line": "sbatch /project/.raven/wrappers/attempt123/batch.sh"},
    {"submit_argv": ["sbatch", "/project/.raven/wrappers/attempt123/batch.sh"]},
])
def test_resubmit_old_raven_wrapper_requires_new_run_attempt(info):
    with pytest.raises(ValueError, match="Run again with these settings"):
        build("42", info, {})
    clone = build("42", info, {}, overrides={"script": "/project/train.sh"})
    assert script_of(clone.argv) == ["/project/train.sh"]


def test_unknown_resubmit_survives_restart_and_reconciles_without_second_submit(app, monkeypatch):
    calls, marker = [], []
    def lost_ack(argv, workdir):
        calls.append(argv)
        marker.append(flags_of(argv)["comment"].split(" ", 1)[0])
        return False, "timed out after submission", None
    monkeypatch.setattr(app.actions.slurm, "submit", lost_ack)
    info = {"submit_line": "sbatch sweep.sh"}
    first = build("42", info, {})
    assert app.actions.resubmit(first)[0] is False
    assert app.store.events[-1]["outcome"] == "unknown"
    assert ": unknown " in app.store.events[-1]["text"]
    assert ": failed " not in app.store.events[-1]["text"]
    receipts = list((Path(app.store.state_dir) / "resubmissions").glob("*.json"))
    assert receipts and any(json.loads(path.read_text())["state"] == "unknown" for path in receipts)
    monkeypatch.setattr(app.actions.slurm.b, "run", lambda argv, timeout: ("", .001))
    second = build("42", info, {})
    assert app.actions.resubmit(second)[0] is False
    assert len(calls) == 1
    monkeypatch.setattr(app.actions.slurm.b, "run", lambda argv, timeout: (f"4242|{marker[0]}|alex\n", .001))
    assert app.actions.resubmit(second)[:1] == (True,)
    assert app.actions.resubmit(second)[2] == "4242"
    assert app.actions.resubmit(first)[2] == "4242"
    assert len(calls) == 1


def test_uncertain_clone_confirmation_keeps_unknown_feedback(app, monkeypatch):
    clone = build("42", {"submit_line": "sbatch sweep.sh"}, {})
    app.confirm = {"action": "resubmit", "clone": clone, "action_scope": app._action_scope()}
    app.mode = "confirm"
    monkeypatch.setattr(app.actions, "resubmit", lambda value: (
        False, "Resubmit outcome unknown; accounting receipt is delayed; do not retry", None))
    app.finish_confirm(True)
    assert not app.command_ok
    assert app.message.startswith("Resubmit outcome unknown")
    assert "failed" not in app.message and "do not retry" in app.message


def test_pending_estimate_change_does_not_invalidate_cancel(app):
    record = next(job for job in app.store.jobs if job.pending)
    app.run_command("cancel " + record.id)
    app.store.apply_jobs([replace(job, start="2099-01-01T00:00:00") if job.id == record.id else job for job in app.store.jobs])
    app.finish_confirm(True)
    assert app.command_ok and any(call[0] == "scancel" for call in app.actions.slurm.b.calls)


def test_consumed_confirmation_retains_review_without_repeating_mutation(app):
    job_id = app.selected_id
    app.run_command("cancel " + job_id)
    app.finish_confirm(True)
    calls = [call for call in app.actions.slurm.b.calls if call[0] == "scancel"]
    assert len(calls) == 1
    assert [job.id for job in app.confirm["jobs"]] == [job_id]
    assert app.confirm["consumed"] is True
    app.finish_confirm(True)
    assert [call for call in app.actions.slurm.b.calls if call[0] == "scancel"] == calls
    assert not app.command_ok and "already consumed" in app.message


def test_confirmed_scheduler_action_and_clone_preparation_run_off_the_ui_thread(app, monkeypatch):
    app.interactive = True
    seen = []
    original = app.actions.slurm.cancel
    def slow(ids):
        seen.append(threading.current_thread().name)
        time.sleep(.1)
        return original(ids)
    monkeypatch.setattr(app.actions.slurm, "cancel", slow)
    app.run_command("cancel " + app.selected_id)
    begin = time.monotonic()
    app.finish_confirm(True)
    assert time.monotonic() - begin < .08
    assert "background" in app.message
    deadline = time.monotonic() + 3
    while app.research.pending and time.monotonic() < deadline:
        app.research.poll_task()
        time.sleep(.01)
    assert seen and all(name != "MainThread" for name in seen)
    assert "sent" in app.message



def test_explicit_mutation_takes_priority_over_automatic_project_read(app):
    from raven.job_panels import _worker
    app.interactive = True
    hub = _worker(app, None)
    release = threading.Event()
    callback = lambda value: pytest.fail("Superseded automatic result must not publish")
    assert hub.start_task(lambda: release.wait(2), callback)
    app.project_state.update(auto_pending=True, auto_callback=callback)
    app.run_command("cancel " + app.selected_id)
    app.finish_confirm(True)
    assert "background" in app.message
    release.set()
    deadline = time.monotonic() + 3
    while hub.pending and time.monotonic() < deadline:
        hub.poll_task()
        time.sleep(.01)
    assert "sent" in app.message


def test_resubmit_lookup_and_validation_never_block_the_interactive_key(app, monkeypatch):
    app.interactive = True
    seen = []
    def lookup(record):
        seen.append(threading.current_thread().name)
        time.sleep(.1)
        return {"submit_line": "sbatch sweep.sh"}
    def preview(*args):
        seen.append(threading.current_thread().name)
        time.sleep(.1)
        return True, "validated"
    monkeypatch.setattr(app.actions.slurm, "record_submit_info", lookup)
    monkeypatch.setattr(app.actions.slurm, "preview_submit", preview)
    begin = time.monotonic()
    app.run_command("resubmit " + app.selected_id)
    assert time.monotonic() - begin < .08
    deadline = time.monotonic() + 3
    while app.research.pending and time.monotonic() < deadline:
        app.research.poll_task()
        time.sleep(.01)
    assert seen and all(name != "MainThread" for name in seen)
    assert app.mode == "confirm"


def test_connection_change_after_review_cannot_apply_old_target_ids(app):
    app.run_command("cancel " + app.selected_id)
    app.store._recording_scope = "another-cluster"
    app.finish_confirm(True)
    assert not app.command_ok and "Connection changed" in app.message
    assert not app.actions.slurm.b.calls


def test_pending_parent_with_running_tasks_is_refused_and_pending_scope_is_rechecked(tmp_path):
    backend = BackendFixture()
    backend.jobs["42"] = job(ArrayJobId="42", ArrayTaskId="0-2")
    backend.jobs["42_3"] = job(JobId="45", ArrayJobId="42", ArrayTaskId="3", JobState="RUNNING")
    ctx = O.Context(slurm=SimpleNamespace(b=backend, user="alice"), state_dir=str(tmp_path), scope={"host": "cluster", "user": "alice"})
    params = dict(job_id="42", field="TimeLimit", value="00:30:00")
    with pytest.raises(ValueError, match="non-pending"):
        C._pending_edit(params, ctx)
    del backend.jobs["42_3"]
    review = C._pending_edit(params, ctx)
    assert review["plan"]["payload"]["argv"][2] == "JobId=42_[0,1,2]"
    assert any("3 tasks" in row for row in review["rows"])
    backend.jobs["42_3"] = job(JobId="45", ArrayJobId="42", ArrayTaskId="3", JobState="RUNNING")
    with pytest.raises(ValueError, match="non-pending"):
        C.apply("pending-edit", review["plan"], ctx)
    assert not any(argv[:2] == ("scontrol", "update") for argv in backend.calls)


def test_dependency_repair_orders_edge_reversal_without_a_transient_cycle():
    repairs = [{"job_id": "1", "before": "", "after": "afterok:2"},
               {"job_id": "2", "before": "afterok:1", "after": ""}]
    references = {"1": {"dependency": ""}, "2": {"dependency": "afterok:1"}}
    assert [row["job_id"] for row in S._repair_order(repairs, references)] == ["2", "1"]


def test_pending_edit_readback_normalizes_dependencies_and_relative_begin():
    assert C._same_value("Dependency", "afterok:1001", "afterok:1001(unfulfilled)")
    import datetime as dt
    now = time.time()
    future = dt.datetime.fromtimestamp(now + 3600).replace(microsecond=0).isoformat()
    assert C._same_value("BeginTime", "now+1hour", future, reference=now)
    assert not C._same_value("BeginTime", "now+1hour", "2099-01-01T00:00:00", reference=now)


def test_large_array_throttle_reads_one_exact_child_not_the_entire_parent(tmp_path):
    class LargeArray(BackendFixture):
        def run(self, argv, timeout=8):
            if argv[:4] == ["scontrol", "show", "job", "-o"] and argv[4] == "42":
                raise AssertionError("An 800-task parent reply would exceed the response budget")
            return super().run(argv, timeout)
    backend = LargeArray()
    backend.jobs["42"] = job(ArrayJobId="42", ArrayTaskId="0-799", ArrayTaskThrottle="10")
    ctx = O.Context(slurm=SimpleNamespace(b=backend, user="alice"), state_dir=str(tmp_path), scope={"host": "cluster", "user": "alice"})
    review = C._array_throttle(dict(job_id="42", limit="20"), ctx)
    assert review["plan"]["payload"]["read_id"] == "42_0"
    assert C.apply("array-throttle", review["plan"], ctx)["data"]["confirmed"]


@pytest.mark.skipif(os.name != "posix", reason="process-group stop semantics require POSIX")
def test_submission_timeout_signals_the_entire_owned_group_and_keeps_the_ack(tmp_path):
    received = tmp_path / "child-signal"
    ready = tmp_path / "child-ready"
    child = "import signal,time,sys; from pathlib import Path; " + \
        f"signal.signal(signal.SIGTERM,lambda *args:(Path({str(received)!r}).write_text('TERM'),sys.exit(0))); " + \
        f"Path({str(ready)!r}).write_text('ready'); time.sleep(60)"
    parent = "import subprocess,sys,time; from pathlib import Path; " + \
        f"subprocess.Popen([sys.executable,'-c',{child!r}]); " + \
        "print('Submitted batch job 4242',flush=True); time.sleep(60)"
    ok, output = Backend().call([sys.executable, "-c", parent], timeout=.5)
    assert not ok and "outcome unknown" in output and "Submitted batch job 4242" in output
    assert ready.exists() and received.read_text() == "TERM"
