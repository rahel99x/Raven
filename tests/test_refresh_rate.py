"""Polling ranges use complete tracks while retaining admission and error bounds."""
from __future__ import annotations

import concurrent.futures
import threading
from types import SimpleNamespace

import pytest

from raven import refresh_rate as rate
from raven.config import Config
from raven.model import Store
from raven.sampler import Sampler
from raven.slurm import CommandError


def sampler(intervals=None, *, workers=1):
    result = Sampler(SimpleNamespace(), Store(persist=False), intervals or {}, [], workers=workers)
    for health in result.store.health.values():
        health.enabled = False
    return result


def app(cfg=None, **targets):
    result = SimpleNamespace(cfg=cfg or Config(), sampler=None, research=None, logs=None,
                             messages=[], failures=[], **{key: value for key, value in targets.items() if key not in {"sampler", "research", "logs"}})
    for name in ("sampler", "research", "logs"):
        if name in targets:
            setattr(result, name, targets[name])
    result.say = result.messages.append
    result.fail = result.failures.append
    return result


@pytest.mark.parametrize("value", [1, 2, 25, 50, 1.0, 50.0])
def test_rate_accepts_finite_whole_values(value):
    assert rate.validate_multiplier(value) == int(value)


@pytest.mark.parametrize("maximum", [50, 100])
def test_poll_slider_uses_entire_domain_without_clamped_dead_regions(maximum):
    intervals = [rate.poll_interval(position, maximum) for position in range(1, maximum + 1)]
    assert intervals[0] == 5 and intervals[-1] == .5
    assert all(.5 <= value <= 5 for value in intervals)
    assert all(left > right for left, right in zip(intervals, intervals[1:]))
    assert all(rate.poll_position(value, maximum) == position
               for position, value in enumerate(intervals, 1))


@pytest.mark.parametrize("seconds,expected", [(100, 1), (5, 1), (2, 20), (.5, 50), (.001, 50)])
def test_interval_input_maps_to_nearest_bounded_polling_position(seconds, expected):
    assert rate.poll_position(seconds) == expected


@pytest.mark.parametrize("bad", [None, True, False, "5", [], 0, -1, float("nan"),
                                float("inf"), float("-inf"), 10**1000])
def test_bad_interval_input_cannot_become_polling_position(bad):
    with pytest.raises(ValueError, match="finite and positive"):
        rate.poll_position(bad)


@pytest.mark.parametrize("bad", [None, True, "2", 1, 0, 2.5])
def test_polling_helpers_reject_bad_slider_maximum(bad):
    for helper in (rate.poll_interval, rate.poll_position):
        with pytest.raises(ValueError, match="slider maximum"):
            helper(2, bad)


@pytest.mark.parametrize("source", ["jobs", "live", "gpu", "trace"])
def test_native_fast_polling_domain_and_explicit_slow_intervals(source):
    for base in (.01, .5, 2, 5):
        assert rate.source_interval(base, 1, source=source) == 5
        assert rate.source_interval(base, 50, source=source) == .5
        assert rate.source_interval(base, 25, source=source) == pytest.approx(1.618728771408822)
    for base in (30, 60, 600):
        assert rate.source_interval(base, 1, source=source) == base
        assert rate.source_interval(base, 50, source=source) == base / 10
    assert rate.source_interval(0, 50, source=source) == 0


@pytest.mark.parametrize("value", [None, True, False, "1", "50", [], {}, -1, 0, 51, 1.5,
                                   float("nan"), float("inf"), float("-inf"), 10**1000])
def test_invalid_rate_rejected_before_pool_creation(value, monkeypatch):
    monkeypatch.setattr(concurrent.futures, "ThreadPoolExecutor", lambda **kwargs: pytest.fail("invalid rate created a worker"))
    with pytest.raises(ValueError, match="polling_multiplier"):
        Sampler(SimpleNamespace(), Store(persist=False), {}, [], polling_multiplier=value)


@pytest.mark.parametrize("base", [0, .01, .25, .5, 2, 5, 30, 60, 600])
@pytest.mark.parametrize("source", ["weather", "budget", "plugin.source"])
def test_default_preserves_non_native_configured_intervals(base, source):
    assert rate.source_interval(base, 1, source=source) == base
    assert rate.file_interval(base, 1, remote=False) == base
    assert rate.file_interval(base, 1, remote=True) == base


@pytest.mark.parametrize("source,base,expected", [("jobs", 2, .5), ("plugin.source", 100, 10),
                                                 ("gpu", 5, .5), ("gpu", 600, 60),
                                                 ("weather", 120, 30), ("budget", 600, 60),
                                                 ("gpu", 2, .5), ("jobs", .1, .5), ("jobs", 0, 0)])
def test_fastest_polling_respects_native_range_and_other_source_cost_floors(source, base, expected):
    assert rate.source_interval(base, 50, source=source) == expected


@pytest.mark.parametrize("base,remote,expected", [(5, False, .5), (5, True, 1.5),
                                               (.5, False, .5), (.5, True, .5),
                                               (100, False, 10), (100, True, 10)])
def test_file_polling_floors(base, remote, expected):
    assert rate.file_interval(base, 50, remote=remote) == expected


@pytest.mark.parametrize("value", [None, True, "1", -1, float("nan"), float("inf"), 10**1000])
def test_invalid_base_interval_is_reported(value):
    with pytest.raises(ValueError, match="polling interval"):
        rate.source_interval(value, 2)


def test_changing_rate_recomputes_dynamic_plugin_intervals_without_compounding():
    worker = sampler()
    try:
        worker.add_source("sensor", 100, lambda slurm, store: None)
        worker.set_polling_multiplier(5)
        assert worker.effective_interval("sensor") == pytest.approx(82.86427728546845)
        assert worker.intervals["sensor"] == 100
        worker.set_polling_multiplier(10)
        assert worker.effective_interval("sensor") == pytest.approx(65.51285568595508)
        worker.intervals["sensor"] = 40
        assert worker.effective_interval("sensor") == pytest.approx(26.205142274382033)
        worker.add_source("late", 200, lambda slurm, store: None)
        assert worker.effective_interval("late") == pytest.approx(131.02571137191016)
        worker.set_polling_multiplier(1)
        assert worker.effective_interval("sensor") == 40
        assert worker.effective_interval("late") == 200
    finally:
        worker.shutdown()


def test_live_rate_changes_preserve_last_run_and_failure_backoff():
    worker = sampler()
    def unavailable(slurm, store):
        raise CommandError("source unavailable")
    worker.add_source("sensor", 10, unavailable)
    try:
        worker.round(now=100, wait=True)
        health = worker.health("sensor")
        assert health.backoff == 10 and health.errors == 1
        worker.set_polling_multiplier(50)
        assert worker.last_run["sensor"] == 100 and health.backoff == 10
        assert not worker.due("sensor", 110.99)
        assert worker.due("sensor", 111)
        worker.round(now=111, wait=True)
        assert health.backoff == 20 and health.errors == 2
        worker.set_polling_multiplier(1)
        assert worker.last_run["sensor"] == 111 and health.backoff == 20
        assert not worker.due("sensor", 140.99)
        assert worker.due("sensor", 141)
    finally:
        worker.shutdown()


def test_rate_wakes_scheduler_without_force_refresh_or_repeated_wakeups():
    worker = sampler({"jobs": 2})
    try:
        worker.last_run["jobs"] = 100
        worker.health("jobs").backoff = 64
        assert not worker.kick.is_set()
        worker.set_polling_multiplier(5)
        assert worker.kick.is_set()
        worker.kick.clear()
        worker.set_polling_multiplier(5)
        assert not worker.kick.is_set()
        assert worker.last_run["jobs"] == 100 and worker.health("jobs").backoff == 64
    finally:
        worker.shutdown()


def test_running_source_never_overlaps_after_rate_change():
    entered, release = threading.Event(), threading.Event()
    calls = []
    def blocked(slurm, store):
        calls.append(1)
        entered.set()
        assert release.wait(2)
    worker = sampler(workers=1)
    worker.add_source("sensor", 100, blocked)
    try:
        futures = worker.round(now=100)
        assert entered.wait(1)
        worker.set_polling_multiplier(50)
        for tick in range(1000, 1100):
            assert not worker.round(now=tick)
        assert calls == [1] and worker.health("sensor").inflight
        release.set()
        concurrent.futures.wait(futures, timeout=2)
        assert all(f.done() for f in futures)
        worker.round(now=110, wait=True)
        assert calls == [1, 1]
    finally:
        release.set()
        worker.shutdown()


def test_disabled_sources_stay_disabled_and_initial_dependencies_stay_ordered():
    worker = sampler({"jobs": 2, "live": 10})
    try:
        worker.set_polling_multiplier(50)
        assert not worker.due("jobs", 100)
        worker.health("jobs").enabled = worker.health("live").enabled = True
        assert worker.due("jobs", 100)
        assert not worker.due("live", 100)
        worker.health("jobs").calls = 1
        assert worker.due("live", 100)
        worker.health("jobs").inflight = True
        assert not worker.due("live", 100)
    finally:
        worker.shutdown()


def test_history_departure_retry_limit_stays_intact_when_rate_changes():
    worker = sampler({"finished": 600})
    try:
        worker.health("finished").enabled = True
        worker.health("finished").calls = 1
        worker.last_run["finished"] = 100
        worker.request_history()
        worker.set_polling_multiplier(50)
        assert not worker.due("finished", 104.9)
        assert worker.due("finished", 105)
        worker.health("finished").backoff = 100
        assert not worker.due("finished", 105)
        assert worker._finished_retry_remaining == 5
    finally:
        worker.shutdown()


def test_preference_restores_to_all_available_readers_and_future_reader_config():
    worker = sampler({"jobs": 2})
    hub = SimpleNamespace(polling_multiplier=1)
    logs = SimpleNamespace(polling_multiplier=1, catalog=SimpleNamespace(polling_multiplier=1))
    model = app(sampler=worker, research=hub, logs=logs)
    try:
        rate.initialize(model)
        rate.restore(model, {"multiplier": 12})
        assert rate.multiplier(model) == 12
        assert model.cfg.get("polling_multiplier") == 12
        assert worker.polling_multiplier == hub.polling_multiplier == logs.polling_multiplier == 12
        assert logs.catalog.polling_multiplier == 12
        assert rate.save(model) == {"multiplier": 12}
        assert rate.cadence(model, "jobs") == pytest.approx(2.9818116582973215)
        assert "2.98s" in rate.cadence_summary(model) and "12x" not in rate.cadence_summary(model)
        saved = rate.save(model)
        restarted = app()
        rate.restore(restarted, saved)
        assert restarted.cfg.get("polling_multiplier") == 12
        assert rate.multiplier(restarted) == 12
        rate.execute(model, "rate", ["reset"])
        assert rate.multiplier(model) == worker.polling_multiplier == 1
        assert worker.effective_interval("jobs") == 5
    finally:
        worker.shutdown()


@pytest.mark.parametrize("saved", [None, [], {}, {"multiplier": True}, {"multiplier": "50"},
                                   {"multiplier": float("nan")}, {"multiplier": 0}, {"multiplier": 51}])
def test_malformed_persisted_preference_cannot_override_configured_rate(saved):
    model = app(Config({"polling_multiplier": 4}))
    rate.restore(model, saved)
    assert rate.multiplier(model) == 4


@pytest.mark.parametrize("command_args", [["rate", "0"], ["rate", "51"], ["rate", "1.5"],
                                         ["rate", "nan"], ["rate", "50", "extra"], ["rate", "-1"]])
def test_invalid_commands_leave_rate_unchanged(command_args):
    model = app()
    assert rate.run_command(model, command_args)
    assert model.failures and not model.messages
    assert rate.multiplier(model) == 1


def test_command_query_reset_and_clamped_adjustments_with_no_workers():
    model = app()
    assert not rate.run_command(model, ["other"])
    assert not rate.run_command(model, [])
    assert rate.run_command(model, ["rate", "50"])
    assert rate.multiplier(model) == 50
    assert rate.adjust(model, 1) == 50
    assert rate.adjust(model, -100) == 1
    assert rate.adjust(model, 10**1000) == 50
    assert rate.run_command(model, ["rate", "reset"])
    assert rate.run_command(model, ["rate"])
    assert "5s" in model.messages[-1] and "1x" not in model.messages[-1]
    assert rate.multiplier(SimpleNamespace()) == 1
    assert not rate.handle_key(model, "x")
    assert not rate.handle_mouse(model, 0, 0)
    assert rate.overlay(None, {}, model, 80, 24) is None


def test_native_ui_cadence_preserves_explicit_slow_intervals_without_sampler():
    model = app()
    rate.set_multiplier(model, 5)
    assert rate.cadence(model, "jobs") == pytest.approx(4.143213864273422)
    model.cfg.set("intervals.jobs", 100)
    assert rate.cadence(model, "jobs") == pytest.approx(82.86427728546845)
    rate.set_multiplier(model, 1)
    assert rate.cadence(model, "jobs") == 100


def test_bad_configured_rate_does_not_initialize_application_state():
    model = app(Config({"polling_multiplier": False}))
    with pytest.raises(ValueError, match="polling_multiplier"):
        rate.initialize(model)
    assert not hasattr(model, "refresh_rate_state")


def test_hub_polling_rate_reads_new_files_without_resetting_base_cache_or_worker(monkeypatch):
    from raven.research import ResearchHub
    timer = SimpleNamespace(value=100.0)
    monkeypatch.setattr("raven.research.time.monotonic", lambda: timer.value)
    hub = ResearchHub(Config({"research": {"interval": 5}}))
    reads = []
    hub._read = lambda context: reads.append(context["jid"]) or {"status": "ready", "summary": "sample"}
    context = {"generation": 0, "view": "experiment", "jid": "7"}
    try:
        hub.request(context, wait=True)
        initial_pool, initial_generation = hub.pool, hub.generation
        hub.set_polling_multiplier(50)
        assert hub.interval == 5 and hub.refresh_interval() == .5
        assert hub.pool is initial_pool and hub.generation == initial_generation
        timer.value = 100.49
        hub.request(context, wait=True)
        assert reads == ["7"]
        timer.value = 100.5
        hub.request(context, wait=True)
        assert reads == ["7", "7"]
        hub.set_polling_multiplier(1)
        timer.value = 105.49
        hub.request(context, wait=True)
        assert reads == ["7", "7"]
        timer.value = 105.5
        hub.request(context, wait=True)
        assert reads == ["7", "7", "7"]
    finally:
        hub.close()


def test_config_rate_constructs_hub_and_remote_file_limit_remains_bounded():
    from raven.research import ResearchHub
    hub = ResearchHub(Config({"polling_multiplier": 50}), files=SimpleNamespace(remote=True))
    try:
        assert hub.polling_multiplier == 50
        assert hub.interval == 5 and hub.refresh_interval() == 1.5
        hub.interval = 100
        assert hub.refresh_interval() == 10
    finally:
        hub.close()


def test_real_app_command_restores_rate_from_scoped_ui_preferences(tmp_path):
    from raven.controller import App
    state_root = str(tmp_path / "desktop-state")
    store = Store(state_dir=state_root)
    worker = Sampler(SimpleNamespace(), store, {"jobs": 2}, [])
    try:
        model = App(store, worker, None, Config(), "alice")
        assert "rate" in model.commands()
        model.run_command("rate 20")
        assert model.command_ok and "2.05s" in model.message and "20x" not in model.message
        assert worker.effective_interval("jobs") == pytest.approx(2.047457531190213)
        model.save()
        restored = App(Store(state_dir=state_root), None, None, Config(), "alice")
        assert rate.multiplier(restored) == restored.logs.polling_multiplier == 20
        restored.run_command("rate reset")
        restored.save()
        restarted = App(Store(state_dir=state_root), None, None, Config(), "alice")
        assert rate.multiplier(restarted) == 1
    finally:
        worker.shutdown()


@pytest.mark.parametrize("value", [False, "50", 51, float("nan")])
def test_cli_bad_config_rate_rejected_before_backend_or_state_creation(value, monkeypatch):
    from raven import cli
    monkeypatch.setattr(cli, "make_backend", lambda *args: pytest.fail("bad rate created a backend"))
    with pytest.raises(ValueError, match="polling_multiplier"):
        cli.build(cli.parse(["--fake", "--no-plugins", "--no-state"]), Config({"polling_multiplier": value}))


def test_cli_explicit_rate_overrides_saved_preference_for_all_readers(monkeypatch):
    from raven import cli
    monkeypatch.setattr(Store, "load_ui", lambda self: {"workbench": {"refresh_rate": {"multiplier": 5}}})
    session = cli.build(cli.parse(["--fake", "--no-plugins", "--no-state", "--rate", "50"]), Config())
    try:
        assert rate.multiplier(session.app) == 50
        assert session.sampler.polling_multiplier == session.app.research.polling_multiplier == 50
        assert session.app.logs.polling_multiplier == 50
        assert rate.save(session.app) == {"multiplier": 50}
        assert session.sampler.intervals["jobs"] == 2
    finally:
        session.close()


def test_repeated_app_frames_and_arrow_selection_preserve_accounting_failure_deadline(monkeypatch):
    from raven.controller import App
    from raven.model import Finished
    timer = SimpleNamespace(value=1000.0)
    monkeypatch.setattr("raven.sampler.time.time", lambda: timer.value)
    calls = []
    def unavailable(jid):
        calls.append(jid)
        raise CommandError("accounting is unavailable")
    store = Store(persist=False)
    store.finished = [Finished("7", "failed", "FAILED"), Finished("8", "failed too", "FAILED")]
    worker = Sampler(SimpleNamespace(fin_steps=unavailable), store, {"fin_details": 5}, [])
    for health in store.health.values():
        health.enabled = health.name == "fin_details"
    model = App(store, worker, None, Config(), "alice")
    model.selected_id = "7"
    try:
        model.tick()
        worker.round(now=timer.value, wait=True)
        health = worker.health("fin_details")
        assert calls == ["7"] and health.backoff == 5
        deadline = worker.last_run["fin_details"]
        for timer.value in (1000.2, 1001, 1002, 1003, 1004, 1005):
            model.tick()
            worker.round(now=timer.value, wait=True)
            assert worker.last_run["fin_details"] == deadline
        assert calls == ["7"]
        # Selecting another recent job, temporarily opening another tab, and
        # moving back cannot bypass a failing accounting service's deadline.
        model.selected_id = "8"
        model.tick()
        model.tab = "cluster"
        model.tick()
        model.tab = "jobs"
        model.tick()
        rate.set_multiplier(model, 50)
        timer.value = 1005.49
        worker.round(now=timer.value, wait=True)
        assert calls == ["7"] and worker.last_run["fin_details"] == deadline
        timer.value = 1005.5
        model.tick()
        worker.round(now=timer.value, wait=True)
        assert calls == ["7", "8"] and health.backoff == 10
    finally:
        worker.shutdown()


def test_arrow_selection_preserves_failing_job_details_deadline():
    worker = sampler({"details": 20})
    health = worker.health("details")
    health.enabled, health.calls, health.backoff = True, 1, 40
    worker.last_run["details"] = 1000
    try:
        for jid in ("7", "8", None, "7"):
            worker.select(jid)
            assert worker.last_run["details"] == 1000
        worker.set_polling_multiplier(50)
        assert not worker.due("details", 1041.99)
        assert worker.due("details", 1042)
        health.backoff = 0
        worker.select("8")
        assert worker.last_run["details"] == 0
        assert worker.due("details", 1000)
    finally:
        worker.shutdown()
