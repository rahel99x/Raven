"""Time buckets reuse measurements independently of callable presentation."""
from decimal import Decimal
from fractions import Fraction
import re
import math
import sys

import pytest

from tower import charts as C, layout as L, metric_raster as R


def test_callable_style_redraws_reuse_buckets_but_keep_each_new_presentation(monkeypatch):
    cache, glyphs = R.MetricRasterCache(), L.Glyphs(False)
    values = [float(index % 7) for index in range(200)]
    times = [float(index) for index in range(200)]
    original, calls = C._time_points, []
    def prepare(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)
    monkeypatch.setattr(C, '_time_points', prepare)
    for hue in ('red', 'cyan', 'magenta'):
        options = dict(sample_times=times, sample_interval=1., times=(0.,199.),
                       title='Measured '+hue, color=lambda _, hue=hue:hue,
                       lo=0., hi=8.)
        rows, metadata = cache.render(glyphs, values, 90, 8, **options)
        assert len(calls) == 1
        with monkeypatch.context() as direct:
            direct.setattr(C, '_time_points', original)
            expected = {}
            assert rows == C.braille_chart(glyphs, values, 90, 8, metadata=expected, **options)
            assert metadata == expected
        assert hue in ''.join(text for row in rows for text, _ in row)
    assert not cache.entries  # Existing callable presentation contract is kept.
    assert len(cache.time_points.entries) == 1


@pytest.mark.parametrize('ascii_', [False, True])
@pytest.mark.parametrize('filled', [False, True])
@pytest.mark.parametrize('envelope', [False, True])
@pytest.mark.parametrize('samples', [
    [(0.,0.), (1.,100.), (1.,0.), (2.,100.), (3.,0.)],
    [(0.,1.), (1.,None), (2.,3.), (300.,1.), (301.,None), (302.,4.)],
    [(3.,2.), (2.,3.), (1.,2.), (0.,1.)],
    [(0.,0.), (1.,float('nan')), (2.,float('inf')), (3.,-1.)],
    [(0.,-sys.float_info.max), (1.,sys.float_info.max), (2.,-sys.float_info.max)],
    [(0.,math.ulp(0.)), (0.,math.ulp(0.)), (1.,math.ulp(0.)*2)],
])
def test_cached_raster_exactly_preserves_extrema_gaps_steps_and_extreme_floats(ascii_,filled,envelope,samples):
    cache, glyphs = R.MetricRasterCache(), L.Glyphs(ascii_)
    times, values = map(list, zip(*samples))
    painter = C.vbar_chart if filled else C.braille_chart
    for selected in (None, (.5,1.5), (0.,300.)):
        options = dict(sample_times=times, sample_interval=1., times=selected,
                       title='Evidence', envelope=envelope, color=lambda _:'cyan')
        reference = {}
        expected = painter(glyphs, values, 65, 7, metadata=reference, **options)
        for _ in range(2):
            actual, metadata = cache.render(glyphs, values, 65, 7, filled=filled, **options)
            assert actual == expected and metadata == reference


def test_in_place_value_and_time_corrections_do_not_reuse_old_bins(monkeypatch):
    cache, glyphs = R.MetricRasterCache(), L.Glyphs(False)
    values, times = [float(i%11) for i in range(200)], [float(i) for i in range(200)]
    options = dict(sample_times=times, sample_interval=1., title='corrected', color=lambda _:'cyan')
    original, calls = C._time_points, []
    def prepare(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)
    monkeypatch.setattr(C, '_time_points', prepare)
    first = cache.render(glyphs, values, 100, 6, **options)
    values[50] = 10000.
    second = cache.render(glyphs, values, 100, 6, **options)
    times[100] = 20000.
    third = cache.render(glyphs, values, 100, 6, **options)
    assert len(calls) == 3 and first != second and second != third


def test_zoom_width_cadence_detail_and_fitted_axis_changes_use_their_actual_geometry():
    cache, glyphs = R.MetricRasterCache(), L.Glyphs(False)
    values = [float((index%13)*1000) for index in range(200)]
    times = [float(index) for index in range(200)]
    defaults = dict(sample_times=times, sample_interval=1., title='Range', color=lambda _:'cyan')
    variants = [({}, 80), ({'times':(80.,85.), 'fitted':True, 'time_units':True},80),
                ({'axis_formatter':lambda value:f'{value:.8f}', 'fitted':True},80),
                ({'sample_interval':.01},80), ({'title':''},80), ({},31),
                ({'envelope':False},80)]
    for changed, width in variants:
        options = {**defaults, **changed}
        reference = {}
        expected = C.braille_chart(glyphs, values, width, 7, metadata=reference, **options)
        for _ in range(2):
            rows, metadata = cache.render(glyphs, values, width, 7, **options)
            assert rows == expected and metadata == reference
    assert len(cache.time_points.entries) == len(variants)


def test_prepared_details_and_vertices_are_detached_between_consumers():
    cache = R.TimePointCache()
    values = [float(i%9) for i in range(500)]
    times = [float(i) for i in range(500)]
    prepared = cache.bind(values, times, R._freeze(values))
    details = {}
    points, bounds = prepared(values, times, 20, [0.,499.], 1., True, details)
    expected_points = list(points)
    expected_details = {x:dict(value) for x,value in details.items()}
    points[:] = [(999,999,False)]
    details[next(iter(details))]['turns'] = 999
    bounds[0] = 999
    actual_details = {}
    actual_points, actual_bounds = prepared(values, times, 20, [0.,499.], 1., True, actual_details)
    assert actual_points == expected_points
    assert actual_details == expected_details
    assert actual_bounds == [0.,499.]


def test_preparation_bounds_account_for_source_and_generated_geometry(monkeypatch):
    monkeypatch.setattr(R,'MAX_PREPARED_ENTRIES',2)
    monkeypatch.setattr(R,'MAX_PREPARED_POINTS',2000)
    cache = R.TimePointCache()
    values = [float(i%9) for i in range(30)]
    times = [float(i) for i in range(30)]
    prepare = cache.bind(values,times,R._freeze(values))
    for width in range(1,20):
        prepare(values,times,width,None,1.,True,{})
        assert len(cache.entries) <= 2 and cache.points <= 2000
    monkeypatch.setattr(R,'MAX_PREPARED_POINTS',1)
    other = R.TimePointCache()
    prepared = other.bind(values,times,R._freeze(values))
    prepared(values,times,20,None,1.,True,{})
    assert not other.entries and other.points==0


@pytest.mark.parametrize('values,times', [
    ([Decimal('1'),Decimal('2')],[0.,1.]),
    ([Fraction(1),Fraction(2)],[0.,1.]),
    ([1.,2.],range(2)),
])
def test_unsupported_numeric_or_sequence_types_keep_original_uncached_behavior(values,times):
    cache, glyphs = R.MetricRasterCache(), L.Glyphs(False)
    options = dict(sample_times=times, sample_interval=1., title='Extensions', color=lambda _:'cyan')
    expected = {}
    try:
        direct = C.braille_chart(glyphs,values,80,6,metadata=expected,**options)
    except (TypeError, ValueError, OverflowError) as error:
        # Unsupported extensions retain the painter's original failure too.
        with pytest.raises(type(error), match=re.escape(str(error))):
            cache.render(glyphs,values,80,6,**options)
        assert not cache.time_points.entries
        return
    for _ in range(2):
        rows, metadata = cache.render(glyphs,values,80,6,**options)
        assert rows==direct and metadata==expected
    assert not cache.time_points.entries


class LegacyFloat(float):
    """Select the original extensible-numeric coordinate path."""


class LegacyInt(int):
    """Integer arithmetic remains exact while selecting the fallback path."""


@pytest.mark.parametrize('envelope', [False, True])
@pytest.mark.parametrize('detail', [False, True])
def test_native_coordinate_fast_path_matches_original_fraction_binning(envelope,detail):
    import random
    randomizer = random.Random(60819)
    cases = [
        ([0.,1.,1.,2.,1000.], None),
        ([-sys.float_info.max,0.,sys.float_info.max], None),
        ([0.,math.ulp(0.),math.ulp(0.)*2],None),
        ([2**55,2**55+1,2**55+2,2**55+3],None),
        ([1800000000.+index*math.ulp(1800000000.) for index in range(80)],None),
    ]
    for _ in range(40):
        stamps = [randomizer.randrange(-50,51)/10 for _ in range(70)]
        selected = tuple(sorted(randomizer.sample(range(-50,51),2))) if randomizer.random()<.5 else None
        cases.append((stamps,selected))
    for times, bounds in cases:
        values = [None if randomizer.random()<.15 else randomizer.uniform(-1e5,1e5) for _ in times]
        fallback = [(LegacyInt(value) if type(value) is int else LegacyFloat(value)) for value in times]
        for width in (0,1,2,7,80,2048):
            actual_details, expected_details = ({} if detail else None), ({} if detail else None)
            actual = C._time_points(values,times,width,bounds,1.,envelope,actual_details)
            expected = C._time_points(values,fallback,width,bounds,1.,envelope,expected_details)
            assert actual == expected and actual_details == expected_details
