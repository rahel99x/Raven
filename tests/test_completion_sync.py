"""Queue-to-accounting reconciliation: no invented success, missed refresh, or duplicate transition."""
from dataclasses import replace
import threading
from types import SimpleNamespace

import pytest

from raven.model import COMPLETION_KEEP, TRANSITION_KEEP, Finished, Job, Store
from raven.sampler import HISTORY_FAST_ATTEMPTS, Sampler
from raven.slurm import CommandError


def active(jid="42", **kw):
    return Job(jid, "training", "gpu", "RUNNING", **kw)


def done(jid="42", state="COMPLETED", **kw):
    return Finished(jid, "training", state, **kw)


def memory():
    return Store(persist=False)


def test_initial_history_is_not_a_live_completion():
    store = memory()
    store.apply_finished([done()])
    store.apply_jobs([])
    assert [j.id for j in store.finished] == ["42"]
    assert store.history_revision == 0
    assert store.snapshot()["job_transitions"] == []


def test_departure_preserves_last_job_without_inventing_terminal_status():
    store = memory()
    job = active("42_7", hosts=["gpu01"])
    store.apply_jobs([job])
    store.details[job.id] = {"StdOut": "/run/stdout.log", "WorkDir": "/run"}
    store.apply_jobs([])
    job.hosts.append("later-mutation")
    snap = store.snapshot()
    assert snap["finished"] == [] and snap["jobs"] == []
    assert snap["departed_jobs"]["42_7"].state == "RUNNING"
    assert snap["departed_jobs"]["42_7"].hosts == ["gpu01"]
    assert snap["details"]["42_7"]["StdOut"] == "/run/stdout.log"
    transition = snap["job_transitions"][-1]
    assert transition["kind"] == "departed" and transition["state"] == "ACCOUNTING"
    assert transition["confirmed"] is False and transition["seq"] == 1 and transition["mono"] > 0
    assert store.events[-1]["confirmed"] is False


@pytest.mark.parametrize("accounting_first", [False, True])
@pytest.mark.parametrize("state", ["COMPLETED", "FAILED", "CANCELLED", "OUT_OF_MEMORY", "TIMEOUT"])
def test_both_poll_orders_confirm_once_and_never_duplicate_active_job(accounting_first, state):
    store = memory()
    store.apply_jobs([active()])
    if accounting_first:
        store.apply_finished([done(state=state)])
        assert store.finished == [] and store.history_revision == 0
        store.apply_jobs([])
    else:
        store.apply_jobs([])
        store.apply_finished([])  # Accounting lag is normal, not completion.
        assert store.departed_jobs and not store.finished
        store.apply_finished([done(state=state)])
    for _ in range(3):
        store.apply_jobs([])
        store.apply_finished([done(state=state)])
    assert [(j.id, j.state) for j in store.finished] == [("42", state)]
    assert store.departed_jobs == {} and store.history_revision == 1
    assert [event["kind"] for event in store.job_transitions] == ["departed", "history"]
    assert store.job_transitions[-1]["confirmed"] is True


def test_return_before_accounting_cancels_pending_transition():
    store = memory()
    store.apply_jobs([active(start="2026-10-05T01:00:00")])
    store.apply_jobs([])
    store.apply_jobs([active(start="2026-10-05T02:00:00")])
    stale = done(start="2026-10-05T01:00:00", end="2026-10-05T01:59:00")
    store.apply_finished([stale])
    assert not store.departed_jobs and not store.finished and store.history_revision == 0
    assert store.job_transitions[-1]["kind"] == "returned"
    store.apply_jobs([])
    store.apply_finished([stale])
    assert store.departed_jobs and not store.finished
    store.apply_finished([done(start="2026-10-05T02:00:00", end="2026-10-05T02:01:00")])
    assert store.history_revision == 1


def test_confirmed_job_requeue_does_not_reconfirm_old_attempt():
    store = memory()
    old = done(start="2026-10-05T01:00:00", end="2026-10-05T02:00:00")
    store.apply_jobs([active()])
    store.apply_jobs([])
    store.apply_finished([old])
    store.apply_jobs([active()])  # Also safe when queue start metadata is unavailable.
    assert not store.finished and store.history_revision == 1
    store.apply_finished([replace(old, rss=12345, state="FAILED")])
    store.apply_jobs([])
    store.apply_finished([old])
    assert not store.finished and store.departed_jobs and store.history_revision == 1
    store.apply_finished([done(start="2026-10-05T03:00:00", end="2026-10-05T04:00:00")])
    assert store.history_revision == 2


@pytest.mark.parametrize("state", ["RUNNING", "PENDING", "COMPLETING", "REQUEUED", "REQUEUE_HOLD", "REQUEUE_FED",
                                    "RESIZING", "SIGNALING", "STAGE_OUT", "SPECIAL_EXIT", "FUTURE_UNKNOWN", ""])
def test_nonterminal_accounting_never_confirms_a_departure(state):
    store = memory()
    store.apply_jobs([active()])
    store.apply_jobs([])
    store.apply_finished([done(state=state)])
    assert not store.finished and store.history_revision == 0 and "42" in store.departed_jobs


def test_array_group_splitting_is_not_an_individual_completion():
    store = memory()
    store.apply_jobs([Job("42_[0-7]", "array", "gpu", "PENDING")])
    store.apply_jobs([Job("42_[1-7]", "array", "gpu", "PENDING"), active("42_0")])
    assert not store.departed_jobs and not store.job_transitions
    store.apply_finished([done("42_7")])
    assert store.history_revision == 0
    store.apply_jobs([Job("42_[1-7]", "array", "gpu", "PENDING")])
    store.apply_finished([done("42_7")])
    assert "42_0" in store.departed_jobs and store.history_revision == 0


def test_confirmed_recent_job_survives_incomplete_snapshot_but_respects_window():
    from raven import clock
    store = memory()
    store.apply_jobs([active()])
    store.apply_jobs([])
    store.apply_finished([done(end="2000-01-01T00:00:00")])
    store.apply_finished([])
    assert [j.id for j in store.finished] == ["42"]
    assert store.history_revision == 1
    store.apply_finished([], history_days=1)
    assert clock.now() > 1_000_000_000 and not store.finished
    assert store.history_revision == 1


def test_transition_state_and_preserved_details_are_bounded():
    store = memory()
    for i in range(COMPLETION_KEEP + 20):
        store.apply_jobs([active(str(i))])
    store.apply_jobs([])
    assert len(store.departed_jobs) == COMPLETION_KEEP
    assert len(store.job_transitions) == TRANSITION_KEEP
    assert len(store.preserved_detail_ids()) == COMPLETION_KEEP
    sequence = [row["seq"] for row in store.job_transitions]
    assert sequence == sorted(set(sequence))
    snap = store.snapshot()
    snap["job_transitions"][-1]["state"] = "fabricated"
    assert store.job_transitions[-1]["state"] == "ACCOUNTING"


def test_details_for_departed_and_just_confirmed_jobs_survive_selection_change():
    slurm = SimpleNamespace(details=lambda jid: {"StdOut": f"/{jid}.log"})
    store = memory()
    sampler = Sampler(slurm, store, {}, [])
    try:
        store.apply_jobs([active("1"), active("2")])
        store.details["1"] = {"StdOut": "/original.log", "WorkDir": "/original"}
        store.apply_jobs([active("2")])
        sampler.select("2")
        sampler.src_details()
        assert store.details["1"]["StdOut"] == "/original.log"
        store.apply_finished([done("1", state="FAILED")])
        sampler.src_details()
        assert store.details["1"]["StdOut"] == "/original.log"
        assert store.details["1"]["JobState"] == "FAILED"
    finally:
        sampler.shutdown()


class Clock:
    def __init__(self):
        self.value = 1000.0

    def __call__(self):
        return self.value


class Scheduler:
    def __init__(self):
        self.queue = [active()]
        self.accounting = []
        self.calls = 0
        self.fail = False

    def jobs(self):
        return self.queue

    def finished(self, days):
        self.calls += 1
        if self.fail:
            raise CommandError("accounting temporarily unavailable")
        return self.accounting


@pytest.fixture
def session(monkeypatch):
    from raven import sampler as module
    timer = Clock()
    monkeypatch.setattr(module.time, "time", timer)
    slurm = Scheduler()
    store = memory()
    sampler = Sampler(slurm, store, {"jobs": 5, "finished": 60}, [], workers=2)
    for name, health in store.health.items():
        health.enabled = name in {"jobs", "finished"}
    sampler.round(wait=True)
    assert slurm.calls == 1
    try:
        yield timer, slurm, store, sampler
    finally:
        sampler.shutdown()


def test_departure_refreshes_accounting_without_restart_and_coalesces_queries(session):
    timer, slurm, store, sampler = session
    slurm.queue = []
    timer.value = 1004.9
    sampler.round(wait=True)
    assert slurm.calls == 1 and store.jobs and not store.departed_jobs
    timer.value = 1005
    sampler.round(wait=True)
    assert slurm.calls in (1, 2) and store.departed_jobs
    # Queue and accounting deadlines coincide. Depending on worker order, an
    # empty accounting snapshot can arrive in this round or the next frame.
    timer.value = 1005.01
    slurm.accounting = [done(state="FAILED")]
    sampler.round(wait=True)
    timer.value = 1010
    sampler.round(wait=True)
    assert slurm.calls in (2, 3) and store.finished[0].state == "FAILED"
    assert store.history_revision == 1 and not store.departed_jobs
    confirmed_calls = slurm.calls
    for timer.value in (1011, 1015, 1020):
        sampler.round(wait=True)
    assert slurm.calls == confirmed_calls  # Confirmation stops expedited polling.


def test_lag_retries_are_limited_then_normal_refresh_continues(session):
    timer, slurm, store, sampler = session
    timer.value = 1002
    slurm.queue = []
    sampler.round(wait=True)
    for timer.value in (1005, 1010, 1020, 1040, 1080):
        sampler.round(wait=True)
    assert slurm.calls == 1 + HISTORY_FAST_ATTEMPTS
    assert sampler._finished_retry_remaining == 0
    timer.value = 1090
    sampler.round(wait=True)
    assert slurm.calls == 1 + HISTORY_FAST_ATTEMPTS
    assert store.departed_jobs and not store.finished
    timer.value = 1140
    slurm.accounting = [done()]
    sampler.round(wait=True)
    assert store.history_revision == 1 and not store.departed_jobs


def test_new_departures_do_not_clear_accounting_failure_backoff(session):
    timer, slurm, store, sampler = session
    slurm.queue = [active("2")]
    timer.value = 1002
    sampler.round(wait=True)
    slurm.fail = True
    timer.value = 1005
    sampler.round(wait=True)
    health = store.health["finished"]
    assert health.errors == 1 and health.backoff == 60 and slurm.calls == 2
    timer.value = 1007
    slurm.queue = []
    sampler.round(wait=True)
    assert not sampler.due("finished", 1010)
    assert health.backoff == 60 and slurm.calls == 2
    timer.value = 1125
    slurm.fail = False
    slurm.accounting = [done(), done("2", state="CANCELLED")]
    sampler.round(wait=True)
    assert slurm.calls == 3 and store.history_revision == 2 and health.backoff == 0


def test_departure_during_inflight_accounting_is_not_lost(session):
    timer, slurm, store, sampler = session
    timer.value = 1005
    slurm.queue = [active("2")]
    sampler.round(wait=True)
    entered, release = threading.Event(), threading.Event()
    original = slurm.finished

    def slow(days):
        entered.set()
        assert release.wait(2)
        return [done()]  # Snapshot predates job 2's departure.

    slurm.finished = slow
    timer.value = 1010
    futures = sampler.round()
    try:
        assert entered.wait(1)
        timer.value = 1012
        slurm.queue = []
        sampler.src_jobs()
        release.set()
        for future in futures:
            future.result(timeout=2)
        assert store.history_revision == 1 and "2" in store.departed_jobs
        assert sampler._finished_requested > sampler._finished_handled
        assert not sampler.due("finished", 1016.9)
        assert sampler.due("finished", 1017)
        slurm.finished = original
        slurm.accounting = [done(), done("2")]
        timer.value = 1017
        sampler.round(wait=True)
        assert store.history_revision == 2 and not store.departed_jobs
    finally:
        release.set()


def test_late_historical_details_do_not_overwrite_requeued_job_paths():
    entered, release = threading.Event(), threading.Event()

    def control(jid):
        raise CommandError("old job no longer in scheduler")

    def historical(jid):
        entered.set()
        assert release.wait(2)
        return {"StdOut": "/previous-attempt.log", "JobState": "FAILED"}

    store = memory()
    store.apply_jobs([active()])
    store.apply_jobs([])
    store.apply_finished([done(state="FAILED")])
    sampler = Sampler(SimpleNamespace(details=control, historical_details=historical), store, {}, [])
    sampler.select("42")
    future = sampler.pool.submit(sampler.src_details)
    try:
        assert entered.wait(1)
        store.apply_jobs([active()])
        store.details["42"] = {"StdOut": "/active-attempt.log", "JobState": "RUNNING"}
        release.set()
        future.result(timeout=2)
        assert store.details["42"]["StdOut"] == "/active-attempt.log"
        assert "42" not in sampler._historical_details
    finally:
        release.set()
        sampler.shutdown()


def test_live_details_finishing_after_accounting_keep_final_state_and_real_paths():
    entered, release = threading.Event(), threading.Event()

    def details(jid):
        entered.set()
        assert release.wait(2)
        return {"StdOut": "/job.log", "JobState": "RUNNING"}

    store = memory()
    store.apply_jobs([active()])
    sampler = Sampler(SimpleNamespace(details=details), store, {}, [])
    sampler.select("42")
    future = sampler.pool.submit(sampler.src_details)
    try:
        assert entered.wait(1)
        store.apply_jobs([])
        store.apply_finished([done(state="TIMEOUT")])
        release.set()
        future.result(timeout=2)
        assert store.details["42"]["JobState"] == "TIMEOUT"
        assert store.details["42"]["StdOut"] == "/job.log"
    finally:
        release.set()
        sampler.shutdown()


def test_disabled_or_inflight_accounting_stays_unscheduled(session):
    timer, slurm, store, sampler = session
    sampler.request_history()
    timer.value = 1005
    health = store.health["finished"]
    health.enabled = False
    assert not sampler.due("finished", timer.value)
    health.enabled = True
    health.inflight = True
    assert not sampler.due("finished", timer.value)
    health.inflight = False
    assert sampler.due("finished", timer.value)


@pytest.mark.parametrize("confirmed", [False, True])
def test_return_invalidates_old_attempt_metadata_but_departure_preserves_it(confirmed):
    store = memory()
    store.apply_jobs([active()])
    old = {"StdOut": "/old/out.log", "StdErr": "/old/err.log", "WorkDir": "/old", "JobState": "RUNNING"}
    store.details["42"] = old
    store.fin_steps["42"] = ["old step"]
    store.apply_jobs([])
    assert store.details["42"] == old
    if confirmed:
        store.apply_finished([done(state="FAILED")])
        assert store.details["42"]["StdOut"] == "/old/out.log"
    store.apply_jobs([active()])
    assert "42" not in store.details and "42" not in store.fin_steps
    assert store.job_attempt("42") > 0


def test_return_expedites_selected_details_without_resetting_failure_backoff(session):
    timer, slurm, store, sampler = session
    slurm.details = lambda jid: {"StdOut": "/fresh/out.log", "WorkDir": "/fresh", "JobState": "RUNNING"}
    health = store.health["details"]
    health.enabled = True
    sampler.select("42")
    sampler.last_run["details"] = 1000
    store.details["42"] = {"StdOut": "/old/out.log", "WorkDir": "/old", "JobState": "FAILED"}
    timer.value = 1001
    slurm.queue = []
    sampler.src_jobs()
    slurm.queue = [active()]
    timer.value = 1002
    sampler.src_jobs()
    assert "42" not in store.details
    assert sampler.last_run["details"] == 1000  # No reset bypasses error backoff.
    assert sampler.due("details", 1002)
    health.inflight = True
    assert not sampler.due("details", 1002)
    health.inflight = False
    health.backoff = 30
    assert not sampler.due("details", 1002)
    health.backoff = 0
    sampler.round(wait=True)
    assert store.details["42"]["StdOut"] == "/fresh/out.log"
    assert sampler._details_refresh is None
    assert not sampler.due("details", 1003)


def test_live_detail_reply_from_previous_attempt_cannot_repopulate_returned_paths(session):
    timer, slurm, store, sampler = session
    entered, release = threading.Event(), threading.Event()
    requests = []

    def details(jid):
        requests.append(jid)
        if len(requests) == 1:
            entered.set()
            assert release.wait(2)
            return {"StdOut": "/old/out.log", "WorkDir": "/old", "JobState": "RUNNING"}
        return {"StdOut": "/fresh/out.log", "WorkDir": "/fresh", "JobState": "RUNNING"}

    slurm.details = details
    sampler.select("42")
    sampler.last_run["details"] = 1000
    store.health["details"].enabled = True
    store.health["details"].inflight = True
    future = sampler.pool.submit(sampler.run_source, "details")
    try:
        assert entered.wait(1)
        timer.value = 1001
        slurm.queue = []
        sampler.src_jobs()
        timer.value = 1002
        slurm.queue = [active()]
        sampler.src_jobs()
        release.set()
        future.result(timeout=2)
        assert "42" not in store.details
        assert sampler.due("details", 1002)
        sampler.round(wait=True)
        assert requests == ["42", "42"]
        assert store.details["42"]["StdOut"] == "/fresh/out.log"
    finally:
        release.set()
