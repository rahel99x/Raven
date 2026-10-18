"""Exact exported evidence, crash/replay cutoffs, and hostile read-only SQL."""
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import signal
import shutil
import sqlite3
import subprocess
import sys
import threading
import time

import pytest

from raven import data_export as D, evidence_index as I, reporting
from raven import operations as O, ops_evidence_queries as Q


@pytest.fixture
def capture(tmp_path):
    project = tmp_path / "project with spaces"
    project.mkdir()
    (project / "job.sh").write_text("#!/bin/sh\ntrue\n")
    run = reporting.begin_run(project, "attempt-one", name="experiment", script="job.sh", job_id="42_7", attempt=2)
    (run / "metrics.jsonl").write_text("\n".join(json.dumps({"t": stamp, "metrics": {"loss": value, "counter": stamp * 2},
                         "units": {"loss": "ratio", "counter": "count"}}) for stamp, value in [(1, .2), (2.5, .1), (5, None)]) + "\n")
    result = D.export_data({"project": str(project), "runs": [run.name], "include": ["application"]})
    assert result["status"] == "complete", result
    return str(project), Path(result["outputs"][0]), tmp_path / "private-index"


def test_ingestion_roundtrip_provenance_idempotency_and_rebuild(capture, tmp_path):
    project, bundle, index = capture
    result = I.ingest(index, bundle, project)
    assert result["inserted"] == 6
    assert I.ingest(index, bundle, project)["inserted"] == 0
    rows = I.query(index, "SELECT timestamp,value,unit,status FROM measurements WHERE metric='loss' ORDER BY timestamp")["rows"]
    assert rows == [[1., .2, "ratio", "observed"], [2.5, .1, "ratio", "observed"], [5., None, "ratio", "unavailable"]]
    provenance = I.query(index, "SELECT source_row,path,sha256,identity_json FROM measurements JOIN sources ON source_id=sources.id")["rows"]
    assert len(provenance) == 6 and all(row[1] == "application-metrics.csv" and len(row[2]) == 64 for row in provenance)
    assert all(json.loads(row[3])["attempt"] == "2" for row in provenance)
    rebuilt = tmp_path / "rebuilt"
    I.ingest(rebuilt, bundle, project)
    assert I.query(rebuilt, "SELECT timestamp,value,unit,status FROM measurements WHERE metric='loss' ORDER BY timestamp")["rows"] == rows


def test_checksum_failure_transaction_rolls_back_and_retry_succeeds(capture):
    project, bundle, index = capture
    source = bundle / "application-metrics.csv"
    original = source.read_bytes()
    source.write_bytes(original + b"unreviewed change")
    with pytest.raises(ValueError, match="checksum"):
        I.ingest(index, bundle, project)
    assert I.query(index, "SELECT count(*) FROM captures")["rows"] == [[0]]
    source.write_bytes(original)
    assert I.ingest(index, bundle, project)["inserted"] == 6


@pytest.mark.parametrize("sql", ["DELETE FROM measurements", "ATTACH DATABASE ':memory:' AS other", "PRAGMA writable_schema=ON",
    "SELECT load_extension('anything')", "SELECT randomblob(999999999)", "SELECT * FROM measurements; DELETE FROM sources",
    "CREATE TABLE bad (x)", "VACUUM INTO 'unexpected.db'"])
def test_sql_authorizer_blocks_mutation_and_unbounded_functions(capture, sql):
    project, bundle, index = capture
    I.ingest(index, bundle, project)
    with pytest.raises(ValueError, match="rejected"):
        I.query(index, sql)
    assert I.query(index, "SELECT count(*) FROM measurements")["rows"] == [[6]]


def test_budgets_partial_results_and_cancellation(capture):
    project, bundle, index = capture
    I.ingest(index, bundle, project)
    result = I.query(index, "SELECT metric FROM measurements", max_rows=2)
    assert result["status"] == "partial" and len(result["rows"]) == 2 and "row" in result["reason"]
    result = I.query(index, "SELECT attributes_json FROM measurements", max_bytes=10)
    assert result["status"] == "partial" and result["rows"] == [] and "byte" in result["reason"]
    result = I.query(index, "WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM n) SELECT sum(x) FROM n", vm_steps=2000)
    assert result["status"] == "partial" and "VM-step" in result["reason"]
    cancel = threading.Event(); cancel.set()
    assert I.query(index, cancel=cancel)["reason"] == "cancelled"


def test_rates_preserve_unknowns_reset_gap_and_zero_interval():
    rows = I.rates([(1, 5), (3, 9), (3, 12), (4, 1), (20, 9), (21, None), (22, 13), (24, 17)], max_gap=5, unit="B")
    assert [row["value"] for row in rows] == [None, 2., None, None, None, None, None, 2.]
    assert [rows[i]["reason"] for i in (2, 3, 4, 5)] == ["non-increasing timestamp", "counter reset", "gap exceeds maximum", "missing observation"]
    assert {row["unit"] for row in rows} == {"B/s"}


def test_small_vm_program_cannot_allocate_an_unbounded_string(capture):
    project, bundle, index = capture
    I.ingest(index, bundle, project)
    with pytest.raises(ValueError, match="rejected|memory|budget"):
        I.query(index, "WITH RECURSIVE strings(n,s) AS (SELECT 1,'x' UNION ALL SELECT n+1,s||s FROM strings WHERE n<32) SELECT s FROM strings ORDER BY n DESC LIMIT 1")


def test_attempt_log_and_state_indexes_preserve_evidence_without_inferred_time(capture):
    project, _, index = capture
    run = Path(project) / "runs/attempt-one"
    (run / "logs/stdout.log").write_bytes("hello λ\nsecond\r\n".encode())
    reporting.finish_run(run, state="COMPLETED", runtime_seconds=2, exit_code=0)
    result = D.export_data({"project": project, "runs": [run.name], "include": ["summary", "logs"],
                            "destination": {"mode": "absolute", "path": str(Path(project) / "all-evidence")}})
    assert result["status"] == "complete", result
    I.ingest(index, result["outputs"][0], project)
    logs = I.query(index, "SELECT line,byte_offset,text FROM log_lines ORDER BY source_id,line")["rows"]
    assert logs == [[1, 0, "hello λ"], [2, len("hello λ\n".encode()), "second"]]
    states = I.query(index, "SELECT kind,timestamp_text,previous_state,state FROM transitions")["rows"]
    assert states and all(row[0] == "state-snapshot" and row[1:3] == [None, None] for row in states)
    assert any(row[3] == "COMPLETED" for row in states)
    assert len(I.query(index, view="attempts")["rows"]) == 1


def test_series_transforms_keep_capture_scope_and_explicit_gap_semantics(capture):
    project, bundle, index = capture
    I.ingest(index, bundle, project)
    source, attempt = I.query(index, "SELECT source_id,attempt_key FROM measurements LIMIT 1")["rows"][0]
    selected = I.series(index, source_id=source, attempt=attempt, metric="counter")
    assert [row["value"] for row in I.transform(selected, operation="rate", max_gap=5)["samples"]] == [None, 2., 2.]
    assert [row["value"] for row in I.transform(selected, operation="delta", max_gap=5)["samples"]] == [None, 3., 5.]
    window = I.transform(selected, operation="window", max_gap=5, window_seconds=3)["samples"]
    assert [row["value"] for row in window] == [2., 3.5, 7.5]
    assert [row["count"] for row in window] == [1, 2, 2]
    # A second retained capture has identical observations, but a different
    # immutable inventory/publication. Selecting the original source stays exact.
    another = D.export_data({"project": project, "runs": ["attempt-one"], "include": ["application"],
                            "destination": {"mode": "absolute", "path": str(Path(project) / "second-export")}})
    I.ingest(index, another["outputs"][0], project)
    assert len(I.query(index, "SELECT * FROM measurements")["rows"]) == 12
    assert I.series(index, source_id=source, attempt=attempt, metric="counter") == selected


def test_mixed_rank_streams_require_explicit_selection(capture):
    project, _, index = capture
    run = Path(project) / "runs/attempt-one"
    (run / "metrics.jsonl").write_text("\n".join(json.dumps({"t": t, "rank": rank, "metrics": {"counter": t + rank},
        "units": {"counter": "count"}}) for t in (1, 2) for rank in (0, 1)) + "\n")
    result = D.export_data({"project": project, "runs": [run.name], "include": ["application"],
                            "destination": {"mode": "absolute", "path": str(Path(project) / "rank-export")}})
    assert result["status"] == "complete", result
    I.ingest(index, result["outputs"][0], project)
    source, attempt = I.query(index, "SELECT source_id,attempt_key FROM measurements LIMIT 1")["rows"][0]
    with pytest.raises(ValueError, match="exactly one stream"):
        I.series(index, source_id=source, attempt=attempt, metric="counter")
    one = I.series(index, source_id=source, attempt=attempt, metric="counter", selector={"rank": "0"})
    assert [row[1] for row in one["samples"]] == [1., 2.]


def test_project_inventory_cannot_leak_another_projects_capture(capture):
    project, bundle, index = capture
    manifest = json.loads((bundle / "manifest.json").read_text())
    other = json.loads(json.dumps(manifest["attempts"][0]))
    other["project"] = project + "/unrelated"
    other["identity"]["project"] = other["project"]
    manifest["attempts"].append(other)
    raw = json.dumps(manifest).encode()
    (bundle / "manifest.json").write_bytes(raw)
    marker = json.loads((bundle / "COMPLETE.json").read_text())
    marker["manifest_sha256"] = hashlib.sha256(raw).hexdigest()
    (bundle / "COMPLETE.json").write_text(json.dumps(marker))
    with pytest.raises(ValueError, match="only that project"):
        I.ingest(index, bundle, project)
    assert not index.exists()


def test_mid_query_cancellation_reaps_compatibility_child(capture, monkeypatch):
    project, bundle, index = capture
    I.ingest(index, bundle, project)
    children = []
    popen = I.subprocess.Popen
    def tracked(*args, **kwargs):
        process = popen(*args, **kwargs); children.append(process); return process
    monkeypatch.setattr(I.subprocess, "Popen", tracked)
    cancel = threading.Event()
    timer = threading.Timer(.002, cancel.set); timer.start()
    try:
        result = I.query(index, "WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM n) SELECT sum(x) FROM n",
                         cancel=cancel, vm_steps=2000000, timeout=5)
    finally:
        timer.join()
    assert result["status"] == "partial" and result["reason"] == "cancelled"
    assert all(child.poll() is not None for child in children)


@pytest.mark.parametrize("alteration", [
    "ALTER TABLE metadata RENAME TO hidden_metadata; CREATE VIEW metadata AS WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM n) SELECT 'schema' AS key,sum(x) AS value FROM n;",
    "CREATE TRIGGER unexpected AFTER INSERT ON captures BEGIN DELETE FROM measurements; END;",
])
def test_existing_writer_schema_cannot_execute_injected_views_or_triggers(capture, alteration):
    project, bundle, index = capture
    I.ingest(index, bundle, project)
    with sqlite3.connect(index / I.DATABASE) as connection:
        connection.executescript(alteration)
    with pytest.raises(ValueError, match="unrecognized schema"):
        I.ingest(index, bundle, project)


def test_project_authority_checked_on_the_actual_query_connection(capture):
    project, bundle, index = capture
    I.ingest(index, bundle, project)
    assert I.query(index, "SELECT count(*) FROM attempts", expected_project=project)["rows"] == [[1]]
    with pytest.raises(ValueError, match="different authorized project"):
        I.query(index, "SELECT * FROM captures", expected_project=project + "-other")


def test_user_path_review_index_query_and_sqlite_export(capture, tmp_path):
    project, bundle, index = capture
    ctx = O.Context(scope={"connection": "local", "user": "test"})
    params = O.parameters("evidence-query", {"action": "index", "index": str(index), "bundle": str(bundle), "project": project})
    preview = Q.run("evidence-query", params, ctx)
    assert not index.exists()
    assert Q.apply("evidence-query", preview["plan"], ctx)["data"]["inserted"] == 6
    query = Q.run("evidence-query", {**params, "action": "query", "view": "application"}, ctx)
    assert len(query["data"]["rows"]) == 6
    export = Q.run("evidence-query", {**params, "action": "export", "destination": str(tmp_path / "portable")}, ctx)
    result = Q.apply("evidence-query", export["plan"], ctx)
    assert Path(result["data"]["destination"], I.DATABASE).is_file()
    assert I.query(tmp_path / "portable", "SELECT count(*) FROM measurements")["rows"] == [[6]]
    with pytest.raises(ValueError, match="new directory"):
        Q.apply("evidence-query", export["plan"], ctx)


def test_changed_project_and_symlink_index_refused(capture, tmp_path):
    project, bundle, index = capture
    with pytest.raises(ValueError, match="exact project"):
        I.ingest(index, bundle, project + "other")
    I.ingest(index, bundle, project)
    link = tmp_path / "shortcut"
    link.symlink_to(index, target_is_directory=True)
    with pytest.raises((ValueError, OSError)):
        I.query(link)


def test_large_integer_is_retained_exactly_not_rounded(capture):
    project, bundle, index = capture
    # Re-export authoritative raw evidence, rather than doctor a signed inventory.
    run = Path(project) / "runs" / "attempt-one"
    (run / "metrics.jsonl").write_text(json.dumps({"t": 1, "metrics": {"large": 9007199254740993}}) + "\n")
    result = D.export_data({"project": project, "runs": [run.name], "include": ["application"],
                            "destination": {"mode": "absolute", "path": str(Path(project) / "new-export")}})
    I.ingest(index, result["outputs"][0], project)
    row = I.query(index, "SELECT value,value_text,numeric_status FROM measurements")["rows"][0]
    assert row[0] is None and row[1] == "9007199254740993" and "not exactly representable" in row[2]


def _publish_csv_fixture(bundle, relative, rows):
    """Publish a checksum-consistent external CSV producer fixture."""
    text = io.StringIO(newline="")
    writer = csv.DictWriter(text, fieldnames=list(rows[0]))
    writer.writeheader()
    writer.writerows(rows)
    raw = text.getvalue().encode()
    (bundle / relative).write_bytes(raw)
    manifest_path = bundle / "manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    for item in manifest["files"]:
        if item["path"] == relative:
            item.update(bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())
    manifest_raw = json.dumps(manifest).encode()
    manifest_path.write_bytes(manifest_raw)
    complete_path = bundle / "COMPLETE.json"
    complete = json.loads(complete_path.read_bytes())
    complete["manifest_sha256"] = hashlib.sha256(manifest_raw).hexdigest()
    complete_path.write_text(json.dumps(complete))


@pytest.mark.parametrize("lexeme,expected,status", [
    ("9007199254740993.0", None, "integer not exactly representable"),
    ("9.007199254740993e15", None, "integer not exactly representable"),
    ("-9007199254740993.000", None, "integer not exactly representable"),
    ("1e10000", None, "integer not exactly representable"),
    ("9" * 5000, None, "integer not exactly representable"),
    ("9007199254740994.0", 9007199254740994., "binary64"),
    ("1e-10000", None, "outside binary64 range"),
    ("0.125", .125, "binary64"),
], ids=["decimal-inexact", "exponent-inexact", "negative-inexact", "large-exponent",
        "long-integer", "exact-integer", "underflow", "fraction"])
def test_published_decimal_lexemes_preserve_fidelity(capture, lexeme, expected, status):
    project, bundle, index = capture
    rows = list(csv.DictReader(io.StringIO((bundle / "application-metrics.csv").read_text())))[:1]
    rows[0]["value"] = lexeme
    _publish_csv_fixture(bundle, "application-metrics.csv", rows)
    assert I.ingest(index, bundle, project)["inserted"] == 1
    actual = I.query(index, "SELECT value,value_text,numeric_status FROM measurements")["rows"][0]
    assert actual[:2] == [expected, lexeme]
    assert status in actual[2]
    source, attempt, metric = I.query(index, "SELECT source_id,attempt_key,metric FROM measurements")["rows"][0]
    values = I.series(index, source_id=source, attempt=attempt, metric=metric)
    assert values["samples"][0][1] == expected


def test_wrong_project_csv_rolls_back_without_losing_previous_capture(capture, tmp_path):
    project, bundle, index = capture
    I.ingest(index, bundle, project)
    inconsistent = tmp_path / "different-publication"
    shutil.copytree(bundle, inconsistent)
    rows = list(csv.DictReader(io.StringIO((inconsistent / "application-metrics.csv").read_text())))
    rows[-1]["project"] = project + "/different-project"
    _publish_csv_fixture(inconsistent, "application-metrics.csv", rows)
    with pytest.raises(ValueError, match="project differs"):
        I.ingest(index, inconsistent, project)
    assert I.query(index, "SELECT count(*) FROM captures")["rows"] == [[1]]
    assert I.query(index, "SELECT count(*) FROM measurements")["rows"] == [[6]]


def test_csv_attempt_must_match_its_published_source_prefix(capture):
    project, _, index = capture
    second = reporting.begin_run(project, "attempt-two", name="second", script="job.sh", job_id="43", attempt=1)
    (second / "metrics.jsonl").write_text(json.dumps({"t": 1, "metrics": {"loss": .5}}) + "\n")
    result = D.export_data({"project": project, "runs": ["attempt-one", "attempt-two"], "include": ["application"],
        "destination": {"mode": "absolute", "path": str(Path(project) / "two-attempts")}})
    assert result["status"] == "complete", result
    bundle = Path(result["outputs"][0])
    manifest = json.loads((bundle / "manifest.json").read_bytes())
    sources = [item["path"] for item in manifest["files"] if item.get("family") == "application"]
    assert len(sources) == 2
    rows = list(csv.DictReader(io.StringIO((bundle / sources[0]).read_text())))
    other = next(csv.DictReader(io.StringIO((bundle / sources[1]).read_text())))
    for key in I.IDENTITY:
        rows[0][key] = other[key]
    _publish_csv_fixture(bundle, sources[0], rows)
    with pytest.raises(ValueError, match="identity differs"):
        I.ingest(index, bundle, project)
    assert I.query(index, "SELECT count(*) FROM captures")["rows"] == [[0]]


def test_actual_process_death_mid_transaction_can_replay_once(capture, tmp_path):
    project, bundle, index = capture
    marker = tmp_path / "transaction-in-flight"
    child = r'''
import os, signal, sys
from pathlib import Path
from raven import evidence_index as I
original = I._rows
def paused(*args):
    for row in original(*args):
        yield row
        Path(sys.argv[4]).write_text('inserted')
        os.kill(os.getpid(), signal.SIGSTOP)
I._rows = paused
I.ingest(sys.argv[1], sys.argv[2], sys.argv[3])
'''
    process = subprocess.Popen([sys.executable, "-c", child, str(index), str(bundle), project, str(marker)])
    try:
        deadline = time.monotonic() + 5
        while not marker.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(.01)
        assert marker.exists()
        process.kill(); process.wait(timeout=2)
    finally:
        if process.poll() is None:
            process.kill(); process.wait(timeout=2)
    assert I.ingest(index, bundle, project)["inserted"] == 6
    assert I.ingest(index, bundle, project)["status"] == "already-indexed"
    assert I.query(index, "SELECT count(*) FROM captures")["rows"] == [[1]]
    assert I.query(index, "SELECT count(*) FROM measurements")["rows"] == [[6]]


def test_real_worker_review_and_index_update_retain_admission(capture, tmp_path):
    from raven.raven_worker_client import WorkerClient
    from raven import raven_execution as R
    project, bundle, index = capture
    config = {"site_kind": "desktop", "desktop_explicit": True, "context": {},
              "resources": {"memory_mib": 512, "cpus": 1, "max_tasks": 1, "wall_seconds": 10}}
    with WorkerClient(config) as worker:
        deadline = time.monotonic() + 5
        while not worker.ready and time.monotonic() < deadline:
            worker.poll(); time.sleep(.01)
        assert worker.ready, worker.error
        router = R.ExecutionRouter({"managed": True, "mode": "heavy", "site": "desktop"}, lambda: worker)
        ctx = O.Context(execution=router, scope={"cluster": "fixture", "user": "owner"}, state_dir=str(tmp_path / "state"))
        preview = O.run("evidence-query", {"action": "index", "index": str(index), "bundle": str(bundle), "project": project}, ctx)
        assert preview["plan"]["raven_worker"] and not index.exists()
        result = O.apply("evidence-query", preview["plan"], ctx)
        assert result["data"]["inserted"] == 6
        assert result["execution_receipt"]["operation"] == "evidence-index-update"
        query = O.run("evidence-query", {"index": str(index), "view": "application"}, ctx)
        assert len(query["data"]["rows"]) == 6
        with pytest.raises(ValueError, match="already attempted"):
            O.apply("evidence-query", preview["plan"], ctx)
