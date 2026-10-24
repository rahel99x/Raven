"""Evidence-scoped investigation over bounded workspace queries.

These functions analyse supplied observations. They do not collect, align
unknown clocks, authorize processes, or establish the unobserved critical path.
"""

from __future__ import annotations


def causal_slice(query, selected_id, *, max_nodes=4096):
    from .workspace_analysis_causal import causal_slice as implementation

    return implementation(query, selected_id, max_nodes=max_nodes)


def compare(query, baseline, *, context=None):
    from .workspace_analysis_compare import compare as implementation

    return implementation(query, baseline, context=context)


def counter_lens(query, *, max_series=128, max_points=4096):
    from .workspace_analysis_lens import counter_lens as implementation

    return implementation(query, max_series=max_series, max_points=max_points)


def bottleneck_hints(query, *, limit=20):
    from .workspace_analysis_lens import bottleneck_hints as implementation

    return implementation(query, limit=limit)
