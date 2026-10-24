"""Actual CPU examples and conservative core selection, without GPU emulation."""

from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "trace-topology"


@pytest.fixture
def programs(monkeypatch):
    monkeypatch.syspath_prepend(str(EXAMPLE))
    import common
    import cpu_workload

    return common, cpu_workload


def run_program(tmp_path, name):
    output = tmp_path / name
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("RAVEN_")
    }
    environment["PYTHONPATH"] = str(ROOT)
    process = subprocess.Popen(
        [
            sys.executable,
            str(EXAMPLE / (name + ".py")),
            "--output-dir",
            str(output),
            "--seconds",
            "0",
            "--steps",
            "3",
            "--size",
            "64",
        ],
        cwd=tmp_path,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=35)
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
    assert process.returncode == 0, (stdout, stderr)
    return json.loads((output / "summary.json").read_text())


def test_default_duration_and_invalid_inputs_have_no_artifact_side_effects(
    tmp_path, programs
):
    common, _ = programs
    destination = tmp_path / "not-created"
    args = common.parse_args(["--output-dir", str(destination)], description="test")
    assert args.seconds == 120 and args.steps == 80 and args.sweeps == 4
    assert not destination.exists()
    for arguments in (
        ("--seconds", "nan"),
        ("--steps", "0"),
        ("--workers", "9"),
        ("--size", "7"),
    ):
        with pytest.raises(SystemExit):
            common.parse_args(
                ["--output-dir", str(destination), *arguments], description="test"
            )
    assert not destination.exists()


def test_single_and_multi_thread_run_same_science_in_actual_distinct_threads(
    tmp_path, programs
):
    common, _ = programs
    sequential = run_program(tmp_path, "single_thread")
    threaded = run_program(tmp_path, "multi_thread")
    expected = common.initial_field(64)
    for _ in range(3):
        expected = common.advance(expected)
    for summary in (sequential, threaded):
        assert summary["schema"] == "raven.trace-topology/v1"
        assert summary["parameters"]["seconds"] == 0
        assert summary["checks"]["serial_equivalent"] is True
        assert summary["final_energy"] == common.energy(expected)
        assert all(
            summary["checks"][key]
            for key in ("finite", "mean_conserved", "energy_nonincreasing")
        )
    assert len(sequential["execution"]) == 1
    assert sequential["execution"][0]["thread"] == "MainThread"
    owners = threaded["execution"]
    assert len({row["pid"] for row in owners}) == 1
    assert len({row["native_tid"] for row in owners}) == 2
    assert all(row["thread"].startswith("raven-science") for row in owners)


@pytest.mark.skipif(
    not hasattr(os, "sched_setaffinity"), reason="Linux affinity API required"
)
def test_single_core_threads_are_constrained_to_one_allowed_logical_cpu(tmp_path):
    summary = run_program(tmp_path, "single_core")
    cpu = summary["cpu_plan"][0]["logical_cpu"]
    assert cpu in os.sched_getaffinity(0)
    assert len(summary["execution"]) == 2
    assert all(owner["allowed_cpu_ids"] == [cpu] for owner in summary["execution"])


@pytest.mark.skipif(
    not hasattr(os, "sched_setaffinity"), reason="Linux affinity API required"
)
def test_single_core_restores_original_affinity_when_scientific_work_fails(
    tmp_path, programs, monkeypatch
):
    _, cpu = programs
    original = os.sched_getaffinity(0)
    observed = []

    def failed(*_):
        observed.append(os.sched_getaffinity(0))
        raise LookupError("original scientific failure")

    monkeypatch.setattr(cpu, "evolve", failed)
    with pytest.raises(LookupError, match="original scientific failure"):
        cpu.main(
            "single-core",
            [
                "--output-dir",
                str(tmp_path / "failure"),
                "--seconds",
                "0",
                "--steps",
                "1",
            ],
        )
    assert observed == [{min(original)}]
    assert os.sched_getaffinity(0) == original


def test_core_selection_groups_smt_siblings_but_keeps_socket_and_die_identity(
    tmp_path, programs
):
    _, cpu = programs
    layout = [(0, 0, 0, 1), (1, 0, 0, 1), (2, 1, 0, 1), (3, 1, 1, 1)]
    for logical, socket, die, core in layout:
        folder = tmp_path / ("cpu" + str(logical)) / "topology"
        folder.mkdir(parents=True)
        for name, value in (
            ("physical_package_id", socket),
            ("die_id", die),
            ("core_id", core),
        ):
            (folder / name).write_text(str(value))
    result = cpu.physical_cores({0, 1, 2, 3}, sysfs=tmp_path)
    assert [row["logical_cpu"] for row in result] == [0, 2, 3]
    # The cpuset controls eligibility even when a sibling has a smaller ID.
    assert [
        row["logical_cpu"] for row in cpu.physical_cores({1, 2}, sysfs=tmp_path)
    ] == [1, 2]


def test_unknown_or_unreadable_core_topology_is_not_replaced_with_a_guess(
    tmp_path, programs
):
    _, cpu = programs
    with pytest.raises(RuntimeError, match="Cannot prove physical-core"):
        cpu.physical_cores({1}, sysfs=tmp_path)


@pytest.mark.skipif(
    not hasattr(os, "sched_setaffinity"), reason="Linux affinity API required"
)
def test_multi_core_uses_actual_processes_on_distinct_proven_cores(tmp_path, programs):
    _, cpu = programs
    try:
        cores = cpu.physical_cores(os.sched_getaffinity(0))
    except RuntimeError as error:
        pytest.skip(str(error))
    if len(cores) < 2:
        pytest.skip("This execution cpuset does not contain two proven physical cores")
    summary = run_program(tmp_path, "multi_core")
    assert len({owner["pid"] for owner in summary["execution"]}) == 2
    planned = {row["logical_cpu"] for row in summary["cpu_plan"]}
    assert len(planned) == 2
    assert {tuple(owner["allowed_cpu_ids"]) for owner in summary["execution"]} == {
        (logical,) for logical in planned
    }


def test_existing_output_directory_is_refused_without_replacing_files(
    tmp_path, programs
):
    common, _ = programs
    destination = tmp_path / "existing"
    destination.mkdir()
    (destination / "summary.json").write_text("original")
    args = common.parse_args(["--output-dir", str(destination)], description="test")
    with pytest.raises(FileExistsError):
        common.write_summary(args, {"variant": "test"})
    assert (destination / "summary.json").read_text() == "original"
