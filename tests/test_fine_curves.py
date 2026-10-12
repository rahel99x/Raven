"""Fine subcell curves retain real geometry, gaps, bounds, and render budgets."""
import copy
from decimal import Decimal, localcontext
import math
import sys
from types import SimpleNamespace

import pytest

from raven import analysis_ui as A, chart_interaction as C, charts, layout as L, palette
from raven.config import Config
from raven.controller import App
from raven.model import Job, Store
from raven.views import Views


def render(values, *, width=22, height=6, ascii_=False, **kwargs):
    metadata = {}
    rows = charts.braille_chart(L.Glyphs(ascii_), values, width, height,
                                lo=0., hi=1., metadata=metadata, **kwargs)
    return rows, metadata


def pixels(rows, metadata):
    top, left, bottom, right = metadata["plot_rect"]
    raster_x, raster_y = metadata["raster"]
    found = set()
    for cell_y, row in enumerate(rows[top:bottom]):
        for cell_x, glyph in enumerate(L.row_text(row)[left:right]):
            if raster_y == 4 and glyph in charts.BRAILLE:
                mask = charts.BRAILLE.index(glyph)
                found.update((cell_x * 2 + dx, cell_y * 4 + dy)
                             for dy in range(4) for dx in range(2)
                             if mask & charts.BRAILLE_BITS[dy][dx])
            elif raster_y == 2 and glyph in charts.QUADRANTS:
                mask = charts.QUADRANTS.index(glyph)
                found.update((cell_x * 2 + dx, cell_y * 2 + dy)
                             for dy in range(2) for dx in range(2)
                             if mask & (1 << (dy * 2 + dx)))
            elif raster_x == 1 and glyph in ".-/\\:+":
                found.add((cell_x, cell_y))
    return found


@pytest.mark.parametrize("pixel_y", range(8))
def test_every_vertical_subcell_level_is_distinct_and_horizontal_lines_are_continuous(pixel_y):
    value = 1 - pixel_y / 7
    rows, metadata = render([value, value], width=18, height=2,
                            sample_times=[0., 1.], sample_interval=1.)
    assert metadata["raster"] == (2, 4)
    assert pixels(rows, metadata) == {(x, pixel_y) for x in range(16)}


@pytest.mark.parametrize("rising", [False, True])
def test_diagonal_lines_use_exact_fine_coordinates_without_filling_area(rising):
    values = [0., 1.] if rising else [1., 0.]
    rows, metadata = render(values, sample_times=[0., 1.], sample_interval=1.)
    assert pixels(rows, metadata) == {(x, 23 - x if rising else x) for x in range(24)}


@pytest.mark.parametrize("width,height", [(12, 8), (30, 2), (80, 12)])
def test_steep_and_shallow_segments_form_one_connected_thin_path(width, height):
    rows, metadata = render([0., 1.], width=width, height=height,
                            sample_times=[0., 1.], sample_interval=1.)
    found = pixels(rows, metadata)
    seen, pending = set(), [min(found)]
    while pending:
        point = pending.pop()
        if point in seen:
            continue
        seen.add(point)
        pending.extend((point[0] + dx, point[1] + dy) for dx in (-1, 0, 1) for dy in (-1, 0, 1)
                       if (dx or dy) and (point[0] + dx, point[1] + dy) in found)
    assert seen == found
    # Bresenham follows the measured segment using one dot per dominant-axis
    # step. A steep slope cannot turn into a filled patch of data.
    assert len(found) == max((width - 10) * 2, height * 4)


def test_duplicate_timestamp_step_keeps_vertical_discontinuity_and_flat_sides():
    rows, metadata = render([.2, .2, .8, .8], width=18,
                            sample_times=[0., 1., 1., 2.], sample_interval=1.)
    found = pixels(rows, metadata)
    assert {(8, y) for y in range(5, 19)} <= found
    assert all(y == 18 for x, y in found if x < 8)
    assert all(y == 5 for x, y in found if x > 8)


@pytest.mark.parametrize("unknown", [None, math.nan, math.inf, -math.inf])
def test_fine_path_never_connects_across_an_unknown_observation(unknown):
    rows, metadata = render([.2, unknown, .8], sample_times=[0., 1., 2.], sample_interval=1.)
    found = pixels(rows, metadata)
    assert found == {(0, 18), (23, 5)}


def test_fine_path_never_connects_across_a_sampling_outage():
    rows, metadata = render([.2, .2, .8, .8], width=90,
                            sample_times=[0., 1., 30., 31.], sample_interval=1.)
    found = pixels(rows, metadata)
    assert found and not any(6 <= x <= 151 for x, _ in found)


@pytest.mark.parametrize("rising", [False, True])
def test_selected_time_interval_retains_true_boundary_intersections_without_changing_samples(rising):
    values = [0., 1.] if rising else [1., 0.]
    timestamps = [0., 1.]
    before = copy.deepcopy((values, timestamps))
    rows, metadata = render(values, width=22, height=6, sample_times=timestamps,
                            times=(.25, .75), sample_interval=1.)
    found = pixels(rows, metadata)
    assert found and all(6 <= y <= 17 for _, y in found)
    assert {x for x, _ in found} == set(range(24))
    assert (values, timestamps) == before


def test_opaque_fallback_keeps_earlier_geometry_and_ascii_ignores_unicode_style():
    fine, fine_metadata = render([0., 1.], sample_times=[0., 1.], sample_interval=1.)
    blocks, block_metadata = render([0., 1.], curve_style="blocks", sample_times=[0., 1.], sample_interval=1.)
    assert fine_metadata["raster"] == (2, 4) and block_metadata["raster"] == (2, 2)
    assert fine_metadata["plot_rect"] == block_metadata["plot_rect"]
    assert any(glyph in charts.BRAILLE[1:] for row in fine for glyph in L.row_text(row))
    assert any(glyph in charts.QUADRANTS[1:] for row in blocks for glyph in L.row_text(row))
    ascii_fine, metadata = render([0., 1.], ascii_=True, sample_times=[0., 1.], sample_interval=1.)
    ascii_blocks, _ = render([0., 1.], ascii_=True, curve_style="blocks", sample_times=[0., 1.], sample_interval=1.)
    assert ascii_fine == ascii_blocks and metadata["raster"] == (1, 1)
    assert all(L.row_text(row).isascii() for row in ascii_fine)


@pytest.mark.parametrize("theme", palette.THEME_NAMES)
def test_fine_curve_uses_the_same_theme_foreground_and_background_tokens(theme):
    rows, _ = render([0., 1.], sample_times=[0., 1.], sample_interval=1.)
    styles = {style for row in rows for glyph, style in row if glyph in charts.BRAILLE[1:]}
    assert styles == {"chart-1+bold"}
    resolved = palette.resolve(next(iter(styles)), theme)
    assert resolved.foreground == palette.resolve("chart-1", theme).foreground
    assert resolved.background == palette.resolve("chart-1", theme).background


@pytest.mark.parametrize("width", [1, 3, 17, 100])
def test_dense_timestamp_downsample_keeps_spikes_gaps_and_a_bounded_vertex_budget(width):
    values = [.5] * 50000
    values[10000], values[10001], values[40000] = 0., 1., None
    points, bounds = charts._time_points(values, range(len(values)), width, None, 1., envelope=True)
    assert len(points) <= 4 * width and bounds == (0, len(values) - 1)
    assert any(value is None for _, value, _ in points)
    if width > 1:
        assert any(value == 0. for _, value, _ in points)
        assert any(value == 1. for _, value, _ in points)
    assert values[10000:10002] == [0., 1.]


@pytest.mark.parametrize("values", [[sys.float_info.max] * 100,
    [-sys.float_info.max, sys.float_info.max, math.ulp(0.)],
    [sys.float_info.max] * 20 + [-sys.float_info.max] * 20 + [1e-300],
    [math.ulp(0.)] * 100])
def test_bounded_mean_buckets_retain_extreme_and_subnormal_precision(values):
    points, _ = charts._time_points(values, [0.] * len(values), 1, None, 1.)
    with localcontext() as context:
        context.prec = 1100
        expected = float(sum(map(Decimal.from_float, values)) / len(values))
    assert len(points) == 1 and math.isfinite(points[0][1])
    assert points[0][1] == expected


def test_sampling_outage_inside_single_bucket_does_not_become_a_continuous_edge():
    points, _ = charts._time_points([.2, .2, .8, .8], [0., 1., 30., 31.], 1,
                                    None, 1., envelope=True)
    assert [value for _, value, _ in points] == [.2, .8, .8]
    assert [bridge for _, _, bridge in points] == [False, False, True]


@pytest.mark.parametrize("page", ["analytics", "jobs-details", "research", "chart", "diff", "compare"])
def test_every_metrics_workspace_uses_the_shared_fine_curve_renderer(page):
    cfg = Config({"animations": False, "log_lines": 0})
    store = Store(persist=False)
    store.jobs = [Job(str(jid), "train", "cpu", "RUNNING", cpus=4) for jid in (7, 8)]
    for job in store.jobs:
        for i in range(12):
            store.record(job.id, {"k": "live", "t": 100. + i, "cpu": .2 + i / 20,
                                  "rss": (1 + i / 20) * 1024 ** 3})
    app = App(store, None, None, cfg, "tester", interactive=False)
    app.width, app.height = 160, 70
    app.selected_id = app.analytics_job = app.research_job_id = "7"
    app.analytics_view = "compare" if page == "compare" else "job"
    app.compare_ids = ["7", "8"]
    views = Views(L.Glyphs(False), cfg)
    app.views_ref = views
    if page == "jobs-details":
        app.tab = "jobs"
        app.job_panel_state.update(mode="analytics", analytics_view="job")
    elif page == "research":
        app.tab, app.research_view = "research", "experiment"
        app.research = SimpleNamespace(context=lambda snap, target: {"jid": "7", "job": store.jobs[0]},
            request=lambda context: {"series": {"loss": [{"t": 100. + i, "value": 1 - i / 20} for i in range(12)]}},
            close=lambda: None)
    else:
        app.tab = "analytics"
    if page in ("chart", "diff"):
        app.mode = "analysis"
        app.analysis_state.update(modal=page, chart_job="7", metric="CPU per core (%)", diff_ids=["7", "8"])
    try:
        rows, _ = views.compose(store.snapshot(), app, app.width, app.height)
        overlays = views.overlay(store.snapshot(), app, app.width, app.height) or []
        text = "\n".join(L.row_text(row) for row in rows)
        text += "\n" + "\n".join(L.row_text(row) for _, _, row in overlays)
        assert any(glyph in charts.BRAILLE[1:] for glyph in text)
        assert C.initialize(app)["plots"]
        assert all(L.vlen(L.row_text(row)) <= app.width for row in rows)
    finally:
        if app.research:
            app.research.close()
