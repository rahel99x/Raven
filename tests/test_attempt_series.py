"""Attempt attribution is captured before IO and never inferred from job numbers."""
from dataclasses import replace
import json
from pathlib import Path

import pytest
from raven.model import Store, Job, Live
from raven.report import _ReportStore

SUBMIT = "2026-10-17T10:00:00"
START = "2026-10-17T10:00:01"


def bind(store, jid="7", *, restart=0, start=START, cluster="lab"):
    job = Job(jid, "training", "gpu", "RUNNING", submit=SUBMIT, start=start, cluster=cluster)
    store.apply_jobs([job])
    details = dict(JobId=jid, SubmitTime=SUBMIT, StartTime=start, Restarts=str(restart), Cluster=cluster)
    store.details[jid] = details
    assert store.bind_series_scheduler(jid, details)
    return store.capture_series_identity(jid)


def test_same_job_and_timestamp_different_restart_has_distinct_archive(tmp_path):
    store = Store(str(tmp_path))
    old = bind(store)
    store.record("7", {"t": 1, "cpu": .1}, identity=old, _persist=False)
    old_path = store._series_path("7")
    new = bind(store, restart=1)
    assert old != new and store.series_of("7") == []
    store.record("7", {"t": 2, "cpu": .9}, identity=new, _persist=False)
    assert store.record("7", {"t": 3, "cpu": .5}, identity=old) is None
    store.flush_persistence()
    assert json.loads(Path(old_path).read_text()) == {"t": 1, "cpu": .1}
    new_path = store._series_path("7")
    assert new_path != old_path and json.loads(Path(new_path).read_text()) == {"t": 2, "cpu": .9}
    store.close()
    reopened = Store(str(tmp_path)); bind(reopened, restart=1)
    assert reopened.series_of("7") == [{"t": 2, "cpu": .9}]
    reopened.close()


def test_same_state_requeue_changes_memory_even_without_queue_gap(tmp_path):
    store = Store(str(tmp_path)); old = bind(store)
    store.apply_live("7", Live(t=1, rss=10, avg=.5), identity=old)
    bind(store, start="2026-10-17T10:00:02", restart=1)
    assert "7" not in store.live and not store.series_of("7")
    assert store.apply_live("7", Live(t=2, rss=100), identity=old) is None
    assert "7" not in store.live
    store.close()


def test_unknown_attempt_never_reads_prior_session_or_legacy_file(tmp_path):
    store = Store(str(tmp_path)); store.record("7", {"t": 1})
    first = store._series_path("7"); store.close()
    folder = tmp_path / "series"
    (folder / "7.jsonl").write_text('{"t": 0}\n')
    reopened = Store(str(tmp_path))
    assert reopened.series_of("7") == []
    assert reopened._series_path("7") != first
    assert reopened.series_status("7")["attribution"] == "session-only"
    values, status = reopened.legacy_series("7")
    assert values == [{"t": 0}] and status["attribution"] == "legacy-ambiguous"
    assert Path(first).exists() and (folder / "7.jsonl").exists()
    reopened.close()


def test_zero_restart_verification_preserves_and_promotes_matching_observations(tmp_path):
    store = Store(str(tmp_path))
    job = Job("7", "train", "gpu", "RUNNING", submit=SUBMIT, start=START, cluster="lab")
    store.apply_jobs([job]); store.record("7", {"t": 1})
    unverified = store._series_path("7")
    bind(store)
    assert store.series_of("7") == [{'t':1}]
    assert json.loads(Path(unverified).read_text()) == {"t": 1}
    store.record("7", {"t": 2})
    store.flush_persistence()
    assert store._series_path("7") != unverified
    store.close()
    reopened = Store(str(tmp_path)); bind(reopened)
    assert reopened.series_of("7") == [{"t":1},{"t": 2}]
    reopened.close()


def test_site_scope_and_unsanitized_job_identity_never_collide(tmp_path):
    store = Store(str(tmp_path), source_scope={"site": "alpha"}); bind(store, jid="7.0")
    path = store._series_path("7.0")
    bind(store, jid="7_0")
    assert path != store._series_path("7_0")
    store.close()
    other = Store(str(tmp_path), source_scope={"site": "beta"}); bind(other, jid="7.0")
    assert path != other._series_path("7.0")
    other.close()


def test_queued_restore_and_report_remain_on_captured_attempt(tmp_path):
    store = Store(str(tmp_path)); bind(store); store.record("7", {"t": 1})
    report = _ReportStore(store, store.snapshot(), 100)
    tasks = []; store.configure_series_loader(tasks.append)
    store.series_view("7")
    bind(store, restart=1); store.record("7", {"t": 2})
    tasks.pop()()
    assert store.series_of("7") == [{"t": 2}]
    assert report.series_of("7") == [{"t": 1}]
    store.close()


def test_conflicting_scheduler_details_cannot_verify_recording(tmp_path):
    store = Store(str(tmp_path)); bind(store)
    store.details["7"]["JobId"] = "70"
    assert not store.capture_series_identity("7").verified
    store.close()


def test_verified_partial_archive_keeps_complete_prefix(tmp_path):
    store = Store(str(tmp_path)); bind(store); store.record("7", {"t": 1})
    path = Path(store._series_path("7")); store.close()
    with path.open("a") as stream: stream.write('{"t":')
    store = Store(str(tmp_path)); bind(store)
    assert store.series_of("7") == [{"t": 1}]
    assert store.series_status("7")["ignored"] == 1
    store.close()


from hypothesis import given, settings, strategies as st


@given(st.lists(st.integers(min_value=0, max_value=1000), max_size=40),
       st.lists(st.integers(min_value=0, max_value=1000), max_size=40))
@settings(max_examples=35, deadline=None)
def test_arbitrary_delayed_samples_and_requeue_merges_keep_attempt_isolation(old_times, new_times):
    import tempfile
    with tempfile.TemporaryDirectory() as path:
        store = Store(path); first = bind(store)
        for index, timestamp in enumerate(old_times):
            store.record("7", {"t": timestamp, "k": str(index), "attempt": "old"}, identity=first, _persist=False)
        second = bind(store, restart=1)
        for index, timestamp in enumerate(new_times):
            store.record("7", {"t": timestamp, "k": str(index), "attempt": "new"}, identity=second, _persist=False)
            assert store.record("7", {"t": timestamp, "attempt": "late-old"}, identity=first, _persist=False) is None
        store.flush_persistence(); store.close()
        reader = Store(path, read_only=True); bind(reader, restart=1)
        restored = reader.series_of("7")
        assert all(row["attempt"] == "new" for row in restored)
        assert sorted(row["t"] for row in restored) == sorted(new_times)
        reader.close()


@pytest.mark.parametrize("jid,fields", [("7_3", {"ArrayJobId": "7", "ArrayTaskId": "3"}), ("7+2", {"HetJobId": "7", "HetJobOffset": "2"})])
def test_explicit_component_identity_metadata_invalidates_cached_abstention(tmp_path, jid, fields):
    store = Store(str(tmp_path))
    job = Job(jid, "training", "gpu", "RUNNING", submit=SUBMIT, start=START, cluster="lab")
    store.apply_jobs([job])
    details = dict(JobId="999", SubmitTime=SUBMIT, StartTime=START, Restarts="0", Cluster="lab")
    store.details[jid] = details
    assert not store.capture_series_identity(jid).verified
    details.update(fields)
    assert store.capture_series_identity(jid).verified
    store.close()
