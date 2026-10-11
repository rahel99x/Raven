"""Reviewed own-job priority preferences fail closed when evidence changes."""
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
import threading

import pytest

from tower import operations as O, ops_queue_order as Q


def job(jid, **changes):
    value = dict(JobId=str(jid), JobName="research", UserId="alice(1000)", JobState="PENDING",
        SubmitTime="2026-10-01T12:00:00", Partition="cpu", Account="lab", QOS="normal",
        Priority=str(1000 - int(jid)), Nice="0", Dependency="(null)", Restarts="0")
    value.update(changes)
    return value


class Backend:
    def __init__(self):
        self.jobs = {str(i): job(i) for i in range(10, 16)}
        self.calls = []
        self.flags = "enable_user_top,foo"
        self.queued = None

    def run(self, argv, timeout):
        self.calls.append(argv)
        if argv == ["scontrol", "show", "config"]:
            return "SchedulerParameters = " + self.flags + "\nPriorityType = priority/multifactor\nClusterName = cluster\n", 0
        if argv[0] == "squeue":
            selected = dict(argument[2:].split("=", 1) for argument in argv if argument.startswith("--") and "=" in argument)
            return "\n".join(self.queued if self.queued is not None else
                [key for key, value in self.jobs.items() if value["JobState"] == "PENDING"
                 and all(value[field] == selected[option] for field, option in
                         (("Partition", "partition"), ("Account", "account"), ("QOS", "qos")))]), 0
        if argv[:4] == ["scontrol", "show", "job", "-o"]:
            return "\n".join(" ".join(f"{key}={value}" for key, value in self.jobs[jid].items())
                             for jid in argv[-1].split(",") if jid in self.jobs), 0
        if argv[:2] == ["scontrol", "top"]:
            return "", 0
        raise AssertionError(argv)


@pytest.fixture
def ctx(tmp_path):
    return O.Context(slurm=SimpleNamespace(b=Backend(), user="alice"), scope={"host": "cluster", "user": "alice"},
                     state_dir=str(tmp_path), cancel=threading.Event())


def params(source="14,15", target="10,11", placement="before"):
    return dict(source_jobs=source, target_jobs=target, placement=placement)


def mutating(ctx):
    return [call for call in ctx.slurm.b.calls if call[:2] == ["scontrol", "top"]]


def test_review_includes_all_own_pending_peers_and_uses_one_ordered_native_call(ctx):
    report = O.run("queue-order", params(), ctx)
    assert not mutating(ctx)
    assert report["data"]["order"] == ["14", "15", "10", "11", "12", "13"]
    assert len(report["rows"]) == 6
    result = O.apply("queue-order", report["plan"], ctx)
    assert result["status"] == "ok"
    assert mutating(ctx) == [["scontrol", "top", "14,15,10,11,12,13"]]
    assert result["receipt_path"]
    with pytest.raises(ValueError, match="already attempted"):
        O.apply("queue-order", report["plan"], ctx)


def test_after_preserves_unrelated_order_and_member_relative_priority(ctx):
    report = O.run("queue-order", params("11,10", "14,15", "after"), ctx)
    assert report["data"]["order"] == ["12", "13", "14", "15", "10", "11"]


def test_existing_order_has_no_mutation_plan(ctx):
    report = O.run("queue-order", params("10,11", "14,15"), ctx)
    assert "plan" in report  # Noncontiguous peers between source/target move, too.
    report = O.run("queue-order", params("10,11", "12,13"), ctx)
    assert "plan" not in report


@pytest.mark.parametrize("flag", ["", "disable_user_top", "other_enable_user_top", "enable_user_top=no"])
def test_unavailable_site_capability_fails_before_reading_jobs(ctx, flag):
    ctx.slurm.b.flags = flag
    with pytest.raises(ValueError, match="enable_user_top"):
        O.run("queue-order", params(), ctx)
    assert ctx.slurm.b.calls == [["scontrol", "show", "config"]]


@pytest.mark.parametrize("field,value", [("UserId", "bob(2000)"), ("Account", "other"), ("Partition", "gpu"),
                                         ("Partition", "cpu,gpu"), ("QOS", "urgent"), ("QOS", ""),
                                         ("Priority", "0"), ("Priority", "1"), ("Priority", "nan")])
def test_cross_scope_and_held_jobs_never_mutate(ctx, field, value):
    ctx.slurm.b.jobs["15"][field] = value
    with pytest.raises(ValueError):
        O.run("queue-order", params(), ctx)
    assert not mutating(ctx)


def test_other_buckets_remain_untouched_and_running_members_are_excluded(ctx):
    ctx.slurm.b.jobs["13"]["QOS"] = "urgent"
    ctx.slurm.b.jobs["15"]["JobState"] = "RUNNING"
    report = O.run("queue-order", params(), ctx)
    assert report["data"]["order"] == ["14", "10", "11", "12"]
    O.apply("queue-order", report["plan"], ctx)
    assert mutating(ctx) == [["scontrol", "top", "14,10,11,12"]]


@pytest.mark.parametrize("field,value", [("SubmitTime", "2026-10-02T12:00:00"), ("JobState", "RUNNING"),
    ("Account", "changed"), ("Partition", "changed"), ("QOS", "changed"), ("Priority", "1001"),
    ("Nice", "100"), ("Dependency", "afterok:999"), ("Restarts", "1")])
def test_revalidation_rejects_changed_own_peer_before_mutation(ctx, field, value):
    report = O.run("queue-order", params(), ctx)
    ctx.slurm.b.jobs["12"][field] = value
    with pytest.raises(ValueError):
        O.apply("queue-order", report["plan"], ctx)
    assert not mutating(ctx)


def test_new_pending_peer_invalidates_review_and_preserves_exact_scope(ctx):
    report = O.run("queue-order", params(), ctx)
    ctx.slurm.b.jobs["16"] = job(16)
    with pytest.raises(ValueError, match="queue changed"):
        O.apply("queue-order", report["plan"], ctx)
    assert not mutating(ctx)


@pytest.mark.parametrize("value", ["14,14", "14;id", "14,15;id", "14_[0-8]", "14+1", "-1", "14.0", "", "١٤"])
def test_malformed_or_ambiguous_ids_fail_before_io(ctx, value):
    with pytest.raises(ValueError):
        O.run("queue-order", params(source=value), ctx)
    assert not ctx.slurm.b.calls


def test_known_pending_dependencies_are_not_modified(ctx):
    ctx.slurm.b.jobs["14"]["Dependency"] = "afterok:10"
    report = O.run("queue-order", params(), ctx)
    O.apply("queue-order", report["plan"], ctx)
    assert ctx.slurm.b.jobs["14"]["Dependency"] == "afterok:10"
    assert all(call[:2] != ["scontrol", "update"] for call in ctx.slurm.b.calls)


def test_frozen_drag_attempt_cannot_select_reused_job_id(ctx):
    with pytest.raises(ValueError, match="attempt changed"):
        Q.inspect(params(), ctx, expected={"14": ("14", "2026-09-01T12:00:00", "cluster", None)})
    assert not mutating(ctx)


def test_frozen_drag_cluster_is_verified_even_when_ids_and_submissions_match(ctx):
    with pytest.raises(ValueError, match="different or unverified cluster"):
        Q.inspect(params(), ctx, expected={"14": ("14", "2026-10-01T12:00:00", "other", None)})
    assert not mutating(ctx)


def test_numeric_array_tasks_are_reviewed_without_ranges_or_parent_expansion(ctx):
    ctx.slurm.b.jobs["99_1"] = job(101, ArrayJobId="99", ArrayTaskId="1")
    ctx.slurm.b.jobs["99_2"] = job(102, ArrayJobId="99", ArrayTaskId="2")
    report = O.run("queue-order", params("99_1,99_2"), ctx)
    assert report["data"]["order"][:2] == ["99_1", "99_2"]
    O.apply("queue-order", report["plan"], ctx)
    assert mutating(ctx)[0][2].startswith("99_1,99_2,")


def test_compressed_array_records_are_rejected_even_after_array_squeue(ctx):
    ctx.slurm.b.jobs["99_1"] = job(99, ArrayJobId="99", ArrayTaskId="1-10")
    with pytest.raises(ValueError, match="unexpanded array"):
        O.run("queue-order", params("99_1,14"), ctx)
    assert not mutating(ctx)


def test_maximum_queue_plan_fits_complete_workbench_review(ctx):
    from tower import ops_ui
    ctx.slurm.b.jobs = {str(i): job(i) for i in range(1, 513)}
    report = O.run("queue-order", params("511,512", "1,2"), ctx)
    state = dict(result=report, review_document=None, review_size=None)
    assert not ops_ui._review_is_truncated(state, 80)


def test_cancelled_replay_and_changed_connection_never_apply(ctx):
    plan = O.run("queue-order", params(), ctx)["plan"]
    with pytest.raises(ValueError, match="connection changed"):
        O.apply("queue-order", plan, replace(ctx, scope={"host": "other"}))
    with pytest.raises(ValueError, match="Recorded"):
        O.apply("queue-order", plan, replace(ctx, replay=True))
    ctx.cancel.set()
    with pytest.raises(ValueError, match="cancelled"):
        O.apply("queue-order", plan, ctx)
    assert not mutating(ctx)


def test_queue_bound_does_not_silently_reorder_truncated_list(ctx):
    ctx.slurm.b.queued = [str(i) for i in range(1, 514)]
    with pytest.raises(ValueError, match="512"):
        O.run("queue-order", params(), ctx)
    assert not mutating(ctx)


def test_thousands_of_unrelated_pending_jobs_do_not_expand_review_scope(ctx):
    for jid in range(1000, 4000):
        ctx.slurm.b.jobs[str(jid)] = job(jid, Partition="other", Priority="500")
    report = O.run("queue-order", params(), ctx)
    assert len(report["rows"]) == 6
    queue = next(call for call in ctx.slurm.b.calls if call[0] == "squeue")
    assert {"--partition=cpu", "--account=lab", "--qos=normal", "--user=alice"} <= set(queue)


def test_plan_tamper_does_not_reach_native_action(ctx):
    plan = O.run("queue-order", params(), ctx)["plan"]
    plan["payload"]["proposed"].append("999")
    with pytest.raises(ValueError, match="reviewed plan changed"):
        O.apply("queue-order", plan, ctx)
    assert not mutating(ctx)
