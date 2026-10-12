"""Planning redraws are cache-only, and obsolete workers cannot publish choices."""
from __future__ import annotations

import builtins
import json
import os
from pathlib import Path
import shlex
import threading
import time
from types import SimpleNamespace

import pytest

from raven import cli, planning, planning_io
from raven.config import Config
from raven.metrics import write_metric
from raven.model import Finished, Job
from raven.provenance import capture
from raven.research import RESEARCH_VIEWS, ResearchHub
from raven.submission import prepare


def context(hub, view="tradeoffs", jid="1", *, snap=None, job=None):
    return {"view": view, "jid": jid, "job": job, "snap": snap or {},
            "settings": dict(hub.settings), "generation": hub.generation}


def finish_command(app):
    if app.research.pending:
        app.research.pending[0].result(timeout=5)
        app.research.poll_task()


def test_obsolete_planning_read_cannot_repopulate_source_or_candidate_choices(monkeypatch):
    hub = ResearchHub({"research": {"planning_file": "old.json"}})
    entered, release = threading.Event(), threading.Event()
    def load(path, **kwargs):
        if path == "old.json":
            entered.set()
            assert release.wait(3)
        return {"marker": path}
    monkeypatch.setattr(planning_io, "load_json", load)
    monkeypatch.setattr(planning, "analyze", lambda view, source, **kwargs: {"status": "ok", "marker": source["marker"]})
    try:
        hub.planning_choices = {"status": "ok", "marker": "earlier file"}
        hub.request(context(hub))
        assert entered.wait(1)
        old_worker = hub.future
        hub.configure(planning_file="new.json")
        assert hub.planning_choices is None
        release.set()
        old_worker.result(timeout=3)
        assert hub.planning_source is None
        assert hub.planning_choices is None
        assert not hub.cache
        result = hub.request(context(hub), wait=True)
        assert result["marker"] == "new.json"
        assert hub.planning_choices["marker"] == "new.json"
    finally:
        release.set()
        hub.close()


def test_closed_planning_reader_cannot_publish_side_state(monkeypatch):
    hub = ResearchHub({"research": {"planning_file": "observations.json"}})
    entered, release = threading.Event(), threading.Event()
    def load(path, **kwargs):
        entered.set()
        assert release.wait(3)
        return {"marker": path}
    monkeypatch.setattr(planning_io, "load_json", load)
    monkeypatch.setattr(planning, "analyze", lambda view, source, **kwargs: {"status": "ok", "marker": source["marker"]})
    try:
        hub.request(context(hub))
        assert entered.wait(1)
        worker = hub.future
        hub.close()
        release.set()
        worker.result(timeout=3)
        assert hub.planning_source is None and hub.planning_choices is None
        assert not hub.cache
    finally:
        release.set()
        hub.close()


def test_source_cache_does_not_bypass_a_stricter_view_read_budget(tmp_path):
    path = tmp_path / "large bundle.json"
    path.write_text(json.dumps({"padding": "x" * (1048576 + 100), "scaling": [],
                                "workflow": {"kind": "raven.workflow", "version": 1,
                                             "nodes": [{"id": "task", "runtime_seconds": 1}]}}))
    hub = ResearchHub({"research": {"planning_file": str(path)}})
    try:
        wide = hub.request(context(hub, "scaling"), wait=True)
        assert wide["status"] != "error"
        narrow = hub.request(context(hub, "workflow"), wait=True)
        assert narrow["status"] == "error"
        assert "1048576-byte read limit" in narrow["summary"]
    finally:
        hub.close()


def test_background_frame_and_explicit_command_share_one_bounded_worker():
    hub = ResearchHub({})
    frame_entered, frame_release = threading.Event(), threading.Event()
    command_entered, command_release = threading.Event(), threading.Event()
    workers, completions = [], []
    main_thread = threading.get_ident()
    def read(ctx):
        workers.append(("frame", threading.get_ident()))
        frame_entered.set()
        assert frame_release.wait(3)
        return {"status": "ok"}
    def command():
        workers.append(("command", threading.get_ident()))
        command_entered.set()
        assert command_release.wait(3)
        return {"status": "ok", "prepared": True}
    hub._read = read
    try:
        hub.request(context(hub, "arrays"))
        assert frame_entered.wait(1)
        frame_worker = hub.future
        assert hub.start_task(command, lambda value: completions.append((threading.get_ident(), value)))
        command_worker = hub.future
        for i in range(300):
            hub.request(context(hub, "forecast", jid=str(i)), force=True)
            assert not hub.start_task(lambda: pytest.fail("unbounded command backlog"), lambda value: None)
        assert len(workers) == 1
        assert hub.pool._work_queue.qsize() <= 1
        frame_release.set()
        frame_worker.result(timeout=3)
        assert command_entered.wait(1)
        command_release.set()
        command_worker.result(timeout=3)
        assert completions == []
        hub.poll_task()
        assert completions == [(main_thread, {"status": "ok", "prepared": True})]
        assert len({tid for _, tid in workers}) == 1
        assert hub.pending is None
    finally:
        frame_release.set()
        command_release.set()
        hub.close()


def test_non_job_views_reuse_a_single_cache_entry_across_job_selection_changes():
    hub = ResearchHub({"research": {"interval": 86400}})
    calls = []
    hub._read = lambda ctx: calls.append((ctx["view"], ctx["jid"])) or {"status": "ok"}
    try:
        for view in ("arrays", "artifacts", "passport", "submit", "scaling", "workflow"):
            for i in range(30):
                hub.request(context(hub, view, str(i)), wait=True)
        assert len(calls) == 6 and len(hub.cache) == 6
        for i in range(40):
            hub.request(context(hub, "forecast", str(i)), wait=True)
            assert len(hub.cache) <= 16
    finally:
        hub.close()


@pytest.fixture
def cached_dashboard(tmp_path, monkeypatch):
    for name in os.environ:
        if name.startswith("SBATCH_"):
            monkeypatch.delenv(name)
    metrics = tmp_path / "metrics.jsonl"
    for i in range(8):
        write_metric(metrics, {"loss": 1 / (i + 1)}, t=100 + i, step=i)
    output = tmp_path / "result.json"
    output.write_text('{"score":0.8}')
    contract = tmp_path / "contract.json"
    contract.write_text(json.dumps({"version": 1, "outputs": [{"path": "result.json", "format": "json", "required_keys": ["score"]}]}))
    script = tmp_path / "job.sh"
    script.write_text("#!/bin/bash\n#SBATCH --time=00:30:00\ntrue\n")
    stdout = tmp_path / "job.out"
    stdout.write_text("progress\n")
    selected = Job("901", "science", "main", "PENDING", cpus=4, mem_req="8Gn", limit="01:00:00", reason="Resources")
    bundle = planning.demo_source(selected)
    bundle["jobs"] = [vars(selected)]
    source = tmp_path / "planning.json"
    source.write_text(json.dumps(bundle, allow_nan=False))
    session = cli.build(cli.parse(["--fake", "--no-state", "--no-plugins", "--unicode"]), Config())
    app, store, hub = session.app, session.store, session.app.research
    app.tab = "research"
    app.research_job_id = selected.id
    store.jobs = [selected, Job("900_[0-99]", "array", "main", "PENDING")]
    store.finished = [Finished("900_3", "array", "FAILED", elapsed="00:00:10", exit="1:0")]
    store.details[selected.id] = {"StdOut": str(stdout), "StdErr": str(stdout)}
    hub.configure(metrics_file=str(metrics), contract=str(contract), workdir=str(tmp_path), planning_file=str(source))
    hub.interval = 86400
    hub.passport = capture(tmp_path, script=script)
    hub.plan = prepare(script, workdir=tmp_path)
    try:
        for view, _ in RESEARCH_VIEWS:
            app.research_view = view
            result = hub.request(hub.context(store.snapshot(), app), wait=True)
            assert result["status"] not in {"error", "loading", "empty"}, (view, result)
        yield session
    finally:
        session.close()


@pytest.mark.parametrize("width,height", [(40, 16), (160, 40)])
def test_cached_twelve_view_redraw_performs_no_io_or_scheduler_queries(cached_dashboard, monkeypatch, width, height):
    session = cached_dashboard
    app, hub = session.app, session.app.research
    calls = []
    def forbidden(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("cached redraw attempted filesystem or scheduler access")
    original_worker = hub.future
    started = time.monotonic()
    with monkeypatch.context() as scoped:
        scoped.setattr(builtins, "open", forbidden)
        scoped.setattr(os, "open", forbidden)
        scoped.setattr(os, "stat", forbidden)
        scoped.setattr(os, "scandir", forbidden)
        scoped.setattr(Path, "open", forbidden)
        scoped.setattr(session.backend, "run", forbidden)
        scoped.setattr(session.backend, "call", forbidden)
        scoped.setattr(session.app.research.slurm, "details", forbidden)
        for _ in range(10):
            for view, _ in RESEARCH_VIEWS:
                app.research_view = view
                app.tick()
                rows, _ = session.views.compose(session.store.snapshot(), app, width, height, session.actions)
                assert len(rows) == height
    assert not calls
    assert hub.future is original_worker
    assert len(hub.cache) == len(RESEARCH_VIEWS)
    # A generous ceiling catches accidental blocking waits without enforcing
    # fragile microbenchmarks on a shared CI machine.
    assert time.monotonic() - started < 10


def test_palette_refresh_reloads_file_and_preserves_scientific_options(cached_dashboard):
    session = cached_dashboard
    app, hub = session.app, session.app.research
    path = Path(hub.settings["planning_file"])
    app.run_command(shlex.join(["predict", "--file", str(path), "--cpus", "8", "--coverage", "0.75"]))
    finish_command(app)
    assert app.command_ok
    initial = app.research_result
    assert initial["cohort"]["cpus"] == 8 and initial["target_coverage"] == .75
    initial_samples = initial["metrics"]["runtime_seconds"]["samples"]
    source = json.loads(path.read_text())
    extra = dict(next(row for row in source["history"] if row["cpus"] == 8), id="new-independent-run")
    extra["end"] += 1
    source["history"].append(extra)
    path.write_text(json.dumps(source, allow_nan=False))
    old_generation = hub.generation
    app.run_command("refresh")
    assert hub.generation == old_generation + 1
    assert hub.settings["planning_overrides"]["predict"]["query"]["cpus"] == 8
    refreshed = hub.request(hub.context(session.store.snapshot(), app), wait=True)
    assert refreshed["cohort"]["cpus"] == 8 and refreshed["target_coverage"] == .75
    assert refreshed["metrics"]["runtime_seconds"]["samples"] == initial_samples + 1


def test_command_file_change_adopts_new_file_job_before_background_refresh(cached_dashboard, tmp_path):
    session = cached_dashboard
    app, hub = session.app, session.app.research
    path = tmp_path / "second workspace.json"
    path.write_text(json.dumps({"jobs": [{"id": "777", "name": "new work", "partition": "main", "state": "PENDING",
                                          "cpus": 4, "nodes": 1, "gpus": 0, "reason": "JobHeldUser"}]}))
    app.research_job_id = "901"
    app.run_command(shlex.join(["forecast", "--file", str(path)]))
    finish_command(app)
    assert app.research_result["job_id"] == "777"
    assert app.research_job_id == "777"
    refreshed = hub.request(hub.context(session.store.snapshot(), app), wait=True, force=True)
    assert refreshed["job_id"] == "777" and refreshed["status"] == "blocked"


def test_async_command_cannot_cache_original_job_under_new_selection(cached_dashboard, monkeypatch):
    from raven import planning_commands
    session = cached_dashboard
    app, hub = session.app, session.app.research
    entered, release = threading.Event(), threading.Event()
    real_result = planning_commands.result
    def delayed(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return real_result(*args, **kwargs)
    monkeypatch.setattr(planning_commands, "result", delayed)
    app.interactive = True
    try:
        app.research_job_id = "901"
        app.run_command(shlex.join(["forecast", "--file", hub.settings["planning_file"]]))
        assert entered.wait(1)
        app.research_job_id = "900_3"
        release.set()
        hub.future.result(timeout=3)
        hub.poll_task()
        assert app.research_result["job_id"] == app.research_job_id == "901"
        cached = hub.current(hub.context(session.store.snapshot(), app))
        assert cached["job_id"] == "901"
        assert all(key[2] != "900_3" for key in hub.cache)
    finally:
        release.set()


def test_imported_file_jobs_are_available_to_arrows_immediately_after_command(cached_dashboard, tmp_path):
    session = cached_dashboard
    app, hub = session.app, session.app.research
    path = tmp_path / "two imported jobs.json"
    path.write_text(json.dumps({"jobs": [
        {"id": "777", "name": "first", "partition": "main", "state": "PENDING", "cpus": 4, "nodes": 1, "gpus": 0},
        {"id": "778", "name": "second", "partition": "main", "state": "PENDING", "cpus": 4, "nodes": 1, "gpus": 0}]}))
    app.run_command(shlex.join(["forecast", "--file", str(path)]))
    finish_command(app)
    session.views.compose(session.store.snapshot(), app, 104, 32, session.actions)
    assert [row["id"] for row in app.research_data_jobs] == ["777", "778"]
    app.handle("down")
    assert app.research_job_id == "778"
    refreshed = hub.request(hub.context(session.store.snapshot(), app), wait=True)
    assert refreshed["job_id"] == "778"
