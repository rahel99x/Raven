"""Unchanged maintenance frames keep exact native chart data and live controls."""
import math

import pytest

from raven import charts, chart_interaction as C, clock, layout as L, metric_live as M, refresh_rate as R
from raven.config import Config
from raven.controller import App
from raven.metric_raster import MetricRasterCache, MAX_ENTRIES, MAX_POINTS, MAX_CELLS, _freeze, coalesce_row
from raven.model import Job, Store
from raven.views import Views


@pytest.mark.parametrize('ascii_', [False, True])
@pytest.mark.parametrize('filled', [False, True])
def test_warm_rasters_match_original_renderer_and_detect_interior_corrections(ascii_, filled):
    cache, g = MetricRasterCache(), L.Glyphs(ascii_)
    values = [math.sin(i / 10) for i in range(150)]
    timestamps = list(range(150))
    options = dict(sample_times=timestamps, times=(0, 149), lo=-2, hi=2,
                   sample_interval=1, title='Measured data', unit='%')
    painter = charts.vbar_chart if filled else charts.braille_chart

    def check():
        expected_metadata = {}
        expected = painter(g, values, 90, 5, metadata=expected_metadata, **options)
        for _ in range(2):
            actual, metadata = cache.render(g, values, 90, 5, filled=filled, **options)
            assert actual == expected and metadata == expected_metadata
        return actual

    first = check()
    values[75] = 1.99
    assert check() != first
    timestamps[75] = 115.5
    check()
    values[75] = None
    check()
    values[75] = -0.0
    check()
    options.update(times=(60, 100), lo=-.5, hi=.5)
    check()


@pytest.mark.parametrize('tab', ['jobs', 'analytics'])
@pytest.mark.parametrize('ascii_', [False, True])
def test_maintenance_keeps_native_rasters_and_publishes_fresh_axes_controls_and_age(monkeypatch, tab, ascii_):
    cfg = Config({'animations': False, 'log_lines': 0, 'workspace': {'density': 'compact'}})
    store = Store(persist=False)
    store.apply_jobs([Job('7', 'exact', 'cpu', 'RUNNING', cpus=4, mem_req='8G')])
    for i in range(100):
        store.record('7', dict(k='live', t=1000 + i, cpu=.4, rss=1024 ** 3))
    app = App(store, None, None, cfg, 'test', interactive=False)
    app.tab, app.selected_id, app.analytics_job = tab, '7', '7'
    app.job_panel_state.update(mode='analytics', analytics_view='job')
    views = Views(L.Glyphs(ascii_), cfg)
    app.views_ref = views
    # A fully revealed, paused source reuses its raster while source age grows.
    # A flowing source deliberately moves its buffered edge between updates.
    timer = [1110.0]
    monkeypatch.setattr(clock, 'now', lambda: timer[0])
    calls, original = [], charts.braille_chart
    monkeypatch.setattr(charts, 'braille_chart', lambda *a, **kw: (calls.append(1), original(*a, **kw))[1])
    try:
        first, _ = views.compose(store.snapshot(), app, 180, 60)
        assert calls and C.initialize(app)['plots']
        controls = M.initialize(app)['records']
        assert controls and all(control.key[1] == '7' for control in controls)
        calls.clear()
        timer[0] += 1
        second, _ = views.compose(store.snapshot(), app, 180, 60)
        assert not calls
        assert C.initialize(app)['plots'] and M.initialize(app)['records']
        assert L.to_text(first, 180) != L.to_text(second, 180)
        assert 'Source age 12s' in L.to_text(second, 180)
        R.set_multiplier(app, 50)
        third, _ = views.compose(store.snapshot(), app, 180, 60)
        assert calls and '500ms' in L.to_text(third, 180)
        calls.clear()
        store.series['7'][50]['cpu'] = 2
        views.compose(store.snapshot(), app, 180, 60)
        assert calls
    finally:
        if app.research:
            app.research.close()


def test_cache_copies_published_rows_and_metadata_and_bounds_retained_work():
    cache, g = MetricRasterCache(), L.Glyphs(False)
    options = dict(title='Source', sample_times=[1, 2], times=(1, 2))
    first, metadata = cache.render(g, [1, 2], 80, 5, **options)
    expected = [list(row) for row in first]
    first[0][:] = [('caller highlight', 'sel')]
    metadata.clear()
    assert cache.render(g, [1, 2], 80, 5, **options)[0] == expected
    for i in range(MAX_ENTRIES + 5):
        cache.render(g, list(range(100)), 80, 5, title=str(i))
    assert len(cache.entries) <= MAX_ENTRIES
    assert cache.points <= MAX_POINTS and cache.cells <= MAX_CELLS


def test_extension_options_and_timezone_changes_never_reuse_wrong_raster(monkeypatch):
    cache, g = MetricRasterCache(), L.Glyphs(False)
    calls, original = [], charts.braille_chart
    monkeypatch.setattr(charts, 'braille_chart', lambda *a, **kw: (calls.append(1), original(*a, **kw))[1])
    cache.render(g, [1, 2], 80, 5)
    cache.render(g, [1, 2], 80, 5)
    assert len(calls) == 1
    monkeypatch.setenv('TZ', 'different-context')
    cache.render(g, [1, 2], 80, 5)
    assert len(calls) == 2
    class ExtensionLabel(str):
        pass
    cache.render(g, [1, 2], 80, 5, title=ExtensionLabel('extension'))
    cache.render(g, [1, 2], 80, 5, title=ExtensionLabel('extension'))
    assert len(calls) == 4


def test_numeric_signatures_preserve_special_values_types_and_interior_edits():
    values = [1., -0., float('inf'), float('nan')]
    original = _freeze(values)
    assert original == _freeze(list(values))
    values[1] = 0.
    assert original != _freeze(values)
    assert _freeze([1., 2.]) != _freeze([1, 2])
    assert _freeze([1, 2]) != _freeze((1, 2))
    assert _freeze([1, 2]) != _freeze([True, 2])
    assert _freeze([1, 2 ** 1000]) != _freeze([1, 2 ** 1000 + 1])


def test_mutable_axis_metadata_never_changes_cached_geometry():
    cache, g = MetricRasterCache(), L.Glyphs(False)
    options = dict(times=[1, 2], sample_times=[1, 2])
    _, first = cache.render(g, [1, 2], 80, 5, **options)
    first['x_bounds'][0] = 999
    _, second = cache.render(g, [1, 2], 80, 5, times=[1, 2], sample_times=[1, 2])
    assert second['x_bounds'] == [1, 2]
    second['x_bounds'][1] = 888
    assert cache.render(g, [1, 2], 80, 5, times=[1, 2], sample_times=[1, 2])[1]['x_bounds'] == [1, 2]


@pytest.mark.parametrize('ascii_', [False, True])
@pytest.mark.parametrize('filled', [False, True])
def test_coalesced_cached_chart_preserves_each_glyph_style_and_metadata(ascii_, filled):
    cache, g = MetricRasterCache(), L.Glyphs(ascii_)
    values = [None if index % 11 == 0 else 50 + 40 * math.sin(index / 20) for index in range(200)]
    options = dict(title='Exact curve', unit='%', sample_times=[index * .5 for index in range(200)],
                   times=(0., 99.5), sample_interval=.5, lo=0., hi=100.)
    original_rows, original_meta = cache.render(g, values, 140, 8, filled=filled, **options)

    def expanded(rows):
        return [[(char, style) for text, style in row for char in text] for row in rows]

    rows, metadata = cache.render(g, values, 140, 8, filled=filled, compact_rows=True, **options)
    assert expanded(rows) == expanded(original_rows) and metadata == original_meta
    assert sum(len(row) for row in rows) < sum(len(row) for row in original_rows) / 3
    expected = [list(row) for row in rows]
    rows[0][:] = [('caller selection', 'sel')]
    metadata.clear()
    warm, restored = cache.render(g, values, 140, 8, filled=filled, compact_rows=True, **options)
    assert warm == expected and restored == original_meta


def test_coalescing_preserves_combining_marks_and_extension_styles():
    row = [('界', 'cyan'), ('e', 'cyan'), ('\u0301', 'cyan'), ('x', 'dim'), ('y', 'dim')]
    assert coalesce_row(row) == [('界e\u0301', 'cyan'), ('xy', 'dim')]

    class Style(str):
        def __eq__(self, other):
            pytest.fail('An extension style ran equality during chart coalescing')

    style = Style('cyan')
    result = coalesce_row([('x', style), ('y', style)])
    assert len(result) == 2 and all(segment[1] is style for segment in result)
