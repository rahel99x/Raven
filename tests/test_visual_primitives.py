"""Visual accuracy, not just glyph snapshots: gaps, fractional values and scales."""
import math

import pytest

from raven import charts, layout as L


def test_fractional_meters_have_eighth_cell_precision_and_clamp():
    g = L.Glyphs(False)
    assert L.bar(g, 1 / 16, 2)[0] == "▏░"
    assert L.bar(g, 7 / 16, 2)[0] == "▉░"
    assert L.bar(g, 9 / 16, 2)[0] == "█▏"
    assert L.bar(g, -1, 2)[0] == "░░"
    assert L.bar(g, 2, 2)[0] == "██"
    assert L.bar(g, math.nan, 2) == ("░░", "dim")
    assert L.bar(g, 0.5, -1)[0] == ""
    assert L.bar(L.Glyphs(True), 0.5, 10) == ("#####-----", "green")


def test_gradients_preserve_fractional_shape_without_ansi_in_text():
    row = L.gradient_bar(L.Glyphs(False), 0.5625, 2)
    assert L.row_text(row) == "█▏"
    from raven import palette as P
    assert all(P.resolve(style).foreground is not None for _, style in row)
    assert P.resolve(row[0][1], "darcula").foreground != P.resolve(row[0][1]).foreground
    assert row[0][1] != row[1][1]
    assert "\x1b" not in L.to_text([row], 2)
    assert L.row_text(L.gradient_bar(L.Glyphs(False), None, 5)) == "░░?░░"
    assert L.row_text(L.gradient_bar(L.Glyphs(False), 0, 5)) == "░░░░░"


def test_sparks_and_reduced_buckets_preserve_unobserved_intervals():
    g = L.Glyphs(False)
    assert L.spark(g, [0, None, 1], 3) == "▁ █"
    assert L.spark(g, [0, math.inf, 1], 3) == "▁ █"
    assert L.spark(g, [1], 0) == ""
    assert charts.resample([0, None, 100, 100], 2) == [None, 100]
    assert charts.resample([0, 0, 100, 100], 2) == [0, 100]


def test_chart_summary_reports_newest_unknown_without_reusing_older_reading():
    rows = charts.braille_chart(L.Glyphs(False), [10, 20, None], 90, 4, title="CPU")
    assert "last ?" in L.row_text(rows[0])
    assert "mean 15" in L.row_text(rows[0])
    assert "awaiting samples" in L.row_text(charts.vbar_chart(L.Glyphs(False), [None], 80, 3, title="CPU")[0])


def test_narrow_chart_headers_keep_complete_statistics_and_units():
    header = L.row_text(charts.braille_chart(L.Glyphs(False), [20, 93], 67, 4,
                        title="GPU occupancy on a very long training job", unit="%")[0])
    assert "last 93%" in header
    assert "…" in header
    tokens = header.split()
    for stat in ("last", "mean", "max", "min"):
        if stat in tokens:
            assert tokens[tokens.index(stat) + 1].endswith("%")


def _masks(rows, offset=10):
    # Decode the fine 2 x 4 raster so these assertions inspect measurements,
    # rather than accepting any incidental axis or quiet grid character.
    return [[charts.BRAILLE.index(ch) if ch in charts.BRAILLE else 0
             for ch in L.row_text(row)[offset:]] for row in rows]


def test_fine_curve_has_real_zero_points_and_does_not_bridge_missing_samples():
    g = L.Glyphs(False)
    # 12 columns total -> two data cells, exactly four raster sample positions.
    observed = _masks(charts.braille_chart(g, [0, 0, 0, 0], 12, 4, hi=100)[:-1])
    missing = _masks(charts.braille_chart(g, [None] * 4, 12, 4, hi=100)[:-1])
    assert any(any(row) for row in observed)
    assert not any(any(row) for row in missing)
    # Adjacent extrema produce a connecting stroke. A missing entire cell stays blank.
    rows = charts.braille_chart(g, [0, None, None, 100, 100, 100], 13, 4, hi=100)
    cells = _masks(rows[:-1])
    assert all((row[0] & (8 | 16 | 32 | 128)) == 0 for row in cells)
    assert all((row[1] & (1 | 2 | 4 | 64)) == 0 for row in cells)


def test_timestamp_raster_keeps_actual_locations_and_breaks_backoff_gaps():
    g = L.Glyphs(False)
    # The two early measurements are adjacent at the left; the later point is at
    # the right. Lower-median cadence also recognizes this outage with no hint.
    for interval in (None, 10):
        rows = charts.braille_chart(g, [25, 30, 80], 50, 5, hi=100,
                                   sample_times=[0, 10, 1800], sample_interval=interval)
        masks = _masks(rows[:-2])  # plot, baseline, automatically derived time axis
        assert any(row[0] for row in masks)
        assert any(row[-1] for row in masks)
        assert all(not any(row[1:-1]) for row in masks)
    area = charts.vbar_chart(g, [25, 30, 80], 50, 5, hi=100, sample_times=[0, 10, 1800])
    assert all(not any(ch in L.SPARK for ch in L.row_text(row)[11:-1]) for row in area[:-2])


def test_timestamp_curves_connect_regular_samples_and_preserve_explicit_unknown():
    g = L.Glyphs(False)
    regular = _masks(charts.braille_chart(g, [25, 50, 75], 50, 5, hi=100,
                                        sample_times=[0, 10, 20], sample_interval=10)[:-2])
    assert all(any(row[x] for row in regular) for x in range(40))
    unknown = _masks(charts.braille_chart(g, [25, None, 75], 50, 5, hi=100,
                                        sample_times=[0, 10, 20], sample_interval=10)[:-2])
    assert all(not any(row[1:-1]) for row in unknown)


def test_elapsed_comparison_axes_use_duration_labels_instead_of_epoch_wallclock():
    text = L.row_text(charts.time_axis(0, 3600, 50, elapsed=True))
    assert "0h" in text and "1h" in text and ":" not in text
    rows = charts.braille_chart(L.Glyphs(False), [10, 20], 60, 3, sample_times=[0, 3600],
                               sample_interval=10, elapsed=True)
    assert "1h" in L.row_text(rows[-1]) and ":" not in L.row_text(rows[-1])


@pytest.mark.parametrize("ascii_", [True, False])
@pytest.mark.parametrize("width", [0, 1, 3, 9, 12, 24, 80])
def test_all_advanced_primitives_fit_even_narrow_terminals(ascii_, width):
    g = L.Glyphs(ascii_)
    rows = charts.braille_chart(g, [0, None, 20, 100, math.nan], width, 5, title="CPU", times=(0, 3600))
    rows += charts.vbar_chart(g, [0, None, 20, 100], width, 5, title="Memory")
    rows += charts.heatmap(g, [[0, None, 100], [None, 50, math.inf]], width, labels=["node-a", "node-b"], title="Fleet")
    rows += charts.stacked_bar(g, [("idle", 3, "cyan"), ("busy", 2, "magenta")], width, title="Nodes")
    rows += charts.hbar_rows(g, [("node-a", 12, "red"), ("node-b", None, "")], width)
    assert all(L.vlen(L.row_text(row)) <= width for row in rows)
    text = L.to_text(rows, width)
    assert "\x1b" not in text
    assert text.isascii() if ascii_ else "#" not in text


def test_heatmap_shares_scale_and_labels_unknown_and_latest_values():
    rows = charts.heatmap(L.Glyphs(False), [[0, 100], [0, None]], 50,
                          labels=["observed", "unknown"], title="Utilization", hi=100, unit="%")
    assert "0% to 100%" in L.row_text(rows[0])
    assert L.row_text(rows[1]).rstrip().endswith("100%")
    assert L.row_text(rows[2]).rstrip().endswith("?")
    assert "··" in L.row_text(rows[2])
    assert "unobserved" in L.row_text(rows[-1])


def test_stacks_keep_proportions_and_do_not_claim_unknown_is_zero():
    g = L.Glyphs(False)
    rows = charts.stacked_bar(g, [("A", 3, "cyan"), ("B", 1, "red"), ("C", None, "dim")], 21)
    segments = [(text, style) for text, style in rows[0] if style in ("cyan", "red")]
    assert segments == [("█" * 14, "cyan"), ("█" * 5, "red")]
    assert "│" in L.row_text(rows[0])  # Composition is legible without colour.
    assert "C ?" in L.row_text(rows[1])
    assert "total 0" in L.row_text(charts.stacked_bar(g, [("A", 0, "cyan")], 40)[1])
    assert "awaiting samples" in L.row_text(charts.stacked_bar(g, [("A", None, "cyan")], 40)[1])


def test_gantt_does_not_clamp_outside_jobs_into_fabricated_edge_activity():
    g = L.Glyphs(False)
    rows = charts.gantt(g, [dict(id="1", name="old", state="COMPLETED", start=0, end=10),
                          dict(id="2", name="future", state="COMPLETED", start=300, end=400),
                          dict(id="3", name="unknown", state="COMPLETED", start=150)], 100, 200, 80)
    assert "█" not in L.row_text(rows[1])
    assert "█" not in L.row_text(rows[2])
    assert "?" in L.row_text(rows[3])
    assert "█" not in L.row_text(rows[3])
