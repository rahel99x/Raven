#!/usr/bin/env python3
"""Launch one copied example and reopen only the attempt in its exact receipt."""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
MAX_JSON = 1024 * 1024


def safe_text(value):
    if not isinstance(value, str) or not value or not value.isprintable():
        raise ValueError("Paths and identifiers must be nonempty text without terminal control characters")
    return value


def executable(name):
    found = shutil.which(safe_text(name))
    if found is None:
        raise ValueError(f"Executable {name!r} was not found; install it or correct PATH first")
    # Keep a venv executable's path: resolving its symlink would bypass the venv.
    return os.path.abspath(found)


def read_json(path):
    # Never block on a pipe presented as a receipt, nor allocate an unbounded file.
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_JSON:
            raise ValueError(f"Expected a regular JSON file of at most {MAX_JSON} bytes: {path}")
        data = handle.read(MAX_JSON + 1)
    if len(data) > MAX_JSON:
        raise ValueError(f"JSON file exceeded its size limit: {path}")
    value = json.loads(data)
    if not isinstance(value, dict):
        raise TypeError(f"Expected a JSON object: {path}")
    return value


def example_config(local):
    return {"schema": "raven.config/v1", "profile": "desktop" if local else "default-slurm",
            "site": {"kind": "desktop" if local else "slurm"},
            "connection": {"transport": "none" if local else "local", "host": "", "user": ""}}


def check_variant(args, python):
    """Check optional software before creating a project or requesting resources."""
    if args.variant != "mpi" and (args.ranks is not None or args.mpiexec is not None):
        raise ValueError("--ranks and --mpiexec apply only with --variant mpi")
    if args.variant == "mpi":
        if args.ranks is not None and not 1 <= args.ranks <= 8:
            raise ValueError("This small MPI example supports --ranks 1..8")
        launcher = executable(args.mpiexec or "mpiexec")
        command = "from mpi4py import MPI; print(MPI.Get_library_version())"
        explanation = "MPI needs mpi4py and a compatible MPI runtime in the selected --python environment"
    elif args.variant == "cuda":
        launcher = None
        command = "import torch; assert torch.version.cuda, 'This PyTorch build has no CUDA support'"
        if not args.slurm:
            command += "; assert torch.cuda.is_available(), 'No CUDA device is available on this workstation'"
        explanation = "CUDA needs a CUDA-enabled PyTorch installation in the selected --python environment"
    else:
        return None
    probe = subprocess.run([python, "-c", command], text=True, capture_output=True, timeout=30, check=False)
    if probe.returncode:
        raise ValueError(explanation + ". Load the site's environment or choose --python; no fallback is performed. " + probe.stderr[-1500:])
    return launcher


def run_example(args, program_args):
    raven = executable("raven")
    python = executable(args.python)
    probe = subprocess.run([python, "-c", "import json,sys; print(json.dumps([sys.implementation.name,*sys.version_info[:2]]))"],
                           check=True, text=True, capture_output=True, timeout=10)
    implementation, major, minor = json.loads(probe.stdout)
    if implementation != "cpython" or (major, minor) < (3, 10):
        raise ValueError("This tracing example requires CPython 3.10 or newer")
    if (major, minor) < (3, 12) and not args.legacy_profile:
        raise ValueError("CPython 3.10/3.11 requires explicit --legacy-profile consent, or choose --python with CPython 3.12+")
    if args.slurm:
        executable("sbatch")
    elif args.account or args.partition:
        raise ValueError("--account and --partition apply only with --slurm")
    mpi_launcher = check_variant(args, python)
    # The helper owns artifact placement; argparse also accepts abbreviated flags.
    if any(arg.startswith("--") and ("--output-dir".startswith(arg.split("=", 1)[0])) for arg in program_args):
        raise ValueError("The helper owns --output-dir; use --directory to choose a new project")
    for value in (args.directory, args.account, args.partition):
        if value:
            safe_text(value)
    if args.directory:
        project = Path(args.directory).expanduser().absolute()
        project.mkdir(mode=0o700, parents=True, exist_ok=False)
        project = project.resolve()
    else:
        base = Path.home() / "raven-examples" / "trace-lab"
        base.mkdir(mode=0o700, parents=True, exist_ok=True)
        project = Path(tempfile.mkdtemp(prefix="run-", dir=base)).resolve()
    script = {"cpu": "job.sh", "mpi": "job-mpi.sh", "cuda": "job-cuda.sh"}[args.variant]
    files = ["heat_lab.py", script]
    if args.variant != "cpu":
        files.append(args.variant + "_heat_lab.py")
    for name in files:
        shutil.copy2(HERE / name, project / name)
    configuration = project / "raven-view.json"
    config = example_config(local=not args.slurm)
    with configuration.open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(config, indent=2) + "\n")
    configuration.chmod(0o600)
    receipt = project / "launch.json"
    command = [raven, "--site", config["profile"], "--config", str(configuration), "launch", str(project / script), "--project", str(project),
               "--trace", "--trace-helpers", "--cpu-memory", "--research", "experiment", "--receipt-file", str(receipt)]
    if not args.slurm:
        command.append("--local")
    if args.cpu_placement:
        command.append("--trace-cpu-placement")
    command.extend(["--trace-adapter", "python-legacy-profile" if args.legacy_profile else "python-monitoring",
                    "--trace-scope", "project", "--gpu" if args.variant == "cuda" else "--no-gpu", "--no-export-on-finish"])
    if args.account:
        command.extend(["--account", args.account])
    if args.partition:
        command.append("--sbatch=--partition=" + args.partition)
    if args.slurm and args.variant == "mpi":
        command.append("--sbatch=--ntasks=" + str(args.ranks or 2))
    command.append("--")
    if args.quick:
        command.extend(["--grid", "16", "--steps", "8", "--pause", "0"])
        if args.variant == "cpu":
            command.extend(["--workers", "1"])
    command.extend([*program_args, "--output-dir", str(project / "results")])
    # Scope user launch recipes too: --config only selects the dashboard config,
    # while automation.json is loaded separately from XDG_CONFIG_HOME. System
    # policy, module paths, Slurm configuration and credentials remain inherited.
    config_root = project / ".example-config"
    config_root.mkdir(mode=0o700)
    # Retain prior RUN_DIR/LAUNCH_PLAN: Raven needs both to verify ownership
    # before cleaning a previous run's startup path during nested launches.
    recording_variables = {"RAVEN_METRICS_FILE", "RAVEN_TRACE_SKIP_AUTO", "RAVEN_RUN_ID", "RAVEN_PROJECT_ROOT"}
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith("SBATCH_") and key not in recording_variables}
    environment.update(RAVEN_EXAMPLE_PYTHON=python, RAVEN_TRACE_AUTO="1", XDG_CONFIG_HOME=str(config_root))
    if mpi_launcher:
        environment.update(RAVEN_EXAMPLE_MPIEXEC=mpi_launcher, RAVEN_EXAMPLE_MPI_RANKS=str(args.ranks or 2))
    print("Using private launch defaults; inherited SBATCH_* and recording-target overrides do not apply to this example.", flush=True)
    print(f"Project: {project}\nCommand: {shlex.join(command)}", flush=True)
    result = subprocess.run(command, cwd=project, env=environment, check=False)
    if receipt.is_file():
        print(f"\nReceipt: {receipt}")
        print("View: " + shlex.join(["bash", str(HERE / "view.sh"), str(receipt)]))
        if args.slurm:
            print("After completion, add --history to that same viewer command.")
    else:
        print(f"No launch receipt was written. Inspect the launch output and {project}; do not assume submission failed or automatically resubmit.", file=sys.stderr)
    return result.returncode


def receipt_context(filename):
    receipt = read_json(Path(filename).expanduser())
    if receipt.get("schema") != "raven.launch-receipt/v1" or receipt.get("execution") not in ("local", "slurm"):
        raise ValueError("Expected a Raven local or Slurm launch receipt")
    project = Path(safe_text(receipt.get("project_root")))
    run = Path(safe_text(receipt.get("run_dir")))
    run_id = safe_text(receipt.get("run_id"))
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", run_id):
        raise ValueError("Invalid run ID in launch receipt")
    if not project.is_absolute() or not run.is_absolute():
        raise ValueError("Receipt project and run paths must be absolute")
    project = project.resolve()
    expected = project / "runs" / run_id
    if run != expected or run.resolve() != expected:
        raise ValueError("Receipt must identify its exact project/runs/RUN_ID without a symlink escape")
    if receipt["execution"] == "slurm" and not re.fullmatch(r"[0-9]+", str(receipt.get("job_id", ""))):
        raise ValueError("Expected a single Slurm job ID in this example receipt")
    return receipt, project, run


def trace_sources(run, receipt):
    index_path = run / "trace" / "index.json"
    if index_path.resolve().is_relative_to(run) is False:
        raise ValueError("Trace index leaves the receipt's run directory")
    if not index_path.exists():
        return []
    index = read_json(index_path)
    sources = index.get("sources")
    if index.get("schema") != "raven.trace-index/v1" or not isinstance(sources, list) or len(sources) > 512:
        raise ValueError("Invalid or oversized trace index")
    found = []
    for entry in sources:
        if not isinstance(entry, dict):
            raise TypeError("Invalid trace index entry")
        if entry.get("schema") != "raven.trace/v1":
            if entry.get("schema") == "raven.trace-gap/v1":
                print("Capture notice: " + safe_text(entry.get("reason", "unavailable")))
            continue
        if entry.get("run_id") != receipt["run_id"]:
            raise ValueError("Trace index names another run attempt")
        relative = Path(safe_text(entry.get("path")))
        manifest = (run / relative).resolve()
        if relative.is_absolute() or not manifest.is_relative_to(run):
            raise ValueError("Trace manifest leaves the receipt's run directory")
        process = entry.get("process", {})
        if not isinstance(process, dict) or type(process.get("pid")) is not int or process["pid"] <= 0:
            raise ValueError("Trace entry has no valid process PID")
        role = safe_text(process.get("role", "unknown"))
        status = safe_text(entry.get("status", "unknown"))
        document = read_json(manifest)
        identity = document.get("identity", {})
        expected = {"attempt": receipt["run_id"], "pid": process["pid"],
                    "stream": safe_text(entry.get("stream")), "job_id": str(receipt.get("job_id", "local"))}
        # Imported activity windows omit the redundant run_id == attempt alias.
        # A supplied different run ID is still a conflicting run identity.
        if isinstance(identity, dict) and identity.get("run_id", receipt["run_id"]) != receipt["run_id"]:
            raise ValueError("Trace manifest names another run attempt")
        if document.get("schema") != "raven.trace/v1" or not isinstance(identity, dict) or any(identity.get(key) != value for key, value in expected.items()):
            raise ValueError("Trace manifest identity differs from the exact receipt and process index")
        rank = process.get("rank")
        rank_label = f", rank {rank}" if type(rank) is int else ""
        print(f"Process {process['pid']} ({role}{rank_label}, {status}): {manifest}")
        found.append((process, manifest))
    return found


def view_example(args):
    safe_text(args.receipt)
    receipt, project, run = receipt_context(args.receipt)
    local = receipt["execution"] == "local"
    configuration = project / "raven-view.json"
    config = read_json(configuration)
    if config != example_config(local):
        raise ValueError("The example's private view configuration no longer matches its original scoped settings")
    print(f"Project: {project}\nRun: {receipt['run_id']}\nRetained files: {run}")
    print("In Raven's command palette, enter these in order (wait for project inventory):")
    print(":project " + shlex.quote(str(project)))
    print(":run select " + receipt["run_id"])
    if local:
        print("Standalone run: scientific metrics open in Research / Experiment; no scheduler Jobs/History row exists.")
    else:
        print(f"Then open {'History' if args.history else 'Jobs (or Recents/History after completion)'}, select job {receipt['job_id']}, and choose Trace in Details.")
    sources = trace_sources(run, receipt)
    if args.inspect or args.process is not None or local:
        if args.process is not None:
            sources = [item for item in sources if item[0]["pid"] == args.process]
            if not sources:
                raise ValueError("That PID has no registered trace in this exact run; use a displayed PID")
        if not sources:
            print("No process recording is registered yet. The job may be queued or capture unavailable; inspect its scheduler state, logs, and retained trace diagnostics.")
            return 0
        sources.sort(key=lambda item: (item[0].get("role") == "helper", item[0].get("rank", 0) != 0))
        command = ["raven", "--site", config["profile"], "--config", str(configuration), "trace", "inspect", str(sources[0][1])]
        print("Inspect (bounded prefix, not every recorded event): " + shlex.join(command), flush=True)
        if not args.no_open:
            command[0] = executable(command[0])
            return subprocess.run(command, cwd=project, check=False).returncode
    else:
        command = ["ravenl", "--site", config["profile"], "--config", str(configuration), "--workdir", str(project), "--tab", "history" if args.history else "jobs"]
        print("Open: " + shlex.join(command), flush=True)
        if not args.no_open and sys.stdin.isatty() and sys.stdout.isatty():
            command[0] = executable(command[0])
            return subprocess.run(command, cwd=project, check=False).returncode
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="action", required=True)
    run = actions.add_parser("run", help="copy and launch a new, isolated example")
    mode = run.add_mutually_exclusive_group(required=True)
    mode.add_argument("--slurm", action="store_true", help="submit on this Slurm host")
    mode.add_argument("--local", action="store_true", help="run synchronously without Slurm")
    run.add_argument("--variant", choices=("cpu", "mpi", "cuda"), default="cpu", help="workload backend (default: cpu; normal runs last about two minutes)")
    run.add_argument("--ranks", type=int, help="MPI process count, 1..8 (default: 2)")
    run.add_argument("--mpiexec", help="compatible MPI launcher executable (default: mpiexec)")
    run.add_argument("--directory", help="new project directory (default: unique ~/raven-examples/trace-lab/run-*)")
    run.add_argument("--account", help="Slurm billing account; omit for scheduler defaults")
    run.add_argument("--partition", help="Slurm partition; omit for scheduler defaults")
    run.add_argument("--python", default=os.environ.get("RAVEN_EXAMPLE_PYTHON", "python3"), help="application Python executable, available on compute nodes")
    run.add_argument("--legacy-profile", action="store_true", help="consent to the legacy Python profile adapter")
    run.add_argument("--cpu-placement", action="store_true", help="record optional Linux CPU/socket/core observations at trace events")
    run.add_argument("--quick", action="store_true", help="16x16 grid, 8 steps, no pacing, one worker")
    view = actions.add_parser("view", help="reopen the exact run named by a launch receipt")
    view.add_argument("receipt")
    view.add_argument("--history", action="store_true", help="open Slurm History instead of Jobs")
    view.add_argument("--inspect", action="store_true", help="inspect a bounded retained process stream")
    view.add_argument("--process", type=int, help="inspect this registered PID instead of the main process")
    view.add_argument("--no-open", action="store_true", help="print paths and instructions without starting Raven")
    arguments = list(sys.argv[1:] if argv is None else argv)
    program_args = []
    if arguments[:1] == ["run"] and "--" in arguments:
        delimiter = arguments.index("--")
        arguments, program_args = arguments[:delimiter], arguments[delimiter + 1:]
    args = parser.parse_args(arguments)
    try:
        return run_example(args, program_args) if args.action == "run" else view_example(args)
    except (OSError, ValueError, TypeError, subprocess.SubprocessError) as exc:
        # Paths and external diagnostics may contain terminal escapes.
        message = "".join(char if char.isprintable() else "?" for char in str(exc))
        print("Trace lab: " + message, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
