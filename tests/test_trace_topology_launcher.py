"""Topology reservations and explicit launch arguments, without hardware claims."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples/trace-topology"
SPEC = importlib.util.spec_from_file_location(
    "trace_topology_launcher", EXAMPLE / "launch.py"
)
launch = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(launch)


def arguments(name, *extra, local=False):
    return launch.parser().parse_args(
        [
            "--local" if local else "--slurm",
            "--example",
            name,
            *extra,
        ]
    )


@pytest.mark.parametrize(
    "name,nodes,ranks,cpus,workers,gpus",
    [
        ("single_thread", 1, 1, 1, 1, 0),
        ("multi_thread", 1, 1, 2, 2, 0),
        ("single_core", 1, 1, 1, 2, 0),
        ("multi_core", 1, 1, 2, 2, 0),
        ("single_node", 1, 2, 1, 1, 0),
        ("multi_node", 2, 2, 1, 1, 0),
        ("single_gpu", 1, 1, 1, 1, 1),
        ("multi_gpu", 1, 1, 1, 2, 2),
    ],
)
def test_default_topology_reservation_matches_execution(
    name, nodes, ranks, cpus, workers, gpus
):
    plan = launch.resources(arguments(name))
    assert (
        plan["nodes"],
        plan["ranks"],
        plan["cpus"],
        plan["workers"],
        plan["gpus"],
    ) == (
        nodes,
        ranks,
        cpus,
        workers,
        gpus,
    )


@pytest.mark.parametrize(
    "name,options,local",
    [
        ("single_thread", ["--workers", "2"], False),
        ("multi_core", ["--workers", "1"], False),
        ("single_core", ["--workers", "1"], False),
        ("multi_gpu", ["--workers", "9"], False),
        ("single_core", ["--ranks", "2"], False),
        ("single_node", ["--workers", "2"], False),
        ("single_node", ["--nodes", "2"], False),
        ("multi_node", ["--nodes", "1"], False),
        ("multi_node", ["--nodes", "3", "--ranks", "2"], False),
        ("multi_node", [], True),
        ("single_thread", ["--no-activities"], False),
        ("single_node", ["--mpi", "pmix;command"], False),
        ("single_node", ["--mpiexec", "mpiexec"], False),
        ("single_thread", ["--account", "LabX"], True),
    ],
)
def test_invalid_topologies_never_create_or_submit(
    name, options, local, tmp_path, monkeypatch
):
    project = tmp_path / "absent"
    monkeypatch.setattr(
        launch,
        "preflight",
        lambda *a: pytest.fail("Invalid topology reached preflight"),
    )
    assert (
        launch.main(
            [
                "--local" if local else "--slurm",
                "--example",
                name,
                "--directory",
                str(project),
                *options,
            ]
        )
        == 2
    )
    assert not project.exists()


@pytest.fixture
def captured(monkeypatch):
    calls = []
    monkeypatch.setattr(
        launch,
        "preflight",
        lambda args, plan: (
            "/fixture/raven",
            sys.executable,
            "/fixture/mpiexec" if args.local and plan["node_example"] else None,
        ),
    )

    def execute(command, **options):
        calls.append((command, options))
        receipt = Path(command[command.index("--receipt-file") + 1])
        receipt.write_text(json.dumps({"schema": "raven.launch-receipt/v1"}))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(launch.subprocess, "run", execute)
    return calls


@pytest.mark.parametrize("name", launch.PROGRAMS)
def test_launch_plan_and_job_execution_share_one_topology(name, captured, tmp_path):
    project = tmp_path / name
    assert (
        launch.main(
            ["--slurm", "--example", name, "--directory", str(project), "--quick"]
        )
        == 0
    )
    command, options = captured[0]
    plan = launch.resources(arguments(name))
    assert "--sbatch=--nodes=" + str(plan["nodes"]) in command
    assert "--sbatch=--ntasks=" + str(plan["ranks"]) in command
    assert "--sbatch=--cpus-per-task=" + str(plan["cpus"]) in command
    assert ("--sbatch=--gpus-per-task=" + str(plan["gpus"]) in command) == plan["gpu"]
    assert ("--sbatch=--ntasks-per-node=1" in command) == (name == "multi_node")
    assert ("--sbatch=--hint=nomultithread" in command) == name.endswith("_core")
    assert command[command.index("--trace-profile") + 1] == "topology"
    assert "--trace-helpers" in command and "--cpu-memory" in command
    assert command[-2:] == ["--output-dir", str(project / "results")]
    extra = command[command.index("--") + 1 :]
    assert ("--workers" in extra) != plan["node_example"]
    assert options["env"]["RAVEN_EXAMPLE_PROGRAM"] == name + ".py"
    assert options["env"]["RAVEN_EXAMPLE_GPU"] == str(int(plan["gpu"]))
    assert options["env"]["RAVEN_EXAMPLE_ACTIVITIES"] == str(int(plan["gpu"]))
    assert options["env"]["RAVEN_EXAMPLE_CPUS"] == str(plan["cpus"])
    assert options["cwd"] == project
    assert project.stat().st_mode & 0o777 == 0o700
    assert (project / "raven-view.json").stat().st_mode & 0o777 == 0o600
    for filename in ("common.py", "job.sh", name + ".py"):
        assert (project / filename).read_bytes() == (EXAMPLE / filename).read_bytes()


def test_scoped_configuration_literal_arguments_and_inherited_policy(
    captured, tmp_path, monkeypatch
):
    project = tmp_path / "a 'quoted' project;$(touch injected)"
    preserved = {
        "RAVEN_SITE_POLICY": "/etc/raven-site.json",
        "SLURM_CONF": "/etc/slurm.conf",
        "MODULEPATH": "/site/modules",
        "RAVEN_RUN_DIR": "/previous/run",
        "RAVEN_LAUNCH_PLAN": "/previous/plan.json",
    }
    removed = {
        "SBATCH_ACCOUNT": "OtherLab",
        "RAVEN_METRICS_FILE": "/another/metrics.jsonl",
        "RAVEN_TRACE_SKIP_AUTO": "1",
        "RAVEN_RUN_ID": "previous",
        "RAVEN_PROJECT_ROOT": "/another/project",
        "RAVEN_EXAMPLE_UNKNOWN": "inherited",
    }
    for key, value in {**preserved, **removed}.items():
        monkeypatch.setenv(key, value)
    assert (
        launch.main(
            [
                "--slurm",
                "--example",
                "multi_thread",
                "--workers",
                "3",
                "--directory",
                str(project),
                "--account",
                "LabX;$(touch injected)",
                "--partition",
                "GPU partition",
                "--",
                "--size",
                "64",
            ]
        )
        == 0
    )
    command, options = captured[0]
    assert command[command.index("--account") + 1] == "LabX;$(touch injected)"
    assert "--sbatch=--partition=GPU partition" in command
    assert "--sbatch=--cpus-per-task=3" in command
    assert options["env"]["XDG_CONFIG_HOME"] == str(project / ".example-config")
    assert all(options["env"].get(key) == value for key, value in preserved.items())
    assert all(key not in options["env"] for key in removed)
    assert not (project / "injected").exists()


@pytest.mark.parametrize(
    "option", ["--workers", "--workers=4", "--work", "--output-dir", "--out=other"]
)
def test_program_remainder_cannot_replace_reserved_topology_or_artifacts(
    option, tmp_path, monkeypatch
):
    project = tmp_path / "absent"
    monkeypatch.setattr(
        launch, "preflight", lambda *a: pytest.fail("Managed option reached preflight")
    )
    assert (
        launch.main(
            [
                "--local",
                "--example",
                "multi_thread",
                "--directory",
                str(project),
                "--",
                option,
                "4",
            ]
        )
        == 2
    )
    assert not project.exists()


def test_existing_project_is_never_reused(captured, tmp_path):
    project = tmp_path / "existing"
    project.mkdir()
    (project / "keep").write_text("user content")
    assert (
        launch.main(
            ["--local", "--example", "single_thread", "--directory", str(project)]
        )
        == 2
    )
    assert (project / "keep").read_text() == "user content"
    assert captured == []


def test_node_options_are_literal_and_gpu_recording_is_an_explicit_choice(
    captured, tmp_path
):
    assert (
        launch.main(
            [
                "--slurm",
                "--example",
                "multi_node",
                "--nodes",
                "3",
                "--mpi",
                "pmix_v4",
                "--directory",
                str(tmp_path / "node"),
            ]
        )
        == 0
    )
    assert captured[0][1]["env"]["RAVEN_EXAMPLE_NODES"] == "3"
    assert captured[0][1]["env"]["RAVEN_EXAMPLE_RANKS"] == "3"
    assert captured[0][1]["env"]["RAVEN_EXAMPLE_MPI"] == "pmix_v4"
    assert (
        launch.main(
            [
                "--slurm",
                "--example",
                "multi_gpu",
                "--workers",
                "3",
                "--no-activities",
                "--directory",
                str(tmp_path / "gpu"),
            ]
        )
        == 0
    )
    assert captured[1][1]["env"]["RAVEN_EXAMPLE_ACTIVITIES"] == "0"
    assert "--sbatch=--gpus-per-task=3" in captured[1][0]


@pytest.mark.parametrize("slurm", [False, True])
def test_gpu_preflight_only_probes_execution_devices_on_local_host(monkeypatch, slurm):
    commands = []
    monkeypatch.setattr(launch.helpers, "executable", lambda name: "/fixture/" + name)

    def probe(command, **options):
        commands.append(command)
        return subprocess.CompletedProcess(
            command,
            0,
            '["cpython",3,12]'
            if "sys.implementation.name" in command[2]
            else "GPU-capable framework",
            "",
        )

    monkeypatch.setattr(launch.subprocess, "run", probe)
    args = arguments("multi_gpu", local=not slurm)
    launch.preflight(args, launch.resources(args))
    assert "torch.version.cuda or torch.version.hip" in commands[1][2]
    assert ("torch.cuda.device_count()" in commands[1][2]) != slurm


def test_missing_backend_fails_before_project_creation(monkeypatch, tmp_path):
    monkeypatch.setattr(launch.helpers, "executable", lambda name: "/fixture/" + name)

    def probe(command, **options):
        version = "sys.implementation.name" in command[2]
        return subprocess.CompletedProcess(
            command,
            0 if version else 1,
            '["cpython",3,12]' if version else "",
            "CUDA/ROCm unavailable",
        )

    monkeypatch.setattr(launch.subprocess, "run", probe)
    project = tmp_path / "not-created"
    assert (
        launch.main(["--local", "--example", "multi_gpu", "--directory", str(project)])
        == 2
    )
    assert not project.exists()


def test_legacy_python_requires_explicit_consent(monkeypatch):
    monkeypatch.setattr(launch.helpers, "executable", lambda name: "/fixture/" + name)
    monkeypatch.setattr(
        launch.subprocess,
        "run",
        lambda *a, **kw: subprocess.CompletedProcess(a, 0, '["cpython",3,11]', ""),
    )
    args = arguments("single_thread", local=True)
    with pytest.raises(ValueError, match="legacy-profile consent"):
        launch.preflight(args, launch.resources(args))
    args.legacy_profile = True
    assert launch.preflight(args, launch.resources(args))[1] == "/fixture/python3"


def test_bash_job_uses_arrays_and_exact_launcher_commands(tmp_path):
    executable = tmp_path / "python with spaces"
    capture = tmp_path / "argv.json"
    executable.write_text(
        "#!"
        + sys.executable
        + "\nimport json,os,sys\nopen(os.environ['CAPTURE'],'w').write(json.dumps(sys.argv[1:]))\n"
    )
    executable.chmod(0o700)
    environment = dict(
        os.environ,
        RAVEN_EXAMPLE_PYTHON=str(executable),
        RAVEN_PROJECT_ROOT=str(tmp_path),
        RAVEN_EXAMPLE_PROGRAM="multi_gpu.py",
        RAVEN_EXAMPLE_ACTIVITIES="1",
        RAVEN_EXAMPLE_EXECUTION="local",
        CAPTURE=str(capture),
    )
    result = subprocess.run(
        ["bash", str(EXAMPLE / "job.sh"), "--size", "64", "literal;$(touch injected)"],
        env=environment,
        text=True,
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(capture.read_text()) == [
        "profile_runner.py",
        "multi_gpu.py",
        "--size",
        "64",
        "literal;$(touch injected)",
    ]
    assert not (tmp_path / "injected").exists()
