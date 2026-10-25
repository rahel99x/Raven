"""Fresh dashboard discovery over real producer registrations and journals.

Scheduler records are explicit fixtures. Project publication, background reads,
run binding and trace verification use their production owners; no Docker/live
Slurm qualification is claimed by these tests.
"""

from __future__ import annotations

import hashlib
import json
from itertools import pairwise
from types import SimpleNamespace

import pytest
from test_job_panels import dashboard as dashboard  # noqa: PLC0414 -- pytest fixture
from test_trace_timeline_workflow import connect

from raven import job_panels, project_ui, trace_ui
from raven import layout as L
from raven import reporting as R
from raven.execution.policy import bind
from raven.model import Finished, Job
from raven.research import ResearchHub
from raven.research_reporting import _job_index
from raven.tracing.capture import Recorder, register_run
from raven.tracing.workspace_session import WorkspaceSession


@pytest.fixture
def published_run(tmp_path):
    root = tmp_path / "single-thread project"
    root.mkdir()
    (root / "job.sh").write_text("#!/bin/sh\nprintf 'scientific job\\n'\n")
    token = "a" * 32
    run = R.begin_run(
        root,
        "launch-" + token + "-a1",
        name="raven-single-thread",
        script="job.sh",
        job_id="900",
    )
    inventory = json.loads((run / "run.json").read_text())
    inventory["metadata"] = {
        "raven_launch": {
            "cluster": "cluster",
            "restart_count": 0,
            "submission_token": token,
            "array_id": "",
            "array_index": None,
            "dependency": "",
        }
    }
    R._atomic_json(run / "run.json", inventory, replace=True)
    _job_index(root, "900", "cluster", run.name)
    with (
        Recorder(
            run,
            job_id="900",
            attempt=run.name,
            cluster="cluster",
            register=False,
            background_flush=False,
            identity_extra={"run_id": run.name},
        ) as recorder,
        recorder.span("single_thread_scientific_step"),
    ):
        pass
    trace = run / "trace"
    trace.mkdir()
    R._atomic_json(
        trace / "index.json",
        {
            "schema": "raven.trace-index/v1",
            "sources": [
                {
                    "schema": "raven.trace/v1",
                    "path": str(recorder.path.relative_to(run)),
                    "stream": recorder.stream,
                    "run_id": run.name,
                    "status": "complete",
                    "process": {},
                    "reason": "",
                }
            ],
        },
    )
    register_run(root, run, "900", run.name)
    R.finish_run(run, state="COMPLETED", exit_code=0)
    # Automatic Python launch recordings use the run UUID rather than optional
    # native/snapshot Slurm time anchors. Keep that actual producer contract.
    identity = json.loads(recorder.path.read_text())["identity"]
    assert "slurm_submit" not in identity and "slurm_start" not in identity
    return SimpleNamespace(
        root=root, run=run, manifest=recorder.path, identity=identity
    )


def settle(hub):
    """Publish one actual shared-reader completion without ticking other views."""
    assert hub.pending is not None
    hub.future.result(timeout=5)
    hub.poll_task()


def ordinary_dashboard(
    dashboard, published, *, tab, details=True, stale_research=False, managed=False
):
    app = connect(dashboard)
    shared = {
        "submit": "2026-10-24T05:26:04",
        "start": "2026-10-24T05:26:06",
        "workdir": str(published.root),
        "cluster": "cluster",
        "restarts": 0,
    }
    if tab == "history":
        selected = Finished("900", "raven-single-thread", "COMPLETED", **shared)
        app.store.jobs = []
        app.store.finished = [selected]
    else:
        selected = Job("900", "raven-single-thread", "local", "RUNNING", **shared)
        app.store.jobs = [selected]
        app.store.finished = []
    app.store.details["900"] = (
        {"WorkDir": str(published.root), "Cluster": "cluster", "Restarts": "0"}
        if details
        else {}
    )
    app.selected_id, app.tab, app.mode = "900", tab, "main"
    if tab in ("jobs", "history"):
        # Jobs and History deliberately remember separate Details tabs.
        assert job_panels.run_command(app, ["jobpanel", "trace"])
    app.research_job_id = "stale-research-job" if stale_research else None
    app.files = app.logs.files = dashboard.views.files
    app.research = ResearchHub(app.cfg, files=app.files)
    if managed:
        app.cfg.set("raven", {"managed": True, "mode": "light", "site": "slurm"})
        bind(app)
    assert project_ui.selected_binding(app) is None
    if tab == "traces":
        assert trace_ui.run_command(app, ["traces"])
    return app, selected


@pytest.mark.parametrize("tab", ["jobs", "history", "traces"])
@pytest.mark.parametrize("details", [True, False])
@pytest.mark.parametrize("managed", [False, True], ids=["legacy", "managed-light"])
def test_fresh_dashboard_automatically_binds_and_reads_exact_recording(
    dashboard, published_run, tab, details, managed
):
    app, selected = ordinary_dashboard(
        dashboard, published_run, tab=tab, details=details, managed=managed
    )
    try:
        # No :project, :run select, receipt-specific command or manual state
        # binding: this is the ordinary selected scheduler row path.
        assert project_ui.tick(app, force=True)
        settle(app.research)
        binding = project_ui.selected_binding(app)
        assert binding is not None, app.project_state
        assert app.project_state["binding_origin"] == "automatic"
        assert binding["run_id"] == published_run.run.name
        assert binding["job_id"] == selected.id
        for _ in range(10):
            trace_ui.initialize(app)["next_read"] = 0
            trace_ui.tick(app)
            settle(app.research)
            state = trace_ui.initialize(app)
            if state["result"] is not None and state["result"]["caught_up"]:
                break
        assert state["verified"], state
        assert state["result"]["identity"] == published_run.identity
        assert state["result"]["status"] == "COMPLETE"
        assert trace_ui.run_command(
            app,
            [
                "trace",
                *(["workspace"] if tab == "traces" else []),
                "find",
                "single_thread_scientific_step",
            ],
        )
        if tab == "traces":
            assert state["workspace_result"]["caught_up"]
            assert any(
                row["name"] == "single_thread_scientific_step"
                for row in state["workspace_result"]["intervals"]
            )
            rows, _ = trace_ui.render_global(
                dashboard.views, app.store.snapshot(), app, 80, 60
            )
        else:
            rows, _ = trace_ui.panel(dashboard.views, app, selected, 80)
        text = L.to_text(rows, 80)
        assert "No verified recording is attached yet" not in text
        assert "single_thread_scientific_step" in text
        assert all(L.vlen(L.row_text(row)) <= 80 for row in rows)
    finally:
        trace_ui.shutdown(app)


def test_global_traces_uses_selected_scheduler_job_despite_stale_research_selection(
    dashboard, published_run
):
    app, _ = ordinary_dashboard(
        dashboard, published_run, tab="traces", stale_research=True
    )
    try:
        assert project_ui.tick(app, force=True)
        settle(app.research)
        binding = project_ui.selected_binding(app)
        assert binding is not None, app.project_state
        assert app.project_state["auto_target"] == "900"
        assert binding["job_id"] == "900"
        trace_ui.tick(app)
        settle(app.research)
        assert trace_ui.initialize(app)["verified"]
        assert (
            trace_ui.initialize(app)["result"]["identity"]["run_id"]
            == published_run.run.name
        )
    finally:
        trace_ui.shutdown(app)


@pytest.mark.parametrize("tab", ["jobs", "history", "traces"])
def test_normal_application_loop_publishes_cold_automatic_binding_and_trace(
    dashboard, published_run, tab
):
    app, _ = ordinary_dashboard(
        dashboard, published_run, tab=tab, details=True, managed=True
    )
    try:
        # Exercise the normal feature order and shared background owner, rather
        # than giving project/Trace callbacks priority in a test-only driver.
        for _ in range(12):
            trace_ui.initialize(app)["next_read"] = 0
            app.tick()
            if app.research.pending is not None:
                app.research.future.result(timeout=5)
            state = trace_ui.initialize(app)
            if state["verified"] and state["result"]["caught_up"]:
                break
        binding = project_ui.selected_binding(app)
        assert binding is not None, app.project_state
        assert binding["run_id"] == published_run.run.name
        assert app.project_state["binding_origin"] == "automatic"
        assert state["verified"], state
        assert state["result"]["status"] == "COMPLETE"
        assert state["result"]["identity"]["run_id"] == published_run.run.name
    finally:
        trace_ui.shutdown(app)


@pytest.mark.parametrize("conflict", ["cluster", "restart"])
def test_cold_dashboard_does_not_attach_a_conflicting_scheduler_attempt(
    dashboard, published_run, conflict
):
    app, selected = ordinary_dashboard(
        dashboard, published_run, tab="history", managed=True
    )
    if conflict == "cluster":
        selected.cluster = app.store.details["900"]["Cluster"] = "another-cluster"
    else:
        selected.restarts = 1
        app.store.details["900"]["Restarts"] = "1"
    try:
        assert project_ui.tick(app, force=True)
        settle(app.research)
        assert project_ui.selected_binding(app) is None, app.project_state
        assert app.project_state["auto_status"] != "ready"
        trace_ui.tick(app)
        settle(app.research)
        state = trace_ui.initialize(app)
        assert not state["verified"] and state["result"] is None
        assert state["status"] == "unassociated"
        diagnostic = state["processes"][0]
        assert diagnostic["path"] == str(published_run.manifest)
        if conflict == "cluster":
            assert "another cluster" in diagnostic["reason"]
        else:
            assert diagnostic["events"] > 0
            assert "exact project run is not bound" in diagnostic["reason"]
    finally:
        trace_ui.shutdown(app)


def test_large_recording_catches_up_then_narrow_queries_recover_omitted_calls(tmp_path):
    """The display row bound must not discard or permanently hide raw evidence."""
    calls = 9000
    with Recorder(
        tmp_path,
        job_id="900",
        attempt="large-fixture",
        register=False,
        background_flush=False,
        journal_format="blocks",
        flush_events=256,
    ) as recorder:
        for number in range(calls):
            function = (
                "last_scientific_step" if number == calls - 1 else "scientific_step"
            )
            frame = str(number)
            recorder.emit("call", {"frame": frame, "function": function}, thread="main")
            recorder.emit("return", {"frame": frame}, thread="main")
    source_before = recorder.path.read_bytes()
    manifest = json.loads(source_before)
    journal = recorder.path.parent / manifest["journal"]
    journal_before = hashlib.sha256(journal.read_bytes()).hexdigest()
    assert manifest["state"] == "CLOSED" and manifest["committed_seq"] >= 18_000
    session = WorkspaceSession(
        [recorder.path], index_path=tmp_path / "large.sqlite", light=True
    )
    try:
        result = session.refresh({"mode": "explore", "limit": 2000}, light=True)
        assert not result["caught_up"]
        assert trace_ui._workspace_pending(result)
        assert any("more committed records remain" in row for row in result["warnings"])
        reads = [result["sources"][0]["read_seq"]]
        for _ in range(600):
            if result["caught_up"]:
                break
            result = session.refresh({"mode": "explore", "limit": 2000}, light=True)
            reads.append(result["sources"][0]["read_seq"])
        assert result["caught_up"], result["sources"]
        assert all(before <= after for before, after in pairwise(reads))
        assert reads[-1] > reads[0]
        assert result["sources"][0]["read_seq"] == manifest["committed_seq"]
        assert result["sources"][0]["status"] == "COMPLETE"
        assert not trace_ui._workspace_pending(result)
        assert not any(
            "more committed records remain" in row for row in result["warnings"]
        )
        assert not any("Return without" in row for row in result["warnings"])
        assert result["truncated"] and len(result["intervals"]) == 2000
        assert "last_scientific_step" not in {
            row["name"] for row in result["intervals"]
        }

        searched = session.refresh(
            {"mode": "explore", "search": "last_scientific_step"}, light=True
        )
        (last,) = searched["intervals"]
        assert last["name"] == "last_scientific_step" and not searched["truncated"]
        zoomed = session.refresh(
            {
                "mode": "explore",
                "clock_id": last["clock_id"],
                "start_ns": last["start_ns"],
                "end_ns": last["end_ns"],
            },
            light=True,
        )
        assert any(row["id"] == last["id"] for row in zoomed["intervals"])
        assert not zoomed["truncated"]
        stats = session.index.functions()
        assert not stats["truncated"]
        assert sum(row["count"] for row in stats["functions"]) == calls
        assert recorder.path.read_bytes() == source_before
        assert hashlib.sha256(journal.read_bytes()).hexdigest() == journal_before
    finally:
        session.close()
