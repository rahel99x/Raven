"""A prepared publication must describe its owned input, including races."""
from contextlib import contextmanager
import threading

import pytest

from raven import native_series_cache as N
from raven.views import _prepare_native_metrics


def _samples(value=24.):
    return [{"k": "gpu", "t": 1., "gpu": {"0": [value]}}]


def _prepare(snapshot):
    return _prepare_native_metrics(snapshot, 0, False,
                                   {"live": .5, "gpu": .5}, owned=True)


def _values(publication):
    return list(publication["plots"][0][1])


@contextmanager
def _producer(callback, *release_events):
    """Always release a failing handshake; no producer survives the test."""
    errors = []

    def run():
        try:
            callback()
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=run, name="native-cache-test-producer")
    thread.start()
    try:
        yield
    finally:
        for event in release_events:
            event.set()
        thread.join(timeout=2)
        assert not thread.is_alive(), "The staged producer did not finish"
        assert not errors, errors


def test_concurrent_correction_during_build_cannot_change_owned_lazy_vectors():
    source, original = _samples(), _samples()
    cache = N.NativeSeriesCache()
    build_started, corrected = threading.Event(), threading.Event()

    def correct():
        assert build_started.wait(2)
        source[0]["gpu"]["0"][0] = 99.
        corrected.set()

    def build(snapshot):
        build_started.set()
        assert corrected.wait(2)
        return _prepare(snapshot)

    with _producer(correct, build_started, corrected):
        publication = cache.remember("job", source, build)

    # Materialize after correction: the publication still owns the signed24.
    assert _values(publication) == [24.]
    assert cache.remember("job", original, _prepare) is publication
    assert _values(cache.remember("job", source, _prepare)) == [99.]


def test_correction_between_raw_signature_and_snapshot_is_signed_again(monkeypatch):
    source, original = _samples(), _samples()
    cache = N.NativeSeriesCache()
    raw_signed, corrected = threading.Event(), threading.Event()
    real_signature, staged = N.signature, False

    def sign(values):
        nonlocal staged
        token = real_signature(values)
        if values is source and not staged:
            staged = True
            raw_signed.set()
            assert corrected.wait(2)
        return token

    def correct():
        assert raw_signed.wait(2)
        source[0]["gpu"]["0"][0] = 99.
        corrected.set()

    monkeypatch.setattr(N, "signature", sign)
    with _producer(correct, raw_signed, corrected):
        publication = cache.remember("job", source, _prepare)

    assert _values(publication) == [99.]
    assert cache.entries["job"][0] == real_signature(source)
    replaced = cache.remember("job", original, _prepare)
    assert replaced is not publication
    assert _values(replaced) == [24.]


def test_aba_correction_signs_owned_snapshot_not_restored_raw_input(monkeypatch):
    source = _samples()
    cache = N.NativeSeriesCache()
    raw_signed, corrected = threading.Event(), threading.Event()
    copied, restored = threading.Event(), threading.Event()
    real_signature, real_copy = N.signature, N.owned_snapshot
    staged, captured = False, []

    def sign(values):
        nonlocal staged
        token = real_signature(values)
        if values is source and not staged:
            staged = True
            raw_signed.set()
            assert corrected.wait(2)
        return token

    def own(value, memo=None):
        snapshot = real_copy(value, memo)
        if value is source and memo is None and not captured:
            captured.append(snapshot)
            copied.set()
            assert restored.wait(2)
        return snapshot

    def correct_and_restore():
        assert raw_signed.wait(2)
        source[0]["gpu"]["0"][0] = 99.
        corrected.set()
        assert copied.wait(2)
        source[0]["gpu"]["0"][0] = 24.
        restored.set()

    monkeypatch.setattr(N, "signature", sign)
    monkeypatch.setattr(N, "owned_snapshot", own)
    with _producer(correct_and_restore, raw_signed, corrected, copied, restored):
        publication = cache.remember("job", source, _prepare)

    assert _values(publication) == [99.]
    assert cache.entries["job"][0] == real_signature(captured[0])
    assert cache.entries["job"][0] != real_signature(source)
    replacement = cache.remember("job", source, _prepare)
    assert replacement is not publication
    assert _values(replacement) == [24.]


def test_snapshot_preserves_tuple_list_cycles_and_shared_containers():
    shared = []
    pair = (shared,)
    shared.append(pair)
    source = [pair, pair, shared]
    snapshot = N.owned_snapshot(source)
    assert snapshot is not source
    assert snapshot[0] is snapshot[1]
    assert snapshot[0] is not pair
    assert snapshot[2] is not shared
    assert snapshot[0][0] is snapshot[2]
    assert snapshot[2][0] is snapshot[0]
    assert N.signature(snapshot) == N.signature(source)


@pytest.mark.parametrize("container", ["dict-key", "set-member"])
def test_immutable_tuple_aliases_keep_exact_signatures_and_cache_hits(container):
    pair = (1, 2)
    metadata = {pair: "metric", "alias": pair} if container == "dict-key" else {"alias": pair, "members": {pair}}
    source = _samples()
    source[0]["metadata"] = metadata
    snapshot = N.owned_snapshot(source)
    copied = snapshot[0]["metadata"]
    other = next(key for key in copied if type(key) is tuple) if container == "dict-key" else next(iter(copied["members"]))
    assert copied["alias"] is other
    assert N.signature(snapshot) == N.signature(source)
    cache = N.NativeSeriesCache()
    publication = cache.remember("job", source, _prepare)
    assert cache.remember("job", source, _prepare) is publication


@pytest.mark.parametrize("base", [object, dict, list, tuple, str, float, int, bytearray, set])
def test_snapshot_and_signature_never_execute_extension_hooks(base):
    calls = []

    class Extension(base):
        def __deepcopy__(self, memo):
            calls.append("deepcopy")
            raise AssertionError("An extension copy hook was executed")

        def __reduce__(self):
            calls.append("reduce")
            raise AssertionError("An extension reducer was executed")

        def __reduce_ex__(self, protocol):
            calls.append("reduce_ex")
            raise AssertionError("An extension reducer was executed")

    extension = Extension()
    source = _samples()
    source[0]["extension"] = extension
    snapshot = N.owned_snapshot(source)
    assert snapshot[0]["extension"] is extension
    assert N.signature(snapshot) is None
    cache = N.NativeSeriesCache()
    assert _values(cache.remember("job", source, _prepare)) == [24.]
    assert not cache.entries and cache.bytes == 0
    assert calls == []


def test_extension_inserted_after_hit_check_cannot_execute_hooks_or_be_cached(monkeypatch):
    calls = []

    class Extension:
        def __deepcopy__(self, memo):
            calls.append("deepcopy")
            raise AssertionError("A raced extension copy hook was executed")

        def __reduce_ex__(self, protocol):
            calls.append("reduce")
            raise AssertionError("A raced extension reducer was executed")

    source = _samples()
    real_signature = N.signature
    staged = False

    def sign(values):
        nonlocal staged
        token = real_signature(values)
        if values is source and not staged:
            staged = True
            source[0]["extension"] = Extension()
        return token

    monkeypatch.setattr(N, "signature", sign)
    cache = N.NativeSeriesCache()
    assert _values(cache.remember("job", source, _prepare)) == [24.]
    assert not cache.entries and cache.bytes == 0
    assert calls == []


def test_recursive_snapshot_failure_bypasses_and_removes_stale_cache(monkeypatch):
    source = _samples()
    cache = N.NativeSeriesCache()
    original = cache.remember("job", source, _prepare)
    source[0]["gpu"]["0"][0] = 99.

    def too_deep(value, memo=None):
        raise RecursionError("Staged deep ignored metadata")

    monkeypatch.setattr(N, "owned_snapshot", too_deep)
    replacement = cache.remember("job", source, _prepare)
    assert replacement is not original
    assert _values(replacement) == [99.]
    assert not cache.entries and cache.bytes == 0
