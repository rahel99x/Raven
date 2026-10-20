"""Cluster operations preserve exact job scope across review and execution."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import datetime as dt
import os
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest

from raven import operations as O, ops_cluster as C
from raven.remote import LocalFiles
from raven.slurm import Backend, CommandError


def job(**changes):
    record = dict(JobId="42", JobName="science run", UserId="alice(1000)",
                  JobState="PENDING", SubmitTime="2026-10-01T12:00:00",
                  StartTime="2026-10-01T12:10:00", Restarts="0", TimeLimit="01:00:00",
                  Partition="cpu", Account="lab", QOS="normal", Nice="0",
                  Dependency="(null)", EligibleTime="2026-10-01T12:00:00")
    record.update(changes)
    return record


def wire(record):
    return " ".join(f"{key}={value}" for key, value in record.items())


class BackendFixture:
    def __init__(self):
        self.jobs = {"42": job()}
        self.calls = []
        self.replies = {}
        self.script = "#!/bin/bash\n#SBATCH --time=01:00:00\nprintf '%s\\n' hello\n"
        self.steps = "StepId=42.0 State=RUNNING\nStepId=42.1 State=RUNNING\nStepId=42.batch State=RUNNING\nStepId=42.extern State=RUNNING\n"
        self.history = "42|alice(1000)|2026-10-01T12:00:00|2026-10-01T12:10:00|COMPLETED|cluster\n"
        self.before_update = None

    def run(self, argv, timeout=8):
        self.calls.append(tuple(argv))
        if tuple(argv) in self.replies:
            value = self.replies[tuple(argv)]
            if isinstance(value, Exception):
                raise value
            return value, .001
        if argv[:4] == ["scontrol", "show", "job", "-o"]:
            if "," in argv[4]:
                raise CommandError("Invalid job id specified")
            value = self.jobs.get(argv[4])
            if value is None and "_" in argv[4]:
                parent, task = argv[4].split("_", 1)
                if parent in self.jobs and isinstance(self.jobs[parent], dict) and self.jobs[parent].get("ArrayJobId") == parent:
                    value = dict(self.jobs[parent], ArrayTaskId=task)
            if isinstance(value, Exception):
                raise value
            if value is None:
                raise CommandError("Invalid job id specified")
            return (value if isinstance(value, str) else wire(value)), .001
        if argv[:2] == ["scontrol", "update"]:
            if self.before_update:
                self.before_update(argv)
            jid = argv[2].split("=", 1)[1]
            field, value = argv[3].split("=", 1)
            self.jobs[jid][field] = value
            for sibling in self.jobs.values():
                if isinstance(sibling, dict) and sibling.get("ArrayJobId") == jid:
                    sibling[field] = value
            return "", .001
        if argv[:2] == ["squeue", "--noheader"]:
            parent = next((value.partition("=")[2] for value in argv if value.startswith("--jobs=")), "")
            records = [row for row in self.jobs.values() if isinstance(row, dict) and row.get("ArrayJobId") == parent]
            if "--format=%i|%T" in argv:
                from raven.arrays import parse_range
                result = []
                for record in records:
                    for part in parse_range(record["ArrayTaskId"]):
                        for task in range(part["start"], part["end"] + 1, part["step"]):
                            result.append(f"{parent}_{task}|{record['JobState']}")
                return "\n".join(result), .001
            return "\n".join(parent + "_" + (record["ArrayTaskId"] if record["ArrayTaskId"].isdigit() else "[" + record["ArrayTaskId"] + "]") for record in records), .001
        if argv[:3] == ["scontrol", "write", "batch_script"] or argv[0] == "sacct" and "--batch-script" in argv:
            return self.script, .001
        if argv[0] == "sacct":
            return self.history, .001
        if argv[:4] == ["scontrol", "show", "step", "-o"]:
            return self.steps, .001
        raise CommandError("unconfigured test command: " + " ".join(argv))


@pytest.fixture
def ctx(tmp_path):
    return O.Context(slurm=SimpleNamespace(b=BackendFixture(), user="alice"),
                     files=LocalFiles(), selected="42", scope={"host": "cluster", "user": "alice"},
                     state_dir=str(tmp_path), cancel=threading.Event())


def prepare(ctx, feature="pending-edit", **params):
    defaults = {"pending-edit": dict(field="TimeLimit", value="02:00:00"),
                "array-throttle": dict(limit="0"), "allocation-shell": dict(mode="shell", shell="/bin/bash")}
    return C.run(feature, {**defaults.get(feature, {}), **params}, ctx)


def mutations(ctx):
    return [call for call in ctx.slurm.b.calls if call[:2] == ("scontrol", "update")]


@pytest.mark.parametrize("feature", sorted(C.FEATURES))
def test_catalog_has_terminal_fields_and_documented_proposal(feature):
    spec = next(spec for spec in C.SPECIFICATIONS if spec["key"] == feature)
    assert spec["proposal"]
    assert spec["title"] and spec["summary"]
    assert len({field["key"] for field in spec["fields"]}) == len(spec["fields"])


@pytest.mark.parametrize("value", ["--all", "42;id", "42\n43", "42.0", "42,43", "42_[9-1]", "-1", "٤٢", "4" * 21])
def test_job_id_rejected_before_io(ctx, value):
    with pytest.raises(ValueError):
        prepare(ctx, job_id=value)
    assert ctx.slurm.b.calls == []


@pytest.mark.parametrize("text", ["", "JobId=1 UserId=alice SubmitTime=now", "JobId=42 JobId=42 UserId=alice SubmitTime=now", "JobId=42 SubmitTime=none UserId=alice", "JobId=42 SubmitTime=now", "No such job"])
def test_missing_mismatched_or_ambiguous_identity_rejected(ctx, text):
    ctx.slurm.b.jobs["42"] = text
    with pytest.raises(ValueError):
        prepare(ctx)
    assert not mutations(ctx)


def test_record_parser_keeps_spaces_and_wrapped_fields():
    records = C.parse_records("JobId=42 JobName=my experiment\n UserId=alice SubmitTime=now\nJobId=43 JobName=next", "JobId")
    assert records[0]["JobName"] == "my experiment"
    assert records[0]["UserId"] == "alice"
    assert records[1]["JobId"] == "43"


@pytest.mark.parametrize("text", ["JobId=42 UserId=alice UserId=bob", "x" * (1 << 20 | 1), "JobId=42\x00", None])
def test_record_parser_rejects_invalid_evidence(text):
    with pytest.raises(ValueError):
        C.parse_records(text, "JobId")


@pytest.mark.parametrize("field,value", [("TimeLimit", "1-02:30:00"), ("Partition", "cpu,gpu"), ("QOS", "normal"), ("Account", "lab_123"), ("Nice", "100"), ("Dependency", "afterok:51:52"), ("BeginTime", "now+1hour")])
def test_pending_preparation_readonly_apply_one_exact_field(ctx, field, value):
    review = prepare(ctx, field=field, value=value)
    assert review["status"] == "review"
    assert not mutations(ctx)
    result = C.apply("pending-edit", review["plan"], ctx)
    assert result["status"] == "ok"
    assert mutations(ctx) == [("scontrol", "update", "JobId=42", f"{field}={value}")]


@pytest.mark.parametrize("field,value", [("Command", "evil"), ("TimeLimit", "1;id"), ("TimeLimit", "NaN"), ("Account", "a b"), ("QOS", "--help"), ("Dependency", "afterok:4\n5"), ("Nice", "1.5"), ("BeginTime", "now;id"), ("TimeLimit", "")])
def test_pending_fields_are_allowlisted(ctx, field, value):
    with pytest.raises(ValueError):
        prepare(ctx, field=field, value=value)
    assert not ctx.slurm.b.calls


@pytest.mark.parametrize("changes", [dict(JobState="RUNNING"), dict(JobState="COMPLETED"), dict(UserId="bob(1001)"), dict(SubmitTime="2026-10-02T12:00:00"), dict(Restarts="1"), dict(TimeLimit="03:00:00")])
def test_pending_update_rechecks_live_state_identity_and_original_value(ctx, changes):
    review = prepare(ctx)
    ctx.slurm.b.jobs["42"].update(changes)
    with pytest.raises(ValueError):
        C.apply("pending-edit", review["plan"], ctx)
    assert not mutations(ctx)


def test_completed_replay_cancelled_wrong_scope_and_modified_plans_cannot_apply(ctx):
    review = prepare(ctx)
    for changed_ctx in (replace(ctx, replay=True), replace(ctx, scope={"host": "other"})):
        with pytest.raises(ValueError):
            C.apply("pending-edit", review["plan"], changed_ctx)
    tampered = deepcopy(review["plan"])
    tampered["payload"]["argv"].append("Account=other")
    with pytest.raises(ValueError):
        C.apply("pending-edit", tampered, ctx)
    ctx.cancel.set()
    with pytest.raises(ValueError):
        C.apply("pending-edit", review["plan"], ctx)
    assert not mutations(ctx)


def test_changed_original_value_prevents_repeat_mutation(ctx):
    review = prepare(ctx)
    C.apply("pending-edit", review["plan"], ctx)
    with pytest.raises(ValueError, match="field changed"):
        C.apply("pending-edit", review["plan"], ctx)
    assert len(mutations(ctx)) == 1


def test_scheduler_readback_normalizes_minutes_to_hms(ctx):
    review = prepare(ctx, value="90")
    # Return normal scheduler text after the update, without obscuring the
    # original pre-action recheck.
    original = ctx.slurm.b.run
    def run(argv, timeout=8):
        result = original(argv, timeout)
        if argv[:2] == ["scontrol", "update"]:
            ctx.slurm.b.jobs["42"]["TimeLimit"] = "01:30:00"
        return result
    ctx.slurm.b.run = run
    result = C.apply("pending-edit", review["plan"], ctx)
    assert result["data"]["confirmed"] is True
    assert result["data"]["observed"] == "01:30:00"


@pytest.mark.parametrize("after", [CommandError("controller temporarily unavailable"), job(SubmitTime="new attempt"), job(TimeLimit="04:00:00")])
def test_accepted_update_is_not_claimed_confirmed_without_readback(ctx, after):
    review = prepare(ctx)
    original = ctx.slurm.b.run
    def run(argv, timeout=8):
        result = original(argv, timeout)
        if argv[:2] == ["scontrol", "update"]:
            ctx.slurm.b.jobs["42"] = after
        return result
    ctx.slurm.b.run = run
    result = C.apply("pending-edit", review["plan"], ctx)
    assert result["status"] == "partial"
    assert result["data"]["accepted"] is True
    assert result["data"]["confirmed"] is False
    assert len(mutations(ctx)) == 1


@pytest.mark.parametrize("field,expected,observed,wanted", [("TimeLimit", "90", "01:30:00", True), ("TimeLimit", "UNLIMITED", "INFINITE", True), ("TimeLimit", "60", "Unknown", False), ("Dependency", "0", "(null)", True), ("Nice", "10", "9", False)])
def test_scheduler_field_normalization(field, expected, observed, wanted):
    assert C._same_value(field, expected, observed) is wanted


def array(ctx, throttle="4"):
    ctx.slurm.b.jobs["42"] = job(ArrayJobId="42", ArrayTaskId="0-99", ArrayTaskThrottle=throttle)
    ctx.slurm.b.jobs["42_7"] = job(JobId="49", ArrayJobId="42", ArrayTaskId="7", JobState="RUNNING")


@pytest.mark.parametrize("selected", ["42", "42_7", "42_[0-99]", "42_[0-9999999:2%4]"])
@pytest.mark.parametrize("limit", ["0", "1", "1000000"])
def test_array_throttle_targets_parent_and_preserves_zero(ctx, selected, limit):
    array(ctx)
    review = prepare(ctx, "array-throttle", job_id=selected, limit=limit)
    assert not mutations(ctx)
    C.apply("array-throttle", review["plan"], ctx)
    assert mutations(ctx) == [("scontrol", "update", "JobId=42", f"ArrayTaskThrottle={limit}")]


@pytest.mark.parametrize("limit", ["-1", "1.0", "NaN", "2147483648", "1;id", "١"])
def test_array_throttle_invalid_values_never_query(ctx, limit):
    with pytest.raises(ValueError):
        prepare(ctx, "array-throttle", limit=limit)
    assert not ctx.slurm.b.calls


@pytest.mark.parametrize("changes", [dict(ArrayTaskThrottle="6"), dict(SubmitTime="later"), dict(JobState="COMPLETED"), dict(UserId="bob"), dict(Restarts="1")])
def test_array_throttle_rechecks_parent_before_apply(ctx, changes):
    array(ctx)
    review = prepare(ctx, "array-throttle")
    ctx.slurm.b.jobs["42"].update(changes)
    with pytest.raises(ValueError):
        C.apply("array-throttle", review["plan"], ctx)
    assert not mutations(ctx)


def test_array_remaining_ranges_are_not_attempt_identity(ctx):
    array(ctx)
    review = prepare(ctx, "array-throttle")
    ctx.slurm.b.jobs["42"]["ArrayTaskId"] = "60-99"
    C.apply("array-throttle", review["plan"], ctx)
    assert mutations(ctx)


def test_array_response_can_include_sibling_records(ctx):
    array(ctx)
    ctx.slurm.b.jobs["42"] = wire(ctx.slurm.b.jobs["42"]) + "\n" + wire(ctx.slurm.b.jobs["42_7"])
    review = prepare(ctx, "array-throttle")
    assert review["plan"]["payload"]["job_id"] == "42"


def test_not_array_cannot_set_throttle(ctx):
    with pytest.raises(ValueError, match="not a verified"):
        prepare(ctx, "array-throttle")


@pytest.mark.parametrize("task,wanted", [("0-9%2", 2), ("0-9%0", 0), ("0-9", None)])
def test_old_slurm_throttle_fallback(task, wanted):
    assert C._throttle_value({"ArrayTaskId": task}) == wanted


@pytest.mark.parametrize("lines", ["Filesystem 1024-blocks Used Available Capacity Mounted on\n/dev/sda 1000 100 900 10% /home\n", "Filesystem 1024-blocks Used Available Capacity Mounted on\nvery-long-filesystem\n 1000 100 900 10% /path with spaces\n"])
def test_df_posix_and_wrapped_names(lines):
    record = C.parse_df(lines)
    assert record["free"] == 900
    assert record["total"] == 1000


@pytest.mark.parametrize("text", ["df: permission denied", "Filesystem Inodes IUsed IFree IUse% Mounted on\nx - - - - /", "Header\na 100 10 90 10% /\nb 100 10 90 10% /b", "Header\na 100 10 -1 10% /", None])
def test_df_unknown_and_ambiguous_not_success(text):
    with pytest.raises(ValueError):
        C.parse_df(text)


def test_quota_grace_stars_and_no_limits():
    text = "Filesystem blocks quota limit grace files quota limit grace\n/dev/a 1000* 900 1200 6days 10 0 0\n/dev/b\n 2000 1000 1500 expired 200 100 200 expired\n"
    rows = C.parse_quota(text)
    assert len(rows) == 2
    assert rows[0]["soft_limit_reached"] and not rows[0]["hard_limit_reached"]
    assert rows[1]["hard_limit_reached"]
    assert C.parse_quota("No quotas enabled") == []


def storage(ctx, free="9000000", inodes="10000", quota=None):
    ctx.slurm.b.replies[("df", "-Pk", "--", "/project")] = f"Filesystem 1024-blocks Used Available Capacity Mounted on\n/dev/a 10000000 1000000 {free} 10% /project\n"
    ctx.slurm.b.replies[("df", "-Pi", "--", "/project")] = f"Filesystem Inodes IUsed IFree IUse% Mounted on\n/dev/a 20000 10000 {inodes} 50% /project\n"
    ctx.slurm.b.replies[("quota", "-w", "-u", "alice")] = quota if quota is not None else "Filesystem blocks quota limit grace files quota limit grace\n/dev/a 100 1000 2000 10 1000 2000\n"


def test_storage_reads_space_inodes_and_matching_quota(ctx):
    storage(ctx)
    result = C.run("storage", {"path": "/project"}, ctx)
    assert result["status"] == "ok"
    assert result["data"]["quota_for_path"][0]["filesystem"] == "/dev/a"
    assert all(call[0] in {"df", "quota"} for call in ctx.slurm.b.calls)


@pytest.mark.parametrize("free,inodes", [("1", "10000"), ("9000000", "0")])
def test_storage_threshold_failure(ctx, free, inodes):
    storage(ctx, free, inodes)
    assert C.run("storage", {"path": "/project"}, ctx)["status"] == "blocked"


@pytest.mark.parametrize("quota", ["No quotas", CommandError("quota: not found"), "Filesystem blocks quota limit files quota limit\n/dev/other 100 0 0 1 0 0\n"])
def test_storage_never_assumes_missing_quota_means_unlimited(ctx, quota):
    storage(ctx, quota=quota)
    result = C.run("storage", {"path": "/project"}, ctx)
    assert result["status"] == "partial"
    assert result["warnings"]


def test_storage_hard_quota_prevents_ready(ctx):
    storage(ctx, quota="Filesystem blocks quota limit files quota limit\n/dev/a 2000 1000 2000 1 0 0\n")
    assert C.run("storage", {"path": "/project"}, ctx)["status"] == "blocked"


@pytest.mark.parametrize("tool,kind,flag", [("quota", "group", "-g"), ("quota", "project", "-P"), ("lfs", "user", "-u"), ("lfs", "group", "-g"), ("lfs", "project", "-p")])
def test_group_project_and_lustre_quota_evidence(ctx, tool, kind, flag):
    storage(ctx)
    argv = ("quota", "-w", flag, "1234") if tool == "quota" else ("lfs", "quota", flag, "1234", "/project")
    ctx.slurm.b.replies[argv] = "Filesystem kbytes quota limit grace files quota limit grace\n/project 100 1000 2000 10 1000 2000\n"
    result = C.run("storage", {"path": "/project", "quota_tool": tool, "quota_kind": kind, "quota_user": "1234"}, ctx)
    assert result["status"] == "ok"
    assert result["data"]["quota_kind"] == kind
    assert argv in ctx.slurm.b.calls


@pytest.mark.parametrize("kind", ["group", "project"])
def test_nonuser_quota_needs_explicit_identity(ctx, kind):
    with pytest.raises(ValueError, match="explicit"):
        C.run("storage", {"quota_kind": kind}, ctx)
    assert ctx.slurm.b.calls == []


@pytest.mark.parametrize("feature,params", [("storage", {}), ("slurm-doctor", {}), ("reservations", {})])
def test_readonly_multi_probe_operations_propagate_cancellation(ctx, feature, params):
    ctx.cancel.set()
    with pytest.raises(ValueError, match="cancelled"):
        C.run(feature, params, ctx)
    assert not ctx.slurm.b.calls


@pytest.mark.parametrize("params", [dict(min_free_gib="nan"), dict(min_free_gib="inf"), dict(min_free_gib="-1"), dict(min_free_inodes="1.5"), dict(quota_user="--all"), dict(path="bad\npath")])
def test_storage_invalid_inputs_no_process(ctx, params):
    with pytest.raises(ValueError):
        C.run("storage", params, ctx)
    assert not ctx.slurm.b.calls


def test_storage_real_local_df(tmp_path):
    ctx = O.Context(slurm=SimpleNamespace(b=Backend(), user="nobody"))
    result = C.run("storage", {"path": str(tmp_path), "min_free_gib": "0", "min_free_inodes": "0"}, ctx)
    assert result["data"]["space"]["total"] > 0
    assert result["data"]["inodes"]["total"] > 0


def test_reservation_timeline_boundaries_unknown_times_and_order():
    now = dt.datetime(2026, 10, 1, tzinfo=dt.timezone.utc)
    records = [dict(ReservationName="earlier", StartTime="2026-09-30T12:00:00", EndTime="2026-10-01T00:00:00"),
               dict(ReservationName="future", StartTime="2026-10-03T00:00:00", EndTime="2026-10-04T00:00:00"),
               dict(ReservationName="active", StartTime="2026-09-30T12:00:00", EndTime="2026-10-01T12:00:00"),
               dict(ReservationName="later", StartTime="2026-10-01T12:00:00", EndTime="2026-10-02T12:00:00"),
               dict(ReservationName="unknown", StartTime="Unknown", EndTime="Unknown")]
    result = C.reservation_timeline(records, now, 48)
    assert [r["ReservationName"] for r in result] == ["active", "later", "unknown"]
    assert result[0]["timeline"] == "|" + "=" * 9 + " " * 27 + "|"
    assert result[2]["window_known"] is False


def test_reservation_query_uses_scheduler_timezone(ctx):
    ctx.slurm.b.replies[("scontrol", "show", "reservation", "-o")] = "ReservationName=gpu StartTime=2026-10-01T12:00:00 EndTime=2026-10-01T14:00:00 Users=alice Accounts=lab NodeCnt=1 CoreCnt=32 Flags=SPEC_NODES"
    ctx.slurm.b.replies[("date", "+%Y-%m-%dT%H:%M:%S%z")] = "2026-10-01T11:00:00-0700\n"
    result = C.run("reservations", {"hours": "6"}, ctx)
    assert result["status"] == "ok"
    assert len(result["data"]["reservations"]) == 1
    assert "-07:00" in result["rows"][0]


def test_empty_reservations_and_timezone_failure_are_explicit(ctx):
    ctx.slurm.b.replies[("scontrol", "show", "reservation", "-o")] = "No reservations in the system\n"
    result = C.run("reservations", {}, ctx)
    assert result["data"]["reservations"] == []
    assert result["status"] == "partial"


def test_license_inventory_unknown_counts_and_case_filter(ctx):
    ctx.slurm.b.replies[("scontrol", "show", "licenses", "-o")] = "LicenseName=MATLAB Total=20 Used=4 Free=16 Reserved=0 Remote=no\nLicenseName=abaqus Total=20 Used=4 Free=unknown Remote=yes Server=lm.example\n"
    result = C.run("licenses", {"name": "matlab"}, ctx)
    assert len(result["data"]["licenses"]) == 1
    assert result["data"]["licenses"][0]["counts"]["Free"] == 16
    assert C.run("licenses", {}, ctx)["status"] == "partial"


def doctor(ctx):
    replies = {("scontrol", "--version"): "slurm 24.11.0\n", ("scontrol", "ping"): "Slurmctld(primary) at host is UP\n",
               ("scontrol", "show", "config"): "ClusterName = research\nAuthType = auth/munge\nJobAcctGatherType = jobacct_gather/cgroup\nPrivateKey = NEVER SHOW\n",
               ("sinfo", "-h", "-o", "%P|%a|%l|%D|%t"): "cpu*|up|1-00:00:00|32|mix\n",
               ("sacctmgr", "-n", "-P", "ping"): "SlurmDBD is UP\n"}
    ctx.slurm.b.replies.update(replies)


def test_doctor_never_reports_secret_config_or_mutates_services(ctx):
    doctor(ctx)
    result = C.run("slurm-doctor", {}, ctx)
    assert result["status"] == "ok"
    assert "NEVER SHOW" not in str(result)
    assert len(result["data"]["probes"]) == 5
    assert not any("systemctl" in call for call in ctx.slurm.b.calls)


@pytest.mark.parametrize("error,wanted", [("Munge authentication failed", "Authentication"), ("unable to resolve host", "Name resolution"), ("connection refused", "Connectivity"), ("Permission denied", "Access"), ("scontrol: not found", "Installation")])
def test_doctor_partial_failures_include_useful_diagnosis(ctx, error, wanted):
    doctor(ctx)
    ctx.slurm.b.replies[("scontrol", "ping")] = CommandError(error)
    result = C.run("slurm-doctor", {}, ctx)
    assert result["status"] == "partial"
    assert any(wanted in line for line in result["rows"])
    assert len(result["data"]["probes"]) == 5


def test_doctor_controller_down_and_disabled_accounting(ctx):
    doctor(ctx)
    ctx.slurm.b.replies[("scontrol", "ping")] = "Slurmctld(primary) is DOWN\nSlurmctld(backup) is UP\n"
    ctx.slurm.b.replies[("scontrol", "show", "config")] = "AccountingStorageType = accounting_storage/none\nJobAcctGatherType = jobacct_gather/none\n"
    result = C.run("slurm-doctor", {}, ctx)
    assert len(result["warnings"]) == 3


@pytest.mark.parametrize("label,argv,text", [("Controller", ("scontrol", "ping"), "Slurmctld(backup) has no reported status"), ("Accounting", ("sacctmgr", "-n", "-P", "ping"), "SlurmDBD(primary) is DOWN")])
def test_doctor_only_explicit_up_tokens_establish_health(ctx, label, argv, text):
    doctor(ctx)
    ctx.slurm.b.replies[argv] = text
    result = C.run("slurm-doctor", {}, ctx)
    assert result["status"] == "partial"
    assert next(probe for probe in result["data"]["probes"] if probe["name"] == label)["status"] == "warning"


def test_retained_script_preview_never_writes_and_exact_save_is_0600(ctx, tmp_path):
    preview = C.run("batch-script", {}, ctx)
    assert "plan" not in preview
    target = tmp_path / "retained.sh"
    review = C.run("batch-script", {"output": str(target)}, ctx)
    assert not target.exists()
    result = C.apply("batch-script", review["plan"], ctx)
    assert result["data"]["output"] == str(target)
    assert target.read_text() == ctx.slurm.b.script
    assert target.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        C.apply("batch-script", review["plan"], ctx)


@pytest.mark.parametrize("replacement", ["different script", "#!/bin/bash\necho changed\n"])
def test_retained_script_changed_after_review_not_saved(ctx, tmp_path, replacement):
    target = tmp_path / "retained.sh"
    review = C.run("batch-script", {"output": str(target)}, ctx)
    ctx.slurm.b.script = replacement
    with pytest.raises(ValueError):
        C.apply("batch-script", review["plan"], ctx)
    assert not target.exists()


@pytest.mark.parametrize("change", [dict(SubmitTime="another attempt"), dict(UserId="bob"), dict(Restarts="1")])
def test_retained_script_job_reuse_not_saved(ctx, tmp_path, change):
    target = tmp_path / "retained.sh"
    review = C.run("batch-script", {"output": str(target)}, ctx)
    ctx.slurm.b.jobs["42"].update(change)
    with pytest.raises(ValueError):
        C.apply("batch-script", review["plan"], ctx)
    assert not target.exists()


def test_retained_script_accounting_fallback_has_exact_identity(ctx):
    ctx.slurm.b.jobs.clear()
    ctx.slurm.b.script = "Batch Script for job 42\n\n#!/bin/bash\necho result\n"
    result = C.run("batch-script", {}, ctx)
    assert result["data"]["source"] == "accounting"
    assert result["data"]["script"].startswith("#!/bin/bash")


@pytest.mark.parametrize("history", ["43|alice|now|now|COMPLETED|cluster\n", "42.batch|alice|now|now|COMPLETED|cluster\n", "42|alice|now|now|COMPLETED|a\n42|alice|later|later|COMPLETED|a\n"])
def test_retained_script_ambiguous_or_wrong_historical_job_rejected(ctx, history):
    ctx.slurm.b.jobs.clear()
    ctx.slurm.b.history = history
    with pytest.raises(ValueError):
        C.run("batch-script", {}, ctx)
    assert not any("--batch-script" in call for call in ctx.slurm.b.calls)


def test_retained_script_no_overwrite_or_symlink_follow(ctx, tmp_path):
    target = tmp_path / "out"
    target.write_text("existing")
    with pytest.raises(ValueError, match="already exists"):
        C.run("batch-script", {"output": str(target)}, ctx)
    link = tmp_path / "link"
    link.symlink_to(tmp_path / "missing")
    with pytest.raises(ValueError, match="already exists"):
        C.run("batch-script", {"output": str(link)}, ctx)
    target.unlink()
    review = C.run("batch-script", {"output": str(target)}, ctx)
    target.symlink_to(tmp_path / "missing")
    with pytest.raises(FileExistsError):
        C.apply("batch-script", review["plan"], ctx)
    assert not (tmp_path / "missing").exists()


def test_retained_script_output_directory_replaced(ctx, tmp_path):
    directory = tmp_path / "export"
    directory.mkdir()
    review = C.run("batch-script", {"output": str(directory / "run.sh")}, ctx)
    directory.rename(tmp_path / "original")
    directory.mkdir()
    with pytest.raises(ValueError, match="directory changed"):
        C.apply("batch-script", review["plan"], ctx)
    assert not (directory / "run.sh").exists()


def test_retained_preview_remote_but_save_requires_target_host(ctx, tmp_path):
    remote = replace(ctx, files=SimpleNamespace(remote=True))
    assert C.run("batch-script", {}, remote)["status"] == "ok"
    with pytest.raises(ValueError, match="target host"):
        C.run("batch-script", {"output": str(tmp_path / "run.sh")}, remote)


def running(ctx):
    ctx.slurm.b.jobs["42"]["JobState"] = "RUNNING"


def test_allocation_shell_is_reviewed_one_task_exact_handoff(ctx):
    running(ctx)
    review = prepare(ctx, "allocation-shell")
    assert not any(call[0] in {"srun", "sattach", "salloc"} for call in ctx.slurm.b.calls)
    result = C.apply("allocation-shell", review["plan"], ctx)
    argv = result["data"]["terminal_argv"]
    assert argv == ["srun", "--jobid=42", "--overlap", "--exact", "--nodes=1", "--ntasks=1", "--cpus-per-task=1", "--immediate=5", "--pty", "/bin/bash"]
    assert C.revalidate_terminal(ctx, result["data"]["terminal_identity"]) == argv


@pytest.mark.parametrize("selected,changes", [("42_7", dict(JobId="49", ArrayJobId="42", ArrayTaskId="7")), ("42+1", dict(JobId="49", HetJobId="42", HetJobOffset="1"))])
def test_array_and_heterogeneous_shells_use_exact_numeric_allocation(ctx, selected, changes):
    ctx.slurm.b.jobs[selected] = job(JobState="RUNNING", **changes)
    ctx.slurm.b.steps = "StepId=49.0 State=RUNNING\nStepId=50.0 State=RUNNING\n"
    review = prepare(ctx, "allocation-shell", job_id=selected)
    ready = C.apply("allocation-shell", review["plan"], ctx)
    assert ready["data"]["terminal_argv"][1] == "--jobid=49"
    assert ready["data"]["job_id"] == selected
    attachment = prepare(ctx, "allocation-shell", job_id=selected, mode="attach", step_id="0")
    assert C.apply("allocation-shell", attachment["plan"], ctx)["data"]["terminal_argv"] == ["sattach", "49.0"]


def test_new_shell_can_be_prepared_when_existing_step_inventory_unavailable(ctx):
    running(ctx)
    ctx.slurm.b.replies[("scontrol", "show", "step", "-o", "42")] = CommandError("no job steps found")
    review = prepare(ctx, "allocation-shell")
    assert review["warnings"]
    assert review["plan"]
    with pytest.raises(CommandError):
        prepare(ctx, "allocation-shell", mode="attach", step_id="0")


@pytest.mark.parametrize("step", ["0", "42.0", "1", "42.1"])
def test_attach_only_exact_running_numeric_step(ctx, step):
    running(ctx)
    review = prepare(ctx, "allocation-shell", mode="attach", step_id=step)
    result = C.apply("allocation-shell", review["plan"], ctx)
    assert result["data"]["terminal_argv"] == ["sattach", "42." + step.rsplit(".", 1)[-1]]


@pytest.mark.parametrize("step", ["batch", "42.batch", "42.extern", "43.0", "42.9", "--help", "42.0;id"])
def test_attach_rejects_other_allocations_batch_extern_unknown_steps(ctx, step):
    running(ctx)
    with pytest.raises(ValueError):
        prepare(ctx, "allocation-shell", mode="attach", step_id=step)


@pytest.mark.parametrize("change", [dict(JobState="COMPLETED"), dict(Restarts="1"), dict(StartTime="later"), dict(SubmitTime="later"), dict(UserId="bob")])
def test_shell_identity_revalidated_on_apply_and_immediate_handoff(ctx, change):
    running(ctx)
    review = prepare(ctx, "allocation-shell")
    ready = C.apply("allocation-shell", review["plan"], ctx)
    ctx.slurm.b.jobs["42"].update(change)
    with pytest.raises(ValueError):
        C.apply("allocation-shell", review["plan"], ctx)
    with pytest.raises(ValueError):
        C.revalidate_terminal(ctx, ready["data"]["terminal_identity"])


def test_attach_step_finishes_during_review(ctx):
    running(ctx)
    review = prepare(ctx, "allocation-shell", mode="attach", step_id="0")
    ready = C.apply("allocation-shell", review["plan"], ctx)
    ctx.slurm.b.steps = "StepId=42.0 State=COMPLETED\nStepId=42.1 State=RUNNING\n"
    with pytest.raises(ValueError, match="step is no longer"):
        C.apply("allocation-shell", review["plan"], ctx)
    with pytest.raises(ValueError, match="step is no longer"):
        C.revalidate_terminal(ctx, ready["data"]["terminal_identity"])


@pytest.mark.parametrize("shell", ["bash", "--help", "/bin/bash\n/bin/evil", "/bin/"])
def test_shell_path_must_be_explicit(ctx, shell):
    running(ctx)
    with pytest.raises(ValueError):
        prepare(ctx, "allocation-shell", shell=shell)


def test_shell_requires_local_running_allocation_and_owner(ctx):
    with pytest.raises(ValueError, match="running"):
        prepare(ctx, "allocation-shell")
    running(ctx)
    with pytest.raises(ValueError, match="target host"):
        prepare(replace(ctx, files=SimpleNamespace(remote=True)), "allocation-shell")
    ctx.slurm.b.jobs["42"]["UserId"] = "bob"
    with pytest.raises(ValueError, match="owned"):
        prepare(ctx, "allocation-shell")


def test_sequential_features_do_not_change_other_reviews_or_hidden_job_scope(ctx, tmp_path):
    pending = prepare(ctx)
    storage(ctx)
    C.run("storage", {"path": "/project"}, ctx)
    retained = C.run("batch-script", {"output": str(tmp_path / "retained.sh")}, ctx)
    C.apply("pending-edit", pending["plan"], ctx)
    # Editing resources cannot silently swap a retained script's job identity.
    C.apply("batch-script", retained["plan"], ctx)
    running(ctx)
    shell = prepare(ctx, "allocation-shell")
    doctor(ctx)
    C.run("slurm-doctor", {}, ctx)
    ready = C.apply("allocation-shell", shell["plan"], ctx)
    assert ready["data"]["job_id"] == "42"
    assert len(mutations(ctx)) == 1
