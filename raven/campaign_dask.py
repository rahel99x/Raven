"""One persistent owner for an optional dask-jobqueue SLURMCluster."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import signal
import time

from .campaign_common import atomic, digest, integer, locked, read, text


def validate(recipe):
    if not isinstance(recipe, dict) or recipe.get("schema") != "raven.dask-pool/v1":
        raise ValueError("expected raven.dask-pool/v1")
    maximum = integer(recipe.get("max_jobs"), "max_jobs", 1, 512)
    minimum = integer(recipe.get("min_jobs", 0), "min_jobs", 0, maximum)
    adaptive = recipe.get("adaptive", False)
    if not isinstance(adaptive, bool):
        raise ValueError("adaptive must be a boolean")
    result = {"schema": recipe["schema"], "cores": integer(recipe.get("cores"), "cores", 1, 65536),
              "memory_mb": integer(recipe.get("memory_mb"), "memory_mb", 1, 1 << 30),
              "max_jobs": maximum, "min_jobs": minimum, "adaptive": adaptive,
              "walltime": text(recipe.get("walltime", "01:00:00"), "walltime", 40),
              "job_name": text(recipe.get("job_name", "raven-dask"), "job_name", 80)}
    from .submission import _time
    if not _time(result["walltime"]):
        raise ValueError("invalid pool walltime")
    for key in ("queue", "account", "interface", "local_directory"):
        if recipe.get(key):
            result[key] = text(recipe[key], key)
    return result


def process_identity(pid):
    """Linux start ticks distinguish this controller from a reused PID."""
    try:
        return Path(f"/proc/{int(pid)}/stat").read_text().rsplit(")", 1)[1].split()[19]
    except (OSError, ValueError, IndexError):
        return ""


def alive(status):
    return bool(isinstance(status, dict) and status.get("pid") and status.get("process_start")
                and process_identity(status["pid"]) == status["process_start"]
                and status.get("state") not in {"stopped", "failed"})


def controller(directory, *, cluster_factory=None, clock=time.monotonic, max_cycles=None):
    directory = Path(directory)
    config = read(directory / "config.json")
    recipe = validate(config["recipe"])
    stopped = False
    def stop(*_):
        nonlocal stopped
        stopped = True
    cluster = adaptive = None
    with locked(directory / "controller"):
        handlers = {}
        if __import__("threading").current_thread() is __import__("threading").main_thread():
            for signum in (signal.SIGTERM, signal.SIGINT):
                handlers[signum] = signal.signal(signum, stop)
        state = {"schema": "raven.dask-controller/v1", "owner": config["owner"],
                 "scope": config["scope"], "recipe_digest": digest(recipe), "pid": os.getpid(),
                 "process_start": process_identity(os.getpid()), "state": "starting",
                 "desired_jobs": config["target"], "sequence": 0}
        try:
            atomic(directory / "status.json", state)
            if cluster_factory is None:
                from dask_jobqueue import SLURMCluster
                cluster_factory = SLURMCluster
            kwargs = {key: recipe[key] for key in ("cores", "walltime", "job_name", "queue", "account", "interface", "local_directory") if key in recipe}
            kwargs.update(memory=f"{recipe['memory_mb']}MiB", processes=1)
            cluster = cluster_factory(**kwargs)
            if recipe["adaptive"]:
                adaptive = cluster.adapt(minimum_jobs=recipe["min_jobs"], maximum_jobs=recipe["max_jobs"], interval="2s")
            else:
                cluster.scale(jobs=config["target"])
            cycles = 0
            while not stopped:
                request = read(directory / "request.json", {})
                if request and request.get("owner") == config["owner"] and request.get("sequence", 0) > state["sequence"]:
                    if request.get("action") == "stop":
                        stopped = True
                    elif request.get("action") == "scale":
                        target = integer(request.get("target"), "target", 0, recipe["max_jobs"])
                        if adaptive is not None:
                            adaptive.stop()
                            adaptive = None
                        cluster.scale(jobs=target)
                        state["desired_jobs"] = target
                    else:
                        raise ValueError("invalid controller request")
                    state["sequence"] = request["sequence"]
                if stopped:
                    break
                # One process per Slurm job means connected workers and jobs share units.
                info = cluster.scheduler_info
                connected = len(info.get("workers", {}))
                requested = len(cluster.worker_spec)
                state.update(state="running", adaptive=adaptive is not None, requested_jobs=requested,
                             connected_jobs=connected, queued_or_starting_jobs=max(0, requested - connected),
                             scheduler_address=str(cluster.scheduler_address), heartbeat=time.time())
                atomic(directory / "status.json", state)
                cycles += 1
                if max_cycles is not None and cycles >= max_cycles:
                    stopped = True
                else:
                    time.sleep(0.5)
        except Exception as exc:
            state.update(state="failed", error=str(exc)[:2000], heartbeat=time.time())
            atomic(directory / "status.json", state)
            raise
        finally:
            failures = []
            try:
                if adaptive is not None:
                    try:
                        adaptive.stop()
                    except Exception as exc:
                        failures.append(str(exc))
                if cluster is not None:
                    try:
                        cluster.close(timeout=30)
                    except Exception as exc:
                        failures.append(str(exc))
                if failures:
                    state.update(state="failed", error="controller cleanup failed: " + "; ".join(failures)[:2000], heartbeat=time.time())
                    atomic(directory / "status.json", state)
                    raise ValueError(state["error"])
                if state["state"] != "failed":
                    state.update(state="stopped", heartbeat=time.time())
                    atomic(directory / "status.json", state)
            finally:
                for signum, handler in handlers.items():
                    signal.signal(signum, handler)
    return state


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory")
    args = parser.parse_args(argv)
    controller(args.directory)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
