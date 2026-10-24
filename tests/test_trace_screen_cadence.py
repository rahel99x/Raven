"""Virtual-clock frame scheduling for the existing Trace background owner."""

from __future__ import annotations

import math
from concurrent.futures import Future
from threading import Event
from types import SimpleNamespace

import pytest

from raven import clock, job_panels, screen, trace_ui
from raven import layout as L
from raven.config import Config
from raven.controller import App
from raven.model import Finished, Job, Store
from raven.raven_limits import LightLimits
from raven.research import ResearchHub
from raven.slurm import FakeBackend, Slurm
from raven.ui_wakeup import UIWakeup
from raven.views import Views
from raven.worker_scheduler import WorkerScheduler


@pytest.fixture
def scene(monkeypatch):
    now = [1000.125]
    monkeypatch.setattr(screen.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(clock, "now", lambda: now[0])
    cfg = Config(
        {
            "animations": False,
            "startup_animation": False,
            "log_lines": 0,
            "raven": {"managed": True, "mode": "light", "site": {"kind": "slurm"}},
        }
    )
    store = Store(persist=False, raven_limits=LightLimits({}))
    store.apply_jobs([Job("7", "scientific job", "local", "RUNNING")])
    app = App(store, None, None, cfg, "tester", interactive=True)
    app.actions = SimpleNamespace(slurm=Slurm(FakeBackend(), "tester"))
    views = Views(L.Glyphs(True), cfg)
    app.files = app.logs.files = views.files
    app.views_ref = views
    scheduler = WorkerScheduler(workers=1, lanes=("research",))
    app.research = ResearchHub(cfg, files=views.files, worker_scheduler=scheduler)
    ticks = []
    application_tick = app.tick
    monkeypatch.setattr(app, "tick", lambda: ticks.append(now[0]))

    def prepare(tab="jobs", delay=0.05):
        app.tab, app.selected_id = tab, "7"
        if tab == "history":
            store.jobs = []
            store.finished = [Finished("7", "scientific job", "COMPLETED")]
        if tab in {"jobs", "history"}:
            assert job_panels.run_command(app, ["jobpanel", "trace"])
        state = trace_ui.initialize(app)
        state["context"] = trace_ui._context(app)
        assert state["context"] is not None
        state["next_read"] = now[0] + delay
        cache = screen._FrameCache()
        cache.rebuild(app, views, store, None, 80, 36, maintained=True)
        assert bool(cache.document_token) == (tab != "traces")
        return SimpleNamespace(
            app=app,
            views=views,
            store=store,
            state=state,
            cache=cache,
            hub=app.research,
            now=now,
            ticks=ticks,
            scheduler=scheduler,
            application_tick=application_tick,
        )

    yield prepare
    app.research.close()
    scheduler.shutdown(wait=True, cancel_futures=True)


@pytest.mark.parametrize("tab", ["jobs", "history", "traces"])
@pytest.mark.parametrize("delay", [0.02, 0.05, 0.5])
def test_trace_deadline_requests_maintenance_without_composing_unchanged_rows(
    scene, monkeypatch, tab, delay
):
    s = scene(tab, delay)
    standard_wait = s.cache.next_maintenance - s.now[0]
    assert s.cache.wait_ms() == round(min(delay, standard_wait) * 1000)
    rows, hits = s.cache.rows, s.cache.hits

    def forbidden(*args, **kwargs):
        pytest.fail("Trace timer repainted an unchanged document")

    monkeypatch.setattr(s.views, "compose", forbidden)
    monkeypatch.setattr(s.store, "render_snapshot", forbidden)

    def tick():
        s.ticks.append(s.now[0])
        s.state["next_read"] = s.now[0] + 0.5

    monkeypatch.setattr(s.app, "tick", tick)
    s.now[0] += delay
    assert s.cache.maintain(s.app, 80, 36)
    assert s.ticks == [s.now[0]]
    assert not s.cache.dirty
    assert s.cache.rows is rows and s.cache.hits is hits
    assert s.cache.next_trace == s.state["next_read"]
    # An ordinary global-page frame may already be due after its 200 ms
    # fallback; the 20/50 ms trace deadline itself does not request painting.
    if delay < standard_wait:
        assert not s.cache.due(s.app, 80, 36)


@pytest.mark.parametrize(
    "condition",
    [
        "paused",
        "hidden",
        "modal",
        "context",
        "stale-context",
        "callback",
        "closed",
        "pending",
        "running",
        "missing-hub",
    ],
)
def test_unavailable_reader_never_introduces_a_zero_delay_wait(scene, condition):
    s = scene()
    s.state["next_read"] = 0
    future = Future()
    if condition == "paused":
        s.state["paused"] = True
    elif condition == "hidden":
        s.app.tab = "nodes"
    elif condition == "modal":
        s.app.mode = "details"
    elif condition == "context":
        s.state["context"] = None
    elif condition == "stale-context":
        s.state["context"] = dict(s.state["context"], key=("old-job-attempt",))
    elif condition == "callback":
        s.state["callback"] = lambda value: None
    elif condition == "closed":
        s.hub.closed = True
    elif condition == "pending":
        future.set_result(None)
        s.hub.pending = (future, lambda value: None)
    elif condition == "running":
        s.hub.future = future
    else:
        s.app.research = None
    try:
        s.cache.maintain(s.app, 80, 36)
        assert math.isinf(s.cache.next_trace)
        assert s.cache.wait_ms() > 1
    finally:
        s.app.research = s.hub
        s.hub.closed, s.hub.pending, s.hub.future = False, None, None


def test_declined_submission_retries_on_existing_fallback_without_busy_spinning(scene):
    s = scene()
    s.state["next_read"] = 0
    assert s.cache.maintain(s.app, 80, 36)
    assert s.ticks == [s.now[0]]
    assert s.cache.wait_ms() == round(screen.FALLBACK_MAINTENANCE_SECONDS * 1000)
    for _ in range(20):
        s.now[0] += 0.001
        assert not s.cache.maintain(s.app, 80, 36)
    assert len(s.ticks) == 1
    assert s.cache.wait_ms() > 1


def test_shared_worker_completion_wakes_then_publishes_next_trace_deadline(
    scene, monkeypatch
):
    s = scene()
    wake = UIWakeup()
    release, entered = Event(), Event()
    s.scheduler.ui_wakeup = wake
    notified = Event()

    def read():
        entered.set()
        assert release.wait(5)
        return "verified next page"

    def completed(value):
        assert value == "verified next page"
        s.state["callback"] = None
        s.state["next_read"] = s.now[0] + 0.05

    future = s.hub.pool.submit(read)
    future.add_done_callback(lambda value: notified.set())
    s.hub.future = future
    s.hub.pending = (future, completed)
    s.state["callback"] = completed
    try:
        assert entered.wait(1)
        s.cache.maintain(s.app, 80, 36)
        assert math.isinf(s.cache.next_trace)
        assert not wake.pending
        release.set()
        assert future.result(timeout=5) == "verified next page"
        assert notified.wait(1)
        assert wake.pending  # Production WorkerLane already subscribes UIWakeup.
        assert wake.drain()
        monkeypatch.setattr(s.app, "tick", s.application_tick)
        # _wait_for_input turns the publication hint into this maintenance
        # request; pending callbacks use the normal conservative frame path.
        s.cache.next_maintenance = 0
        assert s.cache.due(s.app, 80, 36)
        s.cache.rebuild(s.app, s.views, s.store, None, 80, 36)
        assert s.hub.pending is None and s.state["callback"] is None
        assert s.cache.wait_ms() == 50
    finally:
        release.set()
        future.result(timeout=5)
        s.scheduler.ui_wakeup = None
        wake.close()


def test_worker_held_hub_lock_does_not_block_frame_readiness_probe(scene):
    s = scene()
    entered, release = Event(), Event()

    def holding_lock():
        with s.hub.lock:
            entered.set()
            assert release.wait(5)

    future = s.hub.pool.submit(holding_lock)
    try:
        assert entered.wait(1)
        assert math.isinf(screen._trace_read_deadline(s.app))
    finally:
        release.set()
        future.result(timeout=5)


def test_trace_deadline_only_inspects_published_metadata(scene, monkeypatch):
    s = scene()

    def forbidden(*args, **kwargs):
        pytest.fail("A frame deadline initiated source or scheduler I/O")

    for name in ("read", "stat", "snapshot_stat", "listdir"):
        monkeypatch.setattr(s.app.files, name, forbidden)
    monkeypatch.setattr(s.app.actions.slurm.b, "run", forbidden)
    assert screen._trace_read_deadline(s.app) == s.state["next_read"]


@pytest.mark.parametrize("clock_only", [False, True])
def test_composition_crossing_future_deadline_does_not_look_like_a_declined_read(
    scene, monkeypatch, clock_only
):
    s = scene(delay=0.05)
    deadline = s.state["next_read"]
    compose = s.views.compose

    def slow_compose(*args, **kwargs):
        result = compose(*args, **kwargs)
        s.now[0] += 0.06
        return result

    monkeypatch.setattr(s.views, "compose", slow_compose)
    if clock_only:
        s.cache.next_document = s.now[0]
        assert s.cache.maintain(s.app, 80, 36)
        assert not s.ticks
    s.cache.rebuild(s.app, s.views, s.store, None, 80, 36, maintained=clock_only)
    assert len(s.ticks) == (0 if clock_only else 1)
    assert s.cache.next_trace == deadline
    assert s.cache.wait_ms() == 1  # The first actual due attempt is still owed.


def test_maintenance_crossing_future_deadline_does_not_backoff_an_unattempted_read(
    scene, monkeypatch
):
    s = scene(delay=0.05)
    deadline = s.state["next_read"]

    def slow_tick():
        s.ticks.append(s.now[0])
        s.now[0] += 0.06

    monkeypatch.setattr(s.app, "tick", slow_tick)
    s.cache.next_maintenance = s.now[0]
    assert s.cache.maintain(s.app, 80, 36)
    assert len(s.ticks) == 1
    assert s.cache.next_trace == deadline
    assert s.cache.wait_ms() == 1


def test_worker_held_store_lock_does_not_block_frame_readiness_probe(scene):
    s = scene()
    entered, release = Event(), Event()

    def holding_lock():
        with s.store.lock:
            entered.set()
            assert release.wait(5)

    future = s.hub.pool.submit(holding_lock)
    try:
        assert entered.wait(1)
        assert math.isinf(screen._trace_read_deadline(s.app))
    finally:
        release.set()
        future.result(timeout=5)


def test_unknown_store_context_implementation_uses_ordinary_maintenance(scene):
    s = scene()

    def forbidden(*args, **kwargs):
        pytest.fail("Unknown store context callback ran inside readiness probe")

    s.app.store = SimpleNamespace(record_context=forbidden, job_attempt=forbidden)
    assert math.isinf(screen._trace_read_deadline(s.app))
