"""Sampling evidence never invents frequency, capability, or job identity."""
from copy import deepcopy
import json
import math

import pytest

from raven import telemetry as T


CONFIG = """ClusterName = research
JobAcctGatherType = jobacct_gather/cgroup
JobAcctGatherFrequency = task=30,energy=0
AccountingStorageTRES = cpu,mem,gres/gpu
CgroupPlugin = cgroup/v2
PrivateKey = DO NOT PUBLISH
"""
JOB = "JobId=17 JobState=RUNNING SubmitTime=2026-01-01T12:00:00 StartTime=2026-01-01T12:05:00 AcctGatherFrequency=task=5"


class Backend:
    def __init__(self, config=CONFIG, job=JOB):
        self.config, self.job, self.calls = config, job, []

    def run(self, argv, timeout=8):
        self.calls.append((list(argv), timeout))
        value = self.config if argv == ["scontrol", "show", "config"] else self.job
        if isinstance(value, Exception):
            raise value
        return value, .01


@pytest.mark.parametrize("value,expected", [
    ("30", {"task": 30}), ("0", {"task": 0}),
    ("task=1,energy=0,network=2,filesystem=3", {"task": 1, "energy": 0, "network": 2, "filesystem": 3}),
    ("task=1,task=5", {"task": None}), ("task=bad", {"task": None}),
    ("task=-1", {"task": None}), ("task=1.5", {"task": None}),
    ("task=nan", {"task": None}), ("task=inf", {"task": None}),
    ("task=١", {"task": None}), ("task=9999999999", {"task": None}),
    ("task=3,new_plugin=4", {"task": 3}), ("task", {"task": None}),
    ("none", {}), ("(null)", {}), ("UNKNOWN", {}), ("", {}),
    (None, {}), (30, {}), (True, {}), ([], {}), ("9" * 1001, {}),
])
def test_frequency_parser(value, expected):
    assert T.parse_frequency(value) == expected


@pytest.mark.parametrize("value", ["17", "17_3", "17+1", "17_3+1", "99999999999999999999", ""])
def test_exact_ids(value):
    assert T.validate_job_id(value) == value


@pytest.mark.parametrize("value", ["--help", "17;id", "17\n18", "17.0", "17_[1-5]", "17,18", "-1", "17/3", " 17", "١٧", "1" * 21, True, 17, []])
def test_invalid_ids_rejected_before_commands(value):
    backend = Backend()
    with pytest.raises(ValueError):
        T.Inspector().inspect(backend, scope="s", job_id=value)
    assert backend.calls == []


def test_parser_only_reports_allowlisted_config_and_strips_controls():
    parsed = T.parse_config(CONFIG + "TaskPlugin = task/cgroup\x1b[31m\x07\n")
    assert "PrivateKey" not in parsed
    assert parsed["ClusterName"] == "research"
    assert "\x1b" not in parsed["TaskPlugin"]
    assert "\x07" not in parsed["TaskPlugin"]


def test_conflicting_duplicate_evidence_is_unknown():
    assert T.parse_config("JobAcctGatherFrequency = 30\nJobAcctGatherFrequency = 5")["JobAcctGatherFrequency"] == "unknown"
    assert T.parse_job("JobId=17 AcctGatherFrequency=task=5 AcctGatherFrequency=task=1")["AcctGatherFrequency"] == "unknown"


@pytest.mark.parametrize("output", [None, b"x", "x" * (T.MAX_OUTPUT + 1)])
def test_config_rejects_invalid_or_oversized_output(output):
    with pytest.raises(ValueError):
        T.parse_config(output)


@pytest.mark.parametrize("output", ["", "No such job", "JobId=17\nJobId=18", "JobId=17 JobId=17", b"JobId=17"])
def test_job_parser_requires_one_record(output):
    with pytest.raises(ValueError):
        T.parse_job(output)


def test_default_is_not_assumed_and_repeated_values_do_not_infer_it():
    report = T.build_report({}, {}, intervals={"live": .5}, live_observation={"timestamp": 10})
    assert report["configured_task_seconds"] is None
    assert report["job_task_seconds"] is None
    assert report["metrics"][0]["producer_interval_seconds"] is None
    assert report["metrics"][0]["raven_interval_seconds"] == .5
    assert report["metrics"][0]["capability"] == "unknown"
    assert "30s" not in "\n".join(T.report_lines(report))


def test_known_default_is_distinct_from_unexposed_job_override():
    report = T.build_report(T.parse_config(CONFIG), {})
    assert report["configured_task_seconds"] == 30
    assert report["job_task_seconds"] is None
    assert "unknown" in report["metrics"][0]["note"]


@pytest.mark.parametrize("field", ["AcctGatherFrequency", "AcctGatherFreq", "JobAcctGatherFrequency"])
@pytest.mark.parametrize("frequency,wanted", [("task=5", 5), ("(null)", 30), ("energy=1", 30), ("task=0", 0), ("task=bad", None), ("invalid", None)])
def test_job_override_and_inheritance(field, frequency, wanted):
    report = T.build_report(T.parse_config(CONFIG), {field: frequency})
    assert report["job_task_seconds"] == wanted
    assert report["metrics"][0]["producer_interval_seconds"] == wanted


def test_gather_none_and_zero_have_different_meanings():
    report = T.build_report({"JobAcctGatherType": "jobacct_gather/none"}, {"AcctGatherFrequency": "task=5"})
    assert report["metrics"][0]["capability"] == "disabled"
    assert report["metrics"][0]["producer_interval_seconds"] is None
    report = T.build_report(T.parse_config(CONFIG), {"AcctGatherFrequency": "task=0"})
    assert report["metrics"][0]["capability"] == "configured"
    assert "termination" in report["metrics"][0]["note"]
    assert "disabled (0s)" in "\n".join(T.report_lines(report))


@pytest.mark.parametrize("plugin,group,status", [("jobacct_gather/cgroup", "cgroup/v2", "unsupported"), ("jobacct_gather/cgroup", "autodetect", "unknown"), ("jobacct_gather/cgroup", "cgroup/v1", "unknown"), ("jobacct_gather/linux", "cgroup/v2", "unknown")])
def test_virtual_memory_needs_compute_node_evidence(plugin, group, status):
    report = T.build_report({"JobAcctGatherType": plugin, "CgroupPlugin": group}, {})
    assert report["metrics"][2]["capability"] == status


def test_gpu_and_reported_files_never_inherit_task_frequency():
    report = T.build_report(T.parse_config(CONFIG), T.parse_job(JOB),
        intervals={"gpu": .5, "trace": 1.5, "research": 2}, gpu_observation={"devices": 1})
    gpu, trace, project = report["metrics"][3:]
    assert gpu["capability"] == "observed"
    assert all(row["producer_interval_seconds"] is None for row in (gpu, trace, project))
    assert [row["raven_interval_seconds"] for row in (gpu, trace, project)] == [.5, 1.5, 2]
    report = T.build_report({}, {}, gpu_enabled=False, gpu_observation={"devices": 1})
    assert report["metrics"][3]["capability"] == "disabled"


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -1, True, "0.5", 10**1000, None])
def test_invalid_interval_and_health_values_are_unknown(value):
    report = T.build_report({}, {}, intervals={"live": value}, health={"live": {"last_ok": value, "backoff": value}})
    assert report["metrics"][0]["raven_interval_seconds"] is None
    assert report["metrics"][0]["last_success_at"] is None
    assert report["metrics"][0]["backoff_seconds"] is None


def test_health_backoff_is_evidence_not_a_new_sampling_frequency():
    report = T.build_report({}, {}, intervals={"live": .5}, observed_at=100,
        health={"live": {"last_ok": 80, "backoff": 60, "error": "permission denied", "enabled": False}})
    text = "\n".join(T.report_lines(report))
    assert "20s before snapshot" in text and "backoff: 60s" in text
    assert "permission denied" in text and "collector is disabled" in text
    assert report["metrics"][0]["raven_interval_seconds"] == .5


def test_cache_ttl_refresh_and_connection_isolation():
    clock = [10.0]
    inspector = T.Inspector(ttl=30, clock=lambda: clock[0])
    backend = Backend()
    first = inspector.inspect(backend, scope="one", job_id="17")
    assert not first["config_cached"] and len(backend.calls) == 2
    first["config"]["ClusterName"] = "mutated"
    second = inspector.inspect(backend, scope="one", job_id="17")
    assert second["config_cached"] and second["cluster"] == "research" and len(backend.calls) == 3
    inspector.inspect(backend, scope="one", refresh=True)
    assert len(backend.calls) == 4
    inspector.inspect(backend, scope="two")
    assert len(backend.calls) == 5
    clock[0] = 40
    assert not inspector.inspect(backend, scope="one")["config_cached"]
    clock[0] = 5
    assert not inspector.inspect(backend, scope="one")["config_cached"]
    assert all(timeout == 4 for _, timeout in backend.calls)


def test_cache_is_bounded():
    inspector = T.Inspector()
    backend = Backend()
    for scope in range(30):
        inspector.inspect(backend, scope=scope)
    assert len(inspector.cache) == T.MAX_CONNECTIONS
    assert list(inspector.cache) == list(range(22, 30))


@pytest.mark.parametrize("failure", [RuntimeError("SSH timed out"), OSError("not found"), ValueError("permission denied")])
def test_command_failures_remain_partial_evidence_and_are_cached(failure):
    backend = Backend(config=failure)
    inspector = T.Inspector()
    report = inspector.inspect(backend, scope="remote", job_id="17")
    assert report["job"]["JobId"] == "17" and report["configured_task_seconds"] is None
    assert str(failure) in report["errors"][0]
    second = inspector.inspect(backend, scope="remote")
    assert second["errors"] and second["config_cached"]
    assert len(backend.calls) == 2


def test_job_failure_does_not_drop_config_or_fabricate_effective_cadence():
    report = T.Inspector().inspect(Backend(job=RuntimeError("Invalid job id")), scope="s", job_id="17")
    assert report["job"] == {} and report["job_task_seconds"] is None
    assert report["configured_task_seconds"] == 30 and report["errors"]


@pytest.mark.parametrize("job,expected,accepted", [
    ("JobId=18 AcctGatherFrequency=task=1", {}, False),
    (JOB, {"submit": "2026-01-02"}, False),
    (JOB, {"state": "RUNNING", "start": "2026-01-01T12:04:00"}, False),
    (JOB, {"state": "PENDING", "start": "2026-01-01T12:04:00"}, True),
    (JOB, {"state": "RUNNING", "start": "2026-01-01T12:05:00"}, True),
])
def test_mismatched_job_or_attempt_cannot_supply_override(job, expected, accepted):
    report = T.Inspector().inspect(Backend(job=job), scope="s", job_id="17", expected=expected)
    assert bool(report["job"]) is accepted
    assert (report["job_task_seconds"] is not None) is accepted


def test_array_allocation_id_requires_explicit_parent_task_evidence():
    details = T.parse_job("JobId=123 ArrayJobId=17 ArrayTaskId=3 AcctGatherFrequency=task=1")
    assert T.matches_job(details, "17_3")
    assert not T.matches_job(details, "17_4")
    assert not T.matches_job(details, "17")


def test_heterogeneous_component_id_requires_explicit_leader_offset_evidence():
    details = T.parse_job("JobId=18 HetJobId=17 HetJobOffset=1 AcctGatherFrequency=task=1")
    assert T.matches_job(details, "17+1")
    assert not T.matches_job(details, "17+2")
    assert not T.matches_job(details, "17")
    report = T.Inspector().inspect(Backend(job="JobId=18 HetJobId=17 HetJobOffset=1 AcctGatherFrequency=task=2"), scope="s", job_id="17+1")
    assert report["job_task_seconds"] == 2
    assert not report["errors"]


def test_expected_attempt_requires_timestamp_evidence():
    details = T.parse_job("JobId=17 AcctGatherFrequency=task=1")
    assert not T.matches_job(details, "17", {"submit": "2026-01-01T00:00:00"})
    assert not T.matches_job(details, "17", {"state": "COMPLETED", "start": "2026-01-01T01:00:00"})


def test_cluster_mismatch_cannot_supply_another_jobs_override():
    report = T.Inspector().inspect(Backend(), scope="s", job_id="17", expected={"cluster": "other"})
    assert not report["job"]
    assert "different cluster" in report["errors"][-1]


def test_serializable_report_preserves_unknown_and_never_exposes_arbitrary_config():
    report = T.Inspector().inspect(Backend(), scope="s", job_id="17")
    payload = json.dumps(report, allow_nan=False)
    assert "DO NOT PUBLISH" not in payload and "null" in payload
    assert json.loads(payload)["schema"] == T.SCHEMA
