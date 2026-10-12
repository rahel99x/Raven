"""Native scoped views retain exact IDs while their visible data changes."""
import curses

import pytest

from raven import history_browser as H, layout as L, screen
from raven.config import Config
from raven.controller import App
from raven.model import Job, Store
from raven.views import Views


@pytest.fixture
def dashboard():
    cfg = Config({"workspace": {"density": "compact"}, "log_lines": 0})
    store = Store(persist=False)
    store.jobs = [Job(str(i), f"stage-{i}", "cpu", "RUNNING", elapsed="00:25:00", limit="01:00:00",
                      dependency=f"afterok:{i - 1}" if i > 1 else "") for i in range(1, 101)]
    app = App(store, None, None, cfg, "tester", interactive=False)
    views = Views(L.Glyphs(False), cfg)
    app.views_ref = views
    yield app, views, store
    if app.research:
        app.research.close()


def render(app, views, store, width=160, height=30):
    return views.compose(store.snapshot(), app, width, height)


def test_scoped_dependency_history_scrolls_to_last_edge_without_changing_job(dashboard, monkeypatch):
    app, views, store = dashboard
    app.enter_tab("deps")
    render(app, views, store)
    assert H.activate(app, "1")
    render(app, views, store)
    assert app.deps_scope_count > app.deps_scope_page
    H.handle_key(app, "esc")
    app.handle("end")
    rows, hits = render(app, views, store)
    assert app.selected_id == "1"
    assert app.deps_scope_top == app.deps_scope_count - app.deps_scope_page
    assert " 100" in L.to_text(rows, 160)
    before = app.deps_scope_top
    rect = app.history_browser_content_rect
    monkeypatch.setattr(store, "snapshot", lambda: pytest.fail("Wheel copied the full snapshot"))
    screen._apply_input(app, ("mouse", (0, rect.x + 4, rect.y + 3, 0, curses.BUTTON4_PRESSED)), hits, curses)
    assert app.deps_scope_top == before - 1
    assert app.selected_id == "1"


@pytest.mark.parametrize("ascii_", [False, True])
@pytest.mark.parametrize("width", [80, 120, 160])
def test_progress_stays_six_cells_when_sorted_and_updates_from_published_data(dashboard, ascii_, width):
    app, views, store = dashboard
    views.set_ascii(ascii_)
    snap = store.snapshot()
    snap["progress"] = {"1": {"progress": {"completed": 3, "total": 10}}}
    app.run_command("sortby jobs progress desc")
    rows, hits = views.compose(snap, app, width, 40)
    header = next(value for _, kind, value in hits if kind == "sort_header" and value[:2] == ("jobs", "progress"))
    assert header[3] - header[2] == 6
    data = {row["id"]: row for row in views.job_rows(snap, app)}
    assert data["1"]["progress"] == ("p =>--" if ascii_ else "▸ █▎░░") and data["1"]["_progress_value"] == .3
    assert all(L.vlen(row["progress"]) == 6 for row in data.values())
    assert data["2"]["progress"].startswith("t" if ascii_ else "◷")
    snap["progress"]["1"]["progress"]["completed"] = 9
    updated = {row["id"]: row for row in views.job_rows(snap, app)}
    assert updated["1"]["progress"] == ("p ===>" if ascii_ else "▸ ███▋") and updated["1"]["_progress_value"] == .9
    assert updated["2"]["progress"] == data["2"]["progress"]


def test_repeated_dependency_representative_buttons_are_individually_clickable(dashboard):
    app, views, store = dashboard
    store.jobs = [Job("1", "a", "cpu", "RUNNING"), Job("2", "b", "cpu", "RUNNING"),
                  Job("900_1", "task", "cpu", "PENDING", dependency="afterok:1:2"),
                  Job("900_2", "task", "cpu", "PENDING", dependency="afterok:900_1")]
    app.enter_tab("deps")
    _, hits = render(app, views, store, 180, 40)
    controls = [value for _, kind, value in hits if kind == "control" and
                isinstance(value, dict) and value["id"].startswith("jobgroup:deps:")]
    assert len(controls) == 2
    assert len({control["id"] for control in controls}) == 2
    assert len({control["action"] for control in controls}) == 1
