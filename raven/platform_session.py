"""Opt-in session hook for governed, shared read-only scheduler observations.

Call wrap_backend once when constructing a connection, before recording wrappers
if cached observations should be recorded. No configured policy means no change
in existing core behavior. No filesystem reads occur while rendering.
"""
from __future__ import annotations

from contextlib import contextmanager

import os
from pathlib import Path
import shlex
import threading
import time

from . import evidence as E, site_policy as P
from .shared_collection import ProcessQueryCache, SharedQueryCache
from .read_acquisition import for_cache
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


def _response_records(cmd, raw):
    """Count native job/step records, not the lines used to format their JSON."""
    # Match only the same exact working-directory wrapper admitted by the
    # command classifier. Never interpret arbitrary shell programs as argv.
    while os.path.basename(cmd[0]) == "sh" and len(cmd) == 3 and cmd[1] == "-c":
        try:
            words = shlex.split(cmd[2])
        except ValueError:
            break
        if (len(words) < 4 or words[0] != "cd" or words[2] != "&&"
                or cmd[2] != "cd " + shlex.quote(words[1]) + " && " + shlex.join(words[3:])):
            break
        cmd = words[3:]
    name = os.path.basename(cmd[0])
    if (name in {"squeue", "sacct", "scontrol"}
            and any(arg == "--json" or arg.startswith("--json=") for arg in cmd[1:])):
        from .slurm_json import job_record_count
        try:
            return name, job_record_count(raw)
        except ValueError as exc:
            raise ValueError(f"{name}: invalid bounded scheduler JSON: {exc}") from exc
    return name, len(raw.splitlines())


class GovernedBackend:
    def __init__(self, backend, policy, cache, scope, *, interval=None, collection=None, owns_cache=False):
        self.inner = backend
        self.policy = P.validate(policy)
        self.cache = cache
        self._owns_cache = owns_cache
        self.acquisition = for_cache(cache)
        # v1 admitted compact JSON by physical line count. Do not reuse those
        # responses under semantic job/step record admission.
        self.scope = {"connection": E.digest(scope), "generation": "native-slurm-cli/v2", "user": str(scope.get("user") or os.getuid()),
                      "cluster": str(scope.get("cluster") or "configured-default")}
        self.interval = self.policy["limits"]["min_interval"] if interval is None else E.finite(interval, "collection interval", .001)
        self.capabilities = P.capabilities({"schema": P.CAPABILITIES, "source": "slurm-cli", "version": "raven-native-read-subset/v1",
                "operations": ["queue", "accounting", "nodes", "statistics", "configuration", "job-detail"],
                "min_interval": .5, "status": "available",
                "reason": "Raven backend supports these read commands; controller connectivity and site permissions remain command results."})
        self._last = threading.local()
        self.collection = collection
        self.ledger = None
        if collection is not None:
            from raven.collection.ledger import Ledger
            self.ledger = Ledger(cache, scope, collection)
            self.capabilities = P.capabilities({"schema": P.CAPABILITIES, "source": "slurm-cli",
                "version": "raven-managed-cli/v1", "operations": list(self.policy["operations"]),
                "min_interval": .001, "status": "available"})

    def __getattr__(self, name):
        return getattr(self.inner, name)

    @contextmanager
    def snapshot_inspection(self):
        """An explicit one-shot selected detail uses the inspection allowance."""
        previous = getattr(self._last, "snapshot_inspection", False)
        self._last.snapshot_inspection = True
        try:
            yield
        finally:
            self._last.snapshot_inspection = previous

    def _execute(self, cmd, timeout, limit, cancel):
        from .bounded_command import execute as _backend_command
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
        self._last.coverage = None
        descriptor = None
        if self.ledger is not None:
            from raven.collection.ledger import classify
            descriptor = classify(cmd)
            if descriptor is None:
                raise CommandError("Raven collection: unclassified generated command is unavailable")
            if not self.ledger.config.get("slurm_enabled", True) and descriptor.endpoint != "local":
                raise CommandError("Raven collection: Slurm is unavailable in this site profile")
            if descriptor.family == "job-detail" and descriptor.read_only and getattr(self._last, "snapshot_inspection", False):
                fresh = True
            if not descriptor.read_only:
                if not fresh:
                    raise CommandError("Raven collection: generated step/probe/mutation requires an explicit reviewed action")
                try:
                    with self.ledger.admit(descriptor, cmd, validation=True):
                        return self._execute(cmd, timeout, limit, cancel)
                except RuntimeError as exc:
                    from raven.collection.ledger import Deferred
                    if isinstance(exc, Deferred):
                        raise
                    raise CommandError(str(exc)) from exc
                finally:
                    self.acquisition.invalidate(self.scope)
        operation = read_operation(cmd)
        if descriptor is not None:
            operation = descriptor.family
        if operation is None:
            try:
                return self._execute(cmd, timeout, limit, cancel)
            finally:
                # Explicit operations can mutate through run rather than
                # call. Reconcile cached scheduler data even on uncertain
                # command outcomes. Routine GPU/non-Slurm sampling does not
                # invalidate unrelated in-flight scheduler reads.
                if fresh or os.path.basename(cmd[0]) in {"sbatch", "scancel", "salloc", "scontrol"}:
                    self.acquisition.invalidate(self.scope)
        E.finite(timeout, "scheduler timeout", .001)
        limits = self.policy["limits"]
        duration = min(timeout + 1., limits["max_duration"])
        if duration <= .1:
            raise CommandError("Site collection duration is too short for a scheduler read")
        requested_interval = max(self.interval, .5)
        if descriptor is not None:
            from raven.collection.ledger import interval as collection_interval
            requested_interval = .001 if fresh else collection_interval(self.ledger.config, descriptor.source, 0)
            if descriptor.endpoint == "local":
                requested_interval = 900.
        request = {"source": "slurm-cli", "operation": operation, "interval": max(requested_interval, limits["min_interval"]), "duration": duration,
                   "max_bytes": min(limits["max_bytes"], 4 << 20), "max_records": limits["max_records"],
                   "allocation": None, "intrusive": False}
        query = {"request": request, "parameters": {"argv": list(cmd), "timeout": timeout, "output_limit": limit}}
        def load(stop):
            # Reuse native bounded local/SSH command handling. This runs on the
            # existing sampler/operation worker, never in an extra executor.
            if self.ledger is not None:
                with self.ledger.admit(descriptor, cmd, validation=fresh):
                    raw = self._execute(cmd, max(.01, duration - .1), min(limit, request["max_bytes"]), stop)
            else:
                raw = self._execute(cmd, max(.01, duration - .1), min(limit, request["max_bytes"]), stop)
            source, count = _response_records(cmd, raw)
            coverage = None
            if count > request["max_records"] and os.path.basename(cmd[0]) == "sacct" and "-X" in cmd and "--json" not in cmd and "-j" not in cmd:
                # Native -X excludes steps at the scheduler. Preserve a
                # deterministic bounded suffix and publish exactly what was
                # omitted, rather than failing the entire History source.
                rows = raw.splitlines()
                raw = "\n".join(rows[-request["max_records"]:]) + "\n"
                coverage = {"partial": True, "available_records": count, "retained_records": request["max_records"],
                            "basis": "last parent rows in scheduler output order"}
            elif count > request["max_records"]:
                raise ValueError(f"Scheduler read exceeded the site record budget: {source} returned "
                                 f"{count} records (limit {request['max_records']}); narrow the query "
                                 "scope or history window, or review the configured record limit")
            # Evidence strings are individually bounded to 64 KiB. Preserve
            # normal large queue/accounting responses as bounded chunks;
            # cache admission still checks their total serialized byte size.
            return {"stdout_chunks": [raw[i:i + 32768] for i in range(0, len(raw), 32768)], "coverage": coverage}
        try:
            result = self.acquisition.query(self.scope, query, load, self.policy, self.capabilities,
                                      read_only=True, cancel=cancel, fresh=fresh)
        except (ValueError, TimeoutError, OSError, RuntimeError) as exc:
            if (cancel is None or not cancel.is_set()) and ("invalidated" in str(exc).lower() or "cancelled during collection" in str(exc).lower()):
                from raven.collection.ledger import Deferred
                raise Deferred("Observation invalidated by a changed scheduler scope; retrying", retry_after=1.) from exc
            if self.ledger is not None:
                from raven.collection.ledger import Deferred
                if isinstance(exc, Deferred):
                    raise
            raise CommandError("Governed collection: " + str(exc)) from exc
        self._last.receipt = {key: value for key, value in result.items() if key != "value"}
        if descriptor is not None:
            self._last.receipt["descriptor"] = {"endpoint": descriptor.endpoint, "family": descriptor.family,
                "scope": self.scope, "freshness": "post-review" if fresh else "observation", "cost": descriptor.cost,
                "max_response_bytes": request["max_bytes"], "deadline_seconds": duration, "cancellable": True,
                "side_effect": "read-only"}
        self._last.coverage = result["value"].get("coverage")
        raw = "".join(result["value"]["stdout_chunks"])
        if len(raw) > limit or len(raw.encode("utf-8")) > limit:
            raise CommandError("Command output exceeded the inspection limit")
        return raw

    @property
    def observation_coverage(self):
        return getattr(self._last, "coverage", None)

    def run(self, cmd, timeout=8.0):
        start = time.monotonic()
        return self._query(cmd, timeout, 4 << 20, None, fresh=False), time.monotonic() - start

    def observe(self, cmd, timeout, limit, cancel):
        """Cancellable background observation under normal source admission."""
        return self._query(cmd, timeout, limit, cancel, fresh=False)

    def command(self, cmd, timeout, limit, cancel):
        """Bounded exact-review path; retain cancellation and cache admission."""
        return self._query(cmd, timeout, limit, cancel, fresh=True)

    def inspect_command(self, argv, timeout, limit, cancel=None, *, depth=0):
        return self.command(argv, timeout, limit, cancel)

    def call(self, cmd, timeout=15.0):
        # Mutations always execute once through the actual connection and
        # invalidate cached observations, even if the action reports failure.
        try:
            if self.ledger is not None:
                from raven.collection.ledger import classify
                descriptor = classify(cmd)
                if descriptor is None:
                    return False, "Raven collection: unclassified generated action is unavailable"
                if not self.ledger.config.get("slurm_enabled", True) and descriptor.endpoint != "local":
                    return False, "Raven collection: Slurm is unavailable in this site profile"
                try:
                    with self.ledger.admit(descriptor, cmd, validation=True):
                        from .slurm import Backend
                        from .remote import SshBackend
                        transport = self.inner
                        seen = set()
                        while getattr(transport, "inner", None) is not None and id(transport) not in seen:
                            seen.add(id(transport))
                            transport = transport.inner
                        limit = min(4 << 20, self.ledger.config["max_response_bytes"])
                        if type(transport) is Backend or type(transport) is SshBackend and transport.runner is SshBackend._run:
                            result = True, self._execute(cmd, timeout, limit, None)
                        else:
                            result = self.inner.call(cmd, timeout)
                        if len(str(result[1]).encode("utf-8")) > limit:
                            raise CommandError("Action reply exceeded the admitted output budget")
                        if not result[0]:
                            raise CommandError(str(result[1]))
                        return result
                except (RuntimeError, CommandError) as exc:
                    return False, str(exc)
            return self.inner.call(cmd, timeout)
        finally:
            self.acquisition.invalidate(self.scope)

    def invalidate(self):
        self.acquisition.invalidate(self.scope)

    def close(self):
        """Release synthetic state without closing another real subscriber."""
        try:
            close = getattr(self.inner, "close", None)
            if callable(close):
                close()
        finally:
            if self._owns_cache and isinstance(self.cache, ProcessQueryCache):
                self.acquisition.broker.close(wait=False)
            # Each cache owns a thread-local connection. Closing this caller's
            # idle handle preserves the on-disk observations and other worker
            # handles; their thread-local owners close when those threads end.
            # A caller-supplied portable cache can serve another subscriber;
            # its close is terminal, unlike the shared cache's per-thread close.
            if self._owns_cache or not isinstance(self.cache, ProcessQueryCache):
                self.cache.close()


def wrap_backend(backend, cfg, scope, state_dir):
    """Return an opt-in governed backend, or the original object unchanged.

    cfg.site_policy is a local JSON path; cfg.collection_cache_dir optionally
    selects a private host-local cache directory. No network setup or packages
    are installed. An invalid explicitly configured policy fails visibly.
    """
    raven = cfg.get("raven", {}) or {}
    collection = raven.get("collection") if isinstance(raven, dict) else None
    if not cfg.get("site_policy") and collection is None:
        return backend
    from .record import ReplayBackend
    current, seen, simulated = backend, set(), False
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, FakeBackend) and collection is None:
            return backend
        simulated = simulated or isinstance(current, (FakeBackend, ReplayBackend))
        current = getattr(current, "inner", None)
    if isinstance(backend, GovernedBackend):
        return backend
    if not isinstance(scope, dict) or not scope:
        raise ValueError("Governed collection requires explicit connection identity")
    if cfg.get("site_policy"):
        policy = P.validate(_read(cfg["site_policy"]))
    else:
        from raven.collection.ledger import settings
        collection = settings(collection)
        policy = P.validate({"schema": P.SCHEMA, "id": "raven-managed-user-host", "revision": "1",
            "sources": ["slurm-cli"], "operations": ["queue", "accounting", "nodes", "statistics", "job-detail",
            "hostnames", "configuration", "shares", "budget-report", "associations"],
            "limits": {"min_interval": .001, "max_duration": 30., "max_bytes": collection["max_response_bytes"],
                       "max_records": collection["max_response_records"], "max_inflight": 2},
            "allocation_required": False, "allow_intrusive": False})
    if collection is None and not state_dir and not cfg.get("collection_cache_dir"):
        raise ValueError("Governed collection requires a private host-local cache directory")
    from .shared_collection import _host, _process_start, _network_mount
    explicit = cfg.get("collection_cache_dir", "")
    if explicit:
        # A fallback is a capability decision, never permission to accept an
        # unsafe explicitly configured path.
        from .config_trust import directory_fd
        explicit_root = Path(explicit).expanduser().absolute()
        if explicit_root.exists() or explicit_root.is_symlink():
            with directory_fd(explicit_root, private=True):
                pass
    portable = False
    try:
        host = _host()
        if _process_start(os.getpid()) is None:
            raise ValueError("Shared collection requires process start identity")
        probe = explicit or (state_dir if collection is None else os.environ.get("XDG_RUNTIME_DIR") or "/tmp")
        if _network_mount(str(Path(probe).absolute())):
            raise ValueError("Shared collection cache requires host-local storage, not a network/shared filesystem")
    except ValueError as exc:
        if not any(reason in str(exc) for reason in ("process start identity", "Linux process/boot identity", "Cannot verify", "filesystem identity is unavailable")):
            raise
        portable = True
        host = "process-local"
    maximum = min(64 << 20, max(policy["limits"]["max_bytes"], 16 << 20))
    private_simulation = simulated and collection is not None
    if portable or private_simulation:
        # Synthetic observations must never enter a live host's cache. Reuse
        # the bounded memory/ledger contract without disk files that background
        # owners could recreate while the dashboard removes its temporary root.
        cache = ProcessQueryCache(max_bytes=maximum)
    else:
        if collection is not None:
            from raven.collection.ledger import cache_directory
            directory = cache_directory(explicit)
        else:
            directory = explicit or str(Path(state_dir) / "collection-cache" / host[:24])
        cache = SharedQueryCache(directory, max_bytes=maximum)
    governed = GovernedBackend(backend, policy, cache, scope, interval=cfg.get("collection_interval"), collection=collection, owns_cache=True)
    governed.cache_coordination = ("process-local; simulated source" if private_simulation else
                                  "process-local; host identity unavailable" if portable else "shared host-local")
    return governed
