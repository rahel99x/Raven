"""A published run's progress belongs to its job, even after selection moves."""
from __future__ import annotations

from dataclasses import replace

import pytest

from raven import job_progress as P, layout as L, project_ui, table_sort
from raven.config import Config
from raven.controller import App
from raven.model import Finished, Job, Store, stamp
from raven.research import ResearchHub
from raven.views import Views


@pytest.fixture
def linked(tmp_path):
    cfg = Config({"animations": False, "startup_animation": False, "log_lines": 0})
    store = Store(persist=False)
    start, submit = "2026-10-08T10:00:00", "2026-10-08T09:00:00"
    store.jobs = [Job("7", "linked", "cpu", "RUNNING", elapsed="00:20:00", limit="01:00:00",
                      start=start, submit=submit), Job("8", "next", "cpu", "RUNNING")]
    app = App(store, None, None, cfg, "reader")
    app.research = ResearchHub(cfg)
    app.views_ref = Views(L.Glyphs(False), cfg)
    app.job_panel_state["mode"] = "off"
    root, run_id = str(tmp_path), "run7"
    inventory = {"schema": "raven.run/v1", "run_id": run_id, "experiment_id": "example",
                 "attempt": 1, "state": "RUNNING", "job_id": "7", "paths": {"metrics": "metrics.jsonl"},
                 "start": stamp(start), "submit": stamp(submit)}
    project_ui.initialize(app).update(root=root, runs=[{"run_id": run_id, "job_id": "7", "inventory": inventory}])
    binding = dict(project_root=root, run_root=root + "/runs/" + run_id, run_id=run_id,
                   job_id="7", attempt=1, state="RUNNING", metrics_file=root + "/runs/" + run_id + "/metrics.jsonl",
                   contract="", passport="", log_manifest="", stdout="", stderr="")
    project_ui._apply_run(app, {"binding": binding, "logs": [], "warnings": []}, automatic=True)
    app.selected_id = "7"
    app.table_state["groups"] = False
    table_sort.set_sort(app, "jobs", "id", "asc")
    P.initialize(app)
    yield app, store, inventory, binding
    app.research.close()


def publish(app, fraction=.3, **changes):
    data = {"status": "ok", "job_id": "7", "last_t": stamp("2026-10-08T10:10:00"),
            "progress": {"completed": fraction * 100, "total": 100, "unit": "steps"}}
    data.update(changes)
    app.research.cache[(app.research.generation, "experiment", "7")] = (0, data)


def sources(app, store):
    return P.published(app, {"jobs": store.jobs, "finished": store.finished})


@pytest.mark.parametrize("ascii_", [False, True])
def test_mark_auto_advance_preserves_exact_far_left_progress(linked, ascii_):
    app, store, _, _ = linked
    app.views_ref.set_ascii(ascii_)
    publish(app)
    before, hits = app.views_ref.compose(store.snapshot(), app, 120, 30)
    header = next(value for _, kind, value in hits if kind == "sort_header" and value[:2] == ("jobs", "progress"))
    y = next(y for y, kind, jid in hits if kind == "job" and jid == "7")
    expected = P.Observation(.3, "reported").format(ascii_)
    assert L.row_text(before[y])[header[2]:header[3]] == expected
    assert len(expected) == 6 and header[2] < next(value[2] for _, kind, value in hits if kind == "sort_header" and value[:2] == ("jobs", "id"))
    generation = app.research.generation
    app.jobs_selection_options = app.jobs_options()
    app.handle("space")
    assert app.marks == {"7"} and app.selected_id == "8"
    project_ui.tick(app, {"jobs": store.jobs, "details": {}})
    assert app.project_state["binding"] is None
    assert app.research.generation > generation and not app.research.cache
    after, hits = app.views_ref.compose(store.snapshot(), app, 120, 30)
    y = next(y for y, kind, jid in hits if kind == "job" and jid == "7")
    assert L.row_text(after[y])[header[2]:header[3]] == expected
    assert sources(app, store)["7"].fraction == .3
    assert "8" not in sources(app, store)


def test_selected_corrections_replace_cached_scalar_including_decreases(linked):
    app, store, _, _ = linked
    publish(app, .8)
    assert sources(app, store)["7"].fraction == .8
    publish(app, .25)
    assert sources(app, store)["7"].fraction == .25
    project_ui.clear_binding(app)
    assert sources(app, store)["7"].fraction == .25


@pytest.mark.parametrize("changes", [
    {"progress": None}, {"progress": {"completed": 1, "total": 0}},
    {"status": "missing"}, {"status": "error"}, {"status": "loading"},
    {"job_id": "8"}, {"generation": -1},
    {"last_t": stamp("2026-10-08T09:59:59")},
])
def test_authoritative_missing_or_invalid_update_evicts_retained_progress(linked, changes):
    app, store, _, _ = linked
    publish(app)
    assert sources(app, store)["7"].fraction == .3
    publish(app, **changes)
    assert "7" not in sources(app, store)
    assert "7" not in app.job_progress_state["reports"]
    project_ui.clear_binding(app)
    assert sources(app, store) == {}


@pytest.mark.parametrize("change", ["root", "path", "attempt", "run", "ambiguous", "suppressed", "off"])
def test_changed_project_source_never_reuses_retained_progress(linked, change):
    app, store, inventory, _ = linked
    publish(app)
    assert sources(app, store)["7"].fraction == .3
    project_ui.clear_binding(app)
    if change == "root":
        app.project_state["root"] += "/other"
    elif change == "path":
        inventory["paths"]["metrics"] = "other.jsonl"
    elif change == "attempt":
        inventory["attempt"] = 2
    elif change == "run":
        inventory["run_id"] = "other"
    elif change == "ambiguous":
        app.project_state["runs"].append({"job_id": "7", "run_id": "another",
                                           "inventory": dict(inventory, run_id="another")})
    elif change == "suppressed":
        app.project_state["auto_suppressed"] = "7"
    else:
        app.project_state["status"] = "off"
    assert sources(app, store) == {}


@pytest.mark.parametrize("change", ["start", "submit", "scheduler_attempt", "replacement"])
def test_requeue_or_replacement_attempt_rejects_retained_progress(linked, change):
    app, store, _, _ = linked
    publish(app)
    assert sources(app, store)["7"].fraction == .3
    project_ui.clear_binding(app)
    if change == "scheduler_attempt":
        store._job_attempts["7"] = 1
    elif change == "replacement":
        store.jobs[0] = replace(store.jobs[0], start="2026-10-08T10:20:00")
    else:
        setattr(store.jobs[0], change, "2026-10-08T10:20:00")
    assert sources(app, store) == {}


@pytest.mark.parametrize("end,expected", [("2026-10-08T10:20:00", True), ("2026-10-08T10:09:59", False)])
def test_completion_preserves_only_reports_inside_the_exact_attempt(linked, end, expected):
    app, store, _, _ = linked
    publish(app)
    assert sources(app, store)["7"].fraction == .3
    project_ui.clear_binding(app)
    job = store.jobs.pop(0)
    store.finished = [Finished(job.id, job.name, "COMPLETED", start=job.start, submit=job.submit, end=end)]
    assert ("7" in sources(app, store)) is expected
    if expected:
        assert sources(app, store)["7"].fraction == .3  # Completion does not invent 100%.


@pytest.mark.parametrize("field,value", [
    ("job_id", "8"), ("attempt", 2), ("run_id", "another"), ("project_root", "/other"),
    ("run_root", "/other/run"), ("metrics_file", "/other/file.jsonl"),
])
def test_unmatched_binding_cannot_seed_inventory_cache(linked, field, value):
    app, store, _, binding = linked
    binding[field] = value
    publish(app)
    assert sources(app, store)["7"].fraction == .3
    assert not app.job_progress_state["reports"]
    project_ui.clear_binding(app)
    assert sources(app, store) == {}


def test_manual_source_stays_generation_scoped(linked):
    app, store, _, _ = linked
    project_ui.clear_binding(app)
    publish(app)
    assert sources(app, store)["7"].fraction == .3
    assert not app.job_progress_state["reports"]
    app.research.configure(metrics_file="/manual/replacement.jsonl")
    assert sources(app, store) == {}


def test_hub_source_mismatch_cannot_retain_a_declared_run_report(linked):
    app, store, _, _ = linked
    app.research.configure(metrics_file="/other/source.jsonl")
    publish(app)
    assert sources(app, store)["7"].fraction == .3
    assert not app.job_progress_state["reports"]
    project_ui.clear_binding(app)
    assert sources(app, store) == {}


@pytest.mark.parametrize("path", ["/outside.jsonl", "../outside.jsonl", "nested/../outside.jsonl"])
def test_invalid_declared_relative_path_cannot_seed_retained_progress(linked, path):
    app, store, inventory, binding = linked
    inventory["paths"]["metrics"] = path
    binding["metrics_file"] = P.os.path.join(binding["run_root"], path)
    app.research.configure(metrics_file=binding["metrics_file"])
    publish(app)
    assert sources(app, store)["7"].fraction == .3
    assert not app.job_progress_state["reports"]


def test_selected_cache_stays_bounded_while_switching_standard_runs(linked):
    app, store, inventory, binding = linked
    count = P.MAX_REPORTS + 1
    app.project_state["runs"] = []
    store.jobs = []
    for index in range(count):
        jid, run_id = str(index + 1000), "run" + str(index)
        store.jobs.append(Job(jid, run_id, "cpu", "RUNNING"))
        record = dict(inventory, job_id=jid, run_id=run_id, start=None, submit=None)
        app.project_state["runs"].append({"job_id": jid, "run_id": run_id, "inventory": record})
    for index, job in enumerate(store.jobs):
        binding.update(job_id=job.id, run_id=job.name, run_root=binding["project_root"] + "/runs/" + job.name)
        binding["metrics_file"] = binding["run_root"] + "/metrics.jsonl"
        app.research.configure(metrics_file=binding["metrics_file"])
        app.research.cache[(app.research.generation, "experiment", job.id)] = (
            0, {"status": "ok", "job_id": job.id, "progress": {"completed": index, "total": count}})
        assert sources(app, store)[job.id].fraction == index / count
    assert len(app.job_progress_state["reports"]) == P.MAX_REPORTS
    assert store.jobs[0].id not in app.job_progress_state["reports"]
    assert store.jobs[-1].id in app.job_progress_state["reports"]


def test_retained_progress_render_has_no_io_requests_or_store_snapshots(linked, monkeypatch):
    app, store, _, _ = linked
    publish(app)
    def forbidden(*args, **kwargs):
        raise AssertionError("Progress rendering performed discovery, reading, requests, or a Store snapshot")
    monkeypatch.setattr(store, "snapshot", forbidden)
    monkeypatch.setattr(store, "record_context", forbidden)
    monkeypatch.setattr(app.research, "request", forbidden)
    for name in ("read", "tail", "stat", "glob", "exists"):
        if hasattr(app.research.files, name):
            monkeypatch.setattr(app.research.files, name, forbidden)
    assert sources(app, store)["7"].fraction == .3
    project_ui.clear_binding(app)
    for _ in range(100):
        assert sources(app, store)["7"].fraction == .3
    assert len(app.job_progress_state["reports"]) == 1
