"""Research rendering handles science-scale floats and hostile external text."""
import math
from types import SimpleNamespace

import pytest

from raven import charts, layout as L, research_views


@pytest.mark.parametrize("ascii_", [False, True])
@pytest.mark.parametrize("width", [0, 3, 12, 48, 100])
@pytest.mark.parametrize("values", [
    [-1e308, 1e308], [-1e308, -1e308], [1e308, 1e308],
    [-float.fromhex("0x1.fffffffffffffp+1023"), float.fromhex("0x1.fffffffffffffp+1023")],
    [1e-320, 2e-320], [0.0, 0.0],
])
def test_actual_experiment_view_handles_extreme_finite_metrics(ascii_, width, values):
    result = {"status": "ok", "path": "results\x1b]52;c;hidden\x07.jsonl", "records": 2,
              "series": {"metric\x1b[31m\u202e": [{"t": 1700000000 + i, "value": value}
                                                  for i, value in enumerate(values)]},
              "progress": {}, "errors": ["invalid\x00\nrecord"]}
    hub = SimpleNamespace(context=lambda snap, app: {"job": None}, request=lambda context: result)
    app = SimpleNamespace(research=hub, research_view="experiment", research_scroll=0, research_rows=0)
    views = SimpleNamespace(g=L.Glyphs(ascii_))
    rows, hits = research_views.render(views, {}, app, width, 80)
    rendered = L.to_text(rows, width)
    assert "\x1b" not in rendered and "\x00" not in rendered and "\u202e" not in rendered
    assert all(L.vlen(line) <= width for line in rendered.splitlines())
    if ascii_:
        assert rendered.isascii()


@pytest.mark.parametrize("ascii_", [False, True])
def test_extreme_positive_compositions_keep_correct_proportions(ascii_):
    rows = charts.stacked_bar(L.Glyphs(ascii_), [("A", 1e308, "cyan"), ("B", 1e308, "magenta")], 42)
    fills = {style: sum(len(text) for text, item_style in rows[0] if item_style == style)
             for style in ["cyan", "magenta"]}
    assert abs(fills["cyan"] - fills["magenta"]) <= 1
    assert fills["cyan"] > 0 and fills["magenta"] > 0


def test_resampled_finite_mean_does_not_overflow():
    observed = charts.resample([1e308] * 10, 1)[0]
    assert math.isfinite(observed) and observed == pytest.approx(1e308)


@pytest.mark.parametrize("ascii_", [False, True])
def test_out_of_range_epoch_does_not_crash_a_terminal_chart(ascii_):
    rows = charts.braille_chart(L.Glyphs(ascii_), [1.0, 2.0], 100, 4,
                                sample_times=[1e308, 1.7e308], title="timestamp boundaries")
    assert all(L.vlen(L.row_text(row)) <= 100 for row in rows)
