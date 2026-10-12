"""Failure explanations must stay bounded, scoped, truthful, and inspectable."""
from __future__ import annotations

import json
import pytest

from raven.investigate import MAX_EVENTS, MAX_EVIDENCE, MAX_LOG_BYTES, MAX_LOG_LINES, investigate, sanitize_text
from raven.model import Finished, Job


def finished(state="FAILED", **kwargs):
    return Finished(id="123_4", state=state, **kwargs)


def hypothesis(case, key):
    return next(h for h in case["hypotheses"] if h["key"] == key)


def test_explicit_slurm_oom_and_gpu_oom_are_different_explanations():
    host = investigate(finished("OUT_OF_MEMORY"), stderr="slurmstepd: error: Detected 1 oom_kill event in StepId=123_4.batch cgroup")
    assert hypothesis(host, "host_oom")["confidence"] == "strong"
    assert not any(h["key"] == "gpu_oom" for h in host["hypotheses"])
    gpu = investigate(finished(), stderr="torch.cuda.OutOfMemoryError: CUDA out of memory. Tried to allocate 1.0 GiB")
    assert hypothesis(gpu, "gpu_oom")["confidence"] == "strong"
    assert not any(h["key"] == "host_oom" for h in gpu["hypotheses"])
    assert "Slurm --mem changes host memory" in " ".join(hypothesis(gpu, "gpu_oom")["next_checks"])


def test_application_memory_exception_does_not_claim_scheduler_oom():
    case = investigate(finished(), stderr="MemoryError: allocation failed")
    assert hypothesis(case, "host_app_oom")["confidence"] == "moderate"
    assert not any(h["key"] == "host_oom" for h in case["hypotheses"])


@pytest.mark.parametrize("state,key", [("TIMEOUT", "timeout"), ("NODE_FAIL", "node"), ("PREEMPTED", "preempt"), ("CANCELLED", "cancel"), ("DEADLINE", "cancel"), ("BOOT_FAIL", "node")])
def test_scheduler_termination_has_evidence_but_no_automatic_action(state, key):
    case = investigate(finished(state))
    assert case["status"] == "failure"
    assert hypothesis(case, key)["confidence"] == "strong"
    assert case["evidence"][0]["id"] in hypothesis(case, key)["support"]
    assert hypothesis(case, key)["next_checks"]


def test_completed_traceback_is_a_warning_with_scheduler_contradiction():
    case = investigate(finished("COMPLETED", exit="0:0"), stderr="Traceback (most recent call last):\nValueError: failed to read an optional cache\nrecovered successfully")
    assert case["state"] == "COMPLETED" and case["status"] == "warning"
    app = hypothesis(case, "application")
    assert app["confidence"] == "weak"
    assert "Historical or recovered" in app["interpretation"]
    assert app["contradictions"] == ["E1"]
    assert "do not change" in case["summary"]


def test_generic_failed_token_is_never_a_certain_cause():
    case = investigate(Job(id="42", name="x", partition="p", state="RUNNING"), stdout="Optional cache failed; continuing")
    generic = hypothesis(case, "generic")
    assert generic["confidence"] == "weak"
    assert case["status"] == "warning"
    assert "not a confirmed" in case["summary"]


def test_missing_logs_do_not_establish_health_even_after_completion():
    case = investigate(finished("COMPLETED"))
    assert case["status"] == "completed"
    assert case["hypotheses"][0]["key"] == "unknown"
    assert "outputs remain unverified" in case["summary"]
    assert sum("excerpt was supplied" in s for s in case["limitations"]) == 2


def test_nonzero_exit_is_application_failure_but_not_memory_root_cause():
    case = investigate(finished(exit="137:0"))
    assert hypothesis(case, "application")["confidence"] == "moderate"
    assert not any(h["key"] in {"host_oom", "gpu_oom", "signal"} for h in case["hypotheses"])


def test_recorded_signal_is_not_equated_with_a_cause():
    case = investigate(finished(exit="0:9"))
    signal = hypothesis(case, "signal")
    assert signal["confidence"] == "strong"
    assert "not why" in " ".join(signal["next_checks"])
    assert not any(h["key"] == "host_oom" for h in case["hypotheses"])


@pytest.mark.parametrize("log,key", [
    ("OSError: [Errno 28] No space left on device", "storage"),
    ("write(): Disk quota exceeded", "quota"),
    ("PermissionError: Permission denied: input.dat", "permission"),
    ("NCCL WARN Cuda failure: unhandled system error", "communication"),
    ("MPI_ABORT was invoked: error from rank 0", "communication"),
    ("UCX ERROR connection reset by peer", "communication"),
    ("slurmstepd: JOB 123_4 CANCELLED DUE TO TIME LIMIT", "timeout"),
])
def test_storage_permission_and_distributed_errors_are_evidence_linked(log, key):
    case = investigate(finished(), stderr=log)
    item = hypothesis(case, key)
    evidence = {e["id"]: e for e in case["evidence"]}
    assert item["support"] and all(eid in evidence for eid in item["support"])
    assert any(evidence[eid]["source"] == "stderr" for eid in item["support"])


def test_stderr_first_error_precedes_stdout_and_retains_original_locations():
    case = investigate(finished(), stderr={"text": "hello\nValueError: broken input\nRuntimeError: later consequence", "path": "/scratch/job.err", "first_line": 500}, stdout="NCCL ERROR: abort")
    first = next(e for e in case["evidence"] if e["id"] == case["first_error"])
    assert first["source"] == "stderr" and first["line"] == 501
    assert first["location"] == "/scratch/job.err:501"
    assert first["line_basis"] == "original"


def test_unknown_log_offsets_are_explicitly_tail_relative():
    case = investigate(finished(), stderr="warmup\nRuntimeError: failed")
    error = next(e for e in case["evidence"] if e["source"] == "stderr")
    assert error["line"] == 2 and error["line_basis"] == "tail-relative"
    assert "tail-relative line 2" in error["location"]


def test_error_after_long_debug_prefix_keeps_relevant_bounded_context():
    case = investigate(finished(), stderr={"text": "debug token " * 1500 + "CUDA out of memory: allocation failed", "first_line": 42})
    gpu = hypothesis(case, "gpu_oom")
    selected = next(e for e in case["evidence"] if e["id"] in gpu["support"])
    assert "CUDA out of memory" in selected["text"]
    assert len(selected["text"]) <= 900 and selected["excerpt_offset"] > 0
    assert selected["line"] == 42 and selected["text"].startswith("... ")


def test_event_scope_does_not_mix_jobs_or_array_siblings():
    events = [{"job": "123_5", "text": "NCCL ERROR broken", "t": 1},
              {"job": "123", "text": "node failure", "t": 2},
              {"text": "GPU out of memory", "t": 3},
              {"job": "123_4.batch", "text": "disk quota exceeded", "t": 4},
              {"job_id": "123_4", "text": "job preempted", "t": 5}]
    case = investigate(finished(), events=events)
    assert {e["timestamp"] for e in case["evidence"] if e["source"] == "event"} == {4, 5}
    assert not any(h["key"] == "communication" for h in case["hypotheses"])
    assert {h["key"] for h in case["hypotheses"]} >= {"quota", "preempt"}
    assert [x["timestamp"] for x in case["chronology"]] == [4, 5]


def test_chronology_orders_comparable_times_without_guessing_naive_timezones():
    case = investigate(finished(end="2026-10-03T13:15:00"), events=[
        {"job": "123_4", "text": "preempted", "t": 20},
        {"job": "123_4", "text": "disk quota exceeded", "t": "1970-01-01T00:00:10Z"},
        {"job": "123_4", "text": "node failure", "t": "uncertain"},
    ])
    assert [e["timestamp"] for e in case["chronology"]] == ["1970-01-01T00:00:10Z", 20, "2026-10-03T13:15:00", "uncertain"]
    assert [e["order_basis"] for e in case["chronology"]] == ["timestamp", "timestamp", "source-order", "source-order"]
    assert "other timestamps retain source order" in case["chronology_order"]


def test_mismatched_scheduler_details_are_ignored():
    case = investigate(finished("COMPLETED"), details={"JobId": "999", "JobState": "OUT_OF_MEMORY", "ExitCode": "1:9"})
    assert case["status"] == "completed"
    assert not any(h["key"] == "host_oom" for h in case["hypotheses"])
    assert any("another job" in s for s in case["limitations"])


def test_disagreeing_scheduler_records_are_explicit():
    case = investigate(finished("COMPLETED"), details={"JobId": "123_4", "JobState": "FAILED"}, stderr="ValueError: bad")
    assert case["state"] == "COMPLETED"
    assert len(hypothesis(case, "application")["contradictions"]) == 2
    assert any("disagree" in s for s in case["limitations"])


def test_unicode_and_terminal_controls_are_sanitized_but_readable_text_remains():
    log = "\x1b[31mRuntimeError:\x1b[0m 🚀 café\x00\x07\x1b]52;c;c2VjcmV0\x07 \u202etrick\x9b\ud800"
    case = investigate(finished(), stderr={"text": log, "path": "err\x1b[2Jfile", "first_line": 1})
    encoded = json.dumps(case, ensure_ascii=False, allow_nan=False)
    assert "🚀 café" in encoded and "c2VjcmV0" not in encoded
    assert not any(c in encoded for c in "\x1b\x00\x07\u202e\x9b\ud800")
    assert sanitize_text("a\tb\r\nc") == "a    b\nc"


def test_huge_byte_and_line_inputs_have_bounded_examination_and_output():
    stderr = ("noise\n" * 1_000_000) + ("RuntimeError: " + "x" * 4000 + "\n") * 1000
    case = investigate(finished(), stderr={"text": stderr, "first_line": 1})
    meta = next(m for m in case["log_sources"] if m["source"] == "stderr")
    assert meta["bytes_examined"] <= MAX_LOG_BYTES and meta["lines_examined"] <= MAX_LOG_LINES
    assert meta["truncated"] and meta["first_line"] is None
    assert len(case["evidence"]) <= MAX_EVIDENCE
    assert len(json.dumps(case)) < 200_000


def test_dense_newlines_and_unicode_are_bounded_without_corrupting_locations():
    text = "\n" * 1400 + "RuntimeError: café\n"
    case = investigate(finished(), stderr={"text": text, "first_line": 20})
    error = next(e for e in case["evidence"] if e["source"] == "stderr")
    assert error["line"] == 1420 and error["line_basis"] == "original"
    unicode_tail = investigate(finished(), stderr="🚀" * 100_000 + "\nRuntimeError: broken\n")
    assert unicode_tail["log_sources"][0]["bytes_examined"] <= MAX_LOG_BYTES


def test_infinite_event_iterator_is_bounded():
    import itertools
    case = investigate(finished(), events=itertools.repeat({"job": "123_4", "text": "no space left on device", "t": 3}))
    assert len([e for e in case["evidence"] if e["source"] == "event"]) <= min(MAX_EVENTS, MAX_EVIDENCE)


def test_nan_malformed_metadata_and_nonmatching_artifacts_do_not_crash_or_leak():
    case = investigate({"id": 42, "state": float("nan"), "ExitCode": []},
                       live={"rss": float("nan")},
                       events=[None, [], {"job": 42, "text": "optional failed", "t": float("nan")}],
                       stderr={"text": b"ValueError: invalid\xff\n", "first_line": float("nan"), "path": {}},
                       artifact_results=[{"job_id": "99", "path": "unrelated", "ok": False}, {"valid": True}])
    assert case["job_id"] == "42" and case["state"] == "UNKNOWN"
    assert "unrelated" not in json.dumps(case, allow_nan=False)
    assert "\ufffd" in json.dumps(case, ensure_ascii=False)


def test_malformed_model_properties_and_enormous_integer_metadata_are_safe():
    bad_job = Job(id="123_4", name="x", partition="p", state="RUNNING", mem_req=float("nan"))
    case = investigate(bad_job, live={"rss": 95}, events=[{"job": "123_4", "text": "optional failed", "t": 10 ** 1000}])
    assert not case["chronology"]
    assert not any(h["key"] == "pressure" for h in case["hypotheses"])
    with pytest.raises(ValueError):
        investigate({"id": 10 ** 10_000})


def test_memory_correlation_is_weak_and_artifact_failures_remain_distinct():
    case = investigate(finished("COMPLETED", req_mem=100, rss=95), artifact_results={"results": [{"path": "result.csv", "valid": False, "reason": "3 columns expected"}]})
    assert hypothesis(case, "pressure")["confidence"] == "weak"
    assert hypothesis(case, "artifacts")["confidence"] == "strong"
    assert not hypothesis(case, "artifacts")["contradictions"]
    assert not any(h["key"] == "host_oom" for h in case["hypotheses"])


def test_actual_artifact_schema_preserves_failed_check_evidence():
    case = investigate(finished("COMPLETED"), artifact_results={"status": "invalid", "valid": False, "summary": "One output failed validation", "outputs": [
        {"path": "results.csv", "status": "invalid", "checks": [{"name": "csv", "status": "fail", "message": "Missing column: accuracy"}]},
        {"path": "optional.json", "status": "valid", "missing": True, "checks": []},
    ], "observed_at": "2026-10-03T12:15:00Z"})
    artifact = hypothesis(case, "artifacts")
    assert artifact["confidence"] == "strong" and not artifact["contradictions"]
    selected = [e for e in case["evidence"] if e["source"] == "artifact"]
    assert len(selected) == 1 and selected[0]["text"] == "Missing column: accuracy"
    assert selected[0]["timestamp"] == "2026-10-03T12:15:00Z"


def test_passed_output_checks_are_retained_without_claiming_scientific_correctness():
    case = investigate(finished("COMPLETED"), artifact_results={"status": "valid", "valid": True, "summary": "All declared checks passed", "outputs": [{"path": "results.csv", "status": "valid", "checks": []}]})
    assert case["outputs_verified"] is True
    assert case["status"] == "completed"
    assert "contract checks passed" in case["summary"]
    assert "scientific correctness remains unverified" in case["summary"]
    assert any(e["source"] == "artifact" and "checks passed" in e["text"] for e in case["evidence"])


@pytest.mark.parametrize("status", ["not_checked", "incomplete", "error"])
def test_unavailable_artifact_checks_do_not_establish_output_failure(status):
    case = investigate(finished("COMPLETED"), artifact_results={"status": "incomplete", "valid": False, "outputs": [
        {"path": "results.csv", "status": status, "checks": [{"status": "fail", "message": "old data replaced concurrently"}]},
    ]})
    assert hypothesis(case, "artifact_unverified")["confidence"] == "weak"
    assert not any(h["key"] == "artifacts" for h in case["hypotheses"])


def test_enormous_line_offset_is_ignored_and_output_remains_json_safe():
    case = investigate(finished(), stderr={"text": "ValueError: invalid", "first_line": 10 ** 10000})
    assert case["log_sources"][0]["line_basis"] == "tail-relative"
    json.dumps(case, allow_nan=False)


@pytest.mark.parametrize("job,kwargs,exception", [
    (None, {}, TypeError), ("123", {}, TypeError), ({}, {}, ValueError),
    ({"id": "1" * 129}, {}, ValueError), ({"id": "1\x1b[31m"}, {}, ValueError),
    ({"id": "1"}, {"details": []}, TypeError),
    ({"id": "1"}, {"stderr": 123}, TypeError),
    ({"id": "1"}, {"events": "failed"}, TypeError),
    ({"id": "1"}, {"events": None}, TypeError),
])
def test_invalid_api_inputs_produce_clear_errors(job, kwargs, exception):
    with pytest.raises(exception):
        investigate(job, **kwargs)


def test_inputs_are_not_mutated_and_every_reference_resolves():
    job = {"id": "123_4", "state": "TIMEOUT"}
    details = {"Reason": "TimeLimit", "ExitCode": "0:15"}
    event = {"job": "123_4", "text": "preempted", "timestamp": "2026-10-03T12:15:00Z"}
    originals = json.dumps([job, details, event], sort_keys=True)
    case = investigate(job, details=details, events=[event], stderr="CUDA out of memory")
    assert json.dumps([job, details, event], sort_keys=True) == originals
    ids = {e["id"] for e in case["evidence"]}
    for item in case["hypotheses"]:
        assert set(item["support"] + item["contradictions"]) <= ids
    assert all(item["evidence_id"] in ids for item in case["chronology"])
    assert case["first_error"] in ids
