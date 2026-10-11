"""Cooperative evidence must never become invented causality or deadlock."""
from __future__ import annotations

import copy
import json

import pytest

from tower import trace_analysis as T


def event(kind, data, *, stream="a", seq=1, t=10, **identity):
    return dict(kind=kind, data=data, stream=stream, seq=seq, mono_ns=t,
                thread="main", **dict(dict(node="node-a", pid="100", job_id="42", attempt="try-1"), **identity))


def span(name="work", *, stream="a", start=10, end=20, seq=1, parent=None, **identity):
    data = dict(span_id=name, name=name)
    if parent is not None:
        data["parent_id"] = parent
    return [event("span_start", data, stream=stream, seq=seq, t=start, **identity),
            event("span_end", dict(span_id=name), stream=stream, seq=seq + 1, t=end, **identity)]


def link(source, target, *, seq=1, relation="precedes"):
    return event("link", dict(source=source, target=target, relation=relation), stream="links", seq=seq, t=seq)


def collective(rank, *, phase="enter", operation="AllReduce", sequence=7, members=(0, 1),
               communicator="world", generation=0, dtype="float32", count=128, root=None,
               stream=None, seq=None, **identity):
    data = dict(communicator=communicator, generation=generation, sequence=sequence,
                operation=operation, rank=rank, members=list(members), dtype=dtype,
                count=count, phase=phase)
    if root is not None:
        data["root"] = root
    seq = (1 if phase == "enter" else 2) if seq is None else seq
    return event("collective", data, stream=stream or f"rank{rank}", seq=seq, t=seq,
                 **dict(dict(pid=str(100 + rank)), **identity))


def complete_collective(**kwargs):
    return [collective(rank, phase=phase, **kwargs) for rank in kwargs.get("members", (0, 1))
            for phase in ("enter", "complete")]


def finding(result):
    return result["data"]["collectives"][0]


def test_empty_and_invalid_inputs_are_explicit():
    assert T.analyze_causality([])["status"] == "EMPTY"
    assert T.audit_collectives([])["status"] == "EMPTY"
    assert T.analyze_causality(None)["status"] == "INVALID"
    assert T.audit_collectives(None)["status"] == "UNKNOWN"


@pytest.mark.parametrize("other,expected", [("sum", "MATCH"), ("max", "MISMATCH"), (None, "UNKNOWN")])
def test_declared_reduction_operator_is_compared(other, expected):
    events = complete_collective()
    for item in events:
        operation = "sum" if item["data"]["rank"] == 0 else other
        if operation is not None:
            item["data"]["reduction"] = operation
    assert T.audit_collectives(events)["status"] == expected


def test_collectives_on_different_clusters_never_supply_each_others_ranks():
    events = [collective(rank, phase=phase, cluster="alpha" if rank == 0 else "beta")
              for rank in (0, 1) for phase in ("enter", "complete")]
    result = T.audit_collectives(events)
    assert result["status"] == "INCOMPLETE"
    assert len(result["data"]["collectives"]) == 2


def test_cross_cluster_causal_dependency_is_not_accepted():
    events = span("a", cluster="alpha") + span("b", stream="b", cluster="beta") + [link("a:a", "b:b")]
    assert T.analyze_causality(events)["status"] == "INVALID"


def test_measured_path_uses_explicit_edges_and_not_cross_node_timestamps():
    events = (span("read", start=1_000, end=1_020) +
              span("compute", stream="b", start=1, end=31, node="node-b", pid="200") +
              span("unrelated", stream="c", start=5, end=45) +
              [link("a:read", "b:compute")])
    result = T.analyze_causality(events)
    assert result["status"] == "COMPLETE"
    assert result["data"]["path"] == ["a:read", "b:compute"]
    assert result["data"]["duration_ns"] == 50
    assert "no inferred cross-node" in result["data"]["clock_model"]


def test_no_timing_adjacency_edges_are_invented():
    result = T.analyze_causality(span("first") + span("second", start=30, end=45, seq=3))
    assert result["data"]["path"] == ["a:second"]
    assert result["data"]["duration_ns"] == 15
    assert result["data"]["edges"] == []


def test_out_of_order_transport_and_exact_replay_are_idempotent():
    events = span() + span("other", stream="b", node="node-b") + [link("a:work", "b:other")]
    expected = T.analyze_causality(events)
    assert T.analyze_causality(list(reversed(events)) + copy.deepcopy(events)) == expected


def test_conflicting_replay_never_produces_path():
    events = span()
    events.append(dict(events[0], data={"span_id": "different"}))
    result = T.analyze_causality(events)
    assert result["status"] == "INVALID"
    assert result["data"]["path"] == []
    assert any("Conflicting replay" in warning for warning in result["warnings"])


@pytest.mark.parametrize("identity", [{"pid": "999"}, {"node": "another"}, {"attempt": "try-2"}, {"job_id": "43"}])
def test_stream_identity_collision_is_invalid(identity):
    events = span()
    events[1].update(identity)
    assert T.analyze_causality(events)["status"] == "INVALID"


@pytest.mark.parametrize("bad", [None, {}, [], {"kind": "span_start"},
                                 event("span_start", {}, seq=True),
                                 event("span_start", {}, t=-1),
                                 event("span_start", {}, t=float("inf")),
                                 event("span_start", {}, t=1.5),
                                 event("span_start", {}, stream="bad\x1b[31m")])
def test_malformed_envelopes_are_bounded_and_rejected(bad):
    assert T.analyze_causality([bad])["status"] == "INVALID"
    assert T.audit_collectives([bad])["status"] == "UNKNOWN"


def test_sequence_gap_is_partial_without_inventing_missing_duration():
    events = span()
    events[1]["seq"] = 5
    result = T.analyze_causality(events)
    assert result["status"] == "PARTIAL"
    assert result["data"]["duration_ns"] == 10
    assert any("Sequence gap" in warning for warning in result["warnings"])


def test_snapshot_need_not_begin_with_sequence_one():
    assert T.analyze_causality(span(seq=1_000))["status"] == "COMPLETE"


def test_loss_signal_is_preserved():
    result = T.analyze_causality(span() + [event("loss", {"count": 4}, stream="collector")])
    assert result["status"] == "PARTIAL"
    assert any("lost trace" in warning for warning in result["warnings"])


@pytest.mark.parametrize("events", [span(start=20, end=10),
                                    [event("span_end", {"span_id": "x"}), event("span_start", {"span_id": "x"}, seq=2)],
                                    span() + [event("span_end", {"span_id": "work"}, seq=3, t=30)],
                                    span() + span(seq=3, start=30, end=40)])
def test_backward_or_reused_span_records_are_invalid(events):
    result = T.analyze_causality(events)
    assert result["status"] == "INVALID"
    assert result["data"]["duration_ns"] is None


@pytest.mark.parametrize("events", [[span()[0]], [span()[1]], span(parent="absent"),
                                    span() + [link("a:work", "b:missing")]])
def test_open_and_missing_evidence_is_partial(events):
    assert T.analyze_causality(events)["status"] == "PARTIAL"


def test_parent_elapsed_time_is_not_counted_again_with_child():
    events = [event("span_start", {"span_id": "parent"}, seq=1, t=0),
              *span("child", seq=2, start=10, end=90, parent="parent"),
              event("span_end", {"span_id": "parent"}, seq=4, t=100)]
    result = T.analyze_causality(events)
    assert result["status"] == "COMPLETE"
    assert result["data"]["path"] == ["a:child"]
    assert result["data"]["duration_ns"] == 80
    assert next(item for item in result["data"]["spans"] if item["id"] == "a:parent")["duration_ns"] == 100
    result = T.analyze_causality(events + [link("a:parent", "a:child")])
    assert result["status"] == "PARTIAL"
    assert result["data"]["duration_ns"] == 80


def test_parent_cycles_and_outside_parent_are_invalid():
    assert T.analyze_causality(span(parent="work"))["status"] == "INVALID"
    events = span("child", parent="parent") + span("parent", seq=3, start=30, end=40)
    assert T.analyze_causality(events)["status"] == "INVALID"


def test_explicit_cycles_are_invalid_even_at_zero_duration_or_nested_nodes():
    events = span("x", start=0, end=0) + span("y", stream="b", start=0, end=0)
    result = T.analyze_causality(events + [link("a:x", "b:y"), link("b:y", "a:x", seq=2)])
    assert result["status"] == "INVALID"
    assert result["data"]["path"] == []
    assert any("cycle" in warning for warning in result["warnings"])
    events = [event("span_start", {"span_id": "p"}, seq=1, t=0),
              *span("c", seq=2, start=0, end=0, parent="p"),
              event("span_end", {"span_id": "p"}, seq=4, t=0)]
    assert T.analyze_causality(events + [link("a:p", "a:c"), link("a:c", "a:p", seq=2)])["status"] == "INVALID"


def test_overlap_is_not_finish_before_start():
    events = [event("span_start", {"span_id": "x"}, seq=1, t=0),
              event("span_start", {"span_id": "y"}, seq=2, t=5),
              event("span_end", {"span_id": "x"}, seq=3, t=10),
              event("span_end", {"span_id": "y"}, seq=4, t=15)]
    assert T.analyze_causality(events + [link("a:x", "a:y")])["status"] == "INVALID"


def test_unknown_link_semantics_never_sum_potentially_overlapping_spans():
    events = span() + span("gpu", stream="b", start=0, end=50)
    result = T.analyze_causality(events + [link("a:work", "b:gpu", relation="launch")])
    assert result["status"] == "PARTIAL"
    assert result["data"]["duration_ns"] == 50
    assert len(result["data"]["edges"]) == 1


def test_dependency_cannot_mix_attempts():
    events = span() + span("next", stream="b", attempt="try-2") + [link("a:work", "b:next")]
    assert T.analyze_causality(events)["status"] == "INVALID"


def test_malformed_endpoint_is_invalid_and_duplicate_links_are_deduplicated():
    assert T.analyze_causality(span() + [link("work", "a:work")])["status"] == "INVALID"
    events = span() + span("next", stream="b") + [link("a:work", "b:next"), link("a:work", "b:next", seq=2)]
    result = T.analyze_causality(events)
    assert result["status"] == "COMPLETE"
    assert len(result["data"]["edges"]) == 1


def test_long_graph_is_iterative_and_does_not_use_recursion():
    events = []
    for index in range(1_100):
        events += span(str(index), stream=f"s{index}", start=0, end=1)
        if index:
            events.append(link(f"s{index-1}:{index-1}", f"s{index}:{index}", seq=index))
    result = T.analyze_causality(events)
    assert result["status"] == "COMPLETE"
    assert result["data"]["duration_ns"] == 1_100


def test_bounds_stop_generator_consumption_and_bound_outputs(monkeypatch):
    monkeypatch.setattr(T, "MAX_EVENTS", 3)
    consumed = []

    def source():
        for index in range(1_000):
            consumed.append(index)
            yield event("note", {}, seq=index, t=index)

    result = T.analyze_causality(source())
    assert result["status"] == "PARTIAL"
    assert len(consumed) == 4
    monkeypatch.setattr(T, "MAX_EVENTS", 50)
    monkeypatch.setattr(T, "MAX_SPANS", 2)
    result = T.analyze_causality(span("a") + span("b", stream="b") + span("c", stream="c"))
    assert result["status"] == "PARTIAL"
    assert len(result["data"]["spans"]) == 2


def test_input_not_mutated_and_all_outputs_json_serializable():
    events = span() + complete_collective()
    before = copy.deepcopy(events)
    json.dumps(T.analyze_causality(events))
    json.dumps(T.audit_collectives(events))
    assert events == before


def test_collective_matching_entries_and_completions():
    result = T.audit_collectives(complete_collective())
    assert result["status"] == "MATCH"
    assert finding(result)["agreement"] == "MATCH"
    assert finding(result)["missing_ranks"] == []
    assert finding(result)["unfinished_ranks"] == []


@pytest.mark.parametrize("operation", ["AllReduce", "MPI_Allreduce", "ncclAllReduce", "all-reduce"])
def test_operation_aliases(operation):
    assert T.audit_collectives(complete_collective(operation=operation))["status"] == "MATCH"


def test_missing_ranks_and_missing_completions_are_separate_not_deadlock():
    result = T.audit_collectives([collective(0), collective(0, phase="complete")])
    assert result["status"] == "INCOMPLETE"
    assert finding(result)["missing_ranks"] == [1]
    result = T.audit_collectives([collective(0), collective(1)])
    assert result["status"] == "INCOMPLETE"
    assert finding(result)["agreement"] == "MATCH"
    assert finding(result)["missing_ranks"] == []
    assert finding(result)["unfinished_ranks"] == [0, 1]


def test_complete_without_enter_is_incomplete():
    result = T.audit_collectives([collective(0, phase="complete"), collective(1, phase="complete")])
    assert result["status"] == "INCOMPLETE"
    assert finding(result)["missing_ranks"] == [0, 1]
    assert finding(result)["unfinished_ranks"] == []


@pytest.mark.parametrize("change", [{"operation": "Bcast", "root": 0}, {"dtype": "int32"}, {"count": 127}])
def test_definite_collective_mismatch(change):
    result = T.audit_collectives([collective(0), collective(1, **change)])
    assert result["status"] == "MISMATCH", result
    assert finding(result)["agreement"] == "MISMATCH"


def test_datatype_aliases_and_zero_length_signatures():
    events = [collective(0, dtype="MPI_FLOAT"), collective(1, dtype="float32")]
    assert finding(T.audit_collectives(events))["agreement"] == "MATCH"
    events = [collective(0, operation="Bcast", root=0, count=0, dtype="int32"),
              collective(1, operation="Bcast", root=0, count=0, dtype="float32")]
    assert finding(T.audit_collectives(events))["agreement"] == "MATCH"
    events = [collective(0, count=0, dtype="int32"), collective(1, count=0, dtype="float32")]
    assert finding(T.audit_collectives(events))["agreement"] == "MISMATCH"


def test_broadcast_root_agreement_and_non_root_receivers():
    assert T.audit_collectives(complete_collective(operation="MPI_Bcast", root=1))["status"] == "MATCH"
    for roots in [(0, 1), (5, 5)]:
        result = T.audit_collectives([collective(0, operation="Bcast", root=roots[0]),
                                     collective(1, operation="Bcast", root=roots[1])])
        assert result["status"] == "MISMATCH"
    assert T.audit_collectives(complete_collective(operation="Bcast"))["status"] == "UNKNOWN"


def test_barrier_ignores_inapplicable_buffers():
    events = complete_collective(operation="Barrier", dtype=None, count=None)
    events[1]["data"].update(count=10, dtype="int32", root=8)
    assert T.audit_collectives(events)["status"] == "MATCH"


@pytest.mark.parametrize("operation", ["Gatherv", "Scatterv", "Alltoallv", "Reduce_scatter", "unknown"])
def test_legitimate_unequal_counts_of_unsupported_operations_are_not_mismatches(operation):
    result = T.audit_collectives([collective(0, operation=operation, count=8),
                                 collective(1, operation=operation, count=16)])
    assert result["status"] == "UNKNOWN"
    assert finding(result)["agreement"] == "UNKNOWN"


@pytest.mark.parametrize("dtype,count", [("my-derived-type", 4), ("MPI_INT", 4), (None, 4), ("float32", None),
                                         ("float32", True), ("float32", -1)])
def test_unknown_signatures_are_not_claimed_compatible(dtype, count):
    assert T.audit_collectives(complete_collective(dtype=dtype, count=count))["status"] == "UNKNOWN"


def test_rank_owner_collision_is_unknown_not_blame():
    events = [collective(0), collective(0, stream="imposter", pid="500", operation="Barrier")]
    result = T.audit_collectives(events)
    assert result["status"] == "UNKNOWN"
    assert any("multiple process" in reason for reason in finding(result)["reasons"])


def test_changed_membership_without_generation_is_unknown():
    result = T.audit_collectives([collective(0), collective(1, members=(1, 0))])
    assert result["status"] == "UNKNOWN"


@pytest.mark.parametrize("change", [{"generation": 1}, {"communicator": "other"}, {"sequence": 8},
                                    {"attempt": "try-2"}, {"job_id": "43"}])
def test_group_scope_preserves_attempt_and_communicator_generations(change):
    result = T.audit_collectives([collective(0), collective(1, **change)])
    assert len(result["data"]["collectives"]) == 2
    assert result["status"] == "INCOMPLETE"


def test_consistent_replays_and_repeated_rank_entry_are_idempotent():
    events = complete_collective()
    replay = copy.deepcopy(events)
    replay += [collective(0, seq=3)]
    assert T.audit_collectives(list(reversed(events)) + replay)["status"] == "MATCH"


def test_rank_changing_parameters_between_entry_and_completion_is_mismatch():
    events = complete_collective()
    events[1]["data"]["count"] = 3
    assert T.audit_collectives(events)["status"] == "MISMATCH"


@pytest.mark.parametrize("change", [{"members": []}, {"members": [0, 0]}, {"members": [True, 1]},
                                    {"members": [[0], 1]}, {"rank": 4}, {"generation": None},
                                    {"phase": "pending"}, {"sequence": -1}, {"operation": None}])
def test_invalid_collective_fields_do_not_crash_or_guess(change):
    record = collective(0)
    record["data"].update(change)
    assert T.audit_collectives([record])["status"] == "UNKNOWN"


def test_collective_limits_and_loss_are_visible(monkeypatch):
    monkeypatch.setattr(T, "MAX_COLLECTIVES", 1)
    result = T.audit_collectives(complete_collective() + complete_collective(sequence=8, seq=3))
    assert len(result["data"]["collectives"]) == 1
    assert result["status"] in ("INCOMPLETE", "UNKNOWN")
    result = T.audit_collectives(complete_collective() + [event("lost", {}, stream="collector")])
    assert result["status"] == "INCOMPLETE"


def test_external_text_never_emits_terminal_controls():
    events = span()
    events[0]["data"]["name"] = "hello\x1b[31m\nunsafe"
    assert "\x1b" not in "\n".join(T.analyze_causality(events)["rows"])


def test_qualified_identifier_cannot_alias_another_stream():
    events = span("x:y", stream="a") + span("y", stream="a:x")
    assert T.analyze_causality(events)["status"] == "INVALID"


def test_long_qualified_endpoints_remain_usable():
    a, b = "a" * 200, "b" * 200
    name = "n" * 200
    result = T.analyze_causality(span(name, stream=a) + span(name, stream=b) + [link(f"{a}:{name}", f"{b}:{name}")])
    assert result["status"] == "COMPLETE"
    assert len(result["data"]["path"]) == 2


def test_oversized_integer_identifiers_do_not_raise_conversion_errors():
    record = span()[0]
    record["data"]["span_id"] = 1 << 100_000
    assert T.analyze_causality([record])["status"] == "INVALID"
    record = collective(0)
    record["data"]["operation"] = 1 << 100_000
    assert T.audit_collectives([record])["status"] == "UNKNOWN"


def test_corrupt_envelope_cannot_certify_a_collective_mismatch():
    events = [collective(0), collective(1, count=1)]
    events.append(dict(events[0], data=dict(events[0]["data"], count=1)))
    result = T.audit_collectives(events)
    assert result["status"] == "UNKNOWN"
    assert finding(result)["agreement"] == "UNKNOWN"
    assert finding(result)["status"] == "UNKNOWN"


def test_per_rank_evidence_is_available_for_explanation():
    result = T.audit_collectives([collective(0), collective(1, count=1)])
    evidence = finding(result)["rank_evidence"]
    assert [(item["rank"], item["count"]) for item in evidence] == [(0, 128), (1, 1)]
    assert evidence[0]["stream"] == "rank0"
