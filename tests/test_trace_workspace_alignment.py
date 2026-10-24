"""Recorded clocks are queried through an explicit uncertain reference axis."""

from __future__ import annotations

import pytest

from raven.tracing.capture import Recorder
from raven.tracing.workspace_index import TraceIndex
from raven.tracing.workspace_model import project, source_clock


def clock(
    anchor,
    reference="run-reference",
    *,
    rate_num=1,
    rate_den=1,
    uncertainty=2,
    valid_end=10000,
):
    return dict(
        schema="raven.trace-clock/v1",
        kind="monotonic",
        id="monotonic",
        calibration=dict(
            schema="raven.trace-clock-calibration/v1",
            reference_id=reference,
            source_anchor_ns=anchor,
            reference_anchor_ns=1000,
            rate_num=rate_num,
            rate_den=rate_den,
            uncertainty_ns=uncertainty,
            valid_start_ns=0,
            valid_end_ns=valid_end,
            provenance="Deterministic fixture anchors, not live node qualification",
        ),
    )


def recording(root, *, node, start, end, metadata=None, name="work"):
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    coverage = {"adapter": "cooperative", "scope": "explicit fixture events"}
    if metadata is not None:
        coverage["clock"] = metadata
    rec = Recorder(
        root,
        job_id="42",
        attempt="one-attempt",
        cluster="local",
        coverage=coverage,
        background_flush=False,
        initial_mono_ns=0,
    )
    rec.identity["node"] = node
    rec.emit("call", {"frame": "call", "function": name}, thread="one", mono_ns=start)
    rec.emit("return", {"frame": "call"}, thread="one", mono_ns=end)
    rec.emit("stream_end", {}, mono_ns=end + 1)
    rec.state = "CLOSED"
    rec.flush()
    rec.close()
    return rec


def finish(index, paths):
    for _ in range(1000):
        result = index.refresh(paths, max_events=64, max_seconds=0.1)
        if result["caught_up"]:
            return result
    pytest.fail("Calibrated index did not catch up")


def test_actual_journals_reach_reference_query_before_limit_and_keep_raw_values(
    tmp_path,
):
    first = recording(
        tmp_path / "a", node="node-a", start=100, end=120, metadata=clock(100), name="a"
    )
    second = recording(
        tmp_path / "b",
        node="node-b",
        start=5000,
        end=5020,
        metadata=clock(5000),
        name="b",
    )
    with TraceIndex(tmp_path / "index.sqlite") as index:
        catalog = finish(index, [first.path, second.path])
        raw = index.query()
        assert {row["start_ns"] for row in raw["intervals"]} == {100, 5000}
        assert len({row["clock_id"] for row in raw["intervals"]}) == 2
        result = index.query(
            start_ns=1005, end_ns=1010, clock_id="run-reference", limit=1
        )
        assert len(result["intervals"]) == 1 and result["truncated"]
        row = result["intervals"][0]
        assert row["clock_id"] == "run-reference"
        assert (row["start_ns"], row["end_ns"], row["duration_ns"]) == (1000, 1020, 20)
        assert row["raw_start_ns"] in {100, 5000}
        assert row["raw_duration_ns"] == 20 and row["raw_clock_id"] != "run-reference"
        assert row["alignment_uncertainty_ns"] == 2
        shown = project(result)
        assert shown["intervals"][0]["start_ns"] == 1000
        assert shown["lanes"][0]["clock_id"] == "run-reference"
        assert all(bound["clock_id"] != "run-reference" for bound in catalog["bounds"])
        assert any(
            bound["clock_id"] == "run-reference" for bound in catalog["aligned_bounds"]
        )
        assert index.detail(row["id"])["start_ns"] == row["raw_start_ns"]


def test_reference_query_includes_possible_uncertainty_overlap(tmp_path):
    rec = recording(
        tmp_path / "project",
        node="node-a",
        start=100,
        end=120,
        metadata=clock(100, uncertainty=5),
    )
    with TraceIndex(tmp_path / "index.sqlite") as index:
        finish(index, [rec.path])
        result = index.query(start_ns=996, end_ns=999, clock_id="run-reference")
        assert len(result["intervals"]) == 1
        assert result["intervals"][0]["start_ns"] == 1000
        assert result["intervals"][0]["alignment_uncertainty_ns"] == 5


def test_reference_axis_keeps_raw_host_and_calibrated_other_node(tmp_path):
    host = recording(
        tmp_path / "host", node="reference-host", start=1000, end=1020, name="host"
    )
    reference = source_clock(host.identity)["id"]
    other = recording(
        tmp_path / "other",
        node="other-host",
        start=5000,
        end=5020,
        metadata=clock(5000, reference),
        name="other",
    )
    with TraceIndex(tmp_path / "index.sqlite") as index:
        finish(index, [host.path, other.path])
        result = index.query(start_ns=1001, end_ns=1010, clock_id=reference)
        assert {row["name"] for row in result["intervals"]} == {"host", "other"}
        assert len({row["id"] for row in result["intervals"]}) == 2
        assert sum("raw_start_ns" in row for row in result["intervals"]) == 1


def test_drift_maps_displayed_elapsed_but_does_not_scale_self_as_exact_measurement(
    tmp_path,
):
    rec = recording(
        tmp_path / "project",
        node="node-a",
        start=100,
        end=120,
        metadata=clock(100, rate_num=2),
    )
    with TraceIndex(tmp_path / "index.sqlite") as index:
        finish(index, [rec.path])
        row = index.query(start_ns=1000, end_ns=1040, clock_id="run-reference")[
            "intervals"
        ][0]
        assert row["duration_ns"] == 40 and row["raw_duration_ns"] == 20
        assert row["raw_self_ns"] == 20 and row["self_ns"] is None
        assert "not independently qualified" in row["alignment_status"]


def test_outside_calibration_validity_retains_raw_source_without_fabricated_reference_row(
    tmp_path,
):
    rec = recording(
        tmp_path / "project",
        node="node-a",
        start=100,
        end=120,
        metadata=clock(100, valid_end=110),
    )
    with TraceIndex(tmp_path / "index.sqlite") as index:
        result = finish(index, [rec.path])
        assert index.query()["intervals"]
        assert not index.query(clock_id="run-reference")["intervals"]
        assert any("validity" in warning for warning in result["warnings"])


def test_calibrated_density_and_statistics_are_incremental_and_not_double_counted(
    tmp_path,
):
    first = recording(
        tmp_path / "a",
        node="a",
        start=100,
        end=120,
        metadata=clock(100, rate_num=2),
    )
    second = recording(
        tmp_path / "b",
        node="b",
        start=5000,
        end=5020,
        metadata=clock(5000, rate_num=2),
    )
    with TraceIndex(tmp_path / "index.sqlite") as index:
        finish(index, [first.path, second.path])
        statements = []
        index.db.set_trace_callback(statements.append)
        stats = index.functions(clock_id="run-reference")
        overview = index.overview(clock_id="run-reference", bins=32)
        index.db.set_trace_callback(None)
        assert sum(row["count"] for row in stats["functions"]) == 2
        assert sum(row["inclusive_ns"] for row in stats["functions"]) == 80
        assert all(row["self_ns"] is None for row in stats["functions"])
        assert sum(row["count"] for row in overview["domains"][0]["bins"]) == 2
        assert overview["domains"][0]["clock_id"] == "run-reference"
        assert not any(
            "FROM intervals " in sql or "FROM aligned_intervals " in sql
            for sql in statements
        )
        assert sum(row["inclusive_ns"] for row in index.functions()["functions"]) == 40
        assert (
            sum(
                row["count"]
                for domain in index.overview()["domains"]
                for row in domain["bins"]
            )
            == 2
        )
        ranged = index.functions(clock_id="run-reference", start_ns=1001, end_ns=1002)
        assert sum(row["inclusive_ns"] for row in ranged["functions"]) == 80
    with TraceIndex(tmp_path / "index.sqlite") as index:
        assert not index.query(clock_id="run-reference")["intervals"]
        assert index.functions(clock_id="run-reference")["alignment"].startswith(
            "raw recorded clocks"
        )
        finish(index, [first.path, second.path])
        assert (
            sum(
                row["count"]
                for row in index.functions(clock_id="run-reference")["functions"]
            )
            == 2
        )


def test_changed_calibration_requires_new_source_generation(tmp_path):
    project = tmp_path / "project"
    project.mkdir(mode=0o700)
    with Recorder(
        project,
        job_id="42",
        attempt="one-attempt",
        cluster="local",
        coverage={
            "adapter": "cooperative",
            "scope": "fixture events",
            "clock": clock(100),
        },
        background_flush=False,
        initial_mono_ns=0,
    ) as rec:
        rec.emit("call", {"frame": "f", "function": "f"}, mono_ns=100)
        rec.emit("return", {"frame": "f"}, mono_ns=120)
        rec.flush()
        with TraceIndex(tmp_path / "index.sqlite") as index:
            finish(index, [rec.path])
            assert index.query(clock_id="run-reference")["intervals"]
            rec.coverage["clock"]["calibration"]["reference_id"] = "changed-reference"
            rec.flush()
            result = index.refresh([rec.path], max_seconds=0.1)
            assert result["sources"][0]["status"] == "INVALID"
            assert any("calibration changed" in row for row in result["warnings"])
            assert not index.query(clock_id="run-reference")["intervals"]


def test_scheduling_intervals_reach_index_without_function_stats_or_symbol_failure(
    tmp_path,
):
    project = tmp_path / "project"
    project.mkdir(mode=0o700)
    with Recorder(
        project,
        job_id="42",
        attempt="one-attempt",
        cluster="local",
        background_flush=False,
        initial_mono_ns=0,
    ) as rec:
        rec.emit(
            "activity",
            {
                "activity_kind": "scheduling",
                "name": "on CPU",
                "state": "running",
                "logical_cpu": 2,
                "start_ns": 100,
                "end_ns": 120,
            },
            mono_ns=100,
        )
    with TraceIndex(tmp_path / "index.sqlite") as index:
        finish(index, [rec.path])
        row = index.query()["intervals"][0]
        assert row["kind"] == "scheduling" and row["duration_ns"] == 20
        assert index.detail(row["id"])["symbol_scope"]["source_id"] == row["source_id"]
        assert not index.functions()["functions"]
