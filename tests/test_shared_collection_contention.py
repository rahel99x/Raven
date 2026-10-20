"""Real SQLite contention cannot repeat loaders or bypass a live reservation."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import sqlite3
import threading

import pytest

from raven import shared_collection as S
from test_flagship_platform import SCOPE, caps, policy, query


def observe_contention(cache, monkeypatch, statement):
    """Observe a real SQLite busy error; do not fabricate database failures."""
    busy, rolled_back = threading.Event(), threading.Event()
    original = cache._connection

    class Connection:
        def __init__(self, connection):
            self.connection = connection

        def __getattr__(self, key):
            return getattr(self.connection, key)

        def execute(self, sql, parameters=()):
            try:
                result = self.connection.execute(sql, parameters)
            except sqlite3.OperationalError as exc:
                if sql.startswith(statement) and S.SharedQueryCache._busy(exc):
                    busy.set()
                raise
            if sql == "ROLLBACK":
                rolled_back.set()
            return result

    @contextmanager
    def connection(**kwargs):
        with original(**kwargs) as db:
            yield Connection(db)

    monkeypatch.setattr(cache, "_connection", connection)
    return busy, rolled_back


@pytest.mark.parametrize("lock", ["writer", "reader"])
def test_result_publication_retries_real_lock_without_repeating_loader(tmp_path, monkeypatch, lock):
    cache = S.SharedQueryCache(tmp_path / "cache", capacity=1)
    busy, _ = observe_contention(cache, monkeypatch, "BEGIN EXCLUSIVE")
    entered, release = threading.Event(), threading.Event()
    calls, q = [], query()
    q["request"]["interval"] = .5

    def load(stop):
        calls.append(1)
        entered.set()
        assert release.wait(2)
        return {"records": ["measured"]}

    with ThreadPoolExecutor(1) as pool, sqlite3.connect(cache.path, isolation_level=None) as blocker:
        future = pool.submit(cache.query, SCOPE, q, load, policy(), caps(), read_only=True)
        try:
            assert entered.wait(2)
            blocker.execute("BEGIN IMMEDIATE" if lock == "writer" else "BEGIN")
            before = blocker.execute("SELECT owner,reserved,value FROM cache").fetchone()
            release.set()
            if lock == "writer":
                assert busy.wait(2), "the test must exercise real publication lock contention"
                assert not future.done()
            else:
                # WAL readers retain a consistent old snapshot while the
                # publication commits without blocking or repeating work.
                assert future.result(2)["value"] == {"records": ["measured"]}
                assert not busy.is_set()
            assert blocker.execute("SELECT owner,reserved,value FROM cache").fetchone() == before
            assert before[0] and before[1] == q["request"]["max_bytes"] and before[2] is None
        finally:
            release.set()
            if blocker.in_transaction:
                blocker.execute("ROLLBACK")
        result = future.result(2)
    assert result["value"] == {"records": ["measured"]} and not result["cached"]
    assert cache.query(SCOPE, q, load, policy(), caps(), read_only=True)["cached"]
    assert calls == [1]


@pytest.mark.parametrize("failure", ["cancel", "deadline"])
def test_blocked_publication_stops_and_new_client_reclaims_only_drained_owner(tmp_path, monkeypatch, failure):
    cache = S.SharedQueryCache(tmp_path / "cache", capacity=1)
    busy, _ = observe_contention(cache, monkeypatch, "BEGIN EXCLUSIVE")
    entered, release, stop = threading.Event(), threading.Event(), threading.Event()
    calls, q = [], query()
    q["request"]["duration"] = .5 if failure == "deadline" else 2

    def load(cancel):
        calls.append("old")
        entered.set()
        assert release.wait(2)
        return {"stale": True}

    with ThreadPoolExecutor(1) as pool, sqlite3.connect(cache.path, isolation_level=None) as blocker:
        future = pool.submit(cache.query, SCOPE, q, load, policy(), caps(), read_only=True, cancel=stop)
        try:
            assert entered.wait(2)
            blocker.execute("BEGIN IMMEDIATE")
            before = blocker.execute("SELECT owner,pid,process_start,reserved FROM cache").fetchone()
            release.set()
            assert busy.wait(2)
            if failure == "cancel":
                stop.set()
            with pytest.raises(ValueError, match="cancelled or exceeded its deadline"):
                future.result(2)
            # Locked cleanup retains the exact reservation and publishes no data.
            assert blocker.execute("SELECT owner,pid,process_start,reserved FROM cache").fetchone() == before
            assert blocker.execute("SELECT value FROM cache").fetchone() == (None,)
            proof = (str(cache.path), cache.host, before[1], before[2], before[0])
            assert proof in S._COMPLETED_OWNERS
        finally:
            release.set()
            if blocker.in_transaction:
                blocker.execute("ROLLBACK")

    independent = S.SharedQueryCache(tmp_path / "cache", capacity=1)
    def replacement(cancel):
        calls.append("new")
        return {"fresh": True}
    assert independent.query(SCOPE, q, replacement, policy(), caps(), read_only=True)["value"] == {"fresh": True}
    assert calls == ["old", "new"] and proof not in S._COMPLETED_OWNERS


def test_invalidation_while_publication_is_blocked_never_releases_a_result(tmp_path, monkeypatch):
    cache = S.SharedQueryCache(tmp_path / "cache")
    busy, _ = observe_contention(cache, monkeypatch, "BEGIN EXCLUSIVE")
    entered, release = threading.Event(), threading.Event()
    calls = []

    def load(stop):
        calls.append(1)
        entered.set()
        assert release.wait(2)
        return {"stale": True}

    with ThreadPoolExecutor(1) as pool, sqlite3.connect(cache.path, isolation_level=None) as blocker:
        future = pool.submit(cache.query, SCOPE, query(), load, policy(), caps(), read_only=True)
        try:
            assert entered.wait(2)
            blocker.execute("BEGIN IMMEDIATE")
            blocker.execute("UPDATE cache SET expires=0 WHERE owner IS NOT NULL")
            release.set()
            assert busy.wait(2)
            blocker.execute("COMMIT")
            with pytest.raises(ValueError, match="invalidated"):
                future.result(2)
        finally:
            release.set()
            if blocker.in_transaction:
                blocker.execute("ROLLBACK")
        assert blocker.execute("SELECT count(*) FROM cache").fetchone() == (0,)
    assert calls == [1]


def test_busy_admission_commit_never_runs_loader_without_durable_ownership(tmp_path, monkeypatch):
    # WAL readers intentionally no longer block COMMIT. Retain the original
    # real-lock rollback regression under SQLite's rollback journal so the
    # committed-owner guard remains tested rather than deleting its assertions.
    original = sqlite3.connect
    class RollbackConnection(sqlite3.Connection):
        def execute(self, statement, *args, **kwargs):
            if statement == "PRAGMA journal_mode=WAL":
                statement = "PRAGMA journal_mode=DELETE"
            return super().execute(statement, *args, **kwargs)
    def rollback_connect(*args, **kwargs):
        return original(*args, factory=RollbackConnection, **kwargs)
    monkeypatch.setattr(sqlite3, "connect", rollback_connect)
    cache = S.SharedQueryCache(tmp_path / "cache", capacity=1)
    busy, rolled_back = observe_contention(cache, monkeypatch, "COMMIT")
    calls, q = [], query()

    def load(stop):
        with sqlite3.connect(cache.path) as verify:
            rows = verify.execute("SELECT owner,reserved FROM cache").fetchall()
        assert len(rows) == 1 and rows[0][0] and rows[0][1] == q["request"]["max_bytes"]
        calls.append(1)
        return {"ok": True}

    with ThreadPoolExecutor(1) as pool, sqlite3.connect(cache.path, isolation_level=None) as blocker:
        blocker.execute("BEGIN")
        assert blocker.execute("SELECT count(*) FROM cache").fetchone() == (0,)
        future = pool.submit(cache.query, SCOPE, q, load, policy(), caps(), read_only=True)
        try:
            assert busy.wait(2) and rolled_back.wait(2)
            assert calls == [], "a rolled-back owner must not invoke the loader"
        finally:
            blocker.execute("ROLLBACK")
        assert future.result(2)["value"] == {"ok": True}
    assert calls == [1]


def test_unknown_same_process_owner_is_not_inferred_to_be_drained(tmp_path):
    cache = S.SharedQueryCache(tmp_path / "cache")
    q, calls = query(), []
    q["request"]["duration"] = .5

    def replace_token(stop):
        with sqlite3.connect(cache.path, isolation_level=None) as db:
            db.execute("UPDATE cache SET owner='unknown-local-owner' WHERE owner IS NOT NULL")
        return {}

    with pytest.raises(ValueError, match="invalidated"):
        cache.query(SCOPE, q, replace_token, policy(), caps(), read_only=True)
    with pytest.raises(TimeoutError, match="deadline"):
        cache.query(SCOPE, q, lambda _: calls.append(1), policy(), caps(), read_only=True)
    with sqlite3.connect(cache.path) as db:
        assert db.execute("SELECT owner,value FROM cache").fetchone() == ("unknown-local-owner", None)
    assert not calls
