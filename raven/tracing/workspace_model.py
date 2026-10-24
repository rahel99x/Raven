"""Pure, evidence-scoped clock and topology projections for trace queries."""

from __future__ import annotations


def source_clock(identity, metadata=None):
    from .workspace_model_clocks import source_clock as implementation

    return implementation(identity, metadata)


def validate_calibration(value):
    from .workspace_model_clocks import validate_calibration as implementation

    return implementation(value)


def transform_time(value, calibration):
    from .workspace_model_clocks import transform_time as implementation

    return implementation(value, calibration)


def align_interval(interval, clock):
    from .workspace_model_clocks import align_interval as implementation

    return implementation(interval, clock)


def device_identity(
    device, *, source_id, node="unknown", boot_id="unknown", cluster="unknown"
):
    from .workspace_model_topology import device_identity as implementation

    return implementation(
        device, source_id=source_id, node=node, boot_id=boot_id, cluster=cluster
    )


def project(query, perspective="execution", *, projection=None, max_lanes=4096):
    from .workspace_model_topology import project as implementation

    return implementation(
        query, perspective, projection=projection, max_lanes=max_lanes
    )


def union_duration(intervals, *, start_ns=None, end_ns=None):
    from .workspace_model_metrics import union_duration as implementation

    return implementation(intervals, start_ns=start_ns, end_ns=end_ns)


def function_statistics(query, *, limit=100):
    from .workspace_model_metrics import function_statistics as implementation

    return implementation(query, limit=limit)


def resource_metrics(query, *, capacity=None):
    from .workspace_model_metrics import resource_metrics as implementation

    return implementation(query, capacity=capacity)


def call_tree(query, *, bottom_up=False, max_nodes=4096, max_depth=128):
    from .workspace_model_metrics import call_tree as implementation

    return implementation(
        query, bottom_up=bottom_up, max_nodes=max_nodes, max_depth=max_depth
    )


def explicit_flows(query, *, limit=2000):
    from .workspace_model_flows import explicit_flows as implementation

    return implementation(query, limit=limit)


def cpu_execution_slices(query, *, limit=5000):
    from .workspace_model_topology import _items, _sources
    from .workspace_model_cpu import execution_slices

    intervals, _ = _items(query)
    return execution_slices(intervals, _sources(query), limit=limit)


def sample_statistics(query, *, limit=100):
    from .workspace_model_metrics import sample_statistics as implementation

    return implementation(query, limit=limit)
