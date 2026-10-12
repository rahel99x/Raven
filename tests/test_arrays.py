"""Independent arithmetic oracles, large-array bounds, and retry invariants."""
import json
import random
import time

import pytest

from raven import arrays
from raven.model import Finished, Job


def row(identity, state, **kwargs):
    return {"id": identity, "state": state, "name": "experiment", **kwargs}


def expand(ranges):
    return {i for r in ranges for i in range(r["start"], r["end"] + 1, r["step"])}


def test_parser_overlap_stride_throttle_and_roundtrip():
    spec = "[0-30:2,3-29:3,6,6,99%20]"
    parsed = arrays.parse_range(spec)
    expected = set(range(0, 31, 2)) | set(range(3, 30, 3)) | {99}
    assert expand(parsed) == expected
    assert sum(r["count"] for r in parsed) == len(expected)
    assert expand(arrays.parse_range(arrays.compact(parsed))) == expected
    assert arrays.parse_range("2-9:4") == [{"start": 2, "end": 6, "step": 4, "count": 2}]


@pytest.mark.parametrize("spec", ["", "-1", "1-0", "0-9:0", "0-9:-1", "0:2", "1,", "1,,2", " 1", "1 ",
                                       "1%0", "1%2%3", "[1", "1]", "١", "1-2:3:4", str(1 << 63), "0-" + "9" * 100,
                                       "1," * 1025 + "2", "[]", "0-9%;rm -rf /", "1\n2"])
def test_malformed_specs_rejected(spec):
    with pytest.raises(ValueError):
        arrays.parse_range(spec)


def test_arithmetic_against_independent_small_set_oracle():
    rng = random.Random(1203)
    for _ in range(300):
        terms, expected = [], set()
        for _ in range(rng.randrange(1, 12)):
            start = rng.randrange(30)
            end = rng.randrange(start, 100)
            step = rng.randrange(1, 15)
            terms.append(f"{start}-{end}:{step}")
            expected.update(range(start, end + 1, step))
        parsed = arrays.parse_range(",".join(terms))
        assert expand(parsed) == expected
        assert sum(r["count"] for r in parsed) == len(expected)


def test_individual_precedence_current_history_and_steps():
    history = [row("123_4", "FAILED", end="2026-10-03T01:00:00"),
               row("123_4", "COMPLETED", end="2026-10-03T02:00:00"),
               row("123_5", "FAILED"), row("123_7", "FAILED")]
    current = [row("123_[0-7]", "PENDING"), row("123_4", "RUNNING"), row("123_5", "RUNNING"),
               row("123_4.batch", "FAILED"), row("123_4.0", "COMPLETED"), row("123", "FAILED"), row("124", "RUNNING")]
    group, = arrays.summarize(current, history)
    assert group["states"] == {"RUNNING": 2, "PENDING": 6}
    assert group["total"] == 8
    assert group["failures"] == ""
    assert not group["total_known"]
    assert group["unobserved"] is None
    assert len(arrays.tasks(group)) == 8
    assert arrays.tasks(group)[4]["state"] == "RUNNING"


def test_latest_history_and_completed_task_duration_only():
    history = [row("123_2", "FAILED", elapsed="00:50:00", end="2026-10-02T01:00:00"),
               row("123_2", "COMPLETED", elapsed="00:01:00", end="2026-10-03T01:00:00"),
               row("123_[3-9]", "COMPLETED", elapsed="00:20:00"),
               row("123_1", "COMPLETED", elapsed="00:03:00")]
    group, = arrays.summarize([], history)
    assert group["total"] == 9
    assert group["failed"] == 0
    assert group["duration"]["samples"] == 2
    assert group["duration"]["p50"] == 120
    assert group["duration"]["max"] == 180


def test_declared_membership_has_explicit_unobserved_tasks():
    group, = arrays.summarize([row("123", "PENDING", array_spec="0-9"), row("123_1", "RUNNING")],
                             [row("123_2", "COMPLETED")])
    assert group["total"] == 10
    assert group["observed"] == 2
    assert group["total_known"]
    assert group["unobserved"] == group["unknown"] == 8
    assert group["states"] == {"RUNNING": 1, "COMPLETED": 1, "UNKNOWN": 8}
    assert not arrays.tasks(group)[0]["observed"]
    with pytest.raises(ValueError, match="observed failure"):
        arrays.select_failed(group, indices="0")


def test_a_billion_tasks_remain_compact_and_huge_offset_is_fast():
    start = time.perf_counter()
    group, = arrays.summarize([row("123_[0-1000000000:2%20]", "PENDING"), row("123_999999998", "RUNNING")])
    assert group["total"] == 500000001
    assert group["states"] == {"RUNNING": 1, "PENDING": 500000000}
    assert len(group["cells"]) == 256
    assert len(group["_segments"]) <= 3
    assert [t["index"] for t in arrays.tasks(group, offset=499999999, limit=10)] == [999999998, 1000000000]
    assert time.perf_counter() - start < 2
    assert len(json.dumps(group)) < 100000


def test_maximum_supported_index_and_empty_pagination():
    group, = arrays.summarize([row(f"123_[0-{arrays.MAX_INDEX}]", "PENDING")], max_cells=0)
    assert group["total"] == arrays.MAX_INDEX + 1
    assert arrays.tasks(group, offset=arrays.MAX_INDEX, limit=2)[0]["index"] == arrays.MAX_INDEX
    assert arrays.tasks(group, offset=arrays.MAX_INDEX + 1) == []
    assert arrays.tasks(group, limit=0) == []


def test_thousands_of_individual_tasks_do_not_scale_quadratically():
    start = time.perf_counter()
    group, = arrays.summarize([row(f"123_{i}", "COMPLETED") for i in range(4000)])
    assert group["total"] == 4000
    assert group["exact"]
    assert time.perf_counter() - start < 2


@pytest.mark.parametrize("size", [10000, 50000])
def test_large_individual_arrays_use_observation_limit_not_arithmetic_fragment_limit(size):
    start = time.perf_counter()
    group, = arrays.summarize([row(f"123_{i}", "COMPLETED") for i in range(size)])
    assert group["total"] == size
    assert group["exact"]
    assert len(group["cells"]) == 256
    assert group["ranges"]["COMPLETED"] == f"0-{size - 1}"
    assert arrays.tasks(group, offset=size - 1, limit=10)[0]["index"] == size - 1
    assert time.perf_counter() - start < 4


def test_individual_observation_limit_remains_explicit(monkeypatch):
    monkeypatch.setattr(arrays, "MAX_SEGMENTS", 4)
    group, = arrays.summarize([row(f"123_{i}", "FAILED") for i in range(5)])
    assert group["total"] == 4
    assert not group["exact"]
    assert group["warnings"] == ["array observation segments exceed bounded observation count"]
    with pytest.raises(ValueError, match="exact"):
        arrays.select_failed(group)


def test_random_priority_overlap_counts_and_paginated_order():
    rng = random.Random(315)
    for _ in range(100):
        history = [row("123_[0-29:2]", "FAILED")]
        current = [row("123_[3-28:3]", "PENDING")]
        observed = {i: "FAILED" for i in range(0, 30, 2)}
        observed.update({i: "PENDING" for i in range(3, 29, 3)})
        for index in rng.sample(range(30), 10):
            state = rng.choice(["RUNNING", "COMPLETED", "FAILED"])
            current.append(row(f"123_{index}", state))
            observed[index] = state
        group, = arrays.summarize(current, history)
        assert group["total"] == len(observed)
        assert group["states"] == dict(__import__("collections").Counter(observed.values()))
        assert [(t["index"], t["state"]) for t in arrays.tasks(group, offset=7, limit=8)] == sorted(observed.items())[7:15]
        failures = expand(arrays.parse_range(group["failures"])) if group["failures"] else set()
        assert failures == {i for i, state in observed.items() if state == "FAILED"}


def test_specific_retry_never_includes_completed_running_unknown_or_cancelled():
    group, = arrays.summarize([row("123_2", "RUNNING")], [row("123_[0-9]", "FAILED"), row("123_3", "COMPLETED"), row("123_4", "CANCELLED by 1000")])
    assert expand(arrays.parse_range(arrays.select_failed(group))) == {0, 1, 5, 6, 7, 8, 9}
    assert arrays.select_failed(group, indices=[5, 6, 6, 7]) == "5-7"
    assert arrays.select_failed(group, indices="5-9:2") == "5-9:2"
    for index in [2, 3, 4, 99]:
        with pytest.raises(ValueError, match="observed failure"):
            arrays.select_failed(group, indices=[index])


def test_model_records_and_cluster_identity():
    live = Job("123_1", "experiment", "main", "RUNNING", elapsed="00:02:00")
    done = Finished("123_2", "experiment", "COMPLETED", elapsed="00:01:00")
    groups = arrays.summarize([live, row("123_1", "FAILED", cluster="other")], [done])
    assert len(groups) == 2
    assert groups[0]["total"] == 2
    assert groups[1]["cluster"] == "other"
    assert groups[1]["failed"] == 1
    assert arrays.tasks([live, done], offset=1)[0]["index"] == 2


def test_retry_plan_preserves_exact_spec_and_has_no_execution(tmp_path):
    script = tmp_path / "a script.sbatch"
    script.write_text("#!/bin/bash\n#SBATCH --array=0-99\necho hello\n")
    group, = arrays.summarize([], [row("123_[0-9]", "FAILED"), row("123_1", "COMPLETED")])
    plan = arrays.retry_plan(group, str(script), indices="2-8:2", limit=3, workdir=str(tmp_path))
    assert "--array=2-8:2%3" in plan["argv"]
    assert plan["array_retry"]["count"] == 4
    assert plan["array_retry"]["requires_confirmation"]
    assert script.read_text().startswith("#!/bin/bash\n#SBATCH --array=0-99")


def test_retry_and_pagination_reject_bad_selection_bounds():
    group, = arrays.summarize([], [row("123_1", "FAILED")])
    for indices in ([], [-1], [True], list(range(1025)), 5):
        with pytest.raises(ValueError):
            arrays.select_failed(group, indices=indices)
    for kwargs in ({"offset": -1}, {"offset": True}, {"limit": 5000}, {"limit": -1}, {"limit": True}):
        with pytest.raises(ValueError):
            arrays.tasks(group, **kwargs)
    group["exact"] = False
    with pytest.raises(ValueError, match="exact"):
        arrays.select_failed(group)


def test_bounded_group_and_cell_limits_and_no_plain_numeric_parent():
    assert arrays.summarize([row("123", "FAILED"), row("123.batch", "FAILED")]) == []
    assert len(arrays.summarize([row("123_1", "PENDING"), row("124_1", "FAILED")], max_groups=1)) == 1
    assert arrays.summarize([row("123_1", "PENDING")], max_groups=0) == []
    for kwargs in ({"max_groups": -1}, {"max_groups": True}, {"max_cells": -1}, {"max_cells": 5000}):
        with pytest.raises(ValueError):
            arrays.summarize([], **kwargs)


def test_adversarial_stride_overlap_becomes_explicitly_inexact_without_hanging():
    start = time.perf_counter()
    group, = arrays.summarize([row("123_[0-100000000000000:10007]", "PENDING"),
                              row("123_[0-100000000000000:10009]", "FAILED")])
    assert not group["exact"]
    assert group["truncated"]
    assert group["warnings"]
    with pytest.raises(ValueError, match="exact"):
        arrays.select_failed(group)
    assert time.perf_counter() - start < 2


def test_input_limit_is_explicit_and_disables_stale_retry(monkeypatch):
    monkeypatch.setattr(arrays, "MAX_RECORDS", 2)
    group, = arrays.summarize([row("123_0", "FAILED"), row("123_1", "FAILED"), row("123_0", "RUNNING")])
    assert not group["exact"]
    assert "input records exceed bounded observation count" in group["warnings"]
    with pytest.raises(ValueError, match="exact"):
        arrays.select_failed(group)
