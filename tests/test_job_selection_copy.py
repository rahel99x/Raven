"""Native list selections must never copy adjacent details or the full screen."""
from dataclasses import replace

import pytest

from tower import clipboard, history_browser, job_copy, job_groups, job_selection
from tower.model import Finished, Job
from test_job_group_mouse_integration import dashboard, dispatch, frame, mark, point


@pytest.fixture
def copied(monkeypatch):
    values = []
    monkeypatch.setattr(clipboard, "copy", lambda text, *_args, **_kwargs: values.append(text) or "copied")
    return values


def copied_ids(copied):
    assert len(copied) == 1
    return [line.split("\t")[0] for line in copied[0].splitlines()]


@pytest.mark.parametrize("route", ["app", "native", "sgr"])
@pytest.mark.parametrize("count", [1, 3])
def test_pointer_single_and_drag_selection_copies_only_selected_jobs(dashboard, copied, route, count):
    app, _store = dashboard
    _, hits = frame(dashboard)
    dispatch(app, hits, point(hits, "4"), "press", route)
    if count > 1:
        dispatch(app, hits, point(hits, str(3 + count)), "drag", route)
    dispatch(app, hits, point(hits, str(3 + count)), "release", route)
    app.handle("y")
    assert copied_ids(copied) == [str(i) for i in range(4, 4 + count)]
    assert all(len(line.split("\t")) == 8 for line in copied[0].splitlines())
    assert "Details" not in copied[0] and "Raven" not in copied[0]


@pytest.mark.parametrize("tab,kind,identifiers", [
    ("jobs", "job", ("4", "5")), ("jobs", "recent", ("24", "25")),
    ("history", "fin", ("24", "25")), ("group", "group", ("4", "5")),
    ("deps", "dep", ("4", "5")),
])
def test_each_native_job_pane_copies_exact_scope(dashboard, copied, tab, kind, identifiers):
    app, store = dashboard
    if tab == "deps":
        store.jobs[3].dependency = "afterok:1"
        store.jobs[4].dependency = "afterok:2"
    _, hits = frame(dashboard, tab)
    dispatch(app, hits, point(hits, identifiers[0], kind), "press", "sgr")
    dispatch(app, hits, point(hits, identifiers[0], kind), "release", "sgr")
    mark(app, *identifiers)
    assert job_copy.copy_selection(app)
    order = job_selection.context(app)["ids"]
    assert copied_ids(copied) == [jid for jid in order if jid in identifiers]


@pytest.mark.parametrize("tab", history_browser.TABS)
@pytest.mark.parametrize("dock", ["right", "top"])
def test_docked_history_selection_copies_only_exact_jobs(dashboard, copied, tab, dock):
    app, _ = dashboard
    app.tab = tab
    history_browser._view(app)["dock"] = dock
    _, hits = frame(dashboard)
    control = next((y, value) for y, kind, value in hits if kind == "control"
                   and value["id"] == f"history:{tab}:job:4")
    position = control[0], control[1]["left"] + 5
    dispatch(app, hits, position, "press", "sgr")
    dispatch(app, hits, position, "release", "sgr")
    mark(app, "4", "5")
    assert job_copy.copy_selection(app)
    assert copied_ids(copied) == [jid for jid in job_selection.context(app)["ids"] if jid in ("4", "5")]


def test_advisor_native_job_selection_is_not_whole_advisor_document(dashboard, copied):
    app, _ = dashboard
    app.tab, app.analytics_view = "analytics", "advisor"
    _, hits = frame(dashboard)
    y, control = next((y, value) for y, kind, value in hits if kind == "control"
                      and value["id"] == "advisor-job:4")
    dispatch(app, hits, (y, control["left"] + 5), "press", "sgr")
    dispatch(app, hits, (y, control["left"] + 5), "release", "sgr")
    mark(app, "4", "5")
    assert job_copy.copy_selection(app)
    assert copied_ids(copied) == ["4", "5"]


def test_offscreen_marked_jobs_are_copied_in_current_list_order_without_full_snapshot(dashboard, copied, monkeypatch):
    app, store = dashboard
    store.jobs.extend(Job(str(i), "offscreen full untruncated name " + str(i), "cpu", "RUNNING",
                          submit="2026-10-09T10:00:00", cluster="local") for i in range(40, 140))
    frame(dashboard, height=30)
    mark(app, "4", "139")
    assert not any(kind == "job" and identifier == "139" for _, kind, identifier in app.last_hits)
    monkeypatch.setattr(store, "snapshot", lambda: pytest.fail("Copying selected jobs copied all metric histories"))
    assert job_copy.copy_selection(app)
    assert copied_ids(copied) == ["4", "139"]
    assert "offscreen full untruncated name 139" in copied[0]


@pytest.mark.parametrize("change", ["submit", "cluster", "missing", "ambiguous"])
@pytest.mark.parametrize("repaint", [False, True])
def test_stale_marked_attempt_never_partially_copies_or_expands_to_screen(dashboard, copied, change, repaint):
    app, store = dashboard
    frame(dashboard)
    mark(app, "4", "5")
    if change in ("submit", "cluster"):
        setattr(store.jobs[4], change, "2026-10-10T12:00:00" if change == "submit" else "elsewhere")
    elif change == "missing":
        store.jobs[:] = [record for record in store.jobs if record.id != "5"]
        store.group = list(store.jobs)
    else:
        store.finished.append(Finished("5", submit="2026-10-10T12:00:00", cluster="local"))
    if repaint:
        frame(dashboard)
    assert job_copy.copy_selection(app)
    assert copied == []
    assert "no partial copy" in app.message.lower()


def test_collapsed_group_copy_preserves_exact_representative_and_does_not_include_hidden_jobs(dashboard, copied):
    app, _ = dashboard
    frame(dashboard)
    group = job_groups.registry(app).index.for_job("1")
    job_groups.fold(app, group.id, True)
    _, hits = frame(dashboard)
    dispatch(app, hits, point(hits, "1"), "press", "sgr")
    dispatch(app, hits, point(hits, "1"), "release", "sgr")
    assert job_copy.copy_selection(app)
    assert copied_ids(copied) == ["1"]


def test_only_marked_job_removed_never_falls_back_to_new_cursor_row(dashboard, copied):
    app, store = dashboard
    frame(dashboard)
    mark(app, "5")
    store.jobs[:] = [record for record in store.jobs if record.id != "5"]
    store.group = list(store.jobs)
    frame(dashboard)
    app.handle("y")
    assert not copied
    assert "no partial copy" in app.message.lower()


def test_marks_across_active_and_recent_rows_copy_both_in_display_order(dashboard, copied):
    app, _ = dashboard
    frame(dashboard)
    mark(app, "4", "24")
    app.handle("y")
    assert copied_ids(copied) == ["4", "24"]


def test_other_pane_healthy_marks_never_replace_focused_single_job(dashboard, copied):
    app, _ = dashboard
    frame(dashboard)
    mark(app, "4")
    _, hits = frame(dashboard, "history")
    dispatch(app, hits, point(hits, "24", "fin"), "press", "sgr")
    dispatch(app, hits, point(hits, "24", "fin"), "release", "sgr")
    app.handle("y")
    assert copied_ids(copied) == ["24"]


@pytest.mark.parametrize("marks", [("4",), ("4", "5")])
@pytest.mark.parametrize("filtered", ["standalone-E", "standalone-F"])
def test_same_pane_filtered_marks_never_become_different_row_or_partial_copy(dashboard, copied, marks, filtered):
    app, _ = dashboard
    frame(dashboard)
    mark(app, *marks)
    app.filter = filtered
    frame(dashboard)
    app.handle("y")
    assert not copied
    assert "no partial copy" in app.message.lower()


def test_cancelled_drag_restores_mark_scope_for_later_filtered_copy_guard(dashboard, copied):
    app, _ = dashboard
    _, hits = frame(dashboard)
    mark(app, "4")
    before = dict(app.job_selection_state["mark_scopes"])
    dispatch(app, hits, point(hits, "6"), "press", "sgr")
    dispatch(app, hits, point(hits, "8"), "drag", "sgr")
    assert job_selection.handle_key(app, "esc")
    assert app.marks == {"4"}
    assert app.job_selection_state["mark_scopes"] == before
    app.filter = "standalone-F"
    frame(dashboard)
    app.handle("y")
    assert not copied


@pytest.mark.parametrize("other", ["details", "modal", "cleared", "legacy_range", "graph", "slider"])
def test_other_selection_owner_is_not_intercepted(dashboard, copied, other):
    app, _ = dashboard
    frame(dashboard)
    mark(app, "4", "5")
    if other == "details":
        app.layout_state.focus = "details"
    elif other == "modal":
        app.mode = "help"
    elif other == "cleared":
        job_selection.clear(app)
    elif other == "legacy_range":
        app.sel_anchor = 7
    elif other == "graph":
        app.chart_interaction_state["capture"] = {"active": True}
    else:
        app.metric_live_state["capture"] = {"active": True}
    assert not job_copy.copy_selection(app)
    assert not copied


def test_copy_limit_is_atomic_and_names_cannot_inject_unselected_lines(dashboard, copied, monkeypatch):
    app, store = dashboard
    store.jobs[3].name = "name\nwith\ttab\x1b[31m"
    frame(dashboard)
    mark(app, "4")
    assert job_copy.copy_selection(app)
    assert copied_ids(copied) == ["4"]
    assert "name\\nwith\\ttab\\x1b[31m" in copied[0]
    copied.clear()
    monkeypatch.setattr(job_copy, "MAX_BYTES", 1)
    assert job_copy.copy_selection(app)
    assert copied == []


def test_frozen_table_copies_frozen_records_and_other_tabs_marks_do_not_leak(dashboard, copied):
    app, store = dashboard
    app.table_tools_state.update(freeze=store.snapshot(), freeze_time=0,
                                 freeze_ids={record.id: record.state for record in store.jobs})
    frame(dashboard)
    mark(app, "4")
    app.marks.add("99999")  # An unrelated unbound mark is never copied.
    store.jobs[:] = [replace(record, name="new name") for record in store.jobs]
    assert job_copy.copy_selection(app)
    assert copied_ids(copied) == ["4"]
    assert "standalone-D" in copied[0] and "new name" not in copied[0]
