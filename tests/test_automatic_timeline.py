"""Real log/journal joins and scheduler-identity changes in the timeline."""
from dataclasses import replace
from types import SimpleNamespace
import threading

import pytest

from raven import operations as O, ops_workspace as W
from raven.remote import LocalFiles
from raven.trace_recorder import Recorder


SUBMIT = "2026-10-10T10:00:00"
START = "2026-10-10T10:01:00"


@pytest.fixture
def context(tmp_path):
    out, err = tmp_path / "out.log", tmp_path / "err.log"
    out.write_text("[2026-10-10T10:01:01Z] epoch evidence\nno timestamp here\n")
    err.write_text("2026-10-10T10:01:02 local clock evidence\n")
    class Scheduler:
        calls = []
        detail = {"JobId": "81", "UserId": "researcher(1000)", "SubmitTime": SUBMIT,
                  "StartTime": START, "EndTime": "2026-10-11T10:00:00", "JobState": "RUNNING",
                  "Cluster": "lab", "WorkDir": str(tmp_path), "StdOut": str(out), "StdErr": str(err)}
        def run(self, argv, timeout):
            self.calls.append(argv)
            assert argv[:3] == ["scontrol", "show", "job"]
            return " ".join(key + "=" + value for key, value in self.detail.items()), .001
    return O.Context(slurm=SimpleNamespace(b=Scheduler(), user="researcher"), files=LocalFiles(),
        jobs=({"id": "81", "user": "researcher", "submit": SUBMIT, "start": START, "state": "RUNNING", "cluster": "lab"},),
        selected="81", scope={"cluster": "lab", "user": "researcher"}, cancel=threading.Event())


def test_blank_manifest_joins_actual_scheduler_logs_and_exact_trace(context, tmp_path):
    with Recorder(tmp_path, job_id="81", attempt=SUBMIT + "/" + START, cluster="lab") as recorder:
        recorder.emit("annotation", {"phase": "solver"})
        recorder.flush()
        report = O.run("investigation-timeline", {}, context)
    result = report["data"]
    assert result["collection"] == "automatic"
    assert {event["kind"] for event in result["events"]} >= {"submitted", "started", "state-observation", "log", "annotation"}
    assert all(event["cluster"] == "lab" and event["attempt"] == SUBMIT + "/" + START for event in result["events"])
    assert not any(event["kind"] == "ended" for event in result["events"]), "Running EndTime is a forecast"
    timed = next(event for event in result["events"] if "epoch evidence" in event["message"])
    unknown = next(event for event in result["events"] if "no timestamp" in event["message"])
    local = next(event for event in result["events"] if "local clock" in event["message"])
    assert timed["clock"] == "unix" and timed["timestamp"] > 0
    assert unknown["timestamp"] is None and unknown["ordinal"] == 2
    assert local["timestamp"] is None and local["timestamp_text"] == "2026-10-10T10:01:02"
    assert len(context.slurm.b.calls) == 1


@pytest.mark.parametrize("field,value", [("SubmitTime", "later"), ("StartTime", "later"), ("Cluster", "different"), ("UserId", "other(1001)")])
def test_changed_scheduler_identity_refuses_all_file_reads(context, monkeypatch, field, value):
    context.slurm.b.detail[field] = value
    def forbidden(*args, **kwargs):
        raise AssertionError("Read another job attempt's file")
    monkeypatch.setattr(W, "_tail", forbidden)
    with pytest.raises(ValueError, match="changed"):
        W.automatic_timeline({}, context)


def test_automatic_trace_never_joins_another_cluster_or_attempt(context, tmp_path):
    for cluster, attempt in (("other", SUBMIT + "/" + START), ("lab", "old-attempt")):
        with Recorder(tmp_path, job_id="81", attempt=attempt, cluster=cluster) as recorder:
            recorder.emit("annotation", {"private": "wrong-source"})
    result = W.automatic_timeline({}, context)
    assert not any("wrong-source" in event["message"] for event in result["events"])


def test_local_logs_refuse_symlink_retargeting_and_continue_other_sources(context, tmp_path):
    out = tmp_path / "out.log"
    out.unlink()
    out.symlink_to(tmp_path / "err.log")
    result = W.automatic_timeline({}, context)
    assert any("StdOut" in warning and "symlink" in warning for warning in result["warnings"])
    assert any("local clock" in event["message"] for event in result["events"])


def test_large_log_tail_is_bounded_and_reported_as_window_ordinals(context, tmp_path):
    (tmp_path / "out.log").write_text("a" * 70000 + "\n" + "tail record\n" * 300)
    result = W.automatic_timeline({}, context)
    records = [event for event in result["events"] if event["source"].startswith("stdout:")]
    assert len(records) == 256 and all(len(event["message"]) < 20 for event in records)
    assert any("bounded tail" in warning for warning in result["warnings"])
    source = next(source for source in result["sources"] if source["source"].startswith("stdout:"))
    assert source["byte_offset"] > 0 and "relative" in source["scope"]


def test_pending_expected_start_is_not_a_started_transition(context):
    context.slurm.b.detail.update(JobState="PENDING", StartTime="2026-10-20T10:00:00")
    pending = replace(context, jobs=({**context.jobs[0], "state": "PENDING", "start": "Unknown"},))
    result = W.automatic_timeline({}, pending)
    assert result["identity"]["attempt"] == SUBMIT
    assert not {"started", "ended"} & {event["kind"] for event in result["events"]}


def test_supplied_timeline_refuses_same_job_from_multiple_clusters():
    event = {"id": "1", "source": "scheduler", "job_id": "81", "attempt": "same", "kind": "state",
             "message": "RUNNING", "clock": "unix", "timestamp": 1, "cluster": "one"}
    value = {"schema": "raven.timeline/v1", "events": [event, dict(event, cluster="two")]}
    with pytest.raises(ValueError, match="multiple clusters"):
        W.timeline(value, "81")
    with pytest.raises(ValueError, match="does not match"):
        W.timeline({"schema": "raven.timeline/v1", "events": [event]}, "81", "different")


def test_missing_timestamps_need_source_order():
    event = {"id": "1", "source": "log", "job_id": "81", "attempt": "a", "kind": "line",
             "message": "raw", "clock": "source-order", "timestamp": None, "cluster": "lab"}
    with pytest.raises(ValueError, match="ordinal"):
        W.timeline({"schema": "raven.timeline/v1", "events": [event]})
    event["ordinal"] = 1
    assert W.timeline({"schema": "raven.timeline/v1", "events": [event]})["events"][0]["timestamp"] is None
    event["timestamp"] = 10 ** 1000
    with pytest.raises(ValueError, match="finite"):
        W.timeline({"schema": "raven.timeline/v1", "events": [event]})


def test_cancellation_before_discovery_never_collects_files(context, monkeypatch):
    context.cancel.set()
    with pytest.raises(ValueError, match="cancel"):
        O.run("investigation-timeline", {}, context)
    assert not context.slurm.b.calls


@pytest.mark.parametrize("ordinal", [True, False, "1", None, 0, -1, 1000000001])
def test_numeric_timestamp_does_not_allow_invalid_sort_ordinals(ordinal):
    event = {"id": "1", "source": "log", "job_id": "81", "attempt": "a", "kind": "line",
             "message": "raw", "clock": "unix", "timestamp": 1, "cluster": "lab", "ordinal": ordinal}
    with pytest.raises(ValueError, match="ordinal"):
        W.timeline({"schema": "raven.timeline/v1", "events": [event]})
