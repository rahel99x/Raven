"""Captured bytes, tool isolation, bounded resource use, and honest results."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import threading
import time

import pytest

from raven import shell_checks as S


@pytest.fixture(autouse=True)
def cache():
    S._CACHE.clear()
    S._TOOLS.clear()
    yield
    S._CACHE.clear()
    S._TOOLS.clear()


@pytest.fixture
def script(tmp_path):
    path = tmp_path / "batch script.sh"
    path.write_text("#!/bin/bash\nprintf '%s\\n' '$HOME'\n")
    return path


def analyzer(tmp_path, monkeypatch, payload="[]", exit_code=0, extra=""):
    """A real executable protocol fixture; actual syntax checker remains native."""
    path = tmp_path / "shellcheck"
    path.write_text(f"#!{sys.executable}\nimport os,sys,json\n"
                    "if '--version' in sys.argv:\n print('ShellCheck test-1');sys.exit(0)\n"
                    + extra + f"\nsys.stdout.write({payload!r})\nsys.exit({exit_code})\n")
    path.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ.get("PATH", os.defpath))
    return path


@pytest.mark.parametrize("name", ["bash", "sh", "dash"])
def test_actual_syntax_never_executes_commands_startup_hooks_or_substitutions(tmp_path, monkeypatch, name):
    if not shutil.which(name):
        pytest.skip(name + " not installed")
    marker = tmp_path / "EXECUTED"
    startup = tmp_path / "startup"
    startup.write_text(f"touch {marker}\n")
    for key in ("BASH_ENV", "ENV", "SHELLCHECK_OPTS"):
        monkeypatch.setenv(key, str(startup))
    monkeypatch.setenv("SHELLOPTS", "xtrace")
    path = tmp_path / "script"
    path.write_text(f"#!/bin/{name}\ntouch {marker}\nx=$(touch {marker})\n. {startup}\neval 'touch {marker}'\n")
    result = S.check_script(path)
    assert result["checks"][0]["status"] == "passed"
    assert not marker.exists()


def test_actual_bash_syntax_error_has_line_and_message(script):
    script.write_text("#!/bin/bash\nif then\n")
    result = S.check_script(script)
    assert result["status"] == "issues"
    assert any(item["line"] == 2 and item["level"] == "error" for item in result["findings"])


@pytest.mark.parametrize("shebang,expected", [
    ("#!/bin/bash", "bash"), ("#!/usr/bin/env bash", "bash"), ("#!/usr/bin/env -S bash", "bash"),
    ("#!/usr/bin/env sh", "sh"), ("#!/bin/dash", "dash"), ("#!/usr/bin/env python", None),
    ("#!/bin/bash -e", None), ("#!/usr/bin/env -S bash -e", None), ("#!/usr/bin/env FOO=1 bash", None),
    ("#!/usr/bin/env -i bash", None), ("#!/bin/fish", None), ("echo hello", None), ("#!", None),
    ("#!/bin/'bash", None),
])
def test_interpreter_is_explicit_never_executes_unknown_flags(shebang, expected):
    assert S.interpreter(shebang + "\necho hi\n")[0] == expected


def test_unsupported_interpreter_does_not_probe_tools(script, monkeypatch):
    script.write_text("#!/usr/bin/python3\nraise RuntimeError('never executed')\n")
    monkeypatch.setattr(S, "_tool", lambda *a, **k: pytest.fail("tool invoked"))
    assert S.check_script(script)["status"] == "unsupported"


def test_missing_tools_are_unavailable_and_not_a_clean_bill(script, monkeypatch):
    monkeypatch.setattr(S.shutil, "which", lambda name: None)
    result = S.check_script(script)
    assert result["status"] == "unavailable"
    assert all(c["status"] == "unavailable" for c in result["checks"])


def test_fifo_device_directory_binary_encoding_oversize_rejected(tmp_path, monkeypatch):
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    start = time.monotonic()
    for path in [fifo, Path("/dev/null"), tmp_path]:
        with pytest.raises((ValueError, OSError)):
            S.check_script(path)
    assert time.monotonic() - start < 1
    path = tmp_path / "script"
    for data, match in [(b"#!/bin/bash\n\0", "NUL"), (b"#!/bin/bash\n\xff", "UTF-8")]:
        path.write_bytes(data)
        with pytest.raises(ValueError, match=match):
            S.check_script(path)
    monkeypatch.setattr(S, "MAX_SCRIPT_BYTES", 10)
    path.write_bytes(b"a" * 11)
    with pytest.raises(ValueError, match="limit"):
        S.check_script(path)


@pytest.mark.parametrize("path", ["bad\x1b[31m.sh", "foo\nbar", "foo\0bar", "", "x" * 4097])
def test_control_paths_rejected_before_open(path):
    with pytest.raises(ValueError, match="path"):
        S.check_script(path)


def test_changed_plan_rejected_before_analyzers(script, monkeypatch):
    expected = hashlib.sha256(script.read_bytes()).hexdigest()
    script.write_text(script.read_text() + "false\n")
    monkeypatch.setattr(S, "_tool", lambda *a, **k: pytest.fail("tool invoked for stale plan"))
    result = S.check_script(script, expected_sha256=expected)
    assert result["status"] == "stale" and not result["matches_plan"]
    assert result["sha256"] != expected


def test_shellcheck_argv_stdin_environment_exact_and_findings_sanitized(script, tmp_path, monkeypatch):
    capture = tmp_path / "capture.json"
    payload = [{"line": 2, "column": 4, "code": 2086, "level": "warning", "message": "Quote this\x1b[31m\nnow"}]
    extra = f"open({str(capture)!r},'w').write(json.dumps({{'argv':sys.argv[1:],'env':dict(os.environ),'data':sys.stdin.read(),'cwd':os.getcwd()}}))"
    analyzer(tmp_path, monkeypatch, json.dumps(payload), 1, extra)
    for key in ("SHELLCHECK_OPTS", "BASH_ENV", "ENV", "LD_PRELOAD", "PYTHONPATH"):
        monkeypatch.setenv(key, "unsafe inherited hook")
    result = S.check_script(script)
    captured = json.loads(capture.read_text())
    assert captured["argv"] == ["--norc", "--format=json", "--shell=bash", "-"]
    assert captured["data"].encode() == script.read_bytes()
    assert set(captured["env"]) <= {"LANG", "LC_ALL", "PATH", "HOME", "XDG_CONFIG_HOME", "LC_CTYPE"}
    assert result["status"] == "findings"
    finding = result["findings"][0]
    assert finding["code"] == "SC2086" and finding["line"] == 2
    assert "\x1b" not in finding["message"] and "\n" not in finding["message"]
    assert finding["excerpt"] == "printf '%s\\n' '$HOME'"
    assert not Path(captured["cwd"]).exists()


@pytest.mark.parametrize("payload,code", [
    ("not JSON", 0), ("{}", 0), ("[]", 1), ("[]", 2),
    (json.dumps([{"line": True, "column": 1, "code": 1, "level": "warning", "message": "m"}]), 1),
    (json.dumps([{"line": 1, "column": 1, "code": 1, "level": "banana", "message": "m"}]), 1),
    (json.dumps([{}] * 513), 1),
])
def test_malformed_analyzer_outputs_never_label_script_checked(script, tmp_path, monkeypatch, payload, code):
    analyzer(tmp_path, monkeypatch, payload, code)
    result = S.check_script(script)
    assert result["status"] == "partial"
    assert result["checks"][-1]["status"] == "unavailable"
    assert not S._CACHE


def test_cache_uses_bytes_tools_and_policy_and_isolated_results(script, tmp_path, monkeypatch):
    count = tmp_path / "count"
    tool = analyzer(tmp_path, monkeypatch, "[]", extra=f"open({str(count)!r},'a').write('x')")
    first = S.check_script(script)
    first["checks"][0]["status"] = "tampered"
    second = S.check_script(script)
    assert second["cached"] and second["checks"][0]["status"] == "passed"
    assert count.read_text() == "x"
    same = tmp_path / "same.sh"
    same.write_bytes(script.read_bytes())
    assert S.check_script(same)["path"] == str(same)
    assert count.read_text() == "x"
    assert not S.check_script(script, refresh=True)["cached"]
    assert count.read_text() == "xx"
    script.write_text(script.read_text() + "true\n")
    assert not S.check_script(script)["cached"]
    tool.write_text(tool.read_text() + "\n# changed executable\n")
    assert not S.check_script(script)["cached"]
    monkeypatch.setattr(S, "POLICY_VERSION", 2)
    assert not S.check_script(script)["cached"]


def test_missing_tool_installation_invalidates_cache(script, tmp_path, monkeypatch):
    real_which = shutil.which
    monkeypatch.setattr(S.shutil, "which", lambda name: None if name == "shellcheck" else real_which(name))
    assert S.check_script(script)["status"] == "partial"
    monkeypatch.setattr(S.shutil, "which", real_which)
    analyzer(tmp_path, monkeypatch)
    result = S.check_script(script)
    assert result["status"] == "checked" and not result["cached"]


def test_process_timeout_and_output_flood_are_bounded():
    start = time.monotonic()
    result = S._run([sys.executable, "-c", "import time;time.sleep(10)"], timeout=0.08)
    assert result["error"] == "analyzer timed out"
    assert time.monotonic() - start < 2
    result = S._run([sys.executable, "-c", "import os\nwhile True: os.write(1,b'x'*65536)"])
    assert "output exceeded" in result["error"]
    assert len(result["stdout"].encode()) + len(result["stderr"].encode()) <= S.MAX_OUTPUT_BYTES


def test_process_cancellation_collects_analyzer_and_is_not_cached(script):
    event = threading.Event()
    timer = threading.Timer(.08, event.set)
    timer.start()
    try:
        result = S._run([sys.executable, "-c", "import time;time.sleep(10)"], cancel=event.is_set)
    finally:
        timer.join()
    assert result["error"] == "cancelled" and result["returncode"] is not None
    assert S.check_script(script, cancel=event.is_set)["status"] == "cancelled"
    assert not S._CACHE


def test_cache_size_stays_bounded(script, monkeypatch):
    monkeypatch.setattr(S.shutil, "which", lambda name: None)
    for number in range(40):
        script.write_text(f"#!/bin/bash\necho {number}\n")
        S.check_script(script)
    assert len(S._CACHE) == 16


@pytest.mark.skipif(not shutil.which("shellcheck"), reason="optional ShellCheck executable is not installed")
def test_real_shellcheck_external_directives_do_not_open_external_scripts(script, tmp_path, monkeypatch):
    external = tmp_path / "library.sh"
    external.write_text("echo $UNQUOTED_SECRET_FROM_EXTERNAL_FILE\n")
    script.write_text(f"#!/bin/bash\n# shellcheck external-sources=true\nsource {external}\n")
    monkeypatch.setenv("SHELLCHECK_OPTS", "--external-sources")
    result = S.check_script(script)
    assert any(item["code"] == "SC1144" for item in result["findings"])
    assert not any("UNQUOTED_SECRET" in item["message"] or item["code"] == "SC2086" for item in result["findings"])
