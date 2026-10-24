"""Recorded real-service evidence is distinct from simulated parser fixtures."""
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("slurm_qualification", ROOT / "scripts/qualify_slurm.py")
Q = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(Q)


def test_compatibility_candidates_and_maintained_smoke_are_pinned():
    values = json.loads(Q.VERSIONS.read_text())["versions"]
    assert {row["version"].rsplit(".", 1)[0] for row in values} >= {"23.02", "23.11", "24.05", "24.11"}
    for row in values:
        assert Q.version_spec(row["version"]) == row
    assert any(row["role"] == "maintained-smoke" for row in values)
    with pytest.raises(ValueError):
        Q.version_spec("latest; echo unsafe")


@pytest.mark.parametrize("scenario", ["lifecycle", "trace-workspace"])
def test_plan_contacts_no_service_and_writes_no_state(tmp_path, monkeypatch, capsys, scenario):
    def forbidden(*args, **kwargs):
        raise AssertionError("Plan must not contact Docker")
    monkeypatch.setattr(Q.subprocess, "run", forbidden)
    output = tmp_path / "new"
    assert Q.main(["--plan", "--scenario", scenario, "--output", str(output)]) == 0
    assert not output.exists()
    result = json.loads(capsys.readouterr().out)
    assert result["production_qualified"] is False
    assert result["scenario"] == scenario


def test_docker_selector_reset_preserves_credentials_and_proxy(monkeypatch):
    monkeypatch.setenv("DOCKER_HOST", "tcp://unrelated.example:2375")
    monkeypatch.setenv("DOCKER_CONFIG", "/credential-selector")
    monkeypatch.setenv("HTTPS_PROXY", "http://configured-proxy")
    env = Q.docker_environment()
    assert "DOCKER_HOST" not in env
    assert env["DOCKER_CONFIG"] == "/credential-selector"
    assert env["HTTPS_PROXY"] == "http://configured-proxy"


def test_published_real_corpus_matches_its_hash_manifest():
    folder = ROOT / "tests/fixtures/slurm/recorded/26.05.5"
    value = json.loads((folder / "manifest.json").read_text())
    assert value["environment"].startswith("real Slurm")
    assert value["status"] == "passed-for-recorded-checks"
    assert not value["production_qualified"]
    assert "physical GPU" in value["unqualified"]
    assert all(check["passed"] for check in value["checks"])
    for command in value["commands"]:
        for stream in ("stdout", "stderr"):
            record = command[stream]
            assert hashlib.sha256((folder / record["file"]).read_bytes()).hexdigest() == record["sha256"]
    labels = {row["label"] for row in value["commands"]}
    assert {"pending-squeue-json", "running-squeue", "accounting-json", "requeue", "controller-restart"} <= labels


@pytest.mark.parametrize("version", ["23.02.8", "23.11.11", "24.05.8", "24.11.7", "26.05.5"])
def test_final_versioned_real_lifecycles_preserve_identity_evidence(version):
    folder = ROOT / "tests/fixtures/slurm/recorded" / version / "final"
    value = json.loads((folder / "manifest.json").read_text())
    provenance = json.loads((folder / "execution-provenance.json").read_text())
    assert value["status"] == "passed-for-recorded-checks"
    assert value["environment"].startswith("real Slurm") and not value["production_qualified"]
    assert provenance["version"] == version and provenance["exit_code"] == 0
    assert value["source_sha256"]["raven/slurm.py"] == provenance["source_sha256"]["raven/slurm.py"]
    assert all(row["passed"] for row in value["checks"])
    checks = {row["name"] for row in value["checks"]}
    assert {"actual numeric job id reused", "Raven retained submission excludes recycled job"} <= checks
    for command in value["commands"]:
        for stream in ("stdout", "stderr"):
            record = command[stream]
            assert hashlib.sha256((folder / record["file"]).read_bytes()).hexdigest() == record["sha256"]
    assert (folder / "image-id.txt").read_text().strip() == provenance["image_id"]
