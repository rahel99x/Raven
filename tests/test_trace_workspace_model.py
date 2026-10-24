"""Clock, topology, and statistics preserve evidence rather than inventing it."""

from __future__ import annotations

import copy
import json

import pytest

from raven.tracing import workspace_model as M


def identity(**values):
    return dict(
        cluster="cluster",
        node="node-a",
        boot_id="boot-a",
        pid=1,
        process_start="start-a",
        stream="stream0001",
        **values,
    )


def ident(**values):
    result = identity()
    result.update(values)
    return result


def source(identifier="s1", **values):
    return dict(
        id=identifier, identity=ident(**values), coverage={}, clock={"id": "clock-a"}
    )


def interval(identifier="i1", start=0, end=100, **values):
    row = dict(
        id=identifier,
        source_id="s1",
        clock_id="clock-a",
        thread="t1",
        depth=0,
        kind="python",
        name="solve",
        file="/project/solve.py",
        line=3,
        first_seq=1,
        last_seq=2,
        start_ns=start,
        end_ns=end,
        duration_ns=end - start,
        self_ns=None,
        data={},
    )
    row.update(values)
    return row


def query(*rows, markers=(), sources=None, **values):
    return dict(
        intervals=list(rows),
        markers=list(markers),
        sources=[source()] if sources is None else sources,
        warnings=[],
        **values,
    )


def calibration(**values):
    result = dict(
        schema="raven.trace-clock-calibration/v1",
        reference_id="reference-clock",
        source_anchor_ns=100,
        reference_anchor_ns=1000,
        rate_num=1,
        rate_den=1,
        uncertainty_ns=5,
        valid_start_ns=0,
        valid_end_ns=1000,
    )
    result.update(values)
    return result


def metric(result, name):
    return [row for row in result["metrics"] if row["name"] == name]


def test_known_host_monotonic_domains_are_stable_but_nodes_boots_and_names_are_separate():
    original = M.source_clock(ident())
    assert original["id"] == M.source_clock(ident(stream="stream0002", pid=2))["id"]
    assert original["alignment"] == "same-host-monotonic"
    for changed in [dict(node="node-b"), dict(boot_id="boot-b"), dict(cluster="other")]:
        assert original["id"] != M.source_clock(ident(**changed))["id"]
    assert (
        original["id"]
        != M.source_clock(
            ident(),
            {
                "schema": "raven.trace-clock/v1",
                "kind": "monotonic",
                "id": "monotonic-raw",
            },
        )["id"]
    )


@pytest.mark.parametrize("field", ["node", "boot_id", "cluster"])
def test_missing_host_clock_identity_isolated_by_process_stream(field):
    first = M.source_clock(ident(**{field: "unknown"}))
    second = M.source_clock(ident(stream="stream0002", **{field: "unknown"}))
    assert first["id"] != second["id"]
    assert first["alignment"] == "source-local"


def test_report_clock_is_scoped_by_artifact_not_ordinal_or_display_name():
    data = {
        "schema": "raven.trace-clock/v1",
        "kind": "producer-report",
        "id": "device-zero",
        "report_sha256": "a" * 64,
    }
    assert (
        M.source_clock(ident(), data)["id"]
        == M.source_clock(ident(node="other"), {"clock": data})["id"]
    )
    assert (
        M.source_clock(ident(), data)["id"]
        != M.source_clock(ident(), dict(data, report_sha256="b" * 64))["id"]
    )
    assert "not independently qualified" in M.source_clock(ident(), data)["provenance"]
    assert M.source_clock(
        ident(), dict(data, description={"label": "GPU"})
    ) == M.source_clock(ident(), data)


def test_owned_producer_session_clock_shares_only_explicit_process_lifetime():
    descriptor = {
        "schema": "raven.trace-clock/v1",
        "kind": "producer-session",
        "id": "torch-profiler-relative-ns",
        "session_id": "session-a",
        "producer": {
            "node": "compute-a",
            "boot_id": "boot-a",
            "pid": 42,
            "process_start": "1234",
        },
    }
    one = M.source_clock(ident(), descriptor)
    two = M.source_clock(ident(stream="other-stream", pid=2), descriptor)
    assert one["id"] == two["id"] and one["alignment"] == "producer-session"
    assert "not independently qualified" in one["provenance"]
    for field, value in [
        ("node", "other-node"),
        ("boot_id", "new-boot"),
        ("pid", 43),
        ("process_start", "new-process"),
    ]:
        changed = dict(
            descriptor, producer=dict(descriptor["producer"], **{field: value})
        )
        assert M.source_clock(ident(), changed)["id"] != one["id"]
    changed = dict(descriptor, session_id="another-session")
    assert M.source_clock(ident(), changed)["id"] != one["id"]
    assert M.source_clock(ident(cluster="other"), descriptor)["id"] != one["id"]


@pytest.mark.parametrize("field", ["node", "boot_id", "process_start"])
def test_incomplete_producer_lifetime_never_merges_sources_by_session_uuid(field):
    descriptor = {
        "schema": "raven.trace-clock/v1",
        "kind": "producer-session",
        "session_id": "session-a",
        "producer": {
            "node": "compute-a",
            "boot_id": "boot-a",
            "pid": 42,
            "process_start": "1234",
        },
    }
    descriptor["producer"][field] = "unknown"
    one = M.source_clock(ident(), descriptor)
    two = M.source_clock(ident(stream="other-stream"), descriptor)
    assert one["id"] != two["id"] and one["alignment"] == "source-local"


@pytest.mark.parametrize(
    "descriptor",
    [
        {"session_id": ""},
        {"producer": None},
        {"producer": {"node": "node-a"}},
        {
            "producer": {
                "node": "node-a",
                "boot_id": "boot-a",
                "pid": True,
                "process_start": "a",
            }
        },
    ],
)
def test_invalid_producer_session_clock_semantics_fail_closed(descriptor):
    data = {
        "schema": "raven.trace-clock/v1",
        "kind": "producer-session",
        "session_id": "session-a",
        "producer": {"node": "a", "boot_id": "b", "pid": 42, "process_start": "1234"},
    }
    data.update(descriptor)
    with pytest.raises(ValueError):
        M.source_clock(ident(), data)


@pytest.mark.parametrize(
    "bad",
    [
        {"schema": "other"},
        {"schema": "raven.trace-clock/v1", "kind": "invented"},
        {
            "schema": "raven.trace-clock/v1",
            "kind": "producer-report",
            "report_sha256": "a",
        },
        {"schema": "raven.trace-clock/v1", "resolution_ns": True},
        {"schema": "raven.trace-clock/v1", "resolution_ns": 0},
    ],
)
def test_malformed_clock_semantics_are_refused(bad):
    with pytest.raises(ValueError):
        M.source_clock(ident(), bad)


def test_affine_alignment_preserves_raw_integer_timestamps_and_error_bounds():
    base = (1 << 62) + 17
    cal = calibration(
        source_anchor_ns=base,
        reference_anchor_ns=base + 1000,
        valid_start_ns=base - 100,
        valid_end_ns=base + 100,
        rate_num=3,
        rate_den=2,
        rate_error_ppb=1,
    )
    clock = M.source_clock(
        ident(), {"schema": "raven.trace-clock/v1", "calibration": cal}
    )
    row = interval(start=base + 1, end=base + 3, clock_id=clock["id"])
    original = copy.deepcopy(row)
    result = M.align_interval(row, clock)
    assert row == original
    assert result["start_ns"] == base + 1
    assert result["aligned_start_ns"] == base + 1001
    assert result["aligned_end_ns"] == base + 1004
    assert result["aligned_clock_id"] == "reference-clock"
    assert result["alignment_uncertainty_ns"] == 7
    assert type(result["aligned_start_ns"]) is int
    assert M.source_clock(ident())["id"] == clock["id"]


@pytest.mark.parametrize(
    "bad",
    [
        {"uncertainty_ns": -1},
        {"rate_num": 0},
        {"rate_den": False},
        {"valid_end_ns": 50},
        {"reference_anchor_ns": float("nan")},
        {"rate_error_ppb": 1_000_000_001},
        {"source_anchor_ns": True},
    ],
)
def test_invalid_calibration_cannot_create_false_precision(bad):
    with pytest.raises(ValueError):
        M.validate_calibration(calibration(**bad))


def test_clock_transform_refuses_extrapolation_and_wrong_source_clock():
    with pytest.raises(ValueError, match="validity"):
        M.transform_time(1001, calibration())
    with pytest.raises(ValueError, match="does not match"):
        M.align_interval(interval(), {"id": "other", "calibration": calibration()})
    with pytest.raises(ValueError):
        M.transform_time(True, calibration())
    assert M.transform_time(99, calibration(rate_num=1, rate_den=3))["time_ns"] == 999


def test_execution_lanes_keep_process_thread_lifetimes_and_reported_identities_separate():
    rows = [
        interval(
            "a", data={"source_node": "reported-a", "source_pid": 10, "source_tid": 1}
        ),
        interval(
            "b", data={"source_node": "reported-a", "source_pid": 20, "source_tid": 1}
        ),
        interval(
            "c",
            data={
                "source_node": "reported-a",
                "source_pid": 10,
                "source_tid": 1,
                "thread_generation": "new",
            },
        ),
    ]
    original = query(*rows)
    before = copy.deepcopy(original)
    result = M.project(original)
    assert len(result["lanes"]) == 3
    assert len({row["lane_id"] for row in result["intervals"]}) == 3
    assert all(
        row["path"][0].startswith("Node reported-a · lifetime unknown")
        for row in result["lanes"]
    )
    assert all(row["boot_id"] == "unknown" for row in result["lanes"])
    assert all(
        row["identity_fidelity"] == "producer-declared report identities"
        for row in result["lanes"]
    )
    assert original == before


def test_cpu_projection_preserves_points_without_charging_calls_to_entry_core():
    point = interval(
        "point",
        start=10,
        end=20,
        kind="cpu_observation",
        logical_cpu=12,
        socket_id=1,
        core_id=2,
        data={"source": "linux-sched_getcpu"},
    )
    recorded_call = interval(logical_cpu=12, socket_id=1, core_id=2)
    result = M.project(query(recorded_call, markers=[point]), projection="cpu")
    assert [row["id"] for row in result["intervals"]] == ["point"]
    assert result["intervals"][0]["point"] is True
    assert result["intervals"][0]["render"] == "point"
    assert "not CPU residency" in result["intervals"][0]["duration_kind"]
    assert result["intervals"][0]["end_ns"] - result["intervals"][0]["start_ns"] == 10
    assert result["lanes"][0]["path"] == [
        "Node node-a · boot boot-a",
        "Socket 1",
        "Core 2",
        "Logical CPU 12",
    ]


def test_cpu_ids_do_not_collapse_between_sockets_nodes_or_unknown_topology():
    points = [
        interval(
            str(i),
            kind="cpu_observation",
            logical_cpu=0,
            socket_id=socket,
            core_id=0,
            source_id=sid,
        )
        for i, (sid, socket) in enumerate(
            [("s1", 0), ("s1", 1), ("s2", 0), ("s1", None)]
        )
    ]
    result = M.project(
        query(markers=points, sources=[source(), source("s2", node="node-b")]), "cpu"
    )
    assert len(result["lanes"]) == 4
    assert any("Socket unknown" in row["label"] for row in result["lanes"])


def test_only_explicit_running_scheduler_slices_become_cpu_execution_intervals():
    rows = [
        interval(
            "running", kind="scheduling", data={"state": "running", "logical_cpu": 2}
        ),
        interval(
            "blocked", kind="scheduling", data={"state": "blocked", "logical_cpu": 2}
        ),
        interval("missing", kind="scheduling", data={"logical_cpu": 2}),
    ]
    assert [row["id"] for row in M.project(query(*rows), "cpu")["intervals"]] == [
        "running"
    ]


def test_device_ids_account_for_nodes_partitions_and_local_visibility():
    first = M.device_identity(
        {"id": "0", "uuid": "physical", "partition": "a"},
        source_id="s1",
        node="node-a",
        boot_id="boot-a",
    )
    assert (
        first["id"]
        == M.device_identity(
            {"id": "7", "uuid": "physical", "partition": "a"},
            source_id="s2",
            node="node-a",
            boot_id="boot-a",
        )["id"]
    )
    for node, partition in [("node-b", "a"), ("node-a", "b")]:
        assert (
            first["id"]
            != M.device_identity(
                {"id": "0", "uuid": "physical", "partition": partition},
                source_id="s1",
                node=node,
                boot_id="boot-a",
            )["id"]
        )
    assert (
        M.device_identity({"ordinal": 0}, source_id="s1")["id"]
        != M.device_identity({"ordinal": 0}, source_id="s2")["id"]
    )
    with pytest.raises(ValueError):
        M.device_identity({"ordinal": False}, source_id="s1")
    with pytest.raises(ValueError):
        M.device_identity({"partition": {}}, source_id="s1")


def test_gpu_context_and_queue_ids_are_process_scoped_and_host_calls_stay_separate():
    rows = [
        interval(
            "a",
            kind="gpu_kernel",
            data={
                "source_pid": 10,
                "device": {"ordinal": 0},
                "context": "0",
                "queue": "0",
            },
        ),
        interval(
            "b",
            kind="gpu_kernel",
            data={
                "source_pid": 20,
                "device": {"ordinal": 0},
                "context": "0",
                "queue": "0",
            },
        ),
        interval("host", kind="host"),
    ]
    result = M.project(query(*rows), "gpu")
    assert len(result["lanes"]) == 2
    assert len(result["intervals"]) == 2
    assert len({row["device"]["id"] for row in result["lanes"]}) == 2
    assert "No device" in M.project(query(interval(kind="host")), "gpu")["warnings"][-1]


def test_projection_bounds_are_visible_and_inputs_are_not_silently_overloaded():
    result = M.project(
        query(interval("a", thread="a"), interval("b", thread="b")), max_lanes=1
    )
    assert result["hidden_lanes"] == 1 and result["truncated"]
    with pytest.raises(ValueError):
        M.project(query(), max_lanes=True)
    with pytest.raises(ValueError):
        M.project(query(), "mystery")
    with pytest.raises(ValueError, match="bounded"):
        M.project(query(*([interval()] * 20001)))


def test_union_duration_is_not_sum_and_does_not_cross_unaligned_clocks():
    assert M.union_duration([(0, 10), (5, 15), (15, 20), (30, 40)]) == 30
    assert M.union_duration([(0, 10), (5, 15)], start_ns=7, end_ns=12) == 5
    with pytest.raises(ValueError, match="different"):
        M.union_duration([interval(clock_id="a"), interval(clock_id="b")])
    with pytest.raises(ValueError):
        M.union_duration([(10, 0)])


def test_function_stats_distinguish_recorded_elapsed_self_unknown_and_binary_identity():
    rows = [
        interval("a", 0, 100, self_ns=60),
        interval("b", 100, 150, self_ns=20),
        interval("c", 0, 30, kind="gpu_kernel", name="kernel"),
        interval("d", 0, 10, source_id="s2"),
    ]
    result = M.function_statistics(query(*rows))
    assert len(result["groups"]) == 3
    solve = next(
        row
        for row in result["groups"]
        if row["source_ids"] == ["s1"] and row["name"] == "solve"
    )
    assert (
        solve["count"],
        solve["inclusive_ns"],
        solve["self_ns"],
        solve["min_ns"],
        solve["max_ns"],
    ) == (2, 150, 80, 50, 100)
    assert (
        next(row for row in result["groups"] if row["kind"] == "gpu_kernel")["self_ns"]
        is None
    )
    assert all(row["inclusive_ns"] >= 0 for row in result["groups"])
    with pytest.raises(ValueError):
        M.function_statistics(query(interval(self_ns=101)))


def test_smt_cpu_metrics_union_siblings_and_require_matching_capacity_coverage():
    rows = [
        interval(
            "a",
            0,
            100,
            kind="scheduling",
            logical_cpu=0,
            socket_id=0,
            core_id=0,
            data={"state": "running"},
        ),
        interval(
            "b",
            50,
            150,
            kind="scheduling",
            logical_cpu=1,
            socket_id=0,
            core_id=0,
            data={"state": "running"},
        ),
    ]
    capacity = {
        "clock_id": "clock-a",
        "start_ns": 0,
        "end_ns": 200,
        "logical_cpu_count": 2,
        "coverage_complete": True,
        "provenance": "captured allocation and scheduler stream",
    }
    result = M.resource_metrics(query(*rows), capacity=capacity)
    assert metric(result, "cpu_allocation_utilization")[0]["value"] == 0.5
    assert metric(result, "physical_core_busy_ns")[0]["value"] == 150
    assert (
        metric(M.resource_metrics(query(*rows)), "cpu_allocation_utilization")[0][
            "value"
        ]
        is None
    )
    assert (
        metric(
            M.resource_metrics(query(*rows, truncated=True), capacity=capacity),
            "cpu_allocation_utilization",
        )[0]["value"]
        is None
    )
    assert (
        metric(
            M.resource_metrics(query(interval(kind="cpu_observation"))),
            "cpu_allocation_utilization",
        )[0]["value"]
        is None
    )


def test_identical_logical_cpu_numbers_on_two_nodes_do_not_union_away_cpu_time():
    rows = [
        interval(
            "a",
            kind="scheduling",
            logical_cpu=0,
            data={"state": "running", "source_node": "a"},
        ),
        interval(
            "b",
            kind="scheduling",
            logical_cpu=0,
            data={"state": "running", "source_node": "b"},
        ),
    ]
    result = M.resource_metrics(query(*rows))
    assert len(metric(result, "logical_cpu_on_cpu_ns")) == 2
    assert sum(row["value"] for row in metric(result, "logical_cpu_on_cpu_ns")) == 200


def test_overlapping_gpu_activities_use_union_not_host_latency_or_occupancy():
    rows = [
        interval("a", 0, 100, kind="gpu_kernel", device={"uuid": "gpu", "id": "0"}),
        interval("b", 50, 150, kind="gpu_kernel", device={"uuid": "gpu", "id": "0"}),
        interval("host", 0, 10000, kind="host"),
    ]
    capacity = {
        "clock_id": "clock-a",
        "start_ns": 0,
        "end_ns": 200,
        "coverage_complete": True,
        "gpu_coverage_complete": True,
        "provenance": "complete device report",
    }
    result = M.resource_metrics(query(*rows), capacity=capacity)
    assert metric(result, "gpu_activity_busy_ns")[0]["value"] == 150
    assert metric(result, "gpu_activity_busy_fraction")[0]["value"] == 0.75
    assert metric(result, "gpu_occupancy")[0]["value"] is None
    assert metric(result, "parallel_efficiency")[0]["value"] is None
    assert (
        metric(
            M.resource_metrics(
                query(*rows, markers=[{"kind": "gap"}]), capacity=capacity
            ),
            "gpu_activity_busy_fraction",
        )[0]["value"]
        is None
    )


def test_unknown_device_scope_and_missing_capacity_provenance_cannot_claim_utilization():
    row = interval(kind="gpu_kernel")
    capacity = {
        "clock_id": "clock-a",
        "start_ns": 0,
        "end_ns": 200,
        "coverage_complete": True,
        "gpu_coverage_complete": True,
        "provenance": True,
    }
    assert (
        metric(
            M.resource_metrics(query(row), capacity=capacity),
            "gpu_activity_busy_fraction",
        )[0]["value"]
        is None
    )


def test_call_tree_preserves_recursion_and_cooperative_async_spans_are_roots():
    rows = [
        interval("outer", 0, 100, depth=0, first_seq=1, self_ns=40),
        interval("inner", 10, 70, depth=1, first_seq=2, self_ns=60),
        interval(
            "async", 5, 95, kind="cooperative", depth=0, first_seq=3, name="async"
        ),
    ]
    tree = M.call_tree(query(*rows))
    solve = [row for row in tree["nodes"] if row["name"] == "solve"]
    assert len(solve) == 2
    parent = next(row for row in solve if row["parent_id"] is None)
    child = next(row for row in solve if row["parent_id"] is not None)
    assert child["parent_id"] == parent["id"]
    assert parent["inclusive_ns"] == 100 and child["inclusive_ns"] == 60
    assert (
        next(row for row in tree["nodes"] if row["name"] == "async")["parent_id"]
        is None
    )
    assert M.call_tree(query(*rows), bottom_up=True)["direction"] == "bottom-up"
    assert M.call_tree(query(*rows), max_nodes=1)["partial"]
    assert all(type(row["inclusive_ns"]) is int for row in tree["nodes"])


def test_bounded_query_or_unknown_parent_does_not_claim_complete_tree():
    tree = M.call_tree(query(interval(depth=3), truncated=True))
    assert tree["partial"]
    assert tree["nodes"][0]["parent_id"] is None
    assert tree["nodes"][0]["self_ns"] is None
    json.dumps(tree, allow_nan=False)


def test_capacity_time_scope_mismatch_keeps_recorded_duration_but_refuses_percentage():
    row = interval(kind="scheduling", logical_cpu=0, data={"state": "running"})
    cap = {
        "clock_id": "clock-a",
        "start_ns": 0,
        "end_ns": 200,
        "logical_cpu_count": 1,
        "coverage_complete": True,
        "provenance": "recorded allocation",
    }
    value = query(row, range={"clock_id": "clock-a", "start_ns": 0, "end_ns": 100})
    result = M.resource_metrics(value, capacity=cap)
    assert metric(result, "logical_cpu_on_cpu_ns")[0]["value"] == 100
    assert metric(result, "cpu_allocation_utilization")[0]["value"] is None


def test_cpu_stacks_split_at_recorded_migrations_without_charging_elapsed_to_one_core():
    rows = [
        interval("call", 0, 100, self_ns=100),
        interval(
            "sched-a",
            0,
            40,
            kind="scheduling",
            logical_cpu=0,
            socket_id=0,
            core_id=0,
            data={"state": "running"},
        ),
        interval(
            "sched-b",
            60,
            100,
            kind="scheduling",
            logical_cpu=2,
            socket_id=0,
            core_id=1,
            data={"state": "running"},
        ),
    ]
    model = M.project(query(*rows), "cpu")
    slices = [row for row in model["intervals"] if row["kind"] == "cpu_stack_slice"]
    assert [(row["start_ns"], row["end_ns"], row["logical_cpu"]) for row in slices] == [
        (0, 40, 0),
        (60, 100, 2),
    ]
    assert sum(row["duration_ns"] for row in slices) == 80
    assert all(row["self_ns"] is None and row["elapsed_ns"] == 100 for row in slices)
    assert all(row["detail_id"] == "call" for row in slices)
    assert not M.cpu_execution_slices(
        query(interval(), markers=[interval(kind="cpu_observation")])
    )["intervals"]


def test_cpu_projection_never_matches_process_local_thread_ids_between_sources():
    call = interval()
    scheduling = interval(
        "other",
        kind="scheduling",
        source_id="s2",
        logical_cpu=0,
        data={"state": "running"},
    )
    assert not M.cpu_execution_slices(
        query(call, scheduling, sources=[source(), source("s2")])
    )["intervals"]


def test_sampled_stacks_are_points_and_keep_full_stack_only_in_original_detail():
    sample = interval(
        "sample",
        10,
        10,
        kind="cpu_sample",
        data={
            "frames": [{"name": "outer"}, {"name": "inner"}],
            "stack_order": "root-to-leaf",
            "logical_cpu": 0,
            "weight": 7,
            "weight_unit": "samples",
        },
    )
    for perspective in ("execution", "cpu"):
        result = M.project(query(markers=[sample]), perspective)
        assert [row["name"] for row in result["intervals"]] == ["outer", "inner"]
        assert all(
            row["point"] and row["start_ns"] == row["end_ns"] == 10
            for row in result["intervals"]
        )
        assert all(
            row["detail_id"] == "sample" and "frames" not in row["data"]
            for row in result["intervals"]
        )
        assert all(
            "per-call duration unknown" in row["duration_kind"]
            for row in result["intervals"]
        )
    assert not M.function_statistics(query(markers=[sample]))["groups"]


def test_arbitrary_complete_host_activities_get_overlap_lanes_without_inferred_stack():
    value = query(
        interval("outer", 0, 100, kind="host"), interval("inner", 10, 50, kind="host")
    )
    result = M.project(value)
    assert len(result["lanes"]) == 2
    assert all("independent activity lane" in lane["label"] for lane in result["lanes"])
    assert all(row["recorded_nesting"] is False for row in result["intervals"])
    explicit = query(
        interval(
            "outer",
            0,
            100,
            kind="host",
            data={"depth": 0, "boundary_fidelity": "matched-producer-boundaries"},
        ),
        interval(
            "inner",
            10,
            50,
            kind="host",
            data={"depth": 1, "boundary_fidelity": "matched-producer-boundaries"},
        ),
    )
    assert all(row["recorded_nesting"] for row in M.project(explicit)["intervals"])


def test_sample_statistics_count_recursion_once_and_never_invent_time():
    sample = interval(
        "sample",
        10,
        10,
        kind="cpu_sample",
        data={
            "frames": [{"name": "recursive"}, {"name": "recursive"}],
            "stack_order": "root-to-leaf",
            "weight": 7,
            "weight_unit": "samples",
        },
    )
    result = M.sample_statistics(query(markers=[sample]))
    group = result["groups"][0]
    assert result["observations"] == 1
    assert group["sample_hits"] == 1 and group["frame_occurrences"] == 2
    assert group["leaf_hits"] == 1 and group["weight"] == 7
    assert "duration_ns" not in group


def test_unknown_kernel_build_does_not_merge_process_device_context_lifetimes():
    base = interval(
        "a",
        kind="gpu_kernel",
        data={"source_pid": 42},
        device={"ordinal": 0},
        context="c",
    )
    other_device = dict(base, id="b", device={"ordinal": 1})
    other_context = dict(base, id="c", context="d")
    other_process = dict(
        base, id="d", data={"source_pid": 42, "process_generation": "new"}
    )
    assert (
        len(
            M.function_statistics(
                query(base, other_device, other_context, other_process)
            )["groups"]
        )
        == 4
    )
    # An explicit build ID is the producer's evidence that the symbol belongs
    # to one binary, so comparable labeled kernels may aggregate across devices.
    known = [
        dict(row, data=dict(row["data"], build_id="build-a"))
        for row in (base, other_device)
    ]
    assert len(M.function_statistics(query(*known))["groups"]) == 1


def test_call_tree_does_not_infer_nesting_across_independent_activation_domains():
    python = interval("python", 0, 100, depth=0, name="python-root")
    producer = interval(
        "producer",
        10,
        50,
        kind="host",
        name="producer-child",
        data={"depth": 1, "boundary_fidelity": "matched-producer-boundaries"},
    )
    rows = M.call_tree(query(python, producer))["nodes"]
    assert len(rows) == 2 and all(row["parent_id"] is None for row in rows)


def test_process_reuse_does_not_join_cpu_slices_or_share_execution_lanes():
    call = interval("call", data={"source_pid": 42, "process_generation": "old"})
    scheduling = interval(
        "schedule",
        kind="scheduling",
        logical_cpu=1,
        data={"state": "running", "source_pid": 42, "process_generation": "new"},
    )
    result = M.project(query(call, scheduling), "cpu")
    assert not any(row["kind"] == "cpu_stack_slice" for row in result["intervals"])
    second = dict(
        call, id="second", data={"source_pid": 42, "process_generation": "new"}
    )
    assert len(M.project(query(call, second))["lanes"]) == 2


def test_report_sources_never_borrow_importer_boot_for_resource_identity():
    sources = [source("s1"), source("s2")]
    for item in sources:
        item["clock"].update(kind="producer-report")
    rows = [
        interval(
            "a",
            source_id="s1",
            kind="scheduling",
            logical_cpu=0,
            socket_id=0,
            core_id=0,
            data={"state": "running", "source_node": "node-a", "source_pid": 42},
        ),
        interval(
            "b",
            source_id="s2",
            kind="scheduling",
            logical_cpu=0,
            socket_id=0,
            core_id=0,
            data={"state": "running", "source_node": "node-a", "source_pid": 42},
        ),
    ]
    result = M.project(query(*rows, sources=sources), "cpu")
    assert len(result["lanes"]) == 2
    assert all(lane["boot_id"] == "unknown" for lane in result["lanes"])
    assert len({lane["path_ids"][0] for lane in result["lanes"]}) == 2
    metrics = M.resource_metrics(query(*rows, sources=sources))
    assert len(metric(metrics, "physical_core_busy_ns")) == 2
    for row in rows:
        row["data"]["source_boot_id"] = "producer-boot"
    result = M.project(query(*rows, sources=sources), "cpu")
    assert len(result["lanes"]) == 1
    assert result["lanes"][0]["boot_id"] == "producer-boot"
    assert (
        metric(
            M.resource_metrics(query(*rows, sources=sources)), "physical_core_busy_ns"
        )[0]["value"]
        == 100
    )


def test_hierarchy_ids_do_not_collide_when_human_lifetime_labels_share_suffix():
    sources = [
        source("prefix-a-same1234", node="unknown"),
        source("prefix-b-same1234", node="unknown"),
    ]
    rows = [
        interval("a", source_id=sources[0]["id"]),
        interval("b", source_id=sources[1]["id"]),
    ]
    lanes = M.project(query(*rows, sources=sources))["lanes"]
    assert lanes[0]["path"][0] == lanes[1]["path"][0]
    assert lanes[0]["path_ids"][0] != lanes[1]["path_ids"][0]
    assert all(len(lane["path_ids"]) == len(lane["path"]) for lane in lanes)


def test_uuid_without_producer_boot_is_source_local_but_explicit_lifetime_can_merge():
    unknown = [
        M.device_identity({"uuid": "physical", "ordinal": 0}, source_id=sid, node="a")
        for sid in ("s1", "s2")
    ]
    assert unknown[0]["id"] != unknown[1]["id"]
    known = [
        M.device_identity(
            {"uuid": "physical", "ordinal": ordinal},
            source_id=sid,
            node="a",
            boot_id="boot-a",
        )
        for ordinal, sid in enumerate(("s1", "s2"))
    ]
    assert known[0]["id"] == known[1]["id"]
    reboot = M.device_identity(
        {"uuid": "physical", "ordinal": 0}, source_id="s1", node="a", boot_id="boot-b"
    )
    assert reboot["id"] != known[0]["id"]


def test_die_numa_and_heterogeneous_core_metadata_are_descriptive_and_identity_safe():
    rows = [
        interval(
            "a",
            kind="scheduling",
            logical_cpu=0,
            socket_id=0,
            die_id=0,
            core_id=0,
            numa_node=0,
            core_type="performance",
            data={"state": "running"},
        ),
        interval(
            "b",
            kind="scheduling",
            logical_cpu=1,
            socket_id=0,
            die_id=1,
            core_id=0,
            numa_node=1,
            core_type="efficiency",
            data={"state": "running"},
        ),
    ]
    view = M.project(query(*rows), "cpu")
    assert len(view["lanes"]) == 2
    assert {lane["die_id"] for lane in view["lanes"]} == {0, 1}
    assert {lane["numa_node"] for lane in view["lanes"]} == {0, 1}
    assert {lane["core_type"] for lane in view["lanes"]} == {
        "performance",
        "efficiency",
    }
    assert all(
        "Die " in lane["label"] and "NUMA " in lane["label"] for lane in view["lanes"]
    )
    assert len(metric(M.resource_metrics(query(*rows)), "physical_core_busy_ns")) == 2
    legacy = M.project(query(interval(kind="cpu_observation")), "cpu")["lanes"][0]
    assert (
        legacy["die_id"] is None
        and legacy["numa_node"] is None
        and legacy["core_type"] is None
    )


def test_logical_tasks_do_not_create_false_cross_task_nesting_or_cpu_slices():
    first = interval(
        "a",
        0,
        100,
        name="outer",
        data={"logical_task_id": "task-a", "task_generation": "1"},
    )
    second = interval(
        "b",
        10,
        50,
        depth=1,
        name="inner",
        data={"logical_task_id": "task-b", "task_generation": "1"},
    )
    result = M.project(query(first, second))
    assert {lane["logical_task_id"] for lane in result["lanes"]} == {"task-a", "task-b"}
    assert all("Logical task" in lane["label"] for lane in result["lanes"])
    assert all(
        row["parent_id"] is None for row in M.call_tree(query(first, second))["nodes"]
    )
    scheduler = interval(
        "scheduler", kind="scheduling", logical_cpu=0, data={"state": "running"}
    )
    result = M.project(query(first, scheduler), "cpu")
    assert not any(row["kind"] == "cpu_stack_slice" for row in result["intervals"])
    scheduler["data"].update(logical_task_id="task-a", task_generation="1")
    result = M.project(query(first, scheduler), "cpu")
    assert any(row["kind"] == "cpu_stack_slice" for row in result["intervals"])


@pytest.mark.parametrize(
    "unhealthy",
    [
        {"status": "INVALID"},
        {"status": "PARTIAL"},
        {"status": "ERROR"},
        {"status": "LIMIT"},
        {"caught_up": False},
        {"validated": False},
        {"coverage": {"complete": False}},
    ],
)
def test_incomplete_source_cannot_claim_complete_utilization(unhealthy):
    cap = {
        "clock_id": "clock-a",
        "start_ns": 0,
        "end_ns": 200,
        "logical_cpu_count": 1,
        "coverage_complete": True,
        "gpu_coverage_complete": True,
        "provenance": "declared capture coverage",
    }
    src = source()
    src.update(unhealthy)
    rows = [
        interval("cpu", kind="scheduling", logical_cpu=0, data={"state": "running"}),
        interval("gpu", kind="gpu_kernel", device={"uuid": "gpu", "ordinal": 0}),
    ]
    result = M.resource_metrics(query(*rows, sources=[src]), capacity=cap)
    assert metric(result, "logical_cpu_on_cpu_ns")[0]["value"] == 100
    assert metric(result, "gpu_activity_busy_ns")[0]["value"] == 100
    assert metric(result, "cpu_allocation_utilization")[0]["value"] is None
    assert metric(result, "gpu_activity_busy_fraction")[0]["value"] is None


def test_not_yet_indexed_data_and_independent_cpu_gpu_coverage_are_not_zeroes():
    cap = {
        "clock_id": "clock-a",
        "start_ns": 0,
        "end_ns": 200,
        "allocated_logical_cpu_ns": 400,
        "coverage_complete": True,
        "cpu_coverage_complete": True,
        "gpu_coverage_complete": False,
        "provenance": "matching allocation epochs",
    }
    rows = [
        interval("cpu", kind="scheduling", logical_cpu=0, data={"state": "running"}),
        interval("gpu", kind="gpu_kernel", device={"uuid": "gpu", "ordinal": 0}),
    ]
    pending = M.resource_metrics(query(*rows, caught_up=False), capacity=cap)
    assert metric(pending, "cpu_allocation_utilization")[0]["value"] is None
    good = M.resource_metrics(query(*rows, caught_up=True), capacity=cap)
    cpu = metric(good, "cpu_allocation_utilization")[0]
    assert cpu["value"] == 0.25 and cpu["denominator"] == 400
    assert metric(good, "gpu_activity_busy_fraction")[0]["value"] is None
    cap.update(cpu_coverage_complete=False, gpu_coverage_complete=True)
    good = M.resource_metrics(query(*rows, caught_up=True), capacity=cap)
    assert metric(good, "cpu_allocation_utilization")[0]["value"] is None
    assert metric(good, "gpu_activity_busy_fraction")[0]["value"] == 0.5


def test_explicit_off_cpu_states_are_thread_durations_and_not_core_idle_time():
    rows = [
        interval("a", 0, 100, kind="scheduling", data={"state": "blocked"}),
        interval("b", 50, 150, kind="scheduling", data={"state": "waiting"}),
        interval("c", 150, 200, kind="scheduling", data={"state": "runnable"}),
        interval(
            "d", 210, 220, kind="scheduling", logical_cpu=0, data={"state": "running"}
        ),
    ]
    result = M.resource_metrics(query(*rows))
    off = metric(result, "observed_off_cpu_ns")[0]
    assert off["value"] == 200 and off["thread"] == "t1"
    assert "not physical-core idle" in off["provenance"]
    assert metric(result, "observed_blocked_ns")[0]["value"] == 100
    assert metric(result, "observed_runnable_ns")[0]["value"] == 50
    assert metric(result, "observed_on_cpu_ns")[0]["value"] == 10
    assert len(metric(result, "physical_core_busy_ns")) == 0


@pytest.mark.parametrize("bad", [True, {}, [], "", "bad\nidentity", "x" * 257])
def test_logical_task_identity_semantics_are_bounded_and_fail_closed(bad):
    row = interval(data={"logical_task_id": bad})
    with pytest.raises(ValueError, match="logical_task_id"):
        M.project(query(row))
    with pytest.raises(ValueError, match="logical_task_id"):
        M.call_tree(query(row))


def test_logical_task_aliases_and_same_labels_preserve_stable_distinct_identity():
    with pytest.raises(ValueError, match="aliases disagree"):
        M.project(query(interval(data={"logical_task_id": "a", "task_id": "b"})))
    rows = [
        interval("integer", data={"logical_task_id": 7}),
        interval("text", data={"logical_task_id": "7"}),
    ]
    lanes = M.project(query(*rows))["lanes"]
    assert len(lanes) == 2 and lanes[0]["path"] == lanes[1]["path"]
    assert lanes[0]["path_ids"] != lanes[1]["path_ids"]


def test_collective_observations_remain_selectable_points_in_execution_and_node_views():
    marker = interval(
        "mpi-entry",
        start=100,
        end=100,
        kind="collective",
        data={"operation": "MPI_Barrier", "phase": "enter", "rank": 0},
    )
    for perspective in ("execution", "node"):
        result = M.project(query(markers=[marker]), perspective)
        assert len(result["intervals"]) == 1
        row = result["intervals"][0]
        assert row["id"] == "mpi-entry" and row["point"]
        assert row["duration_ns"] == 0 and row["self_ns"] is None
        assert row["name"] == "MPI_Barrier · enter"
        assert "no per-call duration" in row["duration_kind"]
    assert not M.project(query(markers=[marker]), "cpu")["intervals"]
