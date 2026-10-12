"""Replay seeks reset display buffers; transport controls preserve continuity."""

from types import SimpleNamespace

import pytest

from raven import clock, job_panels, metric_live as M, record
from raven.model import Job


KEY = ("resource-series", "42", "cpu-rate", "%", "attempt")


@pytest.fixture
def replay(monkeypatch):
    wall = [1000.0]
    monkeypatch.setattr(record.time, "monotonic", lambda: wall[0])
    timer = record.ReplayClock(0, 1000)
    timer.seek(100)
    monkeypatch.setattr(clock, "now", timer.now)
    app = SimpleNamespace(replay=SimpleNamespace(clock=timer), mode="main", tab="jobs")
    M.set_running(app, KEY, True)
    return SimpleNamespace(app=app, timer=timer, wall=wall)


def publish(replay, *, newest=100, previous=95, app=None):
    return M.display_end(app or replay.app, KEY, newest=newest, previous=previous,
                         oldest=0, poll_interval=5)


@pytest.mark.parametrize("action", ["seek", "skip"])
def test_short_forward_seek_resets_a_held_playhead_immediately(replay, action):
    # An outage left the display behind; an intentional seek must not spend
    # the next minutes catching up at normal presentation speed.
    assert publish(replay, newest=60, previous=55) == 60
    generation = replay.timer.generation
    if action == "seek":
        replay.timer.seek(105)
    else:
        replay.timer.skip(5)
    assert replay.timer.generation == generation + 1
    assert publish(replay) == 95
    assert M.playback_status(replay.app, KEY).lag == 10


def test_backward_seek_and_same_position_seek_reset_source_epoch(replay):
    assert publish(replay) == 90
    old = next(iter(replay.app.metric_playback_state.values()))
    replay.timer.seek(90)
    assert publish(replay, newest=90, previous=85) == 80
    after_rewind = next(iter(replay.app.metric_playback_state.values()))
    assert after_rewind is not old
    replay.timer.seek(90)
    assert publish(replay, newest=90, previous=85) == 80
    assert next(iter(replay.app.metric_playback_state.values())) is not after_rewind


def test_speed_changes_keep_generation_and_playhead_continuity(replay):
    assert publish(replay) == 90
    track = next(iter(replay.app.metric_playback_state.values()))
    generation = replay.timer.generation
    replay.timer.set_speed(2)
    assert replay.timer.generation == generation
    assert replay.timer.now() == 100
    assert publish(replay) == 90
    replay.wall[0] += .1
    assert replay.timer.now() == pytest.approx(100.2)
    assert publish(replay) == pytest.approx(90.2)
    assert next(iter(replay.app.metric_playback_state.values())) is track
    replay.timer.set_speed(.25)
    assert replay.timer.generation == generation
    assert publish(replay) == pytest.approx(90.2)


def test_pause_and_resume_hold_the_same_source_clock(replay):
    assert publish(replay) == 90
    track = next(iter(replay.app.metric_playback_state.values()))
    generation = replay.timer.generation
    assert replay.timer.toggle_pause() is True
    replay.wall[0] += 300
    assert replay.timer.now() == 100
    assert publish(replay) == 90
    assert replay.timer.generation == generation
    assert next(iter(replay.app.metric_playback_state.values())) is track
    assert replay.timer.toggle_pause() is False
    replay.wall[0] += .1
    assert publish(replay) == pytest.approx(90.1)
    assert replay.timer.generation == generation
    assert next(iter(replay.app.metric_playback_state.values())) is track


def test_jobs_details_proxy_reads_replay_epoch_from_chart_owner(replay):
    state = job_panels.initialize(replay.app)
    state["mode"] = "analytics"
    proxy, _ = job_panels._scoped_app(replay.app, Job("42", "train", "cpu", "RUNNING"), state)
    assert proxy._chart_owner is replay.app
    assert publish(replay, newest=60, previous=55, app=proxy) == 60
    assert proxy.metric_live_state is replay.app.metric_live_state
    assert not hasattr(proxy, "metric_playback_state")
    # The shallow panel proxy can outlive a replay backend reference. Its
    # presentation generation must still come from the owning application.
    proxy.replay = SimpleNamespace(clock=record.ReplayClock(0, 1000, paused=True))
    replay.timer.seek(105)
    assert publish(replay, app=proxy) == 95
    assert publish(replay) == 95
    assert len(replay.app.metric_playback_state) == 1


def test_replacing_replay_clock_resets_even_with_equal_generation(replay):
    assert publish(replay, newest=60, previous=55) == 60
    replacement = record.ReplayClock(0, 1000, paused=True)
    replacement.seek(100)
    assert replacement.generation == replay.timer.generation
    replay.app.replay = SimpleNamespace(clock=replacement)
    assert publish(replay) == 90
