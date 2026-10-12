"""Optional, bounded shell analysis. Script bytes are inspected, never executed.

Run only from a worker. Results describe the captured SHA256, not future file
contents. Source files are deliberately not followed, and neither user rc files
nor environment startup hooks are inherited.
"""
from __future__ import annotations

from collections import OrderedDict
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import re
import selectors
import shlex
import shutil
import signal
import stat
import subprocess
import tempfile
import threading
import time

MAX_SCRIPT_BYTES = 8 << 20
MAX_OUTPUT_BYTES = 256 << 10
MAX_FINDINGS = 512
TIMEOUT = 5.0
POLICY_VERSION = 1
_CACHE = OrderedDict()
_TOOLS = OrderedDict()
_LOCK = threading.Lock()


def clean(value, limit=4096):
    return "".join(c if c.isprintable() else " " for c in str(value)[:limit])


def _cancelled(cancel):
    return bool(cancel and cancel())


def _capture(path):
    value = os.fspath(path)
    if not isinstance(value, str) or not value or len(value) > 4096 or any(not c.isprintable() for c in value):
        raise ValueError("script path must be a bounded path without control characters")
    path = Path(value).expanduser().absolute()
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(fd, "rb") as handle:
        before = os.fstat(handle.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("shell checks require a regular file; devices and FIFOs are not supported")
        data = handle.read(MAX_SCRIPT_BYTES + 1)
        after = os.fstat(handle.fileno())
    if len(data) > MAX_SCRIPT_BYTES:
        raise ValueError("shell script exceeds the 8 MiB analysis limit")
    if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
        raise ValueError("script changed while being read; rerun the check")
    if b"\0" in data:
        raise ValueError("shell script contains NUL bytes")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("shell script must use UTF-8 text") from exc
    return str(path), data, text


def interpreter(text):
    """Recognize only explicit supported shebangs; never execute shebang options."""
    first = text.split("\n", 1)[0]
    if not first.startswith("#!"):
        return None, "No explicit shebang. Add #!/bin/bash, #!/bin/sh, or #!/bin/dash."
    try:
        words = shlex.split(first[2:].strip())
    except ValueError:
        return None, "Malformed shebang."
    if not words:
        return None, "Empty shebang."
    name = os.path.basename(words[0])
    rest = words[1:]
    if name == "env":
        if rest and rest[0] == "-S":
            rest = rest[1:]
        if not rest or rest[0].startswith("-") or "=" in rest[0]:
            return None, "Unsupported env shebang options or environment assignments."
        name, rest = os.path.basename(rest[0]), rest[1:]
    if name not in {"bash", "sh", "dash"}:
        return None, "Unsupported interpreter: " + clean(name) + "; only bash, sh, and dash are checked."
    if rest:
        return None, "Shebang interpreter flags need manual review; they are not executed or silently ignored."
    return name, ""


def _stop(proc):
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, AttributeError):
        try:
            proc.kill()
        except OSError:
            pass
    proc.wait(timeout=1)


def _run(argv, data=b"", *, cancel=None, timeout=TIMEOUT):
    """Drain bounded output concurrently; no reader threads or unbounded buffers."""
    if _cancelled(cancel):
        return {"error": "cancelled", "returncode": None, "stdout": "", "stderr": ""}
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    error = ""
    proc = None
    with tempfile.TemporaryDirectory(prefix="raven-shell-check-") as directory, tempfile.TemporaryFile() as source:
        source.write(data)
        source.seek(0)
        # An allowlist removes BASH_ENV, ENV, SHELLOPTS, BASHOPTS, exported
        # functions, SHELLCHECK_OPTS, loader hooks, and user configuration paths.
        env = {"PATH": os.defpath, "HOME": directory, "XDG_CONFIG_HOME": directory,
               "LC_ALL": "C", "LANG": "C"}
        try:
            proc = subprocess.Popen(argv, stdin=source, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, cwd=directory, env=env,
                                    start_new_session=True)
            deadline = time.monotonic() + timeout
            with selectors.DefaultSelector() as selector:
                for name in buffers:
                    stream = getattr(proc, name)
                    os.set_blocking(stream.fileno(), False)
                    selector.register(stream, selectors.EVENT_READ, name)
                while selector.get_map():
                    if _cancelled(cancel):
                        error = "cancelled"
                        break
                    if time.monotonic() >= deadline:
                        error = "analyzer timed out"
                        break
                    for key, _ in selector.select(min(0.05, max(0, deadline - time.monotonic()))):
                        chunk = os.read(key.fileobj.fileno(), 16384)
                        if not chunk:
                            selector.unregister(key.fileobj)
                            continue
                        remaining = MAX_OUTPUT_BYTES - sum(map(len, buffers.values()))
                        buffers[key.data].extend(chunk[:max(0, remaining)])
                        if len(chunk) > remaining:
                            error = "analyzer output exceeded 256 KiB"
                            break
                    if error:
                        break
            if error:
                _stop(proc)
            else:
                try:
                    proc.wait(timeout=max(0.001, deadline - time.monotonic()))
                except subprocess.TimeoutExpired:
                    error = "analyzer timed out"
                    _stop(proc)
        except OSError as exc:
            error = clean(exc)
        finally:
            if proc is not None:
                if proc.poll() is None:
                    _stop(proc)
                for name in buffers:
                    getattr(proc, name).close()
    return {"returncode": proc.returncode if proc else None, "error": error,
            **{name: value.decode("utf-8", "replace") for name, value in buffers.items()}}


def _tool(name, cancel=None):
    path = shutil.which(name)
    if not path:
        return {"name": name, "path": "", "version": "not installed", "identity": (name, "missing")}
    path = os.path.abspath(path)
    info = os.stat(path)
    identity = (path, info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
    with _LOCK:
        found = _TOOLS.get(identity)
        if found:
            return dict(found)
    version = _run([path, "--version"], cancel=cancel, timeout=2.0)
    label = clean(version["stdout"], 300).strip() if version["returncode"] == 0 else "version not reported"
    value = {"name": name, "path": path, "version": label or "version not reported", "identity": identity + (label,)}
    if version["error"] == "cancelled":
        return value
    with _LOCK:
        _TOOLS[identity] = dict(value)
        while len(_TOOLS) > 32:
            _TOOLS.popitem(last=False)
    return value


def _finding(tool, level, message, line=0, column=0, code=""):
    return {"tool": tool, "level": level, "message": clean(message),
            "line": line, "column": column, "code": clean(code, 32)}


def _analyze(data, text, shell, tools, cancel):
    findings, checks = [], []
    syntax, checker = tools
    if syntax["path"]:
        argv = [syntax["path"]] + (["--noprofile", "--norc"] if shell == "bash" else []) + ["-n"]
        run = _run(argv, data, cancel=cancel)
        status = "unavailable" if run["error"] else "passed" if run["returncode"] == 0 else "failed"
        checks.append({"tool": shell + " syntax", "status": status, "detail": run["error"]})
        if run["error"]:
            findings.append(_finding("syntax", "warning", run["error"]))
        elif run["returncode"] != 0:
            output = run["stderr"] or run["stdout"] or "Syntax checker exited with an error."
            for item in output.splitlines()[:64]:
                location = re.search(r"(?:line\s+|:\s*)(\d+):", item)
                findings.append(_finding("syntax", "error", item, int(location[1]) if location else 0))
    else:
        checks.append({"tool": shell + " syntax", "status": "unavailable", "detail": "interpreter not installed"})
    if _cancelled(cancel):
        return {"status": "cancelled", "findings": findings, "checks": checks}
    if checker["path"]:
        # ShellCheck forbids enabling external sources from inline directives
        # (SC1144). --norc plus an empty environment excludes the other opt-ins.
        # Pass exact bytes so quoting, heredocs and source positions are intact.
        run = _run([checker["path"], "--norc", "--format=json", "--shell=" + ("sh" if shell == "dash" else shell), "-"],
                   data, cancel=cancel)
        error = run["error"]
        parsed = None
        if not error:
            try:
                parsed = json.loads(run["stdout"])
                if not isinstance(parsed, list) or len(parsed) > MAX_FINDINGS:
                    raise ValueError("expected at most 512 findings")
                for item in parsed:
                    if not isinstance(item, dict) or any(type(item.get(key)) is not int or not 0 <= item[key] <= 100000000
                                                        for key in ("line", "column", "code")):
                        raise ValueError("invalid finding location or rule")
                    if item.get("level") not in {"error", "warning", "info", "style"} or not isinstance(item.get("message"), str):
                        raise ValueError("invalid finding severity or message")
                if run["returncode"] not in {0, 1} or run["returncode"] == 1 and not parsed:
                    raise ValueError("unexpected exit status " + str(run["returncode"]))
            except (ValueError, TypeError) as exc:
                error = "ShellCheck result unavailable: " + clean(exc) + " " + clean(run["stderr"], 512)
        checks.append({"tool": "ShellCheck", "status": "unavailable" if error else "findings" if parsed else "passed", "detail": error})
        if error:
            findings.append(_finding("ShellCheck", "warning", error))
        else:
            findings += [_finding("ShellCheck", item["level"], item["message"], item["line"], item["column"], "SC" + str(item["code"])) for item in parsed]
    else:
        checks.append({"tool": "ShellCheck", "status": "unavailable", "detail": "optional shellcheck executable not installed"})
    status = ("cancelled" if _cancelled(cancel) else "issues" if any(f["level"] == "error" for f in findings)
              else "unavailable" if checks and all(c["status"] == "unavailable" for c in checks)
              else "partial" if any(c["status"] == "unavailable" for c in checks)
              else "findings" if findings else "checked")
    return {"status": status, "findings": findings[:MAX_FINDINGS], "checks": checks}


def check_script(path, *, expected_sha256=None, cancel=None, refresh=False):
    """Check one captured local file; a plan mismatch is never reported clean."""
    if _cancelled(cancel):
        return {"status": "cancelled", "findings": [], "checks": [], "path": clean(path), "sha256": ""}
    path, data, text = _capture(path)
    digest = hashlib.sha256(data).hexdigest()
    result = {"path": path, "sha256": digest, "expected_sha256": expected_sha256,
              "matches_plan": expected_sha256 is None or digest == expected_sha256,
              "bytes": len(data), "cached": False, "findings": [], "checks": [],
              "note": "Snapshot only. ShellCheck inline suppressions apply; source files and rc files are not read. Checks do not prove runtime correctness."}
    shell, reason = interpreter(text)
    result["interpreter"] = shell
    if not result["matches_plan"]:
        return dict(result, status="stale", findings=[_finding("snapshot", "error", "Script differs from the prepared plan. Reprepare the plan before checking it.")])
    if shell is None:
        return dict(result, status="unsupported", findings=[_finding("interpreter", "warning", reason)])
    tools = [_tool(shell, cancel), _tool("shellcheck", cancel)]
    result["tools"] = tools
    key = (digest, shell, tuple(t["identity"] for t in tools), POLICY_VERSION)
    with _LOCK:
        entry = None if refresh else _CACHE.get(key)
        if entry:
            _CACHE.move_to_end(key)
            return dict(result, **copy.deepcopy(entry), cached=True)
    analyzed = _analyze(data, text, shell, tools, cancel)
    wanted = {item["line"] for item in analyzed["findings"] if item["line"] > 0}
    excerpts = {number: clean(line.rstrip("\r\n"), 512) for number, line in enumerate(io.StringIO(text), 1) if number in wanted}
    for item in analyzed["findings"]:
        item["excerpt"] = excerpts.get(item["line"], "")
    if analyzed["status"] != "cancelled" and not any(c["status"] == "unavailable" and c["detail"] != "optional shellcheck executable not installed" and c["detail"] != "interpreter not installed" for c in analyzed["checks"]):
        with _LOCK:
            _CACHE[key] = copy.deepcopy(analyzed)
            while len(_CACHE) > 16:
                _CACHE.popitem(last=False)
    return dict(result, **analyzed)
