"""Published operation examples remain usable through readers and the real CLI."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from raven import operations as O


ROOT = Path(__file__).resolve().parents[1]
SCIENCE = ROOT / "examples/operations-science"
CAMPAIGNS = ROOT / "docs/examples/campaigns"


class NoScheduler:
    def run(self, argv, timeout=8):
        raise AssertionError("This example inspection must not call a scheduler: " + repr(argv))


@pytest.fixture
def context(tmp_path):
    return O.Context(slurm=SimpleNamespace(b=NoScheduler(), user="researcher"),
                     scope={"user": "researcher"}, state_dir=str(tmp_path / "state"))


EXAMPLES = [
    ("environment", {"manifest": str(SCIENCE / "environment.json")}),
    ("acceptance", {"manifest": str(SCIENCE / "acceptance.json"), "results": str(SCIENCE / "results.json")}),
    ("statistics", {"manifest": str(SCIENCE / "statistics.json")}),
    ("bottlenecks", {"path": str(ROOT / "examples/scale-profiler.json")}),
    ("energy", {"path": str(ROOT / "examples/scale-energy.json")}),
    ("workflow-engine", {"engine": "snakemake", "source": str(ROOT / "docs/examples/workflow-engine.json")}),
] + [(key, {"manifest": str(CAMPAIGNS / (key + ".json")), "action": "inspect"})
     for key in ("checkpoint", "search", "packing", "dask", "heterogeneous")]


@pytest.mark.parametrize("feature,params", EXAMPLES, ids=[x[0] for x in EXAMPLES])
def test_published_examples_use_native_readers_without_scheduler_or_mutation(context, tmp_path, feature, params):
    before = set(tmp_path.rglob("*"))
    report = O.run(feature, params, context)
    assert report["schema"] == "raven.operation/v1"
    assert report["feature"] == feature
    assert report["status"] not in {"error", "failed", "blocked", "unavailable"}
    assert report["rows"]
    assert "plan" not in report
    assert set(tmp_path.rglob("*")) == before


def test_reuse_example_verifies_all_real_hashes_without_publishing(context, tmp_path):
    destination = tmp_path / "copied-results"
    report = O.run("reuse", {"manifest": str(SCIENCE / "reuse.json"),
                            "identity": str(SCIENCE / "identity.json"),
                            "destination": str(destination)}, context)
    assert report["feature"] == "reuse"
    assert "plan" in report
    assert not destination.exists()
    assert not Path(context.state_dir).exists()


def test_staging_example_with_real_sources_is_readonly(context, tmp_path):
    value = json.loads((ROOT / "docs/examples/staging-manifest.json").read_text())
    for index, entry in enumerate(value["entries"]):
        source = tmp_path / f"source-{index}"
        source.write_bytes(b"")
        entry.update(source=str(source), destination=str(tmp_path / f"destination-{index}"))
    manifest = tmp_path / "staging.json"
    manifest.write_text(json.dumps(value))
    before = {path: path.read_bytes() for path in tmp_path.iterdir() if path.is_file()}
    result = O.run("staging", {"manifest": str(manifest)}, context)
    assert "plan" in result
    assert {path: path.read_bytes() for path in tmp_path.iterdir() if path.is_file()} == before
    assert not Path(context.state_dir).exists()


def test_remaining_readonly_schema_examples_use_native_parsers(context):
    from raven import scale_clusters, scale_incidents, ops_science
    sources = scale_clusters.configuration(json.loads((ROOT / "examples/scale-clusters.json").read_text()))
    events = scale_incidents.normalize(json.loads((ROOT / "examples/scale-incidents.json").read_text()))
    identity = ops_science._identity(json.loads((SCIENCE / "identity.json").read_text()))
    assert len(sources) == 2
    assert sources[0]["cluster_key"] != sources[1]["cluster_key"]
    assert events
    assert identity["project"] == "sum-three-example"


def test_every_remaining_proposal_has_unique_reachable_operation_and_fields():
    specs = O.catalog()
    expected = {"P01", "P02", "P03", "P04", "P05", "S01", "S02", "S03", "S04", "S05",
                "A01", "A02", "A04", "A05", "A06", "A07", "A08", "A09", "A10", "A11", "A12",
                "A15", "A17", "A18", "A19", "A20"}
    research = [spec for spec in specs if spec["proposal"].startswith(("S", "P", "A"))]
    assert {spec["proposal"] for spec in research} == expected
    assert len(research) == 26
    assert {spec["key"] for spec in specs if spec["proposal"] == "QoL"} == {"queue-order", "rerun-job"}
    assert len({spec["key"] for spec in specs}) == len(specs)
    assert len(specs) >= 27
    for spec in specs:
        assert O.specification(spec["key"]) == spec
        assert all(field["key"] and field["label"] for field in spec["fields"])
        assert len({field["key"] for field in spec["fields"]}) == len(spec["fields"])


def cli(tmp_path, feature, params, *, fake=False):
    env = dict(os.environ)
    env.update(XDG_STATE_HOME=str(tmp_path / "state"), XDG_CONFIG_HOME=str(tmp_path / "config"),
               XDG_CACHE_HOME=str(tmp_path / "cache"))
    launcher = [sys.executable, "-m", "raven"] if fake else [sys.executable, "-c", "from raven.raven_launch import heavy_main; raise SystemExit(heavy_main())", "--site", "desktop"]
    argv = [*launcher, "run", "ops", "run", feature,
            *(f"{key}={value}" for key, value in params.items()), "--json", "--no-state"]
    if fake:
        argv.append("--fake")
    completed = subprocess.run(argv, cwd=ROOT, env=env, capture_output=True, text=True, timeout=20)
    assert "Traceback" not in completed.stderr
    assert completed.stdout.strip(), completed.stderr
    return completed, json.loads(completed.stdout)


@pytest.mark.parametrize("feature,params", EXAMPLES, ids=[x[0] for x in EXAMPLES])
def test_real_headless_cli_reads_examples_without_fake_cluster(tmp_path, feature, params):
    process, report = cli(tmp_path, feature, params)
    assert process.returncode == 0, (report, process.stderr)
    assert report["schema"] == "raven.operation/v1"
    assert report["feature"] == feature
    assert "plan" not in report


def test_cli_malformed_scientific_evidence_returns_error_json_and_failure(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text('{"schema":"raven.statistics/v1","design":"made-up"}')
    process, report = cli(tmp_path, "statistics", {"manifest": str(bad)})
    assert process.returncode != 0
    assert report["status"] == "error"
    assert report["feature"] == "statistics"


def test_fake_scheduler_unsupported_license_evidence_is_not_claimed_successful(tmp_path):
    process, report = cli(tmp_path, "licenses", {}, fake=True)
    assert process.returncode != 0
    assert report["status"] == "error"
    assert report["feature"] == "licenses"
