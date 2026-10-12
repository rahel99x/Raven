"""Subcell pointer ink is cosmetic; event selection and document work stay exact."""
from dataclasses import replace
import math
from types import SimpleNamespace

import pytest

from raven import chart_interaction as C, layout as L, palette as P, screen, selector_glyphs as G


@pytest.fixture
def clock(monkeypatch):
    value = SimpleNamespace(now=0.)
    monkeypatch.setattr(C.time, "monotonic", lambda: value.now)
    return value


@pytest.fixture
def app(clock):
    value = SimpleNamespace(mode="main", tab="analytics", width=120, height=40,
                            selected_id="101", analytics_job="101", analytics_view="job",
                            toolbar_state={}, project_state={}, job_panel_state={}, analysis_state={},
                            research=SimpleNamespace(generation=3), theme="default", animations_enabled=True,
                            cfg={"animations": True}, messages=[])
    value.say = value.messages.append
    value.views_ref = SimpleNamespace(g=L.Glyphs(False))
    C.begin_frame(value, 120, 40)
    return value


def plot(app, identity=("job", "101", "CPU")):
    C.record(app, identity, {"plot_rect": (2, 10, 12, 70), "x_bounds": (0., 100.), "y_bounds": (0., 200.)})
    return C.publish(app, 120, 40)[-1]


def marks(app, **kwargs):
    return {(y, x): (row[0][0], row[0][1]) for y, x, row in C.feedback(app, **kwargs)}


@pytest.mark.parametrize("elapsed,expected", [(.0, G.Cell(6, 31, 0, 0)),
                                             (.006, G.Cell(6, 31, 0, 0)),
                                             (.012, G.Cell(6, 31, 1, 0)),
                                             (.018, G.Cell(6, 31, 1, 0)),
                                             (.024, G.Cell(6, 31, 2, 1)),
                                             (.03, G.Cell(6, 31, 2, 1))])
def test_received_motion_follows_latest_cell_immediately_and_only_eases_subcell_dots(app, clock, elapsed, expected):
    item = plot(app)
    C.hover(app, 5, 30)
    C.hover(app, 6, 31)
    clock.now = elapsed
    assert C._visual_pointer(app, item) == expected
    result = marks(app)
    keys = {(expected.row, x) for x in range(item.visible.left, item.visible.right)}
    keys |= {(y, expected.column) for y in range(item.visible.top, item.visible.bottom)}
    assert set(result) == keys
    assert result[(expected.row, expected.column)][0] == G.glyph(expected.y_slot, expected.x_slot, horizontal=True, vertical=True)
    assert result[(expected.row, item.visible.left)][0] == G.HORIZONTAL_GLYPHS[expected.y_slot]
    assert result[(item.visible.top, expected.column)][0] == G.VERTICAL_GLYPHS[expected.x_slot]
    assert C.initialize(app)["pointer"] == (6, 31)
    assert all(item.visible.contains(y, x) for y, x in result)


def test_reversal_follows_new_cell_entry_edge_and_duplicate_reports_do_not_restart_motion(app, clock):
    item = plot(app)
    C.hover(app, 5, 30)
    C.hover(app, 9, 60)
    clock.now = .02
    C.hover(app, 3, 20)
    visual = C.initialize(app)["visual"]
    assert visual["start"] == (3.875, 20.75) and visual["target"] == (3.5, 20.5)
    assert visual["started"] == .02
    assert C._visual_pointer(app, item) == G.Cell(3, 20, 3, 1)
    clock.now = .04
    C.hover(app, 3, 20)
    assert C.initialize(app)["visual"]["started"] == .02
    cell = C._visual_pointer(app, item)
    assert (cell.row, cell.column) == (3, 20)
    clock.now = .045
    assert C._visual_pointer(app, item) == G.Cell(3, 20, 2, 1)
    assert math.isinf(C.next_deadline(app))


def test_latest_reports_replace_visual_target_without_replaying_an_input_queue(app, clock):
    item = plot(app)
    C.hover(app, 3, 15)
    for index in range(1000):
        C.hover(app, 3 + index % 7, 15 + index % 45)
    target = (3 + 999 % 7, 15 + 999 % 45)
    state = C.initialize(app)
    assert state["pointer"] == target and state["visual"]["target"] == (target[0] + .5, target[1] + .5)
    assert state["revision"] == 0 and not state["zoom"]
    clock.now = .081
    assert C._visual_pointer(app, item) == G.locate(target[0] + .5, target[1] + .5)
    assert math.isinf(C.next_deadline(app))


def test_new_press_snaps_its_visual_anchor_before_a_previous_hover_animation_finishes(app, clock):
    item = plot(app)
    C.hover(app, 5, 30)
    C.hover(app, 9, 60)
    clock.now = .02
    assert C.handle_mouse(app, 3, 15, button="press")
    state = C.initialize(app)
    assert state["capture"]["start"] == state["capture"]["current"] == (3, 15)
    assert state["visual"]["start"] == state["visual"]["target"] == (3.5, 15.5)
    assert C._visual_pointer(app, item) == G.Cell(3, 15, 2, 1)
    assert math.isinf(C.next_deadline(app))


@pytest.mark.parametrize("elapsed", [0., .001, .04, .08])
def test_raw_press_release_zoom_is_independent_of_any_unfinished_visual_interpolation(app, clock, elapsed):
    item = plot(app)
    C.handle_mouse(app, 4, 18, button="press", shift=True)
    C.handle_mouse(app, 9, 56, button="drag")
    assert C.initialize(app)["capture"]["current"] == (9, 56)
    clock.now = elapsed
    C.feedback(app)
    C.handle_mouse(app, 9, 56, button="release")
    bounds = C.bounds(app, item.key)
    assert bounds["x"] == pytest.approx((100 * 8 / 59, 100 * 46 / 59))
    assert bounds["y"] == pytest.approx((200 * 2 / 9, 200 * 7 / 9))
    assert not C.autofit(app, item.key) and not C.active(app)


@pytest.mark.parametrize("start,end", [((4, 18), (9, 56)), ((9, 56), (4, 18)),
                                     ((4, 56), (9, 18)), ((9, 18), (4, 56))])
def test_all_drag_quadrants_draw_a_bounded_rectangle_with_union_glyph_corners(app, clock, start, end):
    item = plot(app)
    C.handle_mouse(app, *start, button="press", shift=True)
    C.handle_mouse(app, *end, button="drag")
    clock.now = .081
    result = marks(app)
    top, bottom = sorted((start[0], end[0]))
    left, right = sorted((start[1], end[1]))
    keys = {(y, x) for y in (top, bottom) for x in range(left, right + 1)}
    keys |= {(y, x) for x in (left, right) for y in range(top, bottom + 1)}
    assert set(result) == keys
    for y, x in ((top, left), (top, right), (bottom, left), (bottom, right)):
        assert result[(y, x)][0] == "⢼"
    assert all(item.visible.contains(y, x) for y, x in result)
    assert C.bounds(app, item.key) is None


def test_intermediate_rectangle_preserves_different_top_and_bottom_subcell_dot_rows(app, clock):
    plot(app)
    C.handle_mouse(app, 4, 18, button="press", shift=True)
    C.handle_mouse(app, 9, 56, button="drag")
    clock.now = .006
    result = marks(app)
    cursor = C._visual_pointer(app, C.initialize(app)["capture"]["plot"])
    assert cursor.y_slot == 0
    assert result[(4, 18)][0] == "⢼"
    assert result[(cursor.row, cursor.column)][0] == G.glyph(cursor.y_slot, cursor.x_slot, horizontal=True, vertical=True)
    assert result[(4, 18)][0] != result[(cursor.row, cursor.column)][0]


@pytest.mark.parametrize("theme", P.THEME_NAMES)
@pytest.mark.parametrize("source_style", ["bg:surface", "chart-2+bg:surface-sunken", "chart-3+rev", "sel"])
def test_every_fine_glyph_uses_one_cell_active_accent_and_exact_painted_background(app, clock, theme, source_style):
    app.theme = theme
    item = plot(app)
    rows = [[(" " * 120, source_style)]] * 40
    C.hover(app, 5, 30)
    C.hover(app, 6, 31)
    clock.now = .03
    result = C.feedback(app, rows=rows)
    source = P.resolve(P.cell_style(source_style, theme), theme)
    expected_bg = source.foreground if "rev" in source.flags else source.background
    for y, x, row in result:
        char, style = row[0]
        assert L.vlen(char) == len(char) == 1 and item.visible.contains(y, x)
        resolved = P.resolve(P.cell_style(style, theme), theme)
        assert resolved.foreground == P.resolve("accent", theme).foreground
        assert resolved.background == expected_bg
        assert "bold" not in style and "rev" not in style
        if theme == "reader":
            assert char in ".+"
        else:
            assert 0x2800 <= ord(char) <= 0x28FF


@pytest.mark.parametrize("disabled", ["ascii", "reader", "animations", "config"])
def test_ascii_reader_and_disabled_animations_snap_without_cosmetic_deadlines(app, clock, disabled):
    if disabled == "ascii":
        C.initialize(app)["ascii"] = True
    elif disabled == "reader":
        app.theme = "reader"
    elif disabled == "animations":
        app.animations_enabled = False
    else:
        app.cfg["animations"] = False
    item = plot(app)
    C.hover(app, 5, 30)
    C.hover(app, 6, 31)
    assert C._visual_pointer(app, item) == G.Cell(6, 31, 2, 1)
    assert math.isinf(C.next_deadline(app))
    chars = {char for _, _, row in C.feedback(app) for char, _ in row}
    if disabled in ("ascii", "reader"):
        assert chars == {".", "+"}
    else:
        assert chars <= set("⠤⢸⢼")


def test_disabling_then_reenabling_animation_does_not_replay_old_motion(app, clock):
    item = plot(app)
    C.hover(app, 5, 30)
    C.hover(app, 9, 60)
    clock.now = .02
    app.animations_enabled = False
    assert C._visual_pointer(app, item) == G.Cell(9, 60, 2, 1)
    assert math.isinf(C.next_deadline(app))
    clock.now = .03
    app.animations_enabled = True
    assert C._visual_pointer(app, item) == G.Cell(9, 60, 2, 1)
    assert math.isinf(C.next_deadline(app))


@pytest.mark.parametrize("hidden", ["menu", "panel", "help", "new_job", "resize", "unpublish"])
def test_hidden_blocked_and_stale_plot_motion_has_no_feedback_or_deadline(app, clock, hidden):
    plot(app)
    C.hover(app, 5, 30)
    C.hover(app, 9, 60)
    clock.now = .02
    if hidden == "menu":
        app.toolbar_state["menu"] = 1
    elif hidden == "panel":
        app.toolbar_state["panel"] = "help"
    elif hidden == "help":
        app.mode = "help"
    elif hidden == "new_job":
        app.analytics_job = "102"
    elif hidden == "resize":
        app.width = 121
    else:
        C.begin_frame(app, 120, 40)
        C.publish(app, 120, 40)
    assert C.feedback(app) == []
    assert math.isinf(C.next_deadline(app))
    assert C.initialize(app).get("visual") is None


@pytest.mark.parametrize("stale", ["context", "geometry", "unpublish"])
def test_deadline_itself_rejects_stale_captured_motion_before_feedback(app, clock, stale):
    item = plot(app)
    C.handle_mouse(app, 4, 18, button="press")
    C.handle_mouse(app, 9, 56, button="drag")
    clock.now = .02
    if stale == "context":
        app.analytics_job = "102"
    elif stale == "geometry":
        C.initialize(app)["plots"] = (replace(item, visible=replace(item.visible, top=3)),)
    else:
        C.initialize(app)["plots"] = ()
    assert math.isinf(C.next_deadline(app))
    assert C.initialize(app).get("visual") is None


def test_unchanged_document_publication_retains_an_active_visual_trajectory(app, clock):
    item = plot(app)
    C.hover(app, 5, 30)
    C.hover(app, 9, 60)
    started = C.initialize(app)["visual"]["started"]
    clock.now = .02
    before = C._visual_pointer(app, item)
    C.begin_frame(app, 120, 40)
    current = plot(app)
    assert C._visual_pointer(app, current) == before
    assert C.initialize(app)["visual"]["started"] == started


def test_selector_feedback_and_deadlines_do_not_query_sources_snapshots_or_rasterizers(app, clock, monkeypatch):
    plot(app)
    rows = [[(" " * 120, "bg:surface-sunken")]] * 40

    def forbidden(*args, **kwargs):
        pytest.fail("Cosmetic selector animation accessed a source or rebuilt a graph")

    app.store = SimpleNamespace(snapshot=forbidden)
    app.research.current = app.research.request = forbidden
    monkeypatch.setattr(C.charts, "braille_chart", forbidden)
    monkeypatch.setattr(C.charts, "vbar_chart", forbidden)
    C.hover(app, 5, 30)
    C.hover(app, 9, 60)
    for now in (0., .008, .016, .025):
        clock.now = now
        assert C.feedback(app, rows=rows)
        deadline = C.next_deadline(app)
        assert deadline == pytest.approx(min(G.SUBCELL_DURATION, now + 1 / 60)) if now < G.SUBCELL_DURATION else math.isinf(deadline)


def test_frame_cache_selector_wakeups_remain_cosmetic_and_preserve_document_deadlines(clock, monkeypatch):
    from raven.config import Config
    from raven.controller import App
    from raven.model import Job, Store
    from raven.views import Views

    cfg = Config({"animations": True, "startup_animation": False, "log_lines": 0})
    store = Store(persist=False)
    store.jobs = [Job("7", "native", "cpu", "RUNNING", cpus=4, mem_req="8G")]
    for index in range(20):
        store.record("7", dict(k="live", t=1000 + index * 10, cpu=index / 25, rss=1024 ** 3))
    app = App(store, None, None, cfg, "test", interactive=True)
    app.tab = "analytics"
    app.analytics_job = app.selected_id = "7"
    views = Views(L.Glyphs(False), cfg)
    app.views_ref = views
    cache = screen._FrameCache()
    cache.rebuild(app, views, store, None, 160, 64)
    item = next(value for value in C.initialize(app)["plots"] if value.kind == "metric")
    C.hover(app, item.visible.top + 1, item.visible.left + 4)
    C.hover(app, item.visible.top + 2, item.visible.left + 5)
    pristine = tuple(tuple(row) for row in cache.rows)
    graph = app.interaction_state["graph"]
    deadlines = (cache.next_maintenance, cache.next_animation, cache.next_live)

    def forbidden(*args, **kwargs):
        pytest.fail("A selector deadline triggered a full document or data refresh")

    monkeypatch.setattr(app, "tick", forbidden)
    monkeypatch.setattr(store, "snapshot", forbidden)
    monkeypatch.setattr(views, "compose", forbidden)
    monkeypatch.setattr(views, "overlay", forbidden)
    monkeypatch.setattr(views.files, "stat", forbidden)
    monkeypatch.setattr(views.files, "tail", forbidden)
    monkeypatch.setattr(C.charts, "braille_chart", forbidden)
    monkeypatch.setattr(C.charts, "vbar_chart", forbidden)
    for now in (.002, .008, .016):
        clock.now = now
        rows, overlays, _ = cache.feedback(app, views)
        assert overlays and cache.next_selector == pytest.approx(min(G.SUBCELL_DURATION, now + 1 / 60))
        assert 1 <= cache.wait_ms() <= 17
        assert not cache.due(app, 160, 64, now=cache.next_selector)
        assert tuple(tuple(row) for row in rows) == pristine
        assert app.interaction_state["graph"] is graph
        assert (cache.next_maintenance, cache.next_animation, cache.next_live) == deadlines
    clock.now = .025
    cache.feedback(app, views)
    assert math.isinf(cache.next_selector) and cache.wait_ms() > 17
    if app.research:
        app.research.close()
