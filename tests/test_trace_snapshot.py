"""Owned job membership and stable process identity gate real snapshot journals."""
from dataclasses import replace
import json
import os
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest

from tower import operations as O, trace_snapshot as S, trace_protocol as T


@pytest.fixture
def sample(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    binary = tmp_path / "py-spy"
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o700)
    monkeypatch.setattr(S.shutil, "which", lambda name: str(binary) if name == "py-spy" else None)
    monkeypatch.setattr(S, "_local_user", lambda: ("alice", 1000))
    process = dict(start="12345", uid=1000, device=1, inode=999)
    monkeypatch.setattr(S, "_process", lambda pid, uid: dict(process))
    job = dict(id="42", user="alice", state="RUNNING", submit="2026-10-10T10:00:00", start="2026-10-10T10:01:00")
    context = O.Context(slurm=SimpleNamespace(user="alice", b=SimpleNamespace()), jobs=(job,),
                        selected="42", scope={"host": "local", "user": "alice", "cluster": "test"},
                        cancel=threading.Event())
    trace = [dict(pid=43210, thread_id=100, thread_name="MainThread", os_thread_id=43210,
                  active=True, owns_gil=True, frames=[
                      dict(name="inner", filename="/project/app.py", line=20, locals=[{"repr": "DO NOT RECORD THIS"}]),
                      dict(name="outer", filename="/project/app.py", line=10)])]
    state = SimpleNamespace(project=project, binary=binary, process=process, ctx=context, calls=[], trace=trace,
                            raw=None, membership="PID JOBID STEPID LOCALID GLOBALID\n43210 42 0 0 0\n",
                            detail="JobId=42 JobState=RUNNING UserId=alice(1000) SubmitTime=2026-10-10T10:00:00 StartTime=2026-10-10T10:01:00 Restarts=0",
                            during=None)

    def command(ctx, argv, timeout=8, limit=MAX_OUTPUT):
        O.checkpoint(ctx)
        state.calls.append((argv, timeout, limit))
        if argv[:3] == ["scontrol", "show", "job"]:
            return state.detail
        if argv[:2] == ["scontrol", "listpids"]:
            return state.membership
        if argv[0] == str(binary):
            if state.during:
                state.during()
            return state.raw if state.raw is not None else json.dumps(state.trace)
        raise AssertionError(argv)

    monkeypatch.setattr(S.O, "command", command)
    state.params = dict(job_id="42", pid="43210", project=str(project))
    return state


MAX_OUTPUT = 1 << 20


def capture(sample):
    return S.apply(S.prepare(sample.params, sample.ctx)["plan"], sample.ctx)


def test_review_never_runs_pyspy_or_writes_snapshot(sample):
    result = S.prepare(sample.params, sample.ctx)
    assert result["plan"]["feature"] == "trace-snapshot"
    assert result["data"]["attempt"] == "2026-10-10T10:00:00/2026-10-10T10:01:00"
    assert all(argv[0] == "scontrol" for argv, _, _ in sample.calls)
    assert not (sample.project / ".tower").exists()


def test_capture_uses_nonblocking_only_and_saves_real_checked_snapshot(sample):
    result = capture(sample)
    snapshots = [call for call in sample.calls if call[0][0] == str(sample.binary)]
    assert snapshots == [([str(sample.binary), "dump", "--pid", "43210", "--json", "--nonblocking"], 5, 1 << 20)]
    evidence = T.read_trace(result["data"]["path"])
    assert evidence["status"] == "PARTIAL"
    assert evidence["complete"] is False
    assert evidence["identity"]["pid"] == 43210
    assert evidence["identity"]["process_start"] == "12345"
    assert evidence["identity"]["slurm_start"] == sample.ctx.jobs[0]["start"]
    assert [frame["function"] for frame in evidence["stacks"][0]["frames"]] == ["outer", "inner"]
    manifest = T.read_json(result["data"]["path"])
    assert manifest["state"] == "CLOSED"
    assert manifest["coverage"]["mode"] == "snapshot"
    assert manifest["coverage"]["complete"] is False
    assert "DO NOT RECORD THIS" not in Path(result["data"]["path"]).with_name("events.jsonl").read_text()


def test_exact_resolved_array_task_allowed(sample):
    sample.ctx = replace(sample.ctx, jobs=(dict(sample.ctx.jobs[0], id="40_3"),), selected="40_3")
    sample.params["job_id"] = "40_3"
    sample.detail = sample.detail.replace("JobId=42", "JobId=81 ArrayJobId=40 ArrayTaskId=3")
    sample.membership = sample.membership.replace("43210 42", "43210 81")
    assert capture(sample)["data"]["identity"]["job_id"] == "40_3"


def test_aggregate_array_parent_is_not_a_process_identity(sample):
    sample.detail = sample.detail.replace("JobId=42", "JobId=42 ArrayJobId=42 ArrayTaskId=1-9")
    with pytest.raises(ValueError, match="single numeric task"):
        S.prepare(sample.params, sample.ctx)


@pytest.mark.parametrize("key,value", [("job_id", "42_[1-9]"), ("job_id", "42.0"), ("job_id", "42;id"),
                                      ("job_id", "42+1"), ("pid", "0"), ("pid", "-3"), ("pid", "٤٣٢١٠"),
                                      ("pid", "1;id"), ("pid", "2147483648"), ("project", "bad\npath")])
def test_ambiguous_and_malicious_parameters_fail_without_io(sample, key, value):
    sample.params[key] = value
    with pytest.raises(ValueError):
        S.prepare(sample.params, sample.ctx)
    assert sample.calls == []


def test_missing_tool_is_clear(sample, monkeypatch):
    monkeypatch.setattr(S.shutil, "which", lambda _: None)
    with pytest.raises(ValueError, match="py-spy is not installed"):
        S.prepare(sample.params, sample.ctx)
    assert sample.calls == []


@pytest.mark.parametrize("change", [{"user": "bob"}, {"state": "COMPLETED"}, {"start": "different"}, {"submit": "different"}])
def test_context_ownership_state_and_attempt_must_match(sample, change):
    sample.ctx = replace(sample.ctx, jobs=(dict(sample.ctx.jobs[0], **change),))
    with pytest.raises(ValueError):
        S.prepare(sample.params, sample.ctx)


@pytest.mark.parametrize("before,after", [("UserId=alice(1000)", "UserId=bob(1001)"),
                                         ("UserId=alice(1000)", "UserId=alice(2000)"),
                                         ("JobId=42", "JobId=43"), ("JobState=RUNNING", "JobState=SUSPENDED"),
                                         ("StartTime=2026-10-10T10:01:00", "StartTime=Unknown")])
def test_live_scheduler_identity_is_authoritative(sample, before, after):
    sample.detail = sample.detail.replace(before, after)
    with pytest.raises(ValueError):
        S.prepare(sample.params, sample.ctx)


@pytest.mark.parametrize("membership", ["", "43210 42", "PID PID JOBID\n43210 43210 42", "PID JOBID\n43210 43",
                                       "PID JOBID\n43210 42\n43210 42", "PID JOBID\n43211 42",
                                       "PID JOBID\n43210", "PID JOBID\n43210 42 0"])
def test_same_uid_is_not_job_membership(sample, membership):
    sample.membership = membership
    with pytest.raises(ValueError):
        S.prepare(sample.params, sample.ctx)
    assert all(call[0][0] == "scontrol" for call in sample.calls)


def test_header_order_is_parsed_not_assumed(sample):
    sample.membership = "JOBID GLOBALID PID STEPID LOCALID\n42 0 43210 batch 0\n"
    assert capture(sample)["data"]["threads"] == 1


def test_pid_reuse_between_review_and_capture_never_attaches(sample):
    plan = S.prepare(sample.params, sample.ctx)["plan"]
    sample.process["start"] = "different"
    with pytest.raises(ValueError, match="reused"):
        S.apply(plan, sample.ctx)
    assert not any(argv[0] == str(sample.binary) for argv, _, _ in sample.calls)


def test_process_or_membership_change_during_capture_discards_data(sample):
    sample.during = lambda: sample.process.update(start="changed")
    with pytest.raises(ValueError, match="discarded"):
        capture(sample)
    assert not (sample.project / ".tower").exists()


def test_membership_disappearing_during_capture_discards_data(sample):
    sample.during = lambda: setattr(sample, "membership", "PID JOBID\n")
    with pytest.raises(ValueError, match="not uniquely verified"):
        capture(sample)
    assert not (sample.project / ".tower").exists()


def test_binary_replacement_invalidates_review(sample):
    plan = S.prepare(sample.params, sample.ctx)["plan"]
    sample.binary.write_text("different executable")
    with pytest.raises(ValueError, match="executable changed"):
        S.apply(plan, sample.ctx)


def test_remote_replay_cancellation_and_scope_changes_never_attach(sample):
    with pytest.raises(ValueError, match="target host"):
        S.prepare(sample.params, replace(sample.ctx, files=SimpleNamespace(remote=True)))
    with pytest.raises(ValueError, match="recorded session"):
        S.prepare(sample.params, replace(sample.ctx, replay=True))
    plan = S.prepare(sample.params, sample.ctx)["plan"]
    with pytest.raises(ValueError, match="connection changed"):
        S.apply(plan, replace(sample.ctx, scope={"host": "other"}))
    sample.ctx.cancel.set()
    with pytest.raises(ValueError, match="cancelled"):
        S.apply(plan, sample.ctx)
    assert not any(argv[0] == str(sample.binary) for argv, _, _ in sample.calls)


def test_wrong_explicit_attempt_and_changed_live_attempt_fail(sample):
    with pytest.raises(ValueError, match="verified submit/start"):
        S.prepare(dict(sample.params, attempt="another-attempt"), sample.ctx)
    plan = S.prepare(sample.params, sample.ctx)["plan"]
    sample.detail = sample.detail.replace("Restarts=0", "Restarts=1")
    with pytest.raises(ValueError, match="attempt changed"):
        S.apply(plan, sample.ctx)


@pytest.mark.parametrize("raw", ["not JSON", "{}", "[]", "[null]", '[{"pid":43210,"pid":43210}]',
                                "[NaN]", "x" * ((1 << 20) + 1)])
def test_bad_tool_output_never_creates_a_journal(sample, raw):
    sample.raw = raw
    with pytest.raises(ValueError):
        capture(sample)
    assert not (sample.project / ".tower").exists()


@pytest.mark.parametrize("field,value", [("pid", 43211), ("pid", True), ("thread_id", 0), ("thread_id", True),
                                        ("thread_id", 1 << 64), ("frames", {}), ("frames", [None]), ("frames", [])])
def test_invalid_thread_structures_fail_before_publish(sample, field, value):
    sample.trace[0][field] = value
    with pytest.raises(ValueError):
        capture(sample)
    assert not (sample.project / ".tower").exists()


@pytest.mark.parametrize("field,value", [("name", {}), ("filename", 5), ("line", True), ("line", 1 << 32)])
def test_invalid_frame_structures_fail(sample, field, value):
    sample.trace[0]["frames"][0][field] = value
    with pytest.raises(ValueError, match="frame structure"):
        capture(sample)


def test_duplicate_thread_and_frame_budget_fail(sample):
    sample.trace.append(dict(sample.trace[0]))
    with pytest.raises(ValueError, match="duplicated thread"):
        capture(sample)
    sample.trace.pop()
    sample.trace[0]["frames"] *= 129
    with pytest.raises(ValueError, match="256 frames"):
        capture(sample)


def test_terminal_controls_are_sanitized_and_frame_order_preserved(sample):
    sample.trace[0]["frames"][0]["name"] = "function\x1b[31m\nname"
    result = capture(sample)
    assert "\x1b" not in "\n".join(result["rows"])
    assert "\n" not in T.read_trace(result["data"]["path"])["stacks"][0]["frames"][-1]["function"]


def test_actual_proc_identity_is_bounded_and_owner_checked():
    value = S._process(os.getpid(), os.getuid())
    assert value["start"].isdigit()
    assert value["uid"] == os.getuid()
    with pytest.raises(ValueError, match="not owned"):
        S._process(os.getpid(), os.getuid() + 1)
    with pytest.raises(ValueError, match="exited"):
        S._process(2147483647, os.getuid())
