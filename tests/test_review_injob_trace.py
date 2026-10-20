"""Application noninterference and complete-prefix adversarial regressions."""

import fcntl
import io
import json
import os
import re
import shlex
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from raven import trace_export
from raven import trace_protocol as protocol
from raven.launch import resolve_plan
from raven.launch_runtime import execute_plan, materialize
from raven.trace_recorder import PythonCapture, Recorder, register_trace


def test_application_callback_does_not_wait_for_stalled_fsync_and_metadata_is_once(
    tmp_path, monkeypatch
):
    recorder = Recorder(tmp_path, journal_format="blocks", flush_interval=0.01)
    capture = PythonCapture(recorder, scope="project", roots=(tmp_path,))
    program = {}
    exec(compile("def small(): return 7\n", str(tmp_path / "app.py"), "exec"), program)
    entered, release = threading.Event(), threading.Event()
    original = os.fsync

    def stalled(fd):
        if threading.current_thread().name == "RavenTraceFlush":
            entered.set()
            assert release.wait(5)
        original(fd)

    monkeypatch.setattr(os, "fsync", stalled)
    capture.start()
    assert capture._included(program["small"].__code__), capture.excluded_prefixes
    try:
        recorder._force_flush = True
        recorder._flush_wake.set()
        assert entered.wait(2)
        started = time.monotonic()
        for _ in range(100):
            assert program["small"]() == 7
        assert not capture._stopped, (recorder.state, recorder.warnings, capture.threads)
        assert len(recorder._frame_queue) >= 200, (
            capture.tool_id,
            capture.threads,
            capture._scope_cache,
        )
        assert time.monotonic() - started < 0.5
    finally:
        release.set()
        capture.stop()
        recorder.close()
    snapshot = protocol.read_trace(recorder.path)
    assert snapshot["status"] == "COMPLETE", snapshot["warnings"]
    assert len([event for event in snapshot["events"] if event["kind"] == "code"]) == 1
    assert len([event for event in snapshot["events"] if event["kind"] == "call"]) == 100
    calls = [event for event in snapshot["events"] if event["kind"] == "call"]
    assert all(event["data"]["function"] == "small" for event in calls)
    raw = (recorder.directory / "events.jsonl").read_bytes()
    assert raw.count(b'"function":"small"') == 1


def test_busy_project_registry_has_a_short_deadline(tmp_path):
    recorder = Recorder(tmp_path, register=False, background_flush=False)
    lock = os.open(tmp_path / ".raven/traces/.index.lock", os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(lock, fcntl.LOCK_EX)
    try:
        started = time.monotonic()
        with pytest.raises(TimeoutError, match="registry is busy"):
            register_trace(tmp_path, recorder.path)
        assert time.monotonic() - started < 1
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        os.close(lock)
        recorder.close()


def test_legacy_registry_above_old_byte_limit_is_compacted_not_disabled(tmp_path):
    recorder = Recorder(tmp_path, job_id="new", register=False, background_flush=False)
    older = tmp_path / "older-source"
    older.write_text("retained")
    index = tmp_path / ".raven/traces/index.json"
    index.write_text(json.dumps({"schema": "raven.trace-index/v1", "records": [
        {"job_id": "old", "path": "older-source", "padding": "x" * 500}
        for _ in range(2200)]}))
    assert index.stat().st_size > 1 << 20
    try:
        register_trace(tmp_path, recorder.path)
        value = protocol.read_json(index)
        assert index.stat().st_size <= 1 << 20
        assert value["omitted_registrations"] > 0
        assert value["records"][-1]["path"] == str(recorder.path.relative_to(tmp_path))
    finally:
        recorder.close()


def test_legacy_descriptive_code_event_does_not_gain_encoding_semantics(tmp_path):
    with Recorder(tmp_path, background_flush=False) as recorder:
        recorder.emit("code", {"description": "existing user event"})
    snapshot = protocol.read_trace(recorder.path)
    assert snapshot["status"] == "COMPLETE"
    assert any(event["data"].get("description") == "existing user event" for event in snapshot["events"])


def test_helper_pool_cannot_spend_main_capacity(tmp_path):
    from raven.launch_autotrace import _RunBudget, budget_limits

    folder = tmp_path / ".raven-runtime"
    folder.mkdir()
    for name in ("trace-budget", "trace-helper-budget"):
        path = folder / name
        path.write_bytes(struct.pack("!QQ", 0, 0))
        path.chmod(0o600)
    settings = {"trace_processes": 8, "trace_run_bytes": 8192}
    helpers = [_RunBudget(tmp_path, settings, helper=True) for _ in range(2)]
    try:
        with pytest.raises(ValueError, match="process budget exhausted"):
            _RunBudget(tmp_path, settings, helper=True)
        main = _RunBudget(tmp_path, settings)
        try:
            assert main.reserve(budget_limits(settings)[1])
            assert not main.reserve(1)
        finally:
            main.close()
    finally:
        for helper in helpers:
            helper.close()


def test_project_scope_excludes_an_environment_inside_project(tmp_path, monkeypatch):
    import sysconfig

    environment = tmp_path / "env"
    library = environment / "lib/python3.12"
    library.mkdir(parents=True)
    monkeypatch.setattr(sysconfig, "get_paths", lambda: {"stdlib": str(library)})
    recorder = Recorder(tmp_path, background_flush=False)
    capture = PythonCapture(recorder, scope="project", roots=(tmp_path,))
    try:
        assert not capture._included(compile("x=1", str(library / "json.py"), "exec"))
        assert capture._included(compile("x=1", str(tmp_path / "application.py"), "exec"))
    finally:
        recorder.close()


@pytest.mark.parametrize("marker", ["pyvenv.cfg", "conda-meta"])
def test_project_scope_excludes_environment_markers_before_callbacks(tmp_path, monkeypatch, marker):
    environment = tmp_path / "alternate-environment"
    library = environment / "lib/python3.12"
    library.mkdir(parents=True)
    if marker == "pyvenv.cfg":
        (environment / marker).write_text("home = /usr/bin\n")
    else:
        (environment / marker).mkdir()
    monkeypatch.setattr(sys, "path", [str(library), *sys.path])
    recorder = Recorder(tmp_path, background_flush=False)
    capture = PythonCapture(recorder, scope="project", roots=(tmp_path,))
    try:
        assert not capture._included(compile("x=1", str(library / "json.py"), "exec"))
    finally:
        recorder.close()


def test_full_prefix_exports_inspection_and_summary_cross_page_boundaries(tmp_path):
    from raven.trace_stream_export import inspect
    from raven.trace_summary import summarize_path

    recorder = Recorder(
        tmp_path,
        background_flush=False,
        journal_format="blocks",
        flush_events=65536,
        initial_mono_ns=1,
        coverage={"adapter": "python-sys-monitoring", "scope": "explicit test frames"},
    )
    # The outer interval spans every page and the old 20,000-event retained cap.
    recorder.emit("call", {"frame": "outer", "function": "outer"}, thread="one", mono_ns=2)
    for index in range(10500):
        recorder.emit(
            "call", {"frame": "inner", "function": "inner"}, thread="one", mono_ns=3 + 2 * index
        )
        recorder.emit("return", {"frame": "inner"}, thread="one", mono_ns=4 + 2 * index)
    recorder.emit("return", {"frame": "outer"}, thread="one", mono_ns=21003)
    recorder.close()
    committed = json.loads(recorder.path.read_text())["committed_seq"]
    for format in ("perfetto", "speedscope", "html"):
        output = tmp_path / (format + ".json")
        receipt = trace_export.export(recorder.path, output, format=format)
        assert receipt["read_seq"] == committed
        if format == "perfetto":
            value = json.loads(output.read_text())
            assert len([event for event in value["traceEvents"] if event["ph"] == "X"]) == 10501
            assert value["raven"]["evicted_events"] == 0
        elif format == "speedscope":
            value = json.loads(output.read_text())
            assert len(value["profiles"][0]["events"]) == 21002
        else:
            model = json.loads(
                re.search(
                    r'<script id="trace-data" type="application/json">(.*?)</script>',
                    output.read_text(),
                    re.S,
                )[1]
            )
            assert len(model["intervals"]) == 10501
    output = io.StringIO()
    inspect(recorder.path, json_output=True, output=output)
    assert len(json.loads(output.getvalue())["events"]) == committed
    summary = summarize_path(recorder.path)
    assert sum(group["count"] for group in summary["sources"][0]["groups"]) == 10501
    assert summary["sources"][0]["last_seq"] == committed


@pytest.mark.parametrize("traced", [False, True])
def test_installed_application_raven_package_keeps_precedence(tmp_path, traced):
    # Simulate the application's own installed Sentry package through its site
    # directory. It must precede the appended Raven runtime fallback.
    site = tmp_path / "application-site"
    (site / "raven").mkdir(parents=True)
    (site / "raven/__init__.py").write_text("class Client: pass\n")
    script = tmp_path / "job.sh"
    script.write_text(
        "#!/bin/sh\n"
        + shlex.join(
            [sys.executable, "-c", "from raven import Client; assert Client.__name__ == 'Client'"]
        )
        + "\n"
    )
    env = {key: value for key, value in os.environ.items() if not key.startswith("RAVEN_")}
    env["PYTHONPATH"] = str(site)
    args = [
        str(script),
        "--project",
        str(tmp_path),
        "--local",
        "--trace" if traced else "--no-trace",
    ]
    if traced and sys.version_info < (3, 12):
        args += ["--trace-adapter", "python-legacy-profile"]
    assert (
        execute_plan(resolve_plan(args, cwd=tmp_path, environ=env), environ=env)["exit_code"] == 0
    )


def test_runtime_content_is_reused_and_cleanup_preserves_run_data(tmp_path, monkeypatch):
    import hashlib

    from raven import launch_runtime
    from raven.runtime_cleanup import cleanup

    script = tmp_path / "job.sh"
    script.write_text("#!/bin/sh\nexit 0\n")
    plan = resolve_plan(
        [str(script), "--project", str(tmp_path), "--local"], cwd=tmp_path, environ={}
    )
    first = execute_plan(plan, environ={})
    first_archive = tmp_path / ".raven/wrappers" / first["token"] / "runtime.pyz"
    # Other review packages can change shared source concurrently. Freeze this
    # archive fixture so the reuse assertion compares exactly equal content.
    archive_bytes = first_archive.read_bytes()

    def frozen_bundle(path):
        path.write_bytes(archive_bytes)
        path.chmod(0o400)
        return hashlib.sha256(archive_bytes).hexdigest()

    monkeypatch.setattr(launch_runtime, "_bundle", frozen_bundle)
    second = materialize(plan)
    second_archive = Path(second["runtime_bundle"])
    assert first_archive.stat().st_ino == second_archive.stat().st_ino
    preview = cleanup(tmp_path, older_than_days=0)
    by_token = {row["token"]: row for row in preview["wrappers"]}
    assert by_token[first["token"]]["status"] == "eligible"
    assert by_token[second["token"]]["status"] == "preserved"
    assert first_archive.exists()
    result = cleanup(tmp_path, older_than_days=0, apply=True)
    assert result["removed_bytes"] > 0 and not first_archive.exists()
    assert second_archive.exists()
    assert (Path(first["run_dir"]) / "run.json").exists()
    assert (Path(first["run_dir"]) / "summary.json").exists()


def test_managed_cleanup_cli_is_metadata_only_and_checks_owned_paths(tmp_path, monkeypatch, capsys):
    from raven import raven_execution as execution
    from raven.trace_recorder import main

    router = execution.ExecutionRouter({"managed": True})
    monkeypatch.setattr(execution, "router_for", lambda: router)
    argv = ["trace", "cleanup-runtime", "--project", str(tmp_path)]
    assert execution.cli_admission(argv, router) is None
    assert "cleanup-runtime" not in execution.OPERATIONS
    assert main(argv[1:]) == 0
    assert json.loads(capsys.readouterr().out)["wrappers"] == []
    wrappers = tmp_path / ".raven/wrappers"
    wrappers.mkdir(parents=True)
    wrappers.chmod(0o755)
    assert main([*argv[1:], "--apply"]) == 2
    assert "private owned runtime paths" in capsys.readouterr().err
    assert wrappers.exists()


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX fork/exec")
def test_fork_exec_children_do_not_attach_or_spend_process_slots(tmp_path):
    application = tmp_path / "application.py"
    application.write_text(
        "import subprocess,os\nfor _ in range(20):\n subprocess.run(['/bin/true'],preexec_fn=os.setsid,check=True)\n"
    )
    script = tmp_path / "job.sh"
    script.write_text("#!/bin/sh\n" + shlex.join([sys.executable, str(application)]) + "\n")
    args = [
        str(script),
        "--project",
        str(tmp_path),
        "--local",
        "--trace",
        "--no-cpu-memory",
        "--no-gpu",
    ]
    if sys.version_info < (3, 12):
        args += ["--trace-adapter", "python-legacy-profile"]
    receipt = execute_plan(resolve_plan(args, cwd=tmp_path, environ={}), environ={})
    assert receipt["exit_code"] == 0
    run = Path(receipt["run_dir"])
    assert struct.unpack("!QQ", (run / ".raven-runtime/trace-budget").read_bytes())[1] == 1
    assert struct.unpack("!QQ", (run / ".raven-runtime/trace-helper-budget").read_bytes())[1] == 0
    sources = json.loads((run / "trace/index.json").read_text())["sources"]
    assert len([source for source in sources if source["schema"] == "raven.trace/v1"]) == 1
    assert len(json.loads((tmp_path / ".raven/traces/index.json").read_text())["records"]) == 1


def test_near_recursion_limit_application_does_not_receive_capture_error(tmp_path):
    program = tmp_path / "recursion.py"
    program.write_text(
        "import sys\nfrom pathlib import Path\nfrom raven.trace_recorder import Recorder,PythonCapture\n"
        "def depth(n): return 0 if n == 0 else 1 + depth(n-1)\n"
        "sys.setrecursionlimit(1000)\nassert depth(985)==985\n"
        "r=Recorder(Path(__file__).parent,journal_format='blocks')\nc=PythonCapture(r,scope='project')\n"
        "c.start()\ntry:\n assert depth(985)==985\nfinally:\n c.stop();r.close()\n"
    )
    environment = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]))
    result = subprocess.run(
        [sys.executable, str(program)], capture_output=True, text=True, env=environment, timeout=15
    )
    assert result.returncode == 0, result.stderr
