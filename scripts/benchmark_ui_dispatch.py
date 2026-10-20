#!/usr/bin/env python3
"""Compare reviewed controller dispatch with the current composed owner path.

The old method runs against the same current feature callbacks. This isolates
control-flow cost, rather than claiming original whole-release performance.
Run alone on an idle host; no scheduler, logs or network requests are made.
"""

from __future__ import annotations

import argparse
import ast
import json
import resource
import statistics
import subprocess
import time
from pathlib import Path

from raven import controller, workbench
from raven.config import Config
from raven.model import Store

REVIEWED_REVISION = "b98742fb967f18131905696f4afd3a61f2ad03f2"


def measure(revision: str, *, rounds: int, keys: int) -> dict:
    source = subprocess.check_output(
        ["git", "show", f"{revision}:raven/controller.py"], text=True
    )
    tree = ast.parse(source)
    klass = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "App"
    )
    method = next(
        node
        for node in klass.body
        if isinstance(node, ast.FunctionDef) and node.name == "_handle"
    )
    method.name = "_baseline_handle"
    namespace = dict(vars(controller))
    exec(  # noqa: S102 -- intentionally execute the selected reviewed Git baseline.
        compile(
            ast.fix_missing_locations(ast.Module(body=[method], type_ignores=[])),
            "<reviewed-controller-handle>",
            "exec",
        ),
        namespace,
    )
    app = controller.App(
        Store(persist=False), None, None, Config(), "alex", ascii_=True
    )
    app.mode = "notifications"
    workbench.owners()
    workbench.module_map()
    values = {}
    try:
        for label, function in (
            (
                "reviewed_control_flow_current_feature_contracts",
                namespace["_baseline_handle"],
            ),
            ("foreground_focus_stack", controller.App._handle),
        ):
            function(app, "noop")
            samples = []
            cpu_begin = time.process_time()
            for _ in range(rounds):
                begin = time.perf_counter()
                for _ in range(keys):
                    function(app, "noop")
                samples.append((time.perf_counter() - begin) / keys * 1e6)
            values[label] = {
                "median_us_per_key": statistics.median(samples),
                "round_us_per_key": samples,
                "measured_rounds_cpu_seconds": time.process_time() - cpu_begin,
            }
        assert app.mode == "notifications" and not app.confirm
    finally:
        if app.research is not None:
            app.research.close()
    return {
        "schema": "raven.focus-latency/v1",
        "baseline_revision": revision,
        "fixture": {
            "mode": "notifications",
            "key": "noop",
            "rounds": rounds,
            "keys_per_round": keys,
            "feature_contracts": "identical current owners",
        },
        "results": values,
        "process_high_water_rss_kib": resource.getrusage(
            resource.RUSAGE_SELF
        ).ru_maxrss,
        "python_allocations": "not measured; process RSS is reported separately",
        "limits": (
            "Reviewed controller method compiled against current feature callbacks; "
            "isolates dispatch overhead, not original whole-release performance. "
            "Combined-process RSS cannot compare release memory. "
            "Shared cloud Linux/Python host; no live scheduler/GPU/remote qualification."
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", default=REVIEWED_REVISION)
    parser.add_argument("--rounds", type=int, default=7)
    parser.add_argument("--keys", type=int, default=2000)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.rounds < 1 or args.keys < 1:
        parser.error("rounds and keys must be positive")
    result = measure(args.baseline, rounds=args.rounds, keys=args.keys)
    document = json.dumps(result, indent=2) + "\n"
    if args.output:
        args.output.write_text(document)
    print(document, end="")


if __name__ == "__main__":
    main()
