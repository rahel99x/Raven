"""Automatic capture excludes proven instrumentation, preserving user code."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shlex
import sys

import pytest

from raven.launch import resolve_plan
from raven.launch_runtime import execute_plan
from raven.launching import autotrace
from raven.tracing.capture import PythonCapture, Recorder
from raven.tracing.protocol import TraceReader


def _startup(root):
    run = root / "run"
    site = run / ".raven-runtime" / "site"
    site.mkdir(parents=True, mode=0o700)
    source = site / "sitecustomize.py"
    source.write_text("def _raven_startup(): pass\n")
    source.chmod(0o400)
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    plan = {"sitecustomize_sha256": digest}
    manifest = {
        "metadata": {
            "automation": {
                "python_startup": {
                    "automatic": True,
                    "site_directory": ".raven-runtime/site",
                    "sitecustomize_sha256": digest,
                }
            }
        }
    }
    return run, source, plan, manifest


@pytest.mark.parametrize("zipped", [False, True])
def test_runtime_owner_exclusion_is_exact_and_keeps_manual_capture(
    tmp_path, monkeypatch, zipped
):
    run, startup, plan, manifest = _startup(tmp_path)
    owner = tmp_path / ("runtime.pyz/raven" if zipped else "runtime/raven")
    monkeypatch.setattr(autotrace, "__file__", str(owner / "launching/autotrace.py"))
    exclusions = autotrace._capture_exclusions(run, plan, manifest)
    assert exclusions == {
        "internal_roots": (str(owner),),
        "internal_files": (str(startup),),
    }
    (tmp_path / "recording").mkdir()
    with Recorder(tmp_path / "recording", background_flush=False) as recorder:
        automatic = PythonCapture(recorder, scope="all", **exclusions)
        manual = PythonCapture(recorder, scope="all")
        for name in ("launching/autotrace.py", "client.py", "reporting.py"):
            code = compile("pass", str(owner / name), "exec")
            assert not automatic._included(code)
            assert manual._included(code)
        assert not automatic._included(compile("pass", str(startup), "exec"))
        for user_file in (
            tmp_path / "raven.py",
            tmp_path / "sitecustomize.py",
            tmp_path / "runtime-other/raven/client.py",
            tmp_path / "runtime/raven-other/science.py",
        ):
            assert automatic._included(compile("pass", str(user_file), "exec"))


@pytest.mark.parametrize(
    "mutation", ["receipt", "bytes", "mode", "symlink", "directory", "missing"]
)
def test_unverified_generated_startup_is_never_excluded(tmp_path, mutation):
    run, source, plan, manifest = _startup(tmp_path)
    startup = manifest["metadata"]["automation"]["python_startup"]
    if mutation == "receipt":
        startup["sitecustomize_sha256"] = "0" * 64
    elif mutation == "bytes":
        source.chmod(0o600)
        source.write_text("def application_site(): pass\n")
        source.chmod(0o400)
    elif mutation == "mode":
        source.chmod(0o644)
    elif mutation == "symlink":
        target = tmp_path / "user-sitecustomize.py"
        source.rename(target)
        source.symlink_to(target)
    elif mutation == "directory":
        startup["site_directory"] = "user-site"
    else:
        source.unlink()
    assert autotrace._capture_exclusions(run, plan, manifest)["internal_files"] == ()


@pytest.mark.parametrize("startup", [None, [], "legacy descriptive extension"])
def test_descriptive_startup_extension_does_not_disable_capture(tmp_path, startup):
    run, _source, plan, manifest = _startup(tmp_path)
    manifest["metadata"]["automation"]["python_startup"] = startup
    exclusions = autotrace._capture_exclusions(run, plan, manifest)
    assert exclusions["internal_files"] == ()
    with Recorder(tmp_path, background_flush=False) as recorder:
        capture = PythonCapture(
            recorder, legacy=True, scope="project", roots=(tmp_path,), **exclusions
        )
        capture.start()
        try:
            exec(
                compile(
                    "def work(): return 17\nassert work()==17\n",
                    str(tmp_path / "science.py"),
                    "exec",
                ),
                {},
            )
        finally:
            capture.stop()
    result = TraceReader(recorder.path).refresh()
    assert result["complete"]
    assert (
        sum(
            event["kind"] == "call" and event["data"].get("function") == "work"
            for event in result["events"]
        )
        == 1
    )


@pytest.mark.parametrize("legacy", [True, False])
@pytest.mark.parametrize("scope", ["project", "all"])
@pytest.mark.parametrize("zipped", [False, True])
def test_frozen_launch_preserves_science_and_metrics_without_bootstrap_frames(
    tmp_path, monkeypatch, legacy, scope, zipped
):
    if not legacy and not hasattr(sys, "monitoring"):
        pytest.skip("CPython 3.12+ monitoring")
    # These ordinary application filenames must never become blanket ignores.
    (tmp_path / "raven.py").write_text("def application_raven(): return 7\n")
    (tmp_path / "sitecustomize.py").write_text("def application_site(): return 11\n")
    (tmp_path / "science.py").write_text(
        "import contextlib, threading, raven, sitecustomize\n"
        "from _raven_launch_runtime.client import metric\n"
        "def step(value): return value*3\n"
        "def science():\n"
        " total=sum(step(value) for value in range(7))\n"
        " assert raven.application_raven()==7\n"
        " assert sitecustomize.application_site()==11\n"
        " with contextlib.nullcontext(): threading.current_thread()\n"
        " metric('answer',total)\n"
        " return total\n"
        "assert science()==63\n"
    )
    script = tmp_path / "job.sh"
    script.write_text(
        "#!/bin/sh\nset -eu\nexec " + shlex.join([sys.executable, "science.py"]) + "\n"
    )
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("RAVEN_") and key != "PYTHONPATH"
    }
    environment["PYTHONPATH"] = str(tmp_path)
    options = [
        str(script),
        "--project",
        str(tmp_path),
        "--local",
        "--trace",
        "--trace-scope",
        scope,
    ]
    if legacy:
        options += ["--trace-adapter", "python-legacy-profile"]
    if zipped:
        from raven.launching import runtime_cache

        monkeypatch.setattr(
            runtime_cache,
            "runtime_directory",
            lambda plan: Path(plan["runtime_bundle"]),
        )
    plan = resolve_plan(options, cwd=tmp_path, environ=environment)
    receipt = execute_plan(plan, environ=environment)
    assert receipt["exit_code"] == 0
    run = Path(receipt["run_dir"])
    entries = json.loads((run / "trace/index.json").read_text())["sources"]
    recordings = [item for item in entries if item["schema"] == "raven.trace/v1"]
    assert len(recordings) == 1, entries
    reader = TraceReader(run / recordings[0]["path"])
    result = reader.refresh()
    for _ in range(100):
        if result["caught_up"]:
            break
        result = reader.refresh()
    assert result["status"] == "COMPLETE", result["warnings"]
    assert result["manifest"]["dropped"] == 0
    frames = [
        event
        for event in result["events"]
        if event["kind"] in {"call", "resume", "return", "yield", "unwind"}
    ]
    frozen = json.loads(
        (tmp_path / ".raven/wrappers" / receipt["token"] / "plan.json").read_text()
    )
    owner_root = (
        Path(frozen["runtime_bundle"])
        if zipped
        else tmp_path / ".raven/runtime-cache" / frozen["runtime_sha256"]
    ) / "raven"
    owned_startup = str(run / ".raven-runtime/site/sitecustomize.py")
    for event in frames:
        filename = event["data"].get("file", "")
        assert filename != owned_startup
        assert not filename.startswith(str(owner_root) + os.sep)
    science_files = {
        str(tmp_path / name) for name in ("science.py", "raven.py", "sitecustomize.py")
    }
    stacks = {}
    calls = []
    for event in frames:
        if event["data"].get("file") not in science_files:
            continue
        stack = stacks.setdefault(event["thread"], [])
        frame = event["data"]["frame"]
        if event["kind"] in {"call", "resume"}:
            stack.append(frame)
            calls.append(event["data"]["function"])
        else:
            assert stack and stack.pop() == frame, event
    assert all(not stack for stack in stacks.values())
    assert calls.count("step") == 7
    assert calls.count("science") == 1
    assert calls.count("application_raven") == calls.count("application_site") == 1
    if scope == "all":
        assert any(
            event["data"].get("function") == "current_thread" for event in frames
        )
    else:
        assert not result["warnings"], result["warnings"]
    metrics = [
        json.loads(line) for line in (run / "metrics.jsonl").read_text().splitlines()
    ]
    assert any(row.get("metrics", {}).get("answer") == 63 for row in metrics)
    assert any(
        event["kind"] == "counter"
        and event["data"].get("values", {}).get("answer") == 63
        for event in result["events"]
    )
