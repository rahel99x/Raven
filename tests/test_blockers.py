"""Scheduler explanations must preserve uncertainty and dependency semantics."""
from __future__ import annotations

import json
import random
import time

import pytest

from raven.blockers import MAX_DEPTH, MAX_DEPENDENCY_TEXT, MAX_RECORDS, MAX_TREE, explain
from raven.model import Finished, Health, Job, Node, NodeCell, Partition


def queued(jid="90", reason="Dependency", dependency="", **kwargs):
    return Job(id=jid, name="train", partition="gpu", state="PENDING", reason=reason, dependency=dependency, **kwargs)


def done(jid="1", state="COMPLETED", **kwargs):
    return Finished(id=jid, state=state, **kwargs)


def clause(result, index=0):
    return result["dependencies"]["clauses"][index]


@pytest.mark.parametrize("reason", ["Resources", "Priority", "Dependency", "DependencyNeverSatisfied", "JobHeldUser", "JobHeldAdmin", "BeginTime", "Reservation", "ReqNodeNotAvail", "BadConstraints", "PartitionDown", "PartitionInactive", "PartitionNodeLimit", "PartitionTimeLimit", "InvalidAccount", "InvalidQOS", "QOSNotAllowed", "Licenses", "QOSGrpCpuLimit", "AssocGrpMemLimit", "QOSMaxJobsPerUserLimit", "AssocMaxWallDurationPerJobLimit", "JobArrayTaskLimit", "BurstBufferStageIn"])
def test_exact_known_reason_has_evidence_and_no_automatic_action(reason):
    result = explain(queued(reason=reason))
    blocker = result["blockers"][0]
    assert result["status"] == "blocked"
    assert blocker["code"] == reason and blocker["status"] == "confirmed"
    assert blocker["checks"] and blocker["editable"] is False
    ids = {e["id"] for e in result["evidence"]}
    assert all(eid in ids for eid in blocker["support"])
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize("reason", ["QOSMagicFutureLimit", "AssocMystery", "resources", "PriorityExtra", "NewReason"])
def test_unknown_reason_is_not_guessed_from_prefix(reason):
    result = explain(queued(reason=reason))
    assert result["status"] == "unknown"
    assert result["blockers"][0]["status"] == "unknown"


def test_reason_suffix_preserved_and_terminal_controls_removed():
    result = explain(queued(reason="(ReqNodeNotAvail, UnavailableNodes:n[01-04])"))
    assert result["reason"]["code"] == "ReqNodeNotAvail"
    assert result["reason"]["suffix"] == "UnavailableNodes:n[01-04]"
    injected = explain(queued(reason="Resources\x1b[31m\n"))
    assert all(c.isprintable() for c in json.dumps(injected))
    assert "\x1b" not in str(injected)


@pytest.mark.parametrize("state", ["RUNNING", "COMPLETING", "SUSPENDED", "COMPLETED", "FAILED"])
def test_non_pending_job_does_not_claim_active_scheduler_blocker(state):
    result = explain({"id": "1", "state": state, "reason": "Resources"})
    assert result["status"] == "clear" and not result["blockers"]
    assert "not applicable" in result["summary"]
    assert result["placement"]["status"] == "not_checked"


def test_empty_reason_never_claims_ready_or_immediate_start():
    result = explain(queued(reason="None"))
    assert result["status"] == "unknown"
    assert "does not guarantee" in result["summary"]


@pytest.mark.parametrize("kind,state,expected", [
    ("afterok", "COMPLETED", "satisfied"), ("afterok", "FAILED", "impossible"),
    ("afterok", "CANCELLED", "impossible"), ("afterok", "RUNNING", "waiting"),
    ("afterany", "FAILED", "satisfied"), ("afterany", "CANCELLED", "satisfied"),
    ("afterany", "COMPLETED", "satisfied"), ("afterany", "PENDING", "waiting"),
    ("afternotok", "FAILED", "satisfied"), ("afternotok", "TIMEOUT", "satisfied"),
    ("afternotok", "OUT_OF_MEMORY", "satisfied"), ("afternotok", "COMPLETED", "impossible"),
    ("afternotok", "PENDING", "waiting"), ("afternotok", "CANCELLED", "unknown"),
    ("after", "RUNNING", "satisfied"), ("after", "COMPLETED", "satisfied"),
    ("after", "CANCELLED", "satisfied"), ("after", "PENDING", "waiting"),
    ("afterburstbuffer", "COMPLETED", "unknown"),
])
def test_dependency_kind_semantics_do_not_equate_failure_with_all_blockers(kind, state, expected):
    result = explain(queued(dependency=f"{kind}:1"), finished=[done(state=state)])
    assert clause(result)["targets"][0]["status"] == expected
    assert result["dependencies"]["status"] == expected


def test_missing_external_predecessor_is_unknown_not_gone_or_failed():
    result = explain(queued(dependency="afterok:999"))
    target = clause(result)["targets"][0]
    assert target["status"] == "unknown" and target["external"]
    assert "absence does not mean" in target["summary"]
    assert any("lack predecessor records" in s for s in result["limitations"])


def test_and_requires_all_clauses_while_or_accepts_any_satisfied_clause():
    history = [done("1"), done("2", "FAILED")]
    both = explain(queued(dependency="afterok:1,afterok:2"), finished=history)
    either = explain(queued(dependency="afterok:1?afterok:2"), finished=history)
    assert both["dependencies"]["operator"] == "and"
    assert both["dependencies"]["status"] == "impossible"
    assert either["dependencies"]["operator"] == "or"
    assert either["dependencies"]["status"] == "satisfied"


def test_multiple_ids_in_one_clause_all_have_to_satisfy():
    result = explain(queued(dependency="afterok:1:2"), finished=[done("1"), done("2", "FAILED")])
    assert result["dependencies"]["status"] == "impossible"
    assert len(clause(result)["targets"]) == 2


@pytest.mark.parametrize("text", ["afterok:1,afterany:2?afterok:3", "afterok", "afterok:", "afterok:abc", "weird:123", "afterok:1+5", "singleton:1", "afterok:1,,afterany:2", "afterok:1(unrecognized)"])
def test_malformed_or_unsupported_dependencies_stay_unknown(text):
    result = explain(queued(dependency=text), finished=[done("1"), done("2")])
    assert result["dependencies"]["status"] == "unknown"
    assert any("syntax" in s for s in result["limitations"])


def test_scheduler_annotations_preserved_and_contradictions_do_not_fake_certainty():
    satisfied = explain(queued(dependency="afterok:999(fulfilled)"))
    assert clause(satisfied)["status"] == "satisfied"
    stale = explain(queued(dependency="afterok:1(unfulfilled)"), finished=[done()])
    assert clause(stale)["status"] == "unknown"
    assert "conflicts" in clause(stale)["summary"]
    impossible = explain(queued(dependency="afterok:1(fulfilled)"), finished=[done(state="FAILED")])
    assert clause(impossible)["status"] == "unknown"


def test_finished_state_conflicting_with_nonzero_exit_remains_unknown():
    result = explain(queued(dependency="afterok:1"), finished=[done(exit="1:0")])
    assert clause(result)["status"] == "unknown"
    assert "conflicts" in clause(result)["targets"][0]["summary"]
    failure = explain(queued(dependency="afternotok:1"), finished=[done(exit="1:0")])
    assert clause(failure)["status"] == "unknown"


def test_current_queue_overrides_obsolete_history_for_same_id():
    result = explain(queued(dependency="afterok:1"), jobs=[queued("1")], finished=[done("1")])
    assert clause(result)["status"] == "waiting"


def test_corresponding_array_dependency_uses_same_task_not_parent_success():
    owner = queued("90_4", dependency="aftercorr:1")
    result = explain(owner, finished=[done("1"), done("1_4", "FAILED"), done("1_3")])
    assert clause(result)["targets"][0]["job_id"] == "1_4"
    assert clause(result)["status"] == "impossible"
    parent = explain(queued("90", dependency="aftercorr:1"), finished=[done("1")])
    assert clause(parent)["status"] == "unknown"
    assert "task index is absent" in clause(parent)["targets"][0]["summary"]


@pytest.mark.parametrize("now,expected", [(1300, "satisfied"), (1299, "waiting")])
def test_after_offset_checks_observed_begin_timestamp(now, expected):
    result = explain(queued(dependency="after:1+5"), jobs=[{"id": "1", "state": "RUNNING", "start": 1000}], now=now)
    assert clause(result)["status"] == expected


def test_after_offset_without_start_is_unknown_and_cancel_can_use_end():
    unknown = explain(queued(dependency="after:1+5"), jobs=[{"id": "1", "state": "RUNNING"}], now=2000)
    assert clause(unknown)["status"] == "unknown"
    cancelled = explain(queued(dependency="after:1+5"), finished=[{"id": "1", "state": "CANCELLED", "end": 1000}], now=1300)
    assert clause(cancelled)["status"] == "satisfied"


def test_singleton_requires_name_user_and_submission_order_and_is_locally_bounded():
    owner = queued("90", dependency="singleton", user="alex", submit="2026-10-01T12:00:00")
    earlier = queued("1", user="alex", submit="2026-10-01T11:00:00")
    different_user = queued("2", user="bob", submit="2026-10-01T10:00:00")
    later = queued("3", user="alex", submit="2026-10-01T13:00:00")
    result = explain(owner, jobs=[earlier, different_user, later])
    assert clause(result)["status"] == "waiting"
    assert [t["job_id"] for t in clause(result)["targets"]] == ["1"]
    no_peers = explain(owner)
    assert clause(no_peers)["status"] == "unknown"
    assert "federation" in clause(no_peers)["summary"]


def test_cyclic_and_very_deep_dependency_graphs_do_not_recurse_or_hang():
    cycle = explain(queued("1", dependency="afterok:2"), jobs=[queued("2", dependency="afterok:1")])
    assert any(t["cycle"] for t in cycle["dependencies"]["tree"])
    assert cycle["dependencies"]["status"] == "unknown"
    chain = [queued(str(i), dependency=f"afterok:{i + 1}") for i in range(1, 1000)]
    deep = explain(chain[0], jobs=chain)
    assert max(t["depth"] for t in deep["dependencies"]["tree"]) <= MAX_DEPTH
    assert deep["dependencies"]["truncated"] and deep["status"] == "partial"


def test_wide_and_oversized_dependency_inputs_are_bounded():
    owner = queued(dependency="afterok:" + ":".join(str(i) for i in range(1, 129)))
    wide = explain(owner, jobs=[queued(str(i)) for i in range(1, 129)])
    assert len(wide["dependencies"]["tree"]) <= MAX_TREE
    assert wide["truncated"]
    huge = explain(queued(dependency="x" * (MAX_DEPENDENCY_TEXT + 1)))
    assert huge["dependencies"]["status"] == "unknown" and huge["truncated"]


def cell(name, cpus, memory_mib, **kwargs):
    return NodeCell(name=name, partitions=["gpu"], state="idle", cpus=cpus, cpus_idle=cpus, mem=memory_mib, **kwargs)


def test_fragmented_cpu_and_memory_capacity_are_not_added_across_nodes():
    owner = queued(reason="Resources", cpus=16, mem_req="32G")
    rows = [cell("cpu-rich", 32, 8 * 1024), cell("mem-rich", 8, 64 * 1024)]
    result = explain(owner, nodes=rows, partitions=[Partition("gpu", nodes=2)])
    placement = result["placement"]
    assert placement["status"] == "insufficient" and placement["eligible_nodes"] == 0
    assert {c for n in placement["nodes"] for c in n["checks"]} >= {"cpu_capacity", "memory_capacity"}
    assert result["blockers"][-1]["status"] == "possible"


def test_partial_node_snapshot_never_proves_impossible_placement():
    result = explain(queued(cpus=32, mem_req="16G"), nodes=[cell("a", 4, 64 * 1024)], partitions=[Partition("gpu", nodes=5)])
    assert result["placement"]["status"] == "unknown"
    assert not result["placement"]["complete"]


def test_gpu_types_and_availability_must_be_individually_supported():
    owner = queued(cpus=8, mem_req="16G", gpu_type="a100", gpus=1)
    wrong = cell("a40", 32, 128 * 1024, gpu_type="a40", gpus=2)
    used = cell("a100-used", 32, 128 * 1024, gpu_type="a100", gpus=2, gpus_used=2)
    missing = cell("unknown", 32, 128 * 1024, gpus=2)
    result = explain(owner, nodes=[wrong, used, missing], partitions=[Partition("gpu", nodes=3)])
    assert result["placement"]["status"] == "unknown"
    assert result["placement"]["nodes"][2]["status"] == "unknown"
    assert "unknown_gpu_type" in result["placement"]["nodes"][2]["checks"]


def test_os_free_memory_is_not_scheduler_unallocated_memory():
    owner = queued(cpus=4, mem_req="8G")
    node = Node(name="a", state="IDLE", cpus=32, alloc=0, mem_total=128 * 1024, mem_free=128 * 1024, partitions="gpu")
    result = explain(owner, nodes=[node], partitions=[Partition("gpu", nodes=1)])
    assert result["placement"]["status"] == "unknown"
    assert "unknown_memory_reservations" in result["placement"]["nodes"][0]["checks"]


def test_reserved_memory_and_cpu_are_subtracted_not_ignored():
    owner = queued(cpus=16, mem_req="32G")
    node = NodeCell(name="a", partitions=["gpu"], state="mix", cpus=64, cpus_alloc=60, cpus_idle=4, mem=64 * 1024, mem_alloc=60 * 1024)
    result = explain(owner, nodes=[node], partitions=[Partition("gpu", nodes=1)])
    assert result["placement"]["status"] == "insufficient"
    assert result["placement"]["nodes"][0]["memory_unallocated_bytes"] == 4 * 1024**3


def test_multi_node_totals_do_not_require_an_invented_even_distribution():
    owner = queued(cpus=10, nodes=2, mem_req="4G", gpus=2)
    result = explain(owner, nodes=[cell("a", 9, 64 * 1024, gpus=2), cell("b", 1, 64 * 1024)], partitions=[Partition("gpu", nodes=2)])
    assert result["placement"]["status"] == "unknown"
    assert result["placement"]["request_per_node_lower_bound"]["cpus"] == 1
    assert result["placement"]["request_per_node_lower_bound"]["gpus"] is None


def test_explicit_per_node_requirements_support_conditional_screen():
    owner = queued(cpus=16, nodes=2, mem_req="4Gc", gpu_type="a100", gpus=2)
    rows = [cell("a", 8, 32 * 1024, gpu_type="a100", gpus=1), cell("b", 8, 32 * 1024, gpu_type="a100", gpus=1)]
    result = explain(owner, details={"MinCPUsNode": "8", "TresPerNode": "gres/gpu:a100:1"}, nodes=rows, partitions=[Partition("gpu", nodes=2)])
    assert result["placement"]["status"] == "possible"
    assert result["placement"]["request_per_node_lower_bound"]["memory_bytes"] == 32 * 1024**3
    assert "does not establish" in " ".join(result["placement"]["limitations"])


def test_constraints_and_mem_zero_are_unknown_even_if_aggregate_resources_fit():
    rows = [cell("a", 32, 128 * 1024)]
    constrained = explain(queued(cpus=4, mem_req="8G"), details={"Features": "avx512"}, nodes=rows, partitions=[Partition("gpu", nodes=1)])
    assert constrained["placement"]["status"] == "unknown"
    all_memory = explain(queued(cpus=4, mem_req="0"), nodes=rows, partitions=[Partition("gpu", nodes=1)])
    assert all_memory["placement"]["status"] == "unknown"


def test_down_nodes_cannot_be_screened_as_available():
    node = cell("a", 32, 128 * 1024)
    node.state = "idle+drain"
    result = explain(queued(cpus=4, mem_req="8G"), nodes=[node], partitions=[Partition("gpu", nodes=1)])
    assert result["placement"]["status"] == "insufficient"
    assert "unavailable_state" in result["placement"]["nodes"][0]["checks"]


def test_partition_time_evidence_is_conditional_policy_not_forced_action():
    result = explain(queued(reason="Priority", limit="02:00:00"), partitions=[Partition("gpu", limit="01:00:00")])
    limit = next(b for b in result["blockers"] if b["code"] == "partition_time_snapshot")
    assert limit["status"] == "possible" and "QoS override" in limit["checks"][0]


def test_fairshare_is_descriptive_without_synthetic_queue_position():
    result = explain(queued(reason="Priority", account="lab"), share=[{"account": "other", "fairshare": "0.9"}, {"account": "lab", "usage": "456", "fairshare": "0.02"}])
    assert len(result["fairshare"]) == 1
    assert result["fairshare"][0]["fairshare"] == "0.02"
    assert "cannot establish queue position" in result["fairshare"][0]["interpretation"]
    assert "start_time" not in result and "queue_position" not in result


def test_stale_error_disabled_future_and_fresh_health_are_explicit():
    owner = queued(reason="Resources")
    stale = explain(owner, health={"jobs": Health("jobs", last_ok=1000)}, now=1201)
    assert stale["status"] == "partial" and stale["sources"][0]["age_seconds"] == 201
    fresh = explain(owner, health={"jobs": Health("jobs", last_ok=1150)}, now=1201)
    assert fresh["status"] == "blocked" and fresh["sources"][0]["status"] == "fresh"
    future = explain(owner, health={"jobs": Health("jobs", last_ok=2000)}, now=1201)
    assert future["sources"][0]["status"] == "unknown"
    errored = explain(owner, health={"jobs": Health("jobs", last_ok=1199, error="squeue timed out")}, now=1201)
    assert errored["sources"][0]["status"] == "error"
    disabled = explain(owner, health={"nodes": Health("nodes", enabled=False)}, now=1201)
    assert disabled["sources"][0]["status"] == "disabled"


@pytest.mark.parametrize("bad", [None, [], {}, True, -1, float("nan"), float("inf"), 10**400, "not-a-number"])
def test_malformed_extreme_and_nonfinite_numeric_metadata_is_json_safe(bad):
    owner = {"id": "90", "state": "PENDING", "reason": "Resources", "partition": "gpu", "nodes": bad, "cpus": bad, "gpus": bad, "mem_req": bad}
    node = {"name": "a", "partitions": ["gpu"], "state": "idle", "cpus_idle": bad, "mem": bad, "mem_alloc": bad, "gpus": bad, "gpus_used": bad}
    result = explain(owner, nodes=[node], partitions=[{"name": "gpu", "nodes": bad}], health={"jobs": {"last_ok": bad}}, now=bad)
    json.dumps(result, allow_nan=False)
    assert result["placement"]["status"] == "unknown"


def test_large_queue_is_linear_and_bounds_consumption_of_iterables():
    consumed = []
    def records():
        for i in range(MAX_RECORDS + 100):
            consumed.append(i)
            yield {"id": str(i + 1), "state": "RUNNING"}
    started = time.monotonic()
    result = explain(queued("99999", dependency="afterok:1"), jobs=records())
    assert len(consumed) == MAX_RECORDS + 1
    assert result["truncated"] and result["status"] == "partial"
    assert time.monotonic() - started < 5


def test_random_dependency_corpus_remains_bounded_and_json_serializable():
    rng = random.Random(77)
    history = [done(str(i), rng.choice(["COMPLETED", "FAILED", "CANCELLED"])) for i in range(1, 20)]
    for _ in range(250):
        separator = rng.choice([",", "?", ":", ""])
        text = separator.join(rng.choice(["afterok", "afterany", "afternotok", "aftercorr", "after", "bogus"]) + ":" + str(rng.randrange(1, 30)) for _ in range(rng.randrange(1, 7)))
        result = explain(queued("90_4", dependency=text), finished=history)
        assert len(result["dependencies"]["tree"]) <= MAX_TREE
        json.dumps(result, allow_nan=False)


def test_explanation_does_not_mutate_input_snapshot():
    owner = queued(dependency="afterok:1")
    predecessor = done("1")
    details = {"Features": "avx512"}
    before = (vars(owner).copy(), vars(predecessor).copy(), details.copy())
    explain(owner, finished=[predecessor], details=details)
    assert before == (vars(owner), vars(predecessor), details)


def test_invalid_idle_cpu_count_never_fabricates_available_capacity():
    node = cell("a", 4, 64 * 1024)
    node.cpus_idle = 100
    result = explain(queued(cpus=8, mem_req="4G"), nodes=[node], partitions=[Partition("gpu", nodes=1)])
    assert result["placement"]["status"] == "unknown"
    assert "unknown_cpu_availability" in result["placement"]["nodes"][0]["checks"]


def test_satisfied_or_branch_is_not_invalidated_by_unneeded_branch_cycle():
    owner = queued("90", dependency="afterok:1?afterok:2")
    result = explain(owner, jobs=[queued("2", dependency="afterok:3"), queued("3", dependency="afterok:2")], finished=[done("1")])
    assert any(t["cycle"] for t in result["dependencies"]["tree"])
    assert result["dependencies"]["status"] == "satisfied"


def test_target_limit_applies_to_entire_expression_not_each_clause():
    text = ",".join("afterok:" + ":".join(str(i) for i in range(1, 50)) for _ in range(6))
    result = explain(queued(dependency=text))
    assert sum(len(c["targets"]) for c in result["dependencies"]["clauses"]) <= 128
    assert result["truncated"] and result["dependencies"]["status"] == "unknown"


def test_detail_dependency_fallback_is_used_without_mutating_job():
    owner = queued()
    result = explain(owner, details={"Dependency": "afterok:1"}, finished=[done("1", "FAILED")])
    assert result["dependencies"]["status"] == "impossible"
    assert owner.dependency == ""


def test_input_shape_errors_do_not_require_slurm_or_filesystem_access():
    result = explain(None, jobs="not a list", finished=9, partitions=None, nodes=False, share=1, details=[], health=[])
    assert result["status"] == "unknown" and result["state"] == ""
    json.dumps(result, allow_nan=False)


def test_special_exit_is_requeued_held_state_not_terminal_failure():
    result = explain(queued(dependency="afterany:1"), jobs=[{"id": "1", "state": "SPECIAL_EXIT"}])
    assert clause(result)["status"] == "waiting"
    success = explain(queued(dependency="afterok:1"), jobs=[{"id": "1", "state": "SPECIAL_EXIT"}])
    assert clause(success)["status"] == "waiting"


def test_null_reason_is_unknown_rather_than_unrecognized_scheduler_code():
    result = explain(queued(reason="(null)"))
    assert result["status"] == "unknown" and result["blockers"] == []


@pytest.mark.parametrize("state", ["UNKNOWN", "N/A", "future_unrecognized_state"])
def test_unrecognized_state_never_claims_scheduler_is_clear(state):
    result = explain({"id": "1", "state": state})
    assert result["status"] == "unknown"
    assert "unrecognized" in result["summary"]


def test_missing_configured_memory_defaults_do_not_prove_zero_memory_capacity():
    node = NodeCell(name="a", partitions=["gpu"], state="idle", cpus=32, cpus_idle=32)
    result = explain(queued(cpus=4, mem_req="4G"), nodes=[node], partitions=[Partition("gpu", nodes=1)])
    assert result["placement"]["status"] == "unknown"
    assert "unknown_memory_reservations" in result["placement"]["nodes"][0]["checks"]


def test_missing_configured_cpu_defaults_do_not_prove_cpu_exhaustion():
    node = NodeCell(name="a", partitions=["gpu"], state="idle", mem=32 * 1024)
    result = explain(queued(cpus=4, mem_req="4G"), nodes=[node], partitions=[Partition("gpu", nodes=1)])
    assert result["placement"]["status"] == "unknown"
    assert "unknown_cpu_availability" in result["placement"]["nodes"][0]["checks"]
