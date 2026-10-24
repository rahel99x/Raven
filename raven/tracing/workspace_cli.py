"""Bounded trace workspace, imported activities and explicit profiling launchers."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
import time


def _identity_arguments(parser, *, required=True):
    parser.add_argument("--project", required=required)
    parser.add_argument("--job-id", required=required)
    parser.add_argument("--attempt", required=required)
    parser.add_argument("--cluster", default="local")
    parser.add_argument("--node")


def _workspace_parser():
    parser = argparse.ArgumentParser(
        prog="raven trace workspace",
        description="View verified live and retained run recordings; no debugger or scheduler attachment",
    )
    parser.add_argument("paths", nargs="+")
    parser.add_argument(
        "--index", type=Path, help="Private local rebuildable SQLite index"
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print a bounded JSON result (also the default)",
    )
    parser.add_argument("--start-ns", type=int)
    parser.add_argument("--end-ns", type=int)
    parser.add_argument("--clock-id")
    parser.add_argument("--source", action="append", dest="source_ids")
    parser.add_argument("--thread")
    parser.add_argument("--node", help="Recorded producer node")
    parser.add_argument("--process", help="Recorded producer PID")
    parser.add_argument(
        "--cpu",
        dest="logical_cpu",
        type=int,
        help="Logical CPU evidence and matching stack prerequisites",
    )
    parser.add_argument(
        "--device", dest="device_id", help="Stable device ID or reported UUID/ordinal"
    )
    parser.add_argument(
        "--kind", dest="kinds", action="append", help="Recorded event kind; repeatable"
    )
    parser.add_argument("--task", help="Recorded logical task identity")
    parser.add_argument("--rank", help="Recorded MPI rank")
    parser.add_argument("--find", default="", dest="search")
    parser.add_argument("--limit", type=int, default=2000)
    parser.add_argument("--functions", action="store_true")
    parser.add_argument("--overview", action="store_true")
    parser.add_argument("--detail")
    parser.add_argument(
        "--symbol-frame",
        type=int,
        help="Original sampled-stack frame index for --detail symbol resolution",
    )
    parser.add_argument(
        "--call-tree",
        action="store_true",
        help="Aggregate validated recorded nesting, scoped to this returned range",
    )
    parser.add_argument(
        "--bottom-up",
        action="store_true",
        help="Aggregate recorded caller paths from leaves upward",
    )
    parser.add_argument(
        "--causal",
        metavar="EVENT_ID",
        help="Observed dependency slice through an exact event; missing evidence limits the result",
    )
    parser.add_argument(
        "--counters",
        action="store_true",
        help="Counter/scientific phase lens with original units and clocks",
    )
    parser.add_argument(
        "--hints",
        action="store_true",
        help="Evidence-linked investigation prompts, not a performance diagnosis",
    )
    parser.add_argument(
        "--metrics",
        action="store_true",
        help="Defined resource metrics; absent denominators remain unavailable",
    )
    parser.add_argument(
        "--binary",
        type=Path,
        help="Explicit recorded-build ELF for --detail symbol resolution",
    )
    parser.add_argument(
        "--samples",
        action="store_true",
        help="Weighted sample shares; never exact per-call durations",
    )
    parser.add_argument(
        "--capacity",
        type=Path,
        help="Explicit bounded resource/coverage context JSON; no allocation is requested",
    )
    parser.add_argument(
        "--source-root",
        type=Path,
        help="Explicit source root; contents require recorded digest/revision proof",
    )
    parser.add_argument(
        "--projection", choices=("execution", "cpu", "gpu", "node"), default="execution"
    )
    parser.add_argument(
        "--baseline", nargs="+", help="Owner-registered baseline trace manifests"
    )
    parser.add_argument(
        "--comparison-context",
        type=Path,
        help="Bounded JSON comparability and resource metadata",
    )
    parser.add_argument(
        "--serve",
        action="store_true",
        help="Start an owned read-only loopback browser viewer until Ctrl-C",
    )
    parser.add_argument("--open-browser", action="store_true")
    parser.add_argument(
        "--max-seconds",
        type=float,
        default=60,
        help="Initial verified indexing work budget, 0.001–60 seconds",
    )
    return parser


def _dump(result):
    print(json.dumps(result, ensure_ascii=True, allow_nan=False, indent=2), flush=True)


def workspace(argv):
    from .workspace_session import WorkspaceSession
    from ..execution.policy import router_for

    parser = _workspace_parser()
    args = parser.parse_args(argv)
    if not 0.001 <= args.max_seconds <= 60 or not 1 <= args.limit <= 10_000:
        parser.error("Choose a 1 ms–60 s indexing budget and 1–10,000 result limit")
    if args.open_browser and not args.serve:
        parser.error("--open-browser requires --serve")
    if (args.binary or args.source_root) and not args.detail:
        parser.error("--binary and --source-root require --detail EVENT_ID")
    if args.symbol_frame is not None and (
        not args.detail or not 0 <= args.symbol_frame < 4096
    ):
        parser.error(
            "--symbol-frame requires --detail and an original frame index 0–4095"
        )
    light = router_for().managed
    options = {
        "mode": "explore",
        "start_ns": args.start_ns,
        "end_ns": args.end_ns,
        "clock_id": args.clock_id,
        "source_ids": args.source_ids,
        "thread": args.thread,
        **{
            key: getattr(args, key)
            for key in (
                "node",
                "process",
                "logical_cpu",
                "device_id",
                "kinds",
                "task",
                "rank",
            )
        },
        "search": args.search,
        "limit": min(args.limit, 2000) if light else args.limit,
        "perspective": args.projection,
        "functions": args.functions,
        "overview": args.overview,
        "selected": args.detail,
        "call_tree": args.call_tree,
        "bottom_up": args.bottom_up,
        "causal": args.causal,
        "counters": args.counters,
        "hints": args.hints,
        "metrics": args.metrics,
        "samples": args.samples,
        "binary": args.binary,
        "source_root": args.source_root,
        "sample_frame_index": args.symbol_frame,
    }
    session = WorkspaceSession(args.paths, index_path=args.index, light=light)
    if args.comparison_context and not args.baseline:
        session.close()
        parser.error("--comparison-context requires --baseline")
    deadline = time.monotonic() + args.max_seconds
    try:
        if args.capacity:
            from .protocol import read_json

            session.set_capacity(read_json(args.capacity, limit=64 << 10))
        if args.baseline:
            from .protocol import read_json

            context = (
                read_json(args.comparison_context, limit=64 << 10)
                if args.comparison_context
                else {}
            )
            session.set_baseline(args.baseline, context=context, light=light)
        while True:
            result = session.refresh(options, light=light)
            if (
                result.get("caught_up") and result.get("baseline_caught_up", True)
            ) or time.monotonic() >= deadline:
                break
        if args.serve:
            server = session.open_browser(open_browser=args.open_browser)
            _dump(
                {
                    "schema": "raven.trace-workspace-viewer/v1",
                    "url": server.url,
                    "message": server.message,
                    "index": str(session.path),
                    "scope": "Private read-only viewer; Ctrl-C closes the viewer, not the recorded job.",
                }
            )
            try:
                while True:
                    time.sleep(0.5)
            except KeyboardInterrupt:
                return 0
        _dump(result)
        return 0
    finally:
        session.close()


def import_activity(argv):
    from .activity_import import import_activity as read

    parser = argparse.ArgumentParser(
        prog="raven trace import-activity",
        description="Import bounded Chrome/Perfetto JSON activities as an attributed verified journal",
    )
    parser.add_argument("input")
    _identity_arguments(parser)
    parser.add_argument("--max-mib", type=int, default=32)
    args = parser.parse_args(argv)
    if not 1 <= args.max_mib <= 128:
        parser.error("Activity input budget must be 1–128 MiB")
    _dump(
        read(
            args.input,
            project=args.project,
            job_id=args.job_id,
            attempt=args.attempt,
            cluster=args.cluster,
            node=args.node,
            max_bytes=args.max_mib << 20,
        )
    )
    return 0


def profile(argv):
    parser = argparse.ArgumentParser(
        prog="raven trace profile",
        description="Explicit optional framework profiling in the application interpreter; does not add device synchronization",
    )
    _identity_arguments(parser)
    parser.add_argument(
        "--python",
        help="Application Python executable containing its optional profiler dependencies",
    )
    parser.add_argument("--gpu", action="store_true")
    parser.add_argument(
        "--framework-stacks",
        action="store_true",
        help="Experimental framework Python stacks; provider/version-sensitive and higher overhead. Separate Raven Python capture is preferred.",
    )
    parser.add_argument(
        "--required",
        action="store_true",
        help="Require profiling to initialize before running the application",
    )
    parser.add_argument(
        "--max-mib",
        type=int,
        default=32,
        help="Raven import/output bound; does not bound framework-internal buffers",
    )
    parser.add_argument(
        "--window-steps",
        type=int,
        help="Cooperative profiler steps per window; application calls activity_capture.step()",
    )
    parser.add_argument("--max-windows", type=int, default=16)
    parser.add_argument("script", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    script = args.script[1:] if args.script[:1] == ["--"] else args.script
    if (
        not script
        or not 1 <= args.max_mib <= 128
        or (args.window_steps is not None and not 1 <= args.window_steps <= 10000)
        or not 1 <= args.max_windows <= 128
    ):
        parser.error("Choose -- SCRIPT.py [args] and an output budget of 1–128 MiB")
    params = dict(
        project=args.project,
        job_id=args.job_id,
        attempt=args.attempt,
        cluster=args.cluster,
        node=args.node,
        gpu=args.gpu,
        required=args.required,
        max_bytes=args.max_mib << 20,
        active_steps=args.window_steps,
        max_windows=args.max_windows,
        with_stack=args.framework_stacks,
    )
    if args.python:
        executable = Path(args.python).expanduser().absolute()
        if not executable.is_file():
            parser.error("--python must name an existing application interpreter")
        runtime = str(Path(__file__).resolve().parents[2])
        wrapper = (
            "import json,sys;sys.path.insert(0,sys.argv[1]);"
            "from raven.tracing.activity_capture import profile_script;"
            "value=profile_script(json.loads(sys.argv[3]),**json.loads(sys.argv[2]));"
            "print(json.dumps(value,ensure_ascii=True,allow_nan=False))"
        )
        child = subprocess.run(
            [
                str(executable),
                "-c",
                wrapper,
                runtime,
                json.dumps(params),
                json.dumps(script),
            ],
            check=False,
        )
        return child.returncode
    from .activity_capture import profile_script

    _dump(profile_script(script, **params))
    return 0


def sample(argv):
    from .perf_capture import sample_command

    parser = argparse.ArgumentParser(
        prog="raven trace sample",
        description="Sample a newly owned child on supported Linux perf systems; no arbitrary process attachment",
    )
    _identity_arguments(parser)
    parser.add_argument("--frequency", type=int, default=99)
    parser.add_argument("--duration", type=float, default=30)
    parser.add_argument("--max-mib", type=int, default=64)
    parser.add_argument("--required", action="store_true")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if (
        not command
        or not 1 <= args.frequency <= 10_000
        or not 0.1 <= args.duration <= 3600
        or not 1 <= args.max_mib <= 128
    ):
        parser.error(
            "Choose -- COMMAND, frequency 1–10,000 Hz, duration 0.1–3600 s and budget 1–128 MiB"
        )
    result = sample_command(
        command,
        project=args.project,
        job_id=args.job_id,
        attempt=args.attempt,
        cluster=args.cluster,
        frequency=args.frequency,
        max_bytes=args.max_mib << 20,
        capture_seconds=args.duration,
        required=args.required,
    )
    _dump(result)
    return int(result.get("application_exit", 0) or 0)


def main(argv):
    actions = {
        "workspace": workspace,
        "import-activity": import_activity,
        "profile": profile,
        "sample": sample,
    }
    try:
        return actions[argv[0]](argv[1:])
    except (OSError, ValueError, InterruptedError) as exc:
        print("Raven trace workspace: " + str(exc), file=sys.stderr)
        return 2
