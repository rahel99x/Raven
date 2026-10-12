"""Advisor reuse follows exact accounting facts, corrections and window expiry."""
from dataclasses import asdict, replace

import pytest

from raven import advisor
from raven.model import Finished, Job, Live, stamp


GIB = 1024 ** 3


def records(count=12):
    states = ("COMPLETED", "OUT_OF_MEMORY", "TIMEOUT", "FAILED", "CANCELLED")
    return [Finished(str(index), name=f"study-{index % 3}", state=states[index % len(states)],
                     end=f"2026-10-08T12:{index % 60:02}:00", elapsed="00:20:00", cpus=8,
                     cpu_time=1200., req_mem=32 * GIB, rss=(4 + index % 4) * GIB, limit="02:00:00")
            for index in range(count)]


def facts(advice):
    return [asdict(item) for item in advice]


def observed_calculations(monkeypatch):
    calls, original = [], advisor.advise_names

    def calculate(finished):
        calls.append(tuple((item.id, item.name, item.state) for item in finished))
        return original(finished)

    monkeypatch.setattr(advisor, "advise_names", calculate)
    return calls, original


def test_warm_history_reuses_calculation_and_returns_independent_advice(monkeypatch):
    finished = records()
    calls, original = observed_calculations(monkeypatch)
    cache = advisor.HistoryAdviceCache()
    first = cache.names(finished)
    expected = facts(original(finished))
    assert facts(first) == expected
    for _ in range(20):
        later = cache.names(finished)
        assert facts(later) == expected
        assert later is not first
        assert all(new is not old and new.notes is not old.notes for old, new in zip(first, later))
    assert len(calls) == 1
    first[0].notes.append("caller annotation")
    first[0].mem_suggest = "modified"
    first.clear()
    assert facts(cache.names(finished)) == expected


@pytest.mark.parametrize("field,value", [
    ("id", "corrected-id"), ("name", "corrected-name"), ("state", "OUT_OF_MEMORY"),
    ("end", "2026-10-08T15:00:00"), ("elapsed", "01:00:00"), ("cpus", 32),
    ("cpu_time", 9000.), ("req_mem", 64 * GIB), ("rss", 16 * GIB), ("limit", "00:25:00"),
])
def test_in_place_accounting_correction_invalidates_every_consumed_field(monkeypatch, field, value):
    finished = records()
    calls, original = observed_calculations(monkeypatch)
    cache = advisor.HistoryAdviceCache()
    cache.names(finished)
    setattr(finished[0], field, value)
    assert facts(cache.names(finished)) == facts(original(finished))
    assert len(calls) == 2
    assert facts(cache.names(finished)) == facts(original(finished))
    assert len(calls) == 2


@pytest.mark.parametrize("setting,value", [("MEM_HEADROOM", 1.8), ("TIME_HEADROOM", 1.9), ("CPU_TARGET", .4)])
def test_suggestion_settings_invalidate_cached_calculation(monkeypatch, setting, value):
    finished = records()
    calls, original = observed_calculations(monkeypatch)
    cache = advisor.HistoryAdviceCache()
    cache.names(finished)
    monkeypatch.setattr(advisor, setting, value)
    assert facts(cache.names(finished)) == facts(original(finished))
    assert len(calls) == 2


def test_record_order_preserves_equal_time_and_equal_waste_tie_choices(monkeypatch):
    earlier = Finished("first", "same-name", "COMPLETED", elapsed="00:20:00", cpus=4,
                       cpu_time=600, req_mem=32 * GIB, rss=4 * GIB,
                       end="2026-10-08T12:00:00", limit="02:00:00")
    later = replace(earlier, id="second", req_mem=64 * GIB, limit="04:00:00")
    cache = advisor.HistoryAdviceCache()
    calls, original = observed_calculations(monkeypatch)
    assert facts(cache.names([earlier, later])) == facts(original([earlier, later]))
    assert cache.names([earlier, later])[0].mem_req == 32 * GIB
    assert facts(cache.names([later, earlier])) == facts(original([later, earlier]))
    assert cache.names([later, earlier])[0].mem_req == 64 * GIB
    assert len(calls) == 2
    distinct = [replace(earlier, name="first-name"), replace(earlier, name="second-name")]
    assert [item.name for item in cache.names(distinct)] == ["first-name", "second-name"]
    assert [item.name for item in cache.names(list(reversed(distinct)))] == ["second-name", "first-name"]


def test_equal_value_new_snapshots_reuse_names_but_groups_return_current_objects(monkeypatch):
    finished = records()
    current = [replace(record) for record in finished]
    calls, _ = observed_calculations(monkeypatch)
    cache = advisor.HistoryAdviceCache()
    cache.names(finished)
    groups = cache.groups(finished)
    cache.names(current)
    new_groups = cache.groups(current)
    assert len(calls) == 1
    assert groups == new_groups
    for old, new in zip(finished, current):
        assert any(record is new for record in new_groups[new.name])
        assert all(record is not old for record in new_groups[new.name])
    new_groups.clear()
    assert cache.groups(current)


def test_grouping_does_not_eagerly_calculate_name_advice(monkeypatch):
    calls, original = observed_calculations(monkeypatch)
    finished = records()
    cache = advisor.HistoryAdviceCache()
    first = cache.groups(finished)
    second = cache.groups(finished)
    assert not calls
    assert first == second and first is not second
    assert facts(cache.names(finished)) == facts(original(finished))
    assert len(calls) == 1 and cache.entry_count == 1


def test_running_advice_uses_only_matching_group_and_keeps_exact_result():
    finished = records(1200)
    cache = advisor.HistoryAdviceCache()
    groups = cache.groups(finished)
    for index in range(8):
        job = Job(str(5000 + index), f"study-{index % 3}", "main", "RUNNING",
                  elapsed="00:31:00", limit="03:00:00", cpus=8, mem_req="32G")
        live = Live(rss=3 * GIB, avg=.2)
        full = advisor.advise_running(job, live, [], finished)
        scoped = advisor.advise_running(job, live, [], groups.get(job.name, ()))
        assert asdict(scoped) == asdict(full)
        assert len(groups[job.name]) == 400


def test_two_entry_lru_and_total_record_budget_are_bounded(monkeypatch):
    calls, _ = observed_calculations(monkeypatch)
    cache = advisor.HistoryAdviceCache(max_entries=9, max_records=6)
    first, second, third = records(2), records(3), records(4)
    cache.names(first)
    cache.names(second)
    assert (cache.entry_count, cache.record_count) == (2, 5)
    cache.names(first)  # Make the smaller entry most recently used.
    cache.names(third)
    assert (cache.entry_count, cache.record_count) == (2, 6)
    cache.names(first)
    assert len(calls) == 3
    cache.names(second)
    assert len(calls) == 4
    assert cache.entry_count <= 2 and cache.record_count <= 6
    assert advisor.HistoryAdviceCache(max_records=90000).max_records == 50000


def test_oversized_history_bypasses_retention_without_evicting_warm_small_window(monkeypatch):
    calls, original = observed_calculations(monkeypatch)
    cache = advisor.HistoryAdviceCache(max_records=4)
    small, large = records(3), records(6)
    cache.names(small)
    expected = facts(original(large))
    assert facts(cache.names(large)) == expected
    assert facts(cache.names(large)) == expected
    assert (cache.entry_count, cache.record_count) == (1, 3)
    assert sum(len(values) for values in cache.groups(large).values()) == len(large)
    cache.names(small)
    assert len(calls) == 3


def test_disabled_cache_keeps_uncached_semantics_and_no_records(monkeypatch):
    finished = records()
    calls, original = observed_calculations(monkeypatch)
    cache = advisor.HistoryAdviceCache(max_entries=0)
    assert facts(cache.names(finished)) == facts(original(finished))
    assert facts(cache.names(finished)) == facts(original(finished))
    assert len(calls) == 2
    assert sum(map(len, cache.groups(finished).values())) == len(finished)
    assert cache.entry_count == cache.record_count == 0


def test_captured_values_are_not_replaced_by_a_concurrent_in_place_correction(monkeypatch):
    finished = records()
    expected = facts(advisor.advise_names(finished))
    original = advisor.advise_names

    def corrected_after_capture(captured):
        finished[0].rss = 100 * GIB
        assert captured[0] is not finished[0]
        return original(captured)

    monkeypatch.setattr(advisor, "advise_names", corrected_after_capture)
    cache = advisor.HistoryAdviceCache()
    assert facts(cache.names(finished)) == expected
    assert facts(cache.names(finished)) == facts(original(finished))


def test_accounting_window_expiration_is_checked_before_cache_lookup(monkeypatch):
    start = stamp("2026-10-09T12:00:00")
    cutoff = Finished("cutoff", "expires", "COMPLETED", end="2026-10-08T12:00:00",
                      elapsed="00:20:00", cpus=8, cpu_time=1200, rss=4 * GIB)
    recent = replace(cutoff, id="recent", name="retained", end="2026-10-09T11:00:00")
    unavailable = replace(cutoff, id="unknown", name="unknown end", end="Unknown")
    finished = [cutoff, recent, unavailable]
    calls, original = observed_calculations(monkeypatch)
    cache = advisor.HistoryAdviceCache()

    def window(now):
        return [record for record in finished if (stamp(record.end) or now) >= now - 86400]

    first = window(start)
    assert facts(cache.names(first)) == facts(original(first))
    assert {item.name for item in cache.names(first)} == {"expires", "retained", "unknown end"}
    current = window(start + 1)
    assert facts(cache.names(current)) == facts(original(current))
    assert {item.name for item in cache.names(current)} == {"retained", "unknown end"}
    assert len(calls) == 2


def test_append_and_removal_refresh_history_membership(monkeypatch):
    finished = records()
    calls, original = observed_calculations(monkeypatch)
    cache = advisor.HistoryAdviceCache()
    cache.names(finished)
    finished.append(replace(finished[0], id="new", name="new-name"))
    assert facts(cache.names(finished)) == facts(original(finished))
    finished.pop(3)
    assert facts(cache.names(finished)) == facts(original(finished))
    assert len(calls) == 3
