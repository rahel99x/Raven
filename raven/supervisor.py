"""Explicit, reconnectable, single-writer Slurm monitor.

Run ``python -m raven.supervisor --help``. This service never submits, resumes,
cancels, or otherwise changes scheduler jobs. No service starts on installation.
"""
from __future__ import annotations

import argparse
import fcntl
import getpass
import json
import math
import os
from pathlib import Path
import secrets
import signal
import stat
import subprocess
import sys
import threading
import time

from .services_io import bounded_command, private_directory, read_json_file, write_json_file

SCHEMA = "raven.supervisor/v1"
_CHILDREN = {}
_CHILDREN_LOCK = threading.Lock()
_EXIT_REAP_SECONDS = .5


def configuration(*, user=None, cluster="", interval=5, max_records=1000):
    user = user or getpass.getuser()
    if not isinstance(user, str) or not user or len(user) > 256 or not user.isprintable() or user.startswith("-"):
        raise ValueError("Invalid Slurm user")
    if not isinstance(cluster, str) or len(cluster) > 256 or (cluster and (not cluster.isprintable() or cluster.startswith("-"))):
        raise ValueError("Invalid cluster")
    if isinstance(interval, bool):
        raise ValueError("Monitoring interval must be a number")
    interval = float(interval)
    if not math.isfinite(interval) or not 1 <= interval <= 300:
        raise ValueError("Monitoring interval must be between 1 and 300 seconds")
    if isinstance(max_records, bool) or str(max_records) != str(int(max_records)) or not 1 <= int(max_records) <= 10000:
        raise ValueError("Maximum records must be an integer between 1 and 10000")
    return {"schema": SCHEMA, "user": user, "cluster": cluster,
            "interval": interval, "max_records": int(max_records)}


def _lock(directory):
    fd = os.open(directory / "writer.lock", os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
        os.close(fd)
        raise ValueError("Invalid supervisor lock")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        raise RuntimeError("A supervisor already owns this directory") from None
    return fd


def _identity(pid):
    """Linux start ticks distinguish a recycled PID; token/lock are also required."""
    try:
        data = Path(f"/proc/{int(pid)}/stat").read_text()
        return data[data.rfind(")") + 2:].split()[19]
    except (OSError, ValueError, IndexError):
        return None


def _read_optional(path):
    try:
        value = read_json_file(path)
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _reap_children():
    """Collect exited direct children even if a new owner replaced their file."""
    with _CHILDREN_LOCK:
        children = tuple(_CHILDREN.items())
    for pid, child in children:
        if child.poll() is not None:
            with _CHILDREN_LOCK:
                if _CHILDREN.get(pid) is child:
                    _CHILDREN.pop(pid, None)


def _remember_child(child):
    with _CHILDREN_LOCK:
        _CHILDREN[child.pid] = child


def _reap_stopped(owner):
    """A released service lock can precede process exit; wait only on our child.

    No signal is sent. A slow exit remains retained for later reconciliation,
    and persistent services that still own their lock never reach this path.
    """
    pid = owner.get("pid")
    with _CHILDREN_LOCK:
        child = _CHILDREN.get(pid)
    if child is None:
        return
    try:
        child.wait(timeout=_EXIT_REAP_SECONDS)
    except subprocess.TimeoutExpired:
        return
    with _CHILDREN_LOCK:
        if _CHILDREN.get(pid) is child:
            _CHILDREN.pop(pid, None)


def status(directory):
    _reap_children()
    directory = Path(directory).expanduser().absolute()
    if not directory.exists():
        return {"schema": SCHEMA, "state": "absent", "running": False}
    private_directory(directory)
    snapshot = _read_optional(directory / "snapshot.json")
    owner = _read_optional(directory / "owner.json")
    occupied = False
    try:
        fd = _lock(directory)
    except RuntimeError:
        occupied = True
    else:
        os.close(fd)
    same = bool(owner.get("start_ticks") and _identity(owner.get("pid", 0)) == owner["start_ticks"])
    running = occupied and same and bool(owner.get("token"))
    return {**snapshot, "schema": SCHEMA, "owner": owner,
            "state": snapshot.get("state", "starting") if running else ("stopped" if owner else "absent"),
            "running": running, "stale": bool(snapshot) and not running}


def collect(config, *, runner=bounded_command, cancel=None):
    """Bounded account-scoped observations; an unavailable source stays unknown."""
    common = ["-M", config["cluster"]] if config["cluster"] else []
    sources = (
        ("active", ["squeue", "--user", config["user"], "--noheader", "--format=%i|%T|%j|%V|%S|%R", *common],
         ("id", "state", "name", "submit", "start", "reason")),
        ("finished", ["sacct", "-X", "-u", config["user"], "-n", "-P", "-S", "now-24hours",
                      "-o", "JobIDRaw,State,JobName,Submit,Start,End,ExitCode", *common],
         ("id", "state", "name", "submit", "start", "end", "exit_code")),
    )
    result = {"active": [], "finished": [], "errors": [], "truncated": [], "time": time.time()}
    for name, argv, fields in sources:
        try:
            text = runner(argv, timeout=8, limit=2 * 1024 * 1024, cancel=cancel)
            rows = []
            for line in text.splitlines():
                parts = line.rstrip("|").split("|")
                if not line.strip():
                    continue
                if len(parts) != len(fields):
                    raise ValueError(f"Malformed {name} record")
                row = dict(zip(fields, (part.strip()[:1024] for part in parts)))
                row["cluster"] = config["cluster"]
                if not row["id"]:
                    raise ValueError("Missing job identity")
                rows.append(row)
            if len(rows) > config["max_records"]:
                result["truncated"].append(name)
            result[name] = rows[-config["max_records"]:]
        except InterruptedError:
            raise
        except (OSError, RuntimeError, ValueError, TimeoutError) as exc:
            result["errors"].append({"source": name, "reason": str(exc)[:600]})
    return result


def reconcile(previous, current, config):
    def key(row):
        return tuple(str(row.get(field, "")) for field in ("cluster", "id", "submit", "start"))
    old = {key(row): row for row in previous.get("finished", []) + previous.get("active", [])}
    events = list(previous.get("events", []))[-200:]
    active_keys = {key(row) for row in current["active"]}
    # Queue evidence wins over delayed accounting for the exact same attempt.
    observations = current["active"] + [row for row in current["finished"] if key(row) not in active_keys]
    for row in observations:
        prior = old.get(key(row))
        if prior is None or prior.get("state") != row["state"]:
            events.append({"time": current["time"], "cluster": row["cluster"], "id": row["id"],
                           "submit": row["submit"], "before": prior.get("state") if prior else None,
                           "start": row.get("start", ""), "after": row["state"]})
    # Failed reads cannot make all jobs disappear; retain the previous snapshot
    # with an explicit stale source and no invented completion transition.
    for error in current["errors"]:
        current[error["source"]] = previous.get(error["source"], [])[:config["max_records"]]
    return {**current, "events": events[-200:], "schema": SCHEMA, "state": "running",
            "config": config, "generation": int(previous.get("generation", 0)) + 1}


def serve(directory, config, *, token=None, cancel=None, max_polls=None, runner=bounded_command, lock_fd=None):
    directory = private_directory(directory)
    config = configuration(user=config.get("user"), cluster=config.get("cluster", ""),
                           interval=config.get("interval", 5), max_records=config.get("max_records", 1000))
    if lock_fd is None:
        fd = _lock(directory)
    else:
        fd = lock_fd
        info = os.fstat(fd)
        named = os.stat(directory / "writer.lock", follow_symlinks=False)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or
                (info.st_dev, info.st_ino) != (named.st_dev, named.st_ino)):
            os.close(fd)
            raise ValueError("Invalid inherited supervisor lock")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    token = token or secrets.token_hex(24)
    cancel = cancel or threading.Event()
    owner = {"pid": os.getpid(), "start_ticks": _identity(os.getpid()), "token": token}
    previous = _read_optional(directory / "snapshot.json")
    # Never reconcile across users or clusters just because a directory is reused.
    if any(previous.get("config", {}).get(k) != config[k] for k in ("user", "cluster")):
        previous = {}
    try:
        write_json_file(directory / "config.json", config)
        write_json_file(directory / "owner.json", owner)
        write_json_file(directory / "snapshot.json", {**previous, "schema": SCHEMA, "state": "starting", "config": config,
                                                     "owner_token": token})
        polls = 0
        while not cancel.is_set():
            if _read_optional(directory / "stop.json").get("token") == token:
                break
            started = time.monotonic()
            try:
                current = collect(config, runner=runner, cancel=cancel)
            except InterruptedError:
                if cancel.is_set():
                    break
                raise
            previous = reconcile(previous, current, config)
            previous["owner_token"] = token
            write_json_file(directory / "snapshot.json", previous)
            polls += 1
            if max_polls is not None and polls >= max_polls:
                break
            while time.monotonic() - started < config["interval"] and not cancel.wait(0.1):
                if _read_optional(directory / "stop.json").get("token") == token:
                    cancel.set()
                    break
    finally:
        try:
            write_json_file(directory / "snapshot.json", {**previous, "schema": SCHEMA,
                                                         "state": "stopped", "stopped_at": time.time()})
        finally:
            os.close(fd)
    return previous


def start(directory, config, *, timeout=5):
    if _identity(os.getpid()) is None:
        raise ValueError("Persistent monitoring requires Linux /proc process start identity")
    directory = private_directory(directory)
    config = configuration(user=config.get("user"), cluster=config.get("cluster", ""),
                           interval=config.get("interval", 5), max_records=config.get("max_records", 1000))
    if status(directory)["running"]:
        raise RuntimeError("Supervisor is already running")
    token = secrets.token_hex(24)
    # Transfer the already-owned open lock description to the child. A status
    # probe cannot briefly acquire it between launch and the child acquiring it.
    lock_fd = _lock(directory)
    argv = [sys.executable, "-m", "raven.supervisor", "serve", str(directory), "--token", token,
            "--user", config["user"], "--cluster", config["cluster"],
            "--interval", str(config["interval"]), "--max-records", str(config["max_records"]),
            "--lock-fd", str(lock_fd)]
    try:
        process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True,
                                   pass_fds=(lock_fd,))
        _remember_child(process)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            view = status(directory)
            if (view.get("running") and view.get("owner", {}).get("token") == token
                    and view.get("owner_token") == token and view.get("state") in ("starting", "running")):
                return view
            if process.poll() is not None:
                raise RuntimeError("Supervisor could not acquire ownership or start; run its serve command for diagnostics")
            time.sleep(0.02)
        # A timed-out launch gets a token-bound stop request, never a recycled PID kill.
        write_json_file(directory / "stop.json", {"token": token, "time": time.time()})
        raise TimeoutError("Supervisor startup timed out; requested its stop")
    finally:
        os.close(lock_fd)


def stop(directory, *, expected_token=None):
    directory = private_directory(directory)
    view = status(directory)
    if not view["running"]:
        _reap_stopped(view.get("owner", {}))
        return view
    token = view["owner"]["token"]
    if expected_token is not None and token != expected_token:
        raise ValueError("Supervisor changed after review; inspect it again")
    write_json_file(directory / "stop.json", {"token": token, "time": time.time()})
    return {**view, "state": "stop requested"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("start", "status", "stop", "serve"))
    parser.add_argument("directory", help="private state directory (mode 0700)")
    parser.add_argument("--user", default=getpass.getuser())
    parser.add_argument("--cluster", default="")
    parser.add_argument("--interval", default="5")
    parser.add_argument("--max-records", default="1000")
    parser.add_argument("--token", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--lock-fd", default=None, type=int, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        if args.action == "status":
            result = status(args.directory)
        elif args.action == "stop":
            result = stop(args.directory)
        else:
            config = configuration(user=args.user, cluster=args.cluster, interval=args.interval, max_records=args.max_records)
            if args.action == "serve":
                cancel = threading.Event()
                previous = {}
                if threading.current_thread() is threading.main_thread():
                    for signum in (signal.SIGTERM, signal.SIGINT):
                        previous[signum] = signal.signal(signum, lambda _number, _frame: cancel.set())
                try:
                    result = serve(args.directory, config, token=args.token, cancel=cancel, lock_fd=args.lock_fd)
                finally:
                    for signum, handler in previous.items():
                        signal.signal(signum, handler)
            else:
                result = start(args.directory, config)
        print(json.dumps(result, sort_keys=True, allow_nan=False))
        return 0
    except (OSError, ValueError, RuntimeError, TimeoutError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
