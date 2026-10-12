"""Observer conversion cannot hold the publication lock used by UI snapshots."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from threading import Event
from types import SimpleNamespace
import dataclasses

import pytest

from raven.model import Job, Store
from raven.sampler import Sampler


@pytest.fixture
def worker():
    store = Store(persist=False)
    sampler = Sampler(SimpleNamespace(), store, {}, [], gpu_sampling=False, weather=False, budget=False)
    yield sampler, store
    sampler.shutdown()


def test_blocked_nested_transform_does_not_block_a_real_ui_snapshot(worker, monkeypatch):
    sampler, store = worker
    store.jobs = [Job("7", "analysis", "cpu", "RUNNING", hosts=[["nested host"]])]
    entered, release = Event(), Event()
    original = dataclasses.asdict

    def blocked(value):
        entered.set()
        assert release.wait(10)
        return original(value)

    monkeypatch.setattr(dataclasses, "asdict", blocked)
    sampler.job_observers.append(lambda _: None)
    with ThreadPoolExecutor(max_workers=2) as pool:
        publication = pool.submit(sampler.observe_jobs)
        try:
            assert entered.wait(3)
            # This is the actual foreground reader. It must finish while
            # conversion is still blocked, without publishing partial fields.
            snapshot = pool.submit(store.snapshot).result(timeout=1)
            assert snapshot["jobs"][0].id == "7" and not publication.done()
        finally:
            release.set()
            publication.result(timeout=5)


def test_native_observers_receive_independent_records_without_recursive_conversion(worker, monkeypatch):
    sampler, store = worker
    store.jobs = [Job(str(index), "analysis", "cpu", "RUNNING", hosts=["n1", "n2"])
                  for index in range(128)]
    expected = [asdict(job) for job in store.jobs]
    seen = []

    def mutate(jobs):
        jobs[0]["name"] = "modified"
        jobs[0]["hosts"].append("injected")
        jobs.clear()

    sampler.job_observers[:] = [mutate, seen.extend]
    monkeypatch.setattr(dataclasses, "asdict", lambda *_: pytest.fail("native publication recursively converted every scalar"))
    sampler.observe_jobs()
    assert seen == expected and [asdict_job.name for asdict_job in store.jobs] == ["analysis"] * 128
    assert store.jobs[0].hosts == ["n1", "n2"]


def test_nested_dataclass_and_plugin_field_values_keep_asdict_shape_and_isolation(worker):
    @dataclass
    class Host:
        label: str
        options: dict

    @dataclass
    class PluginJob(Job):
        metadata: dict = field(default_factory=dict)

    sampler, store = worker
    store.jobs = [Job("7", "native", "cpu", "RUNNING", hosts=[Host("n1", {"flags": [1]})]),
                  PluginJob("8", "plugin", "cpu", "RUNNING", metadata={"nested": [{"flags": [2]}]})]
    expected = [asdict(job) for job in store.jobs]
    seen = []

    def mutate(jobs):
        jobs[0]["hosts"][0]["options"]["flags"].append(3)
        jobs[1]["metadata"]["nested"][0]["flags"].append(4)

    sampler.job_observers[:] = [mutate, seen.extend]
    sampler.observe_jobs()
    assert seen == expected and [asdict(job) for job in store.jobs] == expected


def test_native_scalar_and_host_capture_survives_a_later_source_update(worker, monkeypatch):
    sampler, store = worker
    native = Job("7", "before", "cpu", "PENDING", submit=1000, est_start=2000, hosts=["n1"])
    custom = Job("8", "fallback", "cpu", "RUNNING", hosts=[["nested"]])
    store.jobs = [native, custom]
    seen, entered, release = [], Event(), Event()
    original = dataclasses.asdict

    def blocked(value):
        entered.set()
        assert release.wait(10)
        return original(value)

    monkeypatch.setattr(dataclasses, "asdict", blocked)
    sampler.job_observers.append(seen.extend)
    with ThreadPoolExecutor(max_workers=1) as pool:
        publication = pool.submit(sampler.observe_jobs)
        try:
            assert entered.wait(3)
            with store.lock:
                native.name, native.est_start = "after", 2200
                native.hosts.append("n2")
        finally:
            release.set()
            publication.result(timeout=5)
    assert seen[0]["name"] == "before" and seen[0]["est_start"] == 2000 and seen[0]["hosts"] == ["n1"]
