"""Only explicitly scoped flow records establish causal edges."""

from __future__ import annotations

import copy
from fractions import Fraction

import pytest
from hypothesis import given
from hypothesis import strategies as st

from raven.tracing import workspace_model as M


def point(
    identifier,
    phase,
    time,
    *,
    flow="f",
    scope="report:one",
    clock="clock-a",
    source="s1",
    seq=None,
):
    return dict(
        id=identifier,
        kind="flow",
        source_id=source,
        clock_id=clock,
        thread="t1",
        start_ns=time,
        end_ns=time,
        seq=seq,
        data=dict(
            flow_id=flow, flow_phase=phase, correlation_scope=scope, source_time_ns=time
        ),
    )


def query(*points):
    return dict(intervals=[], markers=list(points), sources=[])


def test_explicit_flow_chain_has_stable_ids_and_preserves_input():
    value = query(
        point("start", "start", 10, seq=1),
        point("step", "step", 20, seq=2),
        point("end", "end", 30, seq=3),
    )
    original = copy.deepcopy(value)
    result = M.explicit_flows(value)
    assert [
        (row["source_event_id"], row["target_event_id"], row["duration_ns"])
        for row in result["links"]
    ] == [("start", "step", 10), ("step", "end", 10)]
    assert result == M.explicit_flows(value)
    assert value == original


def test_unaligned_cross_node_flow_has_causality_without_invented_duration():
    result = M.explicit_flows(
        query(
            point("start", "start", 1_000_000, source="node-a", clock="clock-a"),
            point("end", "end", 5, source="node-b", clock="clock-b"),
        )
    )
    assert len(result["links"]) == 1
    assert result["links"][0]["duration_ns"] is None
    assert result["links"][0]["timing"] == "unaligned clocks; causal link only"
    assert result["links"][0]["start_ns"] == 1_000_000
    assert result["links"][0]["end_ns"] == 5


def test_local_id_scopes_are_not_merged_between_reported_processes():
    result = M.explicit_flows(
        query(
            point("a", "start", 0, scope="report:pid1"),
            point("b", "end", 5, scope="report:pid2"),
        )
    )
    assert not result["links"]
    assert result["unresolved"] == 2


def test_bare_activity_correlation_ids_do_not_create_edges():
    value = query()
    value["intervals"] = [
        dict(id="host", kind="host", data={"correlation_id": "1"}),
        dict(id="kernel", kind="gpu_kernel", data={"correlation_id": "1"}),
    ]
    assert not M.explicit_flows(value)["links"]


def test_overlapping_or_missing_lifetimes_remain_unresolved():
    result = M.explicit_flows(
        query(
            point("a", "start", 0, seq=1),
            point("b", "start", 1, seq=2),
            point("c", "end", 2, seq=3),
        )
    )
    assert not result["links"]
    assert result["unresolved"] == 3
    assert "ambiguous" in result["warnings"][0]
    assert not M.explicit_flows(query(point("a", "start", 0)))["links"]


def test_sequential_reuse_is_valid_with_source_sequence():
    result = M.explicit_flows(
        query(
            point("a", "start", 0, seq=1),
            point("b", "end", 1, seq=2),
            point("c", "start", 2, seq=3),
            point("d", "end", 3, seq=4),
        )
    )
    assert len(result["links"]) == 2
    assert len({row["id"] for row in result["links"]}) == 2


def test_malformed_semantics_and_duplicate_event_ids_are_visible():
    bad = point("bad", "start", 0)
    del bad["data"]["correlation_scope"]
    result = M.explicit_flows(
        query(bad, point("a", "start", 0), point("a", "start", 0), point("b", "end", 5))
    )
    assert result["rejected"] == 2
    assert len(result["links"]) == 1
    assert "Rejected 2" in result["warnings"][0]


def test_flow_limit_is_explicit_and_negative_duration_is_not_clamped():
    result = M.explicit_flows(
        query(point("a", "start", 0), point("b", "step", 1), point("c", "end", 2)),
        limit=1,
    )
    assert len(result["links"]) == 1 and result["omitted"] == 1
    result = M.explicit_flows(query(point("a", "start", 5), point("b", "end", 1)))
    assert not result["links"] and result["rejected"] == 1
    with pytest.raises(ValueError):
        M.explicit_flows(query(), limit=True)


def test_cross_domain_steps_are_not_sorted_by_unrelated_raw_clocks():
    result = M.explicit_flows(
        query(
            point("a", "start", 1000, clock="a"),
            point("b", "step", 1, clock="b"),
            point("c", "end", 500, clock="c"),
        )
    )
    assert len(result["links"]) == 1
    assert result["links"][0]["source_event_id"] == "a"
    assert result["links"][0]["target_event_id"] == "c"
    assert "intermediate" in result["warnings"][0]


def test_calibrated_flow_carries_clock_error_and_does_not_claim_exact_duration():
    first = point("start", "start", 100, source="a")
    last = point("end", "end", 110, source="b")
    first.update(alignment_uncertainty_ns=7, raw_clock_id="node-a")
    last.update(alignment_uncertainty_ns=8, raw_clock_id="node-b")
    edge = M.explicit_flows(query(first, last))["links"][0]
    assert edge["duration_ns"] == 10 and edge["duration_uncertainty_ns"] == 15
    assert (
        edge["duration_lower_bound_ns"] == 0 and edge["duration_upper_bound_ns"] == 25
    )
    assert "calibrated clock estimate" in edge["timing"]
    last["start_ns"] = last["end_ns"] = 95
    edge = M.explicit_flows(query(first, last))["links"][0]
    assert edge["duration_ns"] is None and edge["duration_upper_bound_ns"] == 10
    assert "bounds overlap" in edge["timing"]
    last["start_ns"] = last["end_ns"] = 80
    result = M.explicit_flows(query(first, last))
    assert not result["links"] and result["rejected"] == 1


def test_calibrated_steps_with_ambiguous_cross_source_order_are_not_fabricated():
    rows = [
        point("a", "start", 100, source="a"),
        point("b", "step", 105, source="b"),
        point("c", "end", 110, source="c"),
    ]
    for row in rows:
        row.update(raw_clock_id="raw-" + row["id"], alignment_uncertainty_ns=10)
    result = M.explicit_flows(query(*rows))
    assert len(result["links"]) == 1
    assert (
        result["links"][0]["source_event_id"] == "a"
        and result["links"][0]["target_event_id"] == "c"
    )
    assert "uncertain clock ordering" in result["warnings"][0]


@given(st.lists(st.tuples(st.integers(0, 100), st.integers(0, 100)), max_size=50))
def test_union_matches_small_discrete_oracle(pairs):
    ranges = [tuple(sorted(pair)) for pair in pairs]
    observed = set()
    for start, end in ranges:
        observed.update(range(start, end))
    assert M.union_duration(ranges) == len(observed)


@given(st.integers(0, 1000), st.integers(1, 10), st.integers(1, 10))
def test_integer_clock_mapping_rounding_stays_inside_reported_uncertainty(
    raw, numerator, denominator
):
    cal = dict(
        schema="raven.trace-clock-calibration/v1",
        reference_id="reference",
        source_anchor_ns=500,
        reference_anchor_ns=10000,
        rate_num=numerator,
        rate_den=denominator,
        uncertainty_ns=0,
        valid_start_ns=0,
        valid_end_ns=1000,
    )
    result = M.transform_time(raw, cal)
    exact = Fraction(10000) + Fraction((raw - 500) * numerator, denominator)
    assert abs(exact - result["time_ns"]) <= result["uncertainty_ns"]
    assert type(result["time_ns"]) is int
