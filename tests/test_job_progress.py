"""Reported completion is exact-job data; a wall-time fraction stays labeled."""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import FrozenInstanceError
from threading import Event, current_thread
from types import SimpleNamespace
import json

import pytest

from raven import job_progress as P, layout as L
from raven.config import Config
from raven.model import Finished, Job, Store, stamp
from raven.research import ResearchHub


@pytest.fixture
def dashboard():
    hub = ResearchHub(Config())
    store = Store(persist=False)
    store.jobs = [Job("900", "exact", "cpu", "RUNNING", elapsed="00:25:00", limit="01:00:00"),
                  Job("901", "other", "cpu", "PENDING")]
    app = SimpleNamespace(research=hub)
    yield SimpleNamespace(app=app, hub=hub, store=store)
    hub.close()


def cache(dashboard, data, *, jid="900", generation=None):
    generation = dashboard.hub.generation if generation is None else generation
    dashboard.hub.cache[(generation, "experiment", jid)] = (0, data)


def source(dashboard):
    return P.published(dashboard.app, dashboard.store.snapshot())


def test_canonical_report_has_a_distinct_reported_marker_and_six_cells(dashboard):
    cache(dashboard, {"status": "ok", "progress": {"completed": 42, "total": 100, "unit": "steps"}})
    observation = P.observation(dashboard.store.jobs[0], source(dashboard))
    assert observation.fraction == .42 and observation.basis == "reported"
    assert observation.unit == "steps" and observation.style == "cyan"
    assert observation.format(True) == "p =>--"
    assert observation.text == "▸ █▋░░" and L.vlen(observation.text) == 6


@pytest.mark.parametrize("latest,expected", [({"progress_fraction": .625}, .625),
                                           ({"progress_pct": 62.5}, .625),
                                           ({"completed_steps": 5, "total_steps": 8}, .625),
                                           ({"progress_fraction": .625, "progress_pct": 62.5}, .625)])
def test_optional_explicit_metric_aliases(dashboard, latest, expected):
    cache(dashboard, {"status": "ok", "latest": latest})
    assert source(dashboard)["900"].fraction == expected


def test_disagreeing_aliases_are_rejected_instead_of_guessing(dashboard):
    cache(dashboard, {"status": "ok", "latest": {"progress_fraction": .25, "progress_pct": 75}})
    assert "900" not in source(dashboard)


def test_canonical_progress_is_authoritative_over_optional_metric_names(dashboard):
    cache(dashboard, {"status": "ok", "progress": {"completed": 1, "total": 4},
                      "latest": {"progress_fraction": .9}})
    assert source(dashboard)["900"].fraction == .25


@pytest.mark.parametrize("progress", [{"completed": 1, "total": 0}, {"completed": -1, "total": 2},
                                      {"completed": 3, "total": 2}, {"completed": True, "total": 2},
                                      {"completed": float("nan"), "total": 2},
                                      {"completed": 1, "total": float("inf")},
                                      {"completed": "1", "total": 2},
                                      {"completed": 1, "total": 2, "unit": "\x1bBAD"}])
def test_invalid_reported_bounds_do_not_create_progress(dashboard, progress):
    cache(dashboard, {"status": "ok", "progress": progress})
    observation = P.observation(dashboard.store.jobs[0], source(dashboard))
    assert observation.basis == "time" and observation.format() == "◷ █▋░░"
    assert observation.format(True) == "t =>--"


@pytest.mark.parametrize("latest", [{"progress_fraction": 1.01}, {"progress_fraction": -.01},
                                    {"progress_pct": 101}, {"progress_pct": -1},
                                    {"completed_steps": 2, "total_steps": 1},
                                    {"completed_steps": 1, "total_steps": 0},
                                    {"progress_fraction": True}, {"progress_pct": float("inf")}])
def test_invalid_alias_bounds_are_ignored(dashboard, latest):
    cache(dashboard, {"status": "ok", "latest": latest})
    assert source(dashboard) == {}


@pytest.mark.parametrize("fraction", [0, .001, .125, .42, .5, .875, .999, 1])
@pytest.mark.parametrize("ascii_", [False, True])
@pytest.mark.parametrize("basis", ["reported", "time"])
def test_all_progress_cells_are_exactly_six_terminal_columns(fraction, ascii_, basis):
    observation = P.Observation(fraction, basis)
    cell = observation.format(ascii_)
    assert L.vlen(cell) == 6 and len(cell) == 6
    assert cell.isascii() if ascii_ else True


@pytest.mark.parametrize("basis,expected", [("pending", "⧗ wait"), ("unknown", "   -- ")])
def test_unknown_and_pending_have_no_sortable_fraction(basis, expected):
    observation = P.Observation(None, basis)
    assert observation.fraction is None and observation.text == expected
    assert L.vlen(observation.text) == 6


def test_time_use_is_not_called_completion_and_can_exceed_one_hundred_percent():
    job = Job("900", "exact", "cpu", "RUNNING", elapsed="01:12:00", limit="01:00:00")
    observation = P.observation(job, {})
    assert observation.basis == "time" and observation.fraction == 1.2
    assert observation.text == "◷ ████" and observation.style == "red"
    assert observation.format(True) == "t ===="
    assert observation.source == "elapsed/requested wall-time"


@pytest.mark.parametrize("basis,marker", [("time", "◷"), ("reported", "▸")])
@pytest.mark.parametrize("fraction,suffix", [(0, " ░░░░"), (.42, " █▋░░"), (1, " ████")])
def test_single_cell_markers_keep_allocation_time_distinct_from_completion(basis, marker, fraction, suffix):
    assert L.vlen(marker) == 1
    assert P.Observation(fraction, basis).text == marker + suffix


@pytest.fixture
def progress_table():
    from raven.controller import App
    from raven.views import Views

    cfg = Config({"log_lines": 0, "animations": False, "startup_animation": False})
    store = Store(persist=False)
    app = App(store, None, None, cfg, "reader")
    app.research = ResearchHub(cfg)
    app.job_panel_state["mode"] = "off"
    views = Views(L.Glyphs(False), cfg)
    app.views_ref = views
    yield store, app, views
    app.research.close()


@pytest.mark.parametrize("width,maximized", [(40, False), (80, False), (160, False), (160, True), (240, True)])
@pytest.mark.parametrize("ascii_", [False, True])
def test_leading_progress_retains_six_cells_beside_marks_and_array_fold_controls(progress_table, width, maximized, ascii_):
    store, app, views = progress_table
    store.jobs = [Job("101_0", "array", "cpu", "RUNNING", elapsed="00:25:00", limit="01:00:00"),
                  Job("101_1", "array", "cpu", "RUNNING", elapsed="00:25:00", limit="01:00:00")]
    app.marks.add("101_0")
    app.table_state["groups"] = True
    views.set_ascii(ascii_)
    if maximized:
        app.run_command("focus main")
        app.run_command("maximize on")
    rows, hits = views.compose(store.snapshot(), app, width, 40)
    headers = [hit for hit in hits if hit[1] == "sort_header" and hit[2][0] == "jobs"]
    _, _, progress = headers[0]
    assert progress[:2] == ("jobs", "progress") and progress[3] - progress[2] == 6
    assert next(hit[2] for hit in headers if hit[2][1] == "id")[2] >= progress[3] + 2
    y = next(y for y, kind, jid in hits if kind == "job" and jid == "101_0")
    text = L.row_text(rows[y])
    expected = P.Observation(25 / 60, "time").format(ascii_)
    assert text[progress[2]:progress[3]] == expected
    assert views.g.mark in text[:progress[2]]
    fold = next(value for row, kind, value in hits if row == y and kind == "control"
                and value.get("group") == "job-groups")
    assert fold["right"] <= progress[2]
    assert all(L.vlen(row_text) <= width for row_text in map(L.row_text, rows))


def test_leading_progress_mouse_cycle_sorts_raw_fractions_and_keeps_job_identity(progress_table):
    from raven import table_sort

    store, app, views = progress_table
    store.jobs = [Job("100", "high", "cpu", "RUNNING"),
                  Job("2", "low", "cpu", "RUNNING"),
                  Job("3", "unknown", "cpu", "RUNNING")]
    # Equal batch activity isolates progress ordering, including a running
    # job without reported progress or a usable walltime estimate.
    for jid, fraction in (("100", .5004), ("2", .5003)):
        app.research.cache[(app.research.generation, "experiment", jid)] = (0, {"status": "ok", "latest": {"progress_fraction": fraction}})
    app.table_state["groups"] = False
    for direction, expected in (("asc", ["2", "100", "3"]), ("desc", ["100", "2", "3"]), (None, ["100", "2", "3"])):
        rows, hits = views.compose(store.snapshot(), app, 160, 40)
        selected = app.selected_id
        y, _, payload = next(hit for hit in hits if hit[1] == "sort_header" and hit[2][:2] == ("jobs", "progress"))
        assert payload[3] - payload[2] == 6
        app.click(y, payload[2], hits)
        assert app.selected_id == selected and app.visible_ids == expected
        assert table_sort.chain(app, "jobs") == ([] if direction is None else [("progress", direction)])
    table_sort.set_sort(app, "jobs", "progress", "asc")
    table_sort.set_sort(app, "jobs", "id", "desc")
    rows, hits = views.compose(store.snapshot(), app, 160, 40)
    assert table_sort.chain(app, "jobs") == [("progress", "asc"), ("id", "desc")]
    assert app.visible_ids == ["2", "100", "3"]
    payload = next(hit[2] for hit in hits if hit[1] == "sort_header" and hit[2][:2] == ("jobs", "progress"))
    assert payload[3] - payload[2] == 6


def test_custom_column_order_and_hidden_progress_preserve_identity_columns(progress_table):
    from raven import table_ui
    from raven.views import JOB_COLS

    store, app, views = progress_table
    assert [col.key for col in JOB_COLS[:2]] == ["progress", "id"]
    app.table_state["order"]["jobs"] = ["id", "progress", "name"]
    assert [col.key for col in table_ui.columns(app, "jobs", JOB_COLS)[:3]] == ["id", "progress", "name"]
    app.table_state["hidden"]["jobs"] = ["progress"]
    columns = table_ui.columns(app, "jobs", JOB_COLS)
    assert columns[0].key == "id" and all(col.key != "progress" for col in columns)
    store.jobs = [Job("900", "exact", "cpu", "RUNNING")]
    _, hits = views.compose(store.snapshot(), app, 80, 30)
    assert any(hit[1] == "sort_header" and hit[2][:2] == ("jobs", "id") for hit in hits)
    assert not any(hit[1] == "sort_header" and hit[2][:2] == ("jobs", "progress") for hit in hits)
    assert app.selected_id == "900"


@pytest.mark.parametrize("limit", ["", "UNLIMITED", "0", "nan", "inf", "-1"])
def test_missing_and_invalid_limits_are_not_application_progress(limit):
    job = Job("900", "exact", "cpu", "RUNNING", elapsed="00:25:00", limit=limit)
    observation = P.observation(job, {})
    assert observation.fraction is None and observation.text == "   -- "


def test_pending_without_reported_progress_is_waiting_not_zero_done(dashboard):
    assert P.observation(dashboard.store.jobs[1], source(dashboard)).text == "⧗ wait"


@pytest.mark.parametrize("key,data", [((0, "experiment", "901"), {"job_id": "900", "progress": {"completed": 1, "total": 2}}),
                                     ((1, "experiment", "900"), {"progress": {"completed": 1, "total": 2}}),
                                     ((0, "evidence", "900"), {"progress": {"completed": 1, "total": 2}}),
                                     ((0, "experiment", None), {"progress": {"completed": 1, "total": 2}}),
                                     ((0, "experiment", "900"), {"generation": 1, "progress": {"completed": 1, "total": 2}}),
                                     ((0, "experiment", "900"), {"status": "loading", "progress": {"completed": 1, "total": 2}})])
def test_wrong_job_old_configuration_or_wrong_view_never_attach(dashboard, key, data):
    dashboard.hub.cache[key] = (0, data)
    assert source(dashboard) == {}


def test_known_attempt_start_rejects_earlier_progress(dashboard):
    start = "2026-10-08T10:00:00"
    dashboard.store.jobs[0].start = start
    cache(dashboard, {"status": "ok", "last_t": stamp(start) - 1,
                      "progress": {"completed": 1, "total": 2}})
    assert source(dashboard) == {}
    dashboard.hub.cache[(0, "experiment", "900")][1]["last_t"] = stamp(start) + 10
    assert source(dashboard)["900"].fraction == .5


def test_finished_record_rejects_reports_outside_accounting_attempt(dashboard):
    start, end = "2026-10-08T10:00:00", "2026-10-08T11:00:00"
    dashboard.store.jobs.clear()
    dashboard.store.finished = [Finished("900", "exact", "COMPLETED", start=start, end=end)]
    cache(dashboard, {"status": "ok", "last_t": stamp(end) + 1,
                      "progress": {"completed": 1, "total": 2}})
    assert source(dashboard) == {}
    dashboard.hub.cache[(0, "experiment", "900")][1]["last_t"] = stamp(end) + .9
    assert source(dashboard)["900"].fraction == .5


def test_changed_attempt_identity_fields_reject_old_report(dashboard):
    dashboard.store.jobs[0].submit = "2026-10-08T09:00:00"
    cache(dashboard, {"status": "ok", "job_submit": "2026-10-07T09:00:00",
                      "progress": {"completed": 1, "total": 2}})
    assert source(dashboard) == {}


def test_runtime_append_and_in_place_updates_are_visible_without_cached_ui_state(dashboard):
    data = {"status": "ok", "progress": {"completed": 1, "total": 4}}
    cache(dashboard, data)
    first = source(dashboard)
    data["progress"]["completed"] = 2
    second = source(dashboard)
    assert first["900"].fraction == .25 and second["900"].fraction == .5
    with pytest.raises(FrozenInstanceError):
        second["900"].fraction = .9


def test_explicit_snapshot_report_uses_exact_dictionary_job_identity(dashboard):
    snap = dashboard.store.snapshot()
    snap["progress"] = {"900": {"completed": 3, "total": 4},
                        "901": {"job_id": "900", "completed": 1, "total": 2},
                        "absent": {"completed": 1, "total": 2}}
    reports = P.published(dashboard.app, snap)
    assert reports == {"900": P.Source(.75)}


def test_publication_does_not_read_series_start_workers_or_touch_files(dashboard, monkeypatch):
    class Untouched:
        def __iter__(self):
            raise AssertionError("Full metric series was traversed")
        def __deepcopy__(self, memo):
            raise AssertionError("Full metric series was copied")
    def forbidden(*args, **kwargs):
        raise AssertionError("Render progress attempted data acquisition")
    cache(dashboard, {"status": "ok", "progress": {"completed": 1, "total": 2}, "series": Untouched()})
    for name in ("context", "current", "request", "start_task"):
        monkeypatch.setattr(dashboard.hub, name, forbidden)
    for name in ("read", "stat"):
        monkeypatch.setattr(dashboard.hub.files, name, forbidden)
    monkeypatch.setattr(dashboard.store, "series_of", forbidden)
    for _ in range(20):
        assert source(dashboard)["900"].fraction == .5
    assert dashboard.hub.future is None


def test_hub_reconfiguration_removes_old_progress_until_new_publication(dashboard):
    cache(dashboard, {"status": "ok", "progress": {"completed": 1, "total": 2}})
    assert source(dashboard)["900"].fraction == .5
    dashboard.hub.configure(metrics_file="/another/run/metrics.jsonl")
    assert source(dashboard) == {}
    cache(dashboard, {"status": "ok", "progress": {"completed": 2, "total": 3}})
    assert source(dashboard)["900"].fraction == 2 / 3


def bound_app(dashboard):
    app = dashboard.app
    app.cfg, app.store = Config(), dashboard.store
    app.interactive, app.tab, app.mode, app.selected_id = True, "jobs", "main", "900"
    app.project_state = {"binding": {"job_id": "900", "run_id": "run900", "attempt": 1,
                                     "run_root": "/exact/run900", "metrics_file": "/exact/run900/metrics.jsonl"},
                         "busy": False, "auto_pending": False}
    app.job_panel_state = {"mode": "inspector", "quick": {"status": "idle"}}
    dashboard.hub.configure(metrics_file="/exact/run900/metrics.jsonl")
    return app


def test_jobs_progress_reads_on_existing_worker_without_opening_research(dashboard, monkeypatch):
    app = bound_app(dashboard)
    entered, release = Event(), Event()
    contexts = []
    def background(context):
        assert current_thread().name.startswith("raven-research")
        contexts.append(context)
        entered.set()
        assert release.wait(3)
        return {"status": "ok", "job_id": "900", "progress": {"completed": 3, "total": 4}}
    def forbidden(*args, **kwargs):
        raise AssertionError("Foreground progress called snapshot or context traversal")
    monkeypatch.setattr(app.store, "snapshot", forbidden)
    monkeypatch.setattr(dashboard.hub, "context", forbidden)
    monkeypatch.setattr(dashboard.hub, "_read", background)
    try:
        assert P.tick(app)
        assert entered.wait(1)
        future = dashboard.hub.future
        for _ in range(20):
            assert not P.tick(app)
            assert dashboard.hub.future is future
        assert app.tab == "jobs" and app.job_panel_state["mode"] == "inspector"
        assert contexts[0]["binding"]["job_id"] == contexts[0]["jid"] == "900"
        assert contexts[0]["snap"]["jobs"] == [app.store.jobs[0]]
        assert "series" not in contexts[0]["snap"]
    finally:
        release.set()
    future.result(timeout=3)
    # Progress publication is the Hub's ordinary atomic cache publication.
    reports = P.published(app, {"jobs": app.store.jobs})
    assert reports["900"].fraction == .75
    assert app.job_panel_state["quick"]["status"] == "idle"


@pytest.mark.parametrize("case", ["wrong_job", "missing_path", "no_binding", "tab", "mode", "once",
                                  "project_busy", "automatic_read", "quick_queued", "quick_loading", "stale_path"])
def test_progress_tick_respects_binding_and_explicit_worker_priorities(dashboard, monkeypatch, case):
    app = bound_app(dashboard)
    if case == "wrong_job":
        app.project_state["binding"]["job_id"] = "901"
    elif case == "missing_path":
        app.project_state["binding"]["metrics_file"] = ""
    elif case == "no_binding":
        app.project_state["binding"] = None
    elif case == "tab":
        app.tab = "analytics"
    elif case == "mode":
        app.mode = "help"
    elif case == "once":
        app.interactive = False
    elif case == "project_busy":
        app.project_state["busy"] = True
    elif case == "automatic_read":
        app.project_state["auto_pending"] = True
    elif case in ("quick_queued", "quick_loading"):
        app.job_panel_state["quick"]["status"] = case.split("_")[1]
    else:
        dashboard.hub.settings["metrics_file"] = "/wrong/job/metrics.jsonl"
    def forbidden(*args, **kwargs):
        raise AssertionError("Priority/binding guard still requested a read")
    monkeypatch.setattr(dashboard.hub, "request", forbidden)
    assert not P.tick(app) and dashboard.hub.future is None


def test_busy_worker_is_never_queued_behind(dashboard):
    app = bound_app(dashboard)
    entered, release = Event(), Event()
    def busy():
        entered.set()
        assert release.wait(3)
    try:
        assert dashboard.hub.start_task(busy, lambda value: None)
        assert entered.wait(1)
        future = dashboard.hub.future
        for _ in range(20):
            assert not P.tick(app) and dashboard.hub.future is future
    finally:
        release.set()
    future.result(timeout=3)
    dashboard.hub.poll_task()


def test_progress_polling_respects_its_base_interval_and_bounded_slider_speed(dashboard, monkeypatch):
    app = bound_app(dashboard)
    requests = []
    now = [100]
    monkeypatch.setattr(P.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(dashboard.hub, "request", lambda context: requests.append(context))
    assert P.tick(app)
    assert not P.tick(app)
    now[0] += 7.9
    assert not P.tick(app)
    now[0] += .1
    assert P.tick(app) and len(requests) == 2
    app.refresh_rate_state = {"multiplier": 50}
    # The fastest global position requests a tenfold speedup for non-native
    # readers: the unchanged eight-second progress interval becomes 800 ms.
    now[0] += .79
    assert not P.tick(app)
    now[0] += .01
    assert P.tick(app) and len(requests) == 3
    assert P.POLL_INTERVAL == 8


def test_configuration_change_restarts_exact_read_after_bound_guard(dashboard, monkeypatch):
    app = bound_app(dashboard)
    requests = []
    monkeypatch.setattr(dashboard.hub, "request", lambda context: requests.append(context))
    assert P.tick(app)
    assert not P.tick(app)
    dashboard.hub.configure(metrics_file="/new/run/metrics.jsonl")
    assert not P.tick(app)  # Old binding must not request the new source.
    app.project_state["binding"]["metrics_file"] = "/new/run/metrics.jsonl"
    app.project_state["binding"]["run_id"] = "newrun"
    assert P.tick(app)
    assert requests[-1]["generation"] == dashboard.hub.generation
    assert requests[-1]["binding"]["run_id"] == "newrun"


def test_running_old_generation_read_cannot_publish_after_configuration_change(dashboard, monkeypatch):
    app = bound_app(dashboard)
    entered, release = Event(), Event()
    def held(context):
        entered.set()
        assert release.wait(3)
        return {"status": "ok", "progress": {"completed": 1, "total": 2}}
    monkeypatch.setattr(dashboard.hub, "_read", held)
    try:
        assert P.tick(app)
        assert entered.wait(1)
        future = dashboard.hub.future
        dashboard.hub.configure(metrics_file="/new/metrics.jsonl")
    finally:
        release.set()
    future.result(timeout=3)
    assert source(dashboard) == {}


def visible_project(dashboard, tmp_path, count=6):
    app = dashboard.app
    app.cfg, app.store = Config(), dashboard.store
    app.interactive, app.tab, app.mode, app.selected_id = True, "jobs", "main", None
    app.job_panel_state = {"quick": {"status": "idle"}}
    app.project_state = {"root": str(tmp_path), "binding": None, "runs": [],
                         "busy": False, "auto_pending": False}
    app.store.jobs = []
    for index in range(count):
        jid, run_id = str(900 + index), "run" + str(900 + index)
        directory = tmp_path / "runs" / run_id
        directory.mkdir(parents=True)
        inventory = {"schema": "raven.run/v1", "run_id": run_id, "experiment_id": "test", "attempt": 1,
                     "state": "RUNNING", "job_id": jid, "paths": {"metrics": "metrics.jsonl"}}
        (directory / "run.json").write_text(json.dumps(inventory))
        (directory / "metrics.jsonl").write_text(json.dumps({"t": 1, "metrics": {"loss": .3},
                                                            "progress": {"completed": index + 1, "total": 10}}) + "\n")
        app.project_state["runs"].append({"run_id": run_id, "job_id": jid, "inventory": inventory})
        app.store.jobs.append(Job(jid, "exact" + jid, "cpu", "RUNNING", elapsed="00:10:00", limit="01:00:00"))
    app.job_progress_visible = [job.id for job in app.store.jobs]
    return app


def finish_batch(dashboard):
    dashboard.hub.future.result(timeout=3)
    dashboard.hub.poll_task()
    return P.published(dashboard.app, {"jobs": dashboard.store.jobs})


def test_all_visible_standard_runs_refresh_in_fair_bounded_batches(dashboard, tmp_path, monkeypatch):
    from raven import projects
    from raven.metrics import MetricReader
    app = visible_project(dashboard, tmp_path)
    calls = []
    original, original_read = projects.select_run, MetricReader.read_confined
    def select(root, run_id, **kwargs):
        assert current_thread().name.startswith("raven-research")
        calls.append(run_id)
        return original(root, run_id, **kwargs)
    def read(reader, *args, **kwargs):
        assert current_thread().name.startswith("raven-research")
        assert reader.max_points == 1 and reader.max_bytes == 65536 and reader.max_streams == 64
        return original_read(reader, *args, **kwargs)
    def forbidden(*args, **kwargs):
        raise AssertionError("Visible progress built a full Store snapshot")
    monkeypatch.setattr(projects, "select_run", select)
    monkeypatch.setattr(MetricReader, "read_confined", read)
    monkeypatch.setattr(app.store, "snapshot", forbidden)
    assert P.tick(app)
    dashboard.hub.future.result(timeout=3)
    assert not P.initialize(app)["reports"]  # No worker mutation of UI reports.
    dashboard.hub.poll_task()
    reports = P.published(app, {"jobs": app.store.jobs})
    assert list(reports) == ["900", "901", "902", "903"]
    assert calls == ["run900", "run901", "run902", "run903"]
    assert not P.tick(app)
    P.initialize(app)["batch_last"] = None
    assert P.tick(app)
    reports = finish_batch(dashboard)
    assert set(reports) == set(app.job_progress_visible)
    assert calls[4:] == ["run904", "run905", "run900", "run901"]
    assert reports["905"].fraction == .6 and app.selected_id is None


def test_batch_metric_appends_update_report_without_research_navigation(dashboard, tmp_path):
    app = visible_project(dashboard, tmp_path, count=1)
    assert P.tick(app)
    assert finish_batch(dashboard)["900"].fraction == .1
    path = tmp_path / "runs" / "run900" / "metrics.jsonl"
    with path.open("a") as stream:
        stream.write(json.dumps({"t": 2, "metrics": {}, "progress": {"completed": 7, "total": 10}}) + "\n")
    P.initialize(app)["batch_last"] = None
    assert P.tick(app)
    assert finish_batch(dashboard)["900"].fraction == .7
    assert app.tab == "jobs"


@pytest.mark.parametrize("transition", ["root", "generation", "attempt", "inventory_path", "cancel", "tab"])
def test_stale_visible_batch_cannot_publish(dashboard, tmp_path, monkeypatch, transition):
    from raven import projects
    app = visible_project(dashboard, tmp_path, count=1)
    entered, release = Event(), Event()
    original = projects.select_run
    def held(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return original(*args, **kwargs)
    monkeypatch.setattr(projects, "select_run", held)
    try:
        assert P.tick(app)
        assert entered.wait(1)
        future = dashboard.hub.future
        if transition == "root":
            app.project_state["root"] = str(tmp_path / "new-root")
        elif transition == "generation":
            dashboard.hub.configure(metrics_file="/another/source")
        elif transition == "attempt":
            app.store.jobs[0].start = "2026-10-08T10:00:00"
        elif transition == "inventory_path":
            app.project_state["runs"][0]["inventory"]["paths"]["metrics"] = "replacement.jsonl"
        elif transition == "tab":
            app.tab = "history"
        else:
            P.cancel_automatic(app)
    finally:
        release.set()
    future.result(timeout=3)
    dashboard.hub.poll_task()
    assert P.published(app, {"jobs": app.store.jobs}) == {}


def test_duplicate_run_attempts_never_guess_one_job_source(dashboard, tmp_path):
    app = visible_project(dashboard, tmp_path, count=1)
    duplicate = dict(app.project_state["runs"][0])
    duplicate["inventory"] = dict(duplicate["inventory"], run_id="duplicate900", attempt=2)
    duplicate["run_id"] = "duplicate900"
    app.project_state["runs"].append(duplicate)
    assert not P.tick(app)
    assert dashboard.hub.future is None


def test_duplicate_without_metrics_still_makes_the_job_attempt_ambiguous(dashboard, tmp_path):
    app = visible_project(dashboard, tmp_path, count=1)
    duplicate = dict(app.project_state["runs"][0])
    duplicate["inventory"] = dict(duplicate["inventory"], run_id="duplicate900", attempt=2, paths={})
    duplicate["run_id"] = "duplicate900"
    app.project_state["runs"].append(duplicate)
    assert not P.tick(app)
    assert dashboard.hub.future is None


def test_application_start_after_slurm_allocation_start_is_valid():
    job = Job("900", "job", "cpu", "RUNNING", start="2026-10-08T10:00:00", submit="2026-10-08T09:00:00")
    metadata = ("900", "run900", 1, "metrics.jsonl", stamp(job.start) + 30, stamp(job.submit) + 1, None)
    assert P._matches_inventory(job, metadata)
    earlier = ("900", "run900", 1, "metrics.jsonl", stamp(job.start) - 60, stamp(job.submit), None)
    assert not P._matches_inventory(job, earlier)


def test_in_place_inventory_change_detaches_old_scalar_progress_immediately(dashboard, tmp_path):
    app = visible_project(dashboard, tmp_path, count=1)
    assert P.tick(app)
    assert finish_batch(dashboard)["900"].fraction == .1
    app.project_state["runs"][0]["inventory"]["paths"]["metrics"] = "changed.jsonl"
    assert P.published(app, {"jobs": app.store.jobs}) == {}


def test_wrong_job_binding_after_worker_revalidation_is_rejected(dashboard, tmp_path):
    app = visible_project(dashboard, tmp_path, count=1)
    path = tmp_path / "runs" / "run900" / "run.json"
    actual = json.loads(path.read_text())
    actual["job_id"] = "999"
    path.write_text(json.dumps(actual))
    assert P.tick(app)
    assert finish_batch(dashboard) == {}


def test_over_limit_inventory_adapter_is_refused_without_traversing_it(dashboard, tmp_path):
    app = visible_project(dashboard, tmp_path, count=1)
    app.project_state["runs"] *= 257
    assert not P.tick(app)
    assert dashboard.hub.future is None


def test_explicit_quick_advisor_detaches_visible_progress_batch(dashboard, tmp_path, monkeypatch):
    from raven import projects, job_panels as J, quick_advisor as Q
    from raven.views import Views
    app = visible_project(dashboard, tmp_path, count=1)
    app.selected_id, app.views_ref = "900", Views(L.Glyphs(False), app.cfg)
    app.job_panel_state.update(mode="quick")
    entered, release = Event(), Event()
    original = projects.select_run
    def held(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return original(*args, **kwargs)
    monkeypatch.setattr(projects, "select_run", held)
    try:
        assert P.tick(app)
        assert entered.wait(1)
        future = dashboard.hub.future
        assert Q.request(app)
        assert Q.initialize(app)["status"] == "queued"
        assert dashboard.hub.pending is None  # Batch callback detached.
    finally:
        release.set()
    future.result(timeout=3)
    J.tick(app)
    dashboard.hub.future.result(timeout=3)
    dashboard.hub.poll_task()
    assert Q.initialize(app)["status"] == "ok"
    assert not P.initialize(app)["reports"]


def test_manual_source_detach_suppresses_only_that_jobs_standard_progress(dashboard, tmp_path):
    app = visible_project(dashboard, tmp_path, count=2)
    app.project_state["auto_suppressed"], app.selected_id = "900", "900"
    assert P.tick(app)
    assert set(finish_batch(dashboard)) == {"901"}
    assert "900" not in P.initialize(app)["reports"]


def test_manual_detach_and_project_off_hide_prior_scalar_reports(dashboard, tmp_path):
    app = visible_project(dashboard, tmp_path, count=1)
    assert P.tick(app)
    assert finish_batch(dashboard)["900"].fraction == .1
    app.project_state["auto_suppressed"] = "900"
    assert P.published(app, {"jobs": app.store.jobs}) == {}
    P.initialize(app)["batch_last"] = None
    assert not P.tick(app)
    app.project_state["auto_suppressed"] = None
    app.project_state["status"] = "off"
    assert P.published(app, {"jobs": app.store.jobs}) == {}
    assert not P.tick(app)


def test_explicit_project_priority_hook_detaches_running_progress_batch(dashboard, tmp_path, monkeypatch):
    from raven import projects, project_ui
    app = visible_project(dashboard, tmp_path, count=1)
    entered, release = Event(), Event()
    original = projects.select_run
    def held(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return original(*args, **kwargs)
    monkeypatch.setattr(projects, "select_run", held)
    try:
        assert P.tick(app)
        assert entered.wait(1)
        future = dashboard.hub.future
        project_ui.cancel_automatic(app)
        assert dashboard.hub.pending is None
    finally:
        release.set()
    future.result(timeout=3)
    dashboard.hub.poll_task()
    assert P.published(app, {"jobs": app.store.jobs}) == {}
