"""Published-geometry job moves retain exact intent across pointer reports."""
import pytest

from raven import job_group_drag as D, job_groups as G, job_selection as S, manual_job_groups as M
from raven.config import Config
from raven.controller import App
from raven.layout import Glyphs, row_text
from raven.model import Job, Store
from raven.views import Views


@pytest.fixture
def screen():
    store = Store(persist=False)
    store.jobs = [Job(str(i), "different-" + str(i), "cpu", "RUNNING",
                      submit=f"2026-01-{i:02d}T00:00:00") for i in range(1, 13)]
    app = App(store, None, None, Config({"animations": False}), "test", interactive=False)
    app.width, app.height = 180, 52
    app.save = lambda: None
    views = Views(Glyphs(True), app.cfg)
    app.views_ref = views
    group = M.create(app, store.snapshot(), ("7", "9"))
    assert group.changed
    app.table_state["groups"] = True
    views.compose(store.snapshot(), app, app.width, app.height)
    yield app, store, views, group.group_id
    if app.research:
        app.research.close()


def point(app, identifier, scope=None):
    frame = D.initialize(app)["frame"]
    for y, rows in frame["rows"].items():
        for source in rows:
            if source["pointed"] == identifier and (scope is None or source["scope"] == scope):
                for x in range(source["rect"][1], source["rect"][3]):
                    if D.source_at(app, y, x):
                        return y, x
    pytest.fail(f"No published job {identifier} in {scope}")


def move(app, y, x, button="left", shift=False, alt=True):
    return D.handle_mouse(app, y, x, button, shift=shift, alt=alt)


def mark(app, *identifiers):
    app.marks = set(identifiers)
    S._bind_marks(app, identifiers)


def test_dragging_marked_jobs_adds_exact_members_in_natural_order(screen):
    app, store, _, gid = screen
    mark(app, "1", "3", "10")
    assert move(app, *point(app, "1"), "press")
    assert move(app, *point(app, "7"), "drag")
    assert G.registry(app).index.groups[gid].members == ("7", "9")
    assert move(app, *point(app, "7"), "release")
    group = G.registry(app).ensure(store.snapshot()).groups[gid]
    assert group.members == ("1", "3", "7", "9", "10")
    assert not app.marks and not D.active(app)


def test_initial_automatic_cursor_does_not_replace_range_selection(screen):
    app, _, _, _ = screen
    first = app.visible_ids[0]
    assert app.cursor["jobs"] == 0
    assert not move(app, *point(app, first), "press", alt=False)
    assert not D.pending(app) and not D.active(app)
    assert not move(app, *point(app, "3"), "drag")
    assert not move(app, *point(app, "3"), "release")
    assert D.initialize(app)["armed"] is None


def test_plain_click_never_arms_move_but_alt_moves_one_without_unrelated_marks(screen):
    app, store, _, gid = screen
    mark(app, "2", "4")
    source = point(app, "1")
    assert not move(app, *source, "press", alt=False)
    assert S.handle_mouse(app, *source, "press")
    assert not move(app, *source, "release")
    assert S.handle_mouse(app, *source, "release")
    assert not D.active(app) and D.initialize(app)["armed"] is None
    assert move(app, *source, "press")
    assert D.initialize(app)["capture"]["source"]["targets"] == ("1",)
    move(app, *point(app, "7"), "drag")
    move(app, *point(app, "7"), "release")
    assert G.registry(app).ensure(store.snapshot()).groups[gid].members == ("1", "7", "9")
    assert app.marks == {"2", "4"}


def test_shift_press_retains_range_even_when_marked(screen):
    app, _, _, _ = screen
    mark(app, "1", "3")
    assert not move(app, *point(app, "1"), "press", shift=True)
    assert not D.active(app)
    assert S.handle_mouse(app, *point(app, "1"), "press", shift=True)
    assert S.handle_mouse(app, *point(app, "4"), "drag", shift=True)
    assert app.marks >= {"1", "2", "3", "4"}


@pytest.mark.parametrize("button", ["right", "wheel-down", "press", "left"])
def test_interrupting_buttons_cancel_without_mutation_or_mark_loss(screen, button):
    app, store, _, gid = screen
    mark(app, "1", "3")
    move(app, *point(app, "1"), "press")
    move(app, *point(app, "7"), "drag")
    assert move(app, 0, 0, button)
    assert not D.active(app) and app.marks == {"1", "3"}
    assert move(app, *point(app, "7"), "release")
    assert G.registry(app).ensure(store.snapshot()).groups[gid].members == ("7", "9")


@pytest.mark.parametrize("change", ["resize", "tab", "sort", "filter", "modal", "revision"])
def test_changed_context_cancels_before_a_late_drop(screen, change):
    app, store, _, gid = screen
    mark(app, "1", "3")
    source, target = point(app, "1"), point(app, "7")
    move(app, *source, "press")
    if change == "resize":
        app.width -= 1
    elif change == "tab":
        app.tab = "history"
    elif change == "sort":
        app.reverse["jobs"] = not app.reverse.get("jobs", False)
    elif change == "filter":
        app.filter = "missing"
    elif change == "modal":
        app.mode = "help"
    else:
        app.manual_job_groups_revision += 1
    assert move(app, *target, "release")
    assert not D.active(app) and app.marks == {"1", "3"}
    assert G.registry(app).ensure(store.snapshot()).groups[gid].members == ("7", "9")


def test_hover_and_drag_do_not_snapshot_infer_or_resolve_sources(screen, monkeypatch):
    app, store, _, _ = screen
    mark(app, "1", "3")
    source, target = point(app, "1"), point(app, "7")
    move(app, *source, "press")
    monkeypatch.setattr(store, "snapshot", lambda: pytest.fail("pointer copied Store"))
    monkeypatch.setattr(G.Registry, "ensure", lambda *_: pytest.fail("pointer inferred groups"))
    monkeypatch.setattr(app, "job_record", lambda *_: pytest.fail("pointer resolved a job"))
    for _ in range(100):
        assert move(app, *target, "drag")
        assert move(app, *source, "motion")
    assert D.handle_key(app, "esc")
    assert app.marks == {"1", "3"}


@pytest.mark.parametrize("which", ["source", "destination"])
def test_reused_id_without_repaint_is_rejected_at_release(screen, which):
    app, store, _, gid = screen
    mark(app, "1", "3")
    move(app, *point(app, "1"), "press")
    target = point(app, "7")
    move(app, *target, "drag")
    changed = "1" if which == "source" else "7"
    next(record for record in store.jobs if record.id == changed).submit = "2026-02-01T00:00:00"
    move(app, *target, "release")
    assert app.marks == {"1", "3"}
    state = app.table_state["manual_groups"]
    assert {member[0] for member in state["groups"][0]["members"]} == {"7", "9"}


def test_repaint_attempt_change_cancels_and_eats_orphan_release(screen):
    app, store, views, _ = screen
    mark(app, "1", "3")
    move(app, *point(app, "1"), "press")
    target = point(app, "7")
    move(app, *target, "drag")
    store.jobs[0].submit = "2026-02-01T00:00:00"
    views.compose(store.snapshot(), app, app.width, app.height)
    assert not D.active(app)
    assert move(app, *target, "release")
    assert app.marks == {"1", "3"}


def test_feedback_is_theme_roles_only_and_does_not_change_copy_rows(screen):
    app, _, _, _ = screen
    mark(app, "1", "3")
    rows = app.frame_rows
    original = [list(row) for row in rows]
    move(app, *point(app, "1"), "press")
    target = point(app, "7")
    move(app, *target, "drag")
    output = D.feedback(app, rows)
    assert rows == original and output is not rows
    assert "2 jobs ->" in row_text(output[-1])
    assert "Release to add" in row_text(output[-1])
    assert any("bg:track" in style for _, style in output[target[0]])
    assert all("#" not in style for _, style in output[-1])


def test_disclosure_details_and_scrollbar_cells_cannot_start_moves(screen):
    app, _, _, _ = screen
    mark(app, "7")
    row = point(app, "7")[0]
    disclosure = next(value for y, kind, value in app.last_hits
                      if y == row and kind == "control" and value["id"].startswith("jobgroup:"))
    assert D.source_at(app, row, disclosure["left"]) is None
    main = app.workspace_main_rect
    assert D.source_at(app, row, main.x + main.width + 3) is None
    assert D.source_at(app, row, main.x + main.width - 1) is None


@pytest.mark.parametrize("tab", ["analytics", "deps", "log", "research"])
@pytest.mark.parametrize("dock", ["left", "right", "top", "bottom"])
def test_all_history_browser_docks_publish_exact_rows_and_drop_groups(screen, tab, dock):
    from raven import history_browser as H
    app, store, views, gid = screen
    app.tab = tab
    H._view(app).update(dock=dock, ratio=40)
    views.compose(store.snapshot(), app, app.width, app.height)
    source = point(app, "12", "history:" + tab)
    target = point(app, "9", "history:" + tab)
    mark(app, "11", "12")
    assert D.source_at(app, *source)["scope"] == "history:" + tab
    assert move(app, *source, "press")
    assert move(app, *target, "drag")
    assert move(app, *target, "release")
    assert G.registry(app).ensure(store.snapshot()).groups[gid].members == ("7", "9", "11", "12")


def test_stationary_marked_row_click_retains_details_selection(screen):
    app, _, _, _ = screen
    mark(app, "3", "4")
    source = point(app, "3")
    assert move(app, *source, "press")
    assert move(app, *source, "release")
    assert app.selected_id == "3" and app.marks == {"3", "4"}
    assert app.visible_ids[app.cursor["jobs"]] == "3"
    assert not D.active(app) and not S.active(app)


def test_repainting_target_under_stationary_pointer_cannot_choose_a_new_group(screen):
    app, store, views, gid = screen
    mark(app, "1", "3")
    move(app, *point(app, "1"), "press")
    target = point(app, "7")
    move(app, *target, "drag")
    # Insert a new row that shifts the target below the unchanged pointer.
    store.jobs.insert(0, Job("0", "new", "cpu", "RUNNING", submit="2026-03-01T00:00:00"))
    views.compose(store.snapshot(), app, app.width, app.height)
    assert not D.active(app)
    assert move(app, *target, "release")
    assert G.registry(app).ensure(store.snapshot()).groups[gid].members == ("7", "9")


def test_drop_on_plain_row_does_not_create_an_implicit_group(screen):
    app, _, _, _ = screen
    mark(app, "1", "3")
    move(app, *point(app, "1"), "press")
    target = point(app, "4")
    move(app, *target, "drag")
    move(app, *target, "release")
    assert len(app.table_state["manual_groups"]["groups"]) == 1
    assert app.marks == {"1", "3"}


def test_browser_alt_move_works_after_click_switches_advisor_to_job_series(screen):
    app, store, views, gid = screen
    app.tab, app.analytics_view = "analytics", "advisor"
    views.compose(store.snapshot(), app, app.width, app.height)
    source = point(app, "12", "history:analytics")
    app.click(*source, app.last_hits, button="press")
    assert app.analytics_view == "job" and app.selected_id == "12"
    D.tick(app)  # Maintenance runs before the replacement frame is composed.
    app.click(*source, app.last_hits, button="release")
    D.tick(app)
    views.compose(store.snapshot(), app, app.width, app.height)
    source = point(app, "12", "history:analytics")
    target = point(app, "9", "history:analytics")
    app.click(*source, app.last_hits, button="press", alt=True)
    assert D.active(app)
    app.click(*target, app.last_hits, button="drag")
    app.click(*target, app.last_hits, button="release")
    assert G.registry(app).ensure(store.snapshot()).groups[gid].members == ("7", "9", "12")


def test_plain_browser_click_after_view_change_never_starts_move(screen):
    app, store, views, _ = screen
    app.tab, app.analytics_view = "analytics", "advisor"
    views.compose(store.snapshot(), app, app.width, app.height)
    source = point(app, "12", "history:analytics")
    app.click(*source, app.last_hits, button="press")
    app.click(*source, app.last_hits, button="release")
    store.jobs[-1].submit = "2026-02-01T00:00:00"
    views.compose(store.snapshot(), app, app.width, app.height)
    app.click(*point(app, "12", "history:analytics"), app.last_hits, button="press")
    assert not D.active(app)
