"""CPU locations are point evidence, never inferred residency intervals."""
import copy

import pytest

from raven.trace_cpu_timeline import build


def event(seq=1, **observation):
    return {"kind": "call", "mono_ns": 1000 + seq, "seq": seq, "thread": "worker",
            "data": {"function": "solve", "cpu_observation": {
                "schema": "raven.cpu-placement/v1", "source": "linux-sched_getcpu",
                "logical_cpu": 4, "socket_id": 0, "core_id": 2,
                "sample_start_ns": 1000 + seq, "sample_end_ns": 1010 + seq, **observation}}}


def test_topology_groups_real_ids_and_does_not_fill_gaps():
    events = [event(), event(2, logical_cpu=12, socket_id=1, core_id=2, sample_start_ns=2000, sample_end_ns=2010),
              event(3, logical_cpu=5, sample_start_ns=3000, sample_end_ns=3010)]
    original = copy.deepcopy(events)
    model = build({"events": events}, 1000)
    assert model["status"] == "observed"
    assert [(row["socket_id"], row["core_id"], row["logical_cpu"]) for row in model["lanes"]] == [(0, 2, 4), (0, 2, 5), (1, 2, 12)]
    assert [row["at_ns"] for row in model["observations"]] == [6, 1005, 2005]
    assert all(row["end_ns"] - row["start_ns"] == 10 for row in model["observations"])
    assert all("duration_ns" not in row for row in model["observations"])
    assert "execution between observations is unknown" in model["warnings"][0]
    assert events == original


def test_unknown_physical_topology_is_distinct_from_core_zero():
    model = build({"events": [event(socket_id=None, core_id=None)]}, 1000)
    assert model["lanes"][0]["label"] == "Socket ? / core ? / logical CPU 4"
    assert model["observations"][0]["core_id"] is None
    assert any("unknown, not core zero" in warning for warning in model["warnings"])


@pytest.mark.parametrize("bad", [
    {"logical_cpu": True}, {"logical_cpu": -1}, {"logical_cpu": 1 << 31}, {"socket_id": "1"},
    {"core_id": False}, {"core_id": -1}, {"sample_start_ns": 999}, {"sample_end_ns": 1000},
    {"sample_end_ns": float("nan")}, {"sample_start_ns": True}, {"sample_end_ns": 1 << 64},
    {"source": "guessed-from-thread"}, {"schema": "raven.cpu-placement/v2"},
])
def test_malformed_optional_cpu_data_never_becomes_measured_placement(bad):
    model = build({"events": [event(), event(2, **bad)]}, 1000)
    assert model["status"] == "partial"
    assert len(model["observations"]) == 1
    assert any("Rejected 1" in warning for warning in model["warnings"])


def test_descriptive_extensions_do_not_change_known_placement():
    assert build({"events": [event(vendor_description={"vendor": "example"})]}, 1000)["observations"] == build({"events": [event()]}, 1000)["observations"]


def test_no_cpu_data_does_not_infer_location_from_thread_or_process():
    row = event()
    row["data"].pop("cpu_observation")
    model = build({"events": [row], "identity": {"pid": 4}}, 1000)
    assert model["status"] == "not-recorded"
    assert model["observations"] == model["lanes"] == []


def test_unavailable_capture_keeps_reported_reason_visible():
    model = build({"events": [], "covered": {"cpu_placement": {"status": "unavailable", "reason": "No sched_getcpu API"}}}, 0)
    assert model["status"] == "unavailable"
    assert "No sched_getcpu API" in model["warnings"]


def test_overlapping_query_brackets_are_indexed_by_time_without_rewriting_sequence():
    model = build({"events": [event(sample_end_ns=5000), event(2)]}, 1000)
    assert [row["seq"] for row in model["observations"]] == [2, 1]


def test_cpu_display_text_does_not_interpret_terminal_controls():
    row = event()
    row["data"]["function"] = "solve\x1b[31m"
    model = build({"events": [row]}, 1000)
    assert "\x1b" not in model["observations"][0]["name"]


def test_cpu_window_remains_bounded():
    with pytest.raises(ValueError, match="20,000"):
        build({"events": [event()] * 20001}, 1000)
