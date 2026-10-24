"""Trace command dispatch above capture, native transports and read services."""

from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import runpy
import sys
import threading
from .capture import Recorder, PythonCapture, default_job_id, register_trace


def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]
    from ..execution.admission import cli_admission

    blocked = cli_admission(["trace", *argv])
    if blocked is not None:
        print(json.dumps(blocked, indent=2))
        return 3
    if argv[:1] and argv[0] in {"workspace", "import-activity", "profile", "sample"}:
        from .workspace_cli import main as workspace_main

        return workspace_main(argv)
    if argv[:1] == ["cleanup-runtime"]:
        from ..runtime_cleanup import main as cleanup_main

        return cleanup_main(argv[1:])
    if argv and argv[0] == "capabilities":
        from .capabilities import main as capabilities_main

        return capabilities_main(argv[1:])
    if argv and argv[0] == "summary":
        from .summary import main as summary_main

        return summary_main(argv[1:])
    if argv is not None and argv and argv[0] == "native":
        from .native import main as native_main

        return native_main(argv[1:])
    if argv and argv[0] == "mpi":
        from .mpi import main as mpi_main

        return mpi_main(argv[1:])
    parser = argparse.ArgumentParser(
        prog="raven trace",
        description="Opt-in event recording; no debugger attach or scheduler modification",
        epilog="Use raven trace capabilities [--json] to inspect supported capture features and platform limits.",
    )
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser(
        "workspace",
        help="Explore an indexed whole retained run or open its private live browser",
    )
    sub.add_parser(
        "import-activity",
        help="Import bounded device/CPU activities from Chrome/Perfetto JSON",
    )
    sub.add_parser(
        "profile",
        help="Explicit optional profiling in the application Python environment",
    )
    sub.add_parser(
        "sample",
        help="Sample stacks of a newly owned program on supported perf systems",
    )
    launch = sub.add_parser(
        "run", help="Record Python execution in a new process-owned journal"
    )
    launch.add_argument("--project", default=os.getcwd())
    launch.add_argument("--job-id", default=default_job_id())
    launch.add_argument("--attempt", default=os.environ.get("RAVEN_RUN_ID"))
    launch.add_argument(
        "--cluster", default=os.environ.get("SLURM_CLUSTER_NAME", "local")
    )
    launch.add_argument("--slurm-submit")
    launch.add_argument("--slurm-start")
    launch.add_argument("--max-mib", type=int, default=64)
    launch.add_argument("--legacy-profile", action="store_true")
    launch.add_argument(
        "--cpu-placement",
        action="store_true",
        help="Observe the calling thread CPU at event times on Linux; does not record continuous residency",
    )
    launch.add_argument("script", nargs=argparse.REMAINDER)
    inspect = sub.add_parser(
        "inspect", help="Read checked events and reconstructed stacks"
    )
    inspect.add_argument("path")
    inspect.add_argument("--json", action="store_true")
    export = sub.add_parser(
        "export",
        help="Export a verified prefix as an offline HTML timeline, Perfetto, or speedscope",
    )
    export.add_argument("path")
    export.add_argument(
        "--format", choices=("perfetto", "speedscope", "html"), default="perfetto"
    )
    export.add_argument(
        "--output",
        required=True,
        help="new private output file; existing files are never replaced",
    )
    register = sub.add_parser(
        "register", help="Register an existing project-contained trace manifest"
    )
    register.add_argument("--project", required=True)
    register.add_argument("path")
    args = parser.parse_args(argv)
    if args.action == "export":
        from ..execution.policy import router_for
        from .export import export as export_trace

        try:
            result = export_trace(
                args.path, args.output, format=args.format, managed=router_for().managed
            )
        except (OSError, ValueError) as exc:
            print(f"Raven trace export: {exc}", file=sys.stderr)
            return 2
        print(json.dumps(result, indent=2))
        return 0
    if args.action == "inspect":
        from ..execution.policy import router_for
        from .stream_export import inspect as inspect_trace

        try:
            inspect_trace(
                args.path, json_output=args.json, managed=router_for().managed
            )
        except (OSError, ValueError) as exc:
            print(f"Raven trace inspect: {exc}", file=sys.stderr)
            return 2
        return 0
    if args.action == "register":
        register_trace(args.project, args.path)
        return 0
    script = list(args.script)
    if script and script[0] == "--":
        script.pop(0)
    if not script:
        parser.error("run requires -- SCRIPT.py [arguments]")
    target = Path(script[0]).absolute()
    if not target.is_file():
        parser.error("run requires an existing Python script")
    coverage = {
        "adapter": "python-profile" if args.legacy_profile else "python-sys-monitoring",
        "scope": "Python frames in this launched process and newly observed threads; native extensions, device execution and fork children are not covered",
        "limitations": "Legacy profile cannot distinguish yield from return or exception unwind"
        if args.legacy_profile
        else "Only declared Python frame events; pre-existing frames are unknown",
    }
    recorder = Recorder(
        args.project,
        job_id=args.job_id,
        attempt=args.attempt,
        cluster=args.cluster,
        max_bytes=args.max_mib << 20,
        coverage=coverage,
        cpu_placement=args.cpu_placement,
        identity_extra={
            key: value
            for key, value in {
                "slurm_submit": args.slurm_submit,
                "slurm_start": args.slurm_start,
                "run_id": os.environ.get("RAVEN_RUN_ID"),
            }.items()
            if value
        },
    )
    capture = PythonCapture(recorder, legacy=args.legacy_profile)
    old_argv, old_path = sys.argv, list(sys.path)
    print(f"Raven trace: {recorder.path}", file=sys.stderr)
    started = False
    try:
        sys.argv = script
        sys.path.insert(0, str(target.parent))
        capture.start()
        started = True
        runpy.run_path(str(target), run_name="__main__")
    finally:
        capture.stop()
        if not started:
            recorder._fail(
                "ERROR",
                "Python capture could not start; the application was not launched",
            )
        remaining = [
            thread
            for thread in threading.enumerate()
            if thread is not threading.current_thread()
            and thread is not recorder._flush_thread
            and thread.is_alive()
        ]
        if remaining:
            recorder._fail(
                "PARTIAL",
                "Python script returned while other threads remained alive; later execution was not captured",
            )
        sys.argv, sys.path[:] = old_argv, old_path
        recorder.close()
    return 0
