"""Harness scope and unavailable-tool behavior, without running Torch workloads."""

import json
import os
import runpy
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "benchmark_activity_capture.py"


def test_unavailable_application_framework_is_reported_without_install_or_fake_overhead(
    tmp_path,
):
    unavailable = tmp_path / "unavailable"
    unavailable.mkdir()
    (unavailable / "torch.py").write_text(
        "raise ImportError('fixture: Torch is deliberately unavailable')\n"
    )
    output = tmp_path / "report.json"
    environment = dict(os.environ, PYTHONPATH=str(unavailable))
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--python",
            sys.executable,
            "--repeats",
            "1",
            "--size",
            "16",
            "--steps",
            "1",
            "--sweeps",
            "1",
            "--output",
            str(output),
        ],
        env=environment,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(output.read_text())
    assert report["schema"] == "raven.activity-capture-benchmark/v1"
    assert len(report["samples"]) == 2
    assert {row["variant"] for row in report["samples"]} == {"disabled", "torch-cpu"}
    assert all(row["status"] == "unavailable" for row in report["samples"])
    assert all("deliberately unavailable" in row["reason"] for row in report["samples"])
    assert report["summary"]["all_timing_samples_complete"] is False
    assert report["summary"]["overhead_vs_disabled"] is None
    assert report["summary"]["identical_numerical_checksums"] is False
    assert report["source_sha256"]["scripts/benchmark_activity_capture.py"]


def test_summary_requires_equivalent_complete_scientific_work_and_separates_allocation_samples():
    summary = runpy.run_path(str(SCRIPT))["_summary"]

    def sample(variant, factor):
        return {
            "variant": variant,
            "measurement": "timing",
            "status": "complete",
            "child_exit": 0,
            "checksum_hex": {"sum": "0x1p0"},
            "scientific_work_wall_seconds": factor,
            "scientific_work_cpu_seconds": factor,
            "work_and_checkpoints_wall_seconds": factor * 2,
            "process_wall_seconds": factor * 3,
            "process_peak_rss_bytes": 100 * factor,
        }

    control, captured = sample("disabled", 1), sample("torch-cpu", 2)
    allocation = dict(
        sample("torch-cpu", 1000), measurement="allocation", python_traced_peak_bytes=10
    )
    good = summary([control, captured, allocation])
    assert good["all_timing_samples_complete"] is True
    assert good["identical_numerical_checksums"] is True
    assert good["overhead_vs_disabled"]["scientific_work_wall_seconds"]["ratio"] == 2
    assert good["variants"]["torch-cpu"]["median"]["process_peak_rss_bytes"] == 200
    incomplete = summary([control, dict(captured, status="capture-incomplete")])
    assert incomplete["overhead_vs_disabled"] is None
    different = summary([control, dict(captured, checksum_hex={"sum": "0x2p0"})])
    assert different["overhead_vs_disabled"] is None
    assert summary([control])["overhead_vs_disabled"] is None
