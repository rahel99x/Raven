#!/usr/bin/env python3
"""Prepare the existing G5 journeys with real local sample files, never participants."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from raven import launch, launch_runtime, run_home, launch_wizard
JOURNEYS = [
    ("G5-1", "Find a running job", "Find a running job and explain its current state, account and requested resources."),
    ("G5-2", "Diagnose a failed job", "Find a failed job, inspect its logs, and distinguish evidence from a proposed cause."),
    ("G5-3", "Inspect results", "Find a retained result, inspect its metric values, and explain any unavailable recording."),
    ("G5-4", "Connect a project", "Connect the supplied project and validate a real metrics file without changing the producer."),
    ("G5-5", "Prepare a reviewed rerun", "Prepare a rerun, identify the charged account and resources, then stop before confirming submission."),
]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    args.output = args.output.absolute()
    args.output.mkdir(parents=True, exist_ok=False, mode=0o700)
    project = args.output / "project"
    shutil.copytree(ROOT / "examples/project-template", project)
    # This computes actual finite samples. These are local demonstration results,
    # never fabricated scheduler/GPU observations or human study outcomes.
    subprocess.run([sys.executable, str(project / "experiment.py"), "--run-id", "study-sample",
                    "--steps", "4", "--terms-per-step", "100"], cwd=project, check=True, timeout=20)
    receipts = {}
    for name, code in (("completed", 0), ("failed", 7)):
        script = project / "jobs" / ("study-" + name + ".sh")
        script.write_text('#!/bin/sh\nprintf "G5 local fixture: ' + name + '\\n"\nexit ' + str(code) + '\n')
        env = {"PATH": os.defpath}
        plan = launch.resolve_plan([str(script), "--project", str(project), "--local", "--no-cpu-memory",
                                    "--no-gpu", "--no-trace", "--no-export-on-finish"], cwd=project, environ=env)
        receipt = launch_runtime.start_local_plan(plan, environ=env)
        until = time.monotonic() + 10
        inventory = Path(receipt["run_dir"]) / "run.json"
        while time.monotonic() < until:
            if inventory.exists() and json.loads(inventory.read_text()).get("state") in ("COMPLETED", "FAILED"):
                break
            time.sleep(.02)
        else:
            raise ValueError("Local study fixture did not complete within its ten-second observation budget")
        (args.output / (name + "-receipt.json")).write_text(json.dumps(receipt, indent=2) + "\n")
        loaded = run_home.load(run_home.receipt_binding(receipt))
        if loaded["report"]["receipt"]["application_exit_code"] != code:
            raise ValueError("Local study fixture did not produce its declared outcome")
        if "G5 local fixture: " + name not in Path(receipt["logs"]["stdout"]).read_text():
            raise ValueError("Local study fixture did not retain its application output")
        draft = run_home.relaunch_draft(loaded["report"])
        review = launch_wizard.review(draft, environ=env)
        if review["plan"]["execution"] != "local":
            raise ValueError("Study rerun review unexpectedly selected a scheduler")
        receipts[name] = {"receipt": name + "-receipt.json", "run_dir": receipt["run_dir"],
                          "observed_state": loaded["inventory"]["state"], "reviewed_execution": "local",
                          "reviewed_script_sha256": review["plan"]["script_sha256"]}
    manifest = {"schema": "raven.g5-preparation/v1",
                "source_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
                "source_sha256": {path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest()
                                  for path in ("scripts/prepare_usability_study.py", "raven/launch.py", "raven/launch_runtime.py", "raven/run_home.py")},
                "journey_source": "docs/QUALIFICATION.md#5-evaluate-user-tasks-and-scientific-outcomes",
                "journeys": [{"id": key, "title": title, "task": task} for key, title, task in JOURNEYS],
                "human_participants": 0, "human_efficacy": "unmeasured", "real_site_sessions": 0,
                "local_run_home_fixtures": receipts,
                "fixture_scope": "existing FakeBackend for scheduler observations; actual local CPU example for results",
                "fixture_sha256": {str(path.relative_to(project)): hashlib.sha256(path.read_bytes()).hexdigest()
                                   for path in sorted(project.rglob("*")) if path.is_file() and "__pycache__" not in str(path)}}
    (args.output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    (args.output / "task-cards.md").write_text("# G5 task cards\n\n" + "\n\n".join(
        "## " + key + " — " + title + "\n\n" + task for key, title, task in JOURNEYS) + "\n")
    with (args.output / "observations.csv").open("w", newline="") as stream:
        csv.writer(stream).writerow(["participant_pseudonym", "journey", "environment", "completed",
                                    "unaided", "seconds", "unnecessary_decisions", "documentation_lookups",
                                    "errors", "recovery", "account_understood", "resources_understood",
                                    "observer_assistance", "accessibility", "notes"])
    print(str(args.output / "manifest.json"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
