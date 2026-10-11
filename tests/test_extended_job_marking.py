"""Exact row marking in docked browsers and the Advisor document."""
import pytest

from tower import history_browser as H, job_selection as S, layout as L
from tower.config import Config
from tower.controller import App
from tower.model import Job, Store
from tower.views import Views


@pytest.fixture
def dashboard():
    store = Store(persist=False)
    store.jobs = [Job(str(i), "task " + str(i), "cpu", "RUNNING",
                      submit="2026-01-01T00:00:00") for i in range(1, 25)]
    app = App(store, None, None, Config({"animations": False}), "test", interactive=False)
    app.width, app.height, app.body_origin = 160, 48, 6
    app.save = lambda: None
    views = Views(L.Glyphs(True), app.cfg)
    app.views_ref = views
    yield app, store, views
    if app.research:
        app.research.close()


def browser_frame(dashboard, *, tab="analytics", dock="right"):
    app, store, views = dashboard
    app.tab = tab
    H._view(app)["dock"] = dock
    H.wrap_render(views, store.snapshot(), app, app.width, 30,
                  lambda width, height: ([[('DATA', '')]], []))
    return H.initialize(app)


def point(app, jobid):
    rect = app.history_browser_rect
    row, _, hit = next(hit for hit in H.initialize(app)["frame"]["hits"]
                       if hit[2]["id"] == f"history:{app.tab}:job:{jobid}")
    return rect.y + row, rect.x + hit["left"] + 5


@pytest.mark.parametrize("tab", H.TABS)
def test_browser_space_marks_exact_row_without_loading_other_sources(dashboard, tab, monkeypatch):
    app, store, _ = dashboard
    state = browser_frame(dashboard, tab=tab)
    identifier = state["items"][0].record.id
    H.activate(app, identifier)
    monkeypatch.setattr(app, "open_log", lambda *_: pytest.fail("Mark opened a log"))
    monkeypatch.setattr(store, "snapshot", lambda: pytest.fail("Mark copied Store"))
    assert H.handle_key(app, "space")
    assert app.marks == {identifier}
    assert H.handle_key(app, "space")
    assert app.marks == set()


def test_horizontal_browser_drag_selects_distinct_columns_on_same_line(dashboard):
    app, _, _ = dashboard
    state = browser_frame(dashboard, dock="top")
    ids = [item.record.id for item in state["items"]]
    first, last = point(app, ids[0]), point(app, ids[2])
    assert first[0] == last[0]
    assert H.handle_mouse(app, *first, "press")
    assert H.handle_mouse(app, *last, "drag")
    assert app.marks == set(ids[:3])
    assert H.handle_mouse(app, *last, "release")
    assert app.analytics_job == ids[2]


@pytest.mark.parametrize("tab", H.TABS)
def test_browser_motion_does_not_resolve_records_or_open_files(dashboard, tab, monkeypatch):
    app, store, _ = dashboard
    state = browser_frame(dashboard, tab=tab)
    ids = [item.record.id for item in state["items"]]
    assert H.handle_mouse(app, *point(app, ids[0]), "press")
    monkeypatch.setattr(app, "job_record", lambda *_: pytest.fail("Motion resolved a source"))
    monkeypatch.setattr(app, "open_log", lambda *_: pytest.fail("Motion opened a file"))
    monkeypatch.setattr(store, "snapshot", lambda: pytest.fail("Motion copied Store"))
    assert H.handle_mouse(app, *point(app, ids[2]), "drag")
    assert app.marks == set(ids[:3])
    assert S.handle_key(app, "esc")
    assert not app.marks


@pytest.mark.parametrize("invalid", ["resize", "dock", "hidden", "reorder", "modal"])
def test_browser_capture_invalidates_before_a_replacement_row_can_be_marked(dashboard, invalid):
    app, _, _ = dashboard
    state = browser_frame(dashboard)
    ids = [item.record.id for item in state["items"]]
    start, end = point(app, ids[0]), point(app, ids[2])
    H.handle_mouse(app, *start, "press")
    if invalid == "resize":
        app.width -= 1
    elif invalid == "dock":
        H._view(app)["dock"] = "top"
    elif invalid == "hidden":
        H._view(app)["enabled"] = False
    elif invalid == "reorder":
        state["selection_ids"] = tuple(reversed(state["selection_ids"]))
    else:
        app.mode = "help"
    assert H.handle_mouse(app, *end, "release")
    assert not app.marks and not S.active(app)


def test_repaint_keeps_mark_bound_to_original_attempt_until_explicit_reselect(dashboard):
    app, store, views = dashboard
    views.compose(store.snapshot(), app, app.width, app.height)
    identifier = app.visible_ids[0]
    assert S.handle_key(app, "space")
    original = S.initialize(app)["mark_tokens"][identifier]
    record = next(record for record in store.jobs if record.id == identifier)
    record.submit = "2026-01-02T00:00:00"
    views.compose(store.snapshot(), app, app.width, app.height)
    assert S.initialize(app)["published_tokens"][identifier] != original
    assert S.context(app)["tokens"][identifier] == original
    app.cursor["jobs"] = app.visible_ids.index(identifier)
    S.handle_key(app, "space")  # deliberate removal
    app.cursor["jobs"] = app.visible_ids.index(identifier)
    S.handle_key(app, "space")  # deliberate selection of the new attempt
    assert S.context(app)["tokens"][identifier] != original


def test_snapshot_attempt_change_cancels_capture_even_when_ids_are_unchanged(dashboard):
    app, store, views = dashboard
    views.compose(store.snapshot(), app, app.width, app.height)
    identifier = app.visible_ids[0]
    y = next(y for y, kind, value in app.last_hits if kind == "job" and value == identifier)
    assert S.handle_mouse(app, y, 8, "press")
    next(record for record in store.jobs if record.id == identifier).submit = "2026-01-02T00:00:00"
    views.compose(store.snapshot(), app, app.width, app.height)
    assert not S.active(app)
    # The invalidated gesture owns its late release so another control cannot
    # interpret it as the completion of an unrelated operation.
    assert S.handle_mouse(app, y + 1, 8, "release")
    assert not app.marks


def test_disabling_groups_cannot_reuse_an_old_identity_check_for_mutated_snapshot(dashboard):
    app, store, views = dashboard
    app.table_state["groups"] = True
    snapshot = store.snapshot()
    views.compose(snapshot, app, app.width, app.height)
    S.handle_key(app, "space")
    S.handle_key(app, "space")
    identifier = sorted(app.marks)[0]
    original = S.initialize(app)["published_tokens"][identifier]
    app.table_state["groups"] = False
    next(record for record in snapshot["jobs"] if record.id == identifier).submit = "2026-01-02T00:00:00"
    # Reuse the very same snapshot object after the inference frame is disabled.
    views.compose(snapshot, app, app.width, app.height)
    assert S.initialize(app)["published_tokens"][identifier] != original
    assert S.context(app)["tokens"][identifier] == original
    app.handle("g")
    assert not app.table_state.get("manual_groups", {}).get("groups")
    assert "changed" in app.message.lower()


@pytest.mark.parametrize("guard", ["legacy_text", "explicit_text", "chart", "slider", "details", "modal"])
def test_group_context_does_not_leak_job_marks_into_other_input_owners(dashboard, guard):
    app, store, views = dashboard
    views.compose(store.snapshot(), app, app.width, app.height)
    assert S.context(app) is not None
    if guard == "legacy_text":
        app.sel_anchor = 0
    elif guard == "explicit_text":
        from tower.text_selection import initialize
        initialize(app)["explicit"] = True
    elif guard == "chart":
        app.chart_interaction_state["capture"] = {"active": True}
    elif guard == "slider":
        app.metric_live_state["capture"] = {"active": True}
    elif guard == "details":
        app.layout_state.focus = "details"
    else:
        app.mode = "confirm"
    assert S.context(app) is None


def test_advisor_direct_click_selects_a_job_and_keeps_document_view(dashboard):
    app, store, views = dashboard
    app.tab, app.analytics_view = "analytics", "advisor"
    rows, hits = views.compose(store.snapshot(), app, app.width, app.height)
    y, _, control = next(hit for hit in hits if hit[1] == "control" and hit[2]["id"].startswith("advisor-job:"))
    identifier = control["label"]
    app.click(y, control["left"] + 5, hits, button="left")
    assert app.analytics_view == "advisor" and app.analytics_job == identifier
    assert S.context(app)["scope"] == "analytics:advisor"
    assert not S.active(app)


def test_advisor_rows_cannot_claim_controls_in_an_adjacent_pane(dashboard):
    app, store, views = dashboard
    app.tab, app.analytics_view = "analytics", "advisor"
    _, hits = views.compose(store.snapshot(), app, app.width, app.height)
    y, _, advisor = next(hit for hit in hits if hit[1] == "control" and hit[2]["id"].startswith("advisor-job:"))
    assert advisor["left"] > 10  # default adaptive Details column
    # Navigation and diagnostic buttons wrap independently from Advisor rows.
    # Assert ownership against the controls actually adjacent in this frame,
    # including the docked job browser, rather than fixing one header's y.
    adjacent = [value for row, kind, value in hits if row == y and kind == "control"
                and (value["right"] <= advisor["left"] or value["left"] >= advisor["right"])]
    assert adjacent
    for control in adjacent:
        assert not S.advisor_pointer(app, y, control["left"])
        assert not S.advisor_pointer(app, y, control["right"] - 1)
    assert not S.advisor_pointer(app, y, advisor["left"] - 1)
    assert not S.advisor_pointer(app, y, advisor["right"])
    assert S.advisor_pointer(app, y, advisor["left"] + 5)


@pytest.mark.parametrize("density", ["compact", "comfortable"])
def test_advisor_keyboard_control_activation_selects_without_opening_another_view(dashboard, density):
    app, store, views = dashboard
    app.tab, app.analytics_view, app.layout_state.density = "analytics", "advisor", density
    _, hits = views.compose(store.snapshot(), app, app.width, app.height)
    y, _, control = next(hit for hit in hits if hit[1] == "control" and hit[2]["id"].startswith("advisor-job:"))
    app.click(y, control["left"] + 5, hits, button="motion")
    app.handle("f8")
    assert app.interaction_state["focused"] == control["id"]
    app.handle("enter")
    assert app.analytics_view == "advisor" and app.analytics_job == control["label"]
    assert app.tab == "analytics" and app.mode == "main" and not app.quit
