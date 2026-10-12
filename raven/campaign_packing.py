"""Bounded logical-task executor. Can be copied and run without Raven installed."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import time


def validate(recipe):
    if not isinstance(recipe, dict) or recipe.get("schema") != "raven.packed-tasks/v1":
        raise ValueError("expected raven.packed-tasks/v1")
    def number(value, name, maximum=1000000):
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
            raise ValueError(f"{name} must be a positive bounded integer")
        return value
    cpus = number(recipe.get("cpus"), "cpus", 65536)
    memory = number(recipe.get("memory_mb"), "memory_mb", 1 << 30)
    parallel = number(recipe.get("parallel", cpus), "parallel", 256)
    tasks = recipe.get("tasks")
    if not isinstance(tasks, list) or not 1 <= len(tasks) <= 4096:
        raise ValueError("packed run needs 1..4096 tasks")
    ids, normalized = set(), []
    import re
    for task in tasks:
        if not isinstance(task, dict):
            raise ValueError("each packed task must be an object")
        task_id = task.get("id")
        if not isinstance(task_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", task_id) or task_id in ids:
            raise ValueError("packed task IDs must be unique safe names")
        ids.add(task_id)
        argv = task.get("argv")
        if not isinstance(argv, list) or not 1 <= len(argv) <= 256 or any(not isinstance(a, str) or not a or len(a) > 8192 or "\0" in a for a in argv):
            raise ValueError("task argv must be a bounded nonempty argument list")
        if argv[0].startswith("-"):
            raise ValueError("task executable cannot begin with a dash")
        if ":" in argv:
            raise ValueError("task arguments cannot contain standalone Slurm colon separators")
        cwd = task.get("cwd", recipe.get("workdir", "."))
        if not isinstance(cwd, str) or not cwd or len(cwd) > 4096 or "\0" in cwd:
            raise ValueError("invalid task working directory")
        task_cpus = number(task.get("cpus", 1), "task cpus", cpus)
        task_memory = number(task.get("memory_mb"), "task memory_mb", memory)
        timeout = number(task.get("timeout_s", 86400), "timeout_s", 604800)
        normalized.append({"id": task_id, "argv": argv, "cwd": str(Path(cwd).absolute()),
                           "cpus": task_cpus, "memory_mb": task_memory, "timeout_s": timeout})
    return {"schema": "raven.packed-tasks/v1", "cpus": cpus, "memory_mb": memory,
            "parallel": parallel, "tasks": normalized}


def _write(path, value):
    temp = path.with_name("." + path.name + "." + os.urandom(8).hex())
    try:
        with open(temp, "x", encoding="utf-8") as stream:
            os.chmod(temp, 0o600)
            json.dump(value, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def _read(path):
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(fd, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > 4 << 20:
            raise ValueError("runner state must be a regular file within 4 MiB")
        data = stream.read((4 << 20) + 1)
        after = os.fstat(stream.fileno())
        named = os.stat(path, follow_symlinks=False)
        signature = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
        if signature(before) != signature(after) or signature(after) != signature(named) or len(data) > 4 << 20:
            raise ValueError("runner state changed during read")
    def unique(pairs):
        obj = {}
        for key, value in pairs:
            if key in obj:
                raise ValueError("duplicate state key")
            obj[key] = value
        return obj
    def finite(_):
        raise ValueError("nonfinite runner state")
    return json.loads(data, object_pairs_hook=unique, parse_constant=finite)


def execute(recipe, output, *, local=False, cancel=None, tick=0.05):
    """Run each task once; unknown/failed/completed receipts all block replay."""
    recipe = validate(recipe)
    if not local and not os.environ.get("SLURM_JOB_ID"):
        raise ValueError("packed runner must run inside Slurm; --local is an explicit test mode")
    output = Path(output).absolute()
    if any(p.is_symlink() for p in (output, *output.parents)):
        raise ValueError("packed output cannot traverse symbolic links")
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    if output.stat().st_uid != os.getuid() or output.stat().st_mode & 0o022:
        raise ValueError("packed output must be owned by this user and not writable by other users")
    import fcntl
    lock = os.open(output / ".lock", os.O_CREAT | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600)
    active, completed, cancelled = {}, {}, False
    try:
        if not stat.S_ISREG(os.fstat(lock).st_mode):
            raise ValueError("packed lock must be a regular file")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("another runner owns this packed output directory") from exc
        identity = output / "manifest.json"
        if identity.exists():
            if _read(identity) != recipe:
                raise ValueError("output directory belongs to a different packed manifest")
        else:
            if any(p.name != ".lock" for p in output.iterdir()):
                raise ValueError("new packed output directory must be empty")
            _write(identity, recipe)
        receipts = output / "receipts"
        logs = output / "logs"
        for child in (receipts, logs):
            if child.is_symlink():
                raise ValueError("packed output subdirectories cannot be symbolic links")
            child.mkdir(mode=0o700, exist_ok=True)
        pending = []
        for task in recipe["tasks"]:
            path = receipts / (task["id"] + ".json")
            if path.exists():
                completed[task["id"]] = _read(path)
                if not isinstance(completed[task["id"]], dict) or completed[task["id"]].get("id") != task["id"]:
                    raise ValueError("packed receipt identity mismatch")
            else:
                pending.append(task)
        while pending or active:
            if cancel is not None and cancel.is_set():
                cancelled = True
            free_cpus = recipe["cpus"] - sum(item["task"]["cpus"] for item in active.values())
            free_memory = recipe["memory_mb"] - sum(item["task"]["memory_mb"] for item in active.values())
            for task in list(pending):
                if cancelled or len(active) >= recipe["parallel"]:
                    break
                if task["cpus"] > free_cpus or task["memory_mb"] > free_memory:
                    continue
                path = receipts / (task["id"] + ".json")
                receipt = {"id": task["id"], "state": "launching", "started": time.time(), "argv": task["argv"]}
                _write(path, receipt)  # Commit intent before spawning, including crash recovery.
                handles = []
                try:
                    for suffix in (".out", ".err"):
                        fd = os.open(logs / (task["id"] + suffix), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                        handles.append(os.fdopen(fd, "wb"))
                    argv = task["argv"] if local else ["srun", "--exclusive", "--exact", "--nodes=1", "--ntasks=1",
                            f"--cpus-per-task={task['cpus']}", f"--mem={task['memory_mb']}M", "--", *task["argv"]]
                    environment = dict(os.environ, OMP_NUM_THREADS=str(task["cpus"]), RAVEN_TASK_ID=task["id"])
                    proc = subprocess.Popen(argv, cwd=task["cwd"], env=environment, stdin=subprocess.DEVNULL,
                                            stdout=handles[0], stderr=handles[1], start_new_session=True)
                    active[task["id"]] = {"task": task, "process": proc, "receipt": receipt, "start": time.monotonic(), "term": None}
                    free_cpus -= task["cpus"]
                    free_memory -= task["memory_mb"]
                except (OSError, ValueError) as exc:
                    receipt.update(state="launch_failed", error=str(exc), finished=time.time())
                    _write(path, receipt)
                    completed[task["id"]] = receipt
                finally:
                    for stream in handles:
                        stream.close()
                pending.remove(task)
            for task_id, item in list(active.items()):
                proc, elapsed = item["process"], time.monotonic() - item["start"]
                if (cancelled or elapsed >= item["task"]["timeout_s"]) and item["term"] is None:
                    try:
                        os.killpg(proc.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    item["term"] = time.monotonic()
                    item["receipt"]["reason"] = "cancelled" if cancelled else "timeout"
                if item["term"] is not None and time.monotonic() - item["term"] >= 2:
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                status = proc.poll()
                if status is not None:
                    # Daemon children are not logical tasks; never let them escape accounting.
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    receipt = item["receipt"]
                    receipt.update(state=receipt.get("reason", "completed" if status == 0 else "failed"),
                                   returncode=status, finished=time.time())
                    _write(receipts / (task_id + ".json"), receipt)
                    completed[task_id] = receipt
                    del active[task_id]
            if cancelled:
                for task in pending:
                    receipt = {"id": task["id"], "state": "cancelled", "finished": time.time()}
                    _write(receipts / (task["id"] + ".json"), receipt)
                    completed[task["id"]] = receipt
                pending.clear()
            if pending or active:
                time.sleep(max(0.005, min(0.25, tick)))
        return {"schema": "raven.packed-results/v1", "tasks": completed,
                "ok": all(row.get("state") == "completed" for row in completed.values())}
    finally:
        for item in active.values():
            try:
                os.killpg(item["process"].pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            item["process"].wait(timeout=5)
        os.close(lock)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest")
    parser.add_argument("output")
    parser.add_argument("--local", action="store_true", help="explicit test mode without Slurm resource isolation")
    args = parser.parse_args(argv)
    import threading
    cancel = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: cancel.set())
    result = execute(_read(args.manifest), args.output, local=args.local, cancel=cancel)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
