#!/usr/bin/env python3
"""Repeatable, offline-first development setup. Requires Python 3.10+."""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import venv

ROOT = Path(__file__).resolve().parents[1]
RESEARCH_SMOKE_VIEWS = (
    "experiment", "arrays", "evidence", "artifacts", "passport", "submit",
    "predict", "forecast", "blockers", "tradeoffs", "scaling", "workflow", "checkpoint-advice", "operations",
)


class SetupError(Exception):
    pass


def run(command, *, capture=False, timeout=120):
    try:
        result = subprocess.run(
            [str(part) for part in command], cwd=ROOT, text=True,
            capture_output=capture, timeout=timeout, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SetupError(f"Could not run {shlex.join(map(str, command))}: {exc}") from exc
    if result.returncode:
        detail = (result.stderr or result.stdout or "").strip() if capture else ""
        raise SetupError(f"Command exited {result.returncode}: {shlex.join(map(str, command))}" + (f"\n{detail}" if detail else ""))
    return result.stdout if capture else None


def select_mode(mode, host):
    if mode == "remote" and not host:
        raise SetupError("Remote mode needs --host LOGIN, for example --host me@login.example.edu.")
    if host and mode in ("demo", "local"):
        raise SetupError("--host can only be used with --mode remote or --mode auto.")
    if host and (host.startswith("-") or any(c.isspace() for c in host)):
        raise SetupError("Use an SSH hostname or user@hostname for --host; put SSH options in ~/.ssh/config.")
    if mode != "auto":
        return mode
    return "remote" if host else "local" if shutil.which("squeue") else "demo"


def select_profile(mode, requested):
    """Resolve an explicit example profile without changing existing defaults."""
    profile = requested or ("" if mode == "demo" else "carc")
    if not profile:
        return ""
    profiles = json.loads((ROOT / "docs" / "config.example.json").read_text(encoding="utf-8"))["profiles"]
    if profile not in profiles or not isinstance(profiles[profile], dict):
        raise SetupError(f"Unknown setup profile {profile!r}. Choose one of: {', '.join(sorted(profiles))}.")
    if mode == "local" and profiles[profile].get("host"):
        raise SetupError("Local setup needs a profile without an SSH host. Use --profile desktop or --mode remote --host LOGIN.")
    return profile


def launch_flags(mode, host, profile):
    flags = ["--config", str(ROOT / "docs" / "config.example.json"), "--profile", profile] if profile else []
    if mode == "demo":
        flags.append("--fake")
    elif mode == "remote":
        flags += ["--host", host]
    return flags


def prepare_venv(path):
    path = path.expanduser().resolve()
    if path == ROOT or path in ROOT.parents:
        raise SetupError("Choose a dedicated virtual-environment directory, not the checkout or one of its parents.")
    if path.exists() and (not path.is_dir() or (any(path.iterdir()) and not (path / "pyvenv.cfg").is_file())):
        raise SetupError(f"Refusing to replace a non-venv path: {path}. Choose a new --venv directory.")
    if not (path / "pyvenv.cfg").is_file():
        print(f"Creating isolated Python environment: {path}", flush=True)
        try:
            # Match `python -m venv` on POSIX: a symlink preserves the real
            # executable location used by relocatable Python builds to locate
            # their standard library. Copying such a binary can strand it at
            # its build-time prefix (for example /install on bundled 3.10).
            # No downloads, ensurepip dependency, or system package changes.
            venv.EnvBuilder(with_pip=False, symlinks=os.name == "posix").create(path)
        except (OSError, RuntimeError) as exc:
            raise SetupError(f"Cannot create a venv: {exc}. Install your OS's Python venv package, or run python3 -m raven directly.") from exc
    else:
        print(f"Reusing Python environment: {path}", flush=True)
    python = path / "bin" / "python"
    result = run([python, "-c", "import sys; print(sys.prefix); sys.exit(0 if sys.version_info >= (3, 10) else 'Python 3.10+ required')"], capture=True)
    if Path(result.strip()).resolve() != path:
        raise SetupError(f"{python} is not an isolated interpreter for {path}.")
    return python


def install_dev(python):
    print("Installing optional development tools from the configured pip index...", flush=True)
    probe = subprocess.run([str(python), "-m", "pip", "--version"], capture_output=True, check=False)
    if probe.returncode:
        run([python, "-m", "ensurepip", "--upgrade"])
    run([python, "-m", "pip", "install", "--disable-pip-version-check", "-e", f"{ROOT}[dev]"], timeout=600)


def install_terminal(python):
    print("Installing the optional embedded terminal engine...", flush=True)
    probe = subprocess.run([str(python), "-m", "pip", "--version"], capture_output=True, check=False)
    if probe.returncode:
        run([python, "-m", "ensurepip", "--upgrade"])
    run([python, "-m", "pip", "install", "--disable-pip-version-check", "-e", f"{ROOT}[terminal]"], timeout=600)
    run([python, "-c", "from raven.terminal_emulator import TerminalEngine; e = TerminalEngine(4, 20); e.feed(b'Raven terminal ready'); assert e.snapshot()['lines']"], capture=True)


def publish_report(source, destination):
    """Never replace a pre-existing file, including a user-selected report."""
    destination = destination.expanduser().absolute()
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        with destination.open("x", encoding="utf-8") as target:
            target.write(source.read_text(encoding="utf-8"))
    except FileExistsError:
        print(f"Existing report preserved: {destination}", flush=True)
        return False
    print(f"Demo report: {destination}", flush=True)
    return True


def view_cases():
    """Exact CLI cases shared by the isolated renderer and its parent check."""
    for tab in ("jobs", "cluster", "history", "analytics", "nodes", "group", "deps", "log", "sources", "research"):
        for encoding in ("ascii", "unicode"):
            yield f"{tab}/{encoding}", ["--tab", tab, "--" + encoding, "--width", "120"]
    for view in RESEARCH_SMOKE_VIEWS:
        for encoding in ("ascii", "unicode"):
            yield f"Research/{view}/{encoding}", ["--tab", "research", "--research-view", view, "--" + encoding]


def validate_views(config):
    """Run fresh full CLI sessions in the selected interpreter, sharing imports.

    This is an internal child process, never the setup driver's interpreter.
    Every case still resolves configuration, builds and settles its own demo,
    renders through the launcher, and closes that session in the CLI's finally.
    """
    sys.path.insert(0, str(ROOT))
    try:
        from raven.raven_launch import light_main
        completed = []
        for name, options in view_cases():
            output, errors = io.StringIO(), io.StringIO()
            command = ["--config", str(config), "--no-state", "--no-plugins",
                       "--fake", "--once", "--no-color", *options]
            try:
                with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                    code = light_main(command)
            except SystemExit as exc:
                code = exc.code
            except Exception as exc:
                raise SetupError(f"The {name} view raised {type(exc).__name__}: {exc}") from exc
            if code != 0:
                raise SetupError(f"The {name} view exited {code}: {errors.getvalue().strip()}")
            frame = output.getvalue()
            if not frame.strip() or (name.endswith("/ascii") and not frame.isascii()) or "\x1b" in frame:
                raise SetupError(f"The {name} view did not produce clean {'ASCII' if name.endswith('/ascii') else 'Unicode'} text output.")
            completed.append({"case": name, "exit_code": code, "text_valid": True})
        return {"schema": "raven.setup-views/v1", "cases": completed}
    finally:
        del sys.path[0]


def validate(python, mode, host, report, profile=""):
    with tempfile.TemporaryDirectory(prefix="raven-setup-") as temporary:
        scratch = Path(temporary)
        config = scratch / "config.json"
        config.write_text("{}\n", encoding="utf-8")
        command = [python, ROOT / "raven", "--config", config, "--no-state", "--no-plugins"]
        print(f"Validating simulated jobs, Unicode and ASCII terminal views, all {len(RESEARCH_SMOKE_VIEWS)} Research workspaces, and the ASCII report...", flush=True)
        snapshot = json.loads(run([*command, "--fake", "--json"], capture=True))
        if not isinstance(snapshot, dict) or not snapshot.get("jobs"):
            raise SetupError("The simulated cluster did not produce any jobs.")
        manifest = json.loads(run([python, Path(__file__).resolve(), "--_validate-views", config], capture=True))
        expected = {"schema": "raven.setup-views/v1", "cases": [
            {"case": name, "exit_code": 0, "text_valid": True} for name, _ in view_cases()]}
        if manifest != expected:
            raise SetupError("The terminal view validation did not complete every expected ASCII/Unicode case.")
        preview = scratch / "demo.txt"
        run([*command, "--fake", "--report", preview], capture=True)
        page = preview.read_text(encoding="utf-8")
        if not page.strip() or not page.isascii() or "JOBS" not in page.upper():
            raise SetupError("The report smoke test did not produce a complete ASCII snapshot.")
        publish_report(preview, report)
        print(f"Checking {mode} capabilities...", flush=True)
        doctor = [python, ROOT / "raven", "--no-state", "--no-plugins"]
        if not profile:
            doctor += ["--config", config]
        run([*doctor, "--doctor", *launch_flags(mode, host, profile)], timeout=45)


def parser():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    ap.add_argument("--mode", choices=("auto", "demo", "local", "remote"), default="auto", help="auto chooses remote with --host, local with squeue, otherwise demo")
    ap.add_argument("--host", default="", help="SSH hostname or user@hostname for remote mode; uses existing SSH authentication")
    ap.add_argument("--profile", default="", help="profile from docs/config.example.json; local/remote default to carc, use desktop for your computer")
    ap.add_argument("--venv", type=Path, default=ROOT / ".venv", help="isolated Python environment to create or reuse")
    ap.add_argument("--report", type=Path, default=ROOT / ".raven" / "demo.txt", help="save a demo ASCII report; existing files are preserved")
    ap.add_argument("--dev", action="store_true", help="also install editable package, pytest and build tools (requires pip index access)")
    ap.add_argument("--terminal", action="store_true", help="install and validate the optional embedded terminal engine (requires pip index access)")
    ap.add_argument("--test", action="store_true", help="also run the complete test suite; use --dev to install test dependencies")
    return ap


def main(argv=None):
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        if arguments[:1] == ["--_validate-views"]:
            if len(arguments) != 2:
                raise SetupError("Internal view validation requires one configuration path.")
            print(json.dumps(validate_views(Path(arguments[1])), ensure_ascii=True))
            return 0
        args = parser().parse_args(arguments)
        if sys.version_info < (3, 10):
            raise SetupError("Python 3.10 or newer is required. Select a newer python3 and rerun setup.")
        if os.name != "posix":
            raise SetupError("Use Linux, macOS, or Windows Subsystem for Linux (WSL); the terminal dashboard uses POSIX curses.")
        mode = select_mode(args.mode, args.host)
        profile = select_profile(mode, args.profile)
        python = prepare_venv(args.venv)
        if args.dev:
            install_dev(python)
        if args.terminal:
            install_terminal(python)
        validate(python, mode, args.host, args.report, profile)
        if args.test:
            print("Running tests...", flush=True)
            try:
                run([python, "-m", "pytest", "-q"], timeout=600)
            except SetupError as exc:
                raise SetupError(f"{exc}\nIf pytest is unavailable, rerun setup with --dev --test.") from exc
        command = [str(python), str(ROOT / "raven")]
        flags = launch_flags(mode, args.host, profile)
        command += flags
        print(f"\nReady ({mode}). Launch in a terminal:\n  {shlex.join(command)}")
        if args.venv.expanduser().resolve() == ROOT / ".venv":
            print(f"Or, from this checkout:\n  {shlex.join(['./scripts/raven', *flags])}")
        print("Press ? for help; q to quit. Read docs/runbook.md for cluster configuration and troubleshooting.")
        return 0
    except (SetupError, ValueError, OSError) as exc:
        print(f"\nSetup needs attention: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
