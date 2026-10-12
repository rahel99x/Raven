"""Portable subcell selector ink and bounded, purely cosmetic interpolation."""
from dataclasses import FrozenInstanceError
import math
import sys

import pytest

from raven import layout as L, selector_glyphs as G


@pytest.mark.parametrize("fraction,slot", [(0., 0), (.249999, 0), (.25, 1), (.499999, 1),
                                          (.5, 2), (.749999, 2), (.75, 3), (.999999, 3)])
def test_vertical_subcell_positions_resolve_four_distinct_dot_rows(fraction, slot):
    cell = G.locate(12 + fraction, 30.5)
    assert cell == G.Cell(12, 30, slot, 1)
    assert G.glyph(cell.y_slot, cell.x_slot, horizontal=True) == G.HORIZONTAL_GLYPHS[slot]


@pytest.mark.parametrize("fraction,slot", [(0., 0), (.499999, 0), (.5, 1), (.999999, 1)])
def test_horizontal_subcell_positions_resolve_two_honest_dot_columns(fraction, slot):
    cell = G.locate(12.5, 30 + fraction)
    assert cell == G.Cell(12, 30, 2, slot)
    assert G.glyph(cell.y_slot, cell.x_slot, vertical=True) == G.VERTICAL_GLYPHS[slot]


def test_mouse_cell_centers_do_not_claim_finer_input_coordinates():
    cell = G.locate(12.5, 30.5)
    assert cell == G.Cell(12, 30, 2, 1)
    assert G.locate(13., 31.) == G.Cell(13, 31, 0, 0)
    assert G.locate(-.1, -.1) == G.Cell(-1, -1, 3, 1)
    with pytest.raises(FrozenInstanceError):
        cell.x_slot = 0


@pytest.mark.parametrize("x_slot", [0, 1])
@pytest.mark.parametrize("y_slot", range(4))
def test_crossing_merges_thin_horizontal_and_vertical_ink_in_one_narrow_cell(y_slot, x_slot):
    horizontal = G.glyph(y_slot, x_slot, horizontal=True)
    vertical = G.glyph(y_slot, x_slot, vertical=True)
    crossing = G.glyph(y_slot, x_slot, horizontal=True, vertical=True)
    assert ord(crossing) - 0x2800 == (ord(horizontal) - 0x2800) | (ord(vertical) - 0x2800)
    assert all(L.vlen(char) == len(char) == 1 for char in (horizontal, vertical, crossing))
    assert all(0x2800 <= ord(char) <= 0x28FF for char in (horizontal, vertical, crossing))
    assert not any(char in "\x1b\ufe0f\u200d" for char in (horizontal, vertical, crossing))


def test_strokes_have_consistent_density_without_full_blocks_or_bold_style():
    assert {bin(ord(char) - 0x2800).count("1") for char in G.HORIZONTAL_GLYPHS} == {2}
    assert {bin(ord(char) - 0x2800).count("1") for char in G.VERTICAL_GLYPHS} == {4}
    assert len(set(G.HORIZONTAL_GLYPHS)) == 4 and len(set(G.VERTICAL_GLYPHS)) == 2
    assert "█" not in G.HORIZONTAL_GLYPHS + G.VERTICAL_GLYPHS


@pytest.mark.parametrize("ascii_", [False, True])
@pytest.mark.parametrize("fine", [False, True])
def test_ascii_and_legacy_fallbacks_keep_established_dots_and_plus(ascii_, fine):
    if not ascii_ and fine:
        assert G.glyph(horizontal=True, vertical=True) not in "+.·"
        return
    dot = "." if ascii_ else "·"
    assert G.glyph(horizontal=True, ascii_=ascii_, fine=fine) == dot
    assert G.glyph(vertical=True, ascii_=ascii_, fine=fine) == dot
    assert G.glyph(horizontal=True, vertical=True, ascii_=ascii_, fine=fine) == "+"
    assert G.glyph(ascii_=ascii_, fine=fine) == " "


@pytest.mark.parametrize("bad", [True, False, None, "1", float("nan"), float("inf"),
                                 -float("inf"), 10 ** 1000, [], object()])
def test_bad_coordinates_do_not_produce_cells_or_throw(bad):
    assert G.locate(bad, 12.) is None
    assert G.locate(12., bad) is None


@pytest.mark.parametrize("kwargs", [{"y_slot": -1}, {"y_slot": 4}, {"y_slot": True}, {"y_slot": .5},
                                     {"x_slot": -1}, {"x_slot": 2}, {"x_slot": False}, {"x_slot": "1"},
                                     {"horizontal": 1}, {"vertical": "yes"}, {"ascii_": None}, {"fine": 1}])
def test_invalid_slot_and_flag_values_return_a_safe_blank(kwargs):
    assert G.glyph(**kwargs) == " "


def test_smoothing_follows_monotonic_clock_elapsed_without_overshoot_and_settles_at_exact_target():
    start, target = (4.5, 30.5), (9.5, 60.5)
    values = [G.interpolate(start, target, step * .004) for step in range(21)]
    assert values[0] == start and values[-1] == target
    for axis in range(2):
        positions = [value[axis] for value in values]
        assert positions == sorted(positions)
        assert start[axis] <= min(positions) <= max(positions) <= target[axis]
    assert G.interpolate(start, target, .04) == (7., 45.5)
    assert G.interpolate(start, target, .081) == target
    assert G.interpolate(start, target, -1.) == start


def test_reverse_diagonal_motion_is_bounded_and_symmetric():
    start, target = (9.5, 60.5), (4.5, 30.5)
    for elapsed in (.001, .01, .03, .07):
        forward = G.interpolate(start, target, elapsed)
        reverse = G.interpolate(target, start, G.SMOOTH_DURATION - elapsed)
        assert forward == pytest.approx(reverse)
        assert target[0] <= forward[0] <= start[0] and target[1] <= forward[1] <= start[1]


def test_visual_retargeting_starts_at_last_painted_position_and_never_replays_events():
    painted = G.interpolate((4.5, 30.5), (9.5, 60.5), .02)
    new_target = (5.5, 20.5)
    assert G.interpolate(painted, new_target, 0.) == painted
    assert G.interpolate(painted, new_target, .08) == new_target


@pytest.mark.parametrize("point", [(5.5, 30.5), (-5.5, -30.5), (.5, -.5), (1.25, 5.75),
                                   (0., 0.), (math.ulp(0.), -math.ulp(0.)),
                                   (sys.float_info.max, -sys.float_info.max)])
def test_identical_endpoints_keep_exact_floats_and_dot_slots_at_every_fraction(point):
    expected = G.locate(*point)
    for elapsed in (.0001, .001, .005, .013, .03, .04, .061, .0799, .08, 1.):
        value = G.interpolate(point, point, elapsed)
        assert value == point and G.locate(*value) == expected


def test_one_cell_visual_motion_uses_subcell_glyphs_before_and_after_cell_boundaries():
    cells = [G.locate(*G.interpolate((10.5, 20.5), (11.5, 21.5), step * .005)) for step in range(17)]
    assert {cell.y_slot for cell in cells} == {0, 1, 2, 3}
    assert {cell.x_slot for cell in cells} == {0, 1}
    assert {cell.row for cell in cells} == {10, 11}
    assert {cell.column for cell in cells} == {20, 21}


@pytest.mark.parametrize("duration", [0., -1., .08, .2, 1., 100000.])
def test_animation_duration_is_bounded_and_can_be_disabled(duration):
    start, target = (1.5, 5.5), (100.5, 20.5)
    assert G.interpolate(start, target, .2, duration=duration) == target
    if duration <= 0:
        assert G.interpolate(start, target, 0, duration=duration) == target
    else:
        assert G.interpolate(start, target, 0, duration=duration) == start


@pytest.mark.parametrize("value", [None, "1", True, float("nan"), float("inf"), 10 ** 1000])
def test_bad_elapsed_and_duration_return_none_for_exact_pointer_fallback(value):
    assert G.interpolate((1., 2.), (3., 4.), value) is None
    assert G.interpolate((1., 2.), (3., 4.), .04, duration=value) is None


@pytest.mark.parametrize("point", [None, [], (1,), (1, 2, 3), (True, 2), (1, "2"),
                                   (1, float("nan")), (1, float("inf")), "12"])
def test_bad_points_cannot_escape_into_plot_geometry(point):
    assert G.interpolate(point, (1., 2.), .04) is None
    assert G.interpolate((1., 2.), point, .04) is None


@pytest.mark.parametrize("first,last", [(-sys.float_info.max, sys.float_info.max),
                                        (sys.float_info.max, sys.float_info.max),
                                        (math.ulp(0.), math.ulp(0.) * 2), (-1e300, -1e301)])
def test_extreme_finite_coordinates_do_not_overflow_difference_or_glyph_mapping(first, last):
    for elapsed in (.001, .04, .079, .08):
        value = G.interpolate((first, first), (last, last), elapsed)
        assert value is not None and all(math.isfinite(axis) for axis in value)
        cell = G.locate(*value)
        assert cell is not None and 0 <= cell.y_slot < 4 and 0 <= cell.x_slot < 2


def test_helper_does_not_read_a_clock_files_or_sources(monkeypatch):
    import builtins
    import time

    def forbidden(*args, **kwargs):
        pytest.fail("Pure selector glyph helper performed IO or read a clock")

    monkeypatch.setattr(builtins, "open", forbidden)
    monkeypatch.setattr(time, "monotonic", forbidden)
    monkeypatch.setattr(time, "time", forbidden)
    value = G.interpolate((1.5, 2.5), (5.5, 6.5), .04)
    cell = G.locate(*value)
    assert L.vlen(G.glyph(cell.y_slot, cell.x_slot, horizontal=True, vertical=True)) == 1
