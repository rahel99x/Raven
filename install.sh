#!/usr/bin/env bash
# Install from this checkout, or bootstrap the official main branch when piped.
# Keep the invocation last: Bash must parse this whole function before the
# installer reconnects standard input to the controlling terminal.
set -euo pipefail

raven_install_main() {
    local raven_source='' raven_python='' raven_plan=0 raven_wrappers=1
    local raven_script="${BASH_SOURCE[0]:-}" raven_candidate raven_checkout
    local -a raven_args=()
    while (($#)); do
        case "$1" in
            -h|--help)
                if ((!raven_wrappers)); then
                    raven_args+=("$1"); shift; continue
                fi
                cat <<'HELP'
Install Raven without sudo, pipx, or shell startup-file changes.

  bash install.sh [--source-dir PATH] [--python EXECUTABLE] [OPTIONS]
  curl -fsSL https://raw.githubusercontent.com/rahel99x/Raven/main/install.sh | bash

From a checkout, install its current contents. Otherwise clone official main
into ${XDG_DATA_HOME:-$HOME/.local/share}/raven/source. Existing Raven checkouts
are reused without fetching or overwriting local edits; use raven update later.

Bootstrap options:
  --source-dir PATH       Choose the bootstrap checkout (overrides local source)
  --python EXECUTABLE     Use a specific Python 3.10+ interpreter
  --                     Forward remaining options directly to the installer

Installer options:
  --prefix PATH          Dedicated release directory
  --bin-dir PATH         Launcher directory (default: ~/.local/bin)
  --without-terminal     Skip optional embedded-terminal dependencies
  --no-setup             Skip guided setup after installation
  --plan                 Inspect only; do not clone or install

Python 3.10+ is required. Git is needed only for a bootstrap checkout.
HELP
                return 0 ;;
            --)
                if ((raven_wrappers)); then raven_wrappers=0; shift; continue; fi ;;
            --source-dir|--python)
                if ((raven_wrappers)); then
                    if (($# < 2)) || [[ -z "$2" ]]; then
                        printf 'Raven installation: %s requires a value.\n' "$1" >&2
                        return 2
                    fi
                    if [[ "$1" == --source-dir ]]; then raven_source="$2"; else raven_python="$2"; fi
                    shift 2; continue
                fi ;;
            --source-dir=*|--python=*)
                if ((raven_wrappers)); then
                    if [[ -z "${1#*=}" ]]; then
                        printf 'Raven installation: %s requires a value.\n' "${1%%=*}" >&2
                        return 2
                    fi
                    if [[ "$1" == --source-dir=* ]]; then raven_source="${1#*=}"; else raven_python="${1#*=}"; fi
                    shift; continue
                fi ;;
            --prefix|--bin-dir)
                if (($# < 2)); then
                    printf 'Raven installation: %s requires a value.\n' "$1" >&2
                    return 2
                fi
                raven_args+=("$1" "$2"); shift 2; continue ;;
            --plan) raven_plan=1 ;;
        esac
        raven_args+=("$1"); shift
    done

    if [[ -n "$raven_python" ]]; then
        if ! "$raven_python" -c 'import sys; raise SystemExit(sys.version_info < (3, 10))' >/dev/null 2>&1; then
            printf 'Raven installation: --python must name a working Python 3.10+ interpreter.\n' >&2
            return 2
        fi
    else
        for raven_candidate in python3 python; do
            if command -v "$raven_candidate" >/dev/null 2>&1 && "$raven_candidate" -c 'import sys; raise SystemExit(sys.version_info < (3, 10))' >/dev/null 2>&1; then
                raven_python="$raven_candidate"; break
            fi
        done
        if [[ -z "$raven_python" ]]; then
            printf 'Raven installation: install Python 3.10+ first, or select it with --python.\n' >&2
            return 2
        fi
    fi

    raven_checkout="$("$raven_python" - "$raven_script" "$raven_source" "$raven_plan" <<'PY'
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time

OFFICIAL = "https://github.com/rahel99x/Raven.git"
UID = os.getuid()
SYSTEM_OWNERS = {0, os.stat("/").st_uid}

class BootstrapError(RuntimeError):
    pass

class BootstrapInterrupted(BaseException):
    def __init__(self, signum):
        self.signum = signum

def interrupted(signum, frame):
    # Popen treats KeyboardInterrupt specially and may reap before unwinding.
    # Keep our child unreaped until cleanup has finished signaling its group.
    raise BootstrapInterrupted(signum)

def absolute(value):
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise BootstrapError("Source paths must not contain control characters.")
    return Path(os.path.abspath(os.path.expanduser(value)))

def directory(path, create=False, parent=False):
    for part in (*reversed(path.parents), path):
        try:
            info = part.lstat()
        except FileNotFoundError:
            if not create:
                continue
            try:
                part.mkdir(mode=0o700)
            except FileExistsError:
                pass
            info = part.lstat()
        if not stat.S_ISDIR(info.st_mode):
            raise BootstrapError("Existing path preserved; choose a real directory, not a symlink: " + str(part))
        if info.st_uid not in SYSTEM_OWNERS | {UID}:
            raise BootstrapError("Source directory has an untrusted owner: " + str(part))
        sticky_parent = (part != path or parent) and info.st_uid in SYSTEM_OWNERS and info.st_mode & stat.S_ISVTX
        if info.st_mode & 0o022 and not sticky_parent:
            raise BootstrapError("Source directories must not be group/world writable: " + str(part))
    if not parent and path.exists() and path.stat().st_uid != UID:
        raise BootstrapError("The source directory must belong to your user: " + str(path))

def raven_files(path):
    try:
        scripts = (path / "scripts").lstat()
    except FileNotFoundError:
        return False
    if not stat.S_ISDIR(scripts.st_mode) or scripts.st_uid not in SYSTEM_OWNERS | {UID} or scripts.st_mode & 0o022:
        return False
    for name in ("pyproject.toml", "scripts/install.py"):
        item = path / name
        try:
            info = item.lstat()
        except FileNotFoundError:
            return False
        if not stat.S_ISREG(info.st_mode) or info.st_uid not in SYSTEM_OWNERS | {UID} or info.st_mode & 0o022:
            return False
    # A bounded project marker avoids executing an unrelated install.py.
    with (path / "pyproject.toml").open("rb") as stream:
        marker = stream.read(65537)
    return len(marker) <= 65536 and re.search(rb'(?m)^name\s*=\s*["\x27]raven["\x27]\s*$', marker) is not None

def git_env():
    # Keep the user's credential, proxy, CA, global/system and URL-rewrite
    # settings. Only inherited checkout selectors can redirect this operation.
    redirects = {"GIT_DIR", "GIT_COMMON_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE",
                 "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_NAMESPACE", "GIT_PREFIX",
                 "GIT_CEILING_DIRECTORIES", "GIT_DISCOVERY_ACROSS_FILESYSTEM", "GIT_IMPLICIT_WORK_TREE",
                 "GIT_REPLACE_REF_BASE", "GIT_SHALLOW_FILE", "GIT_GRAFT_FILE", "GIT_QUARANTINE_PATH", "GIT_CONFIG"}
    redirect_keys = {"core.worktree", "core.bare"}
    env = {key: value for key, value in os.environ.items() if key not in redirects}
    count = env.get("GIT_CONFIG_COUNT")
    if count is not None:
        if not count.isdigit() or len(count) > 3 or int(count) > 128:
            raise BootstrapError("Injected Git configuration exceeds its entry bound.")
        entries, total = [], 0
        for index in range(int(count)):
            key = env.get("GIT_CONFIG_KEY_" + str(index))
            value = env.get("GIT_CONFIG_VALUE_" + str(index))
            if key is None or value is None:
                raise BootstrapError("Injected Git configuration is incomplete.")
            total += len(key) + len(value)
            if total > 65536:
                raise BootstrapError("Injected Git configuration exceeds its byte bound.")
            if key.lower() not in redirect_keys:
                entries.append((key, value))
        env = {key: value for key, value in env.items()
               if not key.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_"))}
        env["GIT_CONFIG_COUNT"] = str(len(entries))
        for index, (key, value) in enumerate(entries):
            env["GIT_CONFIG_KEY_" + str(index)] = key
            env["GIT_CONFIG_VALUE_" + str(index)] = value
    parameters = env.get("GIT_CONFIG_PARAMETERS")
    if parameters:
        if len(parameters) > 65536:
            raise BootstrapError("Injected Git configuration exceeds its byte bound.")
        try:
            values = shlex.split(parameters)
        except ValueError as exc:
            raise BootstrapError("Injected Git configuration is malformed.") from exc
        env["GIT_CONFIG_PARAMETERS"] = " ".join("'" + value.replace("'", "'\\''") + "'" for value in values
            if value.split("=", 1)[0].lower() not in redirect_keys)
    env.update(GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0", GIT_NO_REPLACE_OBJECTS="1")
    return env

def git(*args, capture=False):
    process = None
    try:
        process = subprocess.Popen(["git", "-c", "core.fsmonitor=false", *map(str, args)],
                                   env=git_env(), stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE if capture else sys.stderr,
                                   stderr=subprocess.PIPE if capture else sys.stderr,
                                   text=True, start_new_session=True)
        output, _ = process.communicate(timeout=30 if capture else 600)
    except (OSError, subprocess.TimeoutExpired) as exc:
        stop_git(process)
        raise BootstrapError("Git could not complete the source checkout operation: " + str(exc)) from exc
    except BaseException:
        stop_git(process)
        raise
    if process.returncode:
        raise BootstrapError("Git could not complete the source checkout operation; existing files were preserved.")
    return output.strip() if capture else None

def stop_git(process):
    if process is None or process.returncode is not None:
        return
    previous = {signum: signal.signal(signum, signal.SIG_IGN)
                for signum in (signal.SIGINT, signal.SIGTERM)}
    try:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if hasattr(os, "waitid") and hasattr(os, "WNOWAIT"):
                try:
                    observed = os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
                except ChildProcessError:
                    # An unexpected external reaper removed the ownership pin.
                    return
                if observed is not None:
                    break
            time.sleep(min(0.02, max(0, deadline - time.monotonic())))
        # Do not wait/poll first: even an exited leader must remain unreaped so
        # its PID cannot be reused before this final process-group signal.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)

def checkout(path):
    directory(path)
    if not raven_files(path) or not os.path.lexists(path / ".git") or (path / ".git").is_symlink():
        raise BootstrapError("Existing directory preserved; it is not a Raven Git checkout: " + str(path))
    if git("-C", path, "rev-parse", "--show-toplevel", capture=True) != str(path):
        raise BootstrapError("Existing directory preserved; it is not a standalone Raven checkout: " + str(path))
    # Compare the declared origin; get-url expands authorized transport rewrites.
    remote = git("-C", path, "config", "--get-all", "remote.origin.url", capture=True).lower().rstrip("/")
    if remote.endswith(".git"):
        remote = remote[:-4]
    if remote not in {"https://github.com/rahel99x/raven", "git@github.com:rahel99x/raven", "ssh://git@github.com/rahel99x/raven"}:
        raise BootstrapError("Existing checkout preserved; origin is not the official Raven repository.")

def identity(path):
    info = path.lstat()
    return (info.st_dev, info.st_ino, info.st_uid, stat.S_IFMT(info.st_mode))

def remove_owned(path, expected):
    try:
        if identity(path) == expected and expected[2:] == (UID, stat.S_IFDIR):
            shutil.rmtree(path)
    except FileNotFoundError:
        pass

def main():
    script, explicit, plan = sys.argv[1:]
    if not explicit and script:
        local = absolute(str(Path(script).resolve().parent))
        if raven_files(local):
            directory(local)
            print(local)
            return
    root = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local/share")
    target = absolute(explicit or str(Path(root) / "raven/source"))
    directory(target)
    if not os.path.lexists(target) and plan == "1":
        print("Source plan: clone official Raven main into " + str(target) + ".", file=sys.stderr)
        print("No files or network requests made. Rerun without --plan to install.", file=sys.stderr)
        return
    if shutil.which("git") is None:
        raise BootstrapError("Install Git to bootstrap a checkout, or run install.sh from an existing source checkout.")
    if os.path.lexists(target):
        checkout(target)
        print("Reusing " + str(target) + " without fetching or changing local files.", file=sys.stderr)
        print(target)
        return
    os.umask(0o077)
    directory(target.parent, create=True, parent=True)
    temporary = Path(tempfile.mkdtemp(prefix=".raven-source-", dir=target.parent))
    temporary_identity = identity(temporary)
    target_identity = None
    complete = False
    try:
        staged = temporary / "checkout"
        git("clone", "--branch", "main", "--single-branch", "--", OFFICIAL, staged)
        checkout(staged)
        # Exclusive reservation never replaces a path created while Git ran.
        try:
            target.mkdir(mode=0o700)
        except FileExistsError as exc:
            raise BootstrapError("Destination appeared during cloning and was preserved: " + str(target)) from exc
        target_identity = identity(target)
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        source_fd = os.open(staged, flags)
        try:
            target_fd = os.open(target, flags)
            try:
                # Publish .git last so an interrupted partial checkout is not reused.
                for name in sorted(os.listdir(source_fd), key=lambda name: name == ".git"):
                    os.rename(name, name, src_dir_fd=source_fd, dst_dir_fd=target_fd)
            finally:
                os.close(target_fd)
        finally:
            os.close(source_fd)
        if identity(target) != target_identity:
            raise BootstrapError("Source destination changed during publication; installation stopped.")
        checkout(target)
        complete = True
        print(target)
    finally:
        if not complete and target_identity is not None:
            remove_owned(target, target_identity)
        remove_owned(temporary, temporary_identity)

previous_handlers = {signum: signal.signal(signum, interrupted)
                     for signum in (signal.SIGINT, signal.SIGTERM)}
try:
    main()
except (OSError, ValueError, BootstrapError) as exc:
    print("Raven installation: " + str(exc), file=sys.stderr)
    raise SystemExit(2)
except BootstrapInterrupted as exc:
    print("Raven installation: source checkout cancelled.", file=sys.stderr)
    raise SystemExit(128 + exc.signum)
finally:
    for signum, handler in previous_handlers.items():
        signal.signal(signum, handler)
PY
)" || return $?
    [[ -n "$raven_checkout" ]] || return 0
    # Piped download supplies Bash source, not interactive answers. Reconnect
    # only when output is interactive and an actual controlling TTY exists.
    if [[ ! -t 0 && -t 1 ]] && ( : </dev/tty ) 2>/dev/null; then
        exec "$raven_python" "$raven_checkout/scripts/install.py" "${raven_args[@]}" </dev/tty
    fi
    exec "$raven_python" "$raven_checkout/scripts/install.py" "${raven_args[@]}"
}

raven_install_main "$@"
