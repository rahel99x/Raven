"""Maintenance frames preserve cleared rows and existing metric sources."""
import pytest

from raven import job_selection as S, layout as L
from raven.config import Config
from raven.controller import App
from raven.model import Job, Store
from raven.views import Views


@pytest.fixture
def dashboard():
    cfg = Config({"animations": False, "log_lines": 0})
    store = Store(persist=False)
    store.jobs = [Job("7", "parent", "cpu", "RUNNING", user="tester"),
                  Job("8", "child", "cpu", "PENDING", user="tester", dependency="afterok:7")]
    store.group = list(store.jobs)
    store.record("7", {"k": "live", "t": 1., "cpu": .5, "rss": 1024 ** 3})
    app = App(store, None, None, cfg, "tester", interactive=False)
    views = Views(L.Glyphs(False), cfg)
    app.views_ref = views
    yield app, views, store
    if app.research:
        app.research.close()


@pytest.mark.parametrize("tab", ["group", "deps"])
def test_cleared_table_does_not_regain_highlight_when_rows_change(dashboard, tab):
    app, views, store = dashboard
    app.tab = tab
    render = views.group_tab if tab == "group" else views.deps_tab
    render(store.snapshot(), app, 120, 28)
    assert app.selected_id in {"7", "8"}
    S.clear(app)
    store.jobs.append(Job("9", "new-child", "cpu", "PENDING", user="tester", dependency="afterok:7"))
    store.group = list(store.jobs)
    for _ in range(3):
        rows, _ = render(store.snapshot(), app, 120, 28)
        assert app.selected_id is None
        assert not any("rev" in style for row in rows for _, style in row)
    S.resume(app, tab)
    rows, _ = render(store.snapshot(), app, 120, 28)
    assert app.selected_id in {"7", "8", "9"}
    assert any("rev" in style for row in rows for _, style in row)


def test_scoped_dependency_clear_returns_to_unselected_tree(dashboard):
    app, views, store = dashboard
    app.tab = "deps"
    app.history_browser_state = {"views": {"deps": {"explicit": True, "selected": "7"}}}
    S.clear(app)
    rows, _ = views.deps_tab(store.snapshot(), app, 120, 28)
    assert app.selected_id is None
    assert "dependency chains" in "\n".join(L.row_text(row) for row in rows)
    assert not any("rev" in style for row in rows for _, style in row)


def test_analytics_retains_explicit_source_after_clear(dashboard):
    app, views, store = dashboard
    app.tab = "analytics"
    app.analytics_job = app.selected_id = "7"
    S.clear(app)
    rows = views.analytics_job(store.snapshot(), app, 120, 28)
    assert app.analytics_job == "7" and app.selected_id is None
    assert "parent" in "\n".join(L.row_text(row) for row in rows)


def test_analytics_without_source_does_not_pick_first_job_after_clear(dashboard):
    app, views, store = dashboard
    app.tab = "analytics"
    app.analytics_job = None
    S.clear(app)
    rows = views.analytics_job(store.snapshot(), app, 120, 28)
    assert app.analytics_job is None
    assert "Select a job" in "\n".join(L.row_text(row) for row in rows)
    S.resume(app, "analytics")
    views.analytics_job(store.snapshot(), app, 120, 28)
    assert app.analytics_job == "7"


def test_inline_analytics_retains_explicit_jobs_source_despite_analytics_clear(dashboard):
    from raven.job_panels import _scoped_app, initialize
    app, views, store = dashboard
    app.tab = "analytics"
    S.clear(app)
    app.tab, app.selected_id = "jobs", "7"
    state = initialize(app)
    state.update(mode="analytics", analytics_view="job")
    proxy, _ = _scoped_app(app, store.jobs[0], state)
    rows = views.analytics_job(store.snapshot(), proxy, 120, 28)
    assert proxy.analytics_job == "7"
    assert "parent" in "\n".join(L.row_text(row) for row in rows)
