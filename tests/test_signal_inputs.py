"""Existing reporting export -> indexed exact stream -> managed analysis."""
import json
from pathlib import Path
import subprocess
import sys
import time

import pytest

from raven import data_export as D, evidence_index as I, operations as O, reporting
from raven.signal_inputs import from_index


@pytest.fixture
def stream(tmp_path):
    project = tmp_path / "project"; project.mkdir()
    (project / "job.sh").write_text("#!/bin/sh\ntrue\n")
    run = reporting.begin_run(project, "signal-run", name="signals", script="job.sh", job_id="42", attempt=2)
    (run / "metrics.jsonl").write_text("\n".join(json.dumps({"t": t, "metrics": {"loss": 80 if t < 40 else 5}, "units": {"loss": "%"}}) for t in range(48)) + "\n")
    result = D.export_data({"project": str(project), "runs": [run.name], "include": ["application"]})
    assert result["status"] == "complete"
    index = tmp_path / "index"
    I.ingest(index, result["outputs"][0], str(project))
    source, attempt = I.query(index, "SELECT source_id,attempt_key FROM measurements LIMIT 1")["rows"][0]
    return index, source, attempt


def test_existing_export_conversion_keeps_provenance_and_unknowns(stream):
    index, source, attempt = stream
    result = from_index(index, source_id=source, attempt=attempt, metric="loss")
    series = result["series"][0]
    assert series["identity"]["attempt"] == attempt
    assert series["source"]["source_rows"] == list(range(2, 50))
    assert series["samples"][42] == {"t": 42., "value": 5.}
    assert series["source"]["metric"] == "loss"
    assert len(series["source"]["sha256"]) == 64
    with pytest.raises(ValueError, match="stream"):
        from_index(index, source_id=source, attempt=attempt, metric="loss", selector={"rank": "not-this-stream"})


def test_converter_cli_produces_valid_json_without_writing_index(stream):
    index, source, attempt = stream
    before = (index / I.DATABASE).read_bytes()
    result = subprocess.run([sys.executable, "scripts/prepare_signal_series.py", str(index), "--source-id", str(source), "--attempt", attempt, "--metric", "loss"], capture_output=True, text=True, check=True)
    assert len(json.loads(result.stdout)["series"][0]["samples"]) == 48
    assert (index / I.DATABASE).read_bytes() == before


def test_real_worker_admits_both_read_only_signal_analyses(stream, tmp_path):
    from raven.raven_worker_client import WorkerClient
    from raven import raven_execution as R
    index, source, attempt = stream
    document = from_index(index, source_id=source, attempt=attempt, metric="loss")
    paths = {"telemetry-anomalies": document, "phase-alignment": {"schema": "raven.phase-comparison/v1", "reference": document, "candidate": document}}
    config = {"site_kind": "desktop", "desktop_explicit": True, "context": {},
              "resources": {"memory_mib": 512, "cpus": 1, "max_tasks": 1, "wall_seconds": 10}}
    with WorkerClient(config) as worker:
        deadline = time.monotonic() + 5
        while not worker.ready and time.monotonic() < deadline:
            worker.poll(); time.sleep(.01)
        assert worker.ready, worker.error
        router = R.ExecutionRouter({"managed": True, "mode": "heavy", "site": "desktop"}, lambda: worker)
        ctx = O.Context(execution=router, scope={"cluster": "fixture", "user": "owner"})
        for feature, value in paths.items():
            path = tmp_path / (feature + ".json"); path.write_text(json.dumps(value))
            result = O.run(feature, {"path": str(path)}, ctx)
            assert result["data"]["status"] == "experimental"
            assert result["execution_receipt"]["operation"] == feature
            assert "plan" not in result


def test_published_schema_matches_generated_example_and_conversion(stream):
    import referencing
    from jsonschema import Draft202012Validator
    schemas = Path(__file__).resolve().parents[1] / "docs/schemas"
    documents = [json.loads((schemas / name).read_text()) for name in ("measurement-series.v1.schema.json", "phase-comparison.v1.schema.json")]
    registry = referencing.Registry().with_resources((doc["$id"], referencing.Resource.from_contents(doc)) for doc in documents)
    index, source, attempt = stream
    measurement = from_index(index, source_id=source, attempt=attempt, metric="loss")
    Draft202012Validator(documents[0], registry=registry).validate(measurement)
    example = subprocess.run([sys.executable, "examples/signal-analysis/make_example.py"], capture_output=True, text=True, check=True)
    Draft202012Validator(documents[1], registry=registry).validate(json.loads(example.stdout))


def test_alignment_distinguishes_input_indices_from_retained_csv_rows(stream):
    from raven import phase_alignment as P
    index, source, attempt = stream
    document = from_index(index, source_id=source, attempt=attempt, metric="loss")
    result = P.analyze({"schema": "raven.phase-comparison/v1", "reference": document, "candidate": document})
    assert result["reference"]["sources"][0]["sha256"] == document["series"][0]["source"]["sha256"]
    assert result["aligned_indices"]
    for pair in result["aligned_indices"]:
        assert pair["reference_sample_indices"] == [pair["reference_index"]]
        assert pair["reference_source_rows"] == [pair["reference_index"] + 2]
