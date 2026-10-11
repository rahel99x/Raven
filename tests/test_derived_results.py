"""Shared derivations must be bounded, isolated, cancellable and revision-safe."""
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
import threading
import time

import pytest

from tower.derived_results import ResultCache, ResultKey, StaleResult


def key(**changes):
    values = dict(scope={"cluster": "one", "user": "alice"}, identity={"job_id": "12", "attempt": "first"},
                  revision="r1", algorithm="numerical", version="1", settings={"unit": "s"})
    values.update(changes)
    return ResultKey.create(**values)


def test_every_key_dimension_isolated():
    values = [key(), key(scope={"cluster": "two", "user": "alice"}), key(scope={"cluster": "one", "user": "bob"}),
              key(identity={"job_id": "12", "attempt": "second"}), key(revision="r2"),
              key(algorithm="different"), key(version="2"), key(settings={"unit": "ms"})]
    assert len(set(values)) == len(values)


def test_cached_publication_cannot_be_mutated_through_either_reference():
    cache, source = ResultCache(), {"measurements": [{"value": 1}]}
    first = cache.get_or_compute(key(), lambda: source)
    source["measurements"][0]["value"] = 10
    first["measurements"][0]["value"] = 20
    assert cache.get_or_compute(key(), lambda: pytest.fail("recomputed")) == {"measurements": [{"value": 1}]}
    assert cache.stats()["hits"] == 1


def test_parallel_readers_compute_once_without_holding_publication_lock():
    cache, started, release = ResultCache(), threading.Event(), threading.Event()
    def compute():
        started.set()
        assert release.wait(2)
        return {"value": 1}
    with ThreadPoolExecutor(max_workers=4) as workers:
        first = workers.submit(cache.get_or_compute, key(), compute)
        assert started.wait(2)
        second = workers.submit(cache.get_or_compute, key(), lambda: pytest.fail("duplicate"))
        deadline = time.monotonic() + 2
        while not cache.stats()["joined"] and time.monotonic() < deadline:
            threading.Event().wait(.001)
        assert cache.get_or_compute(key(revision="other"), lambda: {"other": True}) == {"other": True}
        release.set()
        assert first.result(2) == second.result(2) == {"value": 1}
    assert cache.stats()["pending"] == 0


def test_waiter_cancellation_does_not_cancel_other_readers():
    cache, started, release, cancel = ResultCache(), threading.Event(), threading.Event(), threading.Event()
    def compute():
        started.set()
        assert release.wait(2)
        return 7
    with ThreadPoolExecutor(max_workers=2) as workers:
        first = workers.submit(cache.get_or_compute, key(), compute)
        assert started.wait(2)
        second = workers.submit(cache.get_or_compute, key(), lambda: None, cancel=cancel)
        cancel.set()
        with pytest.raises(ValueError, match="cancelled"):
            second.result(2)
        release.set()
        assert first.result(2) == 7
    assert cache.get_or_compute(key(), lambda: None) == 7


def test_stale_work_cannot_publish_or_be_reused():
    cache, current = ResultCache(), [True]
    def compute():
        current[0] = False
        return {"stale": True}
    with pytest.raises(StaleResult):
        cache.get_or_compute(key(), compute, current=lambda: current[0])
    assert cache.stats()["entries"] == cache.stats()["pending"] == 0
    assert cache.get_or_compute(key(), lambda: {"fresh": True}) == {"fresh": True}
    with pytest.raises(StaleResult):
        cache.get_or_compute(key(), lambda: None, current=lambda: False)


@pytest.mark.parametrize("error", [ValueError("invalid"), TimeoutError("source timed out")])
def test_failure_not_cached_and_waiters_do_not_spin_on_computation_timeout(error):
    cache, started, release = ResultCache(), threading.Event(), threading.Event()
    def compute():
        started.set()
        assert release.wait(2)
        raise error
    with ThreadPoolExecutor(max_workers=2) as workers:
        first = workers.submit(cache.get_or_compute, key(), compute)
        assert started.wait(2)
        second = workers.submit(cache.get_or_compute, key(), compute)
        deadline = time.monotonic()+2
        while not cache.stats()["joined"] and time.monotonic() < deadline:
            threading.Event().wait(.001)
        release.set()
        for future in (first, second):
            with pytest.raises(type(error), match=str(error)):
                future.result(2)
    assert cache.get_or_compute(key(), lambda: "recovered") == "recovered"


def test_lru_byte_and_entry_bounds_and_oversize_bypass():
    cache = ResultCache(max_entries=2, max_bytes=20)
    for revision in ("a", "b", "c"):
        cache.get_or_compute(key(revision=revision), lambda: "12345")
    assert cache.stats()["entries"] == 2
    assert cache.stats()["bytes"] == 14
    cache.get_or_compute(key(revision="large"), lambda: "X"*40)
    assert cache.stats()["entries"] == 2
    assert cache.stats()["bytes"] <= 20


@pytest.mark.parametrize("value", [float("nan"), float("inf"), {"x": object()}])
def test_nonportable_or_nonfinite_results_never_retained(value):
    cache = ResultCache()
    with pytest.raises((ValueError, TypeError)):
        cache.get_or_compute(key(), lambda: value)
    assert cache.stats()["entries"] == cache.stats()["pending"] == 0


def test_timeout_completion_boundary_returns_the_actual_result():
    cache = ResultCache()
    class CompletedAfterTimeout:
        def result(self, timeout=None):
            if timeout is not None:
                raise FutureTimeoutError("wait expired just before producer completed")
            return b'{"value":1}'
        def done(self):
            return True
    cache._pending[key()] = CompletedAfterTimeout()
    assert cache.get_or_compute(key(), lambda: pytest.fail("duplicate")) == {"value": 1}


def test_result_limit_matches_existing_operation_contract_not_smaller_input_limit():
    cache = ResultCache(max_bytes=100)
    result = cache.get_or_compute(key(), lambda: {"value": "x"*(9 << 20)})
    assert len(result["value"]) == 9 << 20
    assert cache.stats()["entries"] == 0
