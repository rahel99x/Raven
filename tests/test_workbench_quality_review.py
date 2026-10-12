"""Independent integration regressions for complete exports and restored views."""
from collections import OrderedDict
import copy

import pytest

from raven import analysis_ui, project_ui, report, table_ui
from raven.config import Config
from raven.controller import App
from raven.layout import Glyphs
from raven.model import Finished, Job, Store
from raven.research import ResearchHub
from raven.views import Views


@pytest.fixture
def dashboard():
    cfg = Config({"clipboard": {"osc52": False, "tools": False}})
    store = Store(persist=False)
    store.apply_jobs([Job("1", "active-first", "main", "RUNNING"),
                      Job("2", "pending-second", "gpu", "PENDING")])
    store.apply_finished([Finished("3", "finished-first", "FAILED"),
                          Finished("4", "finished-second", "COMPLETED")])
    app = App(store, None, None, cfg, "reviewer")
    views = Views(Glyphs(True), cfg)
    app.views_ref = views
    views.compose(store.snapshot(), app, 100, 24)
    return app, views, store


def test_complete_report_removes_private_table_facets_without_losing_user_view(dashboard):
    app, views, store = dashboard
    app.run_command("facet jobs state=PENDING")
    app.run_command("facet history id=3")
    before = copy.deepcopy(app.table_state)
    page = report.build(store.snapshot(), app, views)
    history = page.split("03 / HISTORY", 1)[1].split("04 / ANALYTICS", 1)[0]
    assert "finished-first" in history and "finished-second" in history
    assert app.table_state == before


def test_report_isolates_panned_log_cache_from_interactive_buffers(dashboard, tmp_path):
    app, views, store = dashboard
    path = tmp_path / "original.log"
    path.write_bytes(b"wide\tactual\r\nsecond\tactual\r\n")
    store.details["1"] = {"StdOut": str(path)}
    app.open_log("1")
    app.run_command("logpan 3")
    views.compose(store.snapshot(), app, 100, 24)
    assert app.log_workbench_state["pan_cache"]
    token = app.logs._buffer_token[0]
    buffer = app.log_workbench_state["last_buffer"]
    before = copy.deepcopy({key: value for key, value in app.log_workbench_state.items() if key != "last_buffer"},
                           memo={id(token): token})
    buffer_before = copy.deepcopy(vars(buffer), memo={id(buffer.files): buffer.files,
                                                     id(buffer._session_token): buffer._session_token})
    cache = app.log_workbench_state["pan_cache"]
    assert isinstance(cache, OrderedDict)
    report.build(store.snapshot(), app, views)
    assert {key: value for key, value in app.log_workbench_state.items() if key != "last_buffer"} == before
    assert app.log_workbench_state["last_buffer"] is buffer
    assert vars(buffer) == buffer_before
    assert app.log_workbench_state["pan_cache"] is cache


@pytest.mark.parametrize("saved", [None, 12, "bad", [], {"tab": "not-a-tab"},
                                   {"tab": "jobs", "hidden": 17},
                                   {"tab": "history", "facets": ["state=FAILED"]}])
def test_corrupt_saved_table_view_is_rejected_without_crash_or_invalid_navigation(dashboard, saved):
    app, _, _ = dashboard
    table_ui.restore(app, {"views": {"damaged": saved}})
    app.run_command("savedview jobs load damaged")
    assert app.tab == "jobs"
    assert not app.command_ok


def test_ordinary_job_change_does_not_accidentally_clear_source_selection(dashboard, tmp_path):
    """A secondary table snapshot must not reset a pinned original log range."""
    app, views, store = dashboard
    path = tmp_path / "raw.log"
    path.write_bytes(b"one\tactual\r\ntwo\tactual\r\nthree\tactual\r\n")
    store.details["1"] = {"StdOut": str(path)}
    app.open_log("1")
    views.compose(store.snapshot(), app, 100, 24)
    for key in ("home", "v", "down"):
        app.handle(key)
    store.apply_jobs([store.job("1")])
    views.compose(store.snapshot(), app, 100, 24)
    assert app.logs.selection_active
    assert app.logs.selection_bytes(app.prepare_log()) == b"one\tactual\r\ntwo\tactual\r\n"


@pytest.mark.parametrize("route", ["inspector", "investigate", "keyboard"])
def test_explicit_job_navigation_changes_an_existing_bound_run_to_the_chosen_job(dashboard, route):
    app, _, store = dashboard
    app.research = ResearchHub(app.cfg)
    app.project_state["binding"] = {"run_id": "bound-other-attempt", "job_id": "2", "run_root": "/reported/other-attempt"}
    app.tab, app.research_job_id = "research", "2"
    try:
        if route == "inspector":
            assert analysis_ui.open_inspector(app, "1")
            app.handle("e")
        elif route == "investigate":
            app.run_command("investigate 1")
        else:
            app.research_view = "experiment"
            app.handle("up")
        assert app.research_job_id == "1"
        assert project_ui.selected_binding(app) is None
        assert app.research.context(store.snapshot(), app)["jid"] == "1"
    finally:
        app.research.close()


def test_manual_metric_attachment_does_not_retain_an_unrelated_project_identity(dashboard):
    app, _, store = dashboard
    app.research = ResearchHub(app.cfg)
    app.project_state["binding"] = {"run_id": "bound-other-attempt", "job_id": "2", "run_root": "/reported/other-attempt"}
    app.tab, app.research_job_id = "research", "2"
    try:
        app.run_command("metrics /explicit/manual-metrics.jsonl")
        assert app.command_ok
        assert app.research.settings["metrics_file"] == "/explicit/manual-metrics.jsonl"
        assert project_ui.selected_binding(app) is None
        assert app.research.context(store.snapshot(), app)["run_id"] is None
    finally:
        app.research.close()
