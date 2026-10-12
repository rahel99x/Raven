"""Explicit application hook; never guesses or rewrites a batch script.

Only a simple Python script invocation is automatically adapted. Unsupported
commands run unchanged and leave an actionable recording warning. The Python
bootstrap executes in the application's chosen interpreter and preserves its
site initialization, modules, arguments, exit status and ordinary imports.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import runpy
import subprocess
import sys

from . import reporting as R


def _diagnostic(run, message):
    """Recorder diagnostics must never prevent or replace application work."""
    try:
        if run is not None:
            from .launch_runtime import _warn
            _warn(run, message)
            return
    except Exception:
        pass
    try:
        print("Raven tracing: " + message, file=sys.stderr)
    except Exception:
        pass


def _stop_capture(capture, recorder, run):
    try:
        capture.stop()
    except Exception as exc:
        message = "Optional trace shutdown failed; application outcome is preserved: " + str(exc)
        try:
            if recorder is not None:
                recorder._fail("PARTIAL", message)
        except Exception:
            pass
        _diagnostic(run, message)


def hook(command):
    if not command:
        print("raven_exec: expected COMMAND [ARG ...]", file=sys.stderr)
        return 2
    try:
        plan = R._read_json(os.environ["RAVEN_LAUNCH_PLAN"])
        if (not isinstance(plan, dict) or not isinstance(plan.get("settings", {}), dict)
                or not isinstance(plan.get("trace", False), bool)
                or not isinstance(plan.get("settings", {}).get("trace_adapter", "python-monitoring"), str)):
            raise ValueError("invalid launch trace metadata")
    except Exception as exc:
        run = Path(os.environ["RAVEN_RUN_DIR"]) if os.environ.get("RAVEN_RUN_DIR") else None
        _diagnostic(run, "Optional tracing metadata is unavailable; command runs unchanged: " + str(exc))
        os.execvpe(command[0], command, os.environ)
    if not plan.get("trace"):
        os.execvpe(command[0], command, os.environ)
    adapter = plan.get("settings", {}).get("trace_adapter", "python-monitoring")
    supported = (adapter in {"python-monitoring", "python-legacy-profile"}
                 and re.fullmatch(r"python(?:[0-9]+(?:\.[0-9]+)?)?", Path(command[0]).name)
                 and len(command) >= 2 and not command[1].startswith("-") and Path(command[1]).is_file())
    if supported:
        # A legacy application Python must still execute unchanged. Probe only
        # interpreter version with site initialization disabled; never import
        # or run the user's script just to decide recorder compatibility.
        try:
            result = subprocess.run([command[0], "-S", "-c", "import sys;sys.stdout.write('%d.%d' % sys.version_info[:2])"],
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=2, check=False)
            supported = result.returncode == 0 and tuple(map(int, result.stdout.decode("ascii").split("."))) >= (3, 10)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            supported = False
    if not supported:
        run = Path(os.environ["RAVEN_RUN_DIR"]) if os.environ.get("RAVEN_RUN_DIR") else None
        _diagnostic(run, "Tracing needs a simple 'raven_exec python script.py [args]' launch with a Python 3.10+ supported adapter; this command runs unchanged")
        os.execvpe(command[0], command, os.environ)
    bootstrap = "import sys;sys.path.insert(0,sys.argv.pop(1));from raven.launch_trace import python_application;python_application()"
    os.execvpe(command[0], [command[0], "-c", bootstrap, os.environ["RAVEN_RUNTIME_BUNDLE"], *command[1:]], os.environ)


def python_application():
    """Runs inside the application Python, even when Raven is not installed."""
    target, args = Path(sys.argv[1]).absolute(), sys.argv[1:]
    run = Path(os.environ["RAVEN_RUN_DIR"]) if os.environ.get("RAVEN_RUN_DIR") else None
    recorder, capture = None, None
    started = False
    try:
        try:
            from .trace_recorder import Recorder, PythonCapture, register_trace
            from .research_reporting import _lock
            plan = R._read_json(os.environ["RAVEN_LAUNCH_PLAN"])
            legacy = plan.get("settings", {}).get("trace_adapter") == "python-legacy-profile"
            manifest = R._read_json(run / "run.json")
            recorder = Recorder(run, job_id=manifest.get("job_id", "local"), attempt=run.name,
                                cluster=os.environ.get("SLURM_CLUSTER_NAME", "local"),
                                identity_extra={"run_id": run.name},
                                coverage={"adapter": "python-profile" if legacy else "python-sys-monitoring",
                                          "scope": "Python frames in this process; native extensions, GPU kernels, child processes and remote ranks are not traced",
                                          "limitations": "Explicit legacy profile cannot distinguish yield/return or exception unwind" if legacy else "CPython 3.12+ monitoring; pre-existing frames are unknown"})
            capture = PythonCapture(recorder, legacy=legacy)
            capture.start()
            started = True
            register_trace(plan["project_root"], recorder.path)
            with _lock(run):
                folder = R._under(run, "trace/index.json", create_parent=True)
                value = R._read_json(folder) if folder.exists() else {"schema": "raven.trace-index/v1", "sources": []}
                if len(value["sources"]) >= 256:
                    raise ValueError("trace attachment index is full (128 application hooks)")
                for name in ("manifest.json", "events.jsonl"):
                    value["sources"].append({"path": str((recorder.directory / name).relative_to(run)),
                                             "schema": "raven.trace/v1" if name == "manifest.json" else "raven.trace-event/v1",
                                             "status": "recording", "reason": "", "stream": recorder.stream})
                R._atomic_json(folder, value, replace=True)
        except Exception as exc:
            if capture is not None:
                _stop_capture(capture, recorder, run)
            started = False
            if recorder is not None:
                try:
                    recorder._fail("PARTIAL", "Optional Python tracing could not start: " + str(exc))
                except Exception:
                    pass
            _diagnostic(run, "Optional Python tracing unavailable; application executes normally: " + str(exc))
        # Remove the bootstrap's private zip from import precedence after its
        # modules have loaded. Ordinary application imports follow the script.
        archive = os.environ.get("RAVEN_RUNTIME_BUNDLE", "")
        sys.path[:] = [str(target.parent)] + [item for item in sys.path if item != archive and item not in ("", str(target.parent))]
        sys.argv = args
        runpy.run_path(str(target), run_name="__main__")
    finally:
        if capture is not None and started:
            _stop_capture(capture, recorder, run)
        if recorder is not None:
            try:
                recorder.close()
                with _lock(run):
                    index = run / "trace" / "index.json"
                    if index.exists():
                        value = R._read_json(index)
                        for entry in value["sources"]:
                            if entry.get("stream") == recorder.stream:
                                entry["status"] = "complete" if recorder.state == "CLOSED" else "partial"
                                entry["reason"] = "; ".join(recorder.warnings)
                        R._atomic_json(index, value, replace=True)
            except Exception as exc:
                _diagnostic(run, "Trace finalization failed: " + str(exc))
