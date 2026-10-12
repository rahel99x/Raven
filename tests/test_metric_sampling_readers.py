"""Polling requests remain distinct from file producers and source identity."""
from copy import deepcopy

import pytest

from raven import metric_sampling as rates, refresh_rate
from raven.config import Config
from raven.model import Job
from raven.research import ResearchHub


def context(*, jid="7", start="start", generation=0, binding=None, view="experiment"):
    return {"jid": jid, "job": Job(jid, "train", "gpu", "RUNNING", submit="submit", start=start),
            "generation": generation, "binding": binding, "view": view}


def key(*, jid="7", name="loss", attempt="scheduler:submit|start", generation=0, root=None, run=None):
    return ("reported-metric", jid, name, "/project/metrics.jsonl", attempt, root, run, generation)


@pytest.mark.parametrize("value,unicode_label,ascii_label", [
    (0, "0s", "0s"), (2, "2s", "2s"), (86400, "86400s", "86400s"), (.5, "500ms", "500ms"),
    (.0025, "2.5ms", "2.5ms"), (.001, "1ms", "1ms"),
    (.00025, "250µs", "250us"), (.000001, "1µs", "1us"),
])
def test_interval_labels_choose_real_units(value, unicode_label, ascii_label):
    assert rates.format_interval(value) == unicode_label
    assert rates.format_interval(value, ascii_=True) == ascii_label


@pytest.mark.parametrize("value", [None, True, "5", -1, float("nan"), float("inf"), 10**1000])
def test_invalid_interval_labels_do_not_crash_the_control_row(value):
    assert rates.format_interval(value) == "?"


@pytest.mark.parametrize("mutation", [
    {"jid": "8"}, {"start": "new-start"}, {"generation": 1},
    {"binding": {"project_root": "/other", "run_id": "other", "attempt": 2}},
    {"view": "evidence"},
])
def test_file_reader_rate_does_not_transfer_to_other_job_attempt_source_or_view(mutation):
    hub = ResearchHub(Config())
    try:
        hub.set_metric_sampling({key(): 100})
        assert hub.refresh_interval(context()) == .5
        assert hub.refresh_interval(context(**mutation)) == 5
        assert hub.refresh_interval() == 5
    finally:
        hub.close()


def test_bound_file_request_matches_exact_run_and_reset_preserves_other_demands():
    hub = ResearchHub(Config())
    try:
        binding = {"project_root": "/project", "run_id": "run-a", "attempt": 3}
        ctx = context(binding=binding)
        loss = key(attempt=3, root="/project", run="run-a")
        accuracy = key(name="accuracy", attempt=3, root="/project", run="run-a")
        hub.set_metric_sampling({loss: 20, accuracy: 5})
        assert hub.refresh_interval(ctx) == pytest.approx(refresh_rate.poll_interval(20, maximum=100))
        hub.set_metric_sampling({accuracy: 5})
        assert hub.refresh_interval(ctx) == pytest.approx(refresh_rate.poll_interval(5, maximum=100))
        assert hub.refresh_interval(context(binding=dict(binding, run_id="run-b"))) == 5
        hub.set_metric_sampling({})
        assert hub.refresh_interval(ctx) == 5
    finally:
        hub.close()


def test_changing_report_source_invalidates_old_demand_without_changing_base_interval():
    hub = ResearchHub(Config())
    try:
        hub.set_metric_sampling({key(): 100})
        assert hub.refresh_interval(context()) == .5
        hub.configure(metrics_file="/new/metrics.jsonl")
        assert hub.metric_sampling == {}
        assert hub.refresh_interval(context(generation=hub.generation)) == 5
        assert hub.interval == 5
    finally:
        hub.close()


def test_faster_read_requests_gate_real_reads_and_do_not_rewrite_measurements(monkeypatch):
    now = [100.0]
    monkeypatch.setattr("raven.research.time.monotonic", lambda: now[0])
    hub = ResearchHub(Config())
    observed = {"series": {"loss": [{"t": 10.0, "value": .5}, {"t": 70.0, "value": .2}]}, "status": "ok"}
    original = deepcopy(observed)
    calls = []
    monkeypatch.setattr(hub, "_read", lambda ctx: (calls.append(ctx["jid"]), deepcopy(observed))[1])
    try:
        ctx = context()
        assert hub.request(ctx, wait=True) == original
        now[0] += 1
        assert hub.request(ctx, wait=True) == original and calls == ["7"]
        hub.set_metric_sampling({key(): 100})
        assert hub.request(ctx, wait=True) == original and calls == ["7", "7"]
        now[0] += .1
        hub.request(ctx, wait=True)
        assert calls == ["7", "7"]
        now[0] += .4
        hub.request(ctx, wait=True)
        assert calls == ["7", "7", "7"]
        hub.set_metric_sampling({})
        now[0] += 1
        hub.request(ctx, wait=True)
        assert calls == ["7", "7", "7"]
        assert observed == original
    finally:
        hub.close()
