"""Column clicks reorder rows without changing the identity of queued actions."""
from __future__ import annotations

import pytest

from raven import table_ui
from raven.config import Config
from raven.controller import App
from raven.layout import Glyphs, row_text
from raven.model import Finished, Health, Job, Store
from raven.views import Views


@pytest.fixture
def dashboard(tmp_path):
    cfg = Config({"log_lines": 0, "animations": False})
    store = Store(state_dir=str(tmp_path / "private state"))
    jobs = [Job("10", "same", "main", "RUNNING", cpus=2),
            Job("123_10", "other", "main", "RUNNING", cpus=4),
            Job("9", "same", "main", "RUNNING", cpus=8),
            Job("123_2", "other", "main", "RUNNING", cpus=1),
            Job("2", "same", "main", "RUNNING", cpus=4)]
    store.apply_jobs(jobs)
    store.group = list(jobs)
    store.health = {name: Health(name) for name in ("jobs", "finished", "details", "nodes")}
    store.finished = [Finished("90", "same", "FAILED", cpus=2),
                      Finished("8", "same", "COMPLETED", cpus=8),
                      Finished("110_10", "other", "FAILED", cpus=4),
                      Finished("110_2", "other", "COMPLETED", cpus=1)]
    app = App(store, None, None, cfg, "test")
    views = Views(Glyphs(False), cfg)
    app.views_ref = views
    yield app, views, store
    if app.research:
        app.research.close()


def render(app, views, width=160, height=50):
    return views.compose(app.store.snapshot(), app, width, height)


def click_column(app, views, table, column, *, width=160, height=50, button="left", shift=False):
    rows, hits = render(app, views, width, height)
    y, kind, payload = next(hit for hit in hits if hit[1] == "sort_header" and hit[2][:2] == (table, column))
    _, _, left, right = payload
    assert 0 <= left < right <= width
    assert 0 <= y < height
    app.click(y, left, hits, button=button, shift=shift)
    return rows, payload


@pytest.mark.parametrize("ascii_", [False, True])
@pytest.mark.parametrize("density", ["compact", "comfortable"])
@pytest.mark.parametrize("width,height", [(80, 24), (160, 50)])
def test_jobid_click_cycle_restores_numeric_order_and_source_order(dashboard, ascii_, density, width, height):
    app, views, store = dashboard
    # Keep the original single-job order distinct from either numeric sort.
    store.jobs[0], store.jobs[2] = store.jobs[2], store.jobs[0]
    views.set_ascii(ascii_)
    app.run_command("density " + density)
    expected = ["2", "9", "10", "123_2", "123_10"]
    click_column(app, views, "jobs", "id", width=width, height=height)
    assert app.visible_ids == expected
    assert table_ui.chain(app, "jobs") == [("id", "asc")]
    click_column(app, views, "jobs", "id", width=width, height=height)
    # Batch priority remains fixed while JobID reverses within each batch
    # and between the three equal-size single-job batches.
    assert app.visible_ids == ["10", "9", "2", "123_10", "123_2"]
    assert table_ui.chain(app, "jobs") == [("id", "desc")]
    click_column(app, views, "jobs", "id", width=width, height=height)
    # Removing the cascade restores source order within the batch priorities,
    # preserving contiguous array members instead of interleaving them.
    assert app.visible_ids == ["9", "10", "2", "123_10", "123_2"]
    assert table_ui.chain(app, "jobs") == []


def test_two_clicked_columns_combine_and_remove_independently(dashboard):
    app, views, store = dashboard
    click_column(app, views, "jobs", "name")
    click_column(app, views, "jobs", "cpus")
    click_column(app, views, "jobs", "cpus")
    assert table_ui.chain(app, "jobs") == [("name", "asc"), ("cpus", "desc")]
    assert app.visible_ids == ["9", "2", "10", "123_10", "123_2"]
    text = "\n".join(row_text(row) for row in render(app, views)[0])
    assert "NAME ^1" in text and "CPU v2" in text
    click_column(app, views, "jobs", "cpus")
    assert table_ui.chain(app, "jobs") == [("name", "asc")]
    assert app.visible_ids == ["10", "9", "2", "123_10", "123_2"]


@pytest.mark.parametrize("table", ["jobs", "history", "group", "recent"])
def test_click_then_log_without_redraw_preserves_exact_selected_job(dashboard, table):
    app, views, store = dashboard
    app.enter_tab("jobs" if table == "recent" else table)
    render(app, views)
    if table == "recent":
        app.cursor["jobs"] = len(app.visible_ids)
        app.sync_selection()
    selected = app.selected_id
    click_column(app, views, table, "id")
    assert app.selected_id == selected
    # The screen has not rendered again after App.click reordered the rows.
    app.handle("l")
    assert app.tab == "log" and app.log_job == selected


def test_recents_sort_is_independent_and_keeps_the_same_five_candidates(dashboard):
    app, views, store = dashboard
    store.finished.extend(Finished(str(300 + i), "older", "FAILED") for i in range(10))
    render(app, views)
    candidates = set(app.recent_ids)
    click_column(app, views, "recent", "id")
    assert set(app.recent_ids) == candidates
    assert table_ui.chain(app, "recent") == [("id", "asc")]
    assert table_ui.chain(app, "jobs") is None


def test_clicks_in_header_gap_right_click_and_shift_do_not_change_sort(dashboard):
    app, views, store = dashboard
    rows, payload = click_column(app, views, "jobs", "id", button="right")
    assert table_ui.chain(app, "jobs") is None
    click_column(app, views, "jobs", "id", shift=True)
    assert table_ui.chain(app, "jobs") is None
    rows, hits = render(app, views)
    y, _, cell = next(hit for hit in hits if hit[1] == "sort_header" and hit[2][:2] == ("jobs", "id"))
    app.click(y, cell[3], hits)
    assert table_ui.chain(app, "jobs") is None


def test_sorted_sources_toggle_the_original_selected_source_without_redraw(dashboard):
    app, views, store = dashboard
    for index, health in enumerate(store.health.values()):
        health.errors = index
    app.enter_tab("sources")
    render(app, views)
    app.cursor["sources"] = 2
    chosen = app.source_ids[2]
    before = {name: health.enabled for name, health in store.health.items()}
    app.run_command("sortby sources errors desc")
    assert app.source_ids[app.cursor["sources"]] == chosen
    app.handle_action("source_toggle")
    assert store.health[chosen].enabled is not before[chosen]
    assert all(health.enabled == before[name] for name, health in store.health.items() if name != chosen)


def test_mouse_sort_persists_and_back_restores_the_original_cascade(dashboard):
    app, views, store = dashboard
    click_column(app, views, "jobs", "name")
    click_column(app, views, "jobs", "cpus")
    saved = store.load_ui()
    assert saved["workbench"]["table_ui"]["sorts"]["jobs"] == [["name", "asc"], ["cpus", "asc"]]
    restored = App(store, None, None, app.cfg, "test")
    assert table_ui.chain(restored, "jobs") == [("name", "asc"), ("cpus", "asc")]
    app.enter_tab("history")
    app.run_command("sortby jobs id desc")
    app.run_command("back")
    assert app.tab == "jobs"
    assert table_ui.chain(app, "jobs") == [("name", "asc"), ("cpus", "asc")]


def test_sticky_header_remains_clickable_after_panel_scroll(dashboard):
    app, views, store = dashboard
    store.apply_jobs([Job(str(i), "same", "main", "RUNNING") for i in range(100)])
    store.finished = []
    store.departed_jobs.clear()
    app.run_command("density comfortable")
    render(app, views, 100, 24)
    app.handle("end")
    click_column(app, views, "jobs", "id", width=100, height=24)
    assert app.visible_ids == [str(i) for i in range(100)]
    assert app.selected_id == "99"


def test_sort_reveals_selected_array_task_if_folded_representative_changes(dashboard):
    app, views, store = dashboard
    store.apply_jobs([Job("101_0", "zebra", "main", "RUNNING"),
                      Job("101_1", "aardvark", "main", "RUNNING"),
                      Job("201", "solo", "main", "RUNNING")])
    app.run_command("jobgroups on")
    render(app, views)
    app.cursor["jobs"] = app.visible_ids.index("101_0")
    app.sync_selection()
    app.handle("left")
    assert "101" in app.table_state["collapsed"]
    click_column(app, views, "jobs", "name")
    assert "101" not in app.table_state["collapsed"]
    assert app.selected_id == "101_0"
    opened = []
    app.open_log = lambda jid=None: opened.append(jid or app.selected_id)
    app.handle("l")
    assert opened == ["101_0"]


@pytest.mark.parametrize("table", ["jobs", "history"])
@pytest.mark.parametrize("target", ["header", "row", "blank"])
def test_double_click_activates_only_job_rows(dashboard, table, target):
    import curses
    from raven.screen import _apply_input

    app, views, store = dashboard
    app.enter_tab(table)
    rows, hits = render(app, views)
    if target == "header":
        y, _, payload = next(hit for hit in hits if hit[1] == "sort_header" and hit[2][:2] == (table, "name"))
        x = payload[2]
    elif target == "row":
        y, _, identity = next(hit for hit in hits if hit[1] == ("job" if table == "jobs" else "fin"))
        x = 2
    else:
        y, x = 49, 159
    _apply_input(app, ("mouse", (0, x, y, 0, curses.BUTTON1_DOUBLE_CLICKED)), hits, curses)
    if target == "row":
        assert app.selected_id == identity
        assert app.mode == "details" if table == "jobs" else app.tab == "analytics"
    else:
        assert app.tab == table and app.mode == "main"
        if target == "header":
            assert table_ui.chain(app, table) == [("name", "asc")]


@pytest.mark.parametrize("table", ["jobs", "history", "group"])
@pytest.mark.parametrize("action", ["s", "S", "sort name"])
def test_legacy_sort_after_cascade_preserves_action_identity(dashboard, table, action):
    app, views, store = dashboard
    app.enter_tab(table)
    app.run_command("sortby id desc")
    render(app, views)
    app.handle("down")
    selected = app.selected_id
    if action.startswith("sort "):
        app.run_command(action)
    else:
        app.handle(action)
    assert table_ui.chain(app, table) is None
    assert app.selected_id == selected
    render(app, views)
    assert app.selected_id == selected
    opened = []
    app.open_log = lambda jid=None: opened.append(jid or app.selected_id)
    app.handle("l")
    assert opened == [selected]
