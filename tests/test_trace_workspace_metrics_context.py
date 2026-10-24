"""Owner-scoped denominators, independent readiness and observed sample shares."""

from __future__ import annotations

from copy import deepcopy

import pytest

from raven.tracing import workspace_metrics_context as C


def epoch(start=0, end=100, count=2, **values):
    result = dict(
        clock_id="clock-a",
        start_ns=start,
        end_ns=end,
        logical_cpu_count=count,
        unit="logical-cpu",
        coverage_complete=True,
        gpu_coverage_complete=True,
        source_ids=["s1"],
        provenance="Owned measured fixture allocation and capture scope",
    )
    result.update(values)
    return result


def context(epochs=None):
    return dict(
        schema=C.SCHEMA,
        identity=dict(cluster="local", job_id="42", attempt="attempt-one"),
        provenance="Explicit fixture evidence; not live scheduler qualification",
        epochs=epochs or [epoch()],
    )


def fixture():
    from test_trace_workspace_model import interval, query, source

    scheduling = interval(
        "schedule",
        kind="scheduling",
        data=dict(state="running", logical_cpu=0, socket_id=0, core_id=0),
    )
    gpu = interval(
        "gpu",
        start=0,
        end=60,
        kind="gpu_kernel",
        device=dict(uuid="gpu-physical-id", vendor="test-vendor", id=0),
    )
    samples = [
        interval(
            "sample-a",
            kind="cpu_sample",
            start=10,
            end=10,
            data=dict(
                frames=[dict(name="solve"), dict(name="solve")],
                weight=30,
                weight_unit="event-count",
                event="cycles",
                stack_order="root-to-leaf",
            ),
        ),
        interval(
            "sample-unknown",
            kind="cpu_sample",
            start=30,
            end=30,
            data=dict(
                frames=[],
                weight=70,
                weight_unit="event-count",
                event="cycles",
                stack_order="root-to-leaf",
            ),
        ),
    ]
    value = query(
        scheduling,
        gpu,
        markers=samples,
        sources=[
            dict(
                source(), status="COMPLETE", validated=True, coverage={"complete": True}
            )
        ],
        identity=context()["identity"],
        bounds=[dict(clock_id="clock-a", start_ns=0, end_ns=100)],
        range=dict(clock_id="clock-a", start_ns=0, end_ns=100),
        caught_up=True,
    )
    return value


def metric(value, name):
    return next(row for row in value["metrics"] if row["name"] == name)


def test_effective_allocation_epochs_integrate_denominator_and_keep_sample_units():
    value = fixture()
    owner = context([epoch(0, 50, 2), epoch(50, 100, 4)])
    before = deepcopy(owner)
    result = C.metrics(value, context=owner)
    utilization = metric(result, "cpu_allocation_utilization")
    assert utilization["numerator"] == 100 and utilization["denominator"] == 300
    assert utilization["value"] == pytest.approx(1 / 3)
    assert metric(result, "gpu_activity_busy_fraction")["value"] == pytest.approx(0.6)
    group = result["sample_statistics"]["groups"][0]
    assert group["sample_hits"] == 1 and group["frame_occurrences"] == 2
    assert (
        group["observed_weight_denominator"] == 100
        and group["unknown_stack_weight"] == 70
    )
    assert group["inclusive_share"] == pytest.approx(0.3)
    assert group["leaf_share"] == pytest.approx(0.3)
    assert group["weight_unit"] == "event-count"
    assert owner == before


def test_independent_gpu_cpu_readiness_and_unknown_context_preserve_available_data():
    value = fixture()
    gpu_only = C.metrics(value, context=context([epoch(coverage_complete=False)]))
    assert metric(gpu_only, "cpu_allocation_utilization")["value"] is None
    assert metric(gpu_only, "gpu_activity_busy_fraction")["value"] == pytest.approx(0.6)
    cpu_only = C.metrics(value, context=context([epoch(gpu_coverage_complete=False)]))
    assert metric(cpu_only, "cpu_allocation_utilization")["value"] == pytest.approx(0.5)
    assert metric(cpu_only, "gpu_activity_busy_fraction")["value"] is None
    missing = C.metrics(value)
    assert metric(missing, "cpu_allocation_utilization")["value"] is None
    assert metric(missing, "gpu_activity_busy_ns")["value"] == 60
    assert missing["sample_statistics"]["observations"] == 2


def test_identity_source_scope_and_epoch_gaps_cannot_supply_false_denominators():
    value = fixture()
    owner = context()
    owner["identity"]["attempt"] = "different attempt"
    result = C.metrics(value, context=owner)
    assert metric(result, "cpu_allocation_utilization")["value"] is None
    assert "identity differs" in result["context_status"]["reason"]
    for owner in (
        context([epoch(source_ids=["unrelated source"])]),
        context([epoch(0, 40), epoch(60, 100)]),
    ):
        result = C.metrics(value, context=owner)
        assert metric(result, "cpu_allocation_utilization")["value"] is None
        assert metric(result, "gpu_activity_busy_fraction")["value"] is None
        assert metric(result, "gpu_activity_busy_ns")["value"] == 60


def test_filtered_subrange_clips_both_numerator_and_effective_capacity():
    value = fixture()
    value["range"].update(start_ns=25, end_ns=75)
    result = C.metrics(
        value, context=context([epoch(0, 50, 2), epoch(50, 100, 4)]), source_ids=["s1"]
    )
    metric_ = metric(result, "cpu_allocation_utilization")
    assert metric_["numerator"] == 50 and metric_["denominator"] == 150
    assert metric_["value"] == pytest.approx(1 / 3)
    assert metric(result, "gpu_activity_busy_ns")["value"] == 35


@pytest.mark.parametrize(
    "change",
    [
        lambda value: value.update(command="must not execute"),
        lambda value: value["identity"].update(account="not ownership proof"),
        lambda value: value["epochs"][0].update(unit="physical-core"),
        lambda value: value["epochs"][0].update(logical_cpu_count=True),
        lambda value: value["epochs"][0].update(coverage_complete="yes"),
        lambda value: value["epochs"][0].update(source_ids=[]),
        lambda value: value["epochs"].append(epoch(50, 100)),
        lambda value: value["epochs"][0].update(start_ns=100),
    ],
)
def test_malformed_or_semantic_extension_context_is_rejected(change):
    owner = context()
    change(owner)
    with pytest.raises(ValueError):
        C.validate_context(owner)


def test_sample_scope_never_mixes_events_units_or_producer_processes():
    value = fixture()
    extra = deepcopy(value["markers"][0])
    extra["id"] = "another-process"
    extra["data"].update(source_pid=99, weight=500)
    value["markers"].append(extra)
    extra = deepcopy(extra)
    extra["id"] = "another-unit"
    extra["data"].update(weight=10, weight_unit="ns", event="cpu-clock")
    value["markers"].append(extra)
    groups = C.metrics(value)["sample_statistics"]["groups"]
    assert {
        (row["source_pid"], row["weight_unit"], row["observed_weight_denominator"])
        for row in groups
    } == {(None, "event-count", 100), (99, "event-count", 500), (99, "ns", 10)}


def test_execution_filters_cannot_reuse_a_whole_scope_denominator():
    value = fixture()
    value["filters"] = {"logical_cpu": 0}
    result = C.metrics(value, context=context())
    assert metric(result, "cpu_allocation_utilization")["value"] is None
    assert metric(result, "gpu_activity_busy_fraction")["value"] is None
    assert (
        "facet-scoped denominator"
        in metric(result, "cpu_allocation_utilization")["reason"]
    )
    assert metric(result, "gpu_activity_busy_ns")["value"] == 60
    assert result["sample_statistics"]["groups"][0]["inclusive_share"] == pytest.approx(
        0.3
    )
