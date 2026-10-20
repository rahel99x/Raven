"""Existing-script workload launch argument grammar; no entrypoint owners."""

from __future__ import annotations
import argparse
from ..launching import features as F

MAX_ARGUMENTS = 4096
MAX_ARGUMENT_BYTES = 1 << 20


def parser():
    ap = argparse.ArgumentParser(
        prog="raven launch",
        allow_abbrev=False,
        description="Run an existing Bash/POSIX-shell script once with retained reporting. Defaults to Slurm; --local is explicit.",
        epilog="Research: "
        + "; ".join(
            item["name"] + " (" + ", ".join(item["aliases"]) + ")"
            for item in F.FEATURES
        )
        + ". all requests integrations only; it does not enable GPU/tracing or execute Operations. Use -- before application arguments.",
    )
    ap.add_argument(
        "script", help="Existing UTF-8 Bash or POSIX-shell script inside its project"
    )
    ap.add_argument(
        "--research",
        "--r",
        "--re",
        nargs="*",
        action="append",
        default=None,
        metavar="FEATURE",
        help="Research integrations; no values selects Experiment/Evidence and declared context",
    )
    ap.add_argument(
        "--project",
        metavar="PATH",
        help="Explicit reporting/export root; independent of scheduler WorkDir",
    )
    ap.add_argument(
        "--recipe",
        metavar="NAME",
        help="Named project launch settings (not the monitoring --profile)",
    )
    ap.add_argument(
        "--local", action="store_true", help="Explicitly execute here without Slurm"
    )
    ap.add_argument(
        "--account",
        help="Workload submission account; does not alter monitoring/Heavy preferences",
    )
    ap.add_argument(
        "--sbatch",
        action="append",
        default=[],
        metavar="TOKEN",
        help="One exact scheduler token, e.g. --sbatch=--mem=8G; repeatable",
    )
    for name, explanation in (
        (
            "gpu",
            "Request supported device recording inside the job; no allocation is added",
        ),
        ("cpu-memory", "Record supported CPU and memory process measurements"),
        (
            "trace",
            "Automatically trace Python processes inheriting this job's environment; no script hook required",
        ),
    ):
        group = ap.add_mutually_exclusive_group()
        group.add_argument(
            "--" + name,
            dest=name.replace("-", "_"),
            action="store_true",
            default=None,
            help=explanation,
        )
        group.add_argument(
            "--no-" + name,
            dest=name.replace("-", "_"),
            action="store_false",
            help="Explicitly disable " + name + " recording",
        )
    ap.add_argument(
        "--trace-adapter",
        choices=(
            "python-monitoring",
            "python-legacy-profile",
            "native-cooperative",
            "mpi",
        ),
    )
    ap.add_argument(
        "--trace-scope",
        choices=("project", "all"),
        help="Trace project/script files (default), or all Python frames",
    )
    helpers = ap.add_mutually_exclusive_group()
    helpers.add_argument(
        "--trace-helpers",
        action="store_true",
        default=None,
        help="Include helper processes in the initial trace view; their recordings always remain available",
    )
    helpers.add_argument(
        "--no-trace-helpers",
        dest="trace_helpers",
        action="store_false",
        help="Hide helper processes in the initial trace view",
    )
    placement = ap.add_mutually_exclusive_group()
    placement.add_argument(
        "--trace-cpu-placement",
        action="store_true",
        default=None,
        help="Observe Linux CPU/socket/core at Python event times; does not measure continuous residency",
    )
    placement.add_argument(
        "--no-trace-cpu-placement",
        dest="trace_cpu_placement",
        action="store_false",
        help="Disable event-time CPU placement observations",
    )
    ap.add_argument(
        "--trace-process-bytes",
        type=int,
        help="Per-process trace budget in bytes (4096–1073741824; default 67108864)",
    )
    ap.add_argument(
        "--trace-run-bytes",
        type=int,
        help="Run trace budget in bytes (4096–4294967296; default 268435456)",
    )
    ap.add_argument(
        "--trace-processes",
        type=int,
        help="Maximum attached processes (1–512; default 128)",
    )
    ap.add_argument(
        "--metrics-file",
        help="Declared project-relative native application JSONL producer",
    )
    ap.add_argument(
        "--checkpoint-advice",
        help="Project-relative checkpoint duration/exposure JSON; read-only launch advice",
    )
    ap.add_argument(
        "--artifact",
        action="append",
        default=None,
        help="Explicit project-relative output path; repeatable",
    )
    ap.add_argument(
        "--research-source",
        action="append",
        default=None,
        metavar="VIEW=PATH",
        help="Bind a native Research report file without a manual manifest",
    )
    ap.add_argument(
        "--parameter",
        action="append",
        default=None,
        metavar="KEY=VALUE",
        help="Scientific parameter; JSON value or literal string",
    )
    ap.add_argument(
        "--workers",
        type=int,
        help="Declared scientific worker count, distinct from allocated CPUs",
    )
    ap.add_argument(
        "--problem-size", type=float, help="Declared scientific problem size"
    )
    ap.add_argument("--repeat", type=int, help="Declared experiment repeat index")
    export = ap.add_mutually_exclusive_group()
    export.add_argument(
        "--export-on-finish",
        nargs="?",
        const="default",
        metavar="PRESET",
        help="Capture an export preset for each exact completed attempt",
    )
    export.add_argument(
        "--no-export-on-finish",
        action="store_true",
        help="Explicitly disable saved automatic exports",
    )
    ap.add_argument(
        "--check",
        action="store_true",
        help="Read-only plan: no files, submission, probes, or Heavy allocation",
    )
    ap.add_argument(
        "--json",
        action="store_true",
        help="Machine-readable plan or receipt; diagnostics stay on stderr",
    )
    ap.add_argument(
        "--receipt-file",
        metavar="PATH",
        help="Exclusively create a JSON receipt; required for --local --json to preserve workload stdout",
    )
    return ap


def parse(argv):
    argv = list(argv)
    marker = argv.index("--") if "--" in argv else len(argv)
    args = parser().parse_args(argv[:marker])
    args.raven_argv = argv
    args.script_args = argv[marker + 1 :] if marker < len(argv) else []
    if (
        len(args.script_args) > MAX_ARGUMENTS
        or sum(len(x.encode()) for x in args.script_args) > MAX_ARGUMENT_BYTES
        or any("\x00" in x for x in args.script_args)
    ):
        raise ValueError(
            "Script arguments exceed the 4096-argument/1 MiB budget or contain NUL"
        )
    return args
