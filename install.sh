#!/usr/bin/env bash
# Install from this checkout, or bootstrap the official main branch when piped.
# Keep the invocation last: Bash must parse this whole function before the
# installer reconnects standard input to the controlling terminal.
set -euo pipefail
umask 022

raven_install_main() {
    local raven_source='' raven_python='' raven_plan=0 raven_wrappers=1 raven_clone=0
    local raven_script="${BASH_SOURCE[0]:-}" raven_candidate raven_checkout raven_checkout_file raven_code
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
  --clone                 Explicitly allow creating a missing --source-dir
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
            --clone) raven_clone=1; shift; continue ;;
        esac
        raven_args+=("$1"); shift
    done

    if [[ -n "$raven_python" ]]; then
        if ! "$raven_python" -I -c 'import sys; raise SystemExit(sys.version_info < (3, 10))' >/dev/null 2>&1; then
            printf 'Raven installation: --python must name a working Python 3.10+ interpreter.\n' >&2
            return 2
        fi
    else
        for raven_candidate in python3.14 python3.13 python3.12 python3.11 python3.10 python3 python; do
            if command -v "$raven_candidate" >/dev/null 2>&1 && "$raven_candidate" -I -c 'import sys; raise SystemExit(sys.version_info < (3, 10))' >/dev/null 2>&1; then
                raven_python="$raven_candidate"; break
            fi
        done
        if [[ -z "$raven_python" ]]; then
            printf 'Raven installation: install Python 3.10+ first, or select it with --python.\n' >&2
            return 2
        fi
    fi

    raven_checkout_file="$("$raven_python" -I -c 'import os,tempfile; fd,path=tempfile.mkstemp(prefix="raven-bootstrap-"); os.close(fd); print(path)')" || return $?
    raven_code=0
    "$raven_python" -I - "$raven_script" "$raven_source" "$raven_plan" "$raven_clone" >"$raven_checkout_file" <<'PY' || raven_code=$?
import grp
import fcntl
import json
import os
from pathlib import Path
import re
import selectors
import shlex
import shutil
import signal
import stat
import pwd
import subprocess
import sys
import tempfile
import time

OFFICIAL = "https://github.com/rahel99x/Raven.git"
UID = os.getuid()
SYSTEM_OWNERS = {0, os.stat("/").st_uid}

def private_group(gid):
    try:
        user, group = pwd.getpwuid(UID), grp.getgrgid(gid)
        return user.pw_gid == gid and group.gr_name == user.pw_name and not group.gr_mem
    except KeyError:
        return False

def unsafe_write(info, shielded=False):
    return bool(info.st_mode & 0o002 or info.st_mode & 0o020 and not (shielded or private_group(info.st_gid)))

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
    return Path(os.path.realpath(os.path.expanduser(value)))

def shared_root(path):
    # Bootstrap has no installed Raven to import. Read only the same bounded,
    # private inode-bound grant registry used by config_trust after installation.
    registry = absolute(os.environ.get('XDG_CONFIG_HOME') or str(Path.home() / '.config')) / 'raven/trust/roots.json'
    try:
        directory(registry.parent, grants=False)
        if registry.parent.stat().st_uid != UID or registry.parent.stat().st_mode & 0o077:
            return None
        fd = os.open(registry, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != UID or info.st_mode & 0o077 or info.st_size > 65536:
                return None
            def pairs(items):
                result = {}
                for key, value in items:
                    if key in result:
                        raise ValueError('duplicate trust field')
                    result[key] = value
                return result
            value = json.loads(stream.read(65537), object_pairs_hook=pairs)
            after = os.fstat(stream.fileno())
            pin = lambda item: (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns, item.st_ctime_ns)
            if pin(info) != pin(after):
                return None
        if not isinstance(value, dict):
            return None
        roots = value.get('roots', [])
        if value.get('schema') != 'raven.trusted-paths/v1' or not isinstance(roots, list) or len(roots) > 128:
            return None
        for grant in roots:
            if not isinstance(grant, dict) or set(grant) != {'path', 'uid', 'dev', 'ino'}:
                return None
            if not isinstance(grant['path'], str) or len(grant['path']) > 4096 or any(ord(char) < 32 or ord(char) == 127 for char in grant['path']):
                return None
            if any(type(grant[key]) is not int or grant[key] < 0 for key in ('uid','dev','ino')):
                return None
            root = Path(grant['path'])
            if str(root) != grant['path'] or '..' in root.parts:
                return None
            if not root.is_absolute() or root == Path('/') or path != root and root not in path.parents:
                continue
            current = root.lstat()
            if stat.S_ISDIR(current.st_mode) and (current.st_uid, current.st_dev, current.st_ino) == (grant['uid'], grant['dev'], grant['ino']):
                return root
    except (OSError, ValueError, TypeError, KeyError, RecursionError, BootstrapError):
        pass
    return None

def directory(path, create=False, parent=False, grants=True):
    shielded = False
    chosen = shared_root(path) if grants else None
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
        explicit = chosen is not None and (part == chosen or chosen in part.parents or part in chosen.parents)
        if info.st_uid not in SYSTEM_OWNERS | {UID} and not explicit:
            raise BootstrapError("Source directory has an untrusted owner: " + str(part))
        sticky_parent = (part != path or parent) and info.st_uid in SYSTEM_OWNERS and info.st_mode & stat.S_ISVTX
        if unsafe_write(info, shielded or explicit) and not sticky_parent:
            raise BootstrapError("Source directories must not be group/world writable: " + str(part))
        shielded = shielded or info.st_uid == UID and not info.st_mode & 0o077
    if not parent and path.exists() and path.stat().st_uid != UID and chosen is None:
        raise BootstrapError("The source directory must belong to your user: " + str(path))
    return shielded or chosen is not None

def raven_files(path):
    shielded = directory(path)
    try:
        scripts = (path / "scripts").lstat()
    except FileNotFoundError:
        return False
    if not stat.S_ISDIR(scripts.st_mode) or (scripts.st_uid not in SYSTEM_OWNERS | {UID} and not shared_root(path)) or unsafe_write(scripts, shielded):
        return False
    for name in ("pyproject.toml", "scripts/install.py"):
        item = path / name
        try:
            info = item.lstat()
        except FileNotFoundError:
            return False
        if not stat.S_ISREG(info.st_mode) or (info.st_uid not in SYSTEM_OWNERS | {UID} and not shared_root(path)) or unsafe_write(info, shielded):
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
        process = subprocess.Popen(["git", "-c", "core.fsmonitor=false", "-c", "pack.threads=1", "-c", "index.threads=1",
                                   "-c", "fetch.parallel=1", "-c", "checkout.workers=1", *map(str, args)],
                                   env=git_env(), stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   start_new_session=True)
        output, errors = bytearray(), bytearray()
        deadline = time.monotonic() + (30 if capture else 600)
        with selectors.DefaultSelector() as selector:
            for stream in (process.stdout, process.stderr):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ)
            while selector.get_map():
                if time.monotonic() >= deadline:
                    raise subprocess.TimeoutExpired('git', 30 if capture else 600)
                for key, _ in selector.select(min(.1, max(0, deadline - time.monotonic()))):
                    block = os.read(key.fd, 65536)
                    if not block:
                        selector.unregister(key.fileobj)
                        continue
                    if key.fileobj is process.stderr:
                        errors.extend(block)
                        del errors[:-4096]
                    elif capture:
                        output.extend(block)
                        if len(output) > 16 << 20:
                            raise BootstrapError('Git metadata exceeds its output bound.')
            process.wait(timeout=max(.01, deadline - time.monotonic()))
    except (OSError, subprocess.TimeoutExpired) as exc:
        stop_git(process)
        raise BootstrapError("Git could not complete the source checkout operation: " + str(exc)) from exc
    except BaseException:
        stop_git(process)
        raise
    finally:
        if process is not None:
            for stream in (process.stdout, process.stderr):
                if stream is not None:
                    stream.close()
    if process.returncode:
        # Existing credential helpers may put URLs in stderr. Show useful Git
        # diagnostics without echoing embedded credentials to the terminal.
        detail = errors.decode('utf-8', errors='replace')
        detail = re.sub(r'(?i)(https?://)[^/\s@]+@', r'\1[REDACTED]@', detail)
        detail = re.sub(r'(?i)(authorization\s*[:=]\s*)[^\r\n]+', r'\1[REDACTED]', detail)
        detail = re.sub(r'(?i)(\b[\w-]*(?:password|passwd|token|secret|api[_-]?key)[\w-]*\s*[=:]\s*)\S+', r'\1[REDACTED]', detail)[-2048:]
        raise BootstrapError("Git could not complete the source checkout operation; existing files were preserved. " + detail)
    return output.decode('utf-8', errors='strict').strip() if capture else None

def stop_git(process):
    if process is None or process.returncode is not None:
        return
    previous = {signum: signal.signal(signum, signal.SIG_IGN)
                for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT)}
    try:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        deadline = time.monotonic() + 10
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

def sweep_staging(parent):
    for candidate in list(parent.glob('.raven-source-*'))[:256]:
        try:
            info = candidate.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != UID or info.st_mode & 0o077:
                continue
            marker = candidate / '.raven-staging.json'
            fd = os.open(marker, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, 'rb') as stream:
                pin = os.fstat(stream.fileno())
                if not stat.S_ISREG(pin.st_mode) or pin.st_uid != UID or pin.st_mode & 0o077 or pin.st_size > 4096:
                    continue
                value = json.loads(stream.read(4097))
            if value.get('schema') != 'raven.bootstrap-stage/v1' or value.get('path') != str(candidate):
                continue
            pid = value.get('pid')
            if type(pid) is not int or pid <= 1 or time.time() - value.get('created', time.time()) < 3600:
                continue
            try:
                os.kill(pid, 0)
                continue
            except ProcessLookupError:
                pass
            remove_owned(candidate, identity(candidate))
        except (OSError, ValueError, TypeError):
            continue

def main():
    script, explicit, plan, clone = sys.argv[1:]
    if not explicit and script and Path(script).name == 'install.sh' and Path(script).is_file():
        local = absolute(str(Path(script).resolve().parent))
        if raven_files(local):
            directory(local)
            print(local)
            return
    root = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local/share")
    target = absolute(explicit or str(Path(root) / "raven/source"))
    if explicit and clone != '1' and not os.path.lexists(target):
        siblings = [entry.name for entry in target.parent.iterdir() if entry.name.casefold() == target.name.casefold()] if target.parent.is_dir() else []
        raise BootstrapError("Explicit --source-dir does not exist: " + str(target) + (". Did you mean " + ', '.join(siblings) + '?' if siblings else ''))
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
    lock = os.open(target.parent / '.raven-bootstrap.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    lock_info = os.fstat(lock)
    if not stat.S_ISREG(lock_info.st_mode) or lock_info.st_uid != UID or lock_info.st_mode & 0o077 or lock_info.st_nlink != 1:
        os.close(lock)
        raise BootstrapError('The bootstrap lock must be a private file owned by your user.')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        os.close(lock)
        raise BootstrapError('Another Raven bootstrap is in progress.') from exc
    sweep_staging(target.parent)
    temporary = Path(tempfile.mkdtemp(prefix=".raven-source-", dir=target.parent))
    temporary_identity = identity(temporary)
    marker = temporary / '.raven-staging.json'
    fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump({'schema':'raven.bootstrap-stage/v1', 'path':str(temporary), 'pid':os.getpid(), 'created':time.time()}, stream)
    target_identity = None
    complete = False
    try:
        staged = temporary / "checkout"
        try:
            git("clone", "--depth", "1", "--branch", "main", "--single-branch", "--", OFFICIAL, staged)
        except BootstrapError:
            if staged.exists():
                shutil.rmtree(staged)
            git("clone", "--depth", "1", "--branch", "main", "--single-branch", "--", OFFICIAL, staged)
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
        os.close(lock)

previous_handlers = {signum: signal.signal(signum, interrupted)
                     for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT)}
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
    if ((raven_code)); then "$raven_python" -I -c 'import os,sys; os.unlink(sys.argv[1])' "$raven_checkout_file"; return "$raven_code"; fi
    IFS= read -r raven_checkout <"$raven_checkout_file" || raven_checkout=''
    "$raven_python" -I -c 'import os,sys; os.unlink(sys.argv[1])' "$raven_checkout_file"
    [[ -n "$raven_checkout" ]] || return 0
    # Piped download supplies Bash source, not interactive answers. Reconnect
    # only when output is interactive and an actual controlling TTY exists.
    if [[ ! -t 0 && -t 1 ]] && ( : </dev/tty ) 2>/dev/null; then
        exec "$raven_python" -I "$raven_checkout/scripts/install.py" ${raven_args[@]+"${raven_args[@]}"} </dev/tty
    fi
    exec "$raven_python" -I "$raven_checkout/scripts/install.py" ${raven_args[@]+"${raven_args[@]}"}
}

raven_install_main "$@"
