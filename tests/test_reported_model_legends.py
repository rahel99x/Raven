"""Reported chart headers identify visual models without changing observations."""
import math
from types import SimpleNamespace

import pytest

from raven import analysis_ui as A, clock, layout as L


@pytest.mark.parametrize("interactive", [False, True])
@pytest.mark.parametrize("ascii_", [False, True])
@pytest.mark.parametrize("precision", [None, 3])
def test_reported_headers_keep_fit_and_observed_range_labels_with_exact_summaries(interactive, ascii_, precision):
    app = SimpleNamespace()
    state = A.initialize(app)
    state["chart_events"] = False
    if precision is not None:
        state["metric_display"]["loss"] = {"precision": precision}
    points = [{"t": float(index), "value": 50. + 20. * math.sin(index * 2.), "step": index}
              for index in range(2000)]
    metadata = {}
    rows = A.chart_rows(L.Glyphs(ascii_), app, points, 140, 8, "loss", "reported.jsonl",
                        interactive=interactive, metadata=metadata)
    assert metadata["trend"] and metadata["band"]
    header = L.row_text(rows[0])
    assert "[fit + range]" in header
    assert "last" in header and "mean" in header and "max" in header
    if precision is not None:
        assert f"last {points[-1]['value']:.3f}" in header
    description = next(L.row_text(row) for row in rows if " Axis " in L.row_text(row))
    assert "local quadratic fit" in description
    assert "observed low-high range" in description
    if interactive:
        observed = app.analysis_state["chart_visible"]["points"]
        assert len(observed) == len(points)
        assert observed[-1] == points[-1]
    if ascii_:
        assert all(L.row_text(row).isascii() for row in rows)


def test_single_observation_never_receives_a_model_label():
    app = SimpleNamespace()
    metadata = {}
    rows = A.chart_rows(L.Glyphs(False), app, [{"t": 0., "value": 40.}], 100, 8,
                        "loss", "reported.jsonl", interactive=False, metadata=metadata)
    assert not metadata["trend"] and not metadata["band"]
    assert "[fit" not in L.row_text(rows[0]) and "[range" not in L.row_text(rows[0])
    assert not any("Draw:" in L.row_text(row) for row in rows)


def test_normalized_source_and_cadence_are_reused_while_inplace_corrections_still_apply(monkeypatch):
    app = SimpleNamespace()
    points = [{"t": float(index), "value": float(index)} for index in range(2000)]
    calls = []
    median = A.statistics.median
    monkeypatch.setattr(A.statistics, "median", lambda values: (calls.append(1), median(values))[1])

    def draw(width=100):
        metadata = {}
        rows = A.chart_rows(L.Glyphs(False), app, points, width, 8, "loss", "reported.jsonl",
                            interactive=False, metadata=metadata)
        return rows, metadata

    first, _ = draw()
    original = next(iter(app.analysis_state["chart_source_cache"].values()))
    draw(101)  # A new viewport needs a new raster, but no new source preparation.
    assert next(iter(app.analysis_state["chart_source_cache"].values())) is original
    assert len(calls) == 1
    points[999]["value"] = 100000.
    changed, metadata = draw()
    assert changed != first and metadata["y_bounds"][1] > 100000.
    assert len(calls) == 1  # Corrected values do not change timestamp cadence.
    points[999]["t"] = 4000.
    draw()
    assert len(calls) == 2


def test_source_preparation_caches_are_bounded_and_not_saved():
    app = SimpleNamespace()
    for index in range(A.MAX_SOURCE_CACHE + 4):
        points = [{"t": float(t + index), "value": float(t)} for t in range(20)]
        A.chart_rows(L.Glyphs(False), app, points, 100, 8, "loss", "reported.jsonl", interactive=False)
    state = app.analysis_state
    assert len(state["chart_source_cache"]) == A.MAX_SOURCE_CACHE
    assert len(state["chart_cadence_cache"]) == A.MAX_SOURCE_CACHE
    saved = A.save(app)["analysis"]
    assert "chart_source_cache" not in saved and "chart_cadence_cache" not in saved
