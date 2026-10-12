"""Independent array arithmetic and precedence review against tiny set oracles."""
from collections import Counter
import random

import pytest

from raven import arrays


def expanded(ranges):
    ranges = list(ranges)
    values = [index for item in ranges for index in range(item["start"], item["end"] + 1, item["step"])]
    assert len(values) == len(set(values)), "normalized progressions overlap"
    assert len(values) == sum(item["count"] for item in ranges)
    return set(values)


def test_randomized_summary_precedence_against_materialized_observation_table():
    rng = random.Random(402107)
    states = ["PENDING", "RUNNING", "COMPLETED", "FAILED", "OUT_OF_MEMORY", "TIMEOUT", "CANCELLED", "UNKNOWN"]
    for _ in range(400):
        inputs = {"history": [], "current": []}
        oracle = {}
        sequence = 0
        for source in inputs:
            for _ in range(rng.randrange(1, 28)):
                sequence += 1
                individual = bool(rng.getrandbits(1))
                state = rng.choice(states)
                timestamp = f"2026-10-03T00:00:{rng.randrange(60):02d}"
                if individual:
                    index = rng.randrange(50)
                    identity, indices = f"71_{index}", {index}
                else:
                    terms, indices = [], set()
                    for _ in range(rng.randrange(1, 4)):
                        start = rng.randrange(40)
                        end = rng.randrange(start, 50)
                        step = rng.randrange(1, 11)
                        terms.append(f"{start}-{end}:{step}")
                        indices.update(range(start, end + 1, step))
                    identity = "71_[" + ",".join(terms) + f"%{rng.randrange(1, 8)}]"
                duration = rng.randrange(1, 10000)
                inputs[source].append({"id": identity, "state": state, "end": timestamp, "elapsed_s": duration})
                priority = (source == "current", individual, timestamp, sequence)
                for index in indices:
                    if index not in oracle or priority > oracle[index][0]:
                        oracle[index] = (priority, state, source, duration if individual else None)
        group, = arrays.summarize(inputs["current"], inputs["history"], max_cells=0)
        assert group["exact"]
        assert group["states"] == dict(Counter(value[1] for value in oracle.values()))
        assert group["total"] == len(oracle)
        drilldown = arrays.tasks(group, limit=100)
        assert [(item["index"], item["state"], item["source"], item["duration"]) for item in drilldown] == [
            (index, value[1], value[2], value[3]) for index, value in sorted(oracle.items())]
        for offset in [0, 7, len(oracle) - 1, len(oracle), len(oracle) + 10]:
            assert arrays.tasks(group, offset=offset, limit=6) == drilldown[offset:offset + 6]
        expected_failures = {index for index, value in oracle.items() if value[1] in arrays.FAILED_STATES}
        if expected_failures:
            assert expanded(arrays.parse_range(arrays.select_failed(group))) == expected_failures
        else:
            with pytest.raises(ValueError, match="no observed failed"):
                arrays.select_failed(group)
        completed = sorted(value[3] for value in oracle.values() if value[1] == "COMPLETED" and value[3] is not None)
        assert group["duration"]["samples"] == len(completed)
        assert group["duration"]["max"] == (max(completed) if completed else None)


def test_randomized_strides_near_int63_boundary_do_not_overflow_or_duplicate():
    rng = random.Random(9631)
    base = arrays.MAX_INDEX - 300
    for _ in range(250):
        terms, oracle = [], set()
        for _ in range(rng.randrange(1, 10)):
            lo, hi = sorted([rng.randrange(301), rng.randrange(301)])
            stride = rng.randrange(1, 40)
            terms.append(f"{base + lo}-{base + hi}:{stride}")
            oracle.update(base + x for x in range(lo, hi + 1, stride))
        result = arrays.parse_range(",".join(terms))
        assert expanded(result) == oracle
        assert expanded(arrays.parse_range(arrays.compact(result))) == oracle


def test_chinese_remainder_subtraction_against_independent_small_sets():
    rng = random.Random(75042)
    for _ in range(2000):
        start_a, start_b = rng.randrange(12), rng.randrange(12)
        a = arrays._Range(start_a, start_a + rng.randrange(20) * 1, rng.randrange(1, 12))
        b = arrays._Range(start_b, start_b + rng.randrange(20) * 1, rng.randrange(1, 12))
        # The internal range helper normally receives normalized endpoints.
        a = arrays._Range(a.start, a.start + (a.end - a.start) // a.step * a.step, a.step)
        b = arrays._Range(b.start, b.start + (b.end - b.start) // b.step * b.step, b.step)
        material_a = set(range(a.start, a.end + 1, a.step))
        material_b = set(range(b.start, b.end + 1, b.step))
        difference = expanded(item.plain() for item in arrays._subtract(a, b))
        assert difference == material_a - material_b


@pytest.mark.parametrize("bad_id", ["71_[0-9:0]", "71_[-1]", "71_-1", "71_[broken]"])
def test_malformed_active_observation_cannot_enable_retry_of_stale_failure(bad_id):
    history = [{"id": "71_2", "state": "FAILED"}]
    current = [{"id": bad_id, "state": "RUNNING"}]
    group, = arrays.summarize(current, history)
    assert group["exact"] is False and group["warnings"]
    with pytest.raises(ValueError, match="exact"):
        arrays.select_failed(group)


def test_declared_membership_conflict_cannot_claim_full_coverage_or_enable_retry():
    group, = arrays.summarize([{"id": "71", "state": "PENDING", "array_spec": "0-4"}],
                             [{"id": "71_8", "state": "FAILED"}])
    assert group["exact"] is False and group["total_known"] is False
    assert group["warnings"]
    with pytest.raises(ValueError, match="exact"):
        arrays.select_failed(group)


@pytest.mark.parametrize("duration", [10 ** 1000, True, False, float("nan"), float("inf"), -1])
def test_bad_accounting_duration_cannot_crash_or_pollute_statistics(duration):
    group, = arrays.summarize([], [{"id": "71_1", "state": "COMPLETED", "elapsed_s": duration}])
    assert group["duration"]["samples"] == 0
    assert arrays.tasks(group)[0]["duration"] is None


@pytest.mark.parametrize("field,value", [("start", True), ("start", 1.5), ("end", True), ("end", 2.9),
                                       ("step", True), ("step", 1.1)])
def test_compact_cannot_silently_change_membership_by_coercing_nonintegers(field, value):
    ranges = {"start": 1, "end": 2, "step": 1}
    ranges[field] = value
    with pytest.raises(ValueError):
        arrays.compact([ranges])
