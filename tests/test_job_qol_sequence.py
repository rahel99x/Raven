"""One realistic sequence crosses the new controls and old pointer owners."""
from copy import deepcopy

import pytest

from raven import chart_interaction as Charts, column_resize as Columns, group_queue_drag as Queue
from raven import job_groups as Groups, job_group_drag as Membership, job_selection as Selection
from raven import table_ui
from raven.model import Job
from test_group_queue_drag import dashboard, draw, grip, row, send


@pytest.mark.parametrize("route", ["app", "native", "sgr"])
def test_dynamic_grouping_selection_resize_graph_and_stale_queue_sequence(dashboard, monkeypatch, route):
    ui, app = dashboard, dashboard.app
    app.table_state["auto_groups"] = False
    # A background refresh publishes a batch and two independent submissions
    # after the first screen has already been painted.
    ui.store.jobs.extend(Job(str(jid), "new-batch", "cpu", "PENDING",
                             submit="2026-10-10T12:01:00", user="tester", account="lab", cluster="local",
                             command="/project/train.sh", workdir="/project") for jid in range(10, 14))
    ui.store.jobs.extend(Job(str(jid), "separate-" + str(jid), "cpu", "PENDING",
                             submit="2026-10-10T12:02:00", user="tester", cluster="local") for jid in (14, 15))
    ui.store.jobs[0].state = "RUNNING"
    ui.store.jobs[0].cpus = 4
    for step in range(30):
        ui.store.record("1", {"k": "live", "t": 180 + step, "cpu": .2 + step / 60,
                              "rss": 1024 ** 3})
    app.job_panel_state.update(mode="analytics", analytics_view="job")

    def redraw():
        draw(ui, width=300, height=100)
        assert app.tab == "jobs" and app.mode == "main"

    def click_control(key):
        from test_job_list_menu import choose
        action = key.removeprefix("grouping:")
        position = next((y, value["left"]) for y, kind, value in ui.hits
                        if kind == "control" and value["id"] == "joblist:jobs:groups")
        send(ui, position, "press", route)
        send(ui, position, "release", route)
        def menu_send(point, button):
            send(ui, point, button, route)
        if action in ("auto", "undo", "clear"):
            choose(app, (300, 100), "manage", send=menu_send)
        if action == "clear":
            choose(app, (300, 100), "remove", send=menu_send)
        choose(app, (300, 100), action, send=menu_send)
        redraw()

    def index():
        return Groups.registry(app).ensure(ui.store.snapshot())

    redraw()
    original_manual = index().for_job("1").id
    assert index().for_job("10") is None
    click_control("grouping:auto")
    automatic = index().for_job("10")
    assert automatic is not None and Groups.registry(app).is_collapsed(automatic)
    assert index().for_job("1").id == original_manual

    # A manual choice made after Auto must survive Undo Auto.
    send(ui, row(ui, "14"), "press", route)
    send(ui, row(ui, "15"), "drag", route)
    send(ui, row(ui, "15"), "release", route)
    assert app.marks == {"14", "15"}
    app.handle("g")
    redraw()
    new_manual = index().for_job("14").id
    click_control("grouping:undo")
    assert index().for_job("10") is None
    assert index().for_job("14").id == new_manual
    assert index().for_job("1").id == original_manual

    click_control("grouping:auto")
    click_control("grouping:expand")
    assert not app.table_state["collapsed"]
    click_control("grouping:collapse")
    assert all(Groups.registry(app).is_collapsed(group) for group in index().groups.values())
    click_control("grouping:expand")
    assert not app.table_state["collapsed"]
    before_clear = deepcopy(app.table_state["manual_groups"])
    click_control("grouping:clear")
    assert not index().groups and not app.table_state["auto_groups"]
    app.run_command("grouping restore")
    redraw()
    assert app.table_state["manual_groups"] == before_clear

    # Shift-click and plain dragging share the same rows, even after grouping
    # has repeatedly replaced their hit maps and moved Details to another job.
    send(ui, row(ui, "3"), "press", route)
    send(ui, row(ui, "3"), "release", route)
    redraw()
    send(ui, row(ui, "6"), "press", route, shift=True)
    send(ui, row(ui, "6"), "release", route, shift=True)
    assert app.marks == {"3", "6"}
    redraw()
    send(ui, row(ui, "6"), "press", route)
    send(ui, row(ui, "3"), "drag", route)
    send(ui, row(ui, "3"), "release", route)
    assert app.marks == {"3", "6"} and not Queue.active(app)

    # The explicit membership gesture must not be mistaken for queue ordering.
    redraw()
    send(ui, row(ui, "3"), "press", route, alt=True)
    assert Membership.active(app) and not Queue.active(app)
    send(ui, row(ui, "7"), "drag", route, alt=True)
    send(ui, row(ui, "7"), "release", route, alt=True)
    redraw()
    assert index().for_job("3").members == ("3", "6", "7", "8")
    assert index().for_job("4").members == ("4", "5")
    assert not app.marks and not ui.calls

    send(ui, row(ui, "1"), "press", route)
    send(ui, row(ui, "2"), "drag", route)
    send(ui, row(ui, "2"), "release", route)
    redraw()
    # Return the Details cursor to the sampled job without clearing its marks.
    send(ui, row(ui, "1"), "press", route)
    send(ui, row(ui, "1"), "release", route)
    redraw()
    assert app.selected_id == "1" and app.marks == {"1", "2"}
    original_width = app.table_state["widths"].get("jobs", {}).get("name")
    saved = []
    monkeypatch.setattr(app, "save", lambda: saved.append(table_ui.save(app)))

    def name_grip():
        return min((value for value in Columns.initialize(app)["grips"]
                    if value.table == "jobs" and value.column == "name"), key=lambda value: value.y)

    handle = name_grip()
    send(ui, (handle.y, handle.left), "press", route)
    with monkeypatch.context() as guard:
        guard.setattr(ui.store, "snapshot", lambda: pytest.fail("Resize motion copied Store"))
        send(ui, (handle.y + 2, handle.left + 8), "drag", route)
    assert Columns.active(app) and not Selection.active(app) and not saved
    app.handle("esc")
    send(ui, (handle.y + 2, handle.left + 8), "release", route)
    assert app.table_state["widths"].get("jobs", {}).get("name") == original_width
    redraw()
    handle = name_grip()
    send(ui, (handle.y, handle.left), "press", route)
    send(ui, (handle.y, handle.left + 8), "drag", route)
    send(ui, (handle.y, handle.left + 8), "release", route)
    assert len(saved) == 1 and not Columns.active(app)
    redraw()
    assert app.selected_id == "1" and app.marks == {"1", "2"}

    # A fresh graph gesture must own its full stream after a cancelled resize,
    # without reviving range selection or losing the previously marked jobs.
    plot = next(plot for plot in Charts.initialize(app)["plots"] if plot.key[0] == "resource-series")
    first = plot.visible.top + 1, plot.visible.left + 3
    last = plot.visible.bottom - 2, plot.visible.right - 3
    with monkeypatch.context() as guard:
        guard.setattr(ui.store, "snapshot", lambda: pytest.fail("Chart pointer copied Store"))
        send(ui, first, "press", route)
        assert Charts.active(app) and not Columns.active(app) and not Selection.active(app)
        send(ui, last, "drag", route)
        send(ui, last, "release", route)
    assert Charts.bounds(app, plot.key) is not None
    redraw()
    send(ui, first, "right", route)
    assert Charts.bounds(app, plot.key) is None
    assert app.marks == {"1", "2"} and app.selected_id == "1"

    # Queue evidence from this revised layout must be discarded after another
    # background refresh moves its target, including the terminal's late release.
    redraw()
    source = index().for_job("3").id
    target = index().for_job("14").id
    target_point = grip(ui, target)
    send(ui, grip(ui, source), "press", route)
    with monkeypatch.context() as guard:
        guard.setattr(ui.store, "snapshot", lambda: pytest.fail("Queue motion copied Store"))
        send(ui, target_point, "drag", route)
    assert Queue.active(app) and not Charts.active(app)
    ui.store.jobs[:] = [job for job in ui.store.jobs if job.id not in ("9", "10", "11", "12", "13")]
    redraw()
    assert not Queue.active(app)
    send(ui, target_point, "release", route)
    send(ui, target_point, "release", route)
    assert not ui.calls
    assert app.tab == "jobs" and app.mode == "main" and app.selected_id == "1"
    assert app.marks == {"1", "2"}
    assert not any((Columns.active(app), Queue.active(app), Membership.active(app), Selection.active(app), Charts.active(app)))
