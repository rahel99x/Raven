"""Trace prefixes shared by workflow stages must retain loss/clock/identity limits."""
import json
from pathlib import Path

import pytest

from raven import derived_results, investigation_workflow as W, operations as O, ops_trace, trace_analysis as A, trace_protocol as T
from raven.trace_recorder import Recorder


def write(path, value):
    path.write_text(json.dumps(value))


def prepare(tmp_path, *, ranks=(0, 1), collective=True, coverage=None, close=True):
    recorders = []
    for rank in ranks:
        recorder = Recorder(tmp_path, job_id="123", attempt="a", background_flush=False,
                            identity_extra={"pid": 1000+rank}, coverage=coverage)
        recorders.append(recorder)
        with recorder.span("work", span_id="work"):
            pass
        if collective:
            data = {"communicator": "world", "generation": "one", "sequence": 1, "operation": "barrier", "members": [0, 1], "rank": rank}
            recorder.emit("collective", dict(data, phase="enter"))
            recorder.emit("collective", dict(data, phase="complete"))
        recorder.close() if close else recorder.flush()
    planner = {"schema": "raven.investigation-plan/v1", "budget": {"seconds": 10, "resource_seconds": 10, "trials": 1},
               "known_evidence": ["collectives"], "actions": [{"id": "next", "kind": "trace", "seconds": 1,
                 "resource_seconds": 1, "queue_seconds": 0, "decision_gain": 1, "success_probability": 1,
                 "scientific_risk": 0, "requires": ["causal", "collectives"], "model": "declared"}]}
    document = {"schema": W.SCHEMA, "identity": {"cluster": "local", "job_id": "123", "attempt": "a"},
                "sources": [{"id": "causal", "operation": "causal-delay", "path": "."},
                            {"id": "collectives", "operation": "collective-audit", "path": "."}],
                "planner": "planner.json", "diagnostics": []}
    write(tmp_path/"planner.json", planner)
    write(tmp_path/"workflow.json", document)
    return recorders, {"manifest": str(tmp_path/"workflow.json"), "action": "inspect"}, O.Context(scope={"cluster": "local"})


@pytest.fixture(autouse=True)
def fresh_cache(monkeypatch):
    cache = derived_results.ResultCache()
    monkeypatch.setattr(derived_results, "RESULTS", cache)
    monkeypatch.setattr(W, "RESULTS", cache)
    return cache


def test_two_specialized_stages_share_acquisition_and_standalone_analysis(tmp_path, monkeypatch):
    _, params, ctx = prepare(tmp_path)
    read, analyze, reads, analyses = T.read_trace, A.analyze_causality, [], []
    def counted_read(*args, **kwargs):
        reads.append(args[0])
        return read(*args, **kwargs)
    def counted_analysis(events):
        analyses.append(1)
        return analyze(events)
    monkeypatch.setattr(T, "read_trace", counted_read)
    monkeypatch.setattr(A, "analyze_causality", counted_analysis)
    result = W.run(params, ctx)
    assert len(reads) == 4  # Two acquired streams, each checked again before publication.
    assert [item["state"] for item in result["data"]["findings"]] == ["observed-causal-path", "observed-match"]
    assert [item["id"] for item in result["data"]["suggested"]] == ["next"]
    assert "clock_model" in result["data"]["findings"][0]["result"]["analysis"]
    standalone = ops_trace.run("causal-delay", {"path": str(tmp_path), "job_id": "123", "attempt": "a"}, ctx)
    assert standalone["data"]["analysis"] == result["data"]["findings"][0]["result"]["analysis"]
    assert len(analyses) == 1


@pytest.mark.parametrize("problem", ["loss", "gap", "snapshot", "false", "unknown"])
def test_closed_recording_never_overrides_incomplete_evidence(tmp_path, problem):
    coverage = {"scope": "declared events"}
    if problem == "snapshot":
        coverage["mode"] = "snapshot"
    elif problem in {"false", "unknown"}:
        coverage["complete"] = False if problem == "false" else None
    recorders, params, ctx = prepare(tmp_path, coverage=coverage)
    first = recorders[0]
    if problem == "loss":
        manifest = T.read_json(first.path)
        manifest["dropped"] = 1
        write(first.path, manifest)
    elif problem == "gap":
        journal = first.directory/"events.jsonl"
        lines = journal.read_bytes().splitlines(keepends=True)
        journal.write_bytes(b"".join(lines[:2]+lines[3:]))
    result = W.run(params, ctx)
    assert result["status"] == "warning"
    assert result["data"]["suggested"] == []
    assert all(finding["state"] == "unknown" for finding in result["data"]["findings"])
    assert all(not finding["result"]["source_complete"] for finding in result["data"]["findings"])


@pytest.mark.parametrize("collective", [False, True])
def test_no_ranks_or_missing_ranks_cannot_satisfy_prerequisites(tmp_path, collective):
    _, params, ctx = prepare(tmp_path, ranks=(0,), collective=collective)
    result = W.run(params, ctx)
    finding = result["data"]["findings"][1]
    assert finding["state"] == "unknown"
    assert finding["result"]["analysis_status"] in {"EMPTY", "INCOMPLETE"}
    assert result["data"]["suggested"] == []


def rewrite_span(recorder):
    journal = recorder.directory/"events.jsonl"
    records = [json.loads(row) for row in journal.read_text().splitlines()]
    previous = "0"*64
    for record in records:
        if record["event"]["kind"] == "span_start":
            record["event"]["data"]["name"] = "corrected"
        record["prev"] = previous
        record["sha256"] = T.event_digest(record)
        previous = record["sha256"]
    raw = b"".join(T.encoded(record)+b"\n" for record in records)
    journal.write_bytes(raw)
    manifest = T.read_json(recorder.path)
    manifest["committed_bytes"] = len(raw)
    write(recorder.path, manifest)


def test_valid_prefix_correction_rejects_pending_publication_then_recomputes(tmp_path, monkeypatch, fresh_cache):
    recorders, params, ctx = prepare(tmp_path)
    original, changed = ops_trace.analysis_report, False
    def report(*args):
        nonlocal changed
        result = original(*args)
        if not changed:
            changed = True
            rewrite_span(recorders[0])
        return result
    monkeypatch.setattr(ops_trace, "analysis_report", report)
    with pytest.raises(ValueError, match="prefix"):
        W.run(params, ctx)
    misses = fresh_cache.stats()["misses"]
    result = W.run(params, ctx)
    assert result["data"]["findings"][0]["state"] == "observed-causal-path"
    assert fresh_cache.stats()["misses"] > misses
    assert "corrected" in json.dumps(result["data"]["findings"][0]["result"]["analysis"])


def test_appending_to_live_recording_preserves_checked_prefix_and_unknown_coverage(tmp_path, monkeypatch):
    recorders, params, ctx = prepare(tmp_path, close=False)
    original, appended = ops_trace.analysis_report, False
    def report(*args):
        nonlocal appended
        result = original(*args)
        if not appended:
            appended = True
            recorders[0].emit("metric", {"value": 1})
            recorders[0].flush()
        return result
    monkeypatch.setattr(ops_trace, "analysis_report", report)
    try:
        result = W.run(params, ctx)
        assert result["data"]["suggested"] == []
        assert all(item["state"] == "unknown" for item in result["data"]["findings"])
    finally:
        for recorder in recorders:
            recorder.close()


@pytest.mark.parametrize("field,value", [("coverage", {"scope": "revised", "complete": False}), ("dropped", 1), ("warnings", ["new loss warning"])])
def test_source_coverage_correction_invalidates_review(tmp_path, field, value):
    recorders, params, ctx = prepare(tmp_path)
    _, data, _ = W.inspect(params, ctx)
    manifest = T.read_json(recorders[0].path)
    manifest[field] = value
    write(recorders[0].path, manifest)
    with pytest.raises(ValueError, match="prefix"):
        W.verify_sources(data["sources"], ctx)


def test_new_registered_stream_invalidates_workflow_generation(tmp_path):
    _, params, ctx = prepare(tmp_path)
    _, data, _ = W.inspect(params, ctx)
    with Recorder(tmp_path, job_id="123", attempt="a", background_flush=False):
        pass
    with pytest.raises(ValueError, match="streams changed"):
        W.verify_sources(data["sources"], ctx)


def test_mismatched_trace_identity_rejected_even_with_unconfigured_connection(tmp_path):
    _, params, ctx = prepare(tmp_path)
    document = O.read_json(ctx, params["manifest"])
    document["identity"]["cluster"] = "other"
    write(Path(params["manifest"]), document)
    with pytest.raises(ValueError, match="cluster differs"):
        W.run(params, O.Context())


@pytest.mark.parametrize("value", [0, 1, "false", [], {}])
def test_nonboolean_coverage_flag_cannot_claim_completeness(tmp_path, value):
    recorders, _, _ = prepare(tmp_path)
    manifest = T.read_json(recorders[0].path)
    manifest["coverage"]["complete"] = value
    write(recorders[0].path, manifest)
    with pytest.raises(ValueError, match="completeness"):
        T.read_trace(recorders[0].path)


def test_empty_matching_trace_inventory_remains_missing_and_new_registration_invalidates_it(tmp_path):
    _, params, ctx = prepare(tmp_path)
    document = O.read_json(ctx, params["manifest"])
    document["identity"]["attempt"] = "new-attempt"
    write(Path(params["manifest"]), document)
    result = W.run(params, ctx)
    assert result["data"]["findings"] == []
    assert {item["id"] for item in result["data"]["missing"]} == {"causal", "collectives"}
    assert result["data"]["suggested"] == []
    with Recorder(tmp_path, job_id="123", attempt="new-attempt", background_flush=False):
        pass
    with pytest.raises(ValueError, match="streams changed"):
        W.verify_sources(result["data"]["sources"], ctx)
