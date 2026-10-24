"""Absent, unreadable and unassociated recordings retain distinct feedback."""

import json
from pathlib import Path
from threading import Event

import pytest

from raven import layout as L
from raven.tracing import ui as UI
from raven.tracing import workspace_discovery as D
from raven.tracing.capture import Recorder
from test_job_panels import dashboard as dashboard
from test_trace_timeline_workflow import connect
from test_trace_workspace_discovery import context, read


def run_recording(root, **anchors):
    with Recorder(
        root,
        job_id="42",
        cluster="local",
        attempt="launch-token-a1",
        identity_extra={"run_id": "launch-token-a1", **anchors},
        background_flush=False,
    ) as recorder:
        recorder.emit("call", {"frame": "science", "function": "diffuse"})
        recorder.emit("return", {"frame": "science"})
    return str(recorder.path)


@pytest.mark.parametrize("workspace", [False, True])
def test_recorded_run_uuid_requires_binding_and_reports_available_events(
    tmp_path, workspace
):
    path = run_recording(tmp_path)
    ctx = context(root=str(tmp_path), run_root="")

    def observe(selected):
        if workspace:
            return read([path], ctx=selected)
        return UI._read(selected, "", 0, "", None, Event())

    rejected = observe(ctx)
    assert rejected["snapshot"] is None and not rejected["sources"]
    assert rejected["empty_status"] == "unassociated"
    (row,) = rejected["processes"]
    assert row["events"] >= 2 and row["status"] == "unassociated"
    assert "exact project run is not bound" in row["reason"]
    assert ":run select RUN_ID" in row["reason"]
    bound = observe(dict(ctx, run_id="launch-token-a1"))
    assert bound["sources"] == [path] and bound["snapshot"]
    assert bound.get("association_verified", bound["verified"])


def test_negative_metadata_cache_keeps_unassociated_feedback(tmp_path):
    path = run_recording(tmp_path)
    first = read([path])
    cached = read([], cache=True, processes=first["processes"])
    assert cached["empty_status"] == "unassociated"
    assert cached["processes"] == first["processes"]
    assert not cached["sources"] and not cached["association_verified"]


def test_cached_recording_that_changes_attempt_keeps_rejection_reason(tmp_path):
    path = run_recording(tmp_path)
    ctx = context(run_id="launch-token-a1")
    first = read([path], ctx=ctx)
    assert first["association_verified"]
    manifest = json.loads(Path(path).read_text())
    manifest["identity"].update(run_id="another-run", attempt="another-run")
    Path(path).write_text(json.dumps(manifest))
    changed = read([path], ctx=ctx, cache=True, processes=first["processes"])
    assert not changed["snapshot"] and not changed["sources"]
    assert changed["empty_status"] == "unassociated"
    assert "does not match" in changed["processes"][0]["reason"]


def test_contradictory_scheduler_anchor_never_becomes_a_verified_run(tmp_path):
    path = run_recording(tmp_path, slurm_submit="old-submit", slurm_start="start")
    value = read([path], ctx=context(run_id="launch-token-a1"))
    assert not value["sources"] and not value["association_verified"]
    assert "scheduler timestamps conflict" in value["processes"][0]["reason"]


def test_unreadable_metadata_preserves_actual_error_and_missing_stays_missing(tmp_path):
    missing_path = str(tmp_path / "not-published.json")
    unavailable = read([missing_path])
    assert unavailable["empty_status"] == "unavailable"
    assert missing_path in unavailable["processes"][0]["reason"]
    assert read([])["empty_status"] == "missing"
    assert D.empty_status([]) == "missing"


@pytest.mark.parametrize("status", ["unassociated", "unavailable"])
def test_empty_views_show_published_reason_without_claiming_capture_was_disabled(
    status,
):
    state = {"status": status, "processes": [{"reason": "retained diagnostic"}]}
    text = L.to_text(UI._empty_recording_rows(state), 160)
    assert "retained diagnostic" in text
    assert "Launch with raven launch SCRIPT --trace" not in text
    assert "No verified recording is attached yet" not in text


def test_discovery_feedback_bounds_and_sanitizes_retained_messages():
    state = {
        "status": "unavailable",
        "processes": [{"reason": f"failure {index}\x1b[2J"} for index in range(100)],
    }
    rows = UI._empty_recording_rows(state)
    assert len(rows) == 5
    assert "\x1b" not in L.to_text(rows, 160)


def test_legacy_reader_error_is_not_reported_as_disabled_capture():
    text = L.to_text(
        UI._empty_recording_rows(
            {
                "status": "error",
                "error": "Permission denied opening retained manifest",
                "processes": [],
            }
        ),
        160,
    )
    assert "Permission denied opening retained manifest" in text
    assert "Launch with raven launch SCRIPT --trace" not in text


def test_global_feedback_instructions_wrap_at_eighty_columns(dashboard):
    owner = connect(dashboard)
    owner.tab = "traces"
    ctx = UI._context(owner)
    assert ctx
    state = UI.initialize(owner)
    state.update(
        context=ctx,
        status="unassociated",
        processes=[
            {
                "reason": D.association_reason(
                    {"run_id": "recorded-run"}, dict(ctx, run_id="")
                ),
            }
        ],
    )
    rows, hits = UI.render_global(
        dashboard.views, owner.store.snapshot(), owner, 80, 60
    )
    text = "".join(L.row_text(row) for row in rows)
    assert ":project PATH" in text and ":run select RUN_ID" in text
    assert all(L.vlen(L.row_text(row)) <= 80 for row in rows)
