"""Immutable existing-script launches using the tracked reporting protocol.

The batch shell becomes a small supervisor. The original script is executed,
never sourced, from captured bytes with inherited Slurm streams. The private
stdlib-only runtime does not require Raven in the application's environment.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import signal
import socket
import stat
import subprocess
import sys
import time
import uuid
import zipfile

from . import reporting as R
from . import research_reporting as reporting

MAX_BUNDLE_BYTES = 64 << 20
MAX_BUNDLE_FILES = 1024
SHELL_HELPER = '''# Source this optional helper after environment/module setup.
# No traps or shell options are changed. Outside Raven, execute normally.
raven_exec() {
    if [ -n "${RAVEN_RUN_DIR-}" ] && [ -n "${RAVEN_RUNTIME_BUNDLE-}" ]; then
        "$RAVEN_REPORT_PYTHON" -I "$RAVEN_RUNTIME_BUNDLE" hook -- "$@"
    else
        "$@"
    fi
}
raven_metric() {
    "$RAVEN_REPORT_PYTHON" -I "$RAVEN_REPORT_HELPER" metric "$@"
}
'''


def _snapshot(path, expected):
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size > R.MAX_SCRIPT:
            raise ValueError("source script must be regular and at most 8 MiB")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            data = stream.read(R.MAX_SCRIPT + 1)
        after = os.fstat(fd)
        named = os.stat(path, follow_symlinks=False)
        signature = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
        if signature(before) != signature(after) or signature(after) != signature(named):
            raise ValueError("script changed during capture; preview the launch again")
        if len(data) > R.MAX_SCRIPT or hashlib.sha256(data).hexdigest() != expected:
            raise ValueError("script changed after preview; preview the launch again")
        return data
    finally:
        os.close(fd)


def _exclusive(path, data, mode=0o400):
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
        os.fchmod(stream.fileno(), mode)


def _bundle(path):
    """Package only bounded Python sources; never copy credentials or data."""
    package = Path(__file__).parent
    if not package.is_dir():
        # Launching from an already portable zip is deliberately unsupported:
        # this entry point is a compute-side runtime, not a new submit client.
        raise ValueError("launch materialization requires the installed Raven package")
    paths = sorted(package.rglob("*.py"))
    if len(paths) > MAX_BUNDLE_FILES:
        raise ValueError("portable runtime exceeds its source-file budget")
    total = 0
    with zipfile.ZipFile(path, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("__main__.py", "from raven.launch_runtime import main\nraise SystemExit(main())\n")
        for source in paths:
            if source.is_symlink():
                raise ValueError("portable runtime sources cannot be symlinks")
            data = source.read_bytes()
            total += len(data)
            if total > MAX_BUNDLE_BYTES:
                raise ValueError("portable runtime exceeds its 64 MiB source budget")
            archive.writestr("raven/" + str(source.relative_to(package)), data)
    os.chmod(path, 0o400)
    return hashlib.sha256(path.read_bytes()).hexdigest()




def plan_inventory(plan):
    """Small run index record; exact argv/settings remain in the frozen plan."""
    keys = ("schema", "token", "project_root", "script_relative", "script_sha256", "execution",
            "filesystem_id", "filesystem_host", "features", "gpu", "trace")
    return {**{key: plan[key] for key in keys if key in plan},
            "plan_path": ".raven/wrappers/" + plan["token"] + "/plan.json",
            "plan_sha256": hashlib.sha256(R._encoded(plan)).hexdigest()}


def project_filesystem(root, *, create=False):
    """Identify one explicitly shared project mount without hostname guessing."""
    with reporting._lock(root):
        path = R._under(root, ".raven/filesystem.json")
        if not path.exists() and create:
            R._atomic_json(path, {"schema": "raven.project-filesystem/v1", "id": uuid.uuid4().hex,
                                  "origin_host": socket.gethostname()})
        value = R._read_json(path)
        if (not isinstance(value, dict) or value.get("schema") != "raven.project-filesystem/v1"
                or not re.fullmatch(r"[0-9a-f]{32}", str(value.get("id", "")))
                or not isinstance(value.get("origin_host"), str) or not value["origin_host"]):
            raise ValueError("invalid project filesystem marker")
        return value


def materialize(plan, *, token=None):
    """Freeze validated source/plan/runtime before any scheduler invocation."""
    if plan.get("schema") != "raven.launch-plan/v1":
        raise ValueError("expected a validated Raven launch plan")
    plan = json.loads(json.dumps(plan, allow_nan=False))
    root = R._directory(plan["project_root"])
    source = R._under(root, plan["script_relative"])
    if source != Path(plan["script"]):
        raise ValueError("launch source disagrees with its project membership")
    data = _snapshot(source, plan["script_sha256"])
    token = token or uuid.uuid4().hex
    if not re.fullmatch(r"[0-9a-f]{32}", token):
        raise ValueError("invalid launch token")
    interpreter = plan.get("report_python") or plan.get("settings", {}).get("report_python") or sys.executable
    if not os.path.isabs(interpreter) or not os.access(interpreter, os.X_OK):
        raise ValueError("reporting Python must be an absolute executable on the submission host")
    reporting.initialize(root)
    filesystem = project_filesystem(root, create=True)
    parent = R._under(root, ".raven/wrappers/.unused", create_parent=True).parent
    folder = parent / token
    folder.mkdir(mode=0o700)
    # Do not erase a partially materialized bundle on failure: no scheduler was
    # called, and the private directory provides precise diagnostic evidence.
    payload = folder / "payload.sh"
    _exclusive(payload, data)
    archive = folder / "runtime.pyz"
    digest = _bundle(archive)
    helper = folder / "report.py"
    _exclusive(helper, ("import sys\nsys.path.insert(0, " + repr(str(archive)) + ")\n"
                        "from raven.research_reporting import main\nraise SystemExit(main())\n").encode())
    _exclusive(folder / "shell.sh", SHELL_HELPER.encode())
    plan.update(token=token, runtime_bundle=str(archive), runtime_sha256=digest,
                report_python=interpreter, source_snapshot=str(payload.relative_to(root)),
                run_id_base="launch-" + token, filesystem_host=filesystem["origin_host"], filesystem_id=filesystem["id"])
    if plan.get("ui_capture_logs") and plan["execution"] == "local":
        plan["local_logs"] = {stream: str(folder / (stream + ".log")) for stream in ("stdout", "stderr")}
        for path in plan["local_logs"].values():
            _exclusive(Path(path), b"", mode=0o600)
    _exclusive(folder / "plan.json", R._encoded(plan))
    header = "#!/bin/sh\n" + "".join(line.rstrip("\r\n") + "\n" for line in plan.get("sbatch_header", []))
    command = [interpreter, "-I", str(archive), "supervise", str(folder / "plan.json")]
    _exclusive(folder / "batch.sh", (header + "exec " + shlex.join(command) + "\n").encode(), 0o500)
    os.chmod(folder, 0o500)
    return plan


def start_local_plan(plan, *, environ=None):
    """Start the existing supervisor independently of the terminal UI.

    The returned receipt means the supervisor was started, not that the
    application succeeded. Its exact run retains the eventual outcome. Closing
    Raven leaves it running; no dashboard thread installs signal handlers.
    """
    if plan.get("execution") != "local":
        raise ValueError("Background local execution requires explicit standalone selection")
    plan = materialize(dict(plan, ui_capture_logs=True))
    folder = Path(plan["runtime_bundle"]).parent
    streams = []
    try:
        for name in ("stdout", "stderr"):
            fd = os.open(plan["local_logs"][name], os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW)
            streams.append(os.fdopen(fd, "wb"))
        process = subprocess.Popen([plan["report_python"], "-I", plan["runtime_bundle"], "supervise", str(folder / "plan.json")],
            cwd=plan["submit_dir"], env=dict(os.environ if environ is None else environ),
            stdin=subprocess.DEVNULL, stdout=streams[0], stderr=streams[1], start_new_session=True)
    finally:
        for stream in streams:
            stream.close()
    # Reap the child independently; the shared reader remains available for
    # telemetry while the workload executes.
    import threading
    threading.Thread(target=process.wait, name="raven-local-reaper", daemon=True).start()
    return {"schema": "raven.launch-receipt/v1", "execution": "local", "status": "supervisor-started",
            "pid": process.pid, "token": plan["token"], "run_id": plan["run_id_base"] + "-a1",
            "run_dir": str(Path(plan["project_root"]) / "runs" / (plan["run_id_base"] + "-a1")),
            "project_root": plan["project_root"], "logs": plan["local_logs"], "readiness": plan.get("readiness", [])}


def _request(plan):
    settings = plan.get("settings", {})
    request = plan.get("auto_export", settings.get("auto_export"))
    return request if isinstance(request, dict) and request.get("enabled", True) else None


def execute_plan(plan, *, runner=subprocess.run, environ=None):
    """One submission, or one explicitly requested local workload execution."""
    env = dict(os.environ if environ is None else environ)
    plan = materialize(plan)
    root = Path(plan["project_root"])
    if plan["execution"] == "local":
        code, run = supervise(plan, environ=env)
        return {"schema": "raven.launch-receipt/v1", "execution": "local", "exit_code": code,
                "token": plan["token"], "run_id": run.name, "run_dir": str(run),
                "project_root": str(root), "project_source": plan.get("project_source"),
                "readiness": plan.get("readiness", [])}
    if plan["execution"] != "slurm":
        raise ValueError("unsupported launch transport; run raven launch on the submission host")
    reporting._managed_driver_admission("submit")
    options = list(plan["sbatch_options"])
    # Slurm's default is the source basename. The generated batch file must
    # never leak into the effective name, while user/env/directive names win.
    if not any(item.startswith("--job-name=") for item in options) and plan.get("option_sources", {}).get("job-name", "default") == "default":
        options.append("--job-name=" + plan["name"])
    wrapper = str(Path(plan["runtime_bundle"]).with_name("batch.sh"))
    effective = plan.get("effective_options", {})
    pattern = "slurm-%A_%a.out" if effective.get("array") else "slurm-%j.out"
    logs = {"stdout": effective.get("output", effective.get("stdout", pattern))}
    if effective.get("error", effective.get("stderr")) not in (None, logs["stdout"]):
        logs["stderr"] = effective.get("error", effective.get("stderr"))
    settings = plan.get("settings", {})
    intent = {"schema": "raven.launch/v1", "token": plan["token"], "status": "submitting",
              "script": plan["script_relative"], "source_snapshot": plan["source_snapshot"],
              "run_id_base": plan["run_id_base"], "experiment": settings.get("experiment", plan["name"]),
              "name": plan["name"], "parameters": R._parameters(settings.get("parameters", {})),
              "resources": R._resources(settings.get("resources", {})),
              "dependency": effective.get("dependency", ""), "array": bool(effective.get("array")),
              "submitted": time.time(), "workdir": plan["workdir"], "logs": logs,
              "plan": plan, "argv": ["sbatch", "--parsable", *options, wrapper, *plan.get("script_args", [])]}
    if settings.get("array_manifest"):
        from .array_manifest import parse
        declaration = R._read_json(R._under(root, settings["array_manifest"]))
        parse(R._encoded(dict(declaration, array_id="1", cluster=""), 4 << 20))
        if not effective.get("array"):
            raise ValueError("an array manifest requires an explicit Slurm array")
        intent["array_manifest"] = declaration
    request = _request(plan)
    if request:
        try:
            from .auto_export import capture_launch
            capture_launch(root, plan["token"], request)
        except (OSError, ValueError, ImportError) as exc:
            print("Raven: automatic export request remains in the launch plan: " + str(exc), file=sys.stderr)
    output = reporting._submit_intent(root, intent, runner=runner, cwd=plan["submit_dir"], environment=env)
    receipt = R._read_json(root / ".raven" / "launches" / (plan["token"] + ".json"))
    return {"schema": "raven.launch-receipt/v1", "execution": "slurm", "job_id": output.split(";")[0],
            "cluster": receipt.get("cluster", ""), "token": plan["token"], "run_id": receipt["run_id"],
            "run_dir": str(root / "runs" / receipt["run_id"]), "project_root": str(root),
            "project_source": plan.get("project_source"), "readiness": plan.get("readiness", []),
            "intent": str(root / ".raven" / "launches" / (plan["token"] + ".json"))}


def _warn(run, text):
    try:
        print("Raven recording: " + text, file=sys.stderr)
    except OSError:
        pass
    try:
        with reporting._lock(run):
            manifest = R._read_json(run / "run.json")
            warnings = manifest.setdefault("metadata", {}).setdefault("automation", {}).setdefault("warnings", [])
            # Keep the discoverable run inventory below its 64 KiB contract.
            # Complete diagnostics are also printed on the inherited stderr.
            if sum(len(item.encode("utf-8")) for item in warnings) + len(text.encode("utf-8")) <= 8192:
                warnings.append(text)
            elif not warnings or warnings[-1] != "Further recording diagnostics are in the workload stderr (run inventory warning budget reached).":
                warnings.append("Further recording diagnostics are in the workload stderr (run inventory warning budget reached).")
            R._atomic_json(run / "run.json", manifest, replace=True)
    except (OSError, ValueError):
        pass


def _initialize_run(plan, env):
    root = Path(plan["project_root"])
    settings = plan.get("settings", {})
    if project_filesystem(root)["id"] != plan.get("filesystem_id"):
        raise ValueError("project filesystem marker changed; this launch belongs to another project mount")
    if plan["execution"] == "slurm":
        run = reporting.start(root, environ=dict(env, RAVEN_SUBMISSION_TOKEN=plan["token"]))
    else:
        run = R.begin_run(root, plan["run_id_base"] + "-a1", name=plan["name"],
                          script=plan["source_snapshot"], experiment_id=settings.get("experiment", plan["name"]),
                          parameters=settings.get("parameters", {}), resources=settings.get("resources", {}))
    with reporting._lock(run):
        R._atomic_json(run / ".execution.json", {"schema": "raven.execution/v1", "pid": os.getpid(), "started": time.time()})
        manifest = R._read_json(run / "run.json")
        manifest["provenance"].update(script=plan["script_relative"], script_sha256=plan["script_sha256"])
        manifest.setdefault("metadata", {})["automation"] = {
            "schema": "raven.automation/v1", "plan": plan_inventory(plan), "project_root": str(root),
            "filesystem_id": plan["filesystem_id"], "filesystem_host": plan.get("filesystem_host", socket.gethostname()), "runtime_host": socket.gethostname(), "execution": plan["execution"],
            "source_semantics": "$0 and BASH_SOURCE identify the immutable payload; source-relative discovery needs an explicit application hook",
            "logs": ("Inherited scheduler streams" if plan["execution"] == "slurm" else
                     "Captured local stdout/stderr" if plan.get("local_logs") else "Inherited local streams; no automatic local copy"),
            "application_outcome": "pending", "scheduler_outcome": "unknown",
            "warnings": list(manifest.get("metadata", {}).get("raven_launch", {}).get("log_warnings", []))}
        # begin_run's default run-local logs are unused when preserving the
        # original streams. Do not advertise those empty placeholders as data.
        index = R._read_json(run / "logs.json")
        index["logs"] = [entry for entry in index["logs"] if not entry["id"].startswith("application.")]
        for key in ("stdout", "stderr"):
            manifest["paths"].pop(key, None)
        R._atomic_json(run / "logs.json", index, replace=True)
        R._atomic_json(run / "run.json", manifest, replace=True)
        _launch_report(run, plan, manifest)
    for stream, path in plan.get("local_logs", {}).items():
        if plan["execution"] == "local" and stream in ("stdout", "stderr"):
            expected = str(Path(plan["runtime_bundle"]).parent / (stream + ".log"))
            if path != expected:
                raise ValueError("local log disagrees with the captured runtime")
            R.register_log(run, "local." + stream, path, label="Local " + stream, group="Application")
    _attach_declarations(run, plan)
    request = _request(plan)
    if request:
        try:
            from .auto_export import capture
            capture(run, request)
        except (OSError, ValueError, ImportError) as exc:
            _warn(run, "Automatic export request could not be captured: " + str(exc))
    return run




def _launch_report(run, plan, manifest, *, exit_code=None):
    launch = manifest.get("metadata", {}).get("raven_launch", {})
    receipt = {"run_id": run.name, "job_id": manifest.get("job_id"),
               "attempt": manifest.get("attempt", 1), "cluster": launch.get("cluster", ""),
               "token": plan["token"], "execution": plan["execution"],
               "application_exit_code": exit_code, "scheduler_outcome": "unknown"}
    R._atomic_json(run / "reports" / "submit.json", {"schema": "raven.launch-report/v1", "plan": plan,
                                                         "receipt": receipt}, replace=True)


def _declared_path(root, value, run):
    # An explicit per-attempt placeholder avoids guessing scientific ownership
    # of one shared mutable file when arrays/requeues run concurrently.
    from .projects import relative_path
    value = relative_path(value.replace("{run_id}", run.name))
    path = root
    for part in Path(value).parts:
        path /= part
        if path.is_symlink():
            raise ValueError("declared source cannot traverse a symlink")
        if path.exists() and path != root / value and not path.is_dir():
            raise ValueError("declared source parent must be a directory")
    return path


def _attach_declarations(run, plan):
    settings, root = plan.get("settings", {}), Path(plan["project_root"])
    try:
        with reporting._lock(run):
            manifest = R._read_json(run / "run.json")
            metadata = manifest["metadata"]["automation"]
            if settings.get("application_metrics"):
                value = settings["application_metrics"]
                metadata["metric_binding"] = {"path": str(_declared_path(root, value, run)),
                    "format": "raven.metrics/v1", "ownership": "explicit producer declaration"}
                if "{run_id}" not in value:
                    metadata["warnings"].append("Declared metric file ownership is supplied by the producer; use {run_id} in its path to isolate concurrent attempts. Raven cannot infer scientific ownership of shared files.")
            bindings = []
            for item in settings.get("artifacts", []):
                entry = dict(item) if isinstance(item, dict) else {"path": item, "required": True}
                from .artifacts import _contract
                _contract({"version": 1, "outputs": [entry]})
                entry["path"] = str(_declared_path(root, entry["path"], run))
                bindings.append(entry)
            if bindings:
                metadata["artifact_bindings"] = bindings
            if settings.get("scientific_context"):
                metadata["scientific_context"] = settings["scientific_context"]
            R._encoded(manifest, 48 << 10)  # reserve inventory room for bounded coverage/errors
            R._atomic_json(run / "run.json", manifest, replace=True)
    except (ValueError, OSError) as exc:
        _warn(run, "Declared source could not be attached: " + str(exc))



def _register_research_source(run, feature, path, schema, *, status="declared"):
    with reporting._lock(run):
        manifest = R._read_json(run / "run.json")
        bindings = manifest["metadata"]["automation"].setdefault("research_bindings", [])
        entry = {"id": feature, "path": str(Path(path).relative_to(run)), "schema": schema, "status": status}
        bindings[:] = [previous for previous in bindings if previous.get("id") != feature] + [entry]
        R._encoded(manifest, 48 << 10)
        R._atomic_json(run / "run.json", manifest, replace=True)


def _finalize_sources(run, plan):
    settings, root = plan.get("settings", {}), Path(plan["project_root"])
    for feature, source in settings.get("research_sources", {}).items():
        try:
            from .launch_features import FEATURE_REGISTRY
            view = FEATURE_REGISTRY[feature]["source"]
            document = R._read_json(_declared_path(root, source, run))
            if view not in R.REPORT_VIEWS:
                if not isinstance(document, dict):
                    raise ValueError("declared native Research source must be a JSON object")
                target = run / "reports" / ("declared-" + feature + ".json")
                R._atomic_json(target, document, replace=True)
                _register_research_source(run, feature, target, str(document.get("schema", document.get("kind", "declared JSON"))), status="unverified")
                _warn(run, "Native " + feature + " source retained for export; its dedicated Research reader is still required")
                continue
            with reporting._lock(run):
                R.publish_research(run, view, document)
        except Exception as exc:
            _warn(run, "Native " + feature + " source unavailable: " + str(exc))
    if "passport" in plan.get("features", []):
        try:
            from .provenance import capture, save
            value = capture(root, script=plan["source_snapshot"], parameters=settings.get("parameters", {}),
                            resources=settings.get("resources", {}), inputs=[], environment_names=[],
                            job_id=R._read_json(run / "run.json").get("job_id"))
            saved = save(value, run / "passports")
            _register_research_source(run, "passport", saved, value["schema"])
        except Exception as exc:
            _warn(run, "Passport unavailable: " + str(exc))


def supervise(plan, *, environ=None):
    """Run captured shell bytes once, with optional recording failures isolated."""
    env = dict(os.environ if environ is None else environ)
    run = _initialize_run(plan, env)
    folder = Path(plan["runtime_bundle"]).parent
    env.update(RAVEN_RUN_DIR=str(run), RAVEN_RUN_ID=run.name, RAVEN_PROJECT_ROOT=plan["project_root"],
               RAVEN_REPORT_HELPER=str(folder / "report.py"), RAVEN_REPORT_PYTHON=plan["report_python"],
               RAVEN_SHELL_HELPER=str(folder / "shell.sh"), RAVEN_RUNTIME_BUNDLE=plan["runtime_bundle"],
               RAVEN_LAUNCH_PLAN=str(folder / "plan.json"), RAVEN_SOURCE_SCRIPT=plan["script"])
    process, recording = None, None
    signals = []
    # Local foreground signals reach only our foreground group. Give its child
    # a separate group and forward once. Slurm already signals its cgroup;
    # forwarding cancellation again would duplicate traps. Only B: pre-time
    # signals explicitly target the supervisor alone and need forwarding.
    signal_spec = plan.get("effective_options", {}).get("signal", "")
    batch_signal = None
    if signal_spec.startswith("B:"):
        value = signal_spec[2:].split("@", 1)[0]
        batch_signal = int(value) if value.isdecimal() else getattr(signal, "SIG" + value.removeprefix("SIG"), None)
    caught = {signal.SIGINT, signal.SIGTERM, signal.SIGHUP}
    if batch_signal is not None and batch_signal not in {signal.SIGKILL, signal.SIGSTOP}:
        caught.add(batch_signal)
    def forward(signum, _frame):
        if len(signals) < 64:
            signals.append(signum)
        if process is not None and process.poll() is None and (plan["execution"] == "local" or signum == batch_signal):
            try:
                if plan["execution"] == "local":
                    os.killpg(process.pid, signum)
                else:
                    # B: targets the original batch shell, not its job steps
                    # or background applications. Preserve that boundary.
                    process.send_signal(signum)
            except ProcessLookupError:
                pass
    handlers = {sig: signal.signal(sig, forward) for sig in caught}
    started = time.monotonic()
    code, child_signal = 127, False
    try:
        command = [*plan["interpreter"], str(folder / "payload.sh"), *plan.get("script_args", [])]
        process = subprocess.Popen(command, cwd=plan["workdir"], env=env, start_new_session=True)
        for signum in tuple(signals):
            if plan["execution"] == "local" or signum == batch_signal:
                try:
                    if plan["execution"] == "local":
                        os.killpg(process.pid, signum)
                    else:
                        process.send_signal(signum)
                except ProcessLookupError:
                    pass
        try:
            from .retained_telemetry import start_recording
            recording = start_recording(run, dict(plan, runtime_pid=process.pid), environ=env)
        except Exception as exc:
            _warn(run, "Optional telemetry could not start: " + str(exc))
        code = process.wait()
        child_signal = code < 0
        code = 128 - code if code < 0 else code
    except OSError as exc:
        _warn(run, "Application could not be executed: " + str(exc))
    finally:
        if recording is not None:
            try:
                coverage = recording.close()
                with reporting._lock(run):
                    manifest = R._read_json(run / "run.json")
                    manifest["metadata"]["automation"]["recording"] = {
                        "index": "telemetry/index.json", "status": coverage.get("status", "unknown"),
                        "sources": [{key: source[key] for key in ("family", "status", "reason", "samples", "missed_samples", "scope") if key in source}
                                    for source in coverage.get("sources", [])[:16]]}
                    R._atomic_json(run / "run.json", manifest, replace=True)
                if getattr(recording, "error", ""):
                    _warn(run, "Telemetry unavailable: " + str(recording.error))
            except Exception as exc:
                _warn(run, "Optional telemetry could not finish: " + str(exc))
        for sig, handler in handlers.items():
            signal.signal(sig, handler)
    _finalize_sources(run, plan)
    terminal_signal = child_signal or any(sig in {signal.SIGINT, signal.SIGTERM, signal.SIGHUP} for sig in signals)
    try:
        with reporting._lock(run):
            manifest = R._read_json(run / "run.json")
            manifest["metadata"]["automation"].update(application_outcome="exited", application_exit_code=code,
                                                       observed_signals=signals[:64])
            R._atomic_json(run / "run.json", manifest, replace=True)
            _launch_report(run, plan, manifest, exit_code=code)
        reporting.finish(run, exit_code=code, state="INTERRUPTED" if terminal_signal else None,
                         runtime_seconds=time.monotonic() - started,
                         scaling=plan.get("settings", {}).get("scientific_context") if set(plan.get("settings", {}).get("scientific_context", {})) == {"workers", "problem_size", "repeat"} else None)
    except Exception as exc:
        _warn(run, "Application exited with status %s; final reporting failed: %s" % (code, exc))
    if _request(plan):
        try:
            from .auto_export import process_pending
            process_pending(plan["project_root"], run_ids=[run.name], deadline_seconds=5.0, limit=1)
        except Exception as exc:
            _warn(run, "Automatic export is pending recovery: " + str(exc))
    return code, run


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        if argv[:1] == ["supervise"] and len(argv) == 2:
            code, _run = supervise(R._read_json(argv[1]))
            return code
        if argv[:1] == ["hook"]:
            from .launch_trace import hook
            return hook(argv[2:] if argv[1:2] == ["--"] else argv[1:])
        print("Raven portable runtime: expected supervise PLAN or hook -- COMMAND", file=sys.stderr)
        return 2
    except (OSError, ValueError, KeyError) as exc:
        print("Raven runtime unavailable before workload start: " + str(exc), file=sys.stderr)
        return 125


if __name__ == "__main__":
    raise SystemExit(main())
