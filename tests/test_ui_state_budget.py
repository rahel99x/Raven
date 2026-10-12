"""Selection housekeeping stays independent of retained chart history size."""
from collections import deque
from dataclasses import fields
import math
import os
import time
from types import SimpleNamespace

import pytest

from raven import model, project_ui
from raven.config import Config
from raven.controller import App
from raven.model import Finished, Health, Job, Store


def app_for(store):
    app = App(store, None, None, Config({"log_lines": 0}), "reader", interactive=False)
    app.selected_id = "7"
    return app


def forbid_snapshot(store, monkeypatch):
    monkeypatch.setattr(store, "snapshot", lambda: pytest.fail("selection must not clone retained histories"))


@pytest.mark.parametrize("source", ["jobs", "finished", "departed_jobs", "group"])
def test_exact_context_resolves_each_source_without_snapshot(source, monkeypatch):
    store = Store(persist=False)
    job = Finished("7", "recent", "FAILED") if source == "finished" else Job("7", "worker", "cpu", "RUNNING")
    setattr(store, source, {job.id: job} if source == "departed_jobs" else [job])
    store.details[job.id] = {"WorkDir": "/one", "StdOut": "/one/output"}
    forbid_snapshot(store, monkeypatch)
    record, details = store.record_context("7", include_group=True)
    assert record is job
    assert details == store.details["7"] and details is not store.details["7"]
    details["WorkDir"] = "/changed"
    assert store.details["7"]["WorkDir"] == "/one"


def test_context_priority_matches_full_snapshot_lookup():
    store = Store(persist=False)
    queued = Job("7", "current attempt", "cpu", "RUNNING")
    ended = Finished("7", "previous attempt", "FAILED")
    departed = Job("7", "awaiting accounting", "cpu", "COMPLETING")
    grouped = Job("7", "account listing", "cpu", "PENDING")
    store.jobs, store.finished, store.departed_jobs, store.group = [queued], [ended], {"7": departed}, [grouped]
    assert store.record_context("7", include_group=True)[0] is queued
    store.jobs = []
    assert store.record_context("7", include_group=True)[0] is ended
    store.finished = []
    assert store.record_context("7", include_group=True)[0] is departed
    store.departed_jobs = {}
    assert store.record_context("7")[0] is None
    assert store.record_context("7", include_group=True)[0] is grouped


def test_context_observes_next_publication_and_never_chooses_another_job():
    store = Store(persist=False)
    store.jobs = [Job("8", "unrelated", "cpu", "RUNNING")]
    assert store.record_context("7") == (None, {})
    store.jobs.append(Job("7", "new", "cpu", "RUNNING"))
    store.details["7"] = {"WorkDir": "/new"}
    old_record, old_details = store.record_context("7")
    store.jobs = []
    store.departed_jobs["7"] = old_record
    store.details["7"] = {"WorkDir": "/revised"}
    assert store.record_context("7") == (old_record, {"WorkDir": "/revised"})
    assert old_details == {"WorkDir": "/new"}


@pytest.mark.parametrize("jid", [None, "", "unknown"])
def test_absent_context_does_not_fall_back(jid):
    store = Store(persist=False)
    store.jobs = [Job("7", "worker", "cpu", "RUNNING")]
    assert store.record_context(jid) == (None, {})


def test_context_uses_store_lock_for_record_and_details():
    store = Store(persist=False)
    entered = []

    class Lock:
        def __enter__(self):
            entered.append(True)

        def __exit__(self, *args):
            entered.append(False)

    class GuardedDetails(dict):
        def get(self, *args):
            assert entered == [True]
            return super().get(*args)

    store.lock = Lock()
    store.details = GuardedDetails({"7": {"WorkDir": "/safe"}})
    assert store.record_context("7") == (None, {"WorkDir": "/safe"})
    assert entered == [True, False]


def test_tick_and_exact_record_resolution_do_not_visit_chart_histories(monkeypatch):
    store = Store(persist=False)
    store.jobs = [Job(str(index), "worker", "cpu", "RUNNING") for index in range(10000)]
    store.finished = [Finished("f" + str(index), "worker", "FAILED") for index in range(10000)]
    app = app_for(store)
    app.selected_id = "9999"

    class History(dict):
        def items(self):
            pytest.fail("housekeeping must not copy every chart's samples")

    store.hist_cpu = History({str(index): deque([.5] * 600) for index in range(100)})
    store.hist_gpu = History({str(index): deque([.4] * 600) for index in range(100)})
    forbid_snapshot(store, monkeypatch)
    for _ in range(5):
        app.tick()
        assert app.job_record("9999") is store.jobs[-1]
    assert app.job_record("missing") is None


def test_project_tick_honors_explicit_snapshot_without_live_lookup(monkeypatch):
    store = Store(persist=False)
    app = app_for(store)
    frozen_job = Job("7", "frozen", "cpu", "RUNNING")
    frozen = {"jobs": [frozen_job], "finished": [], "details": {"7": {"WorkDir": "/frozen"}}}
    monkeypatch.setattr(store, "record_context", lambda *args, **kwargs: pytest.fail("explicit publication must be used"))
    monkeypatch.setattr(project_ui.projects, "job_project_roots", lambda workdir, registered: [] if workdir == "/frozen" else pytest.fail("wrong publication"))
    assert project_ui.tick(app, frozen) is False


def test_suppressed_project_tick_does_not_lookup_or_clone(monkeypatch):
    store = Store(persist=False)
    app = app_for(store)
    app.project_state["auto_suppressed"] = "7"
    forbid_snapshot(store, monkeypatch)
    monkeypatch.setattr(store, "record_context", lambda *args, **kwargs: pytest.fail("suppressed target needs no lookup"))
    assert project_ui.tick(app) is False


def test_changed_workdir_is_observed_before_polling_deadline(monkeypatch):
    store = Store(persist=False)
    store.jobs = [Job("7", "worker", "cpu", "RUNNING")]
    app = app_for(store)
    store.details["7"] = {"WorkDir": "/old"}
    monkeypatch.setattr(project_ui.projects, "job_project_roots", lambda workdir, registered: [workdir])
    app.project_state["busy"] = True
    assert project_ui.tick(app) is False
    generation = app.project_state["auto_generation"]
    store.details["7"] = {"WorkDir": "/new"}
    assert project_ui.tick(app) is False
    assert app.project_state["auto_roots"] == ("/new",)
    assert app.project_state["auto_generation"] == generation + 1


@pytest.mark.parametrize("source", ["jobs", "finished", "departed_jobs", "group"])
def test_log_path_lookup_uses_exact_context_without_whole_snapshot(source, monkeypatch):
    store = Store(persist=False)
    app = app_for(store)
    record = Finished("7", "recent", "FAILED") if source == "finished" else Job("7", "worker", "cpu", "RUNNING")
    setattr(store, source, {"7": record} if source == "departed_jobs" else [record])
    store.details["7"] = {"StdOut": "/job-7/output.log"}
    app.tab, app.log_job = "log", "7"
    calls = []

    def log_path(application, job, details):
        calls.append((job, details))
        return details["StdOut"], "stdout"

    app.views_ref = SimpleNamespace(log_path=log_path)
    forbid_snapshot(store, monkeypatch)
    assert app.resolve_log_path() == "/job-7/output.log"
    assert calls == [(record, {"StdOut": "/job-7/output.log"})]
    assert app.log_record is record


def test_log_path_retains_exact_old_record_without_falling_back(monkeypatch):
    store = Store(persist=False)
    app = app_for(store)
    app.tab, app.log_job = "log", "7"
    app.log_record = Finished("7", "old", "FAILED", workdir="/old")
    store.jobs = [Job("8", "unrelated", "cpu", "RUNNING")]
    app.views_ref = SimpleNamespace(log_path=lambda application, job, details: (job.workdir + "/stdout", "stdout"))
    forbid_snapshot(store, monkeypatch)
    assert app.resolve_log_path() == "/old/stdout"


def test_full_snapshots_still_isolate_histories_and_health(monkeypatch):
    store = Store(persist=False)
    store.hist_cpu["7"].extend([.2, .3])
    store.hist_gpu["7:n:0"].extend([.4, .5])
    source = Health("jobs", calls=3, error="old", latency_ms=12.5)
    store.health["jobs"] = source
    # Health consists of scalar fields; its dataclass clone does not need a
    # recursive asdict pass on each frame.
    assert all(isinstance(getattr(source, item.name), (str, int, float, bool)) for item in fields(source))
    monkeypatch.setattr(model, "asdict", lambda _: pytest.fail("Health snapshot must not recursively serialize scalars"))
    first = store.snapshot()
    first["hist_cpu"]["7"].append(.9)
    first["hist_gpu"]["7:n:0"].append(.8)
    first["health"]["jobs"].error = "changed copy"
    assert list(store.hist_cpu["7"]) == [.2, .3]
    assert list(store.hist_gpu["7:n:0"]) == [.4, .5]
    assert source.error == "old"
    source.calls = 4
    store.hist_cpu["7"].append(.6)
    second = store.snapshot()
    assert second["health"]["jobs"].calls == 4
    assert second["hist_cpu"]["7"] == [.2, .3, .6]


@pytest.mark.parametrize("value", [None, "", "UNLIMITED", "N/A", "INVALID", "Unknown", "-", "NOT_SET",
    "PARTITION_TIME_LIMIT", "1-02:03:04", "00:00", "1:02.5", "0:00:00.25", "123.5", "bad-01:00:00",
    "-3", "4:05:06:07", "nan", "inf", "-inf", "9" * 300, False, 0, []])
def test_duration_cache_preserves_parser_results(value):
    expected = model._parse_secs(value)
    result = model.secs(value)
    if isinstance(expected, float) and math.isnan(expected):
        assert math.isnan(result)
    else:
        assert result == expected


@pytest.mark.parametrize("value", [None, "", "N/A", "Unknown", "2026-10-08T15:01:02", "1969-12-31T23:59:59",
    "2024-02-29T12:00:00", "2025-02-29T12:00:00", "2026-1-2T3:4:5", "2026-01-01T00:00:60",
    "2026-01-01T00:00:61", "2026-01-01T00:00:62", "2026-01-01T00:00:00Z", "X" * 300, 7, [], False])
def test_timestamp_cache_preserves_local_parser_results(value):
    assert model.stamp(value) == model._parse_stamp(value)


def test_repeated_durations_and_stamps_are_parsed_once(monkeypatch):
    duration_calls, timestamp_calls = [], []
    old_secs, old_stamp = model._parse_secs, model._parse_stamp

    def duration(value):
        duration_calls.append(value)
        return old_secs(value)

    def timestamp(value):
        timestamp_calls.append(value)
        return old_stamp(value)

    model._cached_secs.cache_clear()
    model._cached_stamp.cache_clear()
    monkeypatch.setattr(model, "_parse_secs", duration)
    monkeypatch.setattr(model, "_parse_stamp", timestamp)
    try:
        for _ in range(100):
            assert model.secs("12:34:56.25") == 45296.25
            assert model.stamp("2026-10-08T15:01:02") is not None
        assert duration_calls == ["12:34:56.25"]
        assert timestamp_calls == ["2026-10-08T15:01:02"]
    finally:
        model._cached_secs.cache_clear()
        model._cached_stamp.cache_clear()


def test_parse_caches_bound_entries_and_reject_oversized_keys():
    model._cached_secs.cache_clear()
    model._cached_stamp.cache_clear()
    try:
        for index in range(9000):
            model.secs(str(index))
            model.stamp("invalid-" + str(index))
        assert model._cached_secs.cache_info().currsize == 8192
        assert model._cached_stamp.cache_info().currsize == 8192
        before_secs, before_stamp = model._cached_secs.cache_info(), model._cached_stamp.cache_info()
        model.secs("0" * 300)
        model.stamp("X" * 300)
        assert model._cached_secs.cache_info() == before_secs
        assert model._cached_stamp.cache_info() == before_stamp
    finally:
        model._cached_secs.cache_clear()
        model._cached_stamp.cache_clear()


@pytest.mark.skipif(not hasattr(time, "tzset"), reason="platform has no runtime timezone changes")
def test_timestamp_cache_tracks_effective_timezone_and_dst(monkeypatch):
    original = os.environ.get("TZ")
    model._cached_stamp.cache_clear()
    try:
        monkeypatch.setenv("TZ", "UTC0")
        time.tzset()
        utc = [model.stamp(value) for value in ("2026-01-08T15:01:02", "2026-07-08T15:01:02")]
        monkeypatch.setenv("TZ", "EST5EDT")
        time.tzset()
        eastern = [model.stamp(value) for value in ("2026-01-08T15:01:02", "2026-07-08T15:01:02")]
        assert [after - before for before, after in zip(utc, eastern)] == [5 * 3600, 4 * 3600]
        monkeypatch.setenv("TZ", "UTC0")
        time.tzset()
        assert [model.stamp(value) for value in ("2026-01-08T15:01:02", "2026-07-08T15:01:02")] == utc
    finally:
        if original is None:
            monkeypatch.delenv("TZ", raising=False)
        else:
            monkeypatch.setenv("TZ", original)
        time.tzset()
        model._cached_stamp.cache_clear()


def test_timestamp_cache_changes_when_timezone_environment_changes(monkeypatch):
    model._cached_stamp.cache_clear()
    monkeypatch.setattr(model, "_parse_stamp", lambda value: 1.0 if os.environ.get("TZ") == "before" else 2.0)
    try:
        monkeypatch.setenv("TZ", "before")
        assert model.stamp("2026-10-08T15:01:02") == 1.0
        monkeypatch.setenv("TZ", "after")
        assert model.stamp("2026-10-08T15:01:02") == 2.0
    finally:
        model._cached_stamp.cache_clear()


def test_unhashable_string_subclasses_keep_original_parser_behavior():
    class UnhashableString(str):
        __hash__ = None

    duration = UnhashableString("1-02:03:04")
    timestamp = UnhashableString("2026-10-08T15:01:02")
    assert model.secs(duration) == model._parse_secs(duration)
    assert model.stamp(timestamp) == model._parse_stamp(timestamp)
