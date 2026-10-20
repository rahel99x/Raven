"""Dashboard option parsing independent of application construction."""

from __future__ import annotations
import argparse
import math
import shlex
import sys
from pathlib import Path
from .. import __version__
from ..ui.catalog import RESEARCH_VIEWS, TABS


DESCRIPTION = "raven: an interactive terminal dashboard of your Slurm jobs.\n\n    raven                        # interactive (curses)\n    raven --watch                # the animated non-interactive screen (Ctrl-C exits)\n    raven --once [--tab history] # one frame of text\n    raven --json                 # the whole snapshot as JSON\n    raven --fake                 # a simulated cluster, to try it anywhere\n    raven --write-config         # ~/.config/raven/config.toml with the commented defaults\n    raven --profile mycluster    # a named profile from the config (a cluster: host, account, ...)\n    raven --host login.example.edu   # remote mode: every command over ssh\n    raven --record session.jsonl.gz / --replay session.jsonl.gz --speed 10\n    raven run cancel 123 --yes   # one palette command, no screen\n    raven report --help         # job-bound Research reporting from Bash\n    raven terminal --help       # embedded terminal hooks, replay and diagnostics\n    raven --eval 'n_running'     # an expression over the snapshot\n    raven --wait-for 'n_pending == 0' --timeout 3600   # block until it holds (exit 0) or time out (exit 2)\n\nTabs: Jobs (ordered batches, a cursor, marks, actions), Cluster (partitions, GPUs, fair share, the account's\nload), History (sacct with efficiency and totals), Analytics (charts), Nodes (the nodes running your jobs), Log\n(the selected job's stdout, following), Sources (health of every Slurm command).  ? lists the keys inside."


def parse(argv):
    ap = argparse.ArgumentParser(
        prog="raven",
        description=DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        allow_abbrev=False,
    )
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    ap.add_argument(
        "--config",
        help="configuration file (TOML or JSON); default ~/.config/raven/config.toml",
    )
    ap.add_argument(
        "--write-config",
        action="store_true",
        help="write the commented defaults to the default path and exit",
    )
    ap.add_argument(
        "--doctor",
        action="store_true",
        help="check local, --host, or --fake prerequisites without sampling or changing jobs; combine with --json",
    )

    def gpu_job(value):
        from ..jobs.identity import valid_job_id

        try:
            return valid_job_id(value)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(str(exc))

    ap.add_argument(
        "--gpu-check",
        nargs="?",
        const="all",
        default=None,
        type=gpu_job,
        metavar="JOBID",
        help="diagnose GPU allocation, NVIDIA sampling, traces, and retained samples; omit JOBID to check current jobs",
    )
    ap.add_argument(
        "--gpu-check-output",
        default="",
        metavar="DIRECTORY",
        help="with --gpu-check: create a private new directory with report.json, report.txt, and command evidence",
    )
    ap.add_argument(
        "--ui-trace",
        default="",
        metavar="FILE",
        help="save bounded UI phase timings and page transitions to a new JSON file when the interactive session exits",
    )
    ap.add_argument(
        "--workers",
        choices=("single", "multi"),
        default=None,
        help="background worker mode; the interactive UI stays on its own thread",
    )
    ap.add_argument(
        "--profile",
        default="",
        help="a [profiles.NAME] section of the config to merge over it (a cluster)",
    )
    ap.add_argument(
        "--host",
        default="",
        help="remote mode: run every Slurm command on this login node over ssh",
    )
    ap.add_argument(
        "--ssh-user", default="", help="the login on --host (default: as here)"
    )
    ap.add_argument("--user", default="")
    ap.add_argument(
        "--account",
        default="",
        help="account whose overall load the header shows (default: the first of sshare -U)",
    )
    ap.add_argument(
        "--interval",
        type=float,
        default=0.0,
        help="requested polling seconds, rounded to the 5s..500ms slider; --rate takes precedence",
    )
    ap.add_argument(
        "--rate",
        type=int,
        choices=range(1, 51),
        metavar="1..50",
        default=None,
        help="polling slider position: 1 is 5s, 50 is 500ms; overrides saved preference, with backoff retained",
    )
    ap.add_argument(
        "--days",
        type=float,
        default=0.0,
        help="history window in days (config: history_days)",
    )
    ap.add_argument("--no-gpu", action="store_true", help="no nvidia-smi sampling")
    ap.add_argument(
        "--bell", action="store_true", help="ring when one of your jobs starts"
    )
    # Last preference wins, so --ascii can override a shell alias's --unicode.
    ap.add_argument(
        "--ascii",
        action="store_const",
        const=True,
        default=None,
        help="plain characters for bars, rules and sparklines",
    )
    ap.add_argument(
        "--unicode",
        dest="ascii",
        action="store_const",
        const=False,
        help="solid block and high-resolution terminal graphics (encoding permitting)",
    )
    ap.add_argument("--no-color", action="store_true")
    ap.add_argument(
        "--no-plugins", action="store_true", help="do not load ~/.config/raven/plugins"
    )
    ap.add_argument("--once", action="store_true", help="one frame of text and exit")
    ap.add_argument(
        "--tab",
        choices=[t for t, _ in TABS],
        help="initial tab; also selects the --once / --csv / --run view",
    )
    ap.add_argument(
        "--research-view",
        choices=[k for k, _ in RESEARCH_VIEWS],
        help="Research subview",
    )
    ap.add_argument(
        "--metrics-file", help="application JSONL telemetry; {job_id} selects a stream"
    )
    ap.add_argument("--contract", help="JSON output contract (local cluster files)")
    ap.add_argument("--workdir", help="root for attached metrics and declared outputs")
    ap.add_argument("--passport", help="immutable run passport to inspect")
    ap.add_argument(
        "--planning-file", help="local planning observations or scaling/workflow recipe"
    )
    ap.add_argument("--json", action="store_true", help="the snapshot as JSON and exit")
    ap.add_argument(
        "--report",
        nargs="?",
        const="-",
        default="",
        help="the whole dashboard as an ASCII text report (to PATH, or stdout) and exit",
    )
    ap.add_argument(
        "--csv",
        action="store_true",
        help="the tab's table (jobs by default, or --tab history / nodes / sources) as CSV and exit",
    )
    ap.add_argument(
        "--watch", action="store_true", help="the animated non-interactive screen"
    )
    ap.add_argument(
        "--fake", action="store_true", help="a simulated cluster (no Slurm needed)"
    )
    ap.add_argument(
        "--getting-started",
        action="store_true",
        help="open guided workstation or cluster setup",
    )
    ap.add_argument(
        "--record",
        default="",
        help="record every command and its answer to this file (.jsonl or .jsonl.gz)",
    )
    ap.add_argument(
        "--replay", default="", help="play a recording back instead of talking to Slurm"
    )
    ap.add_argument(
        "--speed",
        type=float,
        default=1.0,
        help="with --replay: how many recorded seconds pass per second",
    )
    ap.add_argument("--paused", action="store_true", help="with --replay: start paused")
    ap.add_argument(
        "--run",
        default="",
        help="run one palette command (e.g. 'cancel 123', 'export csv', 'hold marked') and exit",
    )
    ap.add_argument(
        "--yes", action="store_true", help="with --run: confirm actions without asking"
    )
    ap.add_argument(
        "--eval",
        default="",
        help="print the value of an expression over the snapshot and exit (see README)",
    )
    ap.add_argument(
        "--wait-for",
        default="",
        help="poll until the expression is true (exit 0), or --timeout passes (exit 2)",
    )
    ap.add_argument(
        "--timeout",
        type=float,
        default=0.0,
        help="with --wait-for: seconds to wait (0: forever)",
    )
    ap.add_argument(
        "--poll",
        type=float,
        default=0.0,
        help="with --wait-for: seconds between polls (default: intervals.jobs)",
    )
    ap.add_argument(
        "--alert",
        action="append",
        default=[],
        help="an alert rule for this run (an expression over each job; bell and event), repeatable",
    )
    ap.add_argument(
        "--no-state",
        action="store_true",
        help="do not read or write ~/.local/state/raven",
    )
    ap.add_argument(
        "--width",
        type=int,
        default=0,
        help="with --once / --report: the width (default: the terminal's)",
    )
    ap.add_argument(
        "words",
        nargs="*",
        help="'run <palette command ...>': the same as --run (e.g. raven run cancel 123 --yes); the command's own flags pass through",
    )
    argv = list(sys.argv[1:] if argv is None else argv)
    # Option values may themselves be named "run". Only a positional token
    # starts the palette command; consume global values before looking for it.
    run_index, scan = None, 0
    value_flags = {
        name
        for action in ap._actions
        if action.nargs != 0
        for name in action.option_strings
    }
    while scan < len(argv):
        token = argv[scan]
        if token == "run":
            run_index = scan
            break
        if token == "--":
            break
        if token in value_flags and scan + 1 < len(argv):
            scan += 2
        else:
            scan += 1
    if (
        run_index is not None
    ):  # raven [raven flags] run <command and its flags> [raven flags]
        i = run_index
        pre, post, cmd = argv[:i], argv[i + 1 :], []
        switches = {
            "--yes",
            "--fake",
            "--no-state",
            "--no-plugins",
            "--ascii",
            "--unicode",
            "--no-color",
            "--no-gpu",
            "--bell",
            "--paused",
            "--json",
        }
        valued = {
            "--tab",
            "--config",
            "--profile",
            "--host",
            "--ssh-user",
            "--user",
            "--account",
            "--width",
            "--replay",
            "--record",
            "--speed",
            "--days",
            "--interval",
            "--rate",
            "--ui-trace",
            "--workers",
        }
        batch_command = bool(
            post
            and post[0]
            and any(
                c.startswith(post[0])
                for c in ("resubmit", "prepare", "submit", "array")
            )
        )
        from .command_options import RESEARCH_COMMANDS as COMMANDS, command_value_option
        from .command_options import PLANNING_COMMANDS as planning_commands

        all_commands = (*COMMANDS, *planning_commands, "resubmit")
        candidates = [c for c in all_commands if post and c.startswith(post[0])]
        research_command = (
            post[0]
            if post and post[0] in all_commands
            else candidates[0]
            if len(candidates) == 1
            else ""
        )
        if batch_command:
            # After `run resubmit`, --account belongs to sbatch. Raven's account
            # can still be selected explicitly before `run`.
            valued.discard("--account")
        k = 0
        while k < len(post):
            a = post[k]
            if a == "--":
                cmd += post[k:]
                break
            if command_value_option(research_command, a) and k + 1 < len(post):
                cmd += post[k : k + 2]
                k += 1
            elif a in switches:
                pre.append(a)
            elif a in valued and k + 1 < len(post):
                pre += [a, post[k + 1]]
                k += 1
            elif a.split("=", 1)[0] in valued:
                pre.append(a)
            else:
                cmd.append(a)
            k += 1
        # Shell argv already preserves boundaries; retain them through the palette parser.
        if not cmd:
            ap.error("run requires a palette command")
        free_text = cmd[0] in ("eval", "find", "filter", "note")
        argv = pre + ["--run", " ".join(cmd) if free_text else shlex.join(cmd)]
    args = ap.parse_intermixed_args(argv)
    if args.ui_trace and (
        args.doctor
        or args.gpu_check is not None
        or args.run
        or args.eval
        or args.wait_for
        or args.watch
        or args.report
        or args.csv
        or args.json
        or args.once
        or args.write_config
    ):
        ap.error("--ui-trace requires an interactive session")
    if args.gpu_check_output and args.gpu_check is None:
        ap.error("--gpu-check-output requires --gpu-check")
    if args.gpu_check is not None and (
        args.doctor
        or args.run
        or args.eval
        or args.wait_for
        or args.watch
        or args.report
        or args.csv
        or args.write_config
        or args.record
    ):
        ap.error("--gpu-check cannot be combined with another command or report mode")
    if args.words:
        ap.error(
            f"unexpected argument '{args.words[0]}' (did you mean: raven run {' '.join(args.words)} ?)"
        )
    for name in ("interval", "days", "timeout", "poll", "speed", "width"):
        value = getattr(args, name)
        if not math.isfinite(value) or value < 0 or (name == "speed" and value == 0):
            ap.error(
                f"--{name} must be {'positive' if name == 'speed' else 'non-negative'} and finite"
            )
    return args


def bootstrap_parser(heavy=False):
    name = "ravenh" if heavy else "ravenl"
    parser = argparse.ArgumentParser(
        prog=name,
        add_help=False,
        allow_abbrev=False,
        description="Raven: "
        + (
            "explicit Heavy admission; waiting stays lightweight."
            if heavy
            else "bounded Light monitoring; opening never requests compute."
        ),
    )
    parser.add_argument(
        "--site", help="Named configured profile; desktop must be explicit"
    )
    parser.add_argument(
        "--profile", help="Legacy named Raven profile (read-only compatibility)"
    )
    parser.add_argument(
        "--config", help="Versioned Raven config, or a read-only legacy view"
    )
    parser.add_argument(
        "--host", help="Explicit SSH scheduler host alias for this startup"
    )
    parser.add_argument("--ssh-user", help="SSH login for the explicit connection")
    parser.add_argument(
        "--account",
        help="Account for monitoring and explicit Heavy allocation admission",
    )
    parser.add_argument(
        "--site-policy",
        help="Additional restrictive site policy; cannot replace mandatory policy",
    )
    parser.add_argument(
        "--allocation",
        help="Explicit exact allocation to validate and reuse (Heavy only)",
    )
    parser.add_argument(
        "--plan",
        action="store_true",
        help="Explain effective startup without submitting or writing state",
    )
    parser.add_argument("--interface", choices=("local", "compute"), default="local")
    parser.add_argument(
        "--json",
        action="store_true",
        help="Machine-readable bootstrap result; forwarded for dashboard output",
    )
    parser.add_argument("--help", "-h", action="store_true")
    parser.add_argument(
        "--version",
        action="store_true",
        help="Print the installed release without starting collection",
    )
    parser.add_argument(
        "--show-paths",
        action="store_true",
        help="Include configured paths in config explain",
    )
    parser.add_argument(
        "--write-config",
        nargs="?",
        const="",
        metavar="NEW_JSON",
        help="Create a new Raven JSON config without overwrite",
    )
    return parser


GLOBAL_VALUES = frozenset(
    "--site --profile --config --host --ssh-user --account --site-policy --allocation --interface --user --interval --rate --days --tab --research-view --metrics-file --contract --workdir --passport --planning-file --record --replay --speed --run --eval --wait-for --timeout --poll --alert --width --ui-trace --workers --gpu-check-output".split()
)
GLOBAL_OPTIONAL = frozenset(("--gpu-check", "--report", "--write-config"))
KNOWN_COMMANDS = frozenset(
    (
        "run",
        "report",
        "trace",
        "runtime",
        "terminal",
        "migrate",
        "config",
        "doctor",
        "qualify",
        "launch",
        "export-data",
        "setup",
        "query",
        "update",
        "revert",
    )
)


def command_boundary(argv):
    """Return the first unambiguous command/script operand, never an option value."""
    i = 0
    while i < len(argv):
        token = argv[i]
        if token == "--":
            i += 1
            if i == len(argv):
                return None
            token = argv[i]
        elif token in GLOBAL_VALUES:
            i += 2
            continue
        elif token in GLOBAL_OPTIONAL:
            i += 2 if i + 1 < len(argv) and not argv[i + 1].startswith("-") else 1
            continue
        elif token.startswith("-"):
            i += 1
            continue
        if token in KNOWN_COMMANDS:
            return i, token
        if (
            token.endswith((".sh", ".bash"))
            or token.startswith(("./", "../", "/"))
            or Path(token).is_file()
        ):
            return i, "script"
        return None
    return None
