"""Independent interval oracles, contamination/censoring checks and hard budgets."""
import itertools
import json
import math
import random
import statistics
import time

import pytest

from raven.model import Finished, Job
from raven.predict import predict


def run(i, **changes):
    return {"id": str(i), "name": "train", "partition": "gpu", "state": "COMPLETED",
            "cpus": 8, "nodes": 1, "gpus": 1, "runtime_seconds": 100.0 + i,
            "memory_bytes": 1024.0 * (100 + i), "memory_scope": "job_peak",
            "cpu_seconds": 500.0 + i, "end": 1700000000 + i, **changes}


def history(n=30, **changes):
    return [run(i, **changes) for i in range(n)]


def metric(result, key="runtime_seconds"):
    return result["metrics"][key]


def test_independent_split_conformal_oracle_and_honest_holdout():
    values = [10, 15, 11, 17, 12, 14, 13, 18, 19, 15,
              11, 12, 30, 13, 20, 19, 21, 22, 16, 17,
              13, 11, 12, 50, 19, 20, 15, 16, 17, 18]
    result = predict([run(i, runtime_seconds=x) for i, x in enumerate(values)])
    fit = metric(result)
    # 30 records: six strictly later holdouts; 12 train, 12 calibration.
    center = statistics.median(values[:12])
    rank = math.ceil(13 * .8)
    radius = sorted(abs(x - center) for x in values[12:24])[rank - 1]
    assert fit["estimate"] == center
    assert fit["lower"] == max(0, center - radius)
    assert fit["upper"] == center + radius
    assert fit["calibration_rank"] == rank
    assert fit["training_samples"] == fit["calibration_samples"] == 12
    assert fit["validation_samples"] == 6
    assert fit["observed_coverage"] == sum(fit["lower"] <= x <= fit["upper"] for x in values[24:]) / 6
    assert result["ordering"] == "end_time"


def test_finite_sample_rank_abstains_at_high_coverage():
    result = predict(history(10), coverage=.95)
    fit = metric(result)
    assert fit["samples"] == 10
    assert fit["calibration_samples"] == 5
    assert fit["status"] == "insufficient_calibration"
    assert fit["lower"] is fit["upper"] is None
    assert result["status"] == "insufficient"
    assert "finite-sample" in " ".join(result["limitations"])


def test_ten_samples_support_default_but_do_not_claim_measured_coverage():
    result = predict(history(10))
    fit = metric(result)
    assert fit["calibration_rank"] == 5
    assert fit["observed_coverage"] is None
    assert fit["validation_samples"] == 0
    assert fit["lower"] is not None
    assert fit["target_coverage"] == .8
    assert "Exchangeable" in result["coverage_assumption"]


def test_tiny_cohort_abstains():
    fit = metric(predict(history(9)))
    assert fit["samples"] == 9
    assert fit["estimate"] is fit["lower"] is fit["upper"] is None
    assert fit["status"] == "insufficient"


def test_correct_coverage_on_independent_exchangeable_future_observations():
    rng = random.Random(99311)
    hits = 0
    for _ in range(500):
        records = [run(i, runtime_seconds=100 + rng.gauss(0, 10)) for i in range(40)]
        fit = metric(predict(records))
        future = 100 + rng.gauss(0, 10)
        hits += fit["lower"] <= future <= fit["upper"]
    # Corrected rank is ceil(17 * .8) / 17 = 14 / 17.  A deliberately
    # generous deterministic tolerance checks the algorithm, not random luck.
    assert .75 < hits / 500 < .9


def test_recent_drift_is_reported_from_real_holdout():
    records = [run(i, runtime_seconds=10 if i < 24 else 500) for i in range(30)]
    result = predict(records)
    fit = metric(result)
    assert fit["lower"] == fit["upper"] == 10
    assert fit["observed_coverage"] == 0
    assert fit["status"] == "poor_validation"
    assert result["status"] == "partial"
    assert "drift" in " ".join(result["limitations"])


@pytest.mark.parametrize("key,other", [("name", "other"), ("partition", "cpu"), ("cpus", 16),
                                       ("nodes", 2), ("gpus", 2), ("gpu_type", "h100"),
                                       ("account", "different"), ("qos", "other"),
                                       ("input_size", 200), ("parameters", {"lr": .1}),
                                       ("script_sha256", "a" * 64)])
def test_unqueried_incompatible_cohorts_are_not_silently_pooled(key, other):
    records = history(15) + [run(100 + i, **{key: other}) for i in range(15)]
    result = predict(records)
    assert result["cohort"] is None
    assert result["cohorts_found"] == 2
    assert metric(result)["estimate"] is None


@pytest.mark.parametrize("key,original,other", [("name", "train", "other"), ("partition", "gpu", "cpu"),
                                                ("cpus", 8, 16), ("nodes", 1, 2), ("gpus", 1, 2),
                                                ("gpu_type", "a100", "h100"), ("input_size", 100, 200),
                                                ("parameters", {"lr": .01}, {"lr": .1}),
                                                ("script_sha256", "b" * 64, "a" * 64)])
def test_exact_query_selects_compatible_cohort(key, original, other):
    records = history(30, **{key: original}) + [run(100 + i, **{key: other}) for i in range(30)]
    result = predict(records, {key: original})
    assert metric(result)["samples"] == 30
    assert result["cohort"][key] == original
    assert result["exclusions"]["different_workload_or_allocation"] == 30
    assert "100" not in result["evidence"]


def test_query_missing_optional_identity_does_not_select_arbitrary_workload():
    result = predict(history(15, input_size=100) + [run(i + 100, input_size=200) for i in range(15)],
                     {"name": "train", "partition": "gpu", "cpus": 8, "nodes": 1, "gpus": 1})
    assert result["cohort"] is None
    assert result["status"] == "insufficient"


def test_missing_identity_is_not_a_concrete_allocation():
    records = history()
    for row in records:
        del row["cpus"]
    result = predict(records)
    assert result["cohort"] is None
    assert result["exclusions"]["invalid_or_ineligible_record"] == 30


def test_job_query_finished_records_and_per_task_memory_scope():
    records = [Finished(str(i), "train", "COMPLETED", "00:10:00", 8, 1, 1,
                        500, rss=1024**3, partition="gpu", end=f"2026-01-01T00:{i:02d}:00") for i in range(30)]
    query = Job("next", "train", "gpu", "PENDING", cpus=8, nodes=1, gpus=1)
    result = predict(records, query)
    assert metric(result)["estimate"] == 600
    memory = metric(result, "memory_bytes")
    assert memory["scope"] == "max_task_rss"
    assert memory["estimate"] == 1024**3
    assert "not the sum across ranks" in " ".join(result["limitations"])


@pytest.mark.parametrize("scope", [None, "allocation", "total", "", 4, [], {}])
def test_ambiguous_explicit_memory_scope_abstains(scope):
    result = predict(history(memory_scope=scope))
    fit = metric(result, "memory_bytes")
    assert fit["status"] == "unknown_scope"
    assert fit["estimate"] is fit["upper"] is None
    assert metric(result)["samples"] == 30


def test_mixed_memory_scopes_never_add_or_average_ranks():
    result = predict(history(15) + [run(100 + i, memory_scope="max_task_rss") for i in range(15)])
    assert metric(result, "memory_bytes")["status"] == "mixed_scopes"
    assert metric(result, "memory_bytes")["upper"] is None


def test_missing_memory_does_not_invent_zero():
    records = history()
    for row in records:
        row.pop("memory_bytes")
    fit = metric(predict(records), "memory_bytes")
    assert fit["samples"] == 0
    assert fit["estimate"] is None
    assert fit["status"] == "insufficient"


@pytest.mark.parametrize("state", ["TIMEOUT", "OUT_OF_MEMORY"])
def test_censored_runs_withhold_intervals_and_keep_lower_bound_evidence(state):
    result = predict(history() + [run(100, state=state, runtime_seconds=10000, memory_bytes=10**12)])
    assert result["censored_count"] == 1
    assert result["censored"][0]["lower_bounds"]["runtime_seconds"] == 10000
    for fit in result["metrics"].values():
        assert fit["estimate"] is fit["lower"] is fit["upper"] is None
        assert fit["status"] == "censored"
        assert fit["censored_samples"] == 1
    assert metric(result)["lower_bound"] == 10000
    assert result["status"] == "partial"


def test_censored_unknown_runtime_still_prevents_completion_selection_bias():
    result = predict(history() + [run(100, state="TIMEOUT", runtime_seconds=None)])
    assert metric(result)["lower_bound"] is None
    assert metric(result)["upper"] is None
    assert metric(result)["status"] == "censored"


@pytest.mark.parametrize("state", ["RUNNING", "PENDING", "FAILED", "CANCELLED", "NODE_FAIL", "PREEMPTED", ""])
def test_other_states_are_not_exact_completed_samples(state):
    result = predict(history() + [run(100, state=state, runtime_seconds=10000)])
    assert metric(result)["samples"] == 30
    assert result["exclusions"]["invalid_or_ineligible_record"] == 1


def test_nonzero_completed_exit_is_not_exact_success_evidence():
    result = predict(history() + [run(100, exit="1:0", runtime_seconds=999)])
    assert metric(result)["samples"] == 30
    assert "100" not in result["evidence"]


def test_duplicate_ids_do_not_inflate_calibration():
    records = history(10)
    result = predict(records * 50)
    assert metric(result)["samples"] == 10
    assert result["exclusions"]["duplicate_id"] == 490


def test_anonymous_identical_observations_deduplicate_without_fake_independence():
    records = history(10)
    for row in records:
        del row["id"]
    result = predict(records * 20)
    assert metric(result)["samples"] == 10
    assert all(x.startswith("anonymous:") for x in result["evidence"])


def test_latest_dated_duplicate_wins_independent_of_input_order():
    records = history()
    old = run(0, state="OUT_OF_MEMORY", end=1600000000)
    a, b = predict(records + [old]), predict([old] + records)
    assert metric(a)["samples"] == metric(b)["samples"] == 30
    assert a["censored_count"] == b["censored_count"] == 0
    assert metric(a)["upper"] == metric(b)["upper"]


def test_conflicting_undated_duplicate_abstains_instead_of_hiding_oom():
    records = history()
    records[0]["end"] = None
    result = predict(records + [run(0, state="OUT_OF_MEMORY", end=None)])
    assert result["status"] == "insufficient"
    assert metric(result)["upper"] is None
    assert result["exclusions"]["conflicting_duplicate_id"] == 1


def test_latest_failed_duplicate_does_not_leave_stale_completed_sample():
    result = predict(history() + [run(0, state="FAILED", end=1800000000)])
    assert metric(result)["samples"] == 29
    assert "0" not in result["evidence"]


def test_latest_contradictory_completed_exit_removes_stale_success():
    result = predict(history() + [run(0, exit="1:0", end=1800000000)])
    assert metric(result)["samples"] == 29
    assert "0" not in result["evidence"]


def test_steps_are_not_independent_runs():
    records = history()
    result = predict(records + [run(100, id="1.batch"), run(101, id="2.0"), run(102, id="3_4.extern")])
    assert metric(result)["samples"] == 30
    assert result["exclusions"]["invalid_or_ineligible_record"] == 3


def test_time_ordering_is_deterministic_and_input_independent_when_known():
    records = history()
    shuffled = records[:]
    random.Random(22).shuffle(shuffled)
    assert predict(records) == predict(shuffled)


def test_missing_times_report_input_order_limit():
    result = predict(history(end=None))
    assert result["ordering"] == "input_order"
    assert "not verified chronology" in " ".join(result["limitations"])


@pytest.mark.parametrize("value", [True, False, float("nan"), float("inf"), -1, "100", [], {}, 10**200, 1e200])
def test_invalid_numeric_measurements_remain_unknown_and_json_safe(value):
    result = predict(history(runtime_seconds=value, memory_bytes=value, cpu_seconds=value))
    assert all(fit["estimate"] is None for fit in result["metrics"].values())
    json.dumps(result, allow_nan=False)


def test_large_finite_measurements_do_not_overflow_interval():
    result = predict(history(runtime_seconds=1e100, memory_bytes=1e100, cpu_seconds=1e100))
    assert metric(result)["upper"] == 1e100
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize("options", [{"coverage": 0}, {"coverage": 1}, {"coverage": True}, {"coverage": "0.8"},
                                     {"coverage": float("nan")}, {"min_samples": 1}, {"min_samples": False},
                                     {"min_samples": 1.5}, {"max_records": 0}, {"max_records": 10**9}])
def test_invalid_controls_return_error(options):
    assert predict(history(), **options)["status"] == "error"


@pytest.mark.parametrize("value", ["text", b"bytes", {"id": "1"}, 4, None])
def test_malformed_history_returns_error(value):
    assert predict(value)["status"] == "error"


@pytest.mark.parametrize("query", [{"cpus": True}, {"cpus": 1.5}, {"nodes": 0}, {"gpus": -1},
                                  {"name": "\x1b[31m"}, {"input_size": float("nan")},
                                  {"script_sha256": "bad"}, {"parameters": [[[]]]}])
def test_invalid_query_returns_error(query):
    assert predict(history(), query)["status"] == "error"


def test_bounded_deep_and_large_parameter_trees():
    deep = {"x": {"x": {"x": {"x": {"x": 1}}}}}
    large = {str(i): i for i in range(1000)}
    for parameters in (deep, large, {"x": float("nan")}, {"x": 10**200}):
        assert predict(history(parameters=parameters))["cohort"] is None
        assert predict(history(), {"parameters": parameters})["status"] == "error"


def test_nested_parameter_identity_is_canonical_and_allows_negative_values():
    a = {"optimizer": {"lr": .001, "bias": -1}, "shape": [10, 20], "mixed": True}
    b = {"mixed": True, "shape": [10, 20], "optimizer": {"bias": -1, "lr": .001}}
    result = predict(history(parameters=a), {"parameters": b})
    assert result["cohort"]["parameters"] == a
    assert metric(result)["samples"] == 30


def test_record_budget_does_not_consume_one_extra_generator_item():
    seen = []

    def rows():
        for i in itertools.count():
            seen.append(i)
            yield run(i)

    result = predict(rows(), max_records=20)
    assert len(seen) == result["records_considered"] == 20
    assert result["truncated"]
    assert metric(result)["samples"] == 20


def test_exact_list_budget_is_not_reported_as_truncation():
    assert not predict(history(20), max_records=20)["truncated"]
    assert predict(history(21), max_records=20)["truncated"]


def test_iteration_failure_does_not_publish_partial_prediction():
    def broken():
        yield from history()
        raise RuntimeError("sensitive details")

    result = predict(broken())
    assert result["status"] == "error"
    assert metric(result)["upper"] is None
    assert "sensitive" not in json.dumps(result)


def test_duration_and_timestamp_fallbacks_do_not_execute_or_guess():
    records = history()
    for row in records:
        row.pop("runtime_seconds")
        row["elapsed"] = "1-01:02:03.5"
    assert metric(predict(records))["estimate"] == 90123.5
    for row in records:
        row["elapsed"] = "invalid"
        row["start"] = row["end"] - 10
    assert metric(predict(records))["estimate"] == 10
    for row in records:
        row["elapsed"] = "00:99:99"
        row["start"] = row["end"] + 10
    assert metric(predict(records))["estimate"] is None


def test_large_history_and_evidence_are_bounded_and_fast():
    records = history(10000)
    start = time.perf_counter()
    result = predict(records)
    assert time.perf_counter() - start < 8
    assert len(result["evidence"]) == 512
    assert result["evidence_count"] == 10000
    assert result["evidence_truncated"]
    assert metric(result)["samples"] == 10000


def test_available_cohort_listing_is_bounded():
    result = predict([run(i, name=f"task-{i}") for i in range(1000)])
    assert result["cohorts_found"] == 1000
    assert len(result["available_cohorts"]) == 32


@pytest.mark.parametrize("field,a,b", [("mem_bytes", 1024**3, 2*1024**3), ("time_seconds", 1000, 2000)])
def test_request_changes_are_not_assumed_equivalent(field, a, b):
    records = history(15, **{field: a}) + [run(i+100, **{field: b}) for i in range(15)]
    assert predict(records)["cohort"] is None
    assert metric(predict(records, {field: a}))["samples"] == 15


def test_slurm_memory_and_time_request_aliases_preserve_exact_identity():
    records = [Finished(str(i), "train", "COMPLETED", "00:10:00", 8, 1, 1,
                        500, req_mem=4*1024**3, rss=1024**3, partition="gpu", limit="00:20:00") for i in range(15)]
    query = Job("next", "train", "gpu", "PENDING", cpus=8, nodes=1, gpus=1,
                mem_req="512Mc", limit="00:20:00")
    result = predict(records, query)
    assert metric(result)["samples"] == 15
    assert result["cohort"]["mem_bytes"] == 4*1024**3
    assert result["cohort"]["time_seconds"] == 1200


def test_numerically_equal_int_and_float_allocations_are_one_cohort():
    result = predict(history(15, mem_bytes=1024, time_seconds=1000, input_size=20)
                     + [run(i+100, mem_bytes=1024.0, time_seconds=1000.0, input_size=20.0) for i in range(15)])
    assert metric(result)["samples"] == 30
    assert result["cohort"]["mem_bytes"] == 1024


def test_missing_query_identity_is_explained_without_weakening_match():
    result = predict(history(), {"account": "lab-account", "gpu_type": "a100"})
    assert result["cohort"] is None
    assert result["unproven_query_fields"] == {"account": 30, "gpu_type": 30}
    assert "account, gpu_type" in " ".join(result["limitations"])


def test_explicit_query_memory_scope_is_not_silently_ignored():
    result = predict(history(memory_scope="max_task_rss"), {"memory_scope": "job_peak"})
    assert metric(result, "memory_bytes")["status"] == "scope_mismatch"
    assert metric(result, "memory_bytes")["estimate"] is None
    assert metric(result)["samples"] == 30


def test_known_query_memory_scope_can_select_compatible_measurements_only():
    result = predict(history(15) + [run(i+100, memory_scope="max_task_rss") for i in range(15)],
                     {"memory_scope": "job_peak"})
    assert metric(result, "memory_bytes")["samples"] == 15
    assert metric(result, "memory_bytes")["scope"] == "job_peak"
    assert metric(result)["samples"] == 30


def test_censored_memory_bounds_are_not_combined_across_scopes():
    result = predict(history() + [run(100, state="OUT_OF_MEMORY", memory_scope="max_task_rss", memory_bytes=1e12)],
                     {"memory_scope": "job_peak"})
    assert metric(result, "memory_bytes")["status"] == "censored"
    assert metric(result, "memory_bytes")["lower_bound"] is None
    assert metric(result, "memory_bytes")["upper"] is None
