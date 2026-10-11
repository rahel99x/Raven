"""Opt-in session hook for governed, shared read-only scheduler observations.

Call wrap_backend once when constructing a connection, before recording wrappers
if cached observations should be recorded. No configured policy means no change
in existing core behavior. No filesystem reads occur while rendering.
"""
from __future__ import annotations

import os
from pathlib import Path
import threading
import time

from . import evidence as E, site_policy as P
from .shared_collection import SharedQueryCache
from .slurm import CommandError, FakeBackend


def _read(path):
    from .science_files import snapshot
    _, raw = snapshot(str(Path(path).expanduser().absolute()), limit=4 << 20, keep=True)
    return E.loads(raw)


def read_operation(argv):
    """Classify only a conservative native read subset; unknown commands bypass."""
    if not isinstance(argv, (tuple, list)) or not argv or any(not isinstance(item, str) for item in argv):
        return None
    command = os.path.basename(argv[0])
    if command in {"squeue", "sacct", "sinfo", "sstat"}:
        return {"squeue": "queue", "sacct": "accounting", "sinfo": "nodes", "sstat": "statistics"}[command]
    lower = [item.lower() for item in argv[1:]]
    if command == "scontrol" and lower and lower[0] in {"show", "listpids", "ping", "version"}:
        return "configuration" if lower[:2] == ["show", "config"] else "job-detail"
    if command == "scontrol" and lower in (["--version"], ["-v"]):
        return "configuration"
    return None


class GovernedBackend:
    def __init__(self, backend, policy, cache, scope, *, interval=None):
        self.inner = backend
        self.policy = P.validate(policy)
        self.cache = cache
        self.scope = {"connection": E.digest(scope), "generation": "native-slurm-cli/v1", "user": str(scope.get("user") or os.getuid()),
                      "cluster": str(scope.get("cluster") or "configured-default")}
        self.interval = self.policy["limits"]["min_interval"] if interval is None else E.finite(interval, "collection interval", .001)
        self.capabilities = P.capabilities({"schema": P.CAPABILITIES, "source": "slurm-cli", "version": "tower-native-read-subset/v1",
                "operations": ["queue", "accounting", "nodes", "statistics", "configuration", "job-detail"],
                "min_interval": .5, "status": "available",
                "reason": "Tower backend supports these read commands; controller connectivity and site permissions remain command results."})
        self._last = threading.local()

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def _execute(self, cmd, timeout, limit, cancel):
        from .operations import _backend_command
        if cancel is not None and cancel.is_set():
            raise CommandError("Operation cancelled before collection")
        raw = _backend_command(self.inner, list(cmd), timeout, limit, cancel)
        if not isinstance(raw, str):
            raise CommandError("Scheduler backend returned non-text output")
        if len(raw) > limit or len(raw.encode("utf-8")) > limit:
            raise CommandError("Command output exceeded the inspection limit")
        if cancel is not None and cancel.is_set():
            raise CommandError("Operation cancelled during collection")
        return raw

    def _query(self, cmd, timeout, limit, cancel, *, fresh):
        operation = read_operation(cmd)
        if operation is None:
            try:
                return self._execute(cmd, timeout, limit, cancel)
            finally:
                # Explicit operations can mutate through run rather than
                # call. Reconcile cached scheduler data even on uncertain
                # command outcomes. Routine GPU/non-Slurm sampling does not
                # invalidate unrelated in-flight scheduler reads.
                if fresh or os.path.basename(cmd[0]) in {"sbatch", "scancel", "salloc", "scontrol"}:
                    self.cache.invalidate(self.scope)
        E.finite(timeout, "scheduler timeout", .001)
        limits = self.policy["limits"]
        duration = min(timeout + 1., limits["max_duration"])
        if duration <= .1:
            raise CommandError("Site collection duration is too short for a scheduler read")
        request = {"source": "slurm-cli", "operation": operation, "interval": max(self.interval, .5), "duration": duration,
                   "max_bytes": min(limits["max_bytes"], 4 << 20), "max_records": limits["max_records"],
                   "allocation": None, "intrusive": False}
        query = {"request": request, "parameters": {"argv": list(cmd), "timeout": timeout}}
        def load(stop):
            # Reuse native bounded local/SSH command handling. This runs on the
            # existing sampler/operation worker, never in an extra executor.
            raw = self._execute(cmd, max(.01, duration - .1), min(limit, request["max_bytes"]), stop)
            if len(raw.splitlines()) > request["max_records"]:
                raise ValueError("Scheduler read exceeded the site record budget")
            # Evidence strings are individually bounded to 64 KiB. Preserve
            # normal large queue/accounting responses as bounded chunks;
            # cache admission still checks their total serialized byte size.
            return {"stdout_chunks": [raw[i:i + 32768] for i in range(0, len(raw), 32768)]}
        try:
            result = self.cache.query(self.scope, query, load, self.policy, self.capabilities,
                                      read_only=True, cancel=cancel, fresh=fresh)
        except (ValueError, TimeoutError, OSError) as exc:
            raise CommandError("Governed collection: " + str(exc)) from exc
        self._last.receipt = {key: value for key, value in result.items() if key != "value"}
        raw = "".join(result["value"]["stdout_chunks"])
        if len(raw) > limit or len(raw.encode("utf-8")) > limit:
            raise CommandError("Command output exceeded the inspection limit")
        return raw

    def run(self, cmd, timeout=8.0):
        start = time.monotonic()
        return self._query(cmd, timeout, 4 << 20, None, fresh=False), time.monotonic() - start

    def command(self, cmd, timeout, limit, cancel):
        """Bounded exact-review path; retain cancellation and cache admission."""
        return self._query(cmd, timeout, limit, cancel, fresh=True)

    def call(self, cmd, timeout=15.0):
        # Mutations always execute once through the actual connection and
        # invalidate cached observations, even if the action reports failure.
        try:
            return self.inner.call(cmd, timeout)
        finally:
            self.cache.invalidate(self.scope)

    def invalidate(self):
        self.cache.invalidate(self.scope)


def wrap_backend(backend, cfg, scope, state_dir):
    """Return an opt-in governed backend, or the original object unchanged.

    cfg.site_policy is a local JSON path; cfg.collection_cache_dir optionally
    selects a private host-local cache directory. No network setup or packages
    are installed. An invalid explicitly configured policy fails visibly.
    """
    if not cfg.get("site_policy"):
        return backend
    current, seen = backend, set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, FakeBackend):
            return backend
        current = getattr(current, "inner", None)
    if isinstance(backend, GovernedBackend):
        return backend
    if not isinstance(scope, dict) or not scope:
        raise ValueError("Governed collection requires explicit connection identity")
    policy = P.validate(_read(cfg["site_policy"]))
    if not state_dir and not cfg.get("collection_cache_dir"):
        raise ValueError("Governed collection requires a private host-local cache directory")
    from .shared_collection import _host
    directory = cfg.get("collection_cache_dir") or str(Path(state_dir) / "collection-cache" / _host()[:24])
    cache = SharedQueryCache(directory, max_bytes=min(64 << 20, max(policy["limits"]["max_bytes"], 16 << 20)))
    return GovernedBackend(backend, policy, cache, scope, interval=cfg.get("collection_interval"))
