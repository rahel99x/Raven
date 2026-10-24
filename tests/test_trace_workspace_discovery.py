"""Workspace discovery reads bounded declarations, never a duplicate journal."""

import json
from pathlib import Path
from threading import Event

import pytest

from raven.tracing import protocol as P
from raven.tracing import registrations as R
from raven.tracing import ui as UI
from raven.tracing import workspace_discovery as D
from raven.tracing.capture import Recorder


def context(**extra):
    return {
        "job_id": "42",
        "cluster": "local",
        "submit": "submit",
        "start": "start",
        "run_id": "",
        "root": "",
        "run_root": "/unused-run",
        "remote": False,
        "light": False,
        "show_helpers": None,
        **extra,
    }


def recording(
    root, *, role="main", attempt="submit/start", cluster="local", helpers=False
):
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    with Recorder(
        root,
        job_id="42",
        attempt=attempt,
        cluster=cluster,
        identity_extra={"role": role},
        background_flush=False,
        coverage={
            "adapter": "fixture",
            "scope": "Discovery fixture",
            "helpers_default": helpers,
        },
    ) as rec:
        rec.emit("call", {"frame": "f", "function": "work"})
        rec.emit("return", {"frame": "f"})
    return str(rec.path)


def read(
    paths,
    *,
    ctx=None,
    explicit="",
    index=0,
    event=None,
    cache=False,
    processes=None,
    gaps=(),
):
    return D.read(
        ctx or context(),
        explicit,
        index,
        event or Event(),
        source_cache=paths if cache else None,
        process_cache=processes,
        launch_sources=lambda context: (paths, list(gaps)),
        matches=UI._matches,
        process=UI._process,
    )


def test_source_metadata_is_not_a_journal_verification_or_duplicate_reader(
    tmp_path, monkeypatch
):
    path = recording(tmp_path)
    monkeypatch.setattr(
        P,
        "TraceReader",
        lambda *args, **kwargs: pytest.fail(
            "Metadata discovery constructed a journal reader"
        ),
    )
    value = read([path])
    assert value["sources"] == [path]
    assert value["reader"] is None and value["verified"] is False
    assert value["association_verified"] is True
    snapshot = value["snapshot"]
    assert snapshot["events"] == snapshot["stacks"] == []
    assert snapshot["workspace_catalog_only"] and snapshot["status"] == "WAITING"
    assert snapshot["read_seq"] == 0 and not snapshot["caught_up"]
    assert snapshot["committed_seq"] > 0 and not snapshot["complete"]


def test_metadata_anchor_prefers_main_and_keeps_only_helpers_viewable(tmp_path):
    helper = recording(tmp_path / "helper", role="pool-worker")
    main = recording(tmp_path / "main")
    assert read([helper, main])["path"] == main
    assert read([helper])["path"] == helper
    assert read([helper, main], ctx=context(show_helpers=True))["path"] == helper
    assert read([helper, main], explicit=helper)["path"] == helper
    opted_in = recording(tmp_path / "opted", role="pool-worker", helpers=True)
    assert read([opted_in, main])["path"] == opted_in


def test_cached_role_change_does_not_select_the_same_helper_forever(tmp_path):
    helper = recording(tmp_path / "helper", role="pool-worker")
    main = recording(tmp_path / "main")
    stale = UI._process(P.read_json(helper), helper)
    stale["role"] = "main"
    current = UI._process(P.read_json(main), main)
    value = read([helper, main], cache=True, processes=[stale, current])
    assert value["path"] == main and not value["discovered"]


def test_one_unreadable_manifest_keeps_other_associated_sources_usable(tmp_path):
    malformed = tmp_path / "broken.json"
    malformed.write_text('{"incomplete":')
    good = recording(tmp_path / "good")
    value = read([str(malformed), good])
    assert value["path"] == good and value["association_verified"]
    assert value["sources"] == [good]
    assert value["discovery_errors"][0]["path"] == str(malformed)
    assert value["discovery_errors"][0]["read_error"]
    assert (
        next(row for row in value["processes"] if row["path"] == str(malformed))[
            "status"
        ]
        == "unavailable"
    )
    with pytest.raises(ValueError):
        read([good], explicit=str(malformed))


def test_fresh_foreign_clusters_and_retained_old_attempts_are_ignored(tmp_path):
    foreign = recording(tmp_path / "foreign", cluster="another-site")
    old = recording(tmp_path / "old", attempt="old-attempt")
    good = recording(tmp_path / "good")
    value = read([foreign, old, good])
    assert value["sources"] == [good] and not value["discovery_errors"]
    with pytest.raises(ValueError, match="cluster"):
        read([good], explicit=foreign)


def test_cached_selected_manifest_is_revalidated_and_never_resumes_old_attempt(
    tmp_path,
):
    changed = recording(tmp_path / "changed")
    good = recording(tmp_path / "good")
    process = UI._process(P.read_json(changed), changed)
    manifest = P.read_json(changed)
    manifest["identity"]["attempt"] = "new-attempt"
    Path(changed).write_text(json.dumps(manifest))
    value = read([changed, good], cache=True, processes=[process])
    assert value["path"] == good and value["association_verified"]
    assert changed not in value["sources"]


def test_standalone_metadata_never_calls_scheduler_association_or_launch_discovery(
    tmp_path,
):
    path = recording(tmp_path)

    def forbidden(*args):
        pytest.fail("Declared recording was promoted to scheduler association")

    value = D.read(
        {"standalone": True, "path": path, "remote": False},
        "",
        0,
        Event(),
        launch_sources=forbidden,
        matches=forbidden,
        process=UI._process,
    )
    assert value["snapshot"]["identity"]["job_id"] == "42"
    assert value["verified"] is False and value["association_verified"] is False
    assert "scheduler association not checked" in value["association"]


def test_explicit_recording_directory_uses_the_same_absolute_manifest_as_index(
    tmp_path, monkeypatch
):
    path = recording(tmp_path)
    monkeypatch.chdir(tmp_path)
    relative = str(Path(path).parent.relative_to(tmp_path))
    value = read([], explicit=relative)
    assert value["path"] == path and value["sources"] == [path]


def test_remote_or_cancelled_context_never_substitutes_local_files(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        P,
        "read_json",
        lambda *args, **kwargs: pytest.fail("Unexpected local metadata read"),
    )
    with pytest.raises(ValueError, match="remote views never substitute"):
        read([], ctx=context(remote=True))
    event = Event()
    event.set()
    assert read([], event=event) is None


def test_discovery_source_budget_is_checked_before_source_manifest_reads(monkeypatch):
    monkeypatch.setattr(
        P,
        "read_json",
        lambda *args, **kwargs: pytest.fail(
            "Source quota was enforced after a manifest read"
        ),
    )
    with pytest.raises(ValueError, match="512-source"):
        read(["/unused/source-" + str(index) for index in range(513)])
    with pytest.raises(ValueError, match="512-source"):
        read(["/unused/source-" + str(index) for index in range(513)], cache=True)


def test_cancel_after_first_metadata_read_publishes_no_partial_catalog(
    tmp_path, monkeypatch
):
    first = recording(tmp_path / "a")
    second = recording(tmp_path / "b")
    event = Event()
    original = P.read_json

    def cancelling(path, **kwargs):
        value = original(path, **kwargs)
        event.set()
        return value

    monkeypatch.setattr(P, "read_json", cancelling)
    assert read([first, second], event=event) is None


def test_metadata_summary_is_bounded_and_terminal_safe(tmp_path):
    path = recording(tmp_path)
    doc = P.read_json(path)
    doc["warnings"] = ["\x1b]8;;https://invalid.example\x07" + "x" * 3000]
    Path(path).write_text(json.dumps(doc))
    value = read([path])
    assert len(value["processes"][0]["reason"]) <= 4096
    assert "\x1b" not in value["processes"][0]["reason"]


def test_cached_bound_run_is_revalidated_before_metadata_publication(
    tmp_path, monkeypatch
):
    from raven import projects

    path = recording(tmp_path)

    def changed(*args, **kwargs):
        raise ValueError("run attempt identity changed")

    monkeypatch.setattr(projects, "read_bound_json", changed)
    with pytest.raises(ValueError, match="run attempt identity changed"):
        read([path], cache=True, ctx=context(binding={"job_id": "42"}))


def test_candidate_parser_never_opens_individual_manifests_and_legacy_still_verifies(
    tmp_path,
):
    path = recording(tmp_path)
    calls = []

    def loader(target, **kwargs):
        calls.append(str(target))
        if str(target) == path:
            pytest.fail("Candidate parser opened a source manifest")
        return P.read_json(target, **kwargs)

    candidates = R.discover_trace_candidates(tmp_path, "42", read_json=loader)
    assert candidates == [
        {
            "path": path,
            "expected": {
                "job_id": "42",
                "stream": P.read_json(path)["identity"]["stream"],
            },
        }
    ]
    assert calls == [str(tmp_path / ".raven/traces/index.json")]
    assert R.discover_trace(
        tmp_path, "42", read_json=P.read_json, validate_identity=P.validate_identity
    ) == [path]


def test_project_candidates_keep_registration_stream_expectations(tmp_path):
    recording(tmp_path)
    registry = tmp_path / ".raven/traces/index.json"
    doc = P.read_json(registry)
    doc["records"][0]["stream"] = "wrongstream"
    registry.write_text(json.dumps(doc))
    value = read([], ctx=context(root=str(tmp_path)))
    assert value["snapshot"] is None
    assert "registration identity" in value["discovery_errors"][0]["read_error"]
    with pytest.raises(ValueError, match="stream"):
        P.discover_trace(tmp_path, "42")


def test_project_and_launch_candidates_merge_without_duplicate_journal_owners(tmp_path):
    path = recording(tmp_path)
    value = read([path], ctx=context(root=str(tmp_path)))
    assert value["sources"] == [path] and value["association_verified"]


def test_registered_run_source_expansion_is_bounded_before_manifest_reads(tmp_path):
    registry = tmp_path / ".raven/traces"
    registry.mkdir(parents=True)
    runs = []
    for index in range(2):
        relative = "runs/run" + str(index) + "/trace/index.json"
        target = tmp_path / relative
        target.parent.mkdir(parents=True)
        sources = [
            {
                "schema": "raven.trace/v1",
                "path": "trace/p" + str(source) + "/manifest.json",
                "stream": "stream" + str(source),
            }
            for source in range(300)
        ]
        target.write_text(
            json.dumps({"schema": "raven.trace-index/v1", "sources": sources})
        )
        runs.append(
            {
                "schema": "raven.trace-run/v1",
                "path": relative,
                "job_id": "42",
                "attempt": "submit/start",
            }
        )
    (registry / "index.json").write_text(
        json.dumps({"schema": "raven.trace-index/v1", "records": runs})
    )
    opened = []

    def metadata_only(path, **kwargs):
        opened.append(str(path))
        assert str(path).endswith("index.json")
        return P.read_json(path, **kwargs)

    with pytest.raises(ValueError, match="512-source"):
        R.discover_trace_candidates(tmp_path, "42", read_json=metadata_only)
    assert len(opened) == 3


@pytest.mark.parametrize(
    "relative", ["../escape", "/absolute", "runs/../escape", "", "bad\npath"]
)
def test_global_registration_paths_remain_fail_closed(tmp_path, relative):
    registry = tmp_path / ".raven/traces"
    registry.mkdir(parents=True)
    (registry / "index.json").write_text(
        json.dumps(
            {
                "schema": "raven.trace-index/v1",
                "records": [{"job_id": "42", "path": relative}],
            }
        )
    )
    with pytest.raises(ValueError):
        P.discover_trace_candidates(tmp_path, "42")


def test_candidate_metadata_reservations_and_cancellation_are_explicit(tmp_path):
    recording(tmp_path)
    with pytest.raises(ValueError, match="cumulative metadata"):
        P.discover_trace_candidates(tmp_path, "42", max_metadata_bytes=1024)
    event = Event()
    event.set()
    with pytest.raises(InterruptedError, match="cancelled"):
        P.discover_trace_candidates(tmp_path, "42", cancel=event)
