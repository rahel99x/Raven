"""Node examples validate MPI math and actual observed placement separately."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "trace-topology"
MPIEXEC = shutil.which("mpiexec")
MPI_PYTHON = os.environ.get("RAVEN_MPI_TEST_PYTHON") or (
    sys.executable if importlib.util.find_spec("mpi4py") is not None else None
)


@pytest.fixture
def workload(monkeypatch):
    monkeypatch.syspath_prepend(str(EXAMPLE))
    spec = importlib.util.spec_from_file_location(
        "raven_topology_node_workload", EXAMPLE / "node_workload.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("size,ranks", [(8, 2), (17, 3), (8, 7), (8, 8), (32, 5)])
def test_halo_partitions_match_the_exact_serial_stencil(workload, size, ranks):
    initial = workload.common.initial_field(size)
    field = initial
    for _ in range(5):
        pieces = []
        for rank in range(ranks):
            begin, end = workload.partition_bounds(size, rank, ranks)
            pieces.append(
                workload.advance_partition(
                    field[begin:end], field[(begin - 1) % size], field[end % size]
                )
            )
        field = tuple(value for piece in pieces for value in piece)
    assert field == workload.common.advance(initial, sweeps=5)
    checks = workload.common.check_field(initial, field)
    assert checks["mean_conserved"] and checks["energy_nonincreasing"]


@pytest.mark.parametrize(
    "arguments",
    [(8, 0, 0), (8, -1, 2), (8, 2, 2), (2, 0, 3), (8, False, 2), (128, 0, 129)],
)
def test_invalid_decompositions_fail_without_implicit_empty_ranks(workload, arguments):
    with pytest.raises(ValueError, match="one field cell"):
        workload.partition_bounds(*arguments)


def test_node_contract_uses_observed_hosts_instead_of_requested_counts(workload):
    local = [{"node": "workstation", "rank": rank} for rank in range(2)]
    distributed = [local[0], {"node": "compute-2", "rank": 1}]
    assert workload.validate_world(local, variant="single_node", field_size=8) == [
        "workstation"
    ]
    with pytest.raises(
        workload.CoordinatedFailure, match="do not demonstrate multiple nodes"
    ):
        workload.validate_world(local, variant="multi_node", field_size=8)
    assert workload.validate_world(distributed, variant="multi_node", field_size=8) == [
        "compute-2",
        "workstation",
    ]
    with pytest.raises(workload.CoordinatedFailure, match="all ranks on one"):
        workload.validate_world(distributed, variant="single_node", field_size=8)


def test_root_operation_propagates_artifact_failure_to_collective_peers(workload):
    class Root:
        def Get_rank(self):
            return 0

        def bcast(self, value, root):
            self.error = value
            return value

    root = Root()

    def fail():
        raise FileExistsError("refuse old summary")

    with pytest.raises(workload.CoordinatedFailure, match="Output.*refuse old summary"):
        workload.root_operation(root, fail, "Output")
    peer = SimpleNamespace(Get_rank=lambda: 1, bcast=lambda value, root: root_error)
    root_error = root.error
    with pytest.raises(workload.CoordinatedFailure, match="refuse old summary"):
        workload.root_operation(
            peer, lambda: pytest.fail("peer called root writer"), "Output"
        )


def test_default_node_duration_and_worker_control_are_explicit(workload, tmp_path):
    parser = workload.common.argument_parser("node test", include_workers=False)
    args = workload.common.validate_args(
        parser, parser.parse_args(["--output-dir", str(tmp_path / "new")])
    )
    assert args.seconds == 120 and not hasattr(args, "workers")
    assert not args.output_dir.exists()


def test_help_does_not_require_an_mpi_install_or_allocate_output(tmp_path):
    result = subprocess.run(
        [sys.executable, str(EXAMPLE / "multi_node.py"), "--help"],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 0 and "two minute" in result.stdout.replace("-", " ")
    assert not list(tmp_path.iterdir())


def _run_mpi(script, output, *, metrics, timeout=30):
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("RAVEN_")
    }
    environment.update(
        PYTHONPATH=str(ROOT),
        RAVEN_METRICS_FILE=str(metrics),
        OMP_NUM_THREADS="1",
        OPENBLAS_NUM_THREADS="1",
    )
    if os.geteuid() == 0:
        environment.update(
            OMPI_ALLOW_RUN_AS_ROOT="1", OMPI_ALLOW_RUN_AS_ROOT_CONFIRM="1"
        )
    process = subprocess.Popen(
        [
            MPIEXEC,
            "-n",
            "2",
            MPI_PYTHON,
            str(EXAMPLE / script),
            "--output-dir",
            str(output),
            "--seconds",
            "0",
            "--steps",
            "3",
            "--size",
            "17",
            "--sweeps",
            "2",
        ],
        cwd=output.parent,
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        stdout, stderr = process.communicate(timeout=5)
        pytest.fail(
            "Owned MPI example did not exit within its test budget: " + stderr[-2000:]
        )
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
    return process.returncode, stdout, stderr


@pytest.mark.skipif(
    not (MPIEXEC and MPI_PYTHON),
    reason="requires actual mpi4py and MPI launcher; set RAVEN_MPI_TEST_PYTHON explicitly",
)
def test_actual_single_node_mpi_retains_checked_results_and_rank_zero_metrics(tmp_path):
    output, metrics = tmp_path / "single-node", tmp_path / "metrics.jsonl"
    code, stdout, stderr = _run_mpi("single_node.py", output, metrics=metrics)
    assert code == 0, (stdout, stderr)
    summary = json.loads((output / "summary.json").read_text())
    assert summary["schema"] == "raven.trace-topology/v1"
    assert summary["variant"] == "single_node" and summary["ranks"] == 2
    assert len(summary["observed_nodes"]) == 1
    assert len({row["pid"] for row in summary["placements"]}) == 2
    assert [row["rank"] for row in summary["placements"]] == [0, 1]
    assert summary["numerical_checks"]["serial_reference_equivalent"] is True
    assert summary["numerical_checks"]["serial_reference_max_error"] == 0
    assert summary["parameters"]["workers"] == 2
    rows = [json.loads(line) for line in metrics.read_text().splitlines()]
    assert rows and {row["pid"] for row in rows} == {summary["placements"][0]["pid"]}
    assert any("diffusion_energy" in row.get("metrics", {}) for row in rows)


@pytest.mark.skipif(
    not (MPIEXEC and MPI_PYTHON), reason="requires actual mpi4py and MPI launcher"
)
def test_actual_two_local_ranks_are_not_labeled_multi_node(tmp_path):
    output = tmp_path / "rejected"
    code, stdout, stderr = _run_mpi(
        "multi_node.py", output, metrics=tmp_path / "metrics.jsonl"
    )
    assert code != 0 and "two distinct observed hostnames" in stderr, (stdout, stderr)
    assert not output.exists()


@pytest.mark.skipif(
    not (MPIEXEC and MPI_PYTHON), reason="requires actual mpi4py and MPI launcher"
)
def test_actual_root_output_collision_exits_without_stranding_other_ranks(tmp_path):
    output = tmp_path / "existing"
    output.mkdir()
    (output / "keep").write_text("original")
    code, stdout, stderr = _run_mpi(
        "single_node.py", output, metrics=tmp_path / "metrics.jsonl"
    )
    assert code != 0 and "Output directory" in stderr, (stdout, stderr)
    assert (output / "keep").read_text() == "original" and not (
        output / "summary.json"
    ).exists()
