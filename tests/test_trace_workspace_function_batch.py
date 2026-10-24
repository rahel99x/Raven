"""One cumulative SQL deadline and cooperative cancellation across clock tables."""

from __future__ import annotations

from contextlib import contextmanager
from threading import Event, RLock

import pytest

from raven.tracing import workspace_session as S


class Clock:
    now = 0.0

    def monotonic(self):
        return self.now


class Index:
    def __init__(self, clock, cancel=None):
        self.clock, self.cancel = clock, cancel
        self.lock, self.deadlines, self.calls = RLock(), [], []

    @contextmanager
    def _query_budget(self, seconds=1.0):
        self.deadlines.append(self.clock.now + seconds)
        try:
            yield
        finally:
            self.deadlines.pop()

    def functions(self, **kwargs):
        with self._query_budget():
            self.calls.append((kwargs, min(self.deadlines)))
            self.clock.now += 0.6
            if self.clock.now >= min(self.deadlines):
                raise ValueError(
                    "Trace query work budget exceeded; narrow the range or sources"
                )
            if self.cancel is not None:
                self.cancel.set()
            return {
                "schema": "raven.trace-workspace-functions/v1",
                "functions": [{"name": kwargs["clock_id"]}],
                "truncated": False,
            }


def session(index):
    owner = S.WorkspaceSession.__new__(S.WorkspaceSession)
    owner.index = index
    return owner


def test_function_domains_share_deadline_and_disclose_unqueried_ranges(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(S.time, "monotonic", clock.monotonic)
    index = Index(clock)
    requests = [{"clock_id": "a"}, {"clock_id": "b"}, {"clock_id": "c"}]
    report = session(index)._functions_many(requests)
    assert [call[1] for call in index.calls] == [1.0, 1.0]
    assert [row["name"] for row in report["functions"]] == ["a"]
    assert report["truncated"] is True
    assert report["queried_ranges"] == requests[:1]
    assert report["unqueried_ranges"] == requests[1:]
    assert index.deadlines == []


def test_cancellation_between_function_domains_stops_before_next_sql(monkeypatch):
    clock, cancel = Clock(), Event()
    monkeypatch.setattr(S.time, "monotonic", clock.monotonic)
    index = Index(clock, cancel)
    with pytest.raises(InterruptedError, match="statistics cancelled"):
        session(index)._functions_many([{"clock_id": "a"}, {"clock_id": "b"}], cancel)
    assert len(index.calls) == 1 and index.deadlines == []


def test_no_completed_domain_still_returns_explicit_partial_report(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(S.time, "monotonic", clock.monotonic)
    index = Index(clock)
    original = index.functions

    def delayed(**kwargs):
        clock.now += 0.5
        return original(**kwargs)

    index.functions = delayed
    request = {"clock_id": "a", "thread": "worker", "search": "solve"}
    report = session(index)._functions_many([request])
    assert report["functions"] == [] and report["truncated"] is True
    assert report["unqueried_ranges"] == [request]
    assert report["queried_ranges"] == []
    assert report["payload_bytes"] == 0 and report["byte_limit"] == 8 << 20
    assert report["clock_id"] is None and report["filters"] == {}
    assert report["units"] == "nanoseconds" and report["limitations"] == []


def test_combined_unicode_rows_have_one_byte_budget_without_hiding_queried_ranges(
    monkeypatch,
):
    import json

    clock = Clock()
    monkeypatch.setattr(S.time, "monotonic", clock.monotonic)
    index = Index(clock)
    # Two individually valid ~4.9 MiB tables exceed the aggregate 8 MiB bound.
    # Reuse bounded source strings so the fixture itself stays small in RAM.
    row = {"name": "界" * 8192, "file": "路" * 8192}

    def functions(**kwargs):
        index.calls.append(kwargs)
        return {
            "schema": "raven.trace-workspace-functions/v1",
            "functions": [row] * kwargs["limit"],
            "truncated": False,
            "payload_bytes": 123,
            "byte_limit": 8 << 20,
        }

    index.functions = functions
    requests = [{"clock_id": "a"}, {"clock_id": "b"}]
    report = session(index)._functions_many(requests)
    amount = len(
        json.dumps(
            row, ensure_ascii=True, allow_nan=False, separators=(",", ":")
        ).encode("utf-8")
    )
    assert len(index.calls) == 2
    assert report["queried_ranges"] == requests and report["unqueried_ranges"] == []
    assert report["payload_bytes"] == len(report["functions"]) * amount
    assert report["payload_bytes"] <= 8 << 20
    assert report["payload_bytes"] + amount > 8 << 20
    assert report["payload_bytes"] != 123  # Not the first table's stale byte count.
    assert report["omitted_function_rows"] == 100 - len(report["functions"])
    assert report["truncated"] is True
