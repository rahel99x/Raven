"""Independent run-to-export bridge checks without native qualification claims."""

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from raven import projects
from raven.engineering import experiments, provenance
from raven.schedulers.core import scope_digest


def joined_fixture(tmp_path):
    project = tmp_path / "science"
    run = project / "runs" / "run-a1"
    run.mkdir(parents=True)
    script = b"#!/bin/sh\necho measured\n"
    (project / "job.sh").write_bytes(script)
    submitted = int(datetime(2026, 10, 25, tzinfo=timezone.utc).timestamp())
    script_sha = hashlib.sha256(script).hexdigest()
    inventory = dict(schema="raven.run/v1", project_id="lab", run_id="run-a1", experiment_id="old", submit=submitted,
                     attempt=1, state="COMPLETED", job_id="123", resources=dict(cpus=2),
                     metadata=dict(raven_launch=dict(cluster="carc", restart_count=0, submission_token="a" * 32)),
                     provenance=dict(script="job.sh", script_sha256=script_sha))
    (run / "run.json").write_text(json.dumps(inventory))
    binding = projects.select_run(str(project), "run-a1")["binding"]
    native = dict(scheduler="slurm", scope=scope_digest(dict(cluster="carc", user="alice")),
                  native_id="123", attempt="2026-10-25T00:00:00/restart:0")
    experiments.create(project, "solver")
    linked = experiments.link(project, "solver", binding, scheduler_identity=native)
    bundle = tmp_path / "export"
    bundle.mkdir()
    raw = ("project,run_id,attempt,cluster,job_id,submit_time,metric,value,unit,timestamp,clock,script_sha256\n"
           f"{project},run-a1,1,carc,123,{submitted},loss,0.5,ratio,1,unix-epoch-seconds UTC,{script_sha}\n").encode()
    (bundle / "metrics.csv").write_bytes(raw)
    manifest = dict(schema="raven.export-manifest/v1", export_id="fixture",
                    files=[dict(path="metrics.csv", bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())])
    manifest_raw = json.dumps(manifest).encode()
    (bundle / "manifest.json").write_bytes(manifest_raw)
    (bundle / "COMPLETE.json").write_text(json.dumps(dict(schema="raven.export-complete/v1", export_id="fixture",
                                                          manifest_sha256=hashlib.sha256(manifest_raw).hexdigest())))
    return project, bundle, native, linked


def companion(project, bundle, native, **options):
    return provenance.from_export(bundle, "metrics.csv", native, "Loss",
        fields=["timestamp", "value"], units=dict(timestamp="s", value="ratio"),
        calculation=dict(name="recorded values", version="1", parameters={}),
        experiment_project=project, experiment_id="solver", **options)


def test_export_bridge_matches_full_retained_run_and_stays_partial(tmp_path):
    project, bundle, native, linked = joined_fixture(tmp_path)
    before = {file.name: file.read_bytes() for file in bundle.iterdir()}
    value = companion(project, bundle, native)
    assert value["data"] == [dict(timestamp=1, value=.5)]
    assert value["association"]["status"] == "recorded-link"
    assert value["association"]["experiment"]["attempt"] == linked["attempts"][0]["id"]
    assert value["association"]["source_identity"]["run_id"] == "run-a1"
    assert value["association"]["source_identity"]["project"] == str(project)
    assert value["association"]["requested_identity"] == native
    assert value["status"] == "partial"
    assert provenance.compare(value, value)["native_association_verified"] is False
    assert before == {file.name: file.read_bytes() for file in bundle.iterdir()}


def test_export_bridge_refuses_replaced_run_attempt(tmp_path):
    project, bundle, native, _ = joined_fixture(tmp_path)
    path = project / "runs" / "run-a1" / "run.json"
    run = json.loads(path.read_text())
    run["attempt"] = 2
    path.write_text(json.dumps(run))
    with pytest.raises(ValueError, match="different run attempt|changed|attempt"):
        companion(project, bundle, native)


def test_export_bridge_refuses_another_native_scope_for_same_job_number(tmp_path):
    project, bundle, native, _ = joined_fixture(tmp_path)
    wrong = dict(native, scope=scope_digest(dict(cluster="rcic", user="alice")))
    with pytest.raises(ValueError, match="exact exported run"):
        companion(project, bundle, wrong)


@pytest.mark.parametrize("field", ["cluster", "submit_time", "script_sha256"])
def test_export_bridge_rejects_contradictory_retained_source_fields(tmp_path, field):
    import csv
    import io

    project, bundle, native, _ = joined_fixture(tmp_path)
    rows = list(csv.DictReader(io.StringIO((bundle / "metrics.csv").read_text())))
    rows[0][field] = {"cluster": "rcic", "submit_time": "1", "script_sha256": "f" * 64}[field]
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
    writer.writeheader()
    writer.writerows(rows)
    raw = stream.getvalue().encode()
    (bundle / "metrics.csv").write_bytes(raw)
    manifest = json.loads((bundle / "manifest.json").read_text())
    manifest["files"][0].update(bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())
    manifest_raw = json.dumps(manifest).encode()
    (bundle / "manifest.json").write_bytes(manifest_raw)
    complete = json.loads((bundle / "COMPLETE.json").read_text())
    complete["manifest_sha256"] = hashlib.sha256(manifest_raw).hexdigest()
    (bundle / "COMPLETE.json").write_text(json.dumps(complete))
    with pytest.raises(ValueError, match="contradicts"):
        companion(project, bundle, native)
