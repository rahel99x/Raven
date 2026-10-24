"""Investigations preserve identity, timing uncertainty and unavailable evidence."""

from copy import deepcopy

import pytest

from raven.tracing import workspace_analysis as A


def source(identifier="s", clock="c", **extra):
    return dict(
        id=identifier,
        identity={
            "cluster": "site",
            "node": "node",
            "pid": 1,
            "boot_id": "boot",
            "process_start": "start",
            "stream": "stream",
        },
        clock={"id": clock, **extra},
    )


def event(
    identifier="a",
    *,
    start=10,
    end=20,
    kind="python",
    source_id="s",
    clock="c",
    data=None,
    **extra,
):
    row = dict(
        id=identifier,
        name="solve",
        kind=kind,
        source_id=source_id,
        clock_id=clock,
        start_ns=start,
        end_ns=end,
        duration_ns=end - start,
        thread="t",
        depth=0,
        file="solve.py",
        line=4,
        first_seq=1,
        last_seq=2,
        data=data or {},
    )
    row.update(extra)
    return row


def query(*rows, markers=(), sources=None, **extra):
    return dict(
        identity={"cluster": "site", "job_id": "1", "attempt": "a"},
        sources=[source()] if sources is None else sources,
        intervals=list(rows),
        markers=list(markers),
        **extra,
    )


def descriptor(*, elapsed=100, resources=1, unit="logical-cpu", model="same-cpu"):
    return dict(
        workload="work",
        input="sha256:input",
        build="build",
        scope="whole-run",
        elapsed_ns=elapsed,
        elapsed_provenance="measured process makespan",
        resources={"count": resources, "unit": unit, "model": model},
    )


def context(**extra):
    return dict(
        schema="raven.trace-comparison-context/v1",
        provenance="explicit benchmark declaration",
        workload_comparable=True,
        inputs_comparable=True,
        builds_comparable=True,
        current=descriptor(elapsed=25, resources=4),
        baseline=descriptor(),
        **extra,
    )


def calibration(**extra):
    return dict(
        schema="raven.trace-clock-calibration/v1",
        reference_id="reference",
        source_anchor_ns=0,
        reference_anchor_ns=1000,
        rate_num=1,
        rate_den=1,
        uncertainty_ns=5,
        valid_start_ns=0,
        valid_end_ns=1000,
        **extra,
    )


def test_no_dependency_is_inferred_from_time_or_bare_correlation():
    result = A.causal_slice(
        query(
            event(), event("b", start=30, end=40, data={"correlation_id": "arbitrary"})
        ),
        "a",
    )
    assert result["edges"] == []
    assert result["status"] == "partial"
    assert result["observed_chain"]["node_ids"] == ["a"]


def test_explicit_dependency_chain_and_bounded_evidence_navigation():
    value = query(
        event(),
        event("b", start=30, end=50, data={"depends_on": ["a"]}),
        event("c", start=70, end=80, data={"depends_on": ["b"]}),
    )
    result = A.causal_slice(value, "b")
    assert result["status"] == "observed"
    assert result["ancestors"] == ["a"] and result["descendants"] == ["c"]
    assert result["observed_chain"]["node_ids"] == ["a", "b", "c"]
    assert result["observed_chain"]["elapsed_bounds_ns"] == {"lower": 70, "upper": 70}
    assert result["edges"][0]["timing"]["bounds_ns"] == {"lower": 10, "upper": 10}
    assert "not a sum" in result["observed_chain"]["timing_scope"]
    limited = A.causal_slice(value, "b", max_nodes=1)
    assert limited["partial"] and len(limited["nodes"]) == 1
    assert any(item["kind"] == "node-budget" for item in limited["unknown"])


def test_cross_clock_causality_keeps_elapsed_unavailable_until_calibrated():
    first = event(start=10, end=20)
    last = event(
        "b", start=30, end=40, clock="d", source_id="s2", data={"depends_on": ["a"]}
    )
    value = query(first, last, sources=[source(), source("s2", "d")])
    result = A.causal_slice(value, "b")
    assert result["edges"][0]["timing"]["availability"] == "unavailable"
    assert result["observed_chain"]["elapsed_bounds_ns"] is None
    value["sources"] = [
        source(calibration=calibration()),
        source("s2", "d", calibration=calibration()),
    ]
    aligned = A.causal_slice(value, "b")
    assert aligned["edges"][0]["timing"]["bounds_ns"] == {"lower": 0, "upper": 20}
    assert aligned["observed_chain"]["elapsed_bounds_ns"] == {"lower": 20, "upper": 40}
    assert aligned["observed_chain"]["clock_id"] == "reference"


def test_invalid_calibration_range_never_fabricates_cross_clock_delay():
    value = query(
        event(),
        event(
            "b",
            source_id="s2",
            clock="d",
            start=3000,
            end=3010,
            data={"depends_on": ["a"]},
        ),
        sources=[
            source(calibration=calibration()),
            source("s2", "d", calibration=calibration()),
        ],
    )
    result = A.causal_slice(value, "b")
    assert result["observed_chain"]["elapsed_bounds_ns"] is None


def test_already_projected_reference_events_preserve_their_uncertainty():
    first = event(clock="reference", alignment_uncertainty_ns=5, raw_clock_id="c")
    last = event(
        "b",
        start=30,
        end=40,
        clock="reference",
        source_id="s2",
        alignment_uncertainty_ns=7,
        raw_clock_id="d",
        data={"depends_on": ["a"]},
    )
    value = query(
        first,
        last,
        sources=[
            source(calibration=calibration()),
            source("s2", "d", calibration=calibration()),
        ],
    )
    result = A.causal_slice(value, "b")
    assert result["edges"][0]["timing"]["bounds_ns"] == {"lower": 0, "upper": 22}
    assert result["observed_chain"]["elapsed_bounds_ns"] == {"lower": 18, "upper": 42}


def test_cycles_and_contradictory_dependencies_are_diagnosed():
    result = A.causal_slice(
        query(
            event(data={"depends_on": ["b"]}),
            event("b", start=30, end=40, data={"depends_on": ["a"]}),
        ),
        "a",
    )
    assert result["status"] == "inconsistent"
    assert result["cycles"] == [["a", "b"]]
    assert result["observed_chain"]["elapsed_bounds_ns"] is None


def test_missing_malformed_dependency_and_absent_selection_remain_visible():
    value = query(event(data={"depends_on": ["missing"]}))
    assert A.causal_slice(value, "a")["unknown"][0]["kind"] == "missing-endpoint"
    value["intervals"][0]["data"]["depends_on"] = "missing"
    assert A.causal_slice(value, "a")["unknown"][0]["kind"] == "malformed-dependency"
    assert A.causal_slice(value, "unreturned")["status"] == "unavailable"
    with pytest.raises(ValueError, match="Duplicate"):
        A.causal_slice(query(event(), event()), "a")


def test_activity_correlation_requires_typed_scoped_consistent_evidence():
    host = event(
        kind="host", data={"correlation_id": "1", "correlation_scope": "report"}
    )
    gpu = event(
        "gpu",
        start=30,
        end=40,
        kind="gpu_kernel",
        data={"correlation_id": "1", "correlation_scope": "report"},
    )
    value = query(host, gpu)
    value["activity_links"] = [
        dict(
            schema="raven.trace-activity-correlation/v1",
            source_id="a",
            target_id="gpu",
            correlation_id="1",
            correlation_scope="report",
            provenance="validated exporter declaration",
        )
    ]
    result = A.causal_slice(value, "a")
    assert result["edges"][0]["relation"] == "launches"
    value["activity_links"][0]["correlation_scope"] = "different-report"
    assert A.causal_slice(value, "a")["edges"] == []


def test_comparison_is_unavailable_without_comparability_context():
    result = A.compare(query(event()), query(event()))
    assert result["status"] == "unavailable"
    assert all(metric["value"] is None for metric in result["metrics"])


def test_declared_serial_baseline_produces_exact_efficiency_and_duration_delta():
    result = A.compare(query(event(end=15)), query(event()), context=context())
    assert result["status"] == "comparable"
    metrics = {row["name"]: row for row in result["metrics"]}
    assert metrics["reference_speedup"]["value"] == 4
    assert metrics["parallel_efficiency"]["value"] == 1
    assert result["functions"][0]["delta_inclusive_ns"] == -5
    assert result["functions"][0]["ratio_exact"] == {"numerator": 1, "denominator": 2}
    assert "not proof" in metrics["reference_speedup"]["meaning"]


@pytest.mark.parametrize(
    "change,reason",
    [
        ({"inputs_comparable": False}, "inputs_comparable"),
        ({"current": descriptor(elapsed=0)}, "reference elapsed"),
        (
            {"current": dict(descriptor(), input="different")},
            "input identifiers differ",
        ),
        ({"current": dict(descriptor(), build="different")}, "build declaration"),
    ],
)
def test_comparison_rejects_missing_or_inconsistent_semantics(change, reason):
    ctx = context()
    ctx.update(change)
    result = A.compare(
        query(event(data={"build_id": "build"})),
        query(event(data={"build_id": "build"})),
        context=ctx,
    )
    assert result["status"] == "unavailable"
    assert reason in "; ".join(result["comparability"]["reasons"])


def test_unknown_cross_source_symbols_require_explicit_pairing_not_same_name():
    current, baseline = (
        query(event()),
        query(event(source_id="other"), sources=[source("other")]),
    )
    assert A.compare(current, baseline, context=context())["functions"] == []
    ctx = context(source_pairs=[{"current": "s", "baseline": "other"}])
    assert len(A.compare(current, baseline, context=ctx)["functions"]) == 1
    ctx["source_pairs"].append({"current": "s", "baseline": "other"})
    with pytest.raises(ValueError, match="one-to-one"):
        A.compare(current, baseline, context=ctx)


def test_efficiency_requires_matching_resource_units_and_single_resource_baseline():
    for baseline in (
        descriptor(resources=2),
        descriptor(unit="gpu"),
        descriptor(model="another-cpu"),
    ):
        ctx = context()
        ctx["baseline"] = baseline
        result = A.compare(query(event()), query(event()), context=ctx)
        assert result["metrics"][0]["value"] is not None
        assert result["metrics"][1]["value"] is None


def test_comparison_does_not_clamp_superlinear_or_sum_concurrent_time_as_makespan():
    ctx = context()
    ctx["current"]["elapsed_ns"] = 10
    result = A.compare(
        query(event(), event("b", end=100, thread="other")), query(event()), context=ctx
    )
    assert result["metrics"][1]["value"] == 2.5
    assert result["metrics"][0]["value"] == 10


def test_counter_points_keep_exact_values_process_units_clock_and_loss():
    count = event(
        "counter",
        start=1,
        end=1,
        kind="counter",
        data={
            "values": {"rss": "9007199254740993", "invalid": "NaN"},
            "units": {"rss": "bytes"},
            "source_pid": 1,
        },
    )
    other = event(
        "counter2",
        start=2,
        end=2,
        kind="counter",
        data={"values": {"rss": "7"}, "units": {"rss": "bytes"}, "source_pid": 2},
    )
    phase = event("phase", start=3, end=3, kind="activity_marker", name="compute")
    gap = event("gap", start=4, end=4, kind="gap", data={"reason": "quota reached"})
    value = query(markers=[count, other, phase, gap])
    result = A.counter_lens(value)
    assert len(result["series"]) == 2
    assert result["series"][0]["points"][0]["value"] == "9007199254740993"
    assert result["series"][0]["unit"] == "bytes"
    assert result["phases"][0]["name"] == "compute"
    assert result["gaps"][0]["reason"] == "quota reached"
    assert result["partial"] and result["omitted"]["invalid_values"] == 1


def test_counter_budgets_report_omission_and_do_not_interpolate():
    value = query(
        markers=[
            event(
                str(i),
                start=i,
                end=i,
                kind="counter",
                data={"values": {"temperature": i, "voltage": i / 2}},
            )
            for i in range(5)
        ]
    )
    result = A.counter_lens(value, max_series=1, max_points=2)
    assert len(result["series"]) == 1
    assert len(result["series"][0]["points"]) == 2
    assert result["omitted"]["points"] == 3 and result["omitted"]["series"] == 5
    assert result["partial"]
    assert "never interpolated" in result["limitations"][0]


def test_hint_evidence_keeps_cpu_idle_and_gpu_occupancy_unproved():
    value = query(
        event(kind="host", name="cudaLaunchKernel"),
        markers=[event("loss", start=2, end=2, kind="gap")],
    )
    result = A.bottleneck_hints(value)
    assert {hint["title"] for hint in result["hints"]} == {
        "Recording coverage is incomplete",
        "Inspect the longest returned recorded interval",
        "Device activity is unavailable in this query",
    }
    for hint in result["hints"]:
        assert hint["evidence_ids"] and hint["proposed_checks"]
        assert "unproved" in hint["confidence"]
    assert "does not prove the GPU was idle" in result["hints"][-1]["assumptions"][-1]
    before = deepcopy(value)
    A.bottleneck_hints(value)
    A.counter_lens(value)
    assert value == before


@pytest.mark.parametrize(
    "method,kwargs",
    [
        (A.counter_lens, {"max_points": 0}),
        (A.counter_lens, {"max_series": 10000}),
        (A.bottleneck_hints, {"limit": True}),
        (A.causal_slice, {"max_nodes": 0}),
    ],
)
def test_analysis_limits_are_enforced(method, kwargs):
    with pytest.raises(ValueError):
        method(query(), **kwargs) if method is not A.causal_slice else method(
            query(), "a", **kwargs
        )
