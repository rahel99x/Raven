"""Qualify the optional MPI/CUDA examples without substituting emulation.

Numerical and retained-trace integration checks require actual MPI or CUDA.
Dependency/error unit checks run everywhere; they are not device qualification.
"""
import builtins
import csv
import importlib.util
import json
import math
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from raven.metrics import MetricReader
from raven.trace_protocol import TraceReader


ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "trace-lab"
MPIEXEC = shutil.which("mpiexec")
HAS_MPI = importlib.util.find_spec("mpi4py") is not None and MPIEXEC is not None


def _load_program(name, monkeypatch):
    monkeypatch.syspath_prepend(str(EXAMPLE))
    specification = importlib.util.spec_from_file_location("raven_example_" + name, EXAMPLE / (name + ".py"))
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def _environment():
    environment = {key: value for key, value in os.environ.items() if not key.startswith("RAVEN_")}
    environment.update(PYTHONPATH=str(ROOT), OMP_NUM_THREADS="1", OPENBLAS_NUM_THREADS="1",
                       RAVEN_EXAMPLE_PYTHON=sys.executable)
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        # Restrict Open MPI's root consent to this isolated test subprocess.
        environment.update(OMPI_ALLOW_RUN_AS_ROOT="1", OMPI_ALLOW_RUN_AS_ROOT_CONFIRM="1")
    return environment


def _run(arguments, *, cwd, environment=None, timeout=45):
    process = subprocess.Popen(arguments, cwd=cwd, env=environment or _environment(),
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, start_new_session=True)
    try:
        output, errors = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        output, errors = process.communicate(timeout=10)
        pytest.fail("Optional example exceeded its bounded test duration: " + errors[-4000:])
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=10)
    return subprocess.CompletedProcess(arguments, process.returncode, output, errors)


def _read_field(directory):
    with (directory / "final-field.csv").open(newline="") as handle:
        return [[float(value) for value in row] for row in csv.reader(handle)]


def _reference(tmp_path, *, steps=3):
    output = tmp_path / "cpu-reference"
    result = _run([sys.executable, str(EXAMPLE / "heat_lab.py"), "--output-dir", str(output),
                   "--grid", "8", "--steps", str(steps), "--pause", "0", "--workers", "0"], cwd=tmp_path)
    assert result.returncode == 0, (result.stdout, result.stderr)
    return _read_field(output), json.loads((output / "summary.json").read_text())


def _assert_numerical_equivalence(output, reference, reference_summary):
    field = _read_field(output)
    summary = json.loads((output / "summary.json").read_text())
    assert summary["schema"] == "raven.trace-lab/v1"
    assert summary["checks"]["heat_conserved"] and summary["checks"]["maximum_principle"]
    assert summary["deliberate_pause_seconds"] == 0
    assert len(field) == len(reference) == 8
    for actual, expected in zip(field, reference):
        assert len(actual) == 8 and all(math.isfinite(value) for value in actual)
        assert actual == pytest.approx(expected, rel=1e-12, abs=1e-12)
    for name, expected in reference_summary["final_metrics"].items():
        assert summary["final_metrics"][name] == pytest.approx(expected, rel=1e-12, abs=1e-12)
    assert [item["step"] for item in summary["snapshots"]] == [0, 1, 2, 3]
    for actual, expected in zip(summary["snapshots"], reference_summary["snapshots"]):
        assert actual["cells"] == sum(actual["histogram"]) == 64
        assert actual["histogram"] == expected["histogram"]
        for name in ("minimum", "maximum", "mean", "first_mode_amplitude"):
            assert actual[name] == pytest.approx(expected[name], rel=1e-12, abs=1e-12)
    return summary


def _recordings(run):
    deadline = time.monotonic() + 5
    while True:
        sources = [entry for entry in json.loads((run / "trace" / "index.json").read_text())["sources"]
                   if entry.get("schema") == "raven.trace/v1"]
        if all(source["status"] != "recording" for source in sources) or time.monotonic() >= deadline:
            break
        time.sleep(.01)
    assert sources
    snapshots = []
    for source in sources:
        assert source["status"] == "complete", source
        reader = TraceReader(run / source["path"], retain=20000)
        previous = -1
        while True:
            snapshot = reader.refresh(max_events=20000, max_bytes=8 << 20)
            assert snapshot["status"] != "INVALID", snapshot["warnings"]
            if snapshot["caught_up"]:
                break
            assert snapshot["read_seq"] > previous
            previous = snapshot["read_seq"]
        assert snapshot["status"] == "COMPLETE", snapshot["warnings"]
        assert snapshot["manifest"]["state"] == "CLOSED"
        assert snapshot["identity"]["attempt"] == run.name
        snapshots.append(snapshot)
    return snapshots


@pytest.mark.parametrize("name", ["heat_lab", "mpi_heat_lab", "cuda_heat_lab"])
def test_default_workload_has_two_minutes_of_explicit_live_view_pacing(name, monkeypatch, tmp_path):
    program = _load_program(name, monkeypatch)
    args = program.parse_args(["--output-dir", str(tmp_path / "results")])
    assert args.steps * args.pause == 120
    assert not (tmp_path / "results").exists()


def test_cuda_dependency_failure_is_actionable_and_does_not_create_results(monkeypatch, tmp_path):
    program = _load_program("cuda_heat_lab", monkeypatch)
    original_import = builtins.__import__

    def without_torch(name, *args, **kwargs):
        if name == "torch":
            raise ModuleNotFoundError("No module named 'torch'", name="torch")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_torch)
    output = tmp_path / "results"
    with pytest.raises(RuntimeError, match="CUDA-enabled PyTorch installation"):
        program.main(["--output-dir", str(output), "--pause", "0"])
    assert not output.exists()


def test_cuda_unavailable_refuses_cpu_fallback_before_creating_results(monkeypatch, tmp_path):
    program = _load_program("cuda_heat_lab", monkeypatch)
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(cuda=SimpleNamespace(is_available=lambda: False)))
    output = tmp_path / "results"
    with pytest.raises(RuntimeError, match="does not fall back to CPU"):
        program.main(["--output-dir", str(output), "--pause", "0"])
    assert not output.exists()


@pytest.mark.parametrize("arguments", [["--alpha", "nan"], ["--alpha", ".251"], ["--pause", "inf"],
                                       ["--pause", "5", "--steps", "1000"],
                                       ["--grid", "1024", "--steps", "2000"]])
def test_cuda_rejects_unstable_unbounded_work_before_importing_torch(monkeypatch, tmp_path, arguments):
    program = _load_program("cuda_heat_lab", monkeypatch)

    def unexpected_cuda_probe():
        pytest.fail("Invalid input must be rejected before GPU/dependency probing")

    monkeypatch.setattr(program, "require_cuda", unexpected_cuda_probe)
    with pytest.raises(SystemExit) as error:
        program.main(["--output-dir", str(tmp_path / "results"), *arguments])
    assert error.value.code == 2
    assert not (tmp_path / "results").exists()


def test_cuda_existing_results_are_preserved_before_device_access(monkeypatch, tmp_path):
    program = _load_program("cuda_heat_lab", monkeypatch)
    output = tmp_path / "results"
    output.mkdir()
    artifact = output / "summary.json"
    artifact.write_text("existing scientific evidence\n")

    def unexpected_cuda_probe():
        pytest.fail("An existing result must be protected before accessing the GPU")

    monkeypatch.setattr(program, "require_cuda", unexpected_cuda_probe)
    with pytest.raises(FileExistsError, match="Refusing to replace"):
        program.main(["--output-dir", str(output), "--pause", "0"])
    assert artifact.read_text() == "existing scientific evidence\n"
    assert list(output.iterdir()) == [artifact]


def test_mpi_dependency_failure_is_actionable_without_creating_artifacts(monkeypatch, tmp_path, capsys):
    program = _load_program("mpi_heat_lab", monkeypatch)
    original_import = builtins.__import__

    def without_mpi(name, *args, **kwargs):
        if name == "mpi4py":
            raise ModuleNotFoundError("No module named 'mpi4py'", name="mpi4py")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_mpi)
    output = tmp_path / "results"
    assert program.main(["--output-dir", str(output), "--pause", "0"]) == 2
    assert "install mpi4py" in capsys.readouterr().err
    assert not output.exists()


@pytest.fixture
def actual_cuda():
    torch = pytest.importorskip("torch", reason="Actual CUDA example qualification requires installed PyTorch")
    if not torch.cuda.is_available():
        pytest.skip("Actual CUDA example qualification requires a visible CUDA GPU and compatible driver")
    return torch


def test_actual_cuda_matches_cpu_and_records_real_host_call_trace(actual_cuda, tmp_path):
    reference, reference_summary = _reference(tmp_path)
    project = tmp_path / "cuda project"
    project.mkdir()
    for name in ("heat_lab.py", "cuda_heat_lab.py", "job-cuda.sh"):
        shutil.copyfile(EXAMPLE / name, project / name)
    output = project / "results"
    receipt = project / "launch.json"
    arguments = [sys.executable, "-m", "raven", "launch", str(project / "job-cuda.sh"),
                 "--project", str(project), "--local", "--trace", "--trace-scope", "project",
                 "--receipt-file", str(receipt)]
    if sys.version_info < (3, 12):
        arguments += ["--trace-adapter", "python-legacy-profile"]
    arguments += ["--", "--output-dir", str(output), "--grid", "8", "--steps", "3", "--pause", "0"]
    result = _run(arguments, cwd=tmp_path, timeout=90)
    assert result.returncode == 0, (result.stdout, result.stderr)
    receipt_data = json.loads(receipt.read_text())
    assert receipt_data["exit_code"] == 0
    summary = _assert_numerical_equivalence(output, reference, reference_summary)
    assert summary["backend"] == "cuda" and summary["dtype"] == "float64"
    assert summary["device"] == actual_cuda.cuda.get_device_name(0)
    run = Path(receipt_data["run_dir"])
    snapshots = _recordings(run)
    main = next(snapshot for snapshot in snapshots if snapshot["identity"]["pid"] == summary["parent_pid"])
    functions = {event.get("data", {}).get("function") for event in main["events"]}
    assert {"upload_field", "evolve_cuda", "advance_cuda_step", "periodic_neighbors",
            "report_cuda_step", "spectral_amplitude", "write_results"} <= functions
    metrics = MetricReader().read(run / "metrics.jsonl")
    assert metrics["status"] == "ok" and not metrics["errors"]
    for name, value in summary["final_metrics"].items():
        assert metrics["latest"][name] == value


def _mpi_project(tmp_path, name="mpi project"):
    project = tmp_path / name
    project.mkdir()
    for filename in ("heat_lab.py", "mpi_heat_lab.py", "job-mpi.sh"):
        shutil.copyfile(EXAMPLE / filename, project / filename)
    return project


def _mpi_environment(project, ranks):
    environment = _environment()
    environment.update(RAVEN_PROJECT_ROOT=str(project), RAVEN_EXAMPLE_MPI_RANKS=str(ranks),
                       RAVEN_EXAMPLE_MPIEXEC=MPIEXEC)
    return environment


@pytest.mark.skipif(not HAS_MPI, reason="Actual MPI example qualification requires mpi4py and mpiexec")
@pytest.mark.parametrize("ranks", [1, 2, 3])
def test_actual_mpi_even_and_uneven_partitions_match_serial_science(tmp_path, ranks):
    reference, reference_summary = _reference(tmp_path)
    project = _mpi_project(tmp_path)
    output = project / "results"
    metrics_file = project / "metrics.jsonl"
    environment = _mpi_environment(project, ranks)
    environment["RAVEN_METRICS_FILE"] = str(metrics_file)
    arguments = ["bash", str(project / "job-mpi.sh"), "--output-dir", str(output),
                 "--grid", "8", "--steps", "3", "--pause", "0"]
    result = _run(arguments, cwd=tmp_path, environment=environment)
    assert result.returncode == 0, (result.stdout, result.stderr)
    summary = _assert_numerical_equivalence(output, reference, reference_summary)
    assert summary["backend"] == "mpi"
    assert summary["mpi"]["world_size"] == ranks
    assert summary["mpi"]["metrics_writer_rank"] == 0
    rank_details = summary["mpi"]["ranks"]
    assert [rank["rank"] for rank in rank_details] == list(range(ranks))
    assert len({rank["pid"] for rank in rank_details}) == ranks
    assert [row for rank in rank_details for row in range(rank["row_start"], rank["row_stop"])] == list(range(8))
    # Every cell uses the serial stencil's operation order, even where a rank
    # has fewer rows. Reduction metrics may differ only by rounding.
    assert _read_field(output) == reference
    metrics = MetricReader().read(metrics_file)
    assert metrics["status"] == "ok" and not metrics["errors"]
    for name, value in summary["final_metrics"].items():
        assert metrics["latest"][name] == value
    assert not list(project.glob("metrics.rank-*.jsonl"))


@pytest.mark.skipif(not HAS_MPI, reason="Actual MPI example qualification requires mpi4py and mpiexec")
def test_actual_mpi_existing_results_and_coordinated_failure_finish_without_hanging(tmp_path):
    project = _mpi_project(tmp_path)
    output = project / "results"
    output.mkdir()
    artifact = output / "summary.json"
    artifact.write_text("previous scientific evidence\n")
    environment = _mpi_environment(project, 2)
    environment["RAVEN_METRICS_FILE"] = str(project / "metrics.jsonl")
    arguments = ["bash", str(project / "job-mpi.sh"), "--output-dir", str(output),
                 "--grid", "8", "--steps", "3", "--pause", "0"]
    result = _run(arguments, cwd=tmp_path, environment=environment)
    assert result.returncode != 0 and "Refusing to replace" in result.stderr, (result.stdout, result.stderr)
    assert artifact.read_text() == "previous scientific evidence\n"
    assert list(output.iterdir()) == [artifact]
    artifact.unlink()
    result = _run([*arguments, "--fail-at-step", "2"], cwd=tmp_path, environment=environment)
    assert result.returncode != 0 and "Requested demonstration failure after step 2" in result.stderr, (result.stdout, result.stderr)
    assert not (output / "summary.json").exists() and not (output / "final-field.csv").exists()
    metrics = MetricReader().read(project / "metrics.jsonl")
    assert metrics["status"] == "ok" and not metrics["errors"]
    assert metrics["latest"]["residual"] > 0
    progress = [record["progress"] for line in (project / "metrics.jsonl").read_text().splitlines()
                if "progress" in (record := json.loads(line))][-1]
    assert progress["completed"] == 2 and progress["total"] == 3


@pytest.mark.skipif(not HAS_MPI, reason="Actual MPI example qualification requires mpi4py and mpiexec")
def test_actual_mpi_launch_records_distinct_rank_journals_and_collective_measurements(tmp_path):
    project = _mpi_project(tmp_path)
    output = project / "results"
    receipt = project / "launch.json"
    arguments = [sys.executable, "-m", "raven", "launch", str(project / "job-mpi.sh"),
                 "--project", str(project), "--local", "--trace", "--trace-helpers", "--trace-scope", "project",
                 "--receipt-file", str(receipt)]
    if sys.version_info < (3, 12):
        arguments += ["--trace-adapter", "python-legacy-profile"]
    arguments += ["--", "--output-dir", str(output), "--grid", "8", "--steps", "3", "--pause", "0"]
    result = _run(arguments, cwd=tmp_path, environment=_mpi_environment(project, 2))
    assert result.returncode == 0, (result.stdout, result.stderr)
    receipt_data = json.loads(receipt.read_text())
    assert receipt_data["exit_code"] == 0
    run = Path(receipt_data["run_dir"])
    snapshots = _recordings(run)
    by_rank = {snapshot["identity"]["rank"]: snapshot for snapshot in snapshots if "rank" in snapshot["identity"]}
    assert set(by_rank) == {0, 1}
    assert len({snapshot["identity"]["pid"] for snapshot in by_rank.values()}) == 2
    for snapshot in by_rank.values():
        assert snapshot["identity"]["world_size"] == 2
        functions = {event.get("data", {}).get("function") for event in snapshot["events"]}
        assert {"exchange_halos", "advance_partition", "collective_measurements", "gather_snapshot"} <= functions
    root_functions = {event.get("data", {}).get("function") for event in by_rank[0]["events"]}
    assert "write_results" in root_functions
    summary = json.loads((output / "summary.json").read_text())
    metrics = MetricReader().read(run / "metrics.jsonl")
    assert metrics["status"] == "ok" and not metrics["errors"]
    for name, value in summary["final_metrics"].items():
        assert metrics["latest"][name] == value
    assert not list(run.glob("metrics.rank-*.jsonl"))
    metadata = json.loads((run / "metrics.meta.json").read_text())
    assert metadata["rank_policy"]["mode"] == "coordinator"
    assert metadata["rank_policy"]["rank"] == 0
    assert metadata["kinds"]["residual"] == "residual"
    assert metadata["kinds"]["total_heat"] == "conserved"


def test_mpi_slurm_helper_keeps_requested_ranks_in_resource_and_launcher_settings(monkeypatch, tmp_path):
    """Check command construction; no scheduler or MPI launcher runs here."""
    demo = _load_program("demo", monkeypatch)
    commands = []
    monkeypatch.setattr(demo, "executable", lambda name: sys.executable if name == sys.executable else "/fixture/bin/" + name)

    def capture_command(arguments, **kwargs):
        if arguments[1] == "-c":
            output = '["cpython",3,12]' if "sys.implementation.name" in arguments[2] else "fixture MPI runtime"
            return subprocess.CompletedProcess(arguments, 0, output, "")
        commands.append((arguments, kwargs))
        return subprocess.CompletedProcess(arguments, 0)

    monkeypatch.setattr(demo.subprocess, "run", capture_command)
    project = tmp_path / "MPI fixture project"
    assert demo.main(["run", "--slurm", "--variant", "mpi", "--ranks", "3", "--quick",
                      "--python", sys.executable, "--directory", str(project), "--account", "LabX"]) == 0
    assert len(commands) == 1
    command, options = commands[0]
    assert command[command.index("launch") + 1] == str(project / "job-mpi.sh")
    assert "--sbatch=--ntasks=3" in command
    assert command[command.index("--account") + 1] == "LabX"
    assert "--workers" not in command and "--local" not in command
    assert options["env"]["RAVEN_EXAMPLE_MPI_RANKS"] == "3"
    assert options["env"]["RAVEN_EXAMPLE_MPIEXEC"] == "/fixture/bin/mpiexec"
    assert options["cwd"] == project
    for name in ("heat_lab.py", "mpi_heat_lab.py", "job-mpi.sh"):
        assert (project / name).read_bytes() == (EXAMPLE / name).read_bytes()


def test_cuda_helper_preflight_accepts_headless_submission_host_and_checks_local_device(monkeypatch, tmp_path):
    """A dependency fixture verifies probing policy, not CUDA execution."""
    demo = _load_program("demo", monkeypatch)
    probe_marker = tmp_path / "device-probed"
    (tmp_path / "torch.py").write_text(
        "from pathlib import Path\n"
        "from types import SimpleNamespace\n"
        "version = SimpleNamespace(cuda='12.1')\n"
        "def available():\n"
        f"    Path({str(probe_marker)!r}).write_text('queried')\n"
        "    return False\n"
        "cuda = SimpleNamespace(is_available=available)\n"
    )
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    args = SimpleNamespace(variant="cuda", ranks=None, mpiexec=None, slurm=True)
    assert demo.check_variant(args, sys.executable) is None
    assert not probe_marker.exists()
    args.slurm = False
    with pytest.raises(ValueError, match="No CUDA device is available on this workstation"):
        demo.check_variant(args, sys.executable)
    assert probe_marker.read_text() == "queried"


@pytest.mark.skipif(not HAS_MPI, reason="Actual MPI example helper qualification requires mpi4py and mpiexec")
def test_actual_mpi_run_helper_and_exact_rank_viewer_use_real_recordings(tmp_path):
    binary = tmp_path / "bin"
    binary.mkdir()
    launcher = binary / "raven"
    launcher.write_text("#!" + sys.executable + "\nfrom raven.raven_launch import light_main\nraise SystemExit(light_main())\n")
    launcher.chmod(0o700)
    environment = _environment()
    environment["PATH"] = str(binary) + os.pathsep + os.environ.get("PATH", "")
    project = tmp_path / "real MPI helper project"
    arguments = ["bash", str(EXAMPLE / "run.sh"), "--local", "--variant", "mpi", "--ranks", "2",
                 "--mpiexec", MPIEXEC, "--python", sys.executable, "--quick", "--directory", str(project)]
    if sys.version_info < (3, 12):
        arguments.append("--legacy-profile")
    result = _run(arguments, cwd=tmp_path, environment=environment)
    assert result.returncode == 0, (result.stdout, result.stderr)
    receipt = project / "launch.json"
    data = json.loads(receipt.read_text())
    assert data["execution"] == "local" and data["exit_code"] == 0
    summary = json.loads((project / "results/summary.json").read_text())
    assert summary["mpi"]["world_size"] == 2
    assert summary["checks"]["heat_conserved"] and summary["checks"]["maximum_principle"]
    assert summary["deliberate_pause_seconds"] == 0 and summary["parameters"]["steps"] == 8
    plan = json.loads((project / ".raven/wrappers" / data["token"] / "plan.json").read_text())
    assert "--workers" not in plan["script_args"]
    assert plan["settings"]["trace_scope"] == "project"
    assert plan["settings"]["gpu"] is False
    run = Path(data["run_dir"])
    snapshots = _recordings(run)
    rank_one = next(snapshot for snapshot in snapshots if snapshot["identity"].get("rank") == 1)
    pid = rank_one["identity"]["pid"]
    index = json.loads((run / "trace/index.json").read_text())
    manifest = next(run / source["path"] for source in index["sources"]
                    if source.get("schema") == "raven.trace/v1" and source["process"]["pid"] == pid)
    result = _run(["bash", str(EXAMPLE / "view.sh"), str(receipt), "--process", str(pid)],
                  cwd=tmp_path, environment=environment)
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert "rank 1" in result.stdout and str(manifest) in result.stdout
    assert "checked events" in result.stdout and "INVALID" not in result.stdout
    metrics = MetricReader().read(run / "metrics.jsonl")
    assert metrics["status"] == "ok" and not metrics["errors"]
    assert metrics["latest"]["total_heat"] == summary["final_metrics"]["total_heat"]
