"""Explicit connection lifetime across retained sessions and subscribers."""

import gc
import sqlite3
import threading

import pytest

from raven import shared_collection as S
from raven.platform_session import GovernedBackend
from raven.record import RecordingBackend
from test_flagship_platform import SCOPE, policy


class Backend:
    def __init__(self):
        self.closed = 0

    def run(self, argv, timeout):
        assert not self.closed
        return "123|RUNNING\n", 0.001

    def close(self):
        self.closed += 1


def test_worker_thread_closes_its_connection_even_with_gc_disabled(
    tmp_path, monkeypatch
):
    original = S.sqlite3.connect
    closed = []

    class Connection(sqlite3.Connection):
        def close(self):
            closed.append(threading.get_ident())
            super().close()

    monkeypatch.setattr(
        S.sqlite3, "connect", lambda *a, **kw: original(*a, factory=Connection, **kw)
    )
    cache = S.SharedQueryCache(tmp_path / "cache")
    cache.close()
    closed.clear()
    identifiers = []

    def read():
        identifiers.append(threading.get_ident())
        with cache._connection() as connection:
            assert connection.execute("SELECT 1").fetchone() == (1,)

    collecting = gc.isenabled()
    gc.disable()
    try:
        worker = threading.Thread(target=read)
        worker.start()
        worker.join(timeout=2)
        assert not worker.is_alive()
        assert closed == identifiers
    finally:
        if collecting:
            gc.enable()
        cache.close()


@pytest.mark.parametrize("portable", [False, True])
def test_closing_shared_subscriber_preserves_other_caller_owned_cache(
    tmp_path, portable
):
    first = (
        S.ProcessQueryCache() if portable else S.SharedQueryCache(tmp_path / "cache")
    )
    second = first if portable else S.SharedQueryCache(tmp_path / "cache")
    one = GovernedBackend(Backend(), policy(0.001), first, SCOPE)
    two = GovernedBackend(Backend(), policy(0.001), second, SCOPE)
    assert one.acquisition is two.acquisition
    try:
        assert one.run(["squeue"])[0] == "123|RUNNING\n"
        one.close()
        two.invalidate()
        assert two.run(["squeue", "-h"])[0] == "123|RUNNING\n"
        assert one.inner.closed == 1 and two.inner.closed == 0
        if portable:
            assert first._database is not None
    finally:
        two.close()
        first.close()
        second.close()


def test_recording_only_closes_explicitly_owned_inner_backend(tmp_path):
    external = Backend()
    record = RecordingBackend(external, str(tmp_path / "external.jsonl"))
    record.close()
    assert external.closed == 0
    assert external.run(["squeue"], 1)[0]
    owned = Backend()
    record = RecordingBackend(owned, str(tmp_path / "owned.jsonl"), owns_inner=True)
    record.close()
    record.close()
    assert record.f.closed and owned.closed == 1
