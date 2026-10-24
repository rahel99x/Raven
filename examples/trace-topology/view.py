#!/usr/bin/env python3
"""Open the exact topology attempt through the existing viewer or live workspace."""

from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path
import shlex
import subprocess
import sys

HERE = Path(__file__).resolve().parent
specification = importlib.util.spec_from_file_location(
    "raven_trace_topology_view_helpers", HERE.parent / "trace-lab" / "demo.py"
)
helpers = importlib.util.module_from_spec(specification)
specification.loader.exec_module(helpers)


def parser():
    result = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    result.add_argument("receipt", help="exact launch.json printed by run.sh")
    result.add_argument(
        "--history",
        action="store_true",
        help="open the completed run's History context",
    )
    result.add_argument(
        "--inspect",
        action="store_true",
        help="use the original bounded per-process inspector",
    )
    result.add_argument("--process", type=int, help="choose an exact registered PID")
    result.add_argument(
        "--workspace",
        action="store_true",
        help="serve the live retained workspace and follow new sources",
    )
    result.add_argument(
        "--browser",
        action="store_true",
        help="serve the workspace and request the local browser",
    )
    result.add_argument(
        "--no-open",
        action="store_true",
        help="print commands without starting Raven or its viewer",
    )
    return result


def workspace(args):
    helpers.safe_text(args.receipt)
    receipt, project, run = helpers.receipt_context(args.receipt)
    configuration = project / "raven-view.json"
    config = helpers.read_json(configuration)
    if config != helpers.example_config(receipt["execution"] == "local"):
        raise ValueError(
            "The example's private view configuration no longer matches its original scoped settings"
        )
    sources = helpers.trace_sources(run, receipt)
    if not sources:
        print(
            "No process recording is registered yet. The job may be queued or capture unavailable; inspect its scheduler state, logs, and retained trace diagnostics. No job is resubmitted."
        )
        return 0
    if args.process is not None:
        sources = [item for item in sources if item[0]["pid"] == args.process]
        if not sources:
            raise ValueError(
                "That PID has no registered trace in this exact run; use a displayed PID"
            )
    # This is an exact-attempt initial journal, not an arbitrary sibling file.
    # The workspace verifies its prefix, then follows matching project sources.
    sources.sort(
        key=lambda item: (
            item[0].get("role") != "main",
            item[0].get("rank", 0) != 0,
            item[0].get("role") == "helper",
        )
    )
    command = [
        "raven",
        "--site",
        config["profile"],
        "--config",
        str(configuration),
        "trace",
        "workspace",
        str(sources[0][1]),
        "--follow-project",
        str(project),
        "--serve",
    ]
    if args.browser:
        command.append("--open-browser")
    print("Project: " + str(project) + "\nRun: " + receipt["run_id"])
    if args.history:
        print(
            "Retained History context: the same exact attempt remains available after completion."
        )
    print("Workspace: " + shlex.join(command), flush=True)
    if args.no_open:
        return 0
    command[0] = helpers.executable(command[0])
    return subprocess.run(command, cwd=project, check=False).returncode


def main(argv=None):
    args = parser().parse_args(sys.argv[1:] if argv is None else argv)
    if args.process is not None and args.process <= 0:
        parser().error("--process must name a positive registered PID")
    if args.inspect and (args.workspace or args.browser):
        parser().error(
            "--inspect and --workspace/--browser select different viewer modes"
        )
    try:
        if args.workspace or args.browser:
            return workspace(args)
        return helpers.view_example(args)
    except (OSError, ValueError, TypeError, subprocess.SubprocessError) as error:
        message = "".join(char if char.isprintable() else "?" for char in str(error))
        print("Trace topology viewer: " + message, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
