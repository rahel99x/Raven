"""Low-frequency, background-only Raven self-observation; no input transcripts."""
from __future__ import annotations

import os
from pathlib import Path
import time

INTERVAL = 10.
MAX_PROCESSES = 64
MAX_THREADS = 128


def _read(path, limit=65536):
    with open(path, "rb", buffering=0) as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError("Self-observation file exceeds its bound")
    return data.decode("ascii", "replace")


def process_tree(root=None):
    """Account only descendants, with process/thread enumeration bounds."""
    pending, seen, rows, incomplete = [os.getpid() if root is None else root], set(), [], False
    ticks = os.sysconf("SC_CLK_TCK")
    while pending and len(seen) < MAX_PROCESSES:
        pid = pending.pop()
        if pid in seen:
            continue
        seen.add(pid)
        base = Path("/proc") / str(pid)
        try:
            raw = _read(base / "stat", 8192)
            fields = raw[raw.rfind(")") + 2:].split()
            status = dict(line.split(":", 1) for line in _read(base / "status").splitlines() if ":" in line)
            rss = int(status.get("VmRSS", "0 kB").split()[0]) * 1024
            try:
                pss = next(int(line.split()[1]) * 1024 for line in _read(base / "smaps_rollup").splitlines() if line.startswith("Pss:"))
            except (OSError, ValueError, StopIteration):
                pss = None
            descriptors = 0
            with os.scandir(base / "fd") as entries:
                for _ in entries:
                    descriptors += 1
                    if descriptors >= 8192:
                        incomplete = True
                        break
            threads = int(status.get("Threads", "0").strip())
            rows.append({"pid": pid, "start_ticks": int(fields[19]), "rss_bytes": rss, "pss_bytes": pss,
                         "cpu_seconds": (int(fields[11]) + int(fields[12])) / ticks,
                         "threads": threads, "descriptors": descriptors})
            with os.scandir(base / "task") as entries:
                for index, entry in enumerate(entries):
                    if index >= MAX_THREADS:
                        incomplete = True
                        break
                    try:
                        children = _read(Path(entry.path) / "children", 8192).split()
                        for value in children:
                            if not value.isdigit():
                                continue
                            child = int(value)
                            if child in seen or child in pending:
                                continue
                            if len(pending) + len(seen) >= MAX_PROCESSES:
                                incomplete = True
                                break
                            pending.append(child)
                    except (OSError, ValueError):
                        continue
        except (OSError, ValueError, IndexError):
            incomplete = True
    incomplete = incomplete or bool(pending)
    return {"scope": "local UI descendants; remote compute is reported by its worker", "complete": not incomplete,
            "processes": rows, "rss_sum_bytes": sum(row["rss_bytes"] for row in rows),
            "pss_sum_bytes": sum(row["pss_bytes"] or 0 for row in rows),
            "pss_complete": bool(rows) and all(row["pss_bytes"] is not None for row in rows),
            "threads": sum(row["threads"] for row in rows), "descriptors": sum(row["descriptors"] for row in rows)}


def collect(app):
    now = time.time()
    backend = getattr(getattr(app, "sampler", None), "slurm", None)
    backend = getattr(backend, "b", None)
    ledger = getattr(backend, "ledger", None)
    collection = ledger.report() if ledger is not None else {"scope": "unavailable"}
    sources = []
    sampler = getattr(app, "sampler", None)
    if sampler is not None:
        for name in tuple(sampler.sources)[:64]:
            health = sampler.health(name)
            sources.append({"source": name, "age_seconds": max(0., now - health.last_ok) if health.last_ok else None,
                            "inflight": bool(health.inflight), "interval_seconds": sampler.effective_interval(name)})
    scheduler = getattr(app, "worker_scheduler", None)
    return {"schema": "raven.self-observation/v1", "observed_at": now, "period_seconds": INTERVAL,
            "process_tree": process_tree(), "collection": collection, "sources": sources,
            "queues": scheduler.status() if scheduler is not None else {},
            "policy": app.cfg.get("raven", {}).get("resources", {}).get("light", {})}


def bind(app):
    scheduler = getattr(app, "worker_scheduler", None)
    if scheduler is None or not app.cfg.get("raven", {}).get("managed"):
        return
    app.raven_diagnostic_state = {"future": None, "next": 0., "value": {}, "closed": False,
                                  "lane": scheduler.lane("source")}


def snapshot(app):
    """Painting reads a published value; it never starts work."""
    return getattr(app, "raven_diagnostic_state", {}).get("value", {})


def tick(app):
    state = getattr(app, "raven_diagnostic_state", None)
    if not state or state["closed"]:
        return False
    changed = False
    future = state["future"]
    if future is not None and future.done():
        try:
            state["value"] = future.result()
        except Exception as exc:
            state["value"] = {"schema": "raven.self-observation/v1", "unavailable": str(exc)[:240], "observed_at": time.time()}
        state["future"] = None
        changed = True
    now = time.monotonic()
    if state["future"] is None and now >= state["next"]:
        state["next"] = now + INTERVAL
        try:
            state["future"] = state["lane"].submit(collect, app)
        except RuntimeError:
            pass  # A busy/closing queue cannot turn self-observation into load.
    return changed


def close(app):
    state = getattr(app, "raven_diagnostic_state", None)
    if state and not state["closed"]:
        state["closed"] = True
        state["lane"].shutdown(wait=False, cancel_futures=True)
