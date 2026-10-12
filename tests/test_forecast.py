"""Queue forecasts must abstain honestly and never learn from future starts."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import math
import random

import pytest

from raven.forecast import ForecastTracker, MIN_CALIBRATION, forecast
from raven.model import Finished, Job

NOW = 100_000.


def job(**overrides):
    data = dict(id="target", name="train", partition="gpu", account="research", qos="normal",
                state="PENDING", submit=NOW - 100, est_start=NOW + 100,
                cpus=4, nodes=1, gpus=1, gpu_type="a100", mem_bytes=16 * 1024**3,
                time_seconds=3600, reason="Resources", dependency="")
    data.update(overrides)
    return data


def observation(index, error=30, **overrides):
    data = job(id=str(index), submit=1000 + index, est_start="")
    data.pop("id")
    data.update(job_id=str(index), issued_at=2000 + index * 10,
                predicted_start=2100 + index * 10, actual_start=2100 + index * 10 + error,
                first_issued_at=2000 + index * 10, first_predicted_start=2100 + index * 10,
                last_observed_at=2200 + index * 10, revision_count=0)
    data.update(overrides)
    return data


def history(index, wait=1000, **overrides):
    data = job(id=str(index), submit=20_000 + index * 10 - wait,
               start=20_000 + index * 10, state="COMPLETED", est_start="")
    data.update(overrides)
    return data


def test_scheduler_point_is_distinguished_from_observed_calibration():
    result = forecast(job(), now=NOW)
    assert result["status"] == "forecast"
    assert result["method"] == "scheduler_uncalibrated"
    assert result["predicted_start"] == NOW + 100
    assert result["wait_seconds"] == 100
    assert result["lower_start"] is None and result["upper_start"] is None
    assert not result["calibration"]["valid"]
    assert "uncalibrated" in " ".join(result["limitations"])


def test_scheduler_split_conformal_interval_uses_actual_pre_start_errors():
    result = forecast(job(), observations=[observation(i, i + 1) for i in range(20)], now=NOW)
    assert result["method"] == "scheduler_conformal"
    assert result["samples"] == 20
    assert result["lower_start"] == NOW + 100 - 17
    assert result["upper_start"] == NOW + 100 + 17
    assert result["calibration"]["requested_coverage"] == .8
    assert result["calibration"]["empirical_coverage"] is None
    assert result["calibration"]["coverage_trials"] == 0


def test_more_history_does_not_fabricate_recorded_scheduler_accuracy():
    result = forecast(job(), [history(i) for i in range(100)], now=NOW)
    assert result["method"] == "scheduler_uncalibrated"
    assert result["samples"] == 0


@pytest.mark.parametrize("count", [0, 1, 5, 19])
def test_small_calibration_samples_never_produce_an_interval(count):
    result = forecast(job(), observations=[observation(i) for i in range(count)], now=NOW)
    assert result["lower_start"] is None
    assert result["upper_start"] is None
    assert result["calibration"]["count"] == count


def test_extreme_requested_coverage_abstains_instead_of_capping_quantile():
    result = forecast(job(), observations=[observation(i) for i in range(20)], now=NOW, coverage=.999)
    assert result["method"] == "scheduler_uncalibrated"
    assert not result["calibration"]["valid"]


def test_chronological_coverage_is_honest_out_of_sample():
    records = [observation(i, 5 if i < 20 else 20) for i in range(21)]
    result = forecast(job(), observations=records, now=NOW)
    assert result["calibration"]["coverage_trials"] == 1
    assert result["calibration"]["empirical_coverage"] == 0
    # A calibration-fit score would be 20/21; the held-out outcome missed.


def test_coverage_tree_matches_simple_chronological_oracle():
    rng = random.Random(782)
    errors = [rng.randrange(0, 500) for _ in range(200)]
    expected = []
    for i in range(MIN_CALIBRATION, len(errors)):
        rank = math.ceil((i + 1) * .8)
        expected.append(errors[i] <= sorted(errors[:i])[rank - 1])
    records = [observation(i, issued_at=10000 + i * 1000 - error - 100,
                           predicted_start=10000 + i * 1000 - error,
                           actual_start=10000 + i * 1000) for i, error in enumerate(errors)]
    result = forecast(job(submit=999900, est_start=1000100), observations=records, now=1_000_000)
    assert result["calibration"]["empirical_coverage"] == sum(expected) / len(expected)
    assert result["calibration"]["coverage_trials"] == len(expected)


def test_empirical_conformal_coverage_on_independent_stationary_oracle():
    rng = random.Random(85)
    errors = [abs(rng.gauss(0, 60)) for _ in range(800)]
    records = [observation(i, error) for i, error in enumerate(errors)]
    result = forecast(job(), observations=records, now=NOW)
    q = result["calibration"]["quantile_seconds"]
    holdouts = [abs(rng.gauss(0, 60)) <= q for _ in range(2000)]
    assert .75 <= sum(holdouts) / len(holdouts) <= .85
    assert .75 <= result["calibration"]["empirical_coverage"] <= .85


def test_future_outcomes_and_post_start_forecasts_cannot_leak_into_calibration():
    invalid = [observation(i, actual_start=NOW + 10) for i in range(30)]
    invalid += [observation(i + 100, issued_at=3000, first_issued_at=3000, actual_start=2900, predicted_start=3200) for i in range(30)]
    invalid += [observation(i + 200, issued_at=3000, first_issued_at=3000, actual_start=3000, predicted_start=3200) for i in range(30)]
    invalid += [observation(i + 300, predicted_start=100, first_predicted_start=100) for i in range(30)]
    result = forecast(job(), observations=invalid, now=NOW)
    assert result["samples"] == 0
    assert result["lower_start"] is None


def test_first_latest_revisions_are_not_independent_calibration_samples():
    records = []
    for i in range(20):
        records.append(observation(i, error=100))
        records.append(observation(i, issued_at=2050 + i * 10,
                                   predicted_start=2190 + i * 10, actual_start=2200 + i * 10))
    result = forecast(job(), observations=records, now=NOW)
    assert result["samples"] == 20
    assert result["calibration"]["quantile_seconds"] == 10


def test_near_start_residuals_cannot_calibrate_a_long_lead_forecast():
    records = [observation(i, error=0) for i in range(60)]
    result = forecast(job(est_start=NOW + 7200), observations=records, now=NOW)
    assert result["method"] == "scheduler_uncalibrated"
    assert result["samples"] == 0
    assert result["lower_start"] is None
    assert result["calibration"]["lead_band"] == {"lower_seconds": 3600., "upper_seconds": 21600.}


def test_first_comparable_revision_is_used_when_latest_has_a_different_lead():
    records = [observation(i, error=40, issued_at=2190 + i * 10,
                           predicted_start=2200 + i * 10, actual_start=2240 + i * 10)
               for i in range(20)]
    result = forecast(job(), observations=records, now=NOW)
    assert result["samples"] == 20
    # First forecast lead 100 seconds and residual 140; final 10-second lead
    # with residual 40 cannot make this 100-second lead look more accurate.
    assert result["calibration"]["quantile_seconds"] == 140


@pytest.mark.parametrize("field", ["cpus", "nodes", "gpus", "mem_bytes", "time_seconds", "gpu_type"])
def test_unknown_allocation_does_not_pool_all_partition_outcomes(field):
    target = job()
    target.pop(field)
    result = forecast(target, observations=[observation(i) for i in range(60)], now=NOW)
    assert result["method"] == "scheduler_uncalibrated"
    assert result["samples"] == 0


def test_generic_gpu_requests_only_use_recorded_generic_allocations():
    rows = [observation(i, gpu_type="") for i in range(20)]
    rows += [observation(i + 100, gpu_type="a100") for i in range(20)]
    result = forecast(job(gpu_type=""), observations=rows, now=NOW)
    assert result["samples"] == 20
    assert result["cohort"]["gpu_type"] == ""


def test_target_outcome_is_not_its_own_calibration_evidence():
    records = [observation(0, job_id="target", submit=NOW - 100,
                           issued_at=NOW - 90, predicted_start=NOW - 50, actual_start=NOW - 40)] * 40
    result = forecast(job(), observations=records, now=NOW)
    assert result["samples"] == 0


@pytest.mark.parametrize("field,value", [
    ("name", "other"), ("partition", "cpu"), ("account", "other"), ("qos", "premium"),
    ("cpus", 8), ("nodes", 2), ("gpus", 2), ("gpu_type", "h100"),
    ("mem_bytes", 32 * 1024**3), ("time_seconds", 7200),
])
def test_distinct_request_cohorts_do_not_borrow_scheduler_calibration(field, value):
    target = job(mem_bytes=16 * 1024**3, time_seconds=3600)
    settings = {"mem_bytes": 16 * 1024**3, "time_seconds": 3600, field: value}
    records = [observation(i, **settings) for i in range(20)]
    result = forecast(target, observations=records, now=NOW)
    assert result["samples"] == 0


@pytest.mark.parametrize("field", ["account", "qos", "cpus", "nodes", "gpus", "gpu_type"])
def test_missing_cohort_evidence_does_not_weaken_matching(field):
    records = [observation(i) for i in range(20)]
    for record in records:
        record.pop(field)
    assert forecast(job(), observations=records, now=NOW)["samples"] == 0


def test_memory_walltime_cohort_supports_existing_job_finished_models():
    target = Job("target", "train", "p", "PENDING", submit="1970-01-02T03:45:00+00:00",
                 cpus=4, nodes=1, mem_req="16G", limit="01:00:00")
    past = [Finished(str(i), "train", "COMPLETED", cpus=4, nodes=1, gpus=0,
                     req_mem=16 * 1024**3, limit="01:00:00", partition="p",
                     submit="1970-01-01T05:00:00+00:00", start="1970-01-01T05:30:00+00:00")
            for i in range(40)]
    result = forecast(target, past, now=NOW)
    assert result["method"] == "historical_split_conformal"
    assert result["cohort"]["mem_bytes"] == 16 * 1024**3
    assert result["cohort"]["time_seconds"] == 3600


@pytest.mark.parametrize("reason", ["JobHeldUser", "JobHeldAdmin", "Dependency", "DependencyNeverSatisfied", "InvalidAccount", "InvalidQOS", "PartitionDown", "BadConstraints", "LicensesUnavailable"])
def test_unresolved_blockers_abstain_even_with_many_calibration_records(reason):
    result = forecast(job(reason=reason), observations=[observation(i) for i in range(60)], now=NOW)
    assert result["status"] == "blocked"
    assert result["predicted_start"] is None
    assert result["lower_start"] is None


def test_explicit_dependency_abstains_without_scheduler_reason():
    result = forecast(job(reason="Resources", dependency="afterok:123"), now=NOW)
    assert result["status"] == "blocked"
    assert "afterok:123" in " ".join(result["evidence"])


@pytest.mark.parametrize("dependency", ["", "(null)", "None", "N/A"])
def test_empty_dependency_not_a_blocker(dependency):
    assert forecast(job(dependency=dependency), now=NOW)["status"] == "forecast"


def test_passed_scheduler_prediction_is_stale_not_zero_wait_forecast():
    result = forecast(job(est_start=NOW - 1), [history(i) for i in range(80)], now=NOW)
    assert result["status"] == "stale"
    assert result["predicted_start"] is None
    assert result["wait_seconds"] is None


def test_starting_now_scheduler_point_is_valid():
    assert forecast(job(est_start=NOW), now=NOW)["wait_seconds"] == 0


@pytest.mark.parametrize("state", ["RUNNING", "COMPLETING", "COMPLETED", "FAILED", "CANCELLED"])
def test_non_pending_states_are_observed_not_queue_predictions(state):
    result = forecast(job(state=state, start=NOW - 10), now=NOW)
    assert result["status"] == "active"
    assert result["method"] == "observed_state"
    assert result["predicted_start"] == NOW - 10
    assert result["lower_start"] is None


def test_conditional_history_uses_remaining_wait_after_already_queued_time():
    records = [history(i, wait=1000) for i in range(40)]
    records += [history(i + 100, wait=50) for i in range(100)]
    result = forecast(job(est_start=""), records, now=NOW)
    assert result["method"] == "historical_split_conformal"
    assert result["samples"] == 40
    assert result["queued_seconds"] == 100
    assert result["wait_seconds"] == 900
    assert result["predicted_start"] == result["lower_start"] == result["upper_start"] == NOW + 900


def test_history_must_separate_training_and_calibration_and_deduplicate():
    records = [history(i) for i in range(39)] * 5
    result = forecast(job(est_start=""), records, now=NOW)
    assert result["status"] == "insufficient"
    assert result["samples"] == 39
    assert result["predicted_start"] is None


def test_historical_prediction_uses_training_only_with_holdout_drift_visible():
    records = [history(i, wait=1000 if i < 20 else 4000) for i in range(40)]
    # Separate starts chronologically so the second half really is the holdout.
    result = forecast(job(est_start=""), records, now=NOW)
    assert result["wait_seconds"] == 900
    assert result["calibration"]["quantile_seconds"] == 3000
    assert result["upper_start"] == NOW + 3900


def test_future_starts_and_invalid_waits_are_not_historical_training_data():
    records = [history(i, start=NOW + 1) for i in range(50)]
    records += [history(i + 100, submit=5000, start=4999) for i in range(50)]
    result = forecast(job(est_start=""), records, now=NOW)
    assert result["samples"] == 0


def test_unknown_age_or_partition_requires_abstention_from_history():
    records = [history(i) for i in range(80)]
    assert forecast(job(submit="", est_start=""), records, now=NOW)["status"] == "insufficient"
    assert forecast(job(partition="", est_start=""), records, now=NOW)["status"] == "insufficient"


def test_aware_iso_and_datetime_timestamps_share_epoch_clock():
    target = job(submit="1970-01-02T03:45:00Z", est_start=datetime.fromtimestamp(NOW + 600, timezone.utc))
    result = forecast(target, now=datetime.fromtimestamp(NOW, timezone.utc))
    assert result["queued_seconds"] == 100
    assert result["wait_seconds"] == 600
    assert not any("local timezone" in limitation for limitation in result["limitations"])


def test_consistent_naive_timestamps_are_explicit_local_timezone_assumption():
    target = job(submit="2025-01-01T00:00:00", est_start="2025-01-01T01:00:00")
    result = forecast(target, now=datetime.fromisoformat("2025-01-01T00:30:00"))
    assert result["wait_seconds"] == 1800
    assert "local timezone" in " ".join(result["limitations"])


def test_mixed_aware_naive_timestamps_abstain_instead_of_guessing_offset():
    target = job(submit="2025-01-01T00:00:00", est_start="2025-01-01T01:00:00Z")
    result = forecast(target, now=datetime.fromisoformat("2025-01-01T00:30:00Z"))
    assert result["status"] == "error"
    assert result["predicted_start"] is None


@pytest.mark.parametrize("bad", [False, float("nan"), float("inf"), -1, "today", "2025-01-01", {}, [], 10**1000])
def test_invalid_now_never_produces_nonfinite_json(bad):
    result = forecast(job(), now=bad)
    assert result["status"] == "error"
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize("bad", [True, 0, 1, -1, 2, float("nan"), float("inf"), ".8"])
def test_invalid_coverage_returns_structured_error(bad):
    result = forecast(job(), now=NOW, coverage=bad)
    assert result["status"] == "error"
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize("bad", [True, 0, -1, 100001, "5", 1.5])
def test_invalid_record_limit_returns_structured_error(bad):
    assert forecast(job(), now=NOW, max_records=bad)["status"] == "error"


def test_input_bound_is_enforced_on_generators_and_truthfully_reported():
    consumed = []
    def records():
        for i in range(1000000):
            consumed.append(i)
            yield observation(i)
    result = forecast(job(), observations=records(), now=NOW, max_records=20)
    assert len(consumed) == 21
    assert result["samples"] == 20
    assert "capped" in " ".join(result["limitations"])


@pytest.mark.parametrize("malformed", [None, "records", {}, 1])
def test_malformed_record_collections_return_error(malformed):
    assert forecast(job(), observations=malformed, now=NOW)["status"] == "error"


def test_malformed_individual_records_are_skipped_and_text_is_terminal_safe():
    records = [None, 1, "bad", {"issued_at": []}, observation(1, cpus=False)]
    result = forecast(job(id="target\x1b[31m\n"), observations=records, now=NOW)
    assert result["samples"] == 0
    assert "\x1b" not in json.dumps(result)
    malformed = job(mem_req="1Gc", mem_bytes=None, cpus={}, nodes={})
    json.dumps(forecast(malformed, now=NOW), allow_nan=False)


def test_future_submission_and_missing_job_state_are_errors():
    assert forecast(job(submit=NOW + 1), now=NOW)["status"] == "error"
    assert forecast(job(state=""), now=NOW)["status"] == "error"
    assert forecast(None, now=NOW)["status"] == "error"


def test_tracker_preserves_first_latest_revisions_without_snapshot_duplicates():
    tracker = ForecastTracker()
    initial = job(id="1", submit=100, est_start=500)
    tracker.observe([initial], now=200)
    tracker.observe([initial], now=201)
    tracker.observe([initial], now=202)
    tracker.observe([dict(initial, est_start=550)], now=210)
    tracker.observe([dict(initial, est_start=550)], now=211)
    rows = tracker.observations()
    assert len(rows) == 1
    assert rows[0]["first_predicted_start"] == 500
    assert rows[0]["predicted_start"] == 550
    assert rows[0]["first_issued_at"] == 200
    assert rows[0]["issued_at"] == 210
    assert rows[0]["revision_count"] == 1
    assert rows[0]["last_observed_at"] == 211
    tracker.observe([dict(initial, state="RUNNING", start=560)], now=570)
    tracker.observe([dict(initial, state="RUNNING", start=560)], now=580)
    rows = tracker.observations()
    assert len(rows) == 1 and rows[0]["actual_start"] == 560
    result = forecast(dict(initial, est_start=700), observations=rows, now=600)
    assert result["revisions"]["count"] == 1
    assert result["revisions"]["shift_seconds"] == 50


def test_tracker_never_issues_running_job_forecast_or_uses_after_start_revision():
    tracker = ForecastTracker()
    tracker.observe([job(id="1", submit=100, state="RUNNING", start=150, est_start=200)], now=170)
    assert tracker.observations() == []
    tracker.observe([job(id="2", submit=100, est_start=300)], now=200)
    tracker.observe([job(id="2", submit=100, state="RUNNING", start=200)], now=210)
    assert tracker.observations() == []


def test_tracker_missing_submission_cannot_join_reused_job_id():
    tracker = ForecastTracker()
    tracker.observe([job(id="1", submit="", est_start=500)], now=200)
    assert tracker.observations() == []
    tracker.observe([job(id="1", submit=100, est_start=500)], now=200)
    tracker.observe([job(id="1", submit=300, est_start=600)], now=400)
    tracker.observe([job(id="1", submit=300, state="RUNNING", start=610)], now=620)
    rows = tracker.observations()
    assert len(rows) == 2
    assert next(row for row in rows if row["submit"] == 100)["actual_start"] is None
    assert next(row for row in rows if row["submit"] == 300)["actual_start"] == 610


def test_tracker_requeue_drops_prior_episode_calibration():
    tracker = ForecastTracker()
    initial = job(id="1", submit=100, est_start=500)
    tracker.observe([initial], now=200)
    tracker.observe([dict(initial, state="RUNNING", start=510)], now=520)
    tracker.observe([dict(initial, est_start=800)], now=600)
    rows = tracker.observations()
    assert len(rows) == 1
    assert rows[0]["actual_start"] is None
    assert rows[0]["issued_at"] == 600


def test_tracker_changed_request_invalidates_previous_prediction():
    tracker = ForecastTracker()
    initial = job(id="1", submit=100, est_start=500)
    tracker.observe([initial], now=200)
    tracker.observe([dict(initial, cpus=8, state="RUNNING", start=510)], now=520)
    assert tracker.observations() == []


def test_tracker_observations_are_deep_copies():
    tracker = ForecastTracker()
    tracker.observe([job(id="1", submit=100, est_start=500)], now=200)
    rows = tracker.observations()
    rows[0]["predicted_start"] = -1
    assert tracker.observations()[0]["predicted_start"] == 500


def test_tracker_rewind_discards_future_recording_evidence():
    tracker = ForecastTracker()
    initial = job(id="1", submit=100, est_start=500)
    tracker.observe([initial], now=200)
    tracker.observe([dict(initial, state="RUNNING", start=510)], now=520)
    tracker.observe([], now=150)
    assert tracker.observations() == []


def test_tracker_does_not_record_past_points_as_new_predictions():
    tracker = ForecastTracker()
    tracker.observe([job(id="1", submit=100, est_start=150)], now=200)
    assert tracker.observations() == []
    tracker.observe([job(id="1", submit=100, est_start=500)], now=200)
    tracker.observe([job(id="1", submit=100, est_start=190)], now=210)
    assert tracker.observations()[0]["predicted_start"] == 500
    assert tracker.observations()[0]["revision_count"] == 0


def test_tracker_bounds_pending_and_completed_memory():
    tracker = ForecastTracker(max_jobs=2, max_observations=3)
    for i in range(10):
        tracker.observe([job(id=str(i), submit=100, est_start=500)], now=200 + i)
    assert len(tracker.observations()) == 2
    for i in range(10, 20):
        tracker.observe([job(id=str(i), submit=100, est_start=500)], now=300 + i)
        tracker.observe([job(id=str(i), submit=100, state="RUNNING", start=350 + i)], now=400 + i)
    assert len(tracker.observations()) <= 3


@pytest.mark.parametrize("bad", [0, -1, True, "4", 1.5, 100001])
def test_tracker_rejects_invalid_bounds(bad):
    with pytest.raises(ValueError):
        ForecastTracker(max_jobs=bad)
    with pytest.raises(ValueError):
        ForecastTracker(max_observations=bad)


def test_tracker_restore_roundtrip_retains_only_public_evidence():
    tracker = ForecastTracker()
    initial = job(id="1", submit=100, est_start=500)
    tracker.observe([initial], now=200)
    tracker.observe([dict(initial, state="RUNNING", start=510)], now=520)
    encoded = json.loads(json.dumps(tracker.observations()))
    encoded[0]["password"] = "should-not-survive"
    clone = ForecastTracker()
    clone.restore(encoded, now=600)
    assert clone.observations() == tracker.observations()
    assert "password" not in clone.observations()[0]


def test_tracker_restore_rejects_future_leakage_and_tampered_times():
    tracker = ForecastTracker()
    good = observation(1)
    invalid = [dict(good, issued_at=good["actual_start"]), dict(good, actual_start=NOW + 1),
               dict(good, first_issued_at=good["issued_at"] + 1), dict(good, predicted_start=0),
               dict(good, revision_count=True), dict(good, last_observed_at=NOW + 1),
               dict(good, actual_start="invalid")]
    tracker.restore(invalid, now=NOW)
    assert tracker.observations() == []
    tracker.restore([good], now=NOW)
    assert len(tracker.observations()) == 1


def test_tracker_restore_bounds_and_deduplicates_untrusted_state():
    tracker = ForecastTracker(max_jobs=2, max_observations=3)
    tracker.restore([observation(i) for i in range(100)], now=NOW)
    assert len(tracker.observations()) == 3
    tracker.restore([observation(1)] * 100, now=NOW)
    assert len(tracker.observations()) == 1


def test_tracker_parallel_sampler_observation_is_thread_safe():
    tracker = ForecastTracker()
    jobs = [job(id=str(i), submit=100, est_start=500) for i in range(20)]
    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(lambda _: tracker.observe(jobs, now=200), range(80)))
    rows = tracker.observations()
    assert len(rows) == 20
    assert all(row["revision_count"] == 0 for row in rows)


def test_future_revision_metadata_is_not_visible_in_an_earlier_forecast():
    records = [observation(1, job_id="target", submit=NOW - 100,
                           issued_at=NOW + 1, predicted_start=NOW + 600)]
    assert forecast(job(), observations=records, now=NOW)["revisions"] == {}
