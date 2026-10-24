"""Verified persistent indexing, exact ranges and source isolation."""

from __future__ import annotations

import threading

import pytest

from raven.tracing.capture import Recorder
from raven.tracing.workspace_index import TraceIndex


def recorder(root, *, attempt="attempt-a", job="42", node=None):
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    value = Recorder(
        root,
        job_id=job,
        attempt=attempt,
        cluster="local",
        background_flush=False,
        initial_mono_ns=100,
    )
    if node is not None:
        value.identity["node"] = node
    return value


def finish(index, paths, **kwargs):
    for _ in range(1000):
        result = index.refresh(paths, max_events=512, max_seconds=0.05, **kwargs)
        if result["caught_up"]:
            return result
    pytest.fail("Index did not catch up")


def calls(rec, count=1, *, base=100):
    for position in range(count):
        start = base + position * 10
        rec.emit(
            "call",
            {"frame": "frame", "function": "solve", "file": "test.py", "line": 2},
            thread="one",
            mono_ns=start,
        )
        rec.emit(
            "return",
            {"frame": "frame", "function": "solve"},
            thread="one",
            mono_ns=start + 5,
        )


def test_full_run_beyond_retained_window_and_stable_detail(tmp_path):
    with recorder(tmp_path / "project") as rec:
        calls(rec, 10_050)
    with TraceIndex(tmp_path / "index.sqlite") as index:
        catalog = finish(index, [rec.path])
        assert catalog["sources"][0]["read_seq"] > 20_000
        clock = catalog["bounds"][0]["clock_id"]
        early = index.query(start_ns=100, end_ns=106, clock_id=clock)
        assert len(early["intervals"]) == 1
        row = early["intervals"][0]
        assert (
            row["start_ns"] == 100 and row["end_ns"] == 105 and row["duration_ns"] == 5
        )
        assert index.detail(row["id"]) == row
        assert index.query(start_ns=100_500, end_ns=100_506, clock_id=clock)[
            "intervals"
        ]
        assert index.functions()["functions"][0]["count"] == 10_050
        overview = index.overview(bins=32)
        assert len(overview["domains"][0]["bins"]) <= 32
        assert overview["domains"][0]["start_ns"] == 100


def test_pairing_crosses_refresh_pages_and_self_time_is_synchronous(tmp_path):
    with recorder(tmp_path / "project") as rec:
        rec.emit(
            "call", {"frame": "outer", "function": "outer"}, thread="one", mono_ns=100
        )
        for n in range(140):
            rec.emit("note", {"name": "middle"}, thread="one", mono_ns=101 + n)
        rec.emit(
            "call", {"frame": "inner", "function": "inner"}, thread="one", mono_ns=250
        )
        rec.emit("return", {"frame": "inner"}, thread="one", mono_ns=270)
        rec.emit("return", {"frame": "outer"}, thread="one", mono_ns=300)
    with TraceIndex(tmp_path / "index.sqlite") as index:
        first = index.refresh([rec.path], max_events=32, max_seconds=0.1)
        assert not first["caught_up"]
        assert not index.query()["intervals"]
        finish(index, [rec.path])
        rows = {row["name"]: row for row in index.query()["intervals"]}
        assert rows["outer"]["duration_ns"] == 200
        assert rows["outer"]["self_ns"] == 180
        assert rows["inner"]["depth"] == 1
        assert rows["inner"]["self_ns"] == 20


def test_uint64_nanoseconds_remain_exact_and_rtree_never_replaces_exact_filter(
    tmp_path,
):
    start = (1 << 62) + 17
    with recorder(tmp_path / "project") as rec:
        calls(rec, 2, base=start)
        rec.emit("stream_end", {"reason": "historical fixture"}, mono_ns=start + 30)
        rec.state = "CLOSED"
        rec.flush()
    with TraceIndex(tmp_path / "index.sqlite") as index:
        catalog = finish(index, [rec.path])
        clock = catalog["bounds"][0]["clock_id"]
        rows = index.query(start_ns=start + 1, end_ns=start + 4, clock_id=clock)[
            "intervals"
        ]
        assert rows[0]["start_ns"] == start
        assert rows[0]["duration_ns"] == 5
        assert not index.query(start_ns=start + 6, end_ns=start + 9, clock_id=clock)[
            "intervals"
        ]
        assert index.functions()["functions"][0]["inclusive_ns"] == 10


def test_loss_and_unmatched_entries_do_not_acquire_invented_durations(tmp_path):
    with recorder(tmp_path / "project") as rec:
        rec.emit(
            "call", {"frame": "x", "function": "missing"}, thread="one", mono_ns=100
        )
        rec.emit("gap", {"reason": "lost"}, thread="one", mono_ns=101)
        rec.emit("return", {"frame": "x"}, thread="one", mono_ns=102)
        calls(rec, base=103)
    with TraceIndex(tmp_path / "index.sqlite") as index:
        finish(index, [rec.path])
        result = index.query()
        assert [row["name"] for row in result["intervals"]] == ["solve"]
        assert any(row["kind"] == "gap" for row in result["markers"])
        assert any(row["kind"] == "unmatched_entry" for row in result["markers"])


def test_live_incremental_read_and_reopen_revalidation(tmp_path):
    with recorder(tmp_path / "project") as rec:
        calls(rec)
        rec.flush()
        with TraceIndex(tmp_path / "index.sqlite") as index:
            finish(index, [rec.path])
            original = index.query()["intervals"][0]["id"]
            calls(rec, base=200)
            rec.flush()
            finish(index, [rec.path])
            assert len(index.query()["intervals"]) == 2
    with TraceIndex(tmp_path / "index.sqlite") as index:
        assert index.catalog()["sources"][0]["status"] == "REVALIDATING"
        assert not index.query()["intervals"]
        finish(index, [rec.path])
        assert index.query()["intervals"][0]["id"] == original
        assert len(index.query()["intervals"]) == 2


def test_expected_attempt_is_immutable_and_sources_are_namespaced(tmp_path):
    with recorder(tmp_path / "p1") as one, recorder(tmp_path / "p2") as two:
        calls(one)
        calls(two)
    with TraceIndex(tmp_path / "index.sqlite") as index:
        finish(
            index,
            [one.path, two.path],
            expected={"job_id": "42", "attempt": "attempt-a", "cluster": "local"},
        )
        rows = index.query()["intervals"]
        assert len(rows) == 2 and len({row["id"] for row in rows}) == 2
        with pytest.raises(ValueError, match="expected exact"):
            index.refresh([one.path], expected={"attempt": "other"})
        with recorder(tmp_path / "p3", attempt="other") as three:
            calls(three)
        with pytest.raises(ValueError, match="different job attempts"):
            index.refresh([three.path])
        assert len(index.query()["intervals"]) == 2


def test_private_paths_single_owner_and_shared_thread_queries(tmp_path):
    with recorder(tmp_path / "project") as rec:
        calls(rec)
    with TraceIndex(tmp_path / "index.sqlite") as index:
        finish(index, [rec.path])
        with pytest.raises(ValueError, match="in use"):
            TraceIndex(tmp_path / "index.sqlite")
        results = []
        thread = threading.Thread(target=lambda: results.append(index.query()))
        thread.start()
        thread.join(2)
        assert not thread.is_alive() and results[0]["intervals"]
    assert (tmp_path / "index.sqlite").stat().st_mode & 0o777 == 0o600
    target = tmp_path / "target"
    target.write_text("untouched")
    link = tmp_path / "link.sqlite"
    link.symlink_to(target)
    with pytest.raises(ValueError, match="private user-owned"):
        TraceIndex(link)
    assert target.read_text() == "untouched"


def test_typed_activity_and_cpu_query_brackets_are_preserved_separately(tmp_path):
    with recorder(tmp_path / "project") as rec:
        rec.emit(
            "activity",
            {
                "activity_kind": "gpu_kernel",
                "name": "kernel",
                "start_ns": 100,
                "end_ns": 150,
                "duration_ns": 50,
                "device": {"id": "GPU-1", "ordinal": 0},
                "context": "ctx",
                "queue": "s1",
                "correlation_id": "launch-1",
                "source_node": "producer",
            },
            thread="gpu",
            mono_ns=100,
        )
        rec.emit(
            "note",
            {"name": "location"},
            thread="one",
            mono_ns=160,
            _observation={
                "schema": "raven.cpu-placement/v1",
                "logical_cpu": 7,
                "socket_id": 0,
                "core_id": 3,
                "sample_start_ns": 160,
                "sample_end_ns": 162,
                "source": "linux-sched_getcpu",
            },
        )
    with TraceIndex(tmp_path / "index.sqlite") as index:
        finish(index, [rec.path])
        result = index.query()
        activity = result["intervals"][0]
        assert activity["kind"] == "gpu_kernel" and activity["node"] == "producer"
        assert (
            activity["device"]["id"] == "GPU-1"
            and activity["correlation_id"] == "launch-1"
        )
        point = next(
            row for row in result["markers"] if row["kind"] == "cpu_observation"
        )
        assert point["logical_cpu"] == 7 and point["duration_ns"] == 2
        assert not any(row["kind"] == "cpu_observation" for row in result["intervals"])
        assert index.functions()["functions"][0]["self_ns"] is None


def test_cancellation_budget_and_integrity_quarantine(tmp_path):
    with recorder(tmp_path / "project") as rec:
        calls(rec, 200)
    with TraceIndex(tmp_path / "index.sqlite") as index:
        stop = threading.Event()
        stop.set()
        assert not index.refresh([rec.path], cancel=stop)["sources"]
        first = index.refresh([rec.path], max_events=1, max_seconds=0.05)
        assert first["sources"][0]["read_seq"] == 1
        journal = rec.path.parent / "events.jsonl"
        raw = journal.read_bytes()
        journal.write_bytes(raw.replace(b"solve", b"wrong", 1))
    with TraceIndex(tmp_path / "index.sqlite") as index:
        result = index.refresh([rec.path], max_events=512, max_seconds=0.5)
        assert result["sources"][0]["status"] == "INVALID"
        assert not index.query()["intervals"]
        assert index.catalog()["warnings"]


def test_missing_source_preserves_other_verified_data_and_reports_read_error(tmp_path):
    with recorder(tmp_path / "project") as rec:
        calls(rec)
    with TraceIndex(tmp_path / "index.sqlite") as index:
        finish(index, [rec.path])
        result = index.refresh([rec.path, tmp_path / "missing"])
        assert index.query()["intervals"]
        assert any("unavailable" in warning for warning in result["warnings"])


def test_query_bounds_are_explicit(tmp_path):
    with TraceIndex(tmp_path / "index.sqlite") as index:
        with pytest.raises(ValueError):
            index.query(start_ns=10, end_ns=1)
        with pytest.raises(ValueError):
            index.query(limit=10001)
        with pytest.raises(ValueError):
            index.overview(bins=1025)
        with pytest.raises(ValueError):
            index.refresh([], expected={"pid": 1})


def test_index_quota_stops_explicitly_without_false_complete_prefix(tmp_path):
    with recorder(tmp_path / "project") as rec:
        calls(rec, 2000)
    with TraceIndex(tmp_path / "index.sqlite", max_bytes=1 << 20) as index:
        with pytest.raises(ValueError, match="database budget or storage failed"):
            finish(index, [rec.path])
        assert not index.catalog()["caught_up"]
        assert index.catalog()["index"]["state"] == "storage-blocked"
        prefix = index.query()["intervals"]
        statements = []
        index.db.set_trace_callback(statements.append)
        with pytest.raises(ValueError, match="reopen the index"):
            index.refresh([rec.path])
        index.db.set_trace_callback(None)
        assert not statements  # No repeated expensive revalidation/replay.
        assert index.query()["intervals"] == prefix
        assert index.path.stat().st_size <= 1 << 20
        # Storage failure does not leave the owner lock permanently held or
        # claim that all raw events have been indexed.
    with TraceIndex(tmp_path / "index.sqlite", max_bytes=8 << 20) as index:
        result = finish(index, [rec.path])
        assert result["caught_up"]
        assert index.functions()["functions"][0]["count"] == 2000


def test_live_read_failure_retains_prefix_and_reports_freshness_then_recovers(tmp_path):
    with recorder(tmp_path / "project") as rec:
        calls(rec)
        rec.flush()
        with TraceIndex(tmp_path / "index.sqlite") as index:
            finish(index, [rec.path])
            original = index.query()["intervals"][0]["id"]
            hidden = rec.path.with_name("manifest-hidden.json")
            rec.path.rename(hidden)
            try:
                result = index.refresh([rec.path])
                assert not result["caught_up"]
                assert result["sources"][0]["read_status"] == "retry-later"
                assert result["sources"][0]["error"]
                assert index.query()["intervals"][0]["id"] == original
            finally:
                hidden.rename(rec.path)
            assert finish(index, [rec.path])["caught_up"]
            assert "error" not in index.catalog()["sources"][0]


def test_multires_overview_uses_incremental_counts_not_raw_interval_scans(tmp_path):
    with recorder(tmp_path / "project") as rec:
        calls(rec, 30)
        rec.flush()
        with TraceIndex(tmp_path / "index.sqlite") as index:
            finish(index, [rec.path])
            statements = []
            index.db.set_trace_callback(statements.append)
            first = index.overview(bins=16)
            index.db.set_trace_callback(None)
            assert sum(row["count"] for row in first["domains"][0]["bins"]) == 30
            assert first["domains"][0]["quantum_ns"] >= 1
            assert not any("FROM intervals" in sql for sql in statements)
            # Cached data returned to callers is detached, not mutable index
            # state; adding verified calls advances the density generation.
            first["domains"][0]["bins"][0]["count"] = -1
            assert all(
                row["count"] >= 0
                for row in index.overview(bins=16)["domains"][0]["bins"]
            )
            calls(rec, 3, base=500)
            rec.flush()
            finish(index, [rec.path])
            second = index.overview(bins=16)
            assert sum(row["count"] for row in second["domains"][0]["bins"]) == 33


def test_function_range_requires_explicit_domain_for_unaligned_sources(tmp_path):
    with (
        recorder(tmp_path / "one", node="node-one") as one,
        recorder(tmp_path / "two", node="node-two") as two,
    ):
        calls(one, 2)
        calls(two, 3)
    with TraceIndex(tmp_path / "index.sqlite") as index:
        catalog = finish(index, [one.path, two.path])
        assert len(catalog["bounds"]) == 2
        with pytest.raises(ValueError, match="clock domain"):
            index.functions(start_ns=100, end_ns=500)
        selected = catalog["sources"][0]
        count = 2 if selected["identity"]["node"] == "node-one" else 3
        result = index.functions(
            start_ns=100, end_ns=500, clock_id=selected["clock"]["id"]
        )
        assert result["functions"][0]["count"] == count
        assert sum(row["count"] for row in index.functions()["functions"]) == 5


def test_full_function_statistics_are_incremental_and_source_scoped(tmp_path):
    with recorder(tmp_path / "one") as one, recorder(tmp_path / "two") as two:
        calls(one, 3)
        calls(two, 2)
    with TraceIndex(tmp_path / "index.sqlite") as index:
        finish(index, [one.path, two.path])
        statements = []
        index.db.set_trace_callback(statements.append)
        rows = index.functions()["functions"]
        index.db.set_trace_callback(None)
        assert len(rows) == 2 and len({row["source_id"] for row in rows}) == 2
        assert sum(row["count"] for row in rows) == 5
        assert sum(row["inclusive_ns"] for row in rows) == 25
        assert not any("FROM intervals" in query for query in statements)


def test_quarantined_source_is_removed_from_queries_density_and_statistics(tmp_path):
    with recorder(tmp_path / "one") as one, recorder(tmp_path / "two") as two:
        calls(one)
        calls(two)
    with TraceIndex(tmp_path / "index.sqlite") as index:
        finish(index, [one.path, two.path])
        assert (
            sum(
                row["count"]
                for domain in index.overview()["domains"]
                for row in domain["bins"]
            )
            == 2
        )
        journal = two.path.parent / "events.jsonl"
        replacement = journal.with_name("replacement")
        replacement.write_bytes(journal.read_bytes())
        replacement.replace(journal)
        result = index.refresh([one.path, two.path], max_seconds=0.5)
        assert any(row["status"] == "INVALID" for row in result["sources"])
        assert len(index.query()["intervals"]) == 1
        assert (
            sum(
                row["count"]
                for domain in index.overview()["domains"]
                for row in domain["bins"]
            )
            == 1
        )
        assert sum(row["count"] for row in index.functions()["functions"]) == 1


def test_unrelated_database_is_not_modified_and_failed_probe_releases_owner(tmp_path):
    import hashlib
    import sqlite3

    path = tmp_path / "unrelated.sqlite"
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("CREATE TABLE important(value TEXT)")
    connection.execute("INSERT INTO important VALUES('keep me')")
    connection.commit()
    connection.close()
    path.chmod(0o600)
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    for _ in range(2):
        with pytest.raises(ValueError, match="Unsupported trace workspace index"):
            TraceIndex(path)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    connection = sqlite3.connect(path)
    assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert connection.execute("SELECT value FROM important").fetchone()[0] == "keep me"
    connection.close()


def test_failed_sqlite_connection_releases_lock_and_empty_cache_can_recover(
    tmp_path, monkeypatch
):
    from raven.tracing import workspace_index as owner
    import sqlite3

    original = sqlite3.connect
    with monkeypatch.context() as patch:
        patch.setattr(
            owner.sqlite3,
            "connect",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                sqlite3.OperationalError("injected open failure")
            ),
        )
        with pytest.raises(sqlite3.OperationalError, match="injected"):
            TraceIndex(tmp_path / "index.sqlite")
    assert sqlite3.connect is original
    with TraceIndex(tmp_path / "index.sqlite") as index:
        assert not index.catalog()["sources"]


def test_source_input_and_query_source_filters_are_bounded_before_sql(tmp_path):
    with TraceIndex(tmp_path / "index.sqlite") as index:
        with pytest.raises(ValueError, match="512-source"):
            index.refresh((str(number) for number in range(1_000_000)))
        with pytest.raises(ValueError, match="source IDs"):
            index.query(source_ids=[{"bad": "object"}])
        with pytest.raises(ValueError, match="source IDs"):
            index.query(source_ids="not a list")


def test_query_many_returns_independent_clock_ranges_with_one_catalog(tmp_path):
    with (
        recorder(tmp_path / "one", node="node-one") as one,
        recorder(tmp_path / "two", node="node-two") as two,
    ):
        calls(one, 5)
        calls(two, 5)
    with TraceIndex(tmp_path / "index.sqlite") as index:
        catalog = finish(index, [one.path, two.path])
        requests = [
            dict(clock_id=bound["clock_id"], start_ns=100, end_ns=200, limit=1)
            for bound in catalog["bounds"]
        ]
        statements = []
        index.db.set_trace_callback(statements.append)
        result = index.query_many(requests)
        index.db.set_trace_callback(None)
        assert len(result["intervals"]) == 2
        assert len({row["clock_id"] for row in result["intervals"]}) == 2
        assert len(result["ranges"]) == 2 and result["separate_clocks"]
        assert result["range"]["start_ns"] is None and result["range"]["end_ns"] is None
        assert result["truncated"] and not result["unqueried_ranges"]
        assert (
            sum(
                "SELECT id,validated,status,payload FROM sources" in sql
                for sql in statements
            )
            == 1
        )
        with pytest.raises(ValueError, match="2,000-row"):
            index.query_many(
                [
                    dict(clock_id=bound["clock_id"], limit=1001)
                    for bound in catalog["bounds"]
                ]
            )
        with pytest.raises(ValueError, match="explicit clock"):
            index.query_many([dict(limit=1)])


def test_query_payload_byte_budget_is_explicit_and_detail_remains_complete(tmp_path):
    with recorder(tmp_path / "project") as rec:
        for position in range(150):
            data = {f"large_{number}": "x" * 8000 for number in range(6)}
            data.update(frame="frame", function="large", name="large")
            rec.emit("call", data, thread="one", mono_ns=100 + position * 10)
            rec.emit(
                "return", {"frame": "frame"}, thread="one", mono_ns=105 + position * 10
            )
    with TraceIndex(tmp_path / "index.sqlite") as index:
        catalog = finish(index, [rec.path])
        result = index.query(limit=2000)
        assert result["truncated"] and len(result["intervals"]) < 150
        assert result["payload_bytes"] <= result["byte_limit"] == 8 << 20
        row = index.detail(catalog["sources"][0]["id"] + ":python:300")
        assert row["data"]["large_5"] == "x" * 8000
        assert index.functions()["functions"][0]["count"] == 150


def test_source_registration_rotates_when_io_consumes_the_work_slice(
    tmp_path, monkeypatch
):
    import time

    sources = []
    for position in range(3):
        with recorder(tmp_path / str(position)) as rec:
            calls(rec)
        sources.append(rec.path)
    with TraceIndex(tmp_path / "index.sqlite") as index:
        original = index._source
        seen = []

        def slow_source(path, expected):
            seen.append(path)
            time.sleep(0.003)
            return original(path, expected)

        with monkeypatch.context() as patch:
            patch.setattr(index, "_source", slow_source)
            first = index.refresh(sources, max_seconds=0.001)
            assert first["discovery"]["pending_count"] == 2
            assert not first["caught_up"]
            index.refresh(sources, max_seconds=0.001)
            index.refresh(sources, max_seconds=0.001)
        assert set(seen) == set(sources)
        assert finish(index, sources)["discovery"]["pending_count"] == 0
        assert len(index.query()["intervals"]) == 3


def test_removed_missing_paths_do_not_accumulate_errors_without_bound(tmp_path):
    with recorder(tmp_path / "project") as rec:
        calls(rec)
    with TraceIndex(tmp_path / "index.sqlite") as index:
        finish(index, [rec.path])
        assert not index.refresh([rec.path, tmp_path / "old-missing"])["caught_up"]
        result = index.refresh([rec.path, tmp_path / "current-missing"])
        assert len(index._discovery_errors) == 1
        assert "current-missing" in result["warnings"][0]
        assert "old-missing" not in str(result["warnings"])


def test_imported_report_symbols_preserve_process_device_and_generation_scopes(
    tmp_path,
):
    with recorder(tmp_path / "project") as rec:
        for position, (pid, device, generation) in enumerate(
            (
                (1, "GPU-a", "first"),
                (1, "GPU-b", "first"),
                (2, "GPU-a", "first"),
                (1, "GPU-a", "second"),
            )
        ):
            rec.emit(
                "activity",
                {
                    "activity_kind": "gpu_kernel",
                    "name": "same recorded symbol",
                    "start_ns": 100 + 10 * position,
                    "end_ns": 105 + 10 * position,
                    "source_pid": pid,
                    "source_node": "producer",
                    "source_process_start": generation,
                    "device": {"uuid": device},
                    "context": "ctx",
                },
                mono_ns=100 + 10 * position,
            )
    with TraceIndex(tmp_path / "index.sqlite") as index:
        finish(index, [rec.path])
        rows = index.functions()["functions"]
        assert len(rows) == 4 and all(row["count"] == 1 for row in rows)
        assert {row["reported_identity"]["source_process_start"] for row in rows} == {
            "first",
            "second",
        }
        assert sum(row["inclusive_ns"] for row in rows) == 20


def test_explicit_thread_reuse_does_not_subtract_a_new_lifetime_as_a_child(tmp_path):
    with recorder(tmp_path / "project") as rec:
        rec.emit(
            "call",
            {"frame": "same", "function": "old", "thread_generation": "old"},
            thread="7",
            mono_ns=100,
        )
        rec.emit(
            "call",
            {"frame": "same", "function": "new", "thread_generation": "new"},
            thread="7",
            mono_ns=110,
        )
        rec.emit(
            "return",
            {"frame": "same", "thread_generation": "new"},
            thread="7",
            mono_ns=120,
        )
        rec.emit(
            "return",
            {"frame": "same", "thread_generation": "old"},
            thread="7",
            mono_ns=130,
        )
    with TraceIndex(tmp_path / "index.sqlite") as index:
        finish(index, [rec.path])
        rows = {row["name"]: row for row in index.query()["intervals"]}
        assert rows["old"]["self_ns"] == rows["old"]["duration_ns"] == 30
        assert rows["new"]["self_ns"] == rows["new"]["duration_ns"] == 10
        assert rows["old"]["depth"] == rows["new"]["depth"] == 0
        assert rows["old"]["thread"] == rows["new"]["thread"] == "7"


@pytest.mark.parametrize("boundary", ["stack_checkpoint", "thread_start", "thread_end"])
def test_thread_boundary_never_manufactures_a_call_across_unknown_lifetime(
    boundary, tmp_path
):
    with recorder(tmp_path / "project") as rec:
        rec.emit(
            "call",
            {"frame": "same", "function": "missing", "thread_generation": "old"},
            thread="7",
            mono_ns=100,
        )
        rec.emit(boundary, {"name": "boundary", "frames": []}, thread="7", mono_ns=110)
        rec.emit(
            "return",
            {"frame": "same", "thread_generation": "old"},
            thread="7",
            mono_ns=120,
        )
        calls(rec, base=130)
    with TraceIndex(tmp_path / "index.sqlite") as index:
        finish(index, [rec.path])
        rows = index.query()["intervals"]
        assert [row["name"] for row in rows] == ["solve"]
        assert any(row["kind"] == "unmatched_entry" for row in index.query()["markers"])


def test_overview_run_budget_does_not_multiply_by_node_count(tmp_path):
    paths = []
    for position in range(4):
        with recorder(tmp_path / str(position), node="node-" + str(position)) as rec:
            calls(rec, 40)
        paths.append(rec.path)
    with TraceIndex(tmp_path / "index.sqlite") as index:
        finish(index, paths)
        result = index.overview(bins=8)
        assert len(result["domains"]) == 4
        assert result["total_bins"] <= result["bucket_limit"] == 8
        assert (
            sum(row["count"] for domain in result["domains"] for row in domain["bins"])
            == 160
        )


def test_function_statistic_payloads_are_byte_bounded_without_losing_raw_detail(
    tmp_path,
):
    with recorder(tmp_path / "project") as rec:
        for position in range(180):
            rec.emit(
                "activity",
                {
                    "activity_kind": "gpu_kernel",
                    "name": "large scoped label",
                    "start_ns": 100 + 10 * position,
                    "end_ns": 105 + 10 * position,
                    "device": {
                        "id": str(position),
                        **{
                            f"reported_extension_{field}": "x" * 8000
                            for field in range(6)
                        },
                    },
                },
                mono_ns=100 + 10 * position,
            )
    with TraceIndex(tmp_path / "index.sqlite") as index:
        finish(index, [rec.path])
        result = index.functions(limit=1000)
        assert result["truncated"] and len(result["functions"]) < 180
        assert result["payload_bytes"] <= result["byte_limit"] == 8 << 20
        row = index.query(start_ns=100, end_ns=105)["intervals"][0]
        assert index.detail(row["id"])["device"]["reported_extension_5"] == "x" * 8000
