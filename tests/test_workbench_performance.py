"""Structural redraw budgets: pane fitting must not multiply expensive source work."""
from collections import Counter
import builtins
import importlib
import math
import os
from pathlib import Path
import time

import pytest

from raven import analysis_ui, layout as L, log_workbench, workbench
from raven.config import Config
from raven.controller import App
from raven.model import Job, Live, Store
from raven.research import ResearchHub
from raven.slurm import FakeBackend, Slurm
from raven.views import Views


@pytest.fixture
def dashboard():
    cfg = Config({"log_lines": 0})
    store = Store(persist=False)
    store.apply_jobs([Job(str(i + 1), f"training-{i}", "gpu", "PENDING" if i % 4 == 0 else "RUNNING",
                          cpus=8, mem_req="8G", elapsed="00:10:00", limit="01:00:00") for i in range(1000)])
    for job in store.jobs:
        if not job.pending:
            store.live[job.id] = Live(rate=.5, avg=.4, rss=1 << 30, cpu_time=300)
    backend = FakeBackend("bench", speed=0)
    slurm = Slurm(backend, "bench")
    app = App(store, None, None, cfg, "bench")
    views = Views(L.Glyphs(False), cfg)
    app.views_ref = views
    app.research = ResearchHub(cfg, slurm=slurm)
    assert app.layout_state.density == "comfortable"
    try:
        yield app, views, store, backend
    finally:
        app.research.close()


@pytest.mark.parametrize("width,height", [(160, 40), (80, 24)])
def test_pane_refits_prepare_each_job_only_once_per_frame(dashboard, monkeypatch, width, height):
    app, views, store, _ = dashboard
    prepared = Counter()

    def inspect(job, snap, controller):
        prepared[job.id] += 1
        return []

    # Plugin cell preparation happens once per job-row build, independently of
    # header aggregates. Count actual work rather than timing a shared CI host.
    monkeypatch.setattr(views, "plugin_flags", inspect)
    snap = store.snapshot()
    for _ in range(2):
        prepared.clear()
        rows, hits = views.compose(snap, app, width, height)
        assert prepared == Counter({job.id: 1 for job in store.jobs})
        assert len(rows) == height
        assert all(L.vlen(L.row_text(row)) <= width for row in rows)
        assert all(0 <= y < height - 1 for y, _, _ in hits)


@pytest.mark.parametrize("width,height", [(0, 0), (0, 24), (1, 1), (20, 3), (80, 3)])
def test_empty_body_geometry_does_not_prepare_hidden_job_rows(dashboard, monkeypatch, width, height):
    app, views, store, _ = dashboard
    calls = []
    original = views.job_rows

    def prepare(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(views, "job_rows", prepare)
    rows, hits = views.compose(store.snapshot(), app, width, height)
    assert not calls
    assert len(rows) == height
    assert all(L.vlen(L.row_text(row)) <= width for row in rows)
    assert not hits


def test_warm_workbench_dispatch_reuses_imported_feature_modules(dashboard, monkeypatch):
    app, views, store, _ = dashboard
    loaded = workbench.modules()
    assert len(loaded) == len(workbench.FEATURES)

    def import_again(*args, **kwargs):
        raise AssertionError("warm input dispatch attempted another module import")

    monkeypatch.setattr(importlib, "import_module", import_again)
    snap = store.snapshot()
    for _ in range(25):
        assert workbench.modules() is loaded
        assert "dashboard" in workbench.command_names()
        assert not workbench.handle_key(app, "unbound-benchmark-key")
        assert not workbench.run_command(app, ["unbound-benchmark-command"])
        assert workbench.overlay(views, snap, app, 160, 40) is None


@pytest.mark.parametrize("width,height", [(160, 40), (80, 24)])
def test_cached_large_metrics_only_rasterize_visible_cards_without_io(dashboard, monkeypatch, width, height):
    app, views, store, backend = dashboard
    app.tab, app.research_view, app.research_job_id = "research", "experiment", "1"
    points = [{"t": i, "value": math.sin(i / 100), "step": i} for i in range(10_000)]
    result = {"status": "ok", "records": 10_000, "path": "published metrics snapshot",
              "series": {f"metric_{i:02d}": points for i in range(64)}}
    snap = store.snapshot()
    hub = app.research
    hub.interval = 86_400
    context = hub.context(snap, app)
    hub.cache[hub._key(context)] = (time.monotonic(), result)
    views.compose(snap, app, width, height)  # Warm feature imports and display state.
    rasterized = []
    chart_rows = analysis_ui.chart_rows

    def draw(*args, **kwargs):
        rasterized.append(args[5])
        return chart_rows(*args, **kwargs)

    io_calls = []

    def forbidden(*args, **kwargs):
        io_calls.append(args)
        raise AssertionError("published redraw attempted filesystem, scheduler, or worker I/O")

    monkeypatch.setattr(analysis_ui, "chart_rows", draw)
    with monkeypatch.context() as scope:
        for obj, name in ((builtins, "open"), (os, "stat"), (os, "scandir"), (Path, "open"),
                          (backend, "run"), (backend, "call"), (hub.pool, "submit")):
            scope.setattr(obj, name, forbidden)
        for at_end in (False, True):
            rasterized.clear()
            app.research_scroll = 1_000_000 if at_end else 0
            rows, hits = views.compose(snap, app, width, height)
            # Each normal card occupies nine document rows. Allow partial cards
            # on both edges, without permitting the 256-row workspace source to
            # rasterize dozens of offscreen plots at each pane width.
            assert 0 < len(rasterized) <= height // 9 + 2
            assert len(set(rasterized)) == len(rasterized)
            assert app.research_rows >= 64 * 9
            assert len(rows) == height
            assert all(L.vlen(L.row_text(row)) <= width for row in rows)
            assert all(0 <= y < height - 1 for y, _, _ in hits)
            if at_end:
                assert "metric_63" in rasterized
    assert not io_calls
    assert len(app.analysis_result["series"]) == 64


def test_cached_log_catalog_does_not_probe_guessed_scheduler_paths(dashboard, monkeypatch):
    app, views, store, _ = dashboard
    job = store.jobs[1]
    app.tab, app.log_job, app.log_record = "log", job.id, job
    app.logs.entries = [{"id": "known", "path": "/published/application.log", "label": "application",
                         "group": "Application", "source": "run index"}]
    app.log_workbench_state["preview"] = False
    probes = []

    def exists(path):
        probes.append(path)
        return False

    monkeypatch.setattr(os.path, "exists", exists)
    for _ in range(10):
        rows, hits = log_workbench.render_browser(views, store.snapshot(), app, 100, 24, [], [])
        assert rows
    assert not probes, "Rendering must use published paths; fallback discovery belongs in the reader"
