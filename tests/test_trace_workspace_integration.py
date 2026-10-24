"""Recording-to-index-to-terminal/CLI paths; no live Slurm/GPU claim."""

from __future__ import annotations

import json
import time
from threading import Event, Lock
from types import SimpleNamespace

import pytest

from raven import layout as L, trace_ui
from raven.model import Finished
from raven.trace_recorder import Recorder
from raven.tracing.workspace_session import WorkspaceSession
from test_job_panels import dashboard
from test_trace_timeline_workflow import connect


def _record(project, **kwargs):
    return Recorder(
        project, job_id="900", attempt="submit/start", background_flush=False, **kwargs
    )


def _drain_session(session, options=None):
    for _ in range(100):
        result = session.refresh(options or {})
        if result["caught_up"]:
            return result
    pytest.fail("Bounded fixture did not finish indexing")


def test_real_recordings_share_run_index_and_terminal_domain_selection(tmp_path):
    with _record(tmp_path) as first:
        first.emit("call", {"function": "alpha", "frame": "a"}, thread="thread-1")
        first.emit("return", {"frame": "a"}, thread="thread-1")
    with _record(tmp_path) as second:
        second.emit("call", {"function": "beta", "frame": "b"}, thread="thread-2")
        second.emit("return", {"frame": "b"}, thread="thread-2")
    session = WorkspaceSession(
        [first.path, second.path], index_path=tmp_path / "run.sqlite"
    )
    try:
        result = _drain_session(session, {"functions": True, "overview": True})
        assert {row["name"] for row in result["intervals"]} == {"alpha", "beta"}
        assert len(result["sources"]) == 2
        assert result["workspace_projection"]["lanes"]
        selected = result["intervals"][0]
        chosen = session.refresh({"selected": selected["id"]})
        assert chosen["selected_detail"]["id"] == selected["id"]
    finally:
        session.close()


def test_live_delta_uses_new_committed_bounds_and_keeps_full_archive(tmp_path):
    with _record(tmp_path) as recorder:
        recorder.emit("call", {"function": "early", "frame": "early"}, thread="main")
        recorder.emit("return", {"frame": "early"}, thread="main")
        recorder.flush()
        session = WorkspaceSession([recorder.path], index_path=tmp_path / "run.sqlite")
        try:
            _drain_session(session)
            time.sleep(0.002)
            recorder.emit("call", {"function": "late", "frame": "late"}, thread="main")
            recorder.emit("return", {"frame": "late"}, thread="main")
            recorder.flush()
            live = _drain_session(session, {"mode": "live", "delta_ns": 1_000_000})
            assert [row["name"] for row in live["intervals"]] == ["late"]
            archive = session.refresh({"mode": "explore"})
            assert {row["name"] for row in archive["intervals"]} == {"early", "late"}
        finally:
            session.close()


def test_global_pane_reads_background_and_survives_completion(dashboard, monkeypatch):
    from raven import background_tasks

    app = connect(dashboard)
    job = dashboard.store.jobs[0]
    job.submit, job.start = "submit", "start"
    root = dashboard.paths[job.id][0].parent
    queued = []
    hub = SimpleNamespace(
        lock=Lock(),
        closed=False,
        pending=None,
        future=None,
        start_task=lambda work, done: queued.append((work, done)) or True,
        cancel_task=lambda callback: None,
    )
    monkeypatch.setattr(background_tasks, "research_hub", lambda *args, **kwargs: hub)
    assert trace_ui.run_command(app, ["traces"])
    assert trace_ui.run_command(app, ["trace", "workspace", "job", job.id])
    state = trace_ui.initialize(app)
    with _record(root) as recorder:
        recorder.emit(
            "call", {"function": "survives_completion", "frame": "a"}, thread="main"
        )
        recorder.emit("return", {"frame": "a"}, thread="main")
        recorder.flush()
        for _ in range(10):
            state["next_read"] = 0
            trace_ui.tick(app)
            work, done = queued.pop(0)
            done(work())
            if (state.get("workspace_result") or {}).get("caught_up"):
                break
        assert state["workspace_result"]["intervals"]
        assert trace_ui.run_command(
            app, ["trace", "workspace", "find", "survives_completion"]
        )
        selected = state["workspace_view"]["selected"]
        text, rows, hits = dashboard.render(width=80, height=45)
        assert "Trace workspace" in text
        assert "survives_completion" in text
        assert all(L.vlen(L.row_text(row)) <= 80 for row in rows)
        assert any(kind == "control" for _, kind, _ in hits)
    dashboard.store.jobs = []
    dashboard.store.finished = [
        Finished(job.id, job.name, "COMPLETED", submit=job.submit, start=job.start)
    ]
    state["next_read"] = 0
    trace_ui.tick(app)
    work, done = queued.pop(0)
    done(work())
    assert state["workspace_view"]["selected"] == selected
    trace_ui.shutdown(app)
    assert state.get("workspace_session") is None


def test_workspace_cli_reads_actual_journal_and_private_index(
    tmp_path, capsys, monkeypatch
):
    from raven.tracing.workspace_cli import workspace
    from raven.execution import policy

    monkeypatch.setattr(policy, "router_for", lambda: SimpleNamespace(managed=False))
    with _record(tmp_path) as recorder:
        recorder.emit("span_start", {"span_id": "s", "name": "cli-span"})
        recorder.emit("span_end", {"span_id": "s"})
    assert (
        workspace(
            [
                str(recorder.path),
                "--index",
                str(tmp_path / "cli.sqlite"),
                "--functions",
                "--overview",
            ]
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert result["caught_up"]
    assert result["intervals"][0]["name"] == "cli-span"
    assert result["functions"]
    assert result["overview"]


def test_corrupt_or_foreign_source_cannot_join_current_attempt(tmp_path):
    with _record(tmp_path) as first:
        first.emit("note", {"name": "known"})
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    with Recorder(
        foreign, job_id="901", attempt="other", background_flush=False
    ) as other:
        other.emit("note", {"name": "foreign"})
    session = WorkspaceSession(
        [first.path, other.path],
        expected={"job_id": "900", "attempt": "submit/start", "cluster": "local"},
        index_path=tmp_path / "guard.sqlite",
    )
    try:
        with pytest.raises(ValueError, match="exact run attempt"):
            session.refresh()
        result = session.index.query()
        assert not any(
            row.get("name") == "foreign" for row in result.get("markers", [])
        )
    finally:
        session.close()


def test_global_open_retained_without_scheduler_record(
    dashboard, monkeypatch, tmp_path
):
    from raven import background_tasks

    app = connect(dashboard)
    app.store.jobs = []
    app.store.finished = []
    queued = []
    hub = SimpleNamespace(
        lock=Lock(),
        closed=False,
        pending=None,
        future=None,
        start_task=lambda work, done: queued.append((work, done)) or True,
        cancel_task=lambda callback: None,
    )
    monkeypatch.setattr(background_tasks, "research_hub", lambda *args, **kwargs: hub)
    with _record(tmp_path) as recorder:
        recorder.emit("call", {"function": "archived", "frame": "a"})
        recorder.emit("return", {"frame": "a"})
    assert trace_ui.run_command(app, ["trace", "workspace", "open", str(recorder.path)])
    state = trace_ui.initialize(app)
    for _ in range(10):
        state["next_read"] = 0
        trace_ui.tick(app)
        work, done = queued.pop(0)
        done(work())
        if (state.get("workspace_result") or {}).get("caught_up"):
            break
    assert state["workspace_result"]["intervals"][0]["name"] == "archived"
    assert not state["verified"]
    assert "scheduler association not checked" in state["status"]
    assert app.selected_id is None
    text, _, _ = dashboard.render(width=80, height=45)
    assert "archived" in text
    trace_ui.shutdown(app)


def test_disposable_cache_keeps_four_slots_and_never_removes_raw(tmp_path):
    from raven.tracing.workspace_cache import open_index

    root = tmp_path / "cache"
    raw = tmp_path / "raw-recording"
    raw.write_text("authoritative")
    active = [
        open_index({}, [str(number)], max_bytes=1 << 20, directory=root)
        for number in range(4)
    ]
    try:
        with pytest.raises(ValueError, match="four local"):
            open_index({}, ["overflow"], max_bytes=1 << 20, directory=root)
        with pytest.raises(ValueError, match="four local"):
            open_index({}, ["0"], max_bytes=1 << 20, directory=root)
    finally:
        for index in active:
            index.close()
    # Independent dashboards may build distinct bounded materializations of
    # the same source, rather than contending for one SQLite writer.
    with open_index({}, ["shared"], max_bytes=1 << 20, directory=root) as first:
        with open_index({}, ["shared"], max_bytes=1 << 20, directory=root) as second:
            assert first.path != second.path
    for number in range(20):
        with open_index({}, [str(number)], max_bytes=1 << 20, directory=root):
            pass
    assert len(list(root.rglob("index.sqlite"))) == 4
    assert len(list(root.rglob("*.lock"))) == 5
    assert raw.read_text() == "authoritative"


def test_disposable_cache_recovers_only_its_own_corrupt_database(tmp_path):
    from raven.tracing.workspace_cache import open_index

    root = tmp_path / "cache"
    with open_index({}, ["source"], max_bytes=1 << 20, directory=root) as index:
        path = index.path
    path.write_bytes(b"broken derived bytes")
    with open_index({}, ["source"], max_bytes=1 << 20, directory=root) as index:
        assert index.cache_rebuilt
        assert index.catalog()["sources"] == []


def test_explicit_browser_ingests_without_terminal_ticks_and_close_stops_owned_reader(
    tmp_path,
):
    with _record(tmp_path) as recorder:
        recorder.emit("call", {"function": "before", "frame": "a"})
        recorder.emit("return", {"frame": "a"})
        recorder.flush()
        session = WorkspaceSession([recorder.path], index_path=tmp_path / "live.sqlite")
        try:
            _drain_session(session)
            viewer = session.open_browser()
            assert viewer.url.startswith("http://127.0.0.1:")
            reader = session._browser_reader
            assert reader.is_alive()
            recorder.emit("call", {"function": "after", "frame": "b"})
            recorder.emit("return", {"frame": "b"})
            recorder.flush()
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                if any(
                    row["name"] == "after" for row in session.index.query()["intervals"]
                ):
                    break
                time.sleep(0.02)
            else:
                pytest.fail("Explicit viewer did not ingest new committed events")
        finally:
            session.close()
        assert not reader.is_alive()
        recorder.emit("note", {"name": "science-continues"})
        recorder.flush()
        from raven.tracing.protocol import TraceReader

        final = TraceReader(recorder.path).refresh()
        assert any(
            row["data"].get("name") == "science-continues" for row in final["events"]
        )


def test_same_run_filter_change_rejects_old_background_view(dashboard, monkeypatch):
    from raven import background_tasks

    app = connect(dashboard)
    job = app.store.jobs[0]
    job.submit, job.start = "submit", "start"
    root = dashboard.paths[job.id][0].parent
    queued = []
    hub = SimpleNamespace(
        lock=Lock(),
        closed=False,
        pending=None,
        future=None,
        start_task=lambda work, done: queued.append((work, done)) or True,
        cancel_task=lambda callback: None,
    )
    monkeypatch.setattr(background_tasks, "research_hub", lambda *args, **kwargs: hub)
    trace_ui.run_command(app, ["traces"])
    with _record(root) as recorder:
        for name in ("early", "late"):
            recorder.emit("call", {"function": name, "frame": name})
            recorder.emit("return", {"frame": name})
        recorder.flush()
        trace_ui.tick(app)
        work, done = queued.pop(0)
        old_result = work()
        trace_ui.run_command(app, ["trace", "workspace", "find", "late"])
        done(old_result)
        state = trace_ui.initialize(app)
        assert state.get("workspace_result") is None
        # Its verified index remains useful, while the obsolete view is rejected.
        assert state["workspace_session"] is old_result["workspace_session"]
        trace_ui.tick(app)
        work, done = queued.pop(0)
        done(work())
        assert [row["name"] for row in state["workspace_result"]["intervals"]] == [
            "late"
        ]
    trace_ui.shutdown(app)


def test_cli_resource_context_and_samples_reach_actual_index(
    tmp_path, capsys, monkeypatch
):
    from raven.tracing.workspace_cli import workspace
    from raven.execution import policy

    monkeypatch.setattr(policy, "router_for", lambda: SimpleNamespace(managed=False))
    with _record(tmp_path, initial_mono_ns=0) as recorder:
        recorder.emit(
            "activity",
            dict(
                activity_kind="scheduling",
                name="running",
                state="running",
                logical_cpu=1,
                socket_id=0,
                core_id=1,
                start_ns=100,
                end_ns=150,
            ),
            mono_ns=100,
        )
        recorder.emit(
            "cpu_sample",
            dict(
                frames=[dict(name="solve")],
                weight=10,
                weight_unit="event-count",
                event="cycles",
            ),
            mono_ns=120,
        )
    session = WorkspaceSession([recorder.path], index_path=tmp_path / "context.sqlite")
    try:
        result = _drain_session(session, {"mode": "explore"})
        source = result["sources"][0]
        clock = source["clock"]["id"]
    finally:
        session.close()
    context = dict(
        schema="raven.trace-resource-context/v1",
        identity=dict(cluster="local", job_id="900", attempt="submit/start"),
        provenance="Explicit test allocation/coverage declaration, not live scheduler evidence",
        epochs=[
            dict(
                clock_id=clock,
                start_ns=100,
                end_ns=200,
                logical_cpu_count=2,
                unit="logical-cpu",
                coverage_complete=True,
                gpu_coverage_complete=False,
                source_ids=[source["id"]],
                provenance="Fixture scope",
            )
        ],
    )
    path = tmp_path / "capacity.json"
    path.write_text(json.dumps(context))
    assert (
        workspace(
            [
                str(recorder.path),
                "--index",
                str(tmp_path / "cli-context.sqlite"),
                "--capacity",
                str(path),
                "--start-ns",
                "100",
                "--end-ns",
                "200",
                "--metrics",
                "--samples",
            ]
        )
        == 0
    )
    response = json.loads(capsys.readouterr().out)
    metric = next(
        row
        for row in response["metrics"]["metrics"]
        if row["name"] == "cpu_allocation_utilization"
    )
    assert metric["denominator"] == 200 and metric["numerator"] == 50
    assert metric["value"] == pytest.approx(0.25)
    assert response["samples"]["groups"][0]["name"] == "solve"
    assert response["samples"]["groups"][0]["inclusive_share"] == 1


def test_global_completed_trace_requests_metadata_even_when_details_were_closed(
    dashboard, monkeypatch
):
    from raven import controller

    app = connect(dashboard)
    job = app.store.jobs[0]
    app.store.jobs = []
    app.store.finished = [Finished(job.id, job.name, "COMPLETED")]
    app.tab, app.mode, app.selected_id = "traces", "main", job.id
    app.job_panel_state["mode"] = "off"
    app.research = None
    selected = []
    app.sampler = SimpleNamespace(
        select=lambda jid: None,
        select_fin=selected.append,
        select_trace=lambda jid: None,
    )
    monkeypatch.setattr(controller.workbench, "tick", lambda app: None)
    app.tick()
    assert selected == [job.id]


def test_source_inbox_keeps_full_error_and_verified_recovery(dashboard):
    from raven import notifications

    app = connect(dashboard)
    context = trace_ui._context(app)
    error = "Temporary GPU report read failure\n" + "details " * 1000
    result = dict(
        sources=[dict(id="cpu", error=""), dict(id="gpu", error=error)],
        intervals=[dict(name="available CPU call")],
    )
    trace_ui._workspace_source_notices(app, context, result)
    trace_ui._workspace_source_notices(app, context, result)
    inbox = notifications.sync(app, {})
    notice = next(item for item in inbox.notices if item.source == "Trace source gpu")
    assert notice.text == error and notice.occurrences == 1
    assert not notice.resolved_at
    assert result["intervals"] == [dict(name="available CPU call")]
    result["sources"][1]["error"] = ""
    trace_ui._workspace_source_notices(app, context, result)
    recovered = notifications.initialize(app).get(notice.id)
    assert recovered.resolved_at and "verified" in recovered.resolution
