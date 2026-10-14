#!/usr/bin/env python3
"""Install Raven in a user-owned venv without pipx, sudo, or shell edits.

Each installation is built and checked before a single current-release link is
switched. Existing executables belonging to other installations are preserved.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import venv

ROOT = Path(__file__).resolve().parents[1]
COMMANDS = ("raven", "ravenl", "ravenh")
MARKER = ".raven-install.json"


class InstallError(RuntimeError):
    pass


def run(command, *, timeout=600):
    try:
        result = subprocess.run([str(item) for item in command], cwd=ROOT, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise InstallError("Could not run " + shlex.join(map(str, command)) + ": " + str(exc)) from exc
    if result.returncode:
        raise InstallError("Command failed: " + shlex.join(map(str, command)))


def _directory(path, *, create=False):
    """Do not place persistent launchers through redirected directories."""
    for directory in (*reversed(path.parents), path):
        try:
            info = directory.lstat()
        except FileNotFoundError:
            if not create:
                continue
            directory.mkdir(mode=0o700)
            info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode):
            raise InstallError("Choose a real directory, not a file or symbolic link: " + str(directory))
    if path.exists() and path.stat().st_uid != os.getuid():
        raise InstallError("Installation directories must belong to your user: " + str(path))


def _owned_link(path, target):
    if not os.path.lexists(path):
        return False
    if (not path.is_symlink() or path.lstat().st_uid != os.getuid() or
            os.path.abspath(path.parent / os.readlink(path)) != str(target)):
        raise InstallError("Existing command preserved: " + str(path) +
                           ". Update it with its original installer (for example pipx install --force '.[terminal]'), "
                           "or choose a different --bin-dir.")
    return True


def inspect_destination(prefix, bin_dir):
    _directory(prefix)
    _directory(bin_dir)
    current = prefix / "current"
    previous = None
    if os.path.lexists(current):
        if not current.is_symlink() or current.lstat().st_uid != os.getuid():
            raise InstallError("Existing installation path preserved: " + str(current))
        previous = current.resolve()
        if previous.parent != prefix or not previous.name.startswith("release-"):
            raise InstallError("The current installation is not owned by this installer: " + str(current))
        marker = previous / MARKER
        if (not marker.is_file() or marker.is_symlink() or marker.stat().st_uid != os.getuid() or
                json.loads(marker.read_text()).get("schema") != "raven.user-install/v1"):
            raise InstallError("The previous release has no valid Raven installer marker.")
    for name in COMMANDS:
        _owned_link(bin_dir / name, prefix / "current" / "bin" / name)
    return previous


def install(prefix, bin_dir, *, terminal=True):
    inspect_destination(prefix, bin_dir)
    _directory(prefix, create=True)
    _directory(bin_dir, create=True)
    lock = os.open(prefix / ".install.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    stage = None
    created = []
    published = False
    temporary_link = None
    try:
        info = os.fstat(lock)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise InstallError("The installer lock must be a private file owned by your user.")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise InstallError("Another Raven installation is in progress.") from exc
        previous = inspect_destination(prefix, bin_dir)
        stage = Path(tempfile.mkdtemp(prefix="release-", dir=prefix))
        print("Creating isolated Raven environment: " + str(stage), flush=True)
        try:
            venv.EnvBuilder(with_pip=True, symlinks=True).create(stage)
        except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
            raise InstallError("Python could not create an environment with pip. Use a Python 3.10+ installation with venv/ensurepip, "
                               "or use python3 scripts/setup.py --mode demo for the offline checkout demo. " + str(exc)) from exc
        python = stage / "bin/python"
        package = str(ROOT) + ("[terminal]" if terminal else "")
        run([python, "-m", "pip", "install", "--disable-pip-version-check", package])
        run([python, "-c", "import curses, raven; print('Raven ' + raven.__version__)"])
        for name in COMMANDS:
            run([stage / "bin" / name, "--version"], timeout=30)
        (stage / MARKER).write_text(json.dumps({"schema": "raven.user-install/v1", "source": str(ROOT)}) + "\n")
        # Every public command points through one atomically switched release.
        # New links use exclusive creation; a concurrent external installer is
        # never overwritten, even after the initial destination check.
        for name in COMMANDS:
            path, target = bin_dir / name, prefix / "current" / "bin" / name
            if not _owned_link(path, target):
                path.symlink_to(target)
                created.append(path)
        fd, temporary = tempfile.mkstemp(prefix=".current-", dir=prefix)
        os.close(fd)
        temporary_link = Path(temporary)
        temporary_link.unlink()
        temporary_link.symlink_to(stage.name)
        # The private installer lock serializes this installer's writers; check
        # the old destination again before publishing the fully built release.
        if inspect_destination(prefix, bin_dir) != previous:
            raise InstallError("The active installation changed while building. Retry after the other update finishes.")
        os.replace(temporary_link, prefix / "current")
        temporary_link = None
        published = True
        return {"release": stage, "previous": previous, "command": bin_dir / "raven"}
    finally:
        if temporary_link is not None:
            temporary_link.unlink(missing_ok=True)
        if not published:
            for path in created:
                target = prefix / "current" / "bin" / path.name
                if path.is_symlink() and os.path.abspath(path.parent / os.readlink(path)) == str(target):
                    path.unlink()
            if stage is not None:
                shutil.rmtree(stage)
        os.close(lock)


def parser():
    home = Path.home()
    data = Path(os.environ.get("XDG_DATA_HOME") or home / ".local/share")
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--prefix", type=Path, default=data / "raven/installations", help="Dedicated user-owned release directory")
    ap.add_argument("--bin-dir", type=Path, default=home / ".local/bin", help="Public launcher directory; existing unrelated commands are preserved")
    ap.add_argument("--without-terminal", action="store_true", help="Skip optional embedded-terminal dependencies")
    ap.add_argument("--no-setup", action="store_true", help="Do not enter guided setup after installing")
    ap.add_argument("--plan", action="store_true", help="Check destinations and show the installation plan without writing")
    return ap


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if sys.version_info < (3, 10) or os.name != "posix":
            raise InstallError("Use Python 3.10 or later on Linux, macOS, or WSL.")
        prefix, bin_dir = args.prefix.expanduser().absolute(), args.bin_dir.expanduser().absolute()
        if prefix == ROOT or prefix in ROOT.parents or bin_dir == prefix or prefix in bin_dir.parents:
            raise InstallError("Use dedicated release and launcher directories outside the checkout's parents.")
        previous = inspect_destination(prefix, bin_dir)
        print("Raven releases: " + str(prefix) + "\nCommands: " + str(bin_dir))
        print("Your configuration, shell startup files, and existing releases are preserved.")
        if args.plan:
            print("Ready to install" + ("; previous release: " + str(previous) if previous else ""))
            return 0
        result = install(prefix, bin_dir, terminal=not args.without_terminal)
        print("\nInstalled. Start setup with:\n  " + shlex.join([str(result["command"]), "setup"]))
        print("Worker Python on this host: " + str(prefix / "current/bin/python"))
        path_entries = {os.path.abspath(os.path.expanduser(part)) for part in os.environ.get("PATH", "").split(os.pathsep) if part}
        if str(bin_dir) not in path_entries:
            print("\nThe launcher directory is not on PATH. Use the absolute command above, or enable it in this shell:\n  " +
                  "export PATH=" + shlex.quote(str(bin_dir)) + ':"$PATH"')
        if result["previous"]:
            print("Previous release retained: " + str(result["previous"]))
        if not args.no_setup and sys.stdin.isatty() and sys.stdout.isatty():
            return subprocess.call([str(result["command"]), "setup"])
        return 0
    except (OSError, ValueError, InstallError) as exc:
        print("Raven installation: " + str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
