"""Live queue departures, accounting confirmation, and bounded terminal feedback."""
from __future__ import annotations

import pytest

from raven.config import Config
from raven.controller import App
from raven.layout import Glyphs, row_text
from raven.model import Finished, Job, Store
from raven.transitions import CompletionFeedback
from raven.views import Views


class MonotonicClock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


@pytest.fixture
def clock(monkeypatch):
    clock = MonotonicClock()
    monkeypatch.setattr("raven.transitions.time.monotonic", clock)
    return clock


def transition(seq, kind="departed", job="77", *, mono=100.0):
    return {"seq": seq, "kind": kind, "job": job, "name": "watched experiment",
            "state": "ACCOUNTING" if kind == "departed" else "COMPLETED",
            "t": 1_791_158_400.0, "mono": mono, "confirmed": kind == "history"}


def test_existing_accounting_and_old_transitions_do_not_flash_at_startup(clock):
    snapshot = {"job_transitions": [transition(4, "history")], "history_revision": 3}
    feedback = CompletionFeedback(snapshot)
    feedback.update(snapshot)
    assert feedback.new_history == 0
    assert not feedback.flash_on()
    assert not feedback.active
    assert feedback.moving() == []


def test_history_confirmation_flashes_twice_and_then_stops(clock):
    feedback = CompletionFeedback({"history_revision": 0})
    snapshot = {"job_transitions": [transition(1, "history")], "history_revision": 1}
    feedback.update(snapshot)
    assert feedback.new_history == 1
    phases = [(.1, True), (.4, False), (.8, True), (1.1, False), (1.5, False)]
    for age, expected in phases:
        clock.now = 100.0 + age
        feedback.update(snapshot)
        assert feedback.flash_on() is expected
    assert not feedback.active
    assert feedback.new_history == 1


def test_repeated_snapshot_and_history_redraw_never_restart_feedback(clock):
    feedback = CompletionFeedback()
    snapshot = {"job_transitions": [transition(1, "history")], "history_revision": 1}
    feedback.update(snapshot)
    started = feedback.flash_started
    for age in (.2, .9, 1.8, 10, 300):
        clock.now = 100.0 + age
        feedback.update(snapshot)
        assert feedback.flash_started == started
        assert feedback.new_history == 1
    assert not feedback.flash_on()
    assert not feedback.active


def test_history_acknowledgement_clears_badge_without_truncating_second_flash(clock):
    feedback = CompletionFeedback()
    feedback.update({"history_revision": 2})
    feedback.acknowledge()
    assert feedback.new_history == 0
    clock.now = 100.8
    assert feedback.flash_on()
    clock.now = 101.5
    assert not feedback.active


def test_queue_departure_moves_from_recorded_row_with_monotonic_progress(clock):
    feedback = CompletionFeedback()
    feedback.remember([(11, "job", "77"), (12, "fin", "900")])
    snapshot = {"job_transitions": [transition(1)], "history_revision": 0}
    feedback.update(snapshot)
    first, = feedback.moving()
    assert first["job"] == "77" and first["source"] == 11
    assert first["progress"] == 0
    clock.now = 100.8
    feedback.update(snapshot)
    assert feedback.moving()[0]["progress"] == pytest.approx(.5)
    clock.now = 101.7
    feedback.update(snapshot)
    assert feedback.moving() == []
    assert not feedback.active
    assert not feedback.flash_on()


def test_delayed_frame_does_not_replay_old_departure_motion(clock):
    feedback = CompletionFeedback()
    clock.now = 110.0
    feedback.update({"job_transitions": [transition(1, mono=100.0)]})
    assert not feedback.active
    assert feedback.moving() == []


def test_returned_job_cancels_departure_motion(clock):
    feedback = CompletionFeedback()
    feedback.update({"job_transitions": [transition(1)]})
    assert feedback.moving()
    clock.now = 100.1
    feedback.update({"job_transitions": [transition(1), transition(2, "returned")]})
    assert feedback.moving() == []
    assert feedback.new_history == 0


def test_simultaneous_departure_feedback_is_bounded(clock):
    feedback = CompletionFeedback()
    feedback.remember([(index, "job", str(index)) for index in range(400)])
    feedback.update({"job_transitions": [transition(index + 1, job=str(index)) for index in range(100)]})
    assert len(feedback.sources) <= 256
    assert len(feedback.moving()) <= 8


class LiveDashboard:
    def __init__(self, *, ascii_=True, interactive=True, animations=True, theme="default"):
        self.cfg = Config({"log_lines": 0, "animations": animations, "theme": theme})
        self.store = Store(persist=False)
        self.watch = Job("77", "watched experiment", "main", "RUNNING", cpus=2)
        self.other = Job("900", "remaining live experiment", "main", "RUNNING", cpus=4)
        self.store.apply_jobs([self.watch, self.other])
        self.store.apply_finished([Finished("66", "old completed experiment", "COMPLETED")])
        self.app = App(self.store, None, None, self.cfg, "reader", ascii_=ascii_, interactive=interactive)
        self.views = Views(Glyphs(ascii_), self.cfg)
        self.app.views_ref = self.views
        self.render()

    def render(self, width=130, height=35):
        rows, hits = self.views.compose(self.store.snapshot(), self.app, width, height)
        return "\n".join(row_text(row) for row in rows), rows, hits

    def depart(self):
        self.store.apply_jobs([self.other])
        return self.render()

    def confirm(self, state="COMPLETED"):
        self.store.apply_finished([Finished("77", "watched experiment", state, end="2026-10-05T12:00:00"),
                                   Finished("66", "old completed experiment", "COMPLETED")])
        return self.render()


def test_open_dashboard_moves_departed_job_into_recents_without_restart_or_success_guess(clock):
    dashboard = LiveDashboard()
    app_identity = id(dashboard.app)
    text, _, hits = dashboard.depart()
    assert "77" not in dashboard.app.visible_ids
    assert "77" in dashboard.app.recent_ids
    assert any(kind == "recent" and job_id == "77" for _, kind, job_id in hits)
    assert "watched experiment" in text
    assert "77" in dashboard.store.snapshot()["departed_jobs"]
    assert all(record.id != "77" for record in dashboard.store.finished)
    assert dashboard.app.completion.new_history == 0
    assert not dashboard.app.completion.flash_on()
    assert id(dashboard.app) == app_identity


@pytest.mark.parametrize("state", ["COMPLETED", "FAILED", "TIMEOUT", "CANCELLED", "OUT_OF_MEMORY", "NODE_FAIL", "PREEMPTED"])
def test_accounting_confirmation_updates_recents_and_history_in_place(clock, state):
    dashboard = LiveDashboard()
    dashboard.depart()
    clock.now += .1
    dashboard.confirm(state)
    assert dashboard.app.completion.new_history == 1
    assert dashboard.app.completion.flash_on()
    assert "77" in dashboard.app.recent_ids
    assert "77" not in dashboard.store.snapshot()["departed_jobs"]
    dashboard.app.handle("3")
    text, _, hits = dashboard.render()
    assert any(kind == "fin" and job_id == "77" for _, kind, job_id in hits)
    assert state in text and "watched experiment" in text
    assert dashboard.app.completion.new_history == 0


def test_accounting_arriving_before_queue_departure_confirms_when_queue_changes(clock):
    dashboard = LiveDashboard()
    dashboard.confirm("FAILED")
    assert dashboard.app.completion.new_history == 0
    assert all(record.id != "77" for record in dashboard.store.finished)
    clock.now += .1
    dashboard.depart()
    assert any(record.id == "77" and record.state == "FAILED" for record in dashboard.store.finished)
    assert dashboard.app.completion.new_history == 1
    assert dashboard.app.completion.flash_on()


@pytest.mark.parametrize("interactive,animations,theme", [(False, True, "default"), (True, False, "default"), (True, True, "reader")])
def test_static_modes_keep_live_state_updates_without_animation(clock, interactive, animations, theme):
    dashboard = LiveDashboard(interactive=interactive, animations=animations, theme=theme)
    dashboard.depart()
    dashboard.confirm("FAILED")
    assert not dashboard.app.animations_enabled
    assert "77" in dashboard.app.recent_ids
    dashboard.app.handle("3")
    text, _, hits = dashboard.render()
    assert "FAILED" in text
    assert any(kind == "fin" and job_id == "77" for _, kind, job_id in hits)
