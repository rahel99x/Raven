"""Screen-resolution gates keep published coordinates and source truth aligned."""

from types import SimpleNamespace

import pytest

from raven import metric_playback as P


@pytest.fixture
def app():
    return SimpleNamespace()


def present(app, end=90.0, now=100.0, identity=("line", "cpu"), **options):
    defaults = dict(geometry=(100, 32, 0., 100., "linear"), x_interval=2.,
                    edge_signature=(10, 89., 91., "connected"), oldest=0.)
    defaults.update(options)
    candidate = P.Playback(end, 10., now - end, 5., "live")
    return P.present(app, identity, candidate, now=now, **defaults)


def test_subpixel_flat_edge_motion_reuses_published_end_but_updates_actual_lag(app):
    assert present(app).end == 90
    for step in range(1, 10):
        result = present(app, end=90 + step / 5, now=100 + step / 5)
        assert result.end == 90
        assert result.lag == pytest.approx(10 + step / 5)
    assert present(app, end=92, now=102).end == 92


def test_steep_edge_publishes_on_vertical_pixel_motion_before_horizontal_threshold(app):
    present(app)
    result = present(app, end=90.01, now=100.01,
                     edge_signature=(11, 89., 91., "connected"))
    assert result.end == 90.01 and result.lag == pytest.approx(10)


@pytest.mark.parametrize("signature", [(10, 90., 92., "connected"),
                                       (10, 89., 91., "gap"),
                                       (None, 89., 91., "unknown"),
                                       (10, 89., 91., "duplicate-time-step")])
def test_bracket_changes_spikes_and_discontinuities_publish_even_with_same_y(signature, app):
    present(app)
    assert present(app, end=90.1, now=100.1, edge_signature=signature).end == 90.1


@pytest.mark.parametrize("options", [
    {"geometry": (101, 32, 0., 100., "linear")},
    {"geometry": (100, 64, 0., 100., "linear")},
    {"geometry": (100, 32, 0., 1000., "linear")},
    {"force_token": ("new-attempt",)}, {"force_token": ("new-replay-generation",)},
    {"force_token": ("new-rate",)}, {"force_token": ("new-theme",)},
    {"oldest": -1.}, {"oldest": 89.}, {"reveal_changed": True},
])
def test_changed_geometry_source_epoch_or_revealed_range_bypasses_gate(app, options):
    present(app)
    assert present(app, end=90.1, now=100.1, **options).end == 90.1


def test_new_buffer_or_stall_status_publishes_last_available_endpoint(app):
    present(app)
    point = P.Playback(90.1, 10., 10., 5., "held")
    result = P.present(app, ("line", "cpu"), point, now=100.1,
                       geometry=(100, 32, 0., 100., "linear"), x_interval=2.,
                       edge_signature=(10, 89., 91., "connected"), oldest=0.)
    assert result == point


def test_filled_companion_and_resized_plot_have_independent_presentation_gates(app):
    present(app)
    filled = present(app, end=90.1, now=100.1, identity=("area", "cpu"),
                      geometry=(100, 16, 0., 100., "linear"))
    assert filled.end == 90.1
    assert present(app, end=90.2, now=100.2).end == 90
    assert len(app.metric_presentation_state) == 2


def test_presentation_does_not_slow_or_modify_underlying_adaptive_clock(app):
    key = ("cpu", "42", "attempt")
    for index in range(8):
        now = 100 + index / 5
        raw = P.advance(app, key, now=now, newest=100, previous=95,
                        poll_interval=5, oldest=0)
        displayed = P.present(app, ("line", key), raw, now=now,
                              geometry=(100, 32), x_interval=2,
                              edge_signature=(10, 89, 95), oldest=0)
        assert raw.end == pytest.approx(now - 10)
        assert app.metric_playback_state[key].end == raw.end
        assert displayed.end == 90
        assert displayed.lag == pytest.approx(now - 90)


def test_rewind_and_oldest_rollover_publish_real_new_coordinates(app):
    present(app)
    assert present(app, end=80, now=90).end == 80
    assert present(app, end=89, now=90.1, oldest=89).end == 89


@pytest.mark.parametrize("options", [
    {"geometry": []}, {"edge_signature": {}}, {"force_token": []},
    {"x_interval": None}, {"x_interval": 0}, {"x_interval": -1},
    {"x_interval": float("inf")}, {"now": float("nan")},
])
def test_invalid_gate_inputs_discard_only_the_bypassed_presentation(app, options):
    present(app)
    present(app, identity=("area", "cpu"))
    other = app.metric_presentation_state[("area", "cpu")]
    present(app, end=90.1, **options)
    assert ("line", "cpu") not in app.metric_presentation_state
    assert app.metric_presentation_state[("area", "cpu")] is other


def test_unhashable_identity_bypasses_without_erasing_other_presentations(app):
    present(app)
    before = repr(app.metric_presentation_state)
    present(app, end=90.1, identity=[])
    assert repr(app.metric_presentation_state) == before


def test_valid_frame_after_invalid_geometry_never_rewinds_behind_bypassed_frame(app):
    assert present(app).end == 90
    assert present(app, end=90.1, now=100.1).end == 90
    bypassed = present(app, end=90.2, now=100.2, geometry=[])
    assert bypassed.end == 90.2
    assert present(app, end=90.3, now=100.3).end == 90.3


@pytest.mark.parametrize("invalid", [None, (), P.Playback(float("nan"), 10, 10, 5, "live")])
def test_invalid_playback_discards_the_gate_before_a_later_valid_frame(app, invalid):
    present(app)
    assert P.present(app, ("line", "cpu"), invalid, now=100.2,
                     geometry=(100, 32), x_interval=2, edge_signature=(1,)) is invalid
    assert ("line", "cpu") not in app.metric_presentation_state
    assert present(app, end=90.3, now=100.3).end == 90.3


def test_presentation_state_is_bounded_and_shared_only_by_exact_key_and_owner(app):
    proxy = SimpleNamespace(_chart_owner=app)
    for index in range(P.MAX_STREAMS + 4):
        present(proxy, identity=("line", index))
    assert len(app.metric_presentation_state) == P.MAX_STREAMS
    assert ("line", 0) not in app.metric_presentation_state
    assert not hasattr(proxy, "metric_presentation_state")


def test_reset_clears_presentation_and_source_clocks_together(app):
    key = ("line", "cpu")
    present(app, identity=key)
    P.advance(app, key, now=100, newest=100)
    P.reset(app, key)
    assert not app.metric_presentation_state and not app.metric_playback_state
    present(app)
    P.advance(app, ("source",), now=100, newest=100)
    P.reset(app)
    assert not app.metric_presentation_state and not app.metric_playback_state


def test_presentation_reset_preserves_source_clock_and_other_presentations(app):
    key, other = ("line", "cpu"), ("area", "cpu")
    raw = P.advance(app, key, now=100, newest=100)
    present(app, identity=key)
    present(app, identity=other)
    P.reset_presentation(app, key)
    assert key not in app.metric_presentation_state
    assert other in app.metric_presentation_state
    assert app.metric_playback_state[key].end == raw.end
    P.reset_presentation(app)
    assert not app.metric_presentation_state
    assert app.metric_playback_state[key].end == raw.end
