"""A cancelled-job rerun never reconstructs a launcher or broadens its scope."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest

from tower import operations as O
from tower.slurm import CommandError


def controller(**changes):
    value = dict(JobId="42", JobName="science run", UserId="alice(1000)",
                 JobState="CANCELLED", SubmitTime="2026-10-01T12:00:00",
                 StartTime="2026-10-01T12:10:00", EndTime="2026-10-01T12:11:00",
                 Restarts="0", BatchFlag="1", Requeue="1", Account="lab",
                 Partition="gpu", QOS="normal", NumCPUs="8", TimeLimit="01:00:00",
                 WorkDir="/project/with spaces", Command="/changed-or-removed/run.sh",
                 StdOut="/logs/run-42.out", StdErr="/logs/run-42.err",
                 Dependency="afterok:19(unfulfilled)", Reason="DependencyNeverSatisfied",
                 TresPerNode="gres/gpu:2", SiteSpecificRequest="retained setting")
    value.update(changes)
    return value


def source(job_id="42", **changes):
    value = dict(id=job_id, name="different display name", state="CANCELLED",
                 submit="2026-10-01T12:00:00", start="2026-10-01T12:10:00",
                 end="2026-10-01T12:11:00", user="alice", account="lab", cluster="")
    value.update(changes)
    return value


class RetainedBackend:
    def __init__(self):
        self.record = controller()
        self.calls = []
        self.script = "#!/bin/bash\n#SBATCH --array=0-500\npython train.py --seed \"$BATCH_SEED\" \"$@\"\n"
        self.script_hook = None
        self.requeue_error = None
        self.after = None
        self.cluster = "cluster"

    def run(self, argv, timeout=8):
        self.calls.append(tuple(argv))
        if argv[:4] == ["scontrol", "show", "job", "-o"]:
            if isinstance(self.record, Exception):
                raise self.record
            if self.record is None:
                raise CommandError("Invalid job id specified")
            return (self.record if isinstance(self.record, str) else
                    " ".join(f"{key}={value}" for key, value in self.record.items())), .001
        if argv[:3] == ["scontrol", "write", "batch_script"]:
            if self.script_hook:
                self.script_hook()
            if isinstance(self.script, Exception):
                raise self.script
            return self.script, .001
        if argv == ["scontrol", "show", "config"]:
            return "ClusterName = " + self.cluster + "\n", .001
        if argv[:2] == ["scontrol", "requeue"]:
            if self.requeue_error:
                raise self.requeue_error
            if self.after is None:
                self.record.update(JobState="PENDING", Restarts=str(int(self.record["Restarts"]) + 1),
                                   SubmitTime="2026-10-01T12:12:00", EndTime="Unknown", StartTime="Unknown")
            else:
                self.record = self.after
            return "", .001
        pytest.fail("Unexpected command, possibly approximate submission: " + repr(argv))


@pytest.fixture
def ctx(tmp_path):
    return O.Context(slurm=SimpleNamespace(b=RetainedBackend(), user="alice"),
                     selected="42", finished=(source(),), scope={"host": "cluster", "user": "alice"},
                     state_dir=str(tmp_path), cancel=threading.Event())


def review(ctx, **params):
    return O.run("rerun-job", params, ctx)


def changes(ctx):
    return [call for call in ctx.slurm.b.calls if call[:2] == ("scontrol", "requeue")]


def test_review_preserves_single_job_variable_launch_settings_and_original_paths(ctx):
    result = review(ctx)
    assert result["status"] == "review"
    plan = result["plan"]
    assert plan["params"] == {"job_id": "42"}
    assert plan["payload"]["argv"] == ["scontrol", "requeue", "42"]
    assert plan["payload"]["evidence"] == controller()
    assert result["data"]["script_sha256"] == hashlib.sha256(ctx.slurm.b.script.encode()).hexdigest()
    assert "afterok:19(unfulfilled)" in "\n".join(result["rows"])
    assert any("not rewritten" in row for row in result["rows"])
    assert any("truncate" in row for row in result["rows"])
    assert not changes(ctx)
    assert len(ctx.slurm.b.calls) == 3
    outcome = O.apply("rerun-job", plan, ctx)
    assert outcome["status"] == "ok"
    assert outcome["data"]["accepted"] and outcome["data"]["confirmed"]
    assert outcome["data"]["rerun_dispatched"] is True
    assert outcome["data"]["job_id"] == "42"
    assert changes(ctx) == [("scontrol", "requeue", "42")]
    assert ctx.slurm.b.record["Dependency"] == "afterok:19(unfulfilled)"
    assert ctx.slurm.b.record["StdOut"] == "/logs/run-42.out"


def test_exact_array_task_never_restarts_parent_or_siblings(ctx):
    ctx.slurm.b.record = controller(JobId="57", ArrayJobId="42", ArrayTaskId="7")
    ctx = replace(ctx, selected="42_7", finished=(source("42_7"),))
    plan = review(ctx)["plan"]
    outcome = O.apply("rerun-job", plan, ctx)
    assert outcome["data"]["confirmed"]
    assert changes(ctx) == [("scontrol", "requeue", "42_7")]
    assert all(call[-1] != "42" for call in ctx.slurm.b.calls)


@pytest.mark.parametrize("job_id", ["42_[0-99]", "42_1,42_2", "42+0", "42.0", "42,43", "42;id", "0", "٤٢", "--all"])
def test_bad_target_forms_never_contact_scheduler(ctx, job_id):
    ctx = replace(ctx, selected=job_id, finished=(source(job_id),))
    with pytest.raises(ValueError):
        review(ctx)
    assert not ctx.slurm.b.calls


@pytest.mark.parametrize("fields", [dict(ArrayJobId="42", ArrayTaskId="0-99"),
    dict(ArrayJobId="42", ArrayTaskId="7"), dict(HetJobId="42", HetJobOffset="0"),
    dict(HetJobIdSet="42,43"), dict(PackJobId="42"), dict(BatchFlag="0"), dict(BatchFlag=""),
    dict(Requeue="0"), dict(Requeue=""), dict(JobState="RUNNING"), dict(JobState="PENDING"),
    dict(JobState="COMPLETED"), dict(JobState="CANCELLED+COMPLETING"), dict(UserId="bob(1001)"),
    dict(SubmitTime="2026-10-01T12:02:00"), dict(StartTime="2026-10-01T12:02:00"),
    dict(EndTime="2026-10-01T12:02:00"), dict(Restarts="NaN"), dict(Restarts="-1"), dict(Account="other")])
def test_unverifiable_or_broadening_controller_record_rejected(ctx, fields):
    ctx.slurm.b.record.update(fields)
    with pytest.raises(ValueError):
        review(ctx)
    assert not changes(ctx)


@pytest.mark.parametrize("record", [None, "JobId=42 UserId=alice(1000) SubmitTime=unknown",
    "JobId=42 UserId=alice(1000) UserId=bob(1001) SubmitTime=2026-10-01T12:00:00",
    "JobId=42 SubmitTime=2026-10-01T12:00:00 UserId=alice(1000)\nJobId=42 UserId=alice(1000)",
    CommandError("controller unavailable")])
def test_purged_missing_or_malformed_record_never_falls_back_to_sacct_or_sbatch(ctx, record):
    ctx.slurm.b.record = record
    with pytest.raises(ValueError, match="verifiable retained job"):
        review(ctx)
    assert len(ctx.slurm.b.calls) == 1
    assert not changes(ctx)


@pytest.mark.parametrize("field", ["BatchFlag", "Requeue", "Restarts", "SubmitTime", "StartTime", "EndTime"])
def test_missing_required_retained_evidence_is_not_assumed(ctx, field):
    del ctx.slurm.b.record[field]
    with pytest.raises(ValueError):
        review(ctx)
    assert not changes(ctx)


@pytest.mark.parametrize("rows", [(), (source(state="FAILED"),), (source(state="RUNNING"),),
    (source(submit=""),), (source(end=""),), (source(submit="now"),),
    (source(), source(submit="2026-10-01T11:00:00"))])
def test_source_attempt_must_be_cancelled_and_unambiguous(ctx, rows):
    with pytest.raises(ValueError):
        review(replace(ctx, finished=rows))
    assert not ctx.slurm.b.calls


def test_cancelled_before_start_is_supported(ctx):
    ctx.slurm.b.record["StartTime"] = "Unknown"
    ctx = replace(ctx, finished=(source(start=""),))
    assert O.apply("rerun-job", review(ctx)["plan"], ctx)["data"]["confirmed"]


@pytest.mark.parametrize("batch_flag", ["2", "15"])
def test_previously_requeued_batch_job_retains_launch_identity(ctx, batch_flag):
    ctx.slurm.b.record.update(BatchFlag=batch_flag, Restarts="5")
    assert O.apply("rerun-job", review(ctx)["plan"], ctx)["data"]["observed_restarts"] == "6"


@pytest.mark.parametrize("state", ["CANCELLED by 1000", "CANCELLED+"])
def test_accounting_cancellation_annotation_preserves_raw_source_identity(ctx, state):
    ctx = replace(ctx, finished=(source(state=state),))
    plan = review(ctx)["plan"]
    assert plan["payload"]["source"]["state"] == state
    assert O.apply("rerun-job", plan, ctx)["data"]["confirmed"]


@pytest.mark.parametrize("stamp", ["2026-10-01", "2026-10-01 12:00:00", "2026-10-01T99:00:00", "2026-10-01T12:00"])
def test_incomplete_or_malformed_timestamp_rejected_before_io(ctx, stamp):
    with pytest.raises(ValueError, match="timestamp"):
        review(replace(ctx, finished=(source(submit=stamp),)))
    assert not ctx.slurm.b.calls


@pytest.mark.parametrize("change", [dict(Restarts="1"), dict(WorkDir="/other"), dict(StdOut="/other.log"),
    dict(Dependency="afterok:123"), dict(NumCPUs="16"), dict(TresPerNode="gres/gpu:4"),
    dict(TimeLimit="12:00:00"), dict(SiteSpecificRequest="different"), dict(Requeue="0"),
    dict(JobState="RUNNING"), dict(EndTime="2026-10-01T12:11:01")])
def test_changed_retained_launch_or_attempt_rejected_before_mutation(ctx, change):
    plan = review(ctx)["plan"]
    ctx.slurm.b.record.update(change)
    with pytest.raises(ValueError):
        O.apply("rerun-job", plan, ctx)
    assert not changes(ctx)


def test_source_replaced_after_review_cannot_retarget_newer_cancelled_attempt(ctx):
    plan = review(ctx)["plan"]
    ctx = replace(ctx, finished=(source(end="2026-10-01T12:12:00"),))
    with pytest.raises(ValueError, match="selected cancelled attempt changed"):
        O.apply("rerun-job", plan, ctx)
    assert not changes(ctx)


def test_script_change_after_review_rejected(ctx):
    plan = review(ctx)["plan"]
    ctx.slurm.b.script += "changed\n"
    with pytest.raises(ValueError, match="script changed"):
        O.apply("rerun-job", plan, ctx)
    assert not changes(ctx)


@pytest.mark.parametrize("phase", ["prepare", "apply"])
def test_job_changed_during_script_read_revalidated(ctx, phase):
    if phase == "apply":
        plan = review(ctx)["plan"]
    ctx.slurm.b.script_hook = lambda: ctx.slurm.b.record.update(NumCPUs="99")
    with pytest.raises(ValueError, match="changed"):
        if phase == "prepare":
            review(ctx)
        else:
            O.apply("rerun-job", plan, ctx)
    assert not changes(ctx)


def test_missing_script_preview_does_not_replace_stored_script_with_mutable_path(ctx):
    ctx.slurm.b.script = CommandError("script retrieval is disabled at this site")
    result = review(ctx)
    assert result["warnings"] and result["data"]["script_sha256"] == ""
    assert O.apply("rerun-job", result["plan"], ctx)["data"]["confirmed"]
    assert changes(ctx) == [("scontrol", "requeue", "42")]


def test_known_script_preview_cannot_disappear_after_review(ctx):
    plan = review(ctx)["plan"]
    ctx.slurm.b.script = CommandError("controller stopped retaining script")
    with pytest.raises(CommandError):
        O.apply("rerun-job", plan, ctx)
    assert not changes(ctx)


def test_cluster_provenance_checked_against_current_controller(ctx):
    ctx = replace(ctx, finished=(source(cluster="different"),))
    with pytest.raises(ValueError, match="cluster"):
        review(ctx)
    assert not changes(ctx)
    ctx.slurm.b.cluster = "different"
    assert review(ctx)["status"] == "review"


@pytest.mark.parametrize("after", [CommandError("readback unavailable"), controller(),
    controller(Restarts="1", JobId="99"), controller(Restarts="1", UserId="bob(1001)"),
    controller(Restarts="1", ArrayJobId="42", ArrayTaskId="9")])
def test_accepted_requeue_with_ambiguous_readback_preserves_receipt(ctx, after):
    plan = review(ctx)["plan"]
    ctx.slurm.b.after = after
    result = O.apply("rerun-job", plan, ctx)
    assert result["status"] == "partial"
    assert result["data"]["accepted"] and result["data"]["rerun_dispatched"]
    assert not result["data"]["confirmed"]
    assert result["warnings"]
    assert changes(ctx) == [("scontrol", "requeue", "42")]
    with pytest.raises(ValueError, match="already attempted"):
        O.apply("rerun-job", plan, ctx)


def test_timeout_has_durable_unknown_outcome_and_no_automatic_retry(ctx):
    plan = review(ctx)["plan"]
    ctx.slurm.b.requeue_error = CommandError("Command timed out; inspect current state")
    with pytest.raises(CommandError, match="timed out"):
        O.apply("rerun-job", plan, ctx)
    receipt = json.loads(next((Path(ctx.state_dir) / "operations-receipts").glob("*.result.json")).read_text())
    assert receipt["state"] == "error" and "partially completed" in receipt["reconciliation"]
    with pytest.raises(ValueError, match="already attempted"):
        O.apply("rerun-job", plan, ctx)
    assert changes(ctx) == [("scontrol", "requeue", "42")]


def test_concurrent_duplicate_apply_dispatches_only_once(ctx):
    plan = review(ctx)["plan"]
    def apply(_):
        try:
            return O.apply("rerun-job", deepcopy(plan), ctx)
        except ValueError as exc:
            return exc
    with ThreadPoolExecutor(max_workers=4) as workers:
        results = list(workers.map(apply, range(4)))
    assert sum(isinstance(result, dict) for result in results) == 1
    assert changes(ctx) == [("scontrol", "requeue", "42")]


@pytest.mark.parametrize("kind", ["cancel", "replay", "connection", "tamper", "expiry"])
def test_invalid_review_never_dispatches(ctx, kind, monkeypatch):
    plan = review(ctx)["plan"]
    if kind == "cancel":
        ctx.cancel.set()
    elif kind == "replay":
        ctx = replace(ctx, replay=True)
    elif kind == "connection":
        ctx = replace(ctx, scope={"host": "other"})
    elif kind == "tamper":
        plan["payload"]["job_id"] = "43"
    else:
        monkeypatch.setattr(O.time, "time", lambda: plan["created"] + O.PLAN_LIFETIME + 2)
    with pytest.raises(ValueError):
        O.apply("rerun-job", plan, ctx)
    assert not changes(ctx)


def test_script_preview_is_bounded_for_large_original_script(ctx):
    ctx.slurm.b.script = "#!/bin/bash\n" + ("x" * 2000 + "\n") * 200
    result = review(ctx)
    assert len(result["data"]["script_preview"]) <= 40
    assert all(len(line) <= 512 for line in result["data"]["script_preview"])
    assert len(json.dumps(result)) < 20000
