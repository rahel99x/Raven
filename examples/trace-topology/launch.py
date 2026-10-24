#!/usr/bin/env python3
"""Launch one explicit topology sample using Raven's existing launch workflow."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile

HERE = Path(__file__).resolve().parent
PROGRAMS = (
    "single_thread",
    "multi_thread",
    "single_core",
    "multi_core",
    "single_node",
    "multi_node",
    "single_gpu",
    "multi_gpu",
)
specification = importlib.util.spec_from_file_location(
    "raven_trace_lab_launch_helpers", HERE.parent / "trace-lab" / "demo.py"
)
helpers = importlib.util.module_from_spec(specification)
specification.loader.exec_module(helpers)


def parser():
    result = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    mode = result.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--local", action="store_true", help="run on this workstation without Slurm"
    )
    mode.add_argument("--slurm", action="store_true", help="submit on this Slurm host")
    result.add_argument("--example", choices=PROGRAMS, required=True)
    result.add_argument(
        "--workers",
        type=int,
        help="science threads/processes/devices, 1..8; separate from MPI ranks",
    )
    result.add_argument(
        "--ranks",
        type=int,
        help="single-node MPI ranks, 2..8; multi-node uses one rank per node",
    )
    result.add_argument(
        "--nodes", type=int, help="multi_node only: 2..8 real nodes (default: 2)"
    )
    result.add_argument(
        "--mpiexec", help="local single_node MPI launcher (default: mpiexec)"
    )
    result.add_argument(
        "--mpi",
        help="Slurm srun MPI plugin, for example pmix or pmi2; default: site setting",
    )
    result.add_argument(
        "--account", help="Slurm billing account; omitted means scheduler default"
    )
    result.add_argument(
        "--partition", help="Slurm partition; omitted means scheduler default"
    )
    result.add_argument(
        "--directory", help="new private project directory; existing paths are refused"
    )
    result.add_argument(
        "--python",
        default=os.environ.get("RAVEN_EXAMPLE_PYTHON", "python3"),
        help="application interpreter, also available on execution nodes",
    )
    result.add_argument(
        "--legacy-profile",
        action="store_true",
        help="explicit consent to Python 3.10/3.11's reduced-fidelity tracing adapter",
    )
    result.add_argument(
        "--no-activities",
        action="store_true",
        help="GPU examples: omit optional framework activity recording",
    )
    result.add_argument(
        "--quick",
        action="store_true",
        help="three steps, 64 cells, no deliberate pacing",
    )
    return result


def resources(args):
    """One reviewed topology contract supplies both reservation and execution."""
    node_example = args.example.endswith("_node")
    gpu = args.example.endswith("_gpu")
    if not args.slurm and (args.account or args.partition or args.mpi):
        raise ValueError("--account, --partition and --mpi require --slurm")
    if not node_example and any(
        value is not None for value in (args.ranks, args.nodes, args.mpiexec, args.mpi)
    ):
        raise ValueError(
            "--ranks, --nodes, --mpiexec and --mpi apply only to node examples"
        )
    if node_example and args.workers is not None:
        raise ValueError(
            "Node examples use --ranks/--nodes; each rank receives one CPU"
        )
    if args.mpi and not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", args.mpi):
        raise ValueError("--mpi must be a bounded Slurm MPI plugin name")
    if args.no_activities and not gpu:
        raise ValueError("--no-activities applies only to GPU examples")
    if args.example != "multi_node" and args.nodes is not None:
        raise ValueError("--nodes applies only to multi_node")
    if args.slurm and args.mpiexec:
        raise ValueError(
            "Slurm node examples use srun; choose its site plugin with --mpi"
        )
    if args.example == "multi_node" and not args.slurm:
        raise ValueError(
            "multi_node requires --slurm and at least two real nodes; use single_node for local MPI"
        )
    workers = (
        args.workers
        if args.workers is not None
        else (
            1 if args.example in {"single_thread", "single_gpu"} or node_example else 2
        )
    )
    if not 1 <= workers <= 8:
        raise ValueError("--workers must be between 1 and 8")
    if args.example in {"single_thread", "single_gpu"} and workers != 1:
        raise ValueError(args.example + " requires exactly one worker")
    if (
        args.example in {"multi_thread", "single_core", "multi_core", "multi_gpu"}
        and workers < 2
    ):
        raise ValueError(args.example + " requires at least two workers")
    nodes = (
        args.nodes
        if args.nodes is not None
        else 2
        if args.example == "multi_node"
        else 1
    )
    if not 1 <= nodes <= 8 or args.example == "multi_node" and nodes < 2:
        raise ValueError("multi_node needs 2..8 nodes")
    ranks = (
        args.ranks
        if args.ranks is not None
        else nodes
        if args.example == "multi_node"
        else 2
        if node_example
        else 1
    )
    if node_example and not 2 <= ranks <= 8:
        raise ValueError("Node examples need 2..8 MPI ranks")
    if args.example == "multi_node" and ranks != nodes:
        raise ValueError(
            "multi_node uses exactly one rank per node: --ranks must equal --nodes"
        )
    return dict(
        nodes=nodes,
        ranks=ranks,
        workers=workers,
        cpus=1 if args.example == "single_core" or node_example or gpu else workers,
        gpu=gpu,
        gpus=workers if gpu else 0,
        memory="2G" if gpu else "512M",
        node_example=node_example,
    )


def preflight(args, plan):
    raven, python = helpers.executable("raven"), helpers.executable(args.python)
    result = subprocess.run(
        [
            python,
            "-c",
            "import json,sys; print(json.dumps([sys.implementation.name,*sys.version_info[:2]]))",
        ],
        text=True,
        capture_output=True,
        timeout=10,
        check=True,
    )
    implementation, major, minor = json.loads(result.stdout)
    if implementation != "cpython" or (major, minor) < (3, 10):
        raise ValueError("Tracing needs CPython 3.10 or newer")
    if (major, minor) < (3, 12) and not args.legacy_profile:
        raise ValueError(
            "CPython 3.10/3.11 requires explicit --legacy-profile consent, or choose CPython 3.12+"
        )
    launcher = None
    if args.slurm:
        helpers.executable("sbatch")
        if plan["node_example"] or args.example.endswith("_core"):
            helpers.executable("srun")
    if plan["node_example"]:
        # Import the package, not MPI: MPI_Init belongs inside the owned step,
        # not on a cluster's submission/login host.
        command = "import mpi4py; print(mpi4py.__version__)"
        explanation = (
            "Node examples require mpi4py and a compatible MPI runtime in --python"
        )
        if args.local:
            launcher = helpers.executable(args.mpiexec or "mpiexec")
    elif plan["gpu"]:
        command = "import torch; assert torch.version.cuda or torch.version.hip, 'PyTorch lacks CUDA/ROCm support'"
        if args.local:
            command += (
                "; assert torch.cuda.is_available(), 'No accessible GPU device'; assert torch.cuda.device_count()>="
                + str(plan["gpus"])
                + ", 'Not enough visible GPU devices'"
            )
        explanation = "GPU examples require a CUDA/ROCm-enabled PyTorch build and sufficient accessible execution devices"
    else:
        command = None
    if command:
        result = subprocess.run(
            [python, "-c", command],
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        if result.returncode:
            raise ValueError(
                explanation + "; no fallback is performed. " + result.stderr[-1500:]
            )
    return raven, python, launcher


def run(args, program_args):
    plan = resources(args)
    # Reject abbreviated managed arguments as well as their full spellings.
    for word in program_args:
        if word.startswith("--") and any(
            name.startswith(word.split("=", 1)[0])
            for name in ("--workers", "--output-dir")
        ):
            raise ValueError(
                "The helper owns --workers and --output-dir; use its --workers/--directory options"
            )
    for value in (args.directory, args.account, args.partition):
        if value:
            helpers.safe_text(value)
    raven, python, launcher = preflight(args, plan)
    if args.directory:
        project = Path(args.directory).expanduser().absolute()
        project.mkdir(mode=0o700, parents=True, exist_ok=False)
        project = project.resolve()
    else:
        parent = Path.home() / "raven-examples" / "trace-topology"
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        project = Path(
            tempfile.mkdtemp(prefix=args.example + "-", dir=parent)
        ).resolve()
    for path in sorted(HERE.glob("*.py")):
        # The launcher belongs to the checkout and imports its sibling helper;
        # scientific programs and their frozen dependencies are self-contained.
        if path.name not in {"launch.py", "view.py"}:
            shutil.copy2(path, project / path.name)
    shutil.copy2(HERE / "job.sh", project / "job.sh")
    configuration = project / "raven-view.json"
    config = helpers.example_config(args.local)
    with configuration.open("x", encoding="utf-8") as output:
        output.write(json.dumps(config, indent=2) + "\n")
    configuration.chmod(0o600)
    receipt = project / "launch.json"
    command = [
        raven,
        "--site",
        config["profile"],
        "--config",
        str(configuration),
        "launch",
        str(project / "job.sh"),
        "--project",
        str(project),
        "--trace-profile",
        "topology",
        "--trace-helpers",
        "--cpu-memory",
        "--research",
        "experiment",
        "--receipt-file",
        str(receipt),
        "--no-export-on-finish",
        "--gpu" if plan["gpu"] else "--no-gpu",
        "--trace-adapter",
        "python-legacy-profile" if args.legacy_profile else "python-monitoring",
    ]
    if args.local:
        command.append("--local")
    else:
        for key, value in (
            ("nodes", plan["nodes"]),
            ("ntasks", plan["ranks"]),
            ("cpus-per-task", plan["cpus"]),
            ("mem", plan["memory"]),
            ("time", "00:05:00"),
            ("job-name", "raven-" + args.example.replace("_", "-")),
        ):
            command.append("--sbatch=--" + key + "=" + str(value))
        if args.example == "multi_node":
            command.append("--sbatch=--ntasks-per-node=1")
        if args.example in {"single_core", "multi_core"}:
            command.append("--sbatch=--hint=nomultithread")
        if plan["gpu"]:
            command.append("--sbatch=--gpus-per-task=" + str(plan["gpus"]))
    if args.account:
        command.extend(["--account", args.account])
    if args.partition:
        command.append("--sbatch=--partition=" + args.partition)
    command.append("--")
    if args.quick:
        command.extend(["--seconds", "0", "--steps", "3", "--size", "64"])
    command.extend(program_args)
    if not plan["node_example"]:
        command.extend(["--workers", str(plan["workers"])])
    command.extend(["--output-dir", str(project / "results")])
    private_config = project / ".example-config"
    private_config.mkdir(mode=0o700)
    recording = {
        "RAVEN_METRICS_FILE",
        "RAVEN_TRACE_SKIP_AUTO",
        "RAVEN_RUN_ID",
        "RAVEN_PROJECT_ROOT",
    }
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("SBATCH_")
        and key not in recording
        and not key.startswith("RAVEN_EXAMPLE_")
    }
    environment.update(
        RAVEN_TRACE_AUTO="1",
        XDG_CONFIG_HOME=str(private_config),
        RAVEN_EXAMPLE_PYTHON=python,
        RAVEN_EXAMPLE_PROGRAM=args.example + ".py",
        RAVEN_EXAMPLE_EXECUTION="slurm" if args.slurm else "local",
        RAVEN_EXAMPLE_WORKERS=str(plan["workers"]),
        RAVEN_EXAMPLE_RANKS=str(plan["ranks"]),
        RAVEN_EXAMPLE_CPUS=str(plan["cpus"]),
        RAVEN_EXAMPLE_NODES=str(plan["nodes"]),
        RAVEN_EXAMPLE_MPI=args.mpi or "",
        RAVEN_EXAMPLE_GPU="1" if plan["gpu"] else "0",
        RAVEN_EXAMPLE_ACTIVITIES="0" if args.no_activities or not plan["gpu"] else "1",
    )
    if launcher:
        environment["RAVEN_EXAMPLE_MPIEXEC"] = launcher
    if plan["gpu"]:
        # GPU samples submit independent device streams from one host thread.
        # Keep optional framework CPU pools modest without replacing user choices.
        environment.setdefault("OMP_NUM_THREADS", "1")
        environment.setdefault("MKL_NUM_THREADS", "1")
    print(
        "Private launch defaults preserve site policy and saved Raven settings.",
        flush=True,
    )
    print("Project: " + str(project) + "\nCommand: " + shlex.join(command), flush=True)
    result = subprocess.run(command, cwd=project, env=environment, check=False)
    if receipt.is_file():
        print("\nReceipt: " + str(receipt))
        print("View: " + shlex.join(["bash", str(HERE / "view.sh"), str(receipt)]))
        print("After completion: add --history to the same viewer command.")
    else:
        print(
            "No receipt was written; inspect the launch output before resubmitting: "
            + str(project),
            file=sys.stderr,
        )
    return result.returncode


def main(argv=None):
    arguments = list(sys.argv[1:] if argv is None else argv)
    extra = []
    if "--" in arguments:
        index = arguments.index("--")
        arguments, extra = arguments[:index], arguments[index + 1 :]
    args = parser().parse_args(arguments)
    try:
        return run(args, extra)
    except (OSError, ValueError, TypeError, subprocess.SubprocessError) as error:
        text = "".join(char if char.isprintable() else "?" for char in str(error))
        print("Trace topology: " + text, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
