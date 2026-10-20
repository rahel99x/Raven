"""Visual intervals preserve recording boundaries and their physical clock."""
from copy import deepcopy

import pytest

from raven import trace_timeline as T


def snapshot(rows):
    events = []
    for seq, item in enumerate(rows, 1):
        kind, frame, mono, *extra = item
        thread = extra[0] if extra else "main"
        data = {"function": frame, "frame": frame, "file": "solver.py", "line": 12}
        if kind.startswith("span_"):
            data = {"name": frame, "span_id": frame}
        events.append(dict(seq=seq, kind=kind, data=data, mono_ns=10**18 + mono, thread=thread, pid=123))
    return dict(status="COMPLETE", events=events, identity={"stream": "a-stream", "job_id": "42", "attempt": "first", "pid": 123},
                covered={"adapter": "python-sys-monitoring", "scope": "project"}, warnings=[],
                read_seq=len(events), committed_seq=len(events), evicted=0, sha256="a" * 64)


def test_nested_calls_preserve_depth_source_and_exact_nanoseconds():
    model = T.build(snapshot([("call", "outer", 1), ("call", "inner", 2), ("return", "inner", 5), ("return", "outer", 10)]))
    outer, inner = model["intervals"]
    assert (outer["depth"], inner["depth"]) == (0, 1)
    assert (outer["start_ns"], outer["end_ns"], outer["duration_ns"]) == (0, 9, 9)
    assert (inner["start_ns"], inner["end_ns"], inner["duration_ns"]) == (1, 4, 3)
    assert model["origin_ns"] == 10**18 + 1
    assert inner["file"] == "solver.py" and inner["line"] == 12
    assert model["unpaired_frame_events"] == 0
    assert T.index(model)["by_id"]["python:2"] is inner


def test_generator_segments_stay_separate_and_threads_do_not_share_depth():
    model = T.build(snapshot([("call", "outer", 0), ("call", "generator", 1), ("yield", "generator", 3),
        ("call", "worker", 4, "worker"), ("return", "worker", 5, "worker"),
        ("resume", "generator", 6), ("unwind", "generator", 8), ("return", "outer", 10)]))
    assert [(row["name"], row["depth"], row["duration_ns"]) for row in model["intervals"]] == [
        ("outer", 0, 10), ("generator", 1, 2), ("worker", 0, 1), ("generator", 1, 2)]
    assert model["threads"] == ["main", "worker"]


@pytest.mark.parametrize("boundary", ["gap", "lost", "drop", "loss", "dropped"])
def test_unknown_boundary_never_becomes_a_continuous_interval(boundary):
    model = T.build(snapshot([("call", "outer", 0), ("span_start", "phase", 1),
        (boundary, "unknown", 2), ("span_end", "phase", 3), ("return", "outer", 4)]))
    assert model["intervals"] == []
    assert model["unpaired_frame_events"] == 2
    assert model["unpaired_span_events"] == 2
    assert any(marker["kind"] == boundary for marker in model["markers"])
    assert any("Unknown boundaries" in warning for warning in model["warnings"])


def test_python_checkpoint_does_not_erase_an_independent_cooperative_span():
    model = T.build(snapshot([("call", "outer", 0), ("span_start", "phase", 1),
        ("stack_checkpoint", "unknown", 2), ("span_end", "phase", 3), ("return", "outer", 4)]))
    assert [(row["name"], row["kind"]) for row in model["intervals"]] == [("phase", "cooperative")]
    assert model["unpaired_frame_events"] == 2 and model["unpaired_span_events"] == 0


def test_unknown_prefix_open_tail_and_zero_duration_are_not_fabricated():
    source = snapshot([("return", "missing", 0), ("call", "zero", 1), ("return", "zero", 1), ("call", "open", 2)])
    source.update(status="LIVE", evicted=8)
    model = T.build(source)
    assert len(model["intervals"]) == 1 and model["intervals"][0]["duration_ns"] == 0
    assert model["unpaired_frame_events"] == 2 and model["evicted_events"] == 8
    assert [row["name"] for row in model["markers"]] == ["missing", "open"]


def test_overlapping_cooperative_spans_have_separate_reusable_lanes():
    model = T.build(snapshot([("span_start", "a", 0), ("span_start", "b", 1), ("span_end", "a", 2),
                             ("span_start", "c", 3), ("span_end", "b", 4), ("span_end", "c", 5)]))
    assert [(row["name"], row["depth"]) for row in model["intervals"]] == [("a", 0), ("b", 1), ("c", 0)]
    assert all(row["kind"] == "cooperative" for row in model["intervals"])


def test_invalid_and_oversized_snapshots_are_refused_and_source_is_unchanged():
    source = snapshot([("call", "f", 0), ("return", "f", 1)])
    original = deepcopy(source)
    T.build(source)
    assert source == original
    with pytest.raises(ValueError, match="invalid trace"):
        T.build(dict(source, status="INVALID"))
    with pytest.raises(ValueError, match="20,000"):
        T.build(dict(source, events=source["events"] * 10_001))


def test_untrusted_labels_cannot_write_terminal_controls():
    source = snapshot([("call", "unsafe\x1b[2Jname\n", 0), ("return", "unsafe\x1b[2Jname\n", 1)])
    source["events"][0]["data"]["file"] = "file\x1b]52;c;SECRET\x07.py"
    interval = T.build(source)["intervals"][0]
    assert "\x1b" not in interval["name"] + interval["file"]
    assert "\n" not in interval["name"]


def test_detail_names_preserve_valid_recorded_text_beyond_chart_label_width():
    name = "solver_" + "nested_" * 250
    source = snapshot([("call", name, 0), ("return", name, 1)])
    assert T.build(source)["intervals"][0]["name"] == name
