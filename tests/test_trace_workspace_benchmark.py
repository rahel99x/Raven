"""Benchmark evidence must compare identical scopes and keep memory units honest."""

import json
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/benchmark_trace_workspace.py"


def test_legacy_comparison_uses_same_inclusive_boundary_as_index():
    import importlib.util

    spec = importlib.util.spec_from_file_location("trace_workspace_benchmark", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    model = dict(
        origin_ns=100,
        identity=dict(stream="source"),
        intervals=[
            dict(start_ns=0, end_ns=10, name="ends-at-start"),
            dict(start_ns=20, end_ns=30, name="starts-at-end"),
            dict(start_ns=21, end_ns=30, name="outside"),
        ],
    )
    assert [row["name"] for row in module._legacy_query([model], 110, 120)] == [
        "ends-at-start",
        "starts-at-end",
    ]


def run_fixture(tmp_path, intervals):
    output = tmp_path / "benchmark.json"
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--intervals",
            str(intervals),
            "--queries",
            "2",
            "--repeats",
            "1",
            "--output",
            str(output),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(output.read_text())


def test_benchmark_checks_equivalent_answers_and_separates_allocations_from_rss(
    tmp_path,
):
    report = run_fixture(tmp_path, 8)
    assert report["equivalent_whole_fixture_query_answers"] is True
    assert {sample["variant"] for sample in report["samples"]} == {
        "legacy-retained",
        "indexed-workspace",
    }
    assert len(report["samples"]) == 4
    answers = [sample["query_answers"] for sample in report["samples"]]
    assert all(value == answers[0] for value in answers)
    for sample in report["samples"]:
        assert (
            sample["matched_interval_count"] == report["fixture"]["expected_intervals"]
        )
        assert sample["process_peak_rss_bytes"] > 0
        if sample["measurement"] == "allocation":
            assert sample["python_traced_peak_bytes"] > 0
        else:
            assert sample["python_traced_peak_bytes"] is None
    assert report["fixture"]["sources"][0]["journal_sha256"]


def test_legacy_eviction_is_not_reported_as_whole_run_comparison(tmp_path):
    report = run_fixture(tmp_path, 10_001)
    assert report["summary"]["legacy-retained"]["whole_fixture_comparable"] is False
    assert report["summary"]["indexed-workspace"]["whole_fixture_comparable"] is True
    assert report["equivalent_whole_fixture_query_answers"] is None
    assert report["within_supported_variant_repeat_answers_consistent"] is True
    for sample in report["samples"]:
        if sample["variant"] == "indexed-workspace":
            assert sample["matched_interval_count"] == 10_002
        else:
            assert sample["matched_interval_count"] < 10_002
