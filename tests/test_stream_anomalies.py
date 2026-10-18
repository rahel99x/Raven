"""Known injected changes, exact attempts, evidence, abstention and finite cost."""
from dataclasses import replace
from types import SimpleNamespace

import pytest

from raven import measurement_series as M, stream_anomalies as A
from raven.model import Store, Job
from raven import notifications

IDENTITY = {"job": "7", "scope": "local-fixture", "attempt": "submitted-started-restart0", "verified": True}


def series(values, *, metric="gpu-util", unit="%", stamps=None, phase=None, limit=None):
    return {"identity": dict(IDENTITY), "metric": metric, "unit": unit, "limit": limit,
            "samples": [{"t": t, "value": v, **({"phase": phase(i)} if phase else {})}
                        for i, (t, v) in enumerate(zip(stamps or range(len(values)), values))]}


def document(*items):
    return {"schema": M.SCHEMA, "series": list(items)}


def test_known_gpu_collapse_detects_once_with_raw_evidence_and_no_cause():
    result = A.analyze(document(series([80.] * 40 + [5.] * 40)))
    events = result["events"]
    assert len(events) == 1 and events[0]["kind"] == "down"
    assert events[0]["timestamp"] == 42  # three confirming observations
    assert events[0]["identity"] == IDENTITY
    assert [p["value"] for p in events[0]["window"]][-3:] == [5., 5., 5.]
    assert "cause is not established" in events[0]["diagnosis"]
    assert events[0]["forecast"]["status"] == "abstain"


def test_memory_leak_and_forecast_report_units_interval_and_validity():
    values = [100 * 2**20 + i * 2**20 for i in range(64)]
    result = A.analyze(document(series(values, metric="rss", unit="B", limit=170 * 2**20)))
    assert any(event["kind"] == "trend" for event in result["events"])
    forecast = result["series"][0]["forecast"]
    assert forecast["status"] == "experimental"
    low, high = forecast["remaining_seconds_interval"]
    assert low <= 7 <= high and high <= forecast["validity_horizon_seconds"]
    assert forecast["slope_unit"] == "B/s" and "uncalibrated" in forecast["uncertainty"]
    assert "timestamp" not in forecast


def test_constant_missing_uneven_phase_change_and_noisy_data_can_abstain():
    assert not A.analyze(document(series([8.] * 64)))["events"]
    missing = A.analyze(document(series([None] * 64)))
    assert not missing["events"] and missing["series"][0]["state"] == "missing observation"
    phase = A.analyze(document(series([80.] * 40 + [5.] * 40, phase=lambda i: "train" if i < 40 else "validate")))
    assert not phase["events"]
    noisy = A.analyze(document(series(([1., 100.] * 32))))
    assert not noisy["events"] and noisy["series"][0]["state"] == "noisy baseline"
    stamps = [float(i + (100 if i >= 35 else 0)) for i in range(64)]
    gaps = A.analyze(document(series([80.] * 40 + [5.] * 24, stamps=stamps)))
    assert not gaps["events"]  # uneven cadence, not inferred regular observations
    assert A.forecast([(float(i), 10.) for i in range(32)], limit=100, metric="rss", unit="B")["status"] == "abstain"


def test_nonincreasing_or_unknown_attempt_never_enters_detector():
    bad = document(series([1, 2], stamps=[1, 1]))
    with pytest.raises(ValueError, match="increase"):
        A.analyze(bad)
    bad = document(series([1, 2])); bad["series"][0]["identity"]["verified"] = False
    with pytest.raises(ValueError, match="verified"):
        A.analyze(bad)


def test_monitor_has_finite_streams_episodes_and_windows():
    monitor = A.Monitor(max_streams=3)
    for i in range(20):
        identity = {**IDENTITY, "attempt": str(i)}
        for t, value in enumerate([80.] * 40 + [5.] * 4):
            monitor.update(identity, "gpu", "%", t, value)
    assert len(monitor.streams) == 3 and monitor.evictions == 17
    assert all(len(detector.points) <= A.WINDOW for detector in monitor.streams.values())
    assert len(monitor.recent) == 20
    assert len({event["id"] for event in monitor.recent}) == 20
    assert all("Evidence (seconds, value)" in event["text"] for event in monitor.recent)


def test_store_uses_current_verified_attempt_and_notification_dismissal_is_stable():
    store = Store(persist=False)
    job = Job("7", "scientific job", "main", "RUNNING", submit="2026-10-17T00:00:00", start="2026-10-17T00:00:01", cluster="fixture")
    store.apply_jobs([job])
    store.details[job.id] = {"JobId": job.id, "SubmitTime": job.submit, "StartTime": job.start, "Restarts": "0", "Cluster": "fixture"}
    current = store.capture_series_identity(job.id)
    assert current.verified
    for t in range(44):
        store.record(job.id, {"k": "live", "t": float(t), "cpu": .8 if t < 40 else .05, "rss": 1000000}, identity=current)
    assert len(store.anomalies.recent) == 1
    app = SimpleNamespace(store=store, profile_name="fixture")
    first = notifications.sync(app, {})
    assert len(first.notices) == 1 and first.notices[0].severity == "mild"
    assert "Cause is unknown" in first.notices[0].text
    app.notifications.dismiss(first.notices[0].id)
    assert not notifications.sync(app, {}).notices
    store.details[job.id] = {**store.details[job.id], "Restarts": "1"}
    next_attempt = store.capture_series_identity(job.id)
    assert next_attempt != current
    previous_count = len(store.anomalies.recent)
    assert store.record(job.id, {"k": "live", "t": 99., "cpu": .99}, identity=current) is None
    assert len(store.anomalies.recent) == previous_count
    store.record(job.id, {"k": "live", "t": 100., "cpu": .99}, identity=next_attempt)
    assert len(store.anomalies.recent) == previous_count  # new-attempt warmup


def test_operations_read_only_user_path(tmp_path):
    import json
    from raven import operations as O
    path = tmp_path / "measurements.json"
    path.write_text(json.dumps(document(series([80.] * 40 + [5.] * 8))))
    result = O.run("telemetry-anomalies", {"path": str(path)}, O.Context())
    assert result["data"]["events"] and result["warnings"]
    assert "plan" not in result


@pytest.mark.parametrize("metric,unit,bad", [("rss", "B", -1), ("gpu-util:0", "%", 101), ("gpu", "%", -1)])
def test_physically_impossible_observations_abstain(metric, unit, bad):
    detector = A.Detector(metric, unit, limit=1024)
    for i in range(20): detector.update(i, 50)
    assert detector.update(20, bad) is None
    assert "physically invalid" in detector.status and not detector.points
    assert A.forecast([(i, -i) for i in range(12)], limit=1024, metric="rss", unit="B")["status"] == "abstain"
