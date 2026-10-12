"""Modelled layers remain labelled, bounded and separate from observations."""
from copy import deepcopy
import math
import sys
from types import SimpleNamespace

import pytest

from raven import charts, chart_interaction as C, layout as L, metric_envelope as E, palette
from raven.interaction import Rect
from raven.metric_raster import MetricRasterCache


def draw(values, *, ascii_=False, width=90, height=8, title="CPU", **options):
    metadata = {}
    rows = charts.braille_chart(L.Glyphs(ascii_), values, width, height, lo=0., hi=100.,
                               title=title, metadata=metadata, **options)
    return rows, metadata


@pytest.mark.parametrize("ascii_", [False, True])
def test_dense_zigzags_keep_exact_extrema_but_use_faint_interior_and_bright_edges(ascii_):
    values = [10., 90.] * 2000
    before = list(values)
    rows, metadata = draw(values, ascii_=ascii_, sample_times=range(len(values)), sample_interval=1.)
    assert metadata["trend"] and metadata["band"]
    assert "[fit + range]" in L.row_text(rows[0])
    assert metadata["band_label"] == "observed low-high range"
    assert values == before
    assert "last 90" in L.row_text(rows[0]) and "max 90" in L.row_text(rows[0])
    styles = {style for row in rows[1:9] for text, style in row if text.strip()}
    assert "chart-1+bold" in styles and "chart-2" in styles
    assert "chart-fill+chart-1+dim" in styles
    assert all(L.vlen(L.row_text(row)) <= 90 for row in rows)
    assert all(L.row_text(row).isascii() for row in rows) if ascii_ else True


@pytest.mark.parametrize("ascii_", [False, True])
def test_fit_and_band_layers_never_add_rows_or_move_graph_geometry(ascii_):
    values = [10., 90.] * 1000
    raw, original = draw(values, ascii_=ascii_, trend=False, bands=False)
    fitted, current = draw(values, ascii_=ascii_)
    assert len(fitted) == len(raw)
    for key in ("plot_rect", "axis_rect", "x_bounds", "y_bounds", "raster", "has_data"):
        assert current[key] == original[key]


def test_titleless_microplots_and_explicit_optout_remain_raw():
    values = [10., 90.] * 1000
    implicit, metadata = draw(values, title="")
    explicit, _ = draw(values, title="", trend=False, bands=False)
    assert implicit == explicit and not metadata["trend"] and not metadata["band"]
    rows, meta = draw(values, trend=False, bands=False)
    assert not meta["trend"] and not meta["band"] and "[fit" not in L.row_text(rows[0])


def test_sparse_display_window_omits_fit_even_if_retained_history_is_large():
    times = list(range(1000))
    values = [30. + math.sin(i / 20.) * 10. for i in times]
    whole, all_meta = draw(values, sample_times=times, sample_interval=1.)
    sparse, sparse_meta = draw(values, sample_times=times, times=(998., 999.), sample_interval=1.)
    assert all_meta["trend"] and not sparse_meta["trend"]
    assert sparse_meta["x_bounds"] == (998., 999.)
    assert "[fit" in L.row_text(whole[0]) and "[fit" not in L.row_text(sparse[0])


def test_duplicate_time_steps_and_outages_cannot_be_replaced_by_a_range_or_smoothed():
    values = [10., 90.] * 20
    rows, meta = draw(values, sample_times=[0.] * len(values), sample_interval=1.)
    assert not meta["trend"] and not meta["band"]
    details = {}
    points, _ = charts._time_points([10., 90., 10., 90., 10., 90.], [0., 1., 2., 100., 101., 102.],
                                    1, None, 1., True, details)
    profile = E.columns(points, 1, details)
    assert profile[0].broken
    assert not E.band_columns(profile, 0., 100., 32)


def test_adjacent_dense_reversals_preserve_bands_as_peaks_cross_bucket_boundaries():
    profile = [E.Column(0, 10., 90., 10., 10., 4, False, turns=1),
               E.Column(1, 10., 90., 90., 90., 4, True, turns=1)]
    assert E.band_columns(profile, 0., 100., 32) == tuple(profile)
    assert not E.band_columns(profile[:1], 0., 100., 32)
    disconnected = [profile[0], E.Column(1, 10., 90., 90., 90., 4, False, turns=1)]
    assert not E.band_columns(disconnected, 0., 100., 32)


@pytest.mark.parametrize("theme", palette.THEME_NAMES)
def test_fill_and_model_tokens_follow_the_current_theme(theme):
    fill = palette.resolve("chart-fill+chart-1+dim", theme)
    assert fill.foreground == palette.resolve("chart-1", theme).foreground
    assert "dim" in fill.flags
    assert palette.resolve("chart-2", theme).background == palette.resolve("chart-1", theme).background


@pytest.mark.parametrize("ascii_", [False, True])
def test_selector_crosses_decorative_fill_but_preserves_range_model_and_raw_ink(ascii_):
    app = SimpleNamespace()
    glyph = "." if ascii_ else "⠂"
    layers = [(0, [(glyph, "chart-fill+chart-1+dim"),
                   (glyph, "chart-1+bold"), (glyph, "chart-2"), (glyph, "chart-1+bold")])]
    cells = C._painted_cells(app, Rect(0, 0, 1, 4), 0, layers)
    assert [cell[2] for cell in cells] == [False, True, True, True]
    # Cached classification follows new painted styles after a theme or layer edit.
    layers[0][1][0] = (glyph, "chart-1+bold")
    assert all(cell[2] for cell in C._painted_cells(app, Rect(0, 0, 1, 4), 0, layers))


def test_model_geometry_cache_contains_no_observations_and_is_bounded():
    E._projection.cache_clear()
    source = [E.Column(i * 10, float(i * i), float(i * i), float(i * i), float(i * i), 1, bool(i))
              for i in range(7)]
    first = E.local_trend(source, 61)
    statistics = E._projection.cache_info()
    shifted = [E.Column(item.x, item.low + 100., item.high + 100., item.first + 100.,
                        item.last + 100., 1, item.bridge) for item in source]
    second = E.local_trend(shifted, 61)
    assert [value for _, value, _ in second] == pytest.approx([value + 100. for _, value, _ in first])
    assert E._projection.cache_info().hits > statistics.hits
    for i in range(200):
        E._projection((0, 1, 2, 3, i + 4))
    assert E._projection.cache_info().currsize <= 128


def test_model_interval_joins_share_the_same_observed_anchor():
    values = [5., 80., 15., 25., 95., 40., 20.]
    source = [E.Column(i * 100, value, value, value, value, 1, bool(i))
              for i, value in enumerate(values)]
    model = {x: value for x, value, _ in E.local_trend(source, 601)}
    # Without constrained shared anchors, independently clipped neighbouring
    # intervals can jump at these nodes even though each stays in range.
    for item in source:
        assert model[item.x] == pytest.approx(item.centre)
        if item.x:
            assert abs(model[item.x] - model[item.x - 1]) < 5.


@pytest.mark.parametrize("low,high", [(10., 90.), (-sys.float_info.max, sys.float_info.max),
                                     (math.ulp(0.), math.ulp(0.))])
def test_constant_midrange_has_exact_flat_polynomial_without_repeated_fits(monkeypatch, low, high):
    source = [E.Column(i * 10, low, high, low, high, 12, bool(i), turns=2) for i in range(7)]
    monkeypatch.setattr(E, "_fit", lambda *args: pytest.fail("A constant model needs no numerical solve"))
    expected = tuple((x, source[0].centre, bool(x)) for x in range(61))
    assert E.local_trend(source, 61) == expected


def test_constant_fastpath_preserves_missing_columns_cadence_breaks_and_short_runs(monkeypatch):
    points = [(x, 50., True) for x in range(5)] + [(5, None, False)]
    points += [(x, 50., True) for x in range(6, 11)]
    points += [(11, 50., False)] + [(x, 50., True) for x in range(12, 15)]
    monkeypatch.setattr(E, "_fit", lambda *args: pytest.fail("A constant model needs no numerical solve"))
    model = E.local_trend(E.columns(points, 15), 15)
    assert model == tuple((x, 50., x not in (0, 6)) for x in (*range(5), *range(6, 11)))


def test_almost_constant_values_still_receive_their_actual_local_fit(monkeypatch):
    values = [50.] * 7
    values[3] = math.nextafter(50., math.inf)
    source = [E.Column(i * 10, value, value, value, value, 1, bool(i))
              for i, value in enumerate(values)]
    calls, original = [], E._fit
    monkeypatch.setattr(E, "_fit", lambda *args: (calls.append(1), original(*args))[1])
    model = E.local_trend(source, 61)
    assert calls and dict((x, value) for x, value, _ in model)[30] == values[3]


def test_warm_raster_reuses_fit_but_visible_window_and_corrections_recompute(monkeypatch):
    cache, glyphs = MetricRasterCache(), L.Glyphs(False)
    values = [30. + math.sin(i / 20.) * 10. for i in range(1000)]
    options = dict(title="CPU", sample_times=list(range(1000)), sample_interval=1.)
    calls, original = [], E.local_trend
    monkeypatch.setattr(E, "local_trend", lambda *args: (calls.append(1), original(*args))[1])
    first, metadata = cache.render(glyphs, values, 90, 8, **options)
    preserved = deepcopy(first)
    assert cache.render(glyphs, values, 90, 8, **options)[0] == preserved and len(calls) == 1
    _, sparse = cache.render(glyphs, values, 90, 8, times=(998., 999.), **options)
    assert len(calls) == 2 and not sparse["trend"]
    values[100] = 100.
    assert cache.render(glyphs, values, 90, 8, **options)[0] != preserved and len(calls) == 3
    assert metadata["trend"]
