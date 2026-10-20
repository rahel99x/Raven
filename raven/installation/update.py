"""Explicit source updates through the installation's existing owner.

This module never polls for releases or loads scheduler/user configuration.
Source updates and installation publication are separate steps: if a build
fails, the managed installer's previous release remains active.
"""

from __future__ import annotations

import argparse
import fcntl
import importlib.util
import json
import os
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path

from ..config_trust import directory_fd
from ..config_trust import read as trusted_read

MARKER = ".raven-install.json"
COMMANDS = ("raven", "ravenl", "ravenh")


class UpdateError(ValueError):
    """The requested update cannot preserve installation ownership."""


class MaintenanceInterrupted(BaseException):
    """Avoid Popen.wait's KeyboardInterrupt-specific early reap behavior."""


def _json(path):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise UpdateError("Duplicate field in installation metadata.")
            result[key] = value
        return result

    raw, _ = trusted_read(path, limit=65536, user_only=True)
    try:
        value = json.loads(raw, object_pairs_hook=pairs)
    except (ValueError, RecursionError) as exc:
        raise UpdateError("Invalid installation metadata: " + str(path)) from exc
    if not isinstance(value, dict):
        raise UpdateError("Expected an installation metadata object: " + str(path))
    return value


def _path(value, label):
    if (
        not isinstance(value, str)
        or not value
        or "\x00" in value
        or not Path(value).is_absolute()
    ):
        raise UpdateError(label + " must be an absolute path.")
    return Path(os.path.abspath(value))


def _owned_directory(path):
    with directory_fd(path) as fd:
        if os.fstat(fd).st_uid != os.getuid():
            raise UpdateError("Directory belongs to another user: " + str(path))


def _launcher_directory(prefix, *, managed):
    executable = Path(sys.argv[0]).expanduser()
    if not executable.is_absolute():
        executable = Path(shutil.which(str(executable)) or executable.absolute())
    directory = executable.parent
    expected = prefix / "current" / "bin" if managed else prefix / "bin"
    _check_commands(directory, expected)
    return directory


def _check_commands(directory, expected):
    _owned_directory(directory)
    for name in COMMANDS:
        path = directory / name
        try:
            info = path.lstat()
        except FileNotFoundError as exc:
            raise UpdateError("Missing public launcher: " + str(path)) from exc
        if (
            not stat.S_ISLNK(info.st_mode)
            or info.st_uid != os.getuid()
            or Path(os.path.abspath(path.parent / os.readlink(path))) != expected / name
        ):
            raise UpdateError(
                "Command ownership changed; use its original installer: " + str(path)
            )


def _terminal_installed():
    installed = []
    for name in ("pyte", "wcwidth"):
        try:
            metadata.distribution(name)
            installed.append(True)
        except metadata.PackageNotFoundError:
            installed.append(False)
    if installed[0] != installed[1]:
        raise UpdateError(
            "Legacy installation has ambiguous terminal dependencies. Rerun its original installer once with your chosen options."
        )
    return all(installed)


@dataclass(frozen=True)
class Installation:
    kind: str
    source: Path
    prefix: Path
    bin_dir: Path
    release: Path
    terminal: bool
    installed_commit: str = ""
    package_spec: str = ""
    pipx: str = ""
    source_dirty: bool | None = None

    def record(self):
        return {
            "kind": self.kind,
            "source": str(self.source),
            "prefix": str(self.prefix),
            "bin_dir": str(self.bin_dir),
            "release": str(self.release),
            "terminal": self.terminal,
            "installed_commit": self.installed_commit,
            "package_spec": self.package_spec,
            "source_dirty": self.source_dirty,
        }

    @property
    def control_directory(self):
        # A pipx reinstall replaces its venv; locks/backups must survive outside it.
        return (
            self.prefix
            if self.kind == "managed"
            else self.prefix.parent.parent / "raven-updates"
        )


def discover(source_override=None):
    release = Path(sys.prefix).absolute()
    # The managed public launcher has a shebang naming its immutable release.
    # A manually invoked current/bin/python may instead name the stable link.
    if release.name == "current" and release.is_symlink():
        release = release.resolve()
    _owned_directory(release)
    marker = release / MARKER
    if os.path.lexists(marker):
        saved = _json(marker)
        if saved.get("schema") != "raven.user-install/v1":
            raise UpdateError("Unsupported Raven installation marker.")
        prefix = release.parent
        if not release.name.startswith("release-"):
            raise UpdateError("Managed release is outside its installation directory.")
        _owned_directory(prefix)
        current = prefix / "current"
        if (
            not current.is_symlink()
            or current.lstat().st_uid != os.getuid()
            or current.resolve() != release
        ):
            raise UpdateError(
                "This is an inactive Raven release. Run the current public raven command."
            )
        if "prefix" in saved and _path(saved["prefix"], "Saved prefix") != prefix:
            raise UpdateError("Installation prefix does not match its marker.")
        bin_dir = (
            _path(saved["bin_dir"], "Saved command directory")
            if "bin_dir" in saved
            else _launcher_directory(prefix, managed=True)
        )
        _check_commands(bin_dir, prefix / "current" / "bin")
        terminal = (
            saved.get("terminal") if "terminal" in saved else _terminal_installed()
        )
        if not isinstance(terminal, bool):
            raise UpdateError("Invalid saved terminal option.")
        source = source_override or _path(saved.get("source"), "Saved source checkout")
        commit = saved.get("installed_commit") or ""
        if not isinstance(commit, str):
            raise UpdateError("Invalid installed source revision.")
        dirty = saved.get("source_dirty")
        if dirty is not None and type(dirty) is not bool:
            raise UpdateError("Invalid installed source-change provenance.")
        return Installation(
            "managed",
            Path(source).expanduser().resolve(),
            prefix,
            bin_dir,
            release,
            terminal,
            commit,
            source_dirty=dirty,
        )
    pipx_marker = release / "pipx_metadata.json"
    if os.path.lexists(pipx_marker):
        saved = _json(pipx_marker)
        package = saved.get("main_package", {})
        if (
            not isinstance(package, dict)
            or package.get("package") != "raven"
            or release.parent.name != "venvs"
            or release.name != "raven"
        ):
            raise UpdateError(
                "This pipx environment does not identify the Raven application."
            )
        spec = package.get("package_or_url")
        if not isinstance(spec, str) or not spec:
            raise UpdateError("Pipx did not retain a local Raven source directory.")
        # Only a concrete local checkout is eligible. Never reinterpret a PyPI
        # package name or a VCS URL as the maintained Raven repository.
        source_text = spec.split("[", 1)[0]
        source = _path(source_text, "Pipx source checkout")
        if (
            source_override is not None
            and Path(source_override).expanduser().absolute() != source
        ):
            raise UpdateError(
                "Pipx owns the original source path. Move it back or explicitly reinstall with pipx from the new checkout first."
            )
        pipx = shutil.which("pipx")
        if not pipx:
            raise UpdateError(
                "This installation belongs to pipx. Make pipx available on PATH before updating."
            )
        bin_dir = _launcher_directory(release, managed=False)
        receipt = release / ".raven-update.json"
        prior = _json(receipt) if receipt.exists() else {}
        commit = prior.get("installed_commit", "")
        return Installation(
            "pipx",
            source,
            release,
            bin_dir,
            release,
            "terminal" in spec.partition("[")[2].rstrip("]").split(","),
            commit if isinstance(commit, str) else "",
            spec,
            pipx,
            prior.get("source_dirty")
            if type(prior.get("source_dirty")) is bool
            else None,
        )
    raise UpdateError(
        "This installation is not managed by Raven or a local-checkout pipx install. "
        "Update it with its original installer. For a new user installation, run bash install.sh from your Raven checkout; "
        "existing command owners are preserved."
    )


def protected_paths(extra=()):
    home = Path.home()
    result = [
        Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config") / "raven",
        Path(os.environ.get("XDG_STATE_HOME") or home / ".local/state") / "raven",
        Path(os.environ.get("XDG_CACHE_HOME") or home / ".cache") / "raven",
    ]
    for key in ("RAVEN_CONFIG", "RAVEN_STATE_DIR"):
        if os.environ.get(key):
            result.append(Path(os.environ[key]))
    result.extend(Path(value) for value in extra)
    return tuple(path.expanduser().absolute() for path in result)


def _validate_layout(installation):
    source = Path(os.path.abspath(installation.source))
    for label, path in (
        ("installation directory", installation.prefix),
        ("command directory", installation.bin_dir),
    ):
        if Path(os.path.abspath(path)).is_relative_to(source):
            raise UpdateError(
                "The " + label + " is inside the source checkout. "
                "Automatic update/revert cannot preserve that layout. "
                "Use the original installer to select destinations outside the checkout first."
            )


@contextmanager
def _lock(directory, filename):
    with directory_fd(directory, create=True) as parent:
        if os.fstat(parent).st_uid != os.getuid():
            raise UpdateError("Update metadata must belong to your user.")
        fd = os.open(
            filename,
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=parent,
        )
        try:
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_mode & 0o077
            ):
                raise UpdateError("Update lock must be a private regular file.")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise UpdateError(
                    "Another Raven install/update operation is in progress."
                ) from exc
            current = os.stat(filename, dir_fd=parent, follow_symlinks=False)
            if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
                raise UpdateError(
                    "Update lock was replaced; retry after the other operation finishes."
                )
            yield
        finally:
            os.close(fd)


def _run(command, *, cwd, env=None, timeout=900):
    """Run only the owned installer process group, with bounded cancellation."""
    process = subprocess.Popen(
        [str(x) for x in command], cwd=cwd, env=env, start_new_session=True
    )
    try:
        code = process.wait(timeout=timeout)
    except BaseException:
        if process.returncode is None:
            # Give the managed installer a chance to reap its own build group.
            # WNOWAIT retains our leader PID until group cleanup is complete.
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            if hasattr(os, "waitid") and hasattr(os, "WNOWAIT"):
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    if os.waitid(
                        os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT
                    ):
                        break
                    time.sleep(0.02)
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
        raise
    if code:
        raise UpdateError("The owning installer failed (exit " + str(code) + ").")


def _pipx_env(installation):
    env = dict(os.environ)
    env["PIPX_HOME"] = str(installation.prefix.parent.parent)
    env["PIPX_BIN_DIR"] = str(installation.bin_dir)
    return env


@contextmanager
def _pipx_artifact(installation, source, commit):
    """Build a clean wheel without changing pipx's original source ownership."""
    script = source / "scripts/install.py"
    trusted_read(script, limit=1 << 20, user_only=True)
    spec = importlib.util.spec_from_file_location("_raven_update_installer", script)
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    with tempfile.TemporaryDirectory(
        prefix="raven-pipx-build-", dir=installation.control_directory
    ) as temporary:
        stage = Path(temporary)
        exported = builder._stage_source(stage, {"source_dirty": False})
        if builder._git("rev-parse", "--verify", "HEAD") != commit:
            raise UpdateError(
                "Source revision changed before the pristine wheel build."
            )
        wheels = stage / "wheels"
        wheels.mkdir(mode=0o700)
        build_environment = stage / ".builder"
        # Modern pipx may use uv and omit pip from the application environment;
        # its runpip interface then cannot build wheels. A disposable stdlib
        # venv gives both pipx backends the same pristine build path.
        _run([sys.executable, "-I", "-m", "venv", build_environment], cwd=stage)
        _run(
            [
                build_environment / "bin/python",
                "-I",
                "-m",
                "pip",
                "wheel",
                "--no-deps",
                "--wheel-dir",
                wheels,
                exported,
            ],
            cwd=stage,
            env=_pipx_env(installation),
        )
        artifacts = list(wheels.glob("*.whl"))
        if len(artifacts) != 1:
            raise UpdateError(
                "The pristine build did not produce exactly one Raven wheel."
            )
        expected = {
            str(path.relative_to(exported)).replace(os.sep, "/")
            for path in (exported / "raven").rglob("*")
            if path.is_file()
            and (path.suffix in {".py", ".pyi"} or path.name == "py.typed")
        }
        with zipfile.ZipFile(artifacts[0]) as wheel:
            actual = {
                name
                for name in wheel.namelist()
                if name.startswith("raven/")
                and (name.endswith((".py", ".pyi")) or name.endswith("/py.typed"))
            }
        if actual != expected:
            raise UpdateError(
                "Built Raven wheel differs from the pinned package inventory."
            )
        extras = "[terminal]" if installation.terminal else ""
        yield "raven" + extras + " @ " + artifacts[0].as_uri()


def _write_receipt(installation, commit, *, dirty):
    path = installation.prefix / ".raven-update.json"
    with directory_fd(path.parent) as parent:
        name = ".update-receipt-" + os.urandom(8).hex()
        fd = os.open(
            name,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
            0o600,
            dir_fd=parent,
        )
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump(
                    {
                        "schema": "raven.installed-update/v1",
                        "installed_commit": commit,
                        "source_dirty": dirty,
                    },
                    stream,
                )
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, path.name, src_dir_fd=parent, dst_dir_fd=parent)
        finally:
            try:
                os.unlink(name, dir_fd=parent)
            except FileNotFoundError:
                pass


def _retain_pipx_source(installation, artifact):
    """Preserve pipx's original source after its version-dependent runpip sync."""
    path = installation.prefix / "pipx_metadata.json"
    before = path.lstat()
    saved = _json(path)
    package = saved.get("main_package")
    if not isinstance(package, dict) or package.get("package") != "raven":
        raise UpdateError("Pipx application identity changed while installing.")
    current_spec = package.get("package_or_url")
    if current_spec == installation.package_spec:
        return  # Older pipx versions do not rewrite this field during runpip.
    if current_spec != artifact:
        raise UpdateError(
            "Pipx source ownership changed while installing; its metadata was preserved."
        )
    package["package_or_url"] = installation.package_spec
    with directory_fd(path.parent) as parent:
        name = ".pipx-source-" + os.urandom(8).hex()
        fd = os.open(
            name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=parent,
        )
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump(saved, stream, indent=4)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            current = os.stat(path.name, dir_fd=parent, follow_symlinks=False)

            def identity(value):
                return (
                    value.st_dev,
                    value.st_ino,
                    value.st_size,
                    value.st_mtime_ns,
                    value.st_ctime_ns,
                )

            if identity(before) != identity(current):
                raise UpdateError(
                    "Pipx metadata changed concurrently; no source metadata was overwritten."
                )
            os.replace(name, path.name, src_dir_fd=parent, dst_dir_fd=parent)
            os.fsync(parent)
        finally:
            try:
                os.unlink(name, dir_fd=parent)
            except FileNotFoundError:
                pass


def install_updated(installation, source, commit, *, preserve=()):
    if installation.kind == "managed":
        script = source / "scripts/install.py"
        trusted_read(script, limit=1024 * 1024, user_only=True)
        command = [
            sys.executable,
            str(script),
            "--prefix",
            str(installation.prefix),
            "--bin-dir",
            str(installation.bin_dir),
            "--no-setup",
            "--expected-commit",
            commit,
        ]
        for path in preserve:
            command += ["--preserve-config", str(path)]
        if not installation.terminal:
            command.append("--without-terminal")
        _run(command, cwd=source)
        new_release = (installation.prefix / "current").resolve()
        marker = _json(new_release / MARKER)
        if (
            new_release == installation.release
            or marker.get("schema") != "raven.user-install/v1"
        ):
            raise UpdateError(
                "Installer returned without publishing a new verified release."
            )
        expected = {
            "source": str(source),
            "prefix": str(installation.prefix),
            "bin_dir": str(installation.bin_dir),
            "terminal": installation.terminal,
            "installed_commit": commit,
        }
        if any(marker.get(key) != value for key, value in expected.items()):
            raise UpdateError(
                "Published installation metadata differs from the selected revision/options; inspect the retained previous release."
            )
    else:
        from ..update_source import content_fingerprint, git

        if git(source, "rev-parse", "HEAD").decode().strip() != commit:
            raise UpdateError("Source revision changed before pipx could install it.")
        before = content_fingerprint(source, protected_paths=tuple(preserve))
        with _pipx_artifact(installation, source, commit) as artifact:
            # runpip preserves the source/extras in pipx_metadata.json. Installing
            # the wheel still lets pip resolve newly required dependencies.
            _run(
                [
                    installation.pipx,
                    "runpip",
                    "raven",
                    "install",
                    "--force-reinstall",
                    artifact,
                ],
                cwd=installation.control_directory,
                env=_pipx_env(installation),
            )
            _retain_pipx_source(installation, artifact)
        actual = git(source, "rev-parse", "HEAD").decode().strip()
        if (
            actual != commit
            or content_fingerprint(source, protected_paths=tuple(preserve)) != before
        ):
            raise UpdateError(
                "Source changed while pipx was reinstalling; rerun update after other source changes finish."
            )
        _check_commands(installation.bin_dir, installation.prefix / "bin")
        for name in COMMANDS:
            _run(
                [installation.prefix / "bin" / name, "--version"],
                cwd=source,
                timeout=30,
            )
        _write_receipt(
            installation,
            commit,
            dirty=bool(git(source, "status", "--porcelain", "--untracked-files=all")),
        )


def _revert_installation_plan(installation, saved):
    if not isinstance(saved, dict) or saved.get("kind") != installation.kind:
        raise UpdateError("Backup belongs to a different installation manager.")
    if saved.get("prefix") != str(installation.prefix) or saved.get("bin_dir") != str(
        installation.bin_dir
    ):
        raise UpdateError("Backup belongs to a different installation directory.")
    if installation.kind == "pipx":
        if saved.get("package_spec") != installation.package_spec:
            raise UpdateError("Pipx source settings changed since this backup.")
        return None
    release = _path(saved.get("release"), "Retained release")
    if release.parent != installation.prefix or not release.name.startswith("release-"):
        raise UpdateError("Backup release is outside this installation.")
    _owned_directory(release)
    marker = _json(release / MARKER)
    if marker.get("schema") != "raven.user-install/v1":
        raise UpdateError("Retained release has an unsupported installer marker.")
    if (marker.get("installed_commit") or "") != (saved.get("installed_commit") or ""):
        raise UpdateError("Retained release revision differs from the backup.")
    for name in COMMANDS:
        trusted_read(release / "bin" / name, limit=65536, user_only=True)
        if not os.access(release / "bin" / name, os.X_OK):
            raise UpdateError("Retained launcher is not executable: " + name)
    python = release / "bin/python"
    if not python.is_file() or not os.access(python, os.X_OK):
        raise UpdateError("Retained release interpreter is unavailable.")
    return release


def activate_retained(installation, saved):
    with _lock(installation.prefix, ".install.lock"):
        release = _revert_installation_plan(installation, saved)
        _check_commands(installation.bin_dir, installation.prefix / "current/bin")
        current = installation.prefix / "current"
        if not current.is_symlink() or current.resolve() != installation.release:
            raise UpdateError(
                "Active release changed during revert; no release was switched."
            )
        temporary = installation.prefix / (".revert-" + os.urandom(8).hex())
        try:
            temporary.symlink_to(release.name)
            os.replace(temporary, current)
        finally:
            temporary.unlink(missing_ok=True)


def _summary(installation, result):
    print("Raven installation: " + installation.kind)
    print("Source: " + str(installation.source))
    print("Branch: " + str(result.get("branch", "")))
    print("Commands: " + str(installation.bin_dir))
    print("Installation directory: " + str(installation.prefix))
    print(
        "Embedded terminal: " + ("included" if installation.terminal else "core only")
    )
    print(
        "Installed revision: " + (installation.installed_commit[:12] or "not recorded")
    )
    if result.get("target_head"):
        label = (
            "Available revision: "
            if result.get("network_checked")
            else "Cached upstream revision: "
        )
        print(label + result["target_head"][:12])
    if result.get("source_dirty", result.get("dirty")):
        print(
            "Local source changes are present; ordinary update preserves them and stops."
        )
    if result.get("divergent") or result.get("ahead"):
        print(
            "The checkout cannot fast-forward; ordinary update preserves its commits and stops."
        )


def _backup_message(result):
    backup = result.get("backup") or result.get("recovery_backup")
    if backup:
        print("Recovery backup: " + str(backup))
        print("To restore it: " + shlex.join(["raven", "revert", str(backup)]))


def _prune_backups(installation, *, keep=()):
    """Keep three recovery points and every release those points require."""
    root = installation.control_directory / "backups"
    if not root.is_dir():
        return
    from ..update_source import git

    complete = []
    for directory in sorted(root.glob("raven-source-*"), reverse=True):
        if directory.is_symlink() or not directory.is_dir():
            continue
        try:
            raw, _ = trusted_read(
                directory / "manifest.json", user_only=True, limit=16 << 20
            )
            marker = _json(directory / "COMPLETE.json")
            import hashlib

            value = json.loads(raw)
            original = value.get("original_head") if isinstance(value, dict) else None
            if (
                not isinstance(value, dict)
                or not isinstance(original, str)
                or len(original) not in (40, 64)
                or any(character not in "0123456789abcdef" for character in original)
                or not isinstance(value.get("installation", {}), dict)
                or marker.get("schema") != "raven.source-backup-complete/v1"
                or marker.get("manifest_sha256") != hashlib.sha256(raw).hexdigest()
                or value.get("schema") != "raven.source-backup/v1"
                or value.get("retained_ref")
                != "refs/raven/update-backups/" + directory.name
            ):
                continue
        except (OSError, ValueError, RecursionError):
            continue
        complete.append((directory, value))
    protected = {Path(path) for path in keep if path}
    retained = {path for path, _ in complete[:3]} | protected
    for directory, value in complete:
        if directory in retained:
            continue
        git(
            installation.source,
            "update-ref",
            "-d",
            value["retained_ref"],
            value["original_head"],
        )
        shutil.rmtree(directory)
    if installation.kind != "managed":
        return
    releases = {installation.release, (installation.prefix / "current").resolve()}
    for directory, value in complete:
        if directory in retained:
            release = value.get("installation", {}).get("release")
            if isinstance(release, str) and Path(release).parent == installation.prefix:
                releases.add(Path(release))
    with _lock(installation.prefix, ".install.lock"):
        for release in installation.prefix.glob("release-*"):
            if release in releases or release.is_symlink() or not release.is_dir():
                continue
            try:
                marker = _json(release / MARKER)
            except (OSError, ValueError):
                continue
            if marker.get("schema") == "raven.user-install/v1" and marker.get(
                "prefix"
            ) == str(installation.prefix):
                shutil.rmtree(release)


def _prune_safely(installation, *, keep=()):
    try:
        _prune_backups(installation, keep=keep)
    except (OSError, ValueError) as exc:
        print(
            "Maintenance succeeded; recovery retention cleanup was deferred: "
            + str(exc),
            file=sys.stderr,
        )


def _update(args, installation):
    from ..update_source import update_source
    from ..update_verification import verify

    mode = "plan" if args.plan else "check" if args.check else "update"

    def authorize(info):
        policy = verify(
            info,
            allow_fork=getattr(args, "allow_fork", False),
            require_attestation=getattr(args, "require_attestation", False),
            artifact=getattr(args, "attested_artifact", None),
            bundle=getattr(args, "attestation_bundle", None),
            plan=args.plan,
        )
        print(
            "Selected source range: "
            + info["original_head"]
            + ".."
            + info["target_head"]
        )
        print(
            "Source channel: "
            + policy["source_channel"]
            + "; artifact attestation: "
            + policy["artifact_attestation"]
        )
        return policy

    result = update_source(
        installation.source,
        mode=mode,
        overwrite=args.overwrite,
        protected_paths=protected_paths(args.config),
        backup_root=installation.control_directory / "backups",
        backup_metadata=installation.record(),
        authorize=authorize,
    )
    _summary(installation, result)
    _backup_message(result)
    if mode != "update":
        print(
            "Plan only; no network access."
            if mode == "plan"
            else "Check complete; checkout and installed package unchanged."
        )
        return 0
    if (
        not args.reinstall
        and installation.source_dirty is False
        and result.get("target_head") == installation.installed_commit
        and not result.get("changed")
    ):
        print("Raven is already up to date.")
        return 0
    print(
        "Installing the selected revision; configuration and state are preserved.",
        flush=True,
    )
    if installation.kind == "pipx":
        # pipx replaces the running package in place; retain recovery code first.
        from ..update_revert import revert_source
    try:
        install_updated(
            installation,
            installation.source,
            result["target_head"],
            preserve=protected_paths(args.config),
        )
    except BaseException:
        current = installation.prefix / "current"
        published = (
            installation.kind == "managed"
            and current.is_symlink()
            and current.resolve() != installation.release
        )
        if installation.kind == "managed" and not published:
            print(
                "The previous managed release active at "
                + str(installation.release)
                + " was preserved.",
                file=sys.stderr,
            )
        if result.get("backup") and not published:
            if installation.kind != "pipx":
                from ..update_revert import revert_source
            try:
                revert_source(
                    result["backup"],
                    mode="revert",
                    source_override=installation.source,
                    protected_paths=protected_paths(args.config),
                    backup_root=installation.control_directory / "backups",
                    backup_metadata=installation.record(),
                )
                detail = (
                    "Your configuration and previous release were preserved."
                    if installation.kind == "managed"
                    else "Your configuration was preserved; pipx controls recovery of its installed environment."
                )
                print(
                    "Installation failed; the source checkout was restored automatically. "
                    + detail,
                    file=sys.stderr,
                )
            except (OSError, ValueError, RuntimeError) as rollback:
                print(
                    "Automatic source restoration could not complete: "
                    + str(rollback)
                    + ". Use the retained backup shown below.",
                    file=sys.stderr,
                )
        elif published:
            print(
                "A new managed release is active at "
                + str(current.resolve())
                + "; the post-publication check failed. Inspect it or revert using the retained backup.",
                file=sys.stderr,
            )
        elif installation.kind != "managed":
            print(
                "The source checkout may have advanced. Pipx controls installation recovery; retry raven update or pipx reinstall raven.",
                file=sys.stderr,
            )
        _backup_message(result)
        raise
    print("Raven updated. Restart your dashboard to use the new release.")
    _prune_safely(installation, keep=(result.get("backup"),))
    return 0


def _revert(args, installation):
    from ..update_revert import revert_source

    options = {
        "protected_paths": protected_paths(args.config),
        "backup_root": installation.control_directory / "backups",
        "source_override": installation.source,
        "backup_metadata": installation.record(),
    }
    preview = revert_source(args.backup, mode="plan", **options)
    saved = preview.get("installation")
    _revert_installation_plan(installation, saved)
    if args.plan:
        print("Restore source: " + str(installation.source))
        print("Restore backup: " + str(args.backup))
        print("Current configuration/state will be preserved. No files were changed.")
        return 0
    result = revert_source(args.backup, mode="revert", **options)
    _backup_message(result)
    if installation.kind == "managed":
        activate_retained(installation, saved)
    else:
        install_updated(installation, installation.source, result["original_head"])
    print(
        "Raven reverted. Current configuration and state were preserved; restart your dashboard."
    )
    _prune_safely(
        installation,
        keep=(args.backup, result.get("backup"), result.get("recovery_backup")),
    )
    return 0


def cli(argv=None):
    os.umask(0o022)
    parser = argparse.ArgumentParser(
        prog="raven", description=__doc__, allow_abbrev=False
    )
    commands = parser.add_subparsers(dest="command", required=True)
    update = commands.add_parser(
        "update",
        help="Fetch the tracked branch and update this installation",
        allow_abbrev=False,
    )
    update.add_argument(
        "--overwrite",
        action="store_true",
        help="Back up and replace local source changes; preserve user configuration/state",
    )
    group = update.add_mutually_exclusive_group()
    group.add_argument(
        "--check",
        action="store_true",
        help="Fetch and compare without changing checkout files or the installed package",
    )
    group.add_argument(
        "--plan",
        action="store_true",
        help="Show the update plan without network access or writes",
    )
    update.add_argument(
        "--reinstall",
        action="store_true",
        help="Rebuild even when the recorded installed revision is current",
    )
    update.add_argument(
        "--allow-fork",
        action="store_true",
        help="Explicitly authorize the configured non-official tracking repository",
    )
    update.add_argument(
        "--require-attestation",
        action="store_true",
        help="Fail unless an artifact proves this exact commit under Raven GitHub/Sigstore signer policy",
    )
    update.add_argument(
        "--attested-artifact",
        type=Path,
        help="Locally downloaded artifact from the selected commit for provenance verification",
    )
    update.add_argument(
        "--attestation-bundle",
        type=Path,
        help="Optional downloaded GitHub/Sigstore attestation bundle",
    )
    revert = commands.add_parser(
        "revert",
        help="Restore a saved source backup and its installation",
        allow_abbrev=False,
    )
    revert.add_argument(
        "backup",
        type=Path,
        help="Recovery backup directory printed by update or revert",
    )
    revert.add_argument(
        "--plan",
        action="store_true",
        help="Validate and preview restoration without changing files",
    )
    for sub in (update, revert):
        sub.add_argument(
            "--source",
            type=Path,
            help="Original or relocated source checkout (managed installs only)",
        )
        sub.add_argument(
            "--config",
            action="append",
            default=[],
            metavar="PATH",
            help="Preserve an additional configuration file/directory inside the checkout; repeatable",
        )
    args = parser.parse_args(argv)
    if (
        args.command == "update"
        and (args.attested_artifact or args.attestation_bundle)
        and not args.require_attestation
    ):
        parser.error(
            "--attested-artifact/--attestation-bundle require --require-attestation"
        )
    handlers = {}

    def interrupted(signum, frame):
        raise MaintenanceInterrupted("Interrupted by signal " + str(signum))

    try:
        try:
            for signum in (
                signal.SIGINT,
                signal.SIGTERM,
                signal.SIGHUP,
                signal.SIGQUIT,
            ):
                handlers[signum] = signal.signal(signum, interrupted)
        except ValueError:
            pass  # Embedders may invoke maintenance outside the main thread.
        installation = discover(args.source)
        _validate_layout(installation)
        action = _update if args.command == "update" else _revert
        if args.plan or getattr(args, "check", False):
            return action(args, installation)
        print(
            "Close other Raven dashboards and owned workers before maintenance; this command does not terminate jobs.",
            flush=True,
        )
        with _lock(installation.control_directory, ".update.lock"):
            return action(args, installation)
    except (KeyboardInterrupt, MaintenanceInterrupted) as exc:
        print(
            "Raven "
            + args.command
            + " interrupted. Retained releases and recovery backups are preserved.",
            file=sys.stderr,
        )
        _backup_message({"backup": getattr(exc, "backup", None)})
        return 130
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
        print("Raven " + args.command + ": " + str(exc), file=sys.stderr)
        backup = getattr(exc, "backup", None)
        if backup:
            _backup_message({"backup": backup})
        return 2
    finally:
        for signum, previous in handlers.items():
            signal.signal(signum, previous)
