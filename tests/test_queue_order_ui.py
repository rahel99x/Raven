"""Group drops prepare one frozen background review without implicit apply."""
from dataclasses import replace

import pytest

from tower import job_groups as G, manual_job_groups as M, queue_order_ui as Q, ops_ui
from tower.model import Finished, Job
from test_operations_sequences import operation_dashboard
from test_ops_queue_order import Backend


def setup_groups(ui):
    app = ui.app
    backend = Backend()
    ui.slurm.b = backend
    ui.slurm.user = "alice"
    ui.store.jobs = [Job(str(jid), "experiment", "cpu", "PENDING", submit="2026-10-01T12:00:00",
                        user="alice", account="lab", cluster="cluster", workdir="/project", command="/project/train.sh")
                     for jid in range(10, 16)]
    snap = ui.store.snapshot()
    source = M.create(app, snap, ["14", "15"])
    target = M.create(app, snap, ["10", "11"])
    index = G.registry(app).ensure(snap)
    evidence = M.target_evidences(app, snap, [source.group_id, target.group_id], index=index)
    ui.draw()
    return app, backend, evidence[source.group_id], evidence[target.group_id]


def test_drop_opens_review_with_exact_own_pending_order_but_never_applies(operation_dashboard):
    app, backend, source, target = setup_groups(operation_dashboard)
    assert Q.prepare_drop(app, source, target, "before")
    assert app.mode == "operations"
    assert app.ops_state["feature"] == "queue-order"
    report = app.ops_state["result"]
    assert report["plan"]["payload"]["proposed"] == ["14", "15", "10", "11", "12", "13"]
    assert not any(call[:2] == ["scontrol", "top"] for call in backend.calls)
    operation_dashboard.draw()
    assert any(control.id.startswith("ops") for control in app.interaction_state["graph"].controls)


def test_archived_group_members_need_no_purged_controller_record(operation_dashboard):
    app, backend, source, target = setup_groups(operation_dashboard)
    operation_dashboard.store.finished = [Finished("99", submit="2026-10-01T12:00:00", cluster="cluster",
                                                 user="alice", account="lab")]
    snap = operation_dashboard.store.snapshot()
    assert M.add(app, snap, source.group_id, ["99"], expected=source).changed
    source = M.target_evidence(app, snap, source.group_id)
    assert source.members == ("14", "15", "99")
    assert Q.prepare_drop(app, source, target)
    report = app.ops_state["result"]
    assert report["status"] == "ok" and report["plan"]
    assert report["plan"]["params"]["source_jobs"] == "14,15"
    assert any("1 archived" in warning for warning in report["warnings"])
    assert not any("99" in argument for call in backend.calls for argument in call)


@pytest.mark.parametrize("which", ["source", "target"])
def test_membership_change_after_grip_press_cancels_drop_without_scheduler_io(operation_dashboard, which):
    app, backend, source, target = setup_groups(operation_dashboard)
    chosen = source if which == "source" else target
    M.add(app, operation_dashboard.store.snapshot(), chosen.group_id, ["12"], expected=chosen)
    assert not Q.prepare_drop(app, source, target)
    assert app.mode == "main"
    assert not backend.calls


def test_reused_job_attempt_cancels_drop_before_reading_scheduler(operation_dashboard):
    app, backend, source, target = setup_groups(operation_dashboard)
    operation_dashboard.store.jobs[-1] = replace(operation_dashboard.store.jobs[-1], submit="2026-10-02T12:00:00")
    assert not Q.prepare_drop(app, source, target)
    assert not backend.calls


@pytest.mark.parametrize("mode,tab", [("main", "history"), ("confirm", "jobs"), ("operations", "jobs")])
def test_inactive_or_modal_drop_cannot_open_operation(operation_dashboard, mode, tab):
    app, backend, source, target = setup_groups(operation_dashboard)
    app.mode, app.tab = mode, tab
    assert not Q.prepare_drop(app, source, target)
    assert not backend.calls


def test_worker_busy_never_replaces_existing_operation_or_executes_order(operation_dashboard):
    app, backend, source, target = setup_groups(operation_dashboard)
    ops_ui._open(app, "storage")
    app.ops_state.update(running=True)
    app.mode = "main"
    try:
        assert not Q.prepare_drop(app, source, target)
        assert app.ops_state["feature"] == "storage"
        assert not backend.calls
    finally:
        app.ops_state["running"] = False


def test_site_disabled_shows_error_report_without_scheduler_write(operation_dashboard):
    app, backend, source, target = setup_groups(operation_dashboard)
    backend.flags = ""
    assert Q.prepare_drop(app, source, target)
    assert app.ops_state["result"]["status"] == "error"
    assert "enable_user_top" in app.ops_state["error"]
    assert backend.calls == [["scontrol", "show", "config"]]
