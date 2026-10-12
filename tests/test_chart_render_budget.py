"""Cached dashboard rasters preserve observations and fresh runtime metadata."""
import math
from types import SimpleNamespace

import pytest

from raven import analysis_ui as A, clock, layout as L


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setattr(clock, "now", lambda: 200.0)
    value = SimpleNamespace()
    A.initialize(value)
    return value


def draw(app, points, width=80, height=5, name="loss", source="exact/report.jsonl", ascii_=False):
    return A.chart_rows(L.Glyphs(ascii_), app, points, width, height, name, source, interactive=False)


def uncached(app, points, width=80, height=5, name="loss", source="exact/report.jsonl", ascii_=False):
    return A._render_chart_rows(L.Glyphs(ascii_), app, points, width, height, name, source, interactive=False)


@pytest.mark.parametrize("width,height,ascii_", [(1, 0, True), (40, 3, True), (80, 5, False), (180, 10, False)])
@pytest.mark.parametrize("axis", [{"mode": "auto"}, {"mode": "fixed", "low": -1.0, "high": 4.0},
                                 {"mode": "log"}, {"mode": "log", "low": .01, "high": 10.0}])
def test_cold_and_warm_cards_match_uncached_exact_observations(app, width, height, ascii_, axis):
    points = [{"t": 100, "value": 1}, {"t": 99, "value": 3}, {"t": 101, "value": None},
              {"t": 100, "value": -0.0}, {"t": 140, "value": 1e-3},
              {"t": True, "value": 999}, {"t": float("nan"), "value": 99},
              {"t": 141, "value": float("inf")}, {"t": 142, "value": False},
              {"t": 143, "value": {"unavailable": 1}}]
    app.analysis_state["axes"]["loss"] = axis
    app.analysis_state["metric_display"]["loss"] = {"label": "Measured loss", "unit": "declared", "precision": 4}
    expected = uncached(app, points, width, height, ascii_=ascii_)
    assert draw(app, points, width, height, ascii_=ascii_) == expected
    assert draw(app, points, width, height, ascii_=ascii_) == expected


def test_unchanged_cards_skip_rasterization_but_refresh_source_age(app, monkeypatch):
    points = [{"t": timestamp, "value": timestamp / 100} for timestamp in range(150)]
    calls, raster = [], A.charts.braille_chart
    def chart(*args, **kwargs):
        calls.append(1)
        return raster(*args, **kwargs)
    monkeypatch.setattr(A.charts, "braille_chart", chart)
    first = draw(app, points)
    monkeypatch.setattr(clock, "now", lambda: 269.0)
    second = draw(app, points, source="new/exact/report.jsonl")
    assert calls == [1]
    assert first[:-1] == second[:-1]
    assert "latest 51s ago" in L.row_text(first[-1])
    assert "latest 2m 00s ago" in L.row_text(second[-1])
    assert "new/exact/report.jsonl" in L.row_text(second[-1])


def test_same_list_and_dictionary_corrections_invalidate_interior_samples(app, monkeypatch):
    points = [{"t": timestamp, "value": 1.0} for timestamp in range(200)]
    calls, raster = [], A.charts.braille_chart
    monkeypatch.setattr(A.charts, "braille_chart", lambda *args, **kwargs: (calls.append(1), raster(*args, **kwargs))[1])
    original = draw(app, points)
    points[99]["value"] = 1000.0
    corrected = draw(app, points)
    assert len(calls) == 2 and corrected != original
    assert corrected == uncached(app, points)
    points[99]["t"] = 500.0
    corrected_time = draw(app, points)
    assert len(calls) == 4  # The reference above intentionally bypassed the cache.
    assert corrected_time == uncached(app, points)
    points[99]["value"] = None
    missing = draw(app, points)
    assert missing == uncached(app, points)
    assert "gaps" in L.row_text(missing[-1])


@pytest.mark.parametrize("change", ["color", "label", "unit", "precision", "axis", "width", "height", "glyphs", "timezone"])
def test_visible_preferences_and_geometry_invalidate_cached_raster(app, monkeypatch, change):
    points = [{"t": timestamp, "value": math.sin(timestamp / 20)} for timestamp in range(200)]
    draw(app, points)
    calls, raster = [], A.charts.braille_chart
    monkeypatch.setattr(A.charts, "braille_chart", lambda *args, **kwargs: (calls.append(1), raster(*args, **kwargs))[1])
    params = {}
    if change == "color":
        app.analysis_state["colors"]["loss"] = "green"
    elif change in ("label", "unit", "precision"):
        app.analysis_state["metric_display"]["loss"] = {change: 6 if change == "precision" else "new declared value"}
    elif change == "axis":
        app.analysis_state["axes"]["loss"] = {"mode": "fixed", "low": -2.0, "high": 2.0}
    elif change in ("width", "height"):
        params[change] = 90 if change == "width" else 10
    elif change == "glyphs":
        params["ascii_"] = True
    else:
        monkeypatch.setenv("TZ", "different-cache-context")
    updated = draw(app, points, **params)
    assert calls == [1]
    assert updated == uncached(app, points, **params)


def test_signed_zero_and_bound_types_keep_exact_display_text(app):
    points = [{"t": 100, "value": 0.0}]
    first = draw(app, points)
    points[0]["value"] = -0.0
    second = draw(app, points)
    assert first != second
    assert second == uncached(app, points)
    app.analysis_state["axes"]["loss"] = {"mode": "fixed", "low": 0, "high": 1}
    integer = draw(app, points)
    app.analysis_state["axes"]["loss"]["low"] = 0.0
    floating = draw(app, points)
    assert integer != floating
    assert floating == uncached(app, points)


def test_returned_rows_do_not_mutate_the_cached_chart(app):
    points = [{"t": 100, "value": 1}, {"t": 101, "value": 2}]
    original = draw(app, points)
    expected = [list(row) for row in original]
    original[0][0] = ("caller feedback", "sel")
    original[-1][0] = ("caller source", "")
    original.append([("caller extra", "")])
    assert draw(app, points) == expected


def test_interactive_cursor_range_and_crosshair_still_use_original_path(app, monkeypatch):
    points = [{"t": timestamp, "value": timestamp + .1234567890123456, "step": timestamp + 10}
              for timestamp in range(10)]
    draw(app, points)  # A dashboard entry must not supply an interactive result.
    state = app.analysis_state
    state.update(cursor=2, chart_range={"metric": "loss", "start": 1, "end": 3}, zoom=2.0, pan=0.0)
    original = A._render_chart_rows(L.Glyphs(False), app, points, 80, 5, "loss", "source")
    cache = state["chart_card_cache"]
    result = A.chart_rows(L.Glyphs(False), app, points, 80, 5, "loss", "source")
    assert result == original
    assert state["cursor_t"] == 2
    assert "step 12" in L.to_text(result, 80)
    assert "Range t=1 to 3" in L.to_text(result, 80)
    assert state["chart_visible"]["points"][2]["value"] == points[2]["value"]
    assert state["chart_card_cache"] is cache and len(cache) == 1


def test_card_cache_is_bounded_and_excluded_from_preferences(app):
    points = [{"t": timestamp, "value": timestamp} for timestamp in range(A.MAX_POINTS + 20)]
    for index in range(A.MAX_CARD_CACHE + 4):
        draw(app, points, name=f"metric_{index}")
    cache = app.analysis_state["chart_card_cache"]
    assert len(cache) == A.MAX_CARD_CACHE
    assert sum(entry["points"] for entry in cache.values()) <= A.MAX_CARD_CACHE_POINTS
    assert all(entry["points"] == A.MAX_POINTS for entry in cache.values())
    assert "chart_card_cache" not in A.save(app)["analysis"]
    # A correction outside the retained tail cannot change any displayed data.
    expected = draw(app, points, name="metric_11")
    points[0]["value"] = -999999
    assert draw(app, points, name="metric_11") == expected
