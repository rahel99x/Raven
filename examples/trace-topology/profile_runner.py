#!/usr/bin/env python3
"""Run a GPU example with the current job's optional activity recorder.

The launcher supplies Raven's frozen runtime, so the application's PyTorch
environment does not need a second Raven installation. Scientific exceptions
and nonzero exits propagate; unavailable optional profiling leaves a receipt.
"""

from __future__ import annotations

import importlib
import importlib.machinery
import os
from pathlib import Path
import sys
import types


def runtime_package():
    name = "_raven_launch_runtime"
    if name not in sys.modules:
        bundle = os.environ.get("RAVEN_RUNTIME_DIRECTORY") or os.environ.get(
            "RAVEN_RUNTIME_BUNDLE"
        )
        if not bundle:
            raise ValueError(
                "Activity recording requires the run.sh launcher or raven trace profile"
            )
        package = types.ModuleType(name)
        package.__path__ = [bundle + "/raven"]
        package.__package__ = name
        package.__spec__ = importlib.machinery.ModuleSpec(
            name, loader=None, is_package=True
        )
        sys.modules[name] = package
    return name


def recording_context():
    package = runtime_package()
    try:
        run = Path(os.environ["RAVEN_RUN_DIR"])
        plan_path = os.environ["RAVEN_LAUNCH_PLAN"]
    except KeyError as error:
        raise ValueError(
            "Use run.sh to bind activity recording to its job attempt"
        ) from error
    reporting = importlib.import_module(package + ".reporting")
    autotrace = importlib.import_module(package + ".launching.autotrace")
    plan = reporting._read_json(plan_path)
    run, manifest = autotrace.verified_run(run, plan)
    gpu = os.environ.get("RAVEN_EXAMPLE_GPU", "1")
    if gpu not in {"0", "1"}:
        raise ValueError("RAVEN_EXAMPLE_GPU must be 0 or 1")
    return importlib.import_module(package + ".tracing.activity_capture"), dict(
        # Register windows in the project's discovery index. A nested index
        # inside runs/RUN_ID would not be followed by the job workspace.
        project=Path(reporting._directory(plan["project_root"])),
        job_id=manifest.get("job_id", "local"),
        attempt=run.name,
        cluster=os.environ.get("SLURM_CLUSTER_NAME", "local"),
        gpu=gpu == "1",
        required=False,
        active_steps=2,
        max_windows=64,
        max_bytes=8 << 20,
    )


def main(argv=None):
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments:
        raise ValueError("Choose the existing Python example to profile")
    recorder, context = recording_context()
    receipt = recorder.profile_script(arguments, **context)
    print(
        "Activity recording: "
        + str(receipt["status"])
        + "; retained windows: "
        + str(len(receipt.get("reports", []))),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
