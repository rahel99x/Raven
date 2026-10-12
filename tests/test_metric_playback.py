"""Presentation buffering preserves source truth and bounded UI work."""

from collections import OrderedDict
import math
import random
from types import SimpleNamespace

import pytest

from raven import metric_playback as P


@pytest.fixture
def app():
    return SimpleNamespace()


def tick(app, now=100.0, newest=100.0, **kwargs):
    return P.advance(app, ("cpu", "42", "attempt-one"), now=now,
                     newest=newest, **kwargs)


def test_initial_clock_delays_two_effective_intervals(app):
    point = tick(app, previous=99.5, poll_interval=0.5, oldest=1.0)
    assert point == P.Playback(99.0, 1.0, 1.0, 0.5, "live")
    # No publication is required to reveal the already received next segment.
    following = tick(app, now=100.1, newest=100.0, previous=99.5,
                     poll_interval=0.5, oldest=1.0)
    assert following.end == pytest.approx(99.1)
    assert following.lag == pytest.approx(1.0)
    assert following.status == "live"


def test_actual_cadence_and_effective_polling_both_bound_buffer(app):
    assert tick(app, previous=99.5, poll_interval=5).delay == 10
    P.reset(app)
    assert tick(app, previous=96, poll_interval=.5).delay == 8
    P.reset(app)
    assert tick(app, previous=0, poll_interval=100).delay == P.MAX_DELAY


def test_single_sample_startup_does_not_begin_before_available_history(app):
    point = tick(app, newest=100, oldest=100, poll_interval=5)
    assert point.end == 100
    assert point.status == "buffering"
    for now in (100.1, 101, 104):
        point = tick(app, now=now, newest=100, oldest=100, poll_interval=5)
        assert point.end == 100
        assert point.status == "buffering"
    # The next sample is real, but the presentation buffer is not ready yet.
    point = tick(app, now=105, newest=105, previous=100, oldest=100,
                 poll_interval=5)
    assert point.end == 100 and point.status == "buffering"
    point = tick(app, now=110.1, newest=110, previous=105, oldest=100,
                 poll_interval=5)
    assert point.end == pytest.approx(100.1) and point.status == "live"


def test_outage_stops_at_last_received_sample_and_reports_growing_lag(app):
    tick(app, previous=99.5, poll_interval=.5)
    result = None
    for now in (100.25, 100.5, 100.75, 101, 102, 103, 110):
        result = tick(app, now=now, newest=100, previous=99.5,
                      poll_interval=.5)
        assert result.end <= 100
    assert result == P.Playback(100, 1, 10, .5, "held")
    # Delivery after a held period must not teleport to the new publication.
    resumed = tick(app, now=110.1, newest=110, previous=109.5,
                   poll_interval=.5)
    assert resumed.end == pytest.approx(100.125)
    assert resumed.lag == pytest.approx(9.975)


def test_retention_rollover_resumes_at_actual_oldest_without_losing_cadence(app):
    first = tick(app, previous=99, poll_interval=.5, oldest=0)
    track = next(iter(app.metric_playback_state.values()))
    assert first.end == 98 and list(track.intervals) == [1]
    retained = tick(app, now=100.1, newest=100, previous=99,
                    poll_interval=.5, oldest=99.5)
    assert retained.end == 99.5 and retained.status == "buffering"
    assert retained.delay == 2 and retained.interval == 1
    assert next(iter(app.metric_playback_state.values())) is track
    assert list(track.intervals) == [1]
    following = tick(app, now=101.6, newest=101, previous=100,
                     poll_interval=.5, oldest=99.5)
    assert following.end == pytest.approx(99.6)
    assert following.status == "live"


def test_slower_polling_holds_instead_of_reversing_time(app):
    initial = tick(app, previous=99.5, poll_interval=.5)
    slower = tick(app, now=100.1, newest=100, previous=99.5,
                  poll_interval=5)
    assert slower.end == initial.end and slower.delay == 10
    assert slower.status == "held"
    # Even repeated control paints at the same instant keep the held label.
    same = tick(app, now=100.1, newest=100, previous=99.5, poll_interval=5)
    assert same == slower


def test_faster_polling_recovers_at_bounded_speed(app):
    previous = tick(app, previous=99.5, poll_interval=5)
    last_now = 100.0
    for now in (100.1, 100.2, 100.3, 101, 102, 103):
        result = tick(app, now=now, newest=now, previous=now - .5,
                      poll_interval=.5)
        assert previous.end <= result.end <= previous.end + 1.25 * (now - last_now)
        previous, last_now = result, now


def test_median_estimate_resists_one_long_publication_gap(app):
    tick(app, now=10, newest=10, previous=9, poll_interval=.5)
    for now in (11, 12, 13):
        assert tick(app, now=now, newest=now, previous=now - 1,
                    poll_interval=.5).interval == 1
    # An irregular timestamp is preserved in the caller's data; it does not
    # push the presentation clock backwards or poison a robust cadence.
    point = tick(app, now=23, newest=23, previous=13, poll_interval=.5)
    assert point.interval == 1 and point.delay == 2


def test_repeated_paints_do_not_reweight_cadence(app):
    tick(app, previous=99, poll_interval=.5)
    tick(app, now=102, newest=102, previous=100, poll_interval=.5)
    for step in range(100):
        tick(app, now=102 + step / 1000, newest=102, previous=100,
             poll_interval=.5)
    track = next(iter(app.metric_playback_state.values()))
    assert list(track.intervals) == [1, 2]


def test_distinct_publications_supply_cadence_without_previous_metadata(app):
    assert tick(app, poll_interval=.5).interval == .5
    assert tick(app, now=103, newest=103, poll_interval=.5).interval == 3


@pytest.mark.parametrize("value", [None, True, False, "1", math.inf, -math.inf,
                                     math.nan, 10**1000])
@pytest.mark.parametrize("field", ["now", "newest"])
def test_invalid_clock_or_latest_does_not_change_state(app, field, value):
    tick(app, previous=99)
    before = repr(app.metric_playback_state)
    values = {"now": 100, "newest": 100, field: value}
    assert tick(app, **values) is None
    assert repr(app.metric_playback_state) == before


def test_future_samples_and_overflow_do_not_change_state(app):
    tick(app)
    before = repr(app.metric_playback_state)
    assert tick(app, now=100, newest=101) is None
    assert tick(app, now=1e308, newest=-1e308) is None
    assert repr(app.metric_playback_state) == before


@pytest.mark.parametrize("value", [None, True, "1", 0, -1, math.nan, math.inf,
                                     10**1000])
def test_invalid_polling_uses_observed_interval(app, value):
    assert tick(app, previous=99.5, poll_interval=value).interval == .5


@pytest.mark.parametrize("value", [None, True, "1", 101, 100, math.inf, math.nan])
def test_invalid_previous_uses_effective_polling(app, value):
    assert tick(app, previous=value, poll_interval=2).interval == 2


def test_fallback_interval_requires_no_collector(app):
    assert tick(app).interval == P.DEFAULT_INTERVAL


@pytest.mark.parametrize("change", ["generation", "rewind", "source", "idle"])
def test_discontinuities_reset_the_clock_and_interval_history(app, change):
    first = tick(app, now=100, newest=100, previous=80, poll_interval=.5,
                 generation=1)
    values = dict(now=100.1, newest=100, previous=99.5, poll_interval=.5,
                  generation=1)
    if change == "generation":
        values["generation"] = 2
    elif change == "rewind":
        values.update(now=10, newest=10, previous=9.5)
    elif change == "source":
        values.update(newest=90, previous=89.5)
    else:
        values.update(now=200, newest=200, previous=199.5)
    result = tick(app, **values)
    assert result.end == min(values["now"] - 1, values["newest"])
    assert result.interval == .5
    assert result.end != first.end


def test_exact_keys_and_explicit_reset_are_independent(app):
    tick(app, previous=99)
    key = ("cpu", "42", "attempt-two")
    other = P.advance(app, key, now=200, newest=200, previous=199.5)
    assert other.end == 199
    assert len(app.metric_playback_state) == 2
    P.reset(app, key)
    assert len(app.metric_playback_state) == 1
    P.reset(app)
    assert not app.metric_playback_state


def test_aliases_use_shared_owner_state_without_copying_snapshots(app):
    alias = SimpleNamespace(_chart_owner=app)
    first = tick(app, previous=99.5)
    assert tick(alias, previous=99.5) == first
    assert not hasattr(alias, "metric_playback_state")
    P.reset(alias)
    assert not app.metric_playback_state


def test_storage_and_interval_history_are_bounded_and_lru(app):
    for key in range(P.MAX_STREAMS):
        P.advance(app, (key,), now=100, newest=100, previous=99)
    P.advance(app, (0,), now=100, newest=100, previous=99)
    P.advance(app, (P.MAX_STREAMS,), now=100, newest=100, previous=99)
    assert len(app.metric_playback_state) == P.MAX_STREAMS
    assert (0,) in app.metric_playback_state and (1,) not in app.metric_playback_state
    for step in range(100):
        P.advance(app, (0,), now=101 + step, newest=101 + step, previous=100 + step)
    assert len(app.metric_playback_state[(0,)].intervals) == P.MAX_INTERVALS


def test_invalid_identity_does_not_create_state(app):
    assert P.advance(app, [], now=100, newest=100) is None
    assert not hasattr(app, "metric_playback_state")
    P.reset(app, [])
    assert app.metric_playback_state == OrderedDict()


def test_jitter_rate_changes_and_outages_keep_monotonic_bounded_steps(app):
    rng = random.Random(512)
    now, newest, previous = 100.0, 100.0, 99.5
    result = tick(app, now=now, newest=newest, previous=previous,
                  poll_interval=.5, oldest=0)
    for _ in range(2000):
        elapsed = rng.uniform(.01, .2)
        now += elapsed
        if rng.random() < .15:
            newest, previous = now, newest
        following = tick(app, now=now, newest=newest, previous=previous,
                         poll_interval=rng.choice((.5, 1, 5)), oldest=0)
        assert result.end <= following.end <= newest <= now
        assert following.end - result.end <= 1.25 * elapsed + 1e-10
        assert 0 < following.delay <= P.MAX_DELAY
        assert following.lag == now - following.end
        result = following
