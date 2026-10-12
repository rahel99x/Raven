"""Captured chart gestures tolerate three cells beyond axes, without new hit targets."""
from types import SimpleNamespace

import pytest

from raven import chart_interaction as C
from raven.interaction import Rect


@pytest.fixture
def app():
    value = SimpleNamespace(mode="main", tab="analytics", width=100, height=30,
                            selected_id="7", analytics_job="7", analytics_view="job",
                            toolbar_state={}, project_state={}, job_panel_state={},
                            research=SimpleNamespace(generation=1), messages=[])
    value.say = value.messages.append
    C.begin_frame(value, 100, 30)
    return value


def publish(app, *, clip=(1, 8, 18, 75), rect=(4, 12, 14, 70), identity=("source", "7", "CPU")):
    first = C.mark(app)
    C.record(app, identity, {"plot_rect": rect, "x_bounds": (0., 100.), "y_bounds": (0., 200.)})
    if clip is not None:
        C.place_since(app, first, clip=clip)
    return C.publish(app, 100, 30)[-1]


INSIDE = [(1, 60), (16, 60), (12, 9), (12, 72), (1, 9), (1, 72), (16, 9), (16, 72)]
OUTSIDE = [(0, 60), (17, 60), (12, 8), (12, 73), (0, 8), (0, 73), (17, 8), (17, 73)]


@pytest.mark.parametrize("point", INSIDE)
@pytest.mark.parametrize("shift", [False, True])
def test_drag_and_release_inside_margin_clamp_to_original_plot_on_every_side_and_corner(app, point, shift):
    plot = publish(app)
    assert C.capture_bounds(plot) == Rect(1, 9, 17, 73)
    assert C.handle_mouse(app, 8, 40, button="press", shift=shift)
    assert C.handle_mouse(app, *point, button="drag") and C.active(app)
    expected_y = min(13, max(4, point[0]))
    expected_x = min(69, max(12, point[1]))
    assert C.initialize(app)["capture"]["current"] == (expected_y, expected_x)
    assert all(plot.visible.contains(y, x) for y, x, _ in C.feedback(app))
    assert C.handle_mouse(app, *point, button="release") and not C.active(app)
    bounds = C.bounds(app, plot.key)
    assert bounds["x"] == pytest.approx(tuple(sorted((100 * 28 / 57, 100 * (expected_x - 12) / 57))))
    if shift:
        assert bounds["y"] == pytest.approx(tuple(sorted((200 * 5 / 9, 200 * (13 - expected_y) / 9))))
    else:
        assert C.autofit(app, plot.key)
    assert C.feedback(app) == []  # actual pointer remains outside the plot after release


@pytest.mark.parametrize("point", INSIDE)
def test_margin_does_not_capture_hover_or_right_reset_without_an_existing_drag(app, point):
    plot = publish(app)
    C._apply(app, plot, {"x": (10., 50.), "y": (20., 80.)})
    assert not C.hover(app, *point)
    assert not C.handle_mouse(app, *point, button="press")
    assert not C.active(app)
    assert not C.handle_mouse(app, *point, button="right")
    assert C.bounds(app, plot.key) == {"x": (10., 50.), "y": (20., 80.)}


@pytest.mark.parametrize("point", OUTSIDE)
def test_leaving_margin_cancels_immediately_and_delayed_release_cannot_restore_or_apply_zoom(app, point):
    plot = publish(app)
    assert C.handle_mouse(app, 8, 40, button="press")
    assert C.handle_mouse(app, *point, button="drag")
    assert not C.active(app) and C.bounds(app, plot.key) is None
    assert C.handle_mouse(app, 12, 60, button="release")
    assert C.bounds(app, plot.key) is None
    assert not C.handle_mouse(app, 12, 60, button="release")


@pytest.mark.parametrize("point", OUTSIDE)
def test_release_outside_margin_never_applies_even_without_a_previous_motion_report(app, point):
    plot = publish(app)
    C.handle_mouse(app, 8, 40, button="press")
    assert C.handle_mouse(app, *point, button="release")
    assert not C.active(app) and C.bounds(app, plot.key) is None


@pytest.mark.parametrize("point", [(3, 40), (10, 40), (7, 14), (7, 50)])
def test_clipped_pane_viewport_never_allows_tolerance_into_neighboring_content(app, point):
    plot = publish(app, clip=(4, 15, 10, 50), rect=(2, 10, 12, 60))
    assert plot.visible == plot.viewport == Rect(4, 15, 10, 50)
    assert C.capture_bounds(plot) == plot.visible
    C.handle_mouse(app, 6, 25, button="press")
    assert C.handle_mouse(app, *point, button="drag")
    assert not C.active(app) and C.bounds(app, plot.key) is None
    assert C.handle_mouse(app, 8, 40, button="release")


def test_nested_translation_row_mapping_and_screen_clip_retain_buffer_viewport(app):
    first = C.mark(app)
    C.record(app, ("nested",), {"plot_rect": (2, 4, 10, 30), "x_bounds": (0., 100.), "y_bounds": (0., 200.)})
    C.place_since(app, first, dy=3, dx=8, clip=(3, 8, 15, 40))
    records = C.take_since(app, first)
    mapping = {y: y + 2 for y in range(3, 15)}
    C.put_records(app, C.map_records(records, mapping, dx=10, dy=1, clip=(6, 20, 18, 50)))
    plot = C.publish(app, 100, 30)[0]
    assert plot.rect == Rect(8, 22, 16, 48)
    assert plot.viewport == Rect(6, 20, 18, 50)
    assert C.capture_bounds(plot) == plot.viewport


def test_screen_edges_clip_buffer_and_malformed_reports_cannot_escape(app):
    plot = publish(app, clip=None, rect=(0, 0, 30, 100))
    assert plot.viewport == C.capture_bounds(plot) == Rect(0, 0, 30, 100)
    C.handle_mouse(app, 10, 40, button="press")
    assert C.handle_mouse(app, None, 50, button="drag")
    assert not C.active(app) and C.bounds(app, plot.key) is None
    assert C.handle_mouse(app, 12, 60, button="release")


@pytest.mark.parametrize("point", [(1, 40), (18, 40), (8, 4), (8, 72),
                                  (1, 4), (1, 72), (18, 4), (18, 72)])
def test_margin_includes_three_cells_beyond_y_and_x_axis_labels(app, point):
    first = C.mark(app)
    C.record(app, ("with-labels", "7"), {
        "plot_rect": (4, 12, 14, 70), "axis_rect": (4, 7, 16, 70),
        "x_bounds": (0., 100.), "y_bounds": (0., 200.)})
    C.place_since(app, first, clip=(0, 0, 25, 90))
    plot = C.publish(app, 100, 30)[0]
    assert C.capture_bounds(plot) == Rect(1, 4, 19, 73)
    assert not C.hover(app, *point) and not C.handle_mouse(app, *point, button="press")
    assert C.handle_mouse(app, 8, 40, button="press")
    assert C.handle_mouse(app, *point, button="drag") and C.active(app)
    clamped_y, clamped_x = min(13, max(4, point[0])), min(69, max(12, point[1]))
    assert C.initialize(app)["capture"]["current"] == (clamped_y, clamped_x)
    assert C.handle_mouse(app, *point, button="release") and not C.active(app)
    if clamped_x == 40:
        # Leaving vertically is still an owned release, but its zero time
        # range must remain a harmless no-op.
        assert C.bounds(app, plot.key) is None
    else:
        assert C.bounds(app, plot.key)["x"] == pytest.approx(tuple(sorted((
            100 * 28 / 57, 100 * (clamped_x - 12) / 57))))


def test_captured_plot_retains_ownership_when_its_margin_overlaps_a_different_graph(app):
    first = publish(app)
    second = publish(app, identity=("source", "7", "Memory"), rect=(14, 12, 24, 70), clip=None)
    C._apply(app, second, {"x": (10., 50.), "y": (20., 80.)})
    C.handle_mouse(app, 8, 40, button="press")
    assert C.handle_mouse(app, 14, 60, button="drag") and C.active(app)
    assert C.initialize(app)["capture"]["plot"].key == first.key
    assert C.handle_mouse(app, 14, 60, button="release")
    assert C.bounds(app, first.key) is not None
    assert C.bounds(app, second.key) == {"x": (10., 50.), "y": (20., 80.)}


@pytest.mark.parametrize("change", ["menu", "panel", "mode", "job", "generation", "viewport"])
def test_margin_does_not_weaken_context_source_or_clip_change_cancellation(app, change):
    plot = publish(app)
    C.handle_mouse(app, 8, 40, button="press")
    if change == "menu": app.toolbar_state["menu"] = "View"
    elif change == "panel": app.toolbar_state["panel"] = "help"
    elif change == "mode": app.mode = "confirm"
    elif change == "job": app.selected_id = "8"
    elif change == "generation": app.research.generation += 1
    else:
        C.begin_frame(app, 100, 30)
        publish(app, clip=(3, 10, 17, 73))  # same visible plot, smaller surrounding viewport
    assert C.handle_mouse(app, 15, 60, button="drag")
    assert not C.active(app) and C.bounds(app, plot.key) is None
    assert C.handle_mouse(app, 12, 60, button="release")


def test_fresh_press_elsewhere_supersedes_cancelled_release_marker(app):
    publish(app)
    C.handle_mouse(app, 8, 40, button="press")
    C.handle_mouse(app, 17, 60, button="drag")
    assert C.initialize(app)["cancelled_release"]
    assert not C.handle_mouse(app, 20, 80, button="press")
    assert not C.initialize(app)["cancelled_release"]
    assert not C.handle_mouse(app, 21, 80, button="release")
