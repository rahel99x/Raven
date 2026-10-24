"""Cached rows never authorize another owner's source paths or progress."""

import threading
from pathlib import Path

from raven.tracing.capture import Recorder
from raven.tracing.workspace_index import TraceIndex


def recording(root, name):
    root.mkdir(mode=0o700)
    with Recorder(
        root, job_id="42", attempt="a", cluster="local", background_flush=False
    ) as rec:
        rec.emit("call", {"frame": "f", "function": name}, thread="one")
        rec.emit("return", {"frame": "f"}, thread="one")
    return rec.path


def finish(index, paths):
    for _ in range(100):
        result = index.refresh(paths, max_events=512, max_seconds=0.1)
        if result["caught_up"]:
            return result
    raise AssertionError("Current owner did not catch up")


def test_reopen_subset_does_not_read_cached_peer_and_can_explicitly_add_it(
    tmp_path, monkeypatch
):
    from raven.tracing import protocol

    one, two = recording(tmp_path / "one", "one"), recording(tmp_path / "two", "two")
    cache = tmp_path / "index.sqlite"
    with TraceIndex(cache) as index:
        finish(index, [one, two])
        original = {row["name"]: row["id"] for row in index.query()["intervals"]}
    reads, original_read = [], protocol.read_json

    def read_json(path, *args, **kwargs):
        reads.append(Path(path).absolute())
        return original_read(path, *args, **kwargs)

    with TraceIndex(cache) as index:
        assert not index.catalog()["sources"]
        assert not index.query()["intervals"]
        with monkeypatch.context() as patch:
            patch.setattr(protocol, "read_json", read_json)
            for _ in range(3):
                catalog = index.refresh([one], max_events=512, max_seconds=0.1)
                assert catalog["caught_up"]
        assert two not in reads
        assert len(catalog["sources"]) == 1 and catalog["sources"][0]["validated"]
        assert [row["id"] for row in index.query()["intervals"]] == [original["one"]]
        assert index.db.execute(
            "SELECT validated FROM sources WHERE path=?", (str(two),)
        ).fetchone() == (0,)
        stop = threading.Event()
        stop.set()
        pending = index.refresh([two], cancel=stop)
        assert not pending["caught_up"] and pending["discovery"]["pending_count"] == 1
        assert len(pending["sources"]) == 1
        assert finish(index, [two])["caught_up"]
        restored = {row["name"]: row["id"] for row in index.query()["intervals"]}
        assert restored == original


def test_directory_registration_aliases_do_not_leave_phantom_pending_paths(tmp_path):
    path = recording(tmp_path / "p", "one")
    with TraceIndex(tmp_path / "index.sqlite") as index:
        result = finish(index, [path.parent])
        assert result["caught_up"] and result["discovery"]["pending_count"] == 0
        result = finish(index, [path])
        assert len(result["sources"]) == 1 and result["caught_up"]


def test_owner_collection_error_marks_all_query_snapshots_incomplete_until_recovery(
    tmp_path,
):
    path = recording(tmp_path / "p", "one")
    with TraceIndex(tmp_path / "index.sqlite") as index:
        catalog = finish(index, [path])
        assert catalog["collection_following"] is False
        assert catalog["job_finished"] is None
        index.collection_following = True
        index.owner_job_finished = False
        clock = catalog["bounds"][0]["clock_id"]
        index.collection_error = (
            "New source discovery failed: temporary shared-storage read error"
        )
        query = index.query(clock_id=clock)
        batch = index.query_many([dict(clock_id=clock, limit=1)])
        for result in (index.catalog(), query, batch):
            assert result["collection_error"] == index.collection_error
            assert not result["caught_up"]
            assert result["collection_following"] is True
            assert result["job_finished"] is False
            assert any(
                index.collection_error in warning for warning in result["warnings"]
            )
        assert query["intervals"] and batch["intervals"]
        index.collection_error = ""
        assert index.query()["caught_up"]
        index.owner_job_finished = True
        assert index.catalog()["job_finished"] is True
        index.owner_job_finished = "unverified source claim"
        assert index.catalog()["job_finished"] is None


def test_unrequested_cached_facets_cannot_consume_current_owner_choices(tmp_path):
    old_root = tmp_path / "old"
    old_root.mkdir(mode=0o700)
    with Recorder(
        old_root, job_id="42", attempt="a", cluster="local", background_flush=False
    ) as rec:
        for position in range(260):
            rec.emit(
                "activity",
                {
                    "activity_kind": "host",
                    "name": "old",
                    "source_pid": rec.identity["pid"] + 10000 + position,
                    "start_ns": position,
                    "end_ns": position + 1,
                },
            )
    old = rec.path
    cache = tmp_path / "index.sqlite"
    with TraceIndex(cache) as index:
        finish(index, [old])
        assert len(index.catalog()["dimensions"]["process"]) == 256
    current = recording(tmp_path / "current", "current")
    with TraceIndex(cache) as index:
        catalog = finish(index, [current])
        process = str(catalog["sources"][0]["identity"]["pid"])
        assert catalog["dimensions"]["process"] == [process]
        assert not catalog["dimensions_truncated"]
