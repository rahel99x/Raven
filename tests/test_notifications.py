"""Alert retention, dismissal episodes, scope, bounds, and no-I/O publication."""
from dataclasses import FrozenInstanceError
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from raven import activity_ui, notifications as N
from raven.model import Health


def app_for():
    return SimpleNamespace(store=SimpleNamespace(alerts=None), profile_name="lab-a",
                           actions=SimpleNamespace(slurm=SimpleNamespace(b=object())),
                           logs=SimpleNamespace(error=""), raven_ui_state={})


def test_messages_preserve_multiline_and_strip_terminal_commands():
    value = "first line\r\n\x1b[31msecond\x1b[0m\n\x1b]52;c;secret\x07third\tcolumn\x00"
    center = N.NotificationCenter()
    item = center.get(center.post(value))
    assert item.text == "first line\nsecond\nthird    column^@"
    assert not item.truncated
    assert "\x1b" not in item.text and "secret" not in item.text


@pytest.mark.parametrize("text", ["x" * 100000, "界" * 30000, "x" + "\t" * 40000],
                         ids=["ascii", "unicode", "controls"])
def test_message_byte_bound_is_explicit_and_unicode_safe(text):
    center = N.NotificationCenter()
    item = center.get(center.post(text))
    assert len(item.text.encode("utf-8")) <= N.MAX_MESSAGE_BYTES
    assert item.truncated
    assert "\ufffd" not in item.text


def test_full_alert_survives_legacy_activity_display_limit():
    activity = activity_ui.Activity()
    text = "failure\n" + "details " * 1600 + "FINAL DIAGNOSTIC"
    activity.post(text, "error", path="/tmp/report", job="42", task="export")
    notice = activity.alerts.snapshot().notices[0]
    assert notice.text == text
    assert notice.text.endswith("FINAL DIAGNOSTIC")
    assert notice.job == "42" and notice.task == "export" and notice.path == "/tmp/report"
    assert len(activity.snapshot()[0][0]["text"]) == 8192
    assert activity.exports[0]["path"] == "/tmp/report"


def test_post_counts_duplicates_without_changing_identity_or_first_time():
    center = N.NotificationCenter()
    identity = center.post("failure", source="queue", now=10)
    first = center.snapshot()
    assert center.post("failure", source="queue", now=20) == identity
    latest = center.get(identity)
    assert latest.first_seen == 10 and latest.last_seen == 20 and latest.occurrences == 2
    assert center.snapshot().counts == {"critical": 1, "moderate": 0, "mild": 0}
    assert first.notices[0].occurrences == 1  # snapshots never mutate behind a frame
    assert center.post("failure", source="other", now=30) != identity
    assert center.post("failure", source="queue", job="2") != identity


@pytest.mark.parametrize("text,level,severity", [
    ("ordinary failure", "error", "critical"),
    ("ordinary warning", "warning", "moderate"),
    ("Raven collection deferred: rolling steps budget exhausted", "error", "mild"),
    ("GPU counters are unavailable for this job", "warning", "mild"),
    ("permission denied", "error", "critical"),
    ("scheduler unavailable", "error", "critical"),
    ("GPU temperature warning", "warning", "moderate"),
    ("user classified issue", "mild", "mild"),
    ("operation complete", "success", None),
    ("information", "info", None),
    ("unrecognized level", "fatal", None),
])
def test_severity_uses_only_reported_evidence(text, level, severity):
    center = N.NotificationCenter()
    identity = center.post(text, level)
    assert (center.get(identity).severity if identity else None) == severity


@pytest.mark.parametrize("text", [
    "Permission denied: /lab/source disabled/config.json",
    "squeue: authentication failed; source disabled",
    "GPU counters are unavailable: permission denied opening /dev/nvidia0",
    "Raven collection deferred: authentication failure connecting to Slurm",
    "Raven collection deferred: invalid configuration in config.json",
    "GPU telemetry is unavailable: worker host is outside the verified allocation",
    "Raven collection deferred: verification failed for worker identity",
    "Could not open '/lab/rolling steps budget exhausted/config.json'",
    "source disabled/config.json: unsupported format",
    "diagnostic includes 'GPU counters are unavailable' in a quoted log",
])
def test_incidental_recoverable_phrases_do_not_downgrade_failures(text):
    assert N.severity_for(text, "error") == "critical"
    assert N.severity_for(text, "warning") == "moderate"


@pytest.mark.parametrize("text", [
    "Raven collection deferred: rolling steps budget exhausted",
    "Raven: collection deferred: retry in 10 seconds",
    "GPU counters are unavailable for this job",
    "GPU telemetry is unavailable: no recorded counters",
    "No running jobs are available to sample.",
    "Source disabled by this profile.",
])
def test_explicit_recoverable_observations_keep_mild_severity(text):
    assert N.severity_for(text, "error") == "mild"


@pytest.mark.parametrize("text", [
    "Permission denied: /lab/rolling steps budget exhausted/config.json",
    "Raven collection deferred: authentication failed connecting to Slurm",
    "GPU counters are unavailable: permission denied opening /dev/nvidia0",
])
def test_access_diagnosis_wins_over_incidental_budget_or_gpu_phrases(text):
    assert "permission failure" in N.diagnose(text).issue


def test_cached_snapshot_is_immutable_and_rebuilt_only_after_change():
    center = N.NotificationCenter()
    center.post("problem")
    before = center.snapshot()
    assert center.snapshot() is before
    with pytest.raises(TypeError):
        before.counts["critical"] = 99
    with pytest.raises(FrozenInstanceError):
        before.notices[0].text = "edited"
    assert not center.dismiss("missing")
    assert center.snapshot() is before
    assert center.dismiss(before.notices[0].id)
    after = center.snapshot()
    assert after.revision > before.revision and not after.notices


def test_dismissed_observed_condition_waits_for_resolution_or_change():
    app = app_for()
    health = Health("jobs", error="squeue failed", calls=1)
    snap = {"health": {"jobs": health}}
    center = N.initialize(app)
    identity = N.sync(app, snap).notices[0].id
    assert center.dismiss(identity)
    after = center.snapshot()
    for count in range(2, 20):
        health.calls, health.last_try = count, count * 20
        assert N.sync(app, snap) is after
    health.error = "different failure"
    changed = N.sync(app, snap)
    assert changed.notices[0].id != identity
    center.clear()
    health.error = ""
    N.sync(app, snap)
    health.error = "different failure"
    assert N.sync(app, snap).counts["critical"] == 1


@pytest.mark.parametrize("resolution", ["disabled", "removed"])
def test_disabled_or_removed_source_ends_suppression_episode(resolution):
    app, health = app_for(), Health("jobs", error="failure")
    snap = {"health": {"jobs": health}}
    first = N.sync(app, snap).notices[0]
    app.notifications.dismiss(first.id)
    if resolution == "disabled":
        health.enabled = False
    else:
        snap["health"] = {}
    N.sync(app, snap)
    health.enabled, snap["health"] = True, {"jobs": health}
    assert N.sync(app, snap).notices[0].id != first.id


def test_scope_change_discards_foreign_notices_and_allows_same_error():
    app = app_for()
    activity_ui.initialize(app)
    app.activity.post("lab-a warning", "warning")
    N.sync(app, {"health": {"jobs": Health("jobs", error="scheduler failed")}})
    old = app.notifications.snapshot()
    app.profile_name = "lab-b"
    # Activity can publish before the next paint; it still observes the new scope.
    app.activity.post("lab-b warning", "warning")
    current = app.notifications.snapshot()
    assert [n.text for n in current.notices] == ["lab-b warning"]
    assert not app.notifications.dismiss(old.notices[0].id)
    N.sync(app, {"health": {"jobs": Health("jobs", error="scheduler failed")}})
    assert app.notifications.snapshot().counts == {"critical": 1, "moderate": 1, "mild": 0}


def test_replaced_activity_owns_fresh_center_without_stale_alias():
    app = app_for()
    activity_ui.initialize(app)
    previous = app.notifications
    app.activity = activity_ui.Activity()
    assert N.initialize(app) is app.activity.alerts
    assert app.notifications is not previous


def test_bounds_evict_oldest_once_and_keep_indexes_consistent():
    center = N.NotificationCenter()
    ids = [center.post(f"issue {n}", "warning") for n in range(300)]
    snapshot = center.snapshot()
    assert len(snapshot.notices) == N.MAX_NOTICES
    assert snapshot.evicted == 44 and snapshot.counts["moderate"] == N.MAX_NOTICES
    assert center.get(ids[0]) is None and center.get(ids[-1]) is not None
    assert center.post("issue 0", "warning") != ids[0]
    center.clear("moderate")
    assert not center.snapshot().notices and center.snapshot().retained_bytes == 0
    assert not center._index and not center._keys and not center._sizes


def test_large_messages_use_total_payload_budget_and_compact_condition_keys():
    center = N.NotificationCenter()
    for n in range(40):
        center.condition(n, f"{n}\n" + "x" * N.MAX_MESSAGE_BYTES)
    snapshot = center.snapshot()
    assert snapshot.retained_bytes <= N.MAX_RETAINED_BYTES
    assert 0 < len(snapshot.notices) < 20
    assert snapshot.evicted > 0
    assert all(isinstance(signature[0], bytes) and len(signature[0]) == 20
               for signature in center._conditions.values())
    assert all(n.truncated for n in snapshot.notices)


def test_condition_fingerprint_covers_text_after_removed_multibyte_controls():
    center = N.NotificationCenter()
    prefix = "\x9b31m" * 14000
    first = center.condition("jobs", prefix + "first error")
    second = center.condition("jobs", prefix + "different error")
    assert first != second
    assert center.get(first).text == "first error"
    assert center.get(second).text == "different error"


def test_condition_count_stays_bounded_even_after_notices_are_dismissed():
    center = N.NotificationCenter()
    for n in range(600):
        identity = center.condition(n, f"failure {n}")
        center.dismiss(identity)
    assert not center.snapshot().notices
    assert len(center._conditions) == N.MAX_CONDITIONS


def test_sync_reads_cached_sources_and_alert_events_without_running_services():
    def forbidden(*args, **kwargs):
        pytest.fail("Notification synchronization must not run a diagnostic or a scheduler query")
    app = app_for()
    event = dict(t=100, rule="idle GPU", job="9", text="idle GPU: 9 training")
    engine = SimpleNamespace(recent=[event], errors={"bad rule": "invalid expression"},
                             check=forbidden, active_text=forbidden, active_count=forbidden)
    app.store.alerts = engine
    app.store.snapshot = forbidden
    app.actions.slurm.b = SimpleNamespace(run=forbidden)
    app.logs.error = "log permission denied"
    app.raven_ui_state = {"snapshot": {"error": "worker host outside the verified allocation"}}
    snap = {"health": {"jobs": {"error": "squeue timed out", "enabled": True}}, "error": "collector failed"}
    result = N.sync(app, snap)
    assert result.counts == {"critical": 5, "moderate": 1, "mild": 0}
    alert = next(n for n in result.notices if n.source == "alert: idle GPU")
    assert alert.job == "9" and alert.text == event["text"]
    app.notifications.dismiss(alert.id)
    assert N.sync(app, snap).counts["moderate"] == 0
    engine.recent = [event, dict(event, t=101)]
    assert N.sync(app, snap).counts["moderate"] == 1


def test_unchanged_conditions_do_not_repeat_sanitizing_or_diagnosing(monkeypatch):
    app = app_for()
    snap = {"health": {"jobs": Health("jobs", error="squeue failed")}}
    before = N.sync(app, snap)
    def forbidden(*args, **kwargs):
        pytest.fail("Unchanged conditions must reuse their published record")
    monkeypatch.setattr(N, "message_text", forbidden)
    monkeypatch.setattr(N, "diagnose", forbidden)
    for _ in range(10):
        assert N.sync(app, snap) is before


@pytest.mark.parametrize("message,topic", [
    ("rolling steps budget exhausted", "limit"),
    ("permission denied", "permission"),
    ("squeue timed out", "timeout"),
    ("Invalid account or account/partition combination", "account"),
    ("Worker host is outside the verified allocation", "allocation"),
    ("missing gpu_nvml.so", "GPU"),
    ("invalid TOML config", "configuration"),
    ("sacct returned failure", "Slurm"),
    ("unrecognized failure 17", "not been diagnosed"),
])
def test_local_guidance_identifies_evidence_without_claiming_live_diagnosis(message, topic):
    diagnostic = N.diagnose(message)
    assert topic in diagnostic.issue
    assert diagnostic.causes and diagnostic.fixes
    assert all(isinstance(item, str) and item for item in diagnostic.causes + diagnostic.fixes)


def test_concurrent_posts_keep_one_identity_and_exact_occurrence_count():
    center = N.NotificationCenter()
    with ThreadPoolExecutor(max_workers=4) as pool:
        ids = list(pool.map(lambda _: center.post("concurrent failure", source="worker"), range(120)))
    snapshot = center.snapshot()
    assert len(set(ids)) == 1
    assert len(snapshot.notices) == 1 and snapshot.notices[0].occurrences == 120
    assert snapshot.counts["critical"] == 1
