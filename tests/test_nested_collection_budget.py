"""Cluster fan-out obeys the same live permits as other session collectors."""
from dataclasses import replace
from threading import Event, Lock, get_ident

import pytest

from tower import scale_clusters as clusters
from tower.operations import Context
from tower.worker_scheduler import WorkerScheduler, current_scheduler


def sources(count=8):
    return clusters.configuration({"schema": "tower.cluster-workspace/v1",
                                   "clusters": [{"name": f"cluster-{index}"} for index in range(count)]})


def result(source):
    return {"source": source, "jobs": [], "errors": [], "status": "ok"}


def context():
    return Context(cfg={}, cancel=Event())


def test_single_worker_nested_cluster_collection_preserves_current_task_and_queue(monkeypatch):
    scheduler = WorkerScheduler(mode="single", max_queue=2)
    research, source = scheduler.lane("research"), scheduler.lane("source")
    entered, release = Event(), Event()
    calls, unrelated, identities = [], [], []
    def collect(ctx, target, user, hours):
        assert current_scheduler() is scheduler
        identities.append(get_ident())
        calls.append(target["name"])
        return result(target)
    monkeypatch.setattr(clusters, "collect_one", collect)
    def parent():
        assert WorkerScheduler.current() is scheduler
        identities.append(get_ident())
        entered.set()
        assert release.wait(3)
        value = clusters.collect(context(), sources(), user="tester")
        assert current_scheduler() is scheduler
        return value
    try:
        future = research.submit(parent)
        assert entered.wait(1)
        queued = [source.submit(unrelated.append, index) for index in range(2)]
        release.set()
        value = future.result(timeout=5)
        assert len(value["sources"]) == 8 and value["workers"] == 1
        for accepted in queued:
            accepted.result(timeout=3)
        assert len(set(identities)) == 1 and calls == [item["name"] for item in sources()]
        assert unrelated == [0, 1]
        assert current_scheduler() is None
    finally:
        release.set()
        scheduler.shutdown(wait=True, cancel_futures=True)


def test_live_single_to_multi_admits_waiting_cluster_children_and_drains_back(monkeypatch):
    scheduler = WorkerScheduler(workers=2, mode="single")
    research = scheduler.lane("research")
    first, second, release = Event(), Event(), Event()
    lock, calls, active, peak = Lock(), [], [0], [0]
    def collect(ctx, target, user, hours):
        assert current_scheduler() is scheduler
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
            calls.append(target["name"])
            first.set()
            if len(calls) >= 2:
                second.set()
        try:
            assert release.wait(4)
            return result(target)
        finally:
            with lock:
                active[0] -= 1
    monkeypatch.setattr(clusters, "collect_one", collect)
    try:
        future = research.submit(clusters.collect, context(), sources(), user="tester")
        assert first.wait(1) and not second.wait(.05)
        scheduler.request_mode("multi")
        assert second.wait(2)
        assert scheduler.request_mode("single")["pending"]
        release.set()
        assert len(future.result(timeout=5)["sources"]) == 8
        # A following root cannot be admitted until the accepted child tree
        # drains. It observes the committed Single mode, not an abandoned wait.
        assert research.submit(lambda: scheduler.status()["mode"]).result(timeout=3) == "single"
        assert sorted(calls) == sorted(item["name"] for item in sources())
        assert peak[0] == 2 and not active[0]
    finally:
        release.set()
        scheduler.shutdown(wait=True, cancel_futures=True)


def test_cancellation_retires_all_accepted_children_and_preserves_session(monkeypatch):
    scheduler = WorkerScheduler(mode="single")
    ctx = context()
    calls = []
    def collect(context, target, user, hours):
        calls.append(target["name"])
        ctx.cancel.set()
        return result(target)
    monkeypatch.setattr(clusters, "collect_one", collect)
    lane = scheduler.lane("research")
    try:
        with pytest.raises(ValueError, match="cancelled"):
            lane.submit(clusters.collect, ctx, sources(), user="tester").result(timeout=3)
        assert len(calls) == 1
        assert lane.submit(lambda: "still available").result(timeout=3) == "still available"
        assert scheduler.status()["queued"] == 0
    finally:
        scheduler.shutdown(wait=True, cancel_futures=True)


def test_headless_collection_owns_bounded_scheduler_and_clears_thread_context(monkeypatch):
    used = []
    def collect(ctx, target, user, hours):
        used.append(current_scheduler())
        return result(target)
    monkeypatch.setattr(clusters, "collect_one", collect)
    value = clusters.collect(replace(context(), cfg={"worker_mode": "single"}), sources(2), user="tester")
    assert value["workers"] == 1 and len(set(used)) == 1
    assert used[0].status()["closed"] and used[0].status()["running"] == 0
    assert current_scheduler() is None
