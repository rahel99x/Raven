#!/usr/bin/env python3
"""Bounded offline and opt-in read-only site qualification; never submit a job."""
from __future__ import annotations

import argparse
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import pty
import re
import selectors
import signal
import stat
import struct
import subprocess
import sys
import tempfile
import termios
import time
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
MAX_OUTPUT = 2 << 20
CHECK_TIMEOUT = 20


def run(argv, *, cwd=ROOT, env=None, timeout=CHECK_TIMEOUT):
    """Drain both pipes under one byte/time budget, reaping the owned group."""
    child = subprocess.Popen(list(map(str, argv)), cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    output = bytearray()
    try:
        until = time.monotonic() + timeout
        with selectors.DefaultSelector() as selector:
            for stream in (child.stdout, child.stderr):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ)
            while selector.get_map():
                if time.monotonic() >= until:
                    raise ValueError("Command exceeded qualification deadline")
                for key, _ in selector.select(.05):
                    block = os.read(key.fd, 65536)
                    if not block:
                        selector.unregister(key.fileobj)
                    else:
                        output.extend(block)
                        if len(output) > MAX_OUTPUT:
                            raise ValueError("Command exceeded qualification output budget")
        code = child.wait(timeout=max(.01, until - time.monotonic()))
        if code:
            raise ValueError(f"Command exited {code}: " + output.decode("utf-8", "replace")[-1200:])
        return output.decode("utf-8", "replace")
    finally:
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        if child.poll() is None:
            child.wait(timeout=3)
        child.stdout.close()
        child.stderr.close()


def terminal_check(command, env):
    """Exercise a real curses process in a private PTY, without a scheduler."""
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 42, 140, 0, 0))
    child = subprocess.Popen(command, cwd=ROOT, env=env, stdin=slave, stdout=slave,
                             stderr=slave, start_new_session=True)
    os.close(slave)
    os.set_blocking(master, False)
    total, sent, transcript = 0, False, bytearray()
    until = time.monotonic() + CHECK_TIMEOUT
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(master, selectors.EVENT_READ)
            while child.poll() is None:
                if time.monotonic() >= until:
                    raise ValueError("Interactive terminal did not exit before its deadline")
                for _, _ in selector.select(.05):
                    try:
                        block = os.read(master, 65536)
                    except OSError as exc:
                        if exc.errno == errno.EIO:
                            break
                        raise
                    total += len(block)
                    transcript.extend(block)
                    if total > MAX_OUTPUT:
                        raise ValueError("Interactive terminal exceeded its output budget")
                if not sent and b"Jobs" in transcript:
                    # Passive mouse reports and a complete quit key must not
                    # become accidental shortcuts through fragmented decoding.
                    for fragment in (b"\x1b", b"[<35;100;20M", b"\x1b[<35;40;15M", b"q"):
                        os.write(master, fragment)
                    sent = True
        if child.returncode or not sent or b"Traceback (most recent call last)" in transcript:
            raise ValueError("Interactive smoke check did not render Jobs and exit cleanly")
        return {"terminal": env["TERM"], "rows": 42, "columns": 140, "output_bytes": total,
                "scope": "local pseudo-terminal; no emulator presentation or SSH latency measurement"}
    finally:
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        if child.poll() is None:
            child.wait(timeout=3)
        os.close(master)


def documentation_check(root=ROOT):
    from itertools import islice
    files = [root / "README.md", root / "CONTRIBUTING.md", *islice((root / "docs").rglob("*.md"), 1001)]
    if len(files) > 1002:
        raise ValueError("Documentation exceeds the 1000-document qualification budget")
    deadline = time.monotonic() + 15
    broken, checked = [], 0
    for path in files:
        if time.monotonic() > deadline:
            raise ValueError("Documentation check exceeded its deadline")
        if not path.is_file() or path.stat().st_size > 1 << 20:
            raise ValueError("Missing or oversized documentation file: " + str(path.relative_to(root)))
        for target in re.findall(r"\]\(([^\s)]+)(?:\s+[^)]*)?\)", path.read_text(encoding="utf-8")):
            target = target.strip("<>").split("#", 1)[0]
            if not target or re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", target) or target.startswith("/"):
                continue
            checked += 1
            if not (path.parent / unquote(target)).exists():
                broken.append(str(path.relative_to(root)) + ": " + target)
    if broken:
        raise ValueError("Broken local documentation links: " + "; ".join(broken[:20]))
    return {"documents": len(files), "local_links": checked, "external_urls": "not tested"}


def live_manifest(path):
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as stream:
            metadata = os.fstat(stream.fileno())
            if not stat.S_ISREG(metadata.st_mode):
                raise ValueError("Site manifest must be a regular file")
            raw = stream.read(16385)
    except OSError as exc:
        raise ValueError("Cannot read a regular site manifest: " + str(exc)) from exc
    if len(raw) > 16384:
        raise ValueError("Site manifest exceeds 16 KiB")
    value = json.loads(raw.decode("utf-8"))
    fields = {"schema", "cluster", "job_id", "user", "submit_time", "start_time"}
    if not isinstance(value, dict) or set(value) != fields or value["schema"] != "raven.site-qualification/v1":
        raise ValueError("Expected exact raven.site-qualification/v1 identity fields")
    if not isinstance(value["job_id"], str) or not re.fullmatch(r"[1-9][0-9]{0,18}(?:_[0-9]{1,19})?", value["job_id"]):
        raise ValueError("Use one explicit job or array-task ID")
    for key in ("cluster", "user"):
        if not isinstance(value[key], str) or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.@-]{0,127}", value[key]):
            raise ValueError("Invalid site identity field: " + key)
    for key in ("submit_time", "start_time"):
        if not isinstance(value[key], str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}", value[key]):
            raise ValueError("Use an exact Slurm timestamp for " + key)
    return value


def check_live(path, runner=run):
    value = live_manifest(path)
    raw = runner(["scontrol", "-M", value["cluster"], "--oneliner", "show", "job", value["job_id"]])
    rows = [line for line in raw.splitlines() if line.strip()]
    if len(rows) != 1:
        raise ValueError("Expected exactly one scheduler allocation record")
    fields = dict(re.findall(r"(?:^|\s)([A-Za-z][A-Za-z0-9_]*)=([^\s]+)", rows[0]))
    job_id = (fields.get("ArrayJobId", "") + "_" + fields.get("ArrayTaskId", "")) if "_" in value["job_id"] else fields.get("JobId")
    observed = {"job_id": job_id, "user": fields.get("UserId", "").split("(", 1)[0],
                "submit_time": fields.get("SubmitTime"), "start_time": fields.get("StartTime")}
    if any(observed[key] != value[key] for key in observed):
        raise ValueError("Allocation identity changed or did not match the supplied manifest")
    return {"identity_sha256": hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest(),
            "state": fields.get("JobState", "unknown"),
            "scope": "one read-only allocation identity query; no sampling, steps, submissions or mutations"}


def qualify(*, wheel=None, site_manifest=None):
    checks = []
    def check(name, callback):
        started = time.monotonic()
        try:
            evidence = callback()
            checks.append({"id": name, "status": "qualified", "evidence": evidence,
                           "seconds": round(time.monotonic() - started, 3)})
        except Exception as exc:
            checks.append({"id": name, "status": "failed", "detail": str(exc)[:2400],
                           "seconds": round(time.monotonic() - started, 3)})
    with tempfile.TemporaryDirectory(prefix="raven-qualification-") as temporary:
        scratch = Path(temporary)
        config = scratch / "config.json"
        config.write_text('{"startup_animation":false,"animations":false}\n')
        env = dict(os.environ, TERM="xterm-256color", PYTHONPATH="", XDG_CONFIG_HOME=str(scratch / "config"),
                   XDG_STATE_HOME=str(scratch / "state"), RAVEN_CONFIG=str(config))
        command = [sys.executable, "-m", "raven", "--config", str(config), "--fake", "--no-state", "--no-plugins"]
        check("documentation", documentation_check)
        check("source-cli", lambda: {"version": run([sys.executable, "-m", "raven", "--version"], env=env).strip(),
                                    "doctor": run([*command, "--doctor"], env=env).strip()[-1200:]})
        def pages():
            for tab in ("jobs", "cluster", "history", "analytics", "nodes", "group", "deps", "log", "sources", "research"):
                text = run([*command, "--once", "--ascii", "--no-color", "--tab", tab], env=env)
                if not text.strip() or not text.isascii() or "\x1b" in text:
                    raise ValueError("Expected plain ASCII output from " + tab)
            return {"pages": 10, "data": "simulated", "scheduler_contacted": False}
        check("terminal-pages", pages)
        check("real-pty", lambda: terminal_check(command, env))
        for name in ("trace", "runtime", "report"):
            check("cli-" + name, lambda name=name: {"help_available": bool(run([sys.executable, "-m", "raven", name, "--help"], env=env).strip())})
        def installed():
            if not wheel.is_file() or wheel.suffix != ".whl" or wheel.stat().st_size > 64 << 20:
                raise ValueError("Expected a built wheel smaller than 64 MiB")
            target = scratch / "installed"
            run([sys.executable, "-m", "venv", str(target)], cwd=scratch, env=env, timeout=60)
            python = target / "bin" / "python"
            run([python, "-m", "pip", "install", "--no-index", "--no-deps", str(wheel)], cwd=scratch, env=env, timeout=60)
            text = run([python, "-I", "-m", "raven", "--config", str(config), "--fake", "--no-state", "--no-plugins", "--doctor"], cwd=scratch, env=env)
            return {"wheel_sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(), "outside_checkout": True,
                    "doctor": text.strip()[-1000:]}
        if wheel is not None:
            check("installed-wheel", installed)
        else:
            checks.append({"id": "installed-wheel", "status": "untested", "detail": "Supply --wheel PATH to verify an offline clean installation"})
        if site_manifest is not None:
            check("live-allocation-identity", lambda: check_live(site_manifest))
        else:
            checks.append({"id": "live-allocation-identity", "status": "untested", "detail": "No explicitly selected existing allocation"})
    for name in ("physical-gpu", "multi-node", "terminal-emulator", "production-ssh", "external-user-study", "scientific-outcomes"):
        checks.append({"id": name, "status": "untested", "detail": "Requires a separately recorded target-system or user evaluation"})
    return {"schema": "raven.qualification-report/v1", "scope": "bounded local source, CLI and terminal qualification",
            "production_qualified": False, "status": "failed" if any(item["status"] == "failed" for item in checks) else "qualified-for-recorded-checks",
            "python": platform.python_version(), "system": platform.system(), "checks": checks}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="New JSON report path; existing files are preserved")
    parser.add_argument("--wheel", type=Path, help="Built local wheel to install offline in a temporary environment")
    parser.add_argument("--live-manifest", type=Path, help="Explicit identity of one existing allocation for a read-only query")
    args = parser.parse_args(argv)
    if args.output.exists():
        parser.error("Output already exists; choose a new path")
    report = qualify(wheel=args.wheel.resolve() if args.wheel else None, site_manifest=args.live_manifest)
    try:
        descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(report, stream, ensure_ascii=True, allow_nan=False, indent=2)
            stream.write("\n")
    except OSError as exc:
        parser.exit(2, str(exc) + "\n")
    print(report["status"] + ": " + str(args.output))
    return 1 if report["status"] == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
