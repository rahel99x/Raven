"""Real local recording to visual inspection, without live scheduler claims."""
from __future__ import annotations

import json
import re
from threading import Event, Lock
from types import SimpleNamespace

from raven import job_panels, layout as L, trace_browser, trace_ui
from raven.model import Finished
from raven.slurm import FakeBackend, Slurm
from raven.trace_recorder import Recorder
from test_job_panels import dashboard
from test_trace_lab_example import _launch_example


def connect(dashboard):
    dashboard.app.actions = SimpleNamespace(slurm=Slurm(FakeBackend(), "test"))
    dashboard.render()
    job_panels._activate(dashboard.app, "trace")
    return dashboard.app


def publish(app, context, result):
    state = trace_ui.initialize(app)
    state.update(context=context, result=result["snapshot"], reader=result["reader"],
                 path=result["path"], sources=result["sources"], processes=result["processes"],
                 verified=result["verified"], status="ready", index=result["index"], paint=None)
    return state


def test_cpu_example_flows_from_verified_attempt_to_selected_frame_and_offline_html(dashboard, tmp_path):
    result, receipt, output = _launch_example(tmp_path)
    assert result.returncode == 0, (result.stdout, result.stderr)
    summary = json.loads((output / "summary.json").read_text())
    assert summary["checks"]["heat_conserved"] and summary["checks"]["maximum_principle"]
    app = connect(dashboard)
    job = Finished("local", "Recorded heat example", "COMPLETED", submit=receipt["run_id"])
    dashboard.store.jobs = []
    dashboard.store.finished = [job]
    dashboard.store.details[job.id] = {"WorkDir": receipt["project_root"]}
    app.selected_id = job.id
    app.tab = "history"
    context = trace_ui._context(app)
    read_context = dict(context, run_root=receipt["run_dir"])
    loaded = trace_ui._read(read_context, "", 0, "", None, Event())
    for _ in range(32):
        assert loaded["verified"]
        if loaded["snapshot"]["caught_up"]:
            break
        loaded = trace_ui._read(read_context, "", loaded["index"], loaded["path"],
                                loaded["reader"], Event(), loaded["sources"], loaded["processes"])
    snapshot = loaded["snapshot"]
    assert snapshot["caught_up"] and snapshot["status"] == "COMPLETE"
    assert snapshot["identity"]["pid"] == summary["parent_pid"]
    assert snapshot["identity"]["attempt"] == receipt["run_id"]
    model = snapshot["timeline"]
    assert {"evolve", "advance_step", "update_row", "write_results"} <= {row["name"] for row in model["intervals"]}
    state = publish(app, context, loaded)
    assert trace_ui.run_command(app, ["trace", "find", "advance_step"])
    selected = snapshot["timeline_index"]["by_id"][state["timeline_view"]["selected"]]
    assert selected["name"] == "advance_step"
    assert selected["duration_ns"] >= 0
    rows, hits = trace_ui.panel(dashboard.views, app, job, 80)
    text = L.to_text(rows, 80)
    assert "Selected: advance_step" in text
    assert "heat_lab.py" in text and "wall time" in text
    assert all(L.vlen(L.row_text(row)) <= 80 for row in rows)
    assert any(kind == "job_panel_action" and "trace frame " in str(value) for _, kind, value in hits)

    destination = tmp_path / "heat-timeline.html"
    exported = trace_browser.export(snapshot, destination)
    document = destination.read_text()
    payload = re.search(r'<script id="trace-data" type="application/json">(.*?)</script>', document, re.S)
    assert payload is not None
    browser = json.loads(payload.group(1))
    assert browser["identity"] == model["identity"]
    assert browser["sha256"] == snapshot["sha256"]
    assert browser["read_seq"] == snapshot["read_seq"]
    assert exported["source_status"] == browser["status"] == "COMPLETE"
    browser_selected = next(row for row in browser["intervals"] if row["id"] == selected["id"])
    assert int(browser_selected["duration_ns"]) == selected["duration_ns"]
    assert browser_selected["first_seq"] == selected["first_seq"]
    assert browser_selected["last_seq"] == selected["last_seq"]
    assert destination.stat().st_mode & 0o777 == 0o600


def test_live_pause_follow_and_completion_preserve_selected_verified_interval(dashboard, monkeypatch):
    app = connect(dashboard)
    job = dashboard.store.jobs[0]
    job.submit, job.start = "2026-10-18T10:00:00", "2026-10-18T10:01:00"
    root = dashboard.paths[job.id][0].parent
    queued = []
    hub = SimpleNamespace(lock=Lock(), closed=False, pending=None, future=None,
        start_task=lambda work, done: queued.append((work, done)) or True, cancel_task=lambda callback: None)
    from raven import background_tasks
    monkeypatch.setattr(background_tasks, "research_hub", lambda *args, **kwargs: hub)

    def refresh():
        state = trace_ui.initialize(app)
        state["next_read"] = 0
        trace_ui.tick(app)
        assert len(queued) == 1
        work, done = queued.pop()
        done(work())
        assert state["verified"] and state["result"]["caught_up"]
        return state

    with Recorder(root, job_id=job.id, attempt=job.submit + "/" + job.start, background_flush=False) as recorder:
        recorder.emit("call", {"function": "outer_work", "frame": "outer"})
        recorder.emit("call", {"function": "inner_work", "frame": "inner"})
        recorder.emit("return", {"function": "inner_work", "frame": "inner"})
        recorder.flush()
        state = refresh()
        assert state["result"]["status"] == "LIVE"
        assert trace_ui.run_command(app, ["trace", "find", "inner_work"])
        selected = state["timeline_view"]["selected"]
        window = state["timeline_view"]["window"]
        frozen = state["result"]
        assert trace_ui.run_command(app, ["trace", "pause"])
        recorder.emit("return", {"function": "outer_work", "frame": "outer"})
        recorder.emit("call", {"function": "later_work", "frame": "later"})
        recorder.emit("return", {"function": "later_work", "frame": "later"})
        recorder.flush()
        state["next_read"] = 0
        trace_ui.tick(app)
        assert not queued and state["result"] is frozen
        assert recorder.committed_seq > frozen["read_seq"]
        assert trace_ui.run_command(app, ["trace", "follow"])
        state = refresh()
        assert state["result"]["read_seq"] == recorder.committed_seq
        assert state["timeline_view"]["selected"] == selected
        assert state["timeline_view"]["window"] == window
        assert {"outer_work", "inner_work", "later_work"} <= {row["name"] for row in state["result"]["timeline"]["intervals"]}

    state = refresh()
    assert state["result"]["status"] == "COMPLETE"
    completed = Finished(job.id, job.name, "COMPLETED", submit=job.submit, start=job.start)
    dashboard.store.jobs = []
    dashboard.store.finished = [completed]
    app.tab = "history"
    rows, _ = trace_ui.panel(dashboard.views, app, completed, 80)
    assert "Selected: inner_work" in L.to_text(rows, 80)
    assert state["timeline_view"]["selected"] == selected
    assert state["timeline_view"]["window"] == window

    completed.submit = "2026-10-19T10:00:00"
    rows, _ = trace_ui.panel(dashboard.views, app, completed, 80)
    text = L.to_text(rows, 80)
    assert "waiting for job evidence" in text
    assert "inner_work" not in text and "later_work" not in text
