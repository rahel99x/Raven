"""Redundant run aliases unify normal CPU/device capture, never attempts."""

import json

import pytest

from raven.tracing.capture import Recorder
from raven.tracing.workspace_index import TraceIndex
from raven.tracing.workspace_model import project


def recording(root, *, run_id=None, device=False):
    root.mkdir(mode=0o700)
    coverage = None
    if device:
        coverage = dict(
            adapter="portable activity import",
            scope="Declared report fixture",
            clock=dict(
                schema="raven.trace-clock/v1",
                kind="producer-report",
                id="report-clock",
                report_sha256="a" * 64,
            ),
        )
    with Recorder(
        root,
        job_id="42",
        attempt="run-a",
        cluster="local",
        identity_extra={"run_id": run_id} if run_id else None,
        coverage=coverage,
        background_flush=False,
        initial_mono_ns=0,
    ) as rec:
        if device:
            rec.emit(
                "activity",
                dict(
                    activity_kind="host",
                    name="enqueue",
                    start_ns=10,
                    end_ns=15,
                    source_pid=99,
                    source_tid=1,
                ),
                mono_ns=10,
            )
            rec.emit(
                "activity",
                dict(
                    activity_kind="gpu_kernel",
                    name="kernel",
                    start_ns=20,
                    end_ns=30,
                    source_pid=99,
                    source_tid=2,
                    device={"uuid": "GPU-fixture", "ordinal": 0},
                    context="context",
                    queue="stream",
                ),
                mono_ns=20,
            )
        else:
            rec.emit(
                "call",
                {"frame": "f", "function": "CPU work"},
                thread="one",
                mono_ns=100,
            )
            rec.emit("return", {"frame": "f"}, thread="one", mono_ns=140)
    return rec.path


def finish(index, paths, **kwargs):
    for _ in range(100):
        result = index.refresh(paths, max_events=512, max_seconds=0.1, **kwargs)
        if result["caught_up"]:
            return result
    pytest.fail("Compatible source identities did not catch up")


@pytest.mark.parametrize("alias_first", [True, False])
def test_automatic_python_run_alias_and_imported_device_share_exact_attempt(
    alias_first, tmp_path
):
    python = recording(tmp_path / "python", run_id="run-a")
    activity = recording(tmp_path / "activity", device=True)
    paths = [python, activity] if alias_first else [activity, python]
    with TraceIndex(tmp_path / "index.sqlite") as index:
        catalog = finish(index, paths)
        assert catalog["identity"] == {
            "job_id": "42",
            "attempt": "run-a",
            "cluster": "local",
        }
        sources = {row["path"]: row for row in catalog["sources"]}
        assert sources[str(python)]["identity"]["run_id"] == "run-a"
        assert "run_id" not in sources[str(activity)]["identity"]
        result = index.query()
        assert {row["kind"] for row in result["intervals"]} == {
            "python",
            "host",
            "gpu_kernel",
        }
        assert len(result["bounds"]) == 2  # No invented cross-report alignment.
        gpu = project(result, "gpu")
        assert len(gpu["intervals"]) == 1 and gpu["intervals"][0]["name"] == "kernel"


@pytest.mark.parametrize(
    "first_id,second_id",
    [
        ("custom-a", "custom-b"),
        ("custom-a", None),
        (None, "custom-a"),
        ("run-a", "custom-a"),
    ],
)
def test_distinct_custom_run_ids_remain_an_extra_exact_identity_constraint(
    first_id, second_id, tmp_path
):
    one = recording(tmp_path / "one", run_id=first_id)
    two = recording(tmp_path / "two", run_id=second_id)
    with TraceIndex(tmp_path / "index.sqlite") as index:
        finish(index, [one])
        with pytest.raises(ValueError, match="different job attempts"):
            index.refresh([two])


def test_explicit_expected_run_id_remains_exact_even_for_a_redundant_alias(tmp_path):
    alias = recording(tmp_path / "alias", run_id="run-a")
    missing = recording(tmp_path / "missing")
    with TraceIndex(tmp_path / "index.sqlite") as index:
        finish(index, [alias], expected={"run_id": "run-a"})
        with pytest.raises(ValueError, match="expected exact run attempt"):
            index.refresh([missing], expected={"run_id": "run-a"})
        assert index.catalog()["sources"][0]["identity"]["run_id"] == "run-a"


def test_old_cached_alias_metadata_migrates_without_rewriting_source_identity(tmp_path):
    alias = recording(tmp_path / "alias", run_id="run-a")
    imported = recording(tmp_path / "imported", device=True)
    cache = tmp_path / "index.sqlite"
    with TraceIndex(cache) as index:
        finish(index, [alias])
        original_id = index.query()["intervals"][0]["id"]
        old_binding = dict(
            job_id="42", attempt="run-a", cluster="local", run_id="run-a"
        )
        index.db.execute(
            "UPDATE metadata SET value=? WHERE key='identity'",
            (json.dumps(old_binding),),
        )
        index.db.commit()
    with TraceIndex(cache) as index:
        binding = json.loads(
            index.db.execute(
                "SELECT value FROM metadata WHERE key='identity'"
            ).fetchone()[0]
        )
        assert "run_id" not in binding
        finish(index, [alias, imported])
        assert index.detail(original_id)["name"] == "CPU work"
        identities = [row["identity"] for row in index.catalog()["sources"]]
        assert any(identity.get("run_id") == "run-a" for identity in identities)


@pytest.mark.parametrize(
    "metadata",
    [
        None,
        [],
        {
            "job_id": "42",
            "attempt": "run-a",
            "cluster": "local",
            "run_id": "run-a",
            "unknown_identity_constraint": "keep",
        },
    ],
)
def test_cache_migration_rejects_malformed_or_unknown_identity_constraints(
    tmp_path, metadata
):
    cache = tmp_path / "index.sqlite"
    with TraceIndex(cache) as index:
        index.db.execute(
            "INSERT INTO metadata VALUES('identity',?)", (json.dumps(metadata),)
        )
        index.db.commit()
    with pytest.raises(ValueError, match="Invalid cached trace identity"):
        TraceIndex(cache)
