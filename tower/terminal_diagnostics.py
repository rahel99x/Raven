"""Bounded local PTY capability checks; never contacts Slurm or a remote host."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import re
import shutil
import stat
import sys
import time

if __package__ in (None, ""):
    # The source launcher is supported even outside the checkout, without pip.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


_CHILD = """import os, termios, fcntl, struct
print('CHECK:tty:' + str(all(os.isatty(fd) for fd in (0,1,2))), flush=True)
print('CHECK:foreground:' + str(os.tcgetpgrp(0) == os.getpgrp()), flush=True)
print('PROBE_READY', flush=True)
line = input()
print('CHECK:input:' + str(line == 'tower-probe'), flush=True)
size = struct.unpack('HHHH', fcntl.ioctl(0, termios.TIOCGWINSZ, b'\\0' * 8))[:2]
print('CHECK:resize:' + str(size == (13,67)), flush=True)
print('\\x1b[32mPROBE_COMPLETE\\x1b[0m', flush=True)
"""

PROFILE_SCHEMA = "tower.terminal-profile/v1"
PROFILE_LIMIT = 65536
PROFILE_TOOLS = ("bash", "zsh", "fish", "vim", "nvim", "less", "htop", "tmux", "ssh")
CHECK_NAMES = frozenset(("alternate-buffer", "tty", "foreground", "input", "resize", "output-and-exit"))
PROFILE_LIMITS = (
    "The six local PTY checks do not certify a physical terminal emulator, SSH, or a live Slurm allocation.",
    "Tool availability means executable discovery on PATH; it does not mean that application compatibility was tested.",
    "TERM identifies the requested terminal capabilities, not the terminal emulator product or version.",
    "The pyte adapter supports character cells; graphics protocols and complex emoji shaping are outside this profile.",
    "Normal-buffer resize clips columns; this profile does not claim lossless text reflow.",
)
FULLSCREEN_GUIDANCE = (
    "For an application beyond embedded compatibility, review an owned running allocation with "
    ":ops allocation-shell and set Terminal display to fullscreen. Exit the shell to return to Tower. "
    "This handoff retains the allocation review and exact-attempt checks; it does not create a new allocation."
)


def runtime_metadata():
    """A narrow allowlist: never capture environment values beyond TERM."""
    try:
        engine_version = importlib.metadata.version("pyte")
    except importlib.metadata.PackageNotFoundError:
        engine_version = "unavailable"
    term = os.environ.get("TERM", "unset")
    if not re.fullmatch(r"[A-Za-z0-9_.+-]{1,128}", term):
        term = "unrecognized"
    return {"python": platform.python_version(), "implementation": platform.python_implementation(),
            "os": platform.system(), "engine": "pyte", "engine_version": engine_version, "term": term}


def make_profile(result):
    checks = [{"name": str(item.get("name", "")), "passed": bool(item.get("passed"))}
              for item in result.get("checks", ()) if item.get("name") in CHECK_NAMES]
    return {"schema": PROFILE_SCHEMA, "created": datetime.now(timezone.utc).isoformat(),
            "runtime": runtime_metadata(), "probe": {"status": result.get("status", "failed"),
                "scope": "local PTY and embedded engine", "scheduler_contacted": False, "checks": checks},
            "tested_features": [item["name"] for item in checks if item["passed"]],
            "tools": {name: {"available": shutil.which(name) is not None, "tested": False} for name in PROFILE_TOOLS},
            "limits": list(PROFILE_LIMITS), "fullscreen_guidance": FULLSCREEN_GUIDANCE}


def _validate_profile(value):
    if not isinstance(value, dict) or value.get("schema") != PROFILE_SCHEMA:
        raise ValueError("Unsupported terminal compatibility profile schema")
    runtime, probe, tools = value.get("runtime"), value.get("probe"), value.get("tools")
    if not isinstance(runtime, dict) or set(runtime) != {"python", "implementation", "os", "engine", "engine_version", "term"}:
        raise ValueError("Invalid terminal profile runtime metadata")
    if not all(isinstance(item, str) and len(item) <= 128 and not any(ord(char) < 32 or ord(char) == 127 for char in item) for item in runtime.values()):
        raise ValueError("Invalid terminal profile runtime metadata")
    if not isinstance(probe, dict) or probe.get("status") not in ("passed", "failed") or probe.get("scheduler_contacted") is not False:
        raise ValueError("Invalid terminal profile probe evidence")
    checks = probe.get("checks")
    if not isinstance(checks, list) or len(checks) > len(CHECK_NAMES):
        raise ValueError("Invalid terminal profile checks")
    if any(not isinstance(item, dict) or item.get("name") not in CHECK_NAMES or not isinstance(item.get("passed"), bool) for item in checks):
        raise ValueError("Invalid terminal profile checks")
    if len({item["name"] for item in checks}) != len(checks):
        raise ValueError("Duplicate terminal profile checks")
    if probe["status"] == "passed" and (len(checks) != len(CHECK_NAMES) or not all(item["passed"] for item in checks)):
        raise ValueError("Passed terminal profile lacks complete passing evidence")
    if value.get("tested_features") != [item["name"] for item in checks if item["passed"]]:
        raise ValueError("Terminal profile feature claims differ from its checks")
    if not isinstance(tools, dict) or set(tools) != set(PROFILE_TOOLS):
        raise ValueError("Invalid terminal profile tool inventory")
    if any(not isinstance(item, dict) or not isinstance(item.get("available"), bool) or item.get("tested") is not False for item in tools.values()):
        raise ValueError("Availability-only tool inventory cannot claim application testing")
    # Imported text is never treated as a command or trusted output instruction.
    value = {**value, "limits": list(PROFILE_LIMITS), "fullscreen_guidance": FULLSCREEN_GUIDANCE}
    return value


def save_profile(path, result):
    value = _validate_profile(make_profile(result))
    data = (json.dumps(value, ensure_ascii=True, allow_nan=False, indent=2) + "\n").encode()
    if len(data) > PROFILE_LIMIT:
        raise ValueError("Terminal profile exceeds 64 KiB")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NONBLOCK | os.O_NOFOLLOW
    fd = os.open(os.fspath(path), flags, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("Terminal profile must be a private regular file owned by this user")
        view = memoryview(data)
        while view:
            count = os.write(fd, view)
            if count <= 0:
                raise OSError("Terminal profile write made no progress")
            view = view[count:]
    finally:
        os.close(fd)
    return value


def read_profile(path):
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK | os.O_NOFOLLOW
    fd = os.open(os.fspath(path), flags)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("Terminal profile must be a private regular file owned by this user")
        if info.st_size > PROFILE_LIMIT:
            raise ValueError("Terminal profile exceeds 64 KiB")
        with os.fdopen(fd, "rb") as stream:
            fd = -1
            data = stream.read(PROFILE_LIMIT + 1)
        if len(data) > PROFILE_LIMIT:
            raise ValueError("Terminal profile exceeds 64 KiB")
        def invalid_constant(_value):
            raise ValueError("Terminal profile contains a non-finite number")
        return _validate_profile(json.loads(data, parse_constant=invalid_constant))
    finally:
        if fd >= 0:
            os.close(fd)


def compare_profile(saved, result):
    current = make_profile(result)
    runtime = [{"field": key, "saved": saved["runtime"][key], "current": current["runtime"][key]}
               for key in current["runtime"] if saved["runtime"][key] != current["runtime"][key]]
    tools = [name for name in PROFILE_TOOLS if saved["tools"][name]["available"] != current["tools"][name]["available"]]
    return {"status": "changed" if runtime or tools else "metadata-matched", "runtime_changes": runtime,
            "tool_availability_changes": tools, "saved_probe_status": saved["probe"]["status"],
            "current_probe_status": result.get("status", "failed"),
            "notice": "Matching metadata is not application, physical-terminal, SSH, or cluster certification."}


def _text(snapshot):
    return "\n".join("".join(part[0] for part in row) for row in snapshot.get("lines", ()))


def run_probe(timeout=5.0):
    from tower.terminal_service import TerminalService
    started = time.monotonic()
    result = {"schema": "tower.terminal-selftest/v1", "scope": "local PTY and embedded engine",
              "scheduler_contacted": False, "checks": [], "status": "failed"}
    service = TerminalService()
    try:
        from tower.terminal_emulator import TerminalEngine
        engine = TerminalEngine(4, 20)
        engine.feed(b"normal\x1b[?1049halt\x1b[?1049l")
        result["checks"].append({"name": "alternate-buffer", "passed": "normal" in _text(engine.snapshot())})
        sid = service.open(argv=[sys.executable, "-u", "-c", _CHILD],
                           rows=12, cols=64, label="Tower capability self-test")
        deadline = started + max(1.0, min(10.0, float(timeout)))
        supplied = False
        snapshot = {}
        while time.monotonic() < deadline:
            service.poll()
            snapshot = service.snapshot(sid)
            content = _text(snapshot)
            if not supplied and "PROBE_READY" in content:
                if not service.resize(sid, 13, 67) or not service.send(sid, {"type": "text", "text": "tower-probe\n"}):
                    raise RuntimeError(service.error or "Probe input was rejected")
                supplied = True
            if snapshot.get("status") == "error":
                raise RuntimeError(snapshot.get("error") or "Terminal startup failed")
            if snapshot.get("status") == "exited":
                break
            time.sleep(.005)
        content = _text(snapshot)
        for name in ("tty", "foreground", "input", "resize"):
            result["checks"].append({"name": name, "passed": "CHECK:" + name + ":True" in content})
        result["checks"].append({"name": "output-and-exit", "passed":
                                "PROBE_COMPLETE" in content and snapshot.get("exit_code") == 0})
        result["status"] = "passed" if all(item["passed"] for item in result["checks"]) else "failed"
        result["engine"] = "pyte"
    except (ImportError, OSError, ValueError, RuntimeError) as exc:
        result["error"] = str(exc)[:1000]
    finally:
        service.shutdown()
    result["seconds"] = round(time.monotonic() - started, 4)
    result["limits"] = "This test does not certify a physical terminal emulator, SSH, or a live allocation."
    return result


def start(app):
    """Run the finite diagnostic in its own visible session, keeping UI free."""
    from tower.terminal_ui import open_session
    return open_session(app, argv=[sys.executable, str(Path(__file__).resolve()), "--json"],
                        label="Terminal self-test", kind="diagnostic")


def cli(argv=None):
    parser = argparse.ArgumentParser(prog="tower terminal selftest", description=__doc__)
    parser.add_argument("--json", action="store_true", help="print a structured capability report")
    parser.add_argument("--save-profile", metavar="PATH", help="save a new private compatibility profile; never overwrite an existing file")
    parser.add_argument("--profile", metavar="PATH", help="validate a saved profile and compare it with this runtime and a fresh local probe")
    args = parser.parse_args(argv)
    try:
        saved = read_profile(args.profile) if args.profile else None
        result = run_probe()
        if saved:
            result["profile_comparison"] = compare_profile(saved, result)
        if args.save_profile:
            save_profile(args.save_profile, result)
            result["profile_saved"] = os.fspath(args.save_profile)
    except (OSError, ValueError, RuntimeError) as exc:
        result = {"schema": "tower.terminal-selftest/v1", "status": "error", "checks": [],
                  "error": str(exc), "limits": PROFILE_LIMITS[0]}
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print("Tower embedded terminal: " + result["status"])
        for item in result["checks"]:
            print(("PASS " if item["passed"] else "FAIL ") + item["name"])
        if result.get("error"):
            print(result["error"])
        print(result["limits"])
        comparison = result.get("profile_comparison")
        if comparison:
            print("Profile: " + comparison["status"])
            for change in comparison["runtime_changes"]:
                print(f"  {change['field']}: {change['saved']} -> {change['current']}")
            if comparison["tool_availability_changes"]:
                print("Tool availability changed: " + ", ".join(comparison["tool_availability_changes"]))
            print(comparison["notice"])
        if result.get("profile_saved"):
            print("Profile saved: " + result["profile_saved"])
        print(FULLSCREEN_GUIDANCE)
    return 0 if result["status"] == "passed" else (2 if result["status"] == "error" else 1)


if __name__ == "__main__":
    raise SystemExit(cli())
