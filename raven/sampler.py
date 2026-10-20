"""The scheduler: every source runs on its own cadence in a small thread pool, never overlapping itself, with
per-source health (latency, errors, exponential backoff) and a kick that makes the next round immediate."""
from __future__ import annotations

import concurrent.futures
from copy import deepcopy
from dataclasses import replace
import math
import re
import threading
import time
from typing import Callable, Dict, List, Optional

from . import clock
from .model import Health, Job, Store
from .cadence import validate_multiplier
from .slurm import CommandError, Slurm
from .worker_scheduler import WorkerScheduler


HISTORY_REFRESH_MIN = 5.0
HISTORY_FAST_ATTEMPTS = 5
GPU_ALLOCATION_CACHE_MAX = 10000
GPU_ALLOCATION_CACHE_MIN_AGE = 10.0
GPU_ALLOCATION_CACHE_MAX_AGE = 120.0
METRIC_SAMPLING_MAX = 128


class Sampler(threading.Thread):
    def __init__(self, slurm: Slurm, store: Store, intervals: Dict[str, float], gpu_types: List[str], history_days: float = 2.0,
                 account: str = "", gpu_sampling: bool = True, workers: int = 4, on_event: Optional[Callable[[dict], None]] = None,
                 weather: bool = True, probes: Optional[List[dict]] = None, budget: bool = True, files=None, polling_multiplier: int = 1,
                 worker_scheduler=None, collection=None):
        super().__init__(daemon=True, name="raven-sampler")
        self.polling_multiplier = validate_multiplier(polling_multiplier)
        self.slurm, self.store, self.intervals = slurm, store, dict(intervals)
        from .raven_collection import settings as collection_settings
        self.collection = collection_settings(collection) if collection is not None else None
        self._collection_demand = {"tab": "jobs", "selected": None, "sources": frozenset()}
        self._collection_activity = time.monotonic()
        self._collection_not_before = {}
        self._collection_failures = {}
        self._collection_deferred = set()
        self._live_offset = 0
        self._node_offset = 0
        self.gpu_types, self.history_days, self.account = list(gpu_types), history_days, account
        self.gpu_sampling = gpu_sampling and self.collection is None
        self.on_event = on_event
        self.weather, self.probes, self.budget = weather, list(probes or []), budget
        if self.collection is not None:
            # Existing job/site telemetry remains available through trace and
            # research readers. Monitoring never creates an unsolicited step
            # or test-only submission in either Raven execution mode.
            self.weather, self.probes = False, []
        from .local_files import LocalFiles
        self.files = files or LocalFiles()
        self.want_fin: Optional[str] = None                # a finished job whose steps the details overlay wants
        self.want_trace: Optional[str] = None              # a job (running or finished) whose GPU trace the analytics view wants
        self.trace_status: Dict[str, dict] = {}
        self.telemetry_binding = None
        self.retained_telemetry = {}
        self._trace_requested = 0
        self._trace_handled = 0
        self._retained_trace_floor = 0.
        self._recordings_interval = None
        # Positive allocation evidence omitted by %b, never persisted. Access
        # and queue reapplication share Store.lock with sampler publication.
        self._gpu_allocation_cache: Dict[str, dict] = {}
        self._owns_worker_scheduler = worker_scheduler is None
        self.worker_scheduler = worker_scheduler or WorkerScheduler(workers=workers, lanes=("source", "gpu"))
        self.pool = self.worker_scheduler.lane("source")
        # GPU waits cooperate with the shared governor. A queued child can use
        # the waiting parent's permit instead of requiring a second worker.
        self.gpu_workers = min(workers, 4)
        self.gpu_pool = self.worker_scheduler.lane("gpu")
        self._schedule_lock = threading.RLock()
        # Metric demand changes never edit configured source intervals. Keep
        # only exact current attempts and completion marks for accelerated jobs.
        self._metric_requests = {}
        self._metric_jobs = {}
        self._metric_jobs_ref = None
        self._metric_rates = {}
        self._metric_completed = {}
        self._baseline_completed = {}
        self._metric_scheduled_sources = set()
        self._sampling_context = threading.local()
        self.stop = threading.Event()
        self.kick = threading.Event()
        self.want_detail: Optional[str] = None
        self._historical_details: Dict[str, Dict[str, str]] = {}
        self._historical_detail_identities = {}
        self._details_refresh: Optional[tuple] = None
        self.sources: Dict[str, Callable[[], None]] = {
            "jobs": self.src_jobs, "starts": self.src_starts, "live": self.src_live, "gpu": self.src_gpu, "nodes": self.src_nodes,
            "partitions": self.src_partitions, "finished": self.src_finished, "share": self.src_share, "account": self.src_account, "details": self.src_details,
            "weather": self.src_weather, "budget": self.src_budget, "trace": self.src_trace, "fin_details": self.src_fin_details,
        }
        for name in self.sources:
            self.store.health[name] = Health(name=name)
        self.dependencies = {
            "starts": ("jobs",), "live": ("jobs",), "gpu": ("jobs",),
            "nodes": ("jobs",), "trace": ("jobs",),
            "weather": ("jobs", "partitions", "share"),
        }
        self.last_run: Dict[str, float] = {name: 0.0 for name in self.sources}
        if self.collection is not None:
            import random
            start = time.time()
            jitter = min(1., self.collection["jitter_fraction"])
            self._collection_not_before = {name: start + random.SystemRandom().uniform(0., self._collection_interval(name, 0.) * jitter)
                                           for name in self.sources}
        # A generation counter retains a departure that races an in-flight sacct.
        # Expedited retries are bounded and never reset a failing source's backoff.
        self._finished_requested = 0
        self._finished_handled = 0
        self._finished_retry_remaining = 0
        self._finished_retry_at = 0.0
        self._finished_retry_delay = HISTORY_REFRESH_MIN
        self._finished_completed_at = 0.0
        self.hooks: List[Callable[[dict], None]] = []     # plugin event hooks, called after on_event
        self.job_observers = []                         # bounded read-only consumers of existing samples
        self.observer_errors = {}
        self._job_observer_lock = threading.RLock()

    def add_source(self, name: str, interval: float, fn: Callable[[Slurm, Store], None]) -> None:
        """A plugin source: ``fn(slurm, store)`` every ``interval`` seconds, with health and backoff like the others."""
        if self.collection is not None:
            raise ValueError("Raven managed collectors require an admitted source descriptor; arbitrary plugin collectors are disabled")
        with self._schedule_lock:
            self.sources[name] = lambda: fn(self.slurm, self.store)
            self.intervals[name] = float(interval)
            self.store.health[name] = Health(name=name)
            self.last_run[name] = 0.0

    def set_polling_multiplier(self, value):
        """Wake scheduling without resetting deadlines, errors, or base intervals."""
        value = validate_multiplier(value)
        with self._schedule_lock:
            changed = self.polling_multiplier != value
            self.polling_multiplier = value
        if changed:
            self.kick.set()
        return value

    def set_recordings_interval(self, seconds):
        """Set an independent legacy file-read cadence, preserving source health."""
        if seconds is not None and (type(seconds) not in (int, float) or seconds not in (1., 2.)):
            raise ValueError("Recording reads must use Automatic, 1 second or 2 seconds")
        with self._schedule_lock:
            changed = self._recordings_interval != seconds
            self._recordings_interval = seconds
        if changed:
            self.kick.set()
        return seconds

    def set_collection_settings(self, collection, intervals=None, *, retained_trace_floor=0.):
        """Retune the existing scheduler without restarting or losing evidence.

        Successful completion deadlines follow the new cadence immediately.
        Failed/deferred work retains its recovery deadline, while an in-flight
        source completes once and schedules its successor using the new policy.
        """
        from .raven_collection import settings
        updated = settings(collection)
        new_intervals = dict(self.intervals if intervals is None else intervals)
        if any(type(value) not in (int, float) or not math.isfinite(value) or value < 0
               for value in new_intervals.values()):
            raise ValueError("Source intervals must be finite nonnegative seconds")
        if type(retained_trace_floor) not in (int, float) or not math.isfinite(retained_trace_floor) or retained_trace_floor < 0:
            raise ValueError("Retained telemetry floor must be finite nonnegative seconds")
        with self._schedule_lock:
            if (self.collection == updated and self.intervals == new_intervals
                    and self._retained_trace_floor == retained_trace_floor):
                return False
            self.collection, self.intervals = updated, new_intervals
            self._retained_trace_floor = float(retained_trace_floor)
            import random
            now = time.time()
            for name in self.sources:
                health = self.health(name)
                if health.inflight or health.backoff or name in self._collection_deferred:
                    continue
                cadence = self.effective_interval(name)
                jitter = 0. if name == "trace" and self.telemetry_binding else random.SystemRandom().uniform(0., cadence * min(1., updated["jitter_fraction"]))
                self._collection_not_before[name] = (health.last_ok or now) + cadence + jitter
            self.prime_collection_snapshot()
        self.kick.set()
        return True

    def _requested_interval(self, name, rate=1):
        from .cadence import interval
        if self.collection is not None:
            # Managed profiles already carry the exact requested seconds. The
            # slider is resolved into that policy once; passing a 1 s preset
            # through the legacy native slider would turn it back into 5 s.
            from .raven_collection import FLOORS
            requested = float(self.intervals.get(name, self.collection.get(FLOORS.get(name), 30.)))
            if name == "trace":
                from .cadence import LOCAL_FILE_FLOOR, REMOTE_FILE_FLOOR
                requested = max(requested, REMOTE_FILE_FLOOR if getattr(self.files, "remote", False) else LOCAL_FILE_FLOOR)
            return requested
        return interval(self.intervals.get(name, 30.0), self.polling_multiplier, rate, source=name,
                        file=name == "trace", remote=bool(getattr(self.files, "remote", False)))

    def effective_interval(self, name):
        with self._schedule_lock:
            rate = max(self._active_metric_rates_locked(name).values(), default=1)
            return self._collection_interval(name, self._requested_interval(name, rate))

    def _collection_interval(self, name, requested):
        if name == "trace" and self.collection is None and self._recordings_interval is not None:
            from .cadence import LOCAL_FILE_FLOOR, REMOTE_FILE_FLOOR
            transport = REMOTE_FILE_FLOOR if getattr(self.files, "remote", False) else LOCAL_FILE_FLOOR
            return max(transport, self._recordings_interval)
        if name == "trace" and self.telemetry_binding:
            from .cadence import LOCAL_FILE_FLOOR, REMOTE_FILE_FLOOR
            transport = REMOTE_FILE_FLOOR if getattr(self.files, "remote", False) else LOCAL_FILE_FLOOR
            # Existing recordings stay fresh independently of Slurm polling or
            # an older generic trace cadence. Mandatory site floors still win.
            return max(self._retained_trace_floor, transport, min(2., requested))
        if self.collection is None:
            return requested
        from .raven_collection import interval
        result = interval(self.collection, name, requested)
        if (name != "trace" and time.monotonic() - self._collection_activity >= self.collection["idle_after_seconds"]):
            result *= max(1., self.collection["idle_interval_factor"])
        return result

    def set_collection_demand(self, tab, selected_job=None, *, active=False, sources=()):
        """Publish visible subscriptions; no source IO and no quota resets.

        active means an actual input gesture, not a periodic UI tick. Explicit
        metric views may add live/trace to sources while viewing Jobs Details.
        """
        if self.collection is None:
            return
        with self._schedule_lock:
            demand = {"tab": str(tab), "selected": str(selected_job) if selected_job else None,
                      "sources": frozenset(str(s) for s in tuple(sources)[:32])}
            changed = demand != self._collection_demand
            self._collection_demand = demand
            if active:
                self._collection_activity = time.monotonic()
            if changed and demand["selected"] != getattr(self, "_trace_selected", None):
                self._trace_selected = demand["selected"]
                self._trace_requested += 1
        if changed:
            self.prime_collection_snapshot()
        if changed or active:
            self.kick.set()

    def prime_collection_snapshot(self, sources=None):
        """Make a first explicit snapshot eligible without background jitter.

        This changes only never-attempted sources in the current visible
        demand. Existing source deadlines, failed attempts, endpoint budgets,
        and producer admission remain authoritative across repeated requests.
        """
        if self.collection is None:
            return
        allowed = set(self.sources if sources is None else sources)
        with self._schedule_lock:
            for name in self.sources:
                health = self.health(name)
                if (name in allowed and self._collection_needed(name) and not health.calls
                        and not health.inflight and not self.last_run[name]):
                    self._collection_not_before[name] = 0.

    def _collection_needed(self, name):
        if self.collection is None:
            return True
        if not self.collection.get("slurm_enabled", True) and name != "trace":
            return False
        demand = self._collection_demand
        tab, selected = demand["tab"], demand["selected"]
        if name in ("gpu", "weather"):
            return False
        if name == "live":
            return (self.collection["live_statistics"] != "off" and bool(selected or self._metric_requests)
                    and (tab == "analytics" or "live" in demand["sources"]))
        if name == "trace":
            retained = self.telemetry_binding and self.telemetry_binding.get("job_id") in (selected, self.want_trace)
            return bool(retained) or (self.collection["gpu"] != "off" and bool(selected or self.want_trace)
                    and (tab in ("analytics", "research") or "trace" in demand["sources"]))
        if name == "details":
            return bool(self.want_detail)
        if name == "fin_details":
            return bool(self.want_fin)
        if name in demand["sources"] or name == "jobs":
            return True
        subscriptions = {"starts": ("jobs", "group"), "finished": ("jobs", "history", "analytics", "log", "research"),
                         "nodes": ("nodes", "cluster"), "partitions": ("nodes", "cluster"),
                         "share": ("cluster", "group"), "account": ("group",), "budget": ("cluster", "research")}
        return tab in subscriptions.get(name, ())

    def _baseline_interval(self, name):
        """Use the same source bounds for scheduling and displayed cadences."""
        return self._collection_interval(name, self._requested_interval(name))

    @staticmethod
    def _metric_attempt(job):
        return "|".join(str(getattr(job, field, None) or "") for field in ("submit", "start"))

    def set_metric_sampling(self, requests, *, _expected=None):
        """Replace bounded exact-attempt demands without probing any source.

        Unsupported readers belong to their own sampling service. A stale,
        completed or pending job cannot transfer a rate to a reused ID.
        """
        from .cadence import source, validate_rate
        if not isinstance(requests, dict) or len(requests) > METRIC_SAMPLING_MAX:
            raise ValueError("metric sampling requests must be a mapping of at most 128 metrics")
        validated = [(identity, validate_rate(value), source(identity)) for identity, value in requests.items()]
        with self._schedule_lock:
            previous_jobs, previous_requests = dict(self._metric_jobs), dict(self._metric_requests)
        wanted = {str(identity[1]) for identity, _, name in validated
                  if name in ("live", "gpu", "trace") and isinstance(identity, tuple) and len(identity) > 4}
        with self.store.lock:
            jobs_ref = self.store.jobs
            current = {j.id: (j, self._metric_attempt(j), self.store.job_attempt(j.id))
                       for j in self.store.jobs if j.id in wanted and j.state == "RUNNING"}
        accepted, rates, jobs = {}, {}, {}
        for identity, value, name in validated:
            if name not in ("live", "gpu", "trace") or not isinstance(identity, tuple) or len(identity) <= 4:
                continue
            jid = str(identity[1])
            record = current.get(jid)
            if record is None:
                continue
            if (identity in previous_requests and jid in previous_jobs
                    and previous_jobs[jid][2] != record[2]):
                continue
            attempt = identity[4]
            if attempt not in (record[1], "scheduler:" + record[1]):
                continue
            if value > 1:
                accepted[identity] = value
                jobs[jid] = record
                rates.setdefault(name, {})[jid] = max(value, rates.get(name, {}).get(jid, 1))
        with self._schedule_lock:
            if _expected is not None and (self._metric_requests != _expected
                    or {jid: record[1:] for jid, record in self._metric_jobs.items()}
                    != {jid: record[1:] for jid, record in previous_jobs.items()}):
                return len(self._metric_requests)
            changed = (accepted != self._metric_requests
                       or {jid: record[1:] for jid, record in jobs.items()}
                       != {jid: record[1:] for jid, record in self._metric_jobs.items()})
            self._metric_requests, self._metric_jobs, self._metric_rates = accepted, jobs, rates
            self._metric_jobs_ref = jobs_ref
            self._metric_scheduled_sources.update(rates)
            valid = {(name, jid, record[1], record[2])
                     for name, values in rates.items() for jid in values
                     for record in (jobs[jid],)}
            self._metric_completed = {key: value for key, value in self._metric_completed.items() if key in valid}
        if changed:
            self.kick.set()
        return len(accepted)

    def _active_metric_rates_locked(self, name):
        # This reads only the bounded Job references published by set/jobs. It
        # avoids Store.lock while the scheduler lock is held.
        if self.store.jobs is not self._metric_jobs_ref:
            return {}
        return {jid: rate for jid, rate in self._metric_rates.get(name, {}).items()
                if self._metric_jobs[jid][0].state == "RUNNING"
                and self._metric_jobs[jid][1] == self._metric_attempt(self._metric_jobs[jid][0])}

    def _metric_rate_locked(self, name, jid):
        record = self._metric_jobs.get(jid)
        if (self.store.jobs is not self._metric_jobs_ref or record is None
                or record[0].state != "RUNNING" or record[1] != self._metric_attempt(record[0])):
            return 1
        return self._metric_rates.get(name, {}).get(jid, 1)

    def sampling_interval(self, name, jid, attempt=None):
        """Return the actual safe cadence for one job's shared metric source."""
        jid = str(jid)
        with self._schedule_lock:
            record = self._metric_jobs.get(jid)
            rate = self._metric_rate_locked(name, jid)
            if attempt is not None and (record is None or attempt not in (record[1], "scheduler:" + record[1], record[2])):
                rate = 1
            return self._collection_interval(name, self._requested_interval(name, rate))

    def _sampling_targets(self, name, jobs):
        """Gate scheduled probes; direct source calls retain their old behavior."""
        context = getattr(self._sampling_context, "current", None)
        if context is None or context["source"] != name:
            return jobs
        now = time.monotonic()
        with self._schedule_lock:
            if name == "trace" and context.get("trace_requested", 0) > self._trace_handled:
                context["baseline"] = True
                return jobs
            rates = self._active_metric_rates_locked(name)
            base = self._baseline_interval(name)
            last = self._baseline_completed.get(name)
            baseline_due = last is None or now - last >= base
            # No active per-job override preserves existing source scheduling,
            # including --once, zero intervals and manual refresh behavior.
            context["baseline"] = not rates or baseline_due
            if not rates:
                return jobs
            selected = []
            for job in jobs:
                jid = str(getattr(job, "id", job))
                record = self._metric_jobs.get(jid)
                if jid not in rates or record is None:
                    if baseline_due:
                        selected.append(job)
                    continue
                key = (name, jid, record[1], record[2])
                completed = self._metric_completed.get(key, last)
                cadence = self.sampling_interval(name, jid, record[1])
                if completed is None or now - completed >= cadence:
                    selected.append(job)
                    context["targets"][jid] = key
            return selected

    def _sampling_complete(self, name, jid):
        context = getattr(self._sampling_context, "current", None)
        if context is None or context["source"] != name:
            return
        key = context["targets"].get(str(jid))
        if key is not None:
            with self._schedule_lock:
                if (name == "gpu" and context.get("provider_generation", 0)
                        != getattr(self.slurm, "gpu_provider_generation", 0)):
                    return
                record = self._metric_jobs.get(str(jid))
                if (self.store.jobs is self._metric_jobs_ref and record is not None
                        and key[2:] == record[1:] and record[0].state == "RUNNING"
                        and record[1] == self._metric_attempt(record[0])):
                    self._metric_completed[key] = time.monotonic()

    def _metric_source_due(self, name):
        """Next completion deadline, including unchanged jobs after a reset."""
        now = time.monotonic()
        with self._schedule_lock:
            last = self._baseline_completed.get(name)
            baseline = self._baseline_interval(name)
            if last is None or now - last >= baseline:
                return True
            for jid in self._active_metric_rates_locked(name):
                record = self._metric_jobs[jid]
                key = (name, jid, record[1], record[2])
                completed = self._metric_completed.get(key, last)
                if now - completed >= self.sampling_interval(name, jid, record[1]):
                    return True
        return False

    def emit(self, events: List[dict]) -> None:
        batch = getattr(self.on_event, "notify_many", None)
        if callable(batch):
            try:
                batch(events)
            except Exception:
                pass
        for ev in events:
            for fn in ([self.on_event] if self.on_event and not callable(batch) else []) + self.hooks:
                try:
                    fn(ev)
                except Exception:
                    pass

    # ---- scheduling --------------------------------------------------------------------------------
    def health(self, name: str) -> Health:
        return self.store.health[name]

    def due(self, name: str, now: float) -> bool:
        h = self.health(name)
        immediate_trace = (name == "trace" and self._trace_requested > self._trace_handled
                           and not h.backoff and name not in self._collection_deferred)
        if self.collection is not None:
            if (not self._collection_needed(name)
                    or not immediate_trace and now < self._collection_not_before.get(name, 0.)):
                return False
        if not h.enabled or h.inflight:
            return False
        if h.calls == 0 and not (name == "trace" and self.telemetry_binding):
            for dependency in self.dependencies.get(name, ()):
                prerequisite = self.health(dependency)
                if prerequisite.enabled and (prerequisite.calls == 0 or prerequisite.inflight):
                    return False
        if name in ("live", "gpu") and self.want_detail:
            # The existing first selected-job inspection can establish the
            # recording identity before its first native measurement. This
            # changes order only: no extra scheduler request or blocking wait.
            # Missing fields/errors still permit isolated provisional samples.
            inspection = self.health("details")
            if (inspection.enabled and self._collection_needed("details")
                    and (not inspection.last_ok or inspection.inflight)
                    and self.want_detail not in self.store.details and not inspection.backoff):
                return False
        if name == "gpu" and not self.gpu_sampling:
            return False
        if name == "details" and not self.want_detail:
            return False
        if name in ("account", "budget") and not self.account:
            return False
        if name == "weather" and not self.weather:
            return False
        if name == "budget" and not self.budget:
            return False
        if name == "fin_details" and not self.want_fin:
            return False
        if self.collection is not None and name in self._collection_deferred:
            return now >= self._collection_not_before.get(name, 0.)
        if immediate_trace:
            return True
        regular = now - self.last_run[name] >= self.effective_interval(name) + h.backoff
        if name in self._metric_scheduled_sources and h.calls and not h.backoff:
            regular = self._metric_source_due(name)
        if self.collection is not None:
            return regular
        if name == "details" and not regular:
            request = self._details_refresh
            return bool(request and request[0] == self.want_detail and not h.backoff and
                        request[1] == self.store.job_attempt(request[0]))
        if name != "finished" or regular:
            return regular
        # Failures use the existing source backoff; new departures cannot turn an
        # unavailable accounting service into a tight retry loop.
        if h.backoff:
            return False
        requested = self._finished_requested > self._finished_handled
        retry = self._finished_retry_remaining > 0 and now >= self._finished_retry_at
        last = max(self.last_run[name], self._finished_completed_at)
        return (requested or retry) and now - last >= HISTORY_REFRESH_MIN

    def request_history(self):
        """Coalesce queue departures into an accounting refresh and limited retries."""
        with self._schedule_lock:
            self._finished_requested += 1
            self._finished_retry_remaining = HISTORY_FAST_ATTEMPTS
            self._finished_retry_delay = HISTORY_REFRESH_MIN
        self.kick.set()

    def run_source(self, name: str):
        from .raven_collection import Deferred
        h = self.health(name)
        previous_context = getattr(self._sampling_context, "current", None)
        context = {"source": name, "targets": {}, "baseline": False}
        if name == "trace":
            with self._schedule_lock:
                context["trace_requested"] = self._trace_requested
                context["trace_identity"] = self._telemetry_identity(self.telemetry_binding)
        if name == "gpu":
            context["provider_generation"] = getattr(self.slurm, "gpu_provider_generation", 0)
        self._sampling_context.current = context
        t0 = time.perf_counter()
        h.last_try = time.time()
        try:
            self.sources[name]()
            with self.store.lock:
                if name == "trace" and context["trace_identity"] != self._telemetry_identity(self.telemetry_binding):
                    return
                if (name == "gpu" and context["provider_generation"]
                        != getattr(self.slurm, "gpu_provider_generation", 0)):
                    return
                h.calls += 1
                h.latency_ms = 1000 * (time.perf_counter() - t0)
                h.last_ok = time.time()
                diagnostic = getattr(self.slurm, "parse_diagnostics", {}).get(name, {})
                context["partial"] = bool(diagnostic.get("partial"))
                h.error = diagnostic.get("message", "")
                if context["partial"]:
                    h.errors += diagnostic.get("rejected", 0) + diagnostic.get("ambiguous", 0)
                h.backoff = 0.0
        except Deferred as e:
            # Admission deferral is observable but is not a failed scheduler
            # command and must not trip the source's failure circuit.
            with self.store.lock:
                if name == "trace" and context["trace_identity"] != self._telemetry_identity(self.telemetry_binding):
                    return
                context["deferred"] = True
                context["retry_after"] = e.retry_after
                h.calls += 1
                h.error = str(e)
        except CommandError as e:
            with self.store.lock:
                if name == "trace" and context["trace_identity"] != self._telemetry_identity(self.telemetry_binding):
                    return
                if (name == "gpu" and context["provider_generation"]
                        != getattr(self.slurm, "gpu_provider_generation", 0)):
                    return
                h.calls += 1
                h.errors += 1
                h.error = str(e)
                h.backoff = min(300.0, max(self.intervals.get(name, 30.0), 2 * h.backoff))
        except Exception as e:                            # a bug in a source never stops the others
            with self.store.lock:
                if name == "trace" and context["trace_identity"] != self._telemetry_identity(self.telemetry_binding):
                    return
                if (name == "gpu" and context["provider_generation"]
                        != getattr(self.slurm, "gpu_provider_generation", 0)):
                    return
                h.calls += 1
                h.errors += 1
                h.error = f"{type(e).__name__}: {e}"
                h.backoff = min(300.0, max(self.intervals.get(name, 30.0), 2 * h.backoff))
        finally:
            with self._schedule_lock:
                stale_trace = (name == "trace" and context["trace_identity"]
                               != self._telemetry_identity(self.telemetry_binding))
                if name == "trace":
                    self._trace_handled = max(self._trace_handled, context["trace_requested"])
                if self.collection is not None and not stale_trace:
                    import random
                    failed = bool(h.error) and not context.get("deferred") and not context.get("partial")
                    failures = self._collection_failures.get(name, 0) + 1 if failed else 0
                    self._collection_failures[name] = failures
                    if context.get("deferred"):
                        self._collection_deferred.add(name)
                    else:
                        self._collection_deferred.discard(name)
                    if failures >= self.collection["circuit_failures"]:
                        h.backoff = max(h.backoff, self.collection["circuit_seconds"])
                    cadence = self.effective_interval(name)
                    jitter = 0. if name == "trace" and self.telemetry_binding else random.SystemRandom().uniform(0., cadence * min(1., self.collection["jitter_fraction"]))
                    self._collection_not_before[name] = time.time() + (context.get("retry_after", 5.) if context.get("deferred") else cadence + jitter + h.backoff)
                if context["baseline"] and not context.get("deferred") and not stale_trace and name in ("live", "gpu", "trace"):
                    if (name != "gpu" or context["provider_generation"]
                            == getattr(self.slurm, "gpu_provider_generation", 0)):
                        self._baseline_completed[name] = time.monotonic()
                # Publish completion and the new deadline as one scheduler
                # update, so a live policy change cannot miss a finishing task.
                with self.store.lock:
                    h.inflight = False
            self._sampling_context.current = previous_context

    def round(self, now: Optional[float] = None, wait: bool = False):
        """Submit every due source; ``wait`` blocks until they finish (used by --once and the tests)."""
        now = time.time() if now is None else now
        futures, submitted = [], set()
        while True:
            batch = []
            with self._schedule_lock:
                if self.stop.is_set():
                    break
                order = ("trace", *(name for name in self.sources if name != "trace")) if self.telemetry_binding else self.sources
                for name in order:
                    # Retained files do not consume a Slurm command slot. They
                    # share the bounded worker pool but remain eligible while
                    # a scheduler command is waiting on a slow controller.
                    independent_trace = name == "trace" and self.telemetry_binding
                    if (self.collection is not None and not independent_trace
                            and sum(self.health(source).inflight for source in self.sources
                                    if source != "trace" or not self.telemetry_binding)
                            >= max(1, self.collection["max_inflight"] - 1)):
                        continue
                    if name not in submitted and self.due(name, now):
                        previous = self.last_run[name]
                        health = self.health(name)
                        self.last_run[name], health.inflight = now, True
                        try:
                            future = self.pool.submit(self.run_source, name)
                        except RuntimeError as exc:
                            self.last_run[name], health.inflight = previous, False
                            health.error = str(exc)[:512]
                            continue
                        submitted.add(name)
                        batch.append(future)
            futures.extend(batch)
            if not wait or not batch:
                break
            _, pending = concurrent.futures.wait(batch, timeout=60)
            if pending:
                break
            # Initial dependent sources are now eligible. Sample them before --once
            # returns, without repeating sources even when their interval is zero.
        return futures

    def run(self):
        while not self.stop.is_set():
            self.round()
            self.store.flush_persistence(_series_only=True, _defer_series=True)
            self.kick.wait(self.wake_interval())
            self.kick.clear()

    def wake_interval(self):
        """Wake often enough for every subscribed source's advertised cadence."""
        with self._schedule_lock:
            shortest = min((self.effective_interval(name) for name in self.sources
                            if self.health(name).enabled and self._collection_needed(name)), default=.5)
        return min(.5, max(.05, shortest))

    def refresh_all(self):
        invalidate = getattr(self.slurm, "invalidate_capabilities", None)
        if callable(invalidate):
            invalidate()
        if self.collection is not None:
            # Manual refresh requests the next admitted publication. It cannot
            # erase source clocks, failure recovery, per-job floors or quotas.
            self.kick.set()
            return
        with self._schedule_lock:
            self._metric_completed.clear()
            self._baseline_completed.clear()
        for name in self.last_run:
            self.last_run[name] = 0.0
            self.health(name).backoff = 0.0
        self.kick.set()

    def shutdown(self):
        with self._schedule_lock:
            self.stop.set()
            self.kick.set()
            self.pool.shutdown(wait=False, cancel_futures=True)
            self.gpu_pool.shutdown(wait=False, cancel_futures=True)
        self.store.configure_series_loader(None)
        if self._owns_worker_scheduler:
            self.worker_scheduler.shutdown(wait=False, cancel_futures=True)

    def enable_series_background(self):
        """Restore interactive metric histories on the existing source pool."""
        with self._schedule_lock:
            if not self.stop.is_set():
                self.store.configure_series_loader(self.pool.submit)

    # ---- sources -----------------------------------------------------------------------------------
    def _keep_rejected(self, source, current, previous):
        """A warning proves no missing row. Keep identifiable rejects briefly."""
        diagnostic = getattr(self.slurm, "parse_diagnostics", {}).get(source, {})
        rejected = set(diagnostic.get("rejected_ids", ())) if diagnostic.get("rejected") else set()
        counters = getattr(self, "_partial_rows", {})
        seen = {row.id for row in current}
        kept, active = [], set()
        for row in previous:
            key = (source, row.id)
            if row.id not in seen and row.id in rejected:
                streak = counters.get(key, 0) + 1
                counters[key] = streak
                active.add(key)
                if streak <= 2:
                    kept.append(replace(row, collection_stale=True))
        for key in list(counters):
            if key[0] == source and key not in active:
                counters.pop(key)
        self._partial_rows = counters
        diagnostic["stale_ids"] = [row.id for row in kept]
        return current + kept

    def src_jobs(self):
        jobs = self.slurm.jobs()
        with self.store.lock:
            jobs = self._keep_rejected("jobs", jobs, self.store.jobs)
            returning = {job.id for job in jobs} & self.store.preserved_detail_ids()
            events = self.store.apply_jobs(jobs, _persist=False)
            self._gpu_cache_reapply_locked(time.monotonic())
        self.store.flush_persistence(_events_only=True)
        with self._schedule_lock:
            requests = dict(self._metric_requests)
        if requests:
            self.set_metric_sampling(requests, _expected=requests)
        for jid in returning:
            self._historical_details.pop(jid, None)
            with self._schedule_lock:
                if jid == self.want_detail:
                    self._details_refresh = (jid, self.store.job_attempt(jid))
                    self.kick.set()
        if events:
            self.emit(events)
        if any(ev["kind"] in ("started", "finished", "left") for ev in events):
            self.last_run["starts"] = 0.0                  # projected starts change when the queue moves
        if any(ev["kind"] in ("finished", "left") for ev in events):
            self.request_history()
        self.check_alerts()
        self.observe_jobs()

    def _verify_recording_attempts(self, targets):
        """Rotate bounded exact legacy reads; structured queue rows need none.

        The queue is already published. A site without structured restart
        counters can qualify unselected recordings without an unbounded burst
        of controller calls or coupling correctness to the current cursor.
        """
        read = getattr(self.slurm, 'record_details', None)
        if not callable(read) or not self.store.persist:
            return
        with self.store.lock:
            publication = self.store._queue_publication
            jobs = [job for job in targets if job.state == 'RUNNING' and job.restarts is None
                    and self.store._sample_recording_proofs.get(job.id, (None,))[0] != publication]
            captured = {job.id: (self.store.job_attempt(job.id), self._gpu_identity(job)) for job in jobs}
        if not jobs:
            return
        offset = getattr(self, '_attempt_verification_cursor', 0) % len(jobs)
        ordered = jobs[offset:] + jobs[:offset]
        deadline = time.monotonic() + .5
        attempted = 0
        for job in ordered[:16]:
            if self.stop.is_set() or time.monotonic() >= deadline:
                break
            attempted += 1
            try:
                details = read(job)
            except CommandError:
                # A disappearing/unreadable job remains session-isolated;
                # the next bounded rotation retries through normal admission.
                continue
            from .series_identity import scheduler_attempt
            if scheduler_attempt(job, details) is None:
                continue
            with self.store.lock:
                current = self.store.job(job.id)
                if self.store._queue_publication != publication or current is None or captured[job.id] != (self.store.job_attempt(job.id), self._gpu_identity(current)):
                    continue
                self.store.details[job.id] = details
                self.store.bind_series_scheduler(job.id, details, queue_publication=publication)
        self._attempt_verification_cursor = offset + attempted
        # Promotion copies and queued row redirection share the existing owned
        # persistence lane; all file work remains outside Store.lock.
        self.store.flush_persistence(_series_only=True, _defer_series=True)

    def observe_jobs(self):
        """Observers consume sampled jobs; they never request additional Slurm commands."""
        if not self.job_observers:
            return
        from copy import copy
        from dataclasses import asdict, fields
        native_fields = tuple(field.name for field in fields(Job))
        scalar_types = (str, int, float, bool, type(None))
        with self._job_observer_lock:
            # Keep this publication coherent without recursively converting
            # every field while UI snapshots wait for the Store lock. Native
            # scheduler fields are scalars; its one mutable list is detached.
            with self.store.lock:
                captured = []
                for job in self.store.jobs[:10000]:
                    values = (vars(job).copy() if type(job) is Job else
                              {field.name: getattr(job, field.name) for field in fields(job)})
                    if type(values.get("hosts")) is list:
                        values["hosts"] = values["hosts"].copy()
                    captured.append((job, values))
            jobs = []
            for job, values in captured:
                if type(job) is Job:
                    values = {name: values[name] for name in native_fields}
                    hosts = values["hosts"]
                    native = (type(hosts) is list and
                              all(type(value) in scalar_types for value in hosts) and
                              all(type(value) in scalar_types for name, value in values.items() if name != "hosts"))
                    if native:
                        jobs.append((values, True))
                        continue
                # Preserve asdict's representation for nested plugin values,
                # custom dataclasses and mutable containers, off the UI lock.
                detached = copy(job)
                for name, value in values.items():
                    object.__setattr__(detached, name, value)
                jobs.append((asdict(detached), False))
            for index, observer in enumerate(tuple(self.job_observers[:8])):
                try:
                    isolated = [dict(values, hosts=values["hosts"].copy()) if native else deepcopy(values)
                                for values, native in jobs]
                    observer(isolated)
                    self.observer_errors.pop(index, None)
                except Exception as exc:
                    self.observer_errors[index] = str(exc)[:512]

    def check_alerts(self):
        eng = self.store.alerts
        if eng is None or not eng.rules:
            return
        try:
            eng.check(self.store.render_snapshot(), marks=getattr(self, "marks", ()), tags=self.store.tag_map())
        except Exception:
            pass

    def src_starts(self):
        if any(j.pending for j in self.store.jobs):
            self.store.apply_starts(self.slurm.starts())
            self.observe_jobs()

    def src_live(self):
        if self.collection is not None and not self._collection_needed("live"):
            return
        now = clock.now()
        with self.store.lock:
            jobs = [j for j in self.store.jobs if not j.pending]
            attempts = {j.id: self.store.job_attempt(j.id) for j in jobs}
            recording_identities = {j.id: self.store.capture_series_identity(j.id) for j in jobs}
            identities = {j.id: self._gpu_identity(j) for j in jobs}
            previous = {j.id: self.store.prev_cpu.get(j.id) for j in jobs}
        jobs = self._sampling_targets("live", jobs)
        self._verify_recording_attempts(jobs)
        with self.store.lock:
            recording_identities = {job.id:self.store.capture_series_identity(job.id) for job in jobs}
            previous = {job.id:self.store.prev_cpu.get(job.id) for job in jobs}
        batched = None
        if self.collection is not None:
            requested = {self._collection_demand["selected"]} | set(self._active_metric_rates_locked("live"))
            jobs = [job for job in jobs if job.id in requested]
            if jobs:
                start = self._live_offset % len(jobs)
                bound = self.collection["live_batch_jobs"]
                jobs = (jobs[start:] + jobs[:start])[:bound]
                self._live_offset = start + len(jobs)
                batched = self.slurm.live_batch(jobs, previous, now)
        failures = []
        for job in jobs:
            if self.stop.is_set():
                break
            try:
                lv, keep, steps = batched[job.id] if batched is not None else self.slurm.live(job, previous[job.id], now)
            except CommandError as exc:
                failures.append(str(exc))
                continue
            finally:
                self._sampling_complete("live", job.id)
            with self.store.lock:
                current = self.store.job(job.id)
                if (current is None or self.store.job_attempt(job.id) != attempts[job.id]
                        or self._gpu_identity(current) != identities[job.id]
                        or self.store.capture_series_identity(job.id) != recording_identities[job.id]):
                    continue
                self.store.prev_cpu[job.id] = keep
                self.store.apply_live(job.id, lv, _persist=False, identity=recording_identities[job.id])
                self.store.steps[job.id] = steps
            self.store.flush_persistence(_series_only=True, _defer_series=True)
        with self.store.lock:
            alive = {job.id for job in self.store.jobs}
            for jid in set(self.store.steps) - alive:
                del self.store.steps[jid]
        self.check_alerts()
        if failures:
            raise CommandError(f"Live sampling failed for {len(failures)}/{len(jobs)} jobs: {failures[0]}")

    @staticmethod
    def _gpu_identity(job):
        """Stable queue identity; elapsed time and sampled fields are excluded."""
        return (job.start, job.submit, job.name, job.partition, job.state,
                job.nodelist, job.nodes, job.cpus, job.mem_req, job.limit,
                job.account, job.qos, job.command, job.workdir)

    def _gpu_cache_reapply_locked(self, now):
        current = {job.id: job for job in self.store.jobs}
        for jid, cached in list(self._gpu_allocation_cache.items()):
            job = current.get(jid)
            if (job is None or now >= cached["expires"] or job.state != "RUNNING"
                    or cached["attempt"] != self.store.job_attempt(jid)
                    or cached["identity"] != self._gpu_identity(job)):
                if job is cached.get("publication") and job.gpus == cached["count"] and job.gpu_type == cached["type"]:
                    job.gpu_type, job.gpus = "", 0
                    if job.id in self.store.gpu:
                        self.store.apply_gpu(job.id, None)
                self._gpu_allocation_cache.pop(jid, None)
                continue
            if job.gpus:
                # A new queue row with explicit GPUs supplies fresh evidence
                # itself; never replace it with inferred allocation metadata.
                if (job is not cached.get("publication") or job.gpus != cached["count"]
                        or job.gpu_type and job.gpu_type != cached["type"]):
                    self._gpu_allocation_cache.pop(jid, None)
                continue
            job.gpu_type, job.gpus = cached["type"], cached["count"]
            cached["publication"] = job
        while len(self._gpu_allocation_cache) > GPU_ALLOCATION_CACHE_MAX:
            self._gpu_allocation_cache.pop(next(iter(self._gpu_allocation_cache)))

    def set_gpu_provider(self, provider):
        """Invalidate current GPU observations without dropping historical data."""
        with self.store.lock:
            if not self.slurm.set_gpu_provider(provider):
                return False
            for jid in tuple(self.store.gpu):
                self.store.apply_gpu(jid, None)
            health = self.health("gpu")
            health.error, health.backoff = "", 0.0
        with self._schedule_lock:
            self.last_run["gpu"] = 0.0
            self._baseline_completed.pop("gpu", None)
            for key in tuple(self._metric_completed):
                if key[0] == "gpu":
                    self._metric_completed.pop(key, None)
        self.kick.set()
        return True

    def src_gpu(self):
        if self.collection is not None:
            return
        with self.store.lock:
            provider_generation = getattr(self.slurm, "gpu_provider_generation", 0)
            self._gpu_cache_reapply_locked(time.monotonic())
            running = [j for j in self.store.jobs if j.state == "RUNNING"]
            attempts = {j.id: self.store.job_attempt(j.id) for j in running}
            recording_identities = {j.id: self.store.capture_series_identity(j.id) for j in running}
            identities = {j.id: self._gpu_identity(j) for j in running}
        failures, discovery_error = [], ""
        discover = getattr(self.slurm, "gpu_allocations", None)
        if running and callable(discover):
            try:
                allocations = discover()
                age = max(GPU_ALLOCATION_CACHE_MIN_AGE, min(GPU_ALLOCATION_CACHE_MAX_AGE, self.effective_interval("gpu") * 2))
                with self.store.lock:
                    current = {j.id: j for j in self.store.jobs}
                    now = time.monotonic()
                    for j in running:
                        if current.get(j.id) is not j:
                            continue
                        cached = self._gpu_allocation_cache.get(j.id)
                        if j.id not in allocations:
                            # Unsupported old fields provide no authoritative
                            # reply. A successful available field can remove
                            # evidence for a job absent from that newer queue.
                            if getattr(self.slurm, "_gpu_allocations_supported", None) is True:
                                self._gpu_allocation_cache.pop(j.id, None)
                                if cached:
                                    j.gpu_type, j.gpus = "", 0
                                    if j.id in self.store.gpu:
                                        self.store.apply_gpu(j.id, None)
                            continue
                        kind, count = allocations[j.id]
                        inferred = not j.gpus or cached is not None
                        j.gpu_type, j.gpus = kind, count
                        if not count and j.id in self.store.gpu:
                            self.store.apply_gpu(j.id, None)
                        if count and inferred:
                            self._gpu_allocation_cache[j.id] = dict(
                                type=kind, count=count, identity=self._gpu_identity(j),
                                attempt=attempts[j.id], expires=now + age, publication=j)
                        else:
                            self._gpu_allocation_cache.pop(j.id, None)
                    self._gpu_cache_reapply_locked(now)
            except CommandError as exc:
                discovery_error = f"GPU allocation discovery: {exc}"
        jobs = self._sampling_targets("gpu", [j for j in running if j.gpus])
        self._verify_recording_attempts(jobs)
        with self.store.lock:
            recording_identities = {job.id:self.store.capture_series_identity(job.id) for job in jobs}
        if not jobs:
            if discovery_error:
                raise CommandError(discovery_error)
            return
        pending = {}
        remaining = iter(jobs)
        while not self.stop.is_set():
            # Bound both active commands and queued futures even for a very large account.
            with self._schedule_lock:
                if self.stop.is_set():
                    break
                while len(pending) < self.gpu_workers:
                    job = next(remaining, None)
                    if job is None:
                        break
                    pending[self.gpu_pool.submit(self.slurm.gpu, job)] = job
            if not pending:
                break
            done, _ = self.worker_scheduler.gather_children(pending, timeout=0.5,
                                                           return_when=concurrent.futures.FIRST_COMPLETED)
            for fut in done:
                job = pending.pop(fut)
                try:
                    samples, error = fut.result(), None
                except CommandError as exc:
                    samples, error = None, str(exc)
                finally:
                    self._sampling_complete("gpu", job.id)
                with self.store.lock:
                    current = self.store.job(job.id)
                    if (getattr(self.slurm, "gpu_provider_generation", 0) != provider_generation
                            or current is None or self.store.job_attempt(job.id) != attempts[job.id]
                            or self._gpu_identity(current) != identities[job.id]
                            or self.store.capture_series_identity(job.id) != recording_identities[job.id]):
                        continue
                    self.store.apply_gpu(job.id, samples, _persist=False, identity=recording_identities[job.id])
                    if error:
                        failures.append(error)
                    else:
                        warnings = getattr(self.slurm, "gpu_probe_warnings", {}).get(job.id, ())
                        if warnings:
                            failures.append(f"job {job.id}: partial GPU coverage: " + "; ".join(str(item)[:300] for item in warnings[:3]))
                self.store.flush_persistence(_series_only=True, _defer_series=True)
        for fut in pending:
            fut.cancel()
        self.check_alerts()
        if failures or discovery_error:
            reason = f"GPU sampling incomplete for {len(failures)}/{len(jobs)} jobs: {failures[0]}" if failures else ""
            raise CommandError("; ".join(message for message in (discovery_error, reason) if message))

    def src_nodes(self):
        names = sorted({h for j in self.store.jobs if not j.pending for h in j.hosts})
        if self.collection is not None:
            if names:
                start = self._node_offset % len(names)
                selected = (names[start:] + names[:start])[:self.collection["live_batch_jobs"]]
                nodes = self.slurm.nodes_batch(selected)
                self._node_offset = start + len(selected)
            else:
                nodes = {}
            with self.store.lock:
                current = {host for job in self.store.jobs if not job.pending for host in job.hosts}
                self.store.nodes = {name: node for name, node in self.store.nodes.items() if name in current}
                self.store.nodes.update({name: node for name, node in nodes.items() if name in current})
            return
        nodes = {}
        for n in names:
            node = self.slurm.node(n)
            if node:
                nodes[n] = node
        with self.store.lock:
            self.store.nodes = nodes

    def src_partitions(self):
        parts, tot, nodemap = self.slurm.partitions(self.gpu_types)
        with self.store.lock:
            self.store.partitions, self.store.gpu_inventory, self.store.nodemap = parts, tot, nodemap

    def src_finished(self):
        with self._schedule_lock:
            requested = self._finished_requested
        fin = self.slurm.finished(self.history_days)
        with self.store.lock:
            fin = self._keep_rejected("finished", fin, self.store.finished)
        self.store.apply_finished(fin, history_days=self.history_days)
        with self.store.lock:
            pending = bool(self.store.departed_jobs)
        with self._schedule_lock:
            self._finished_completed_at = time.time()
            self._finished_handled = max(self._finished_handled, requested)
            # Do not consume a retry belonging to a newer departure while this
            # command was running. Its request generation remains outstanding.
            if self._finished_requested == requested:
                if pending:
                    self._finished_retry_remaining = max(0, self._finished_retry_remaining - 1)
                    self._finished_retry_at = self._finished_completed_at + self._finished_retry_delay
                    self._finished_retry_delay = min(60.0, self._finished_retry_delay * 2)
                else:
                    self._finished_retry_remaining = 0
        self.kick.set()

    def src_share(self):
        rows = self.slurm.share()
        with self.store.lock:
            self.store.share = rows
        if not self.account and rows:
            self.account = rows[0]["account"]

    def src_account(self):
        """Everyone's jobs in the account (the Group tab) and the account's load (the header)."""
        jobs = self.slurm.group(self.account)
        with self.store.lock:
            jobs = self._keep_rejected("account", jobs, self.store.group)
        running = [j for j in jobs if not j.pending]
        info = dict(account=self.account, running=len(running), cpus=sum(j.cpus for j in running), gpus=sum(j.gpus for j in running),
                    pending=sum(1 for j in jobs if j.pending))
        with self.store.lock:
            self.store.group, self.store.account = jobs, info

    def default_probes(self) -> List[dict]:
        """One probe per GPU type of each GPU partition, one per CPU partition that has jobs of mine."""
        mine = {j.partition for j in self.store.jobs}
        out = []
        for p in self.store.partitions:
            if p.gpus:
                for t in sorted(p.gpus):
                    out.append(dict(partition=p.name, gres=f"gpu:{t}:1", cpus=8, mem="32G", limit="01:00:00"))
            elif p.name in mine:
                out.append(dict(partition=p.name, gres="", cpus=16, mem="64G", limit="01:00:00"))
        return out[:8]

    def src_weather(self):
        """Pending work ahead per partition cluster-wide, and sbatch --test-only for a few typical jobs."""
        if self.collection is not None:
            return
        ahead = self.slurm.pending_ahead()
        probes = self.probes or self.default_probes()
        results = []
        for pr in probes:
            r = self.slurm.probe(pr.get("partition", "main"), pr.get("gres", ""), int(pr.get("cpus", 8)), str(pr.get("mem", "32G")), str(pr.get("limit", "01:00:00")),
                                 account=self.account if pr.get("account", True) else "")
            r["t"] = time.time()
            results.append(r)
        with self.store.lock:
            self.store.pending_ahead, self.store.weather = ahead, results

    def src_budget(self):
        info = self.slurm.budget(self.account)
        with self.store.lock:
            self.store.budget = info

    @staticmethod
    def _telemetry_identity(binding):
        binding = binding or {}
        launch = binding.get("launch", {})
        launch = launch if isinstance(launch, dict) else {}
        return tuple(binding.get(key) for key in ("project_id", "experiment_id", "run_root", "run_id", "attempt", "job_id", "remote_identity")) + tuple(launch.get(key) for key in ("cluster", "restart_count", "submission_token"))

    def bind_telemetry(self, binding):
        """Publish one exact project binding; all disk reads remain in src_trace."""
        binding = deepcopy(binding) if binding else None
        with self._schedule_lock, self.store.lock:
            prior = self.telemetry_binding
            if self._telemetry_identity(prior) != self._telemetry_identity(binding):
                self.retained_telemetry = {}
                if prior and prior.get("job_id"):
                    self.store.trace.pop(prior["job_id"], None)
                self._trace_requested += 1
                # Recovery belongs to the exact file binding. A new run gets
                # one immediate attempt; the shared ledger still retains all
                # endpoint budgets and circuits for actual admitted operations.
                health = self.health("trace")
                health.backoff, health.error = 0., ""
                self._collection_failures.pop("trace", None)
                self._collection_deferred.discard("trace")
                self._collection_not_before["trace"] = 0.
                self._baseline_completed.pop("trace", None)
                self.kick.set()
            self.telemetry_binding = binding

    def trace_path(self, jid: str) -> str:
        """Find the exact live, completed or departed job's trace without inspection."""
        import os
        if not isinstance(jid, str) or not re.fullmatch(r"[0-9]+(?:_[0-9]+)?(?:\+[0-9]+)?", jid):
            return ""
        record, kv = self.store.record_context(jid)
        # The delimited queue/accounting path preserves spaces. parse_kv's
        # older whitespace-delimited inspection path may have truncated them.
        for wd in (getattr(record, "workdir", ""), kv.get("WorkDir", "")):
            if isinstance(wd, str) and len(wd) <= 4096 and os.path.isabs(wd) and "\0" not in wd:
                return os.path.join(wd, "logs", f"gpu-util-{jid}.csv")
        return ""

    def src_trace(self):
        """Read selected retained CPU/GPU recordings and optional NVIDIA CSVs."""
        from .raven_collection import Deferred
        if self.collection is not None and not self._collection_needed("trace"):
            return
        with self.store.lock:
            want = [j.id for j in self.store.jobs if not j.pending and j.gpus]
        if self.want_trace and self.want_trace not in want:
            want.append(self.want_trace)
        binding = self.telemetry_binding
        if binding and binding.get("job_id") and binding["job_id"] not in want:
            want.append(binding["job_id"])
        if self.collection is not None:
            subscribed = {self.want_trace, self._collection_demand["selected"]}
            want = [jid for jid in want if jid in subscribed][:2]
        selected = self._sampling_targets("trace", want)
        with self.store.lock:
            contexts = {jid: self._trace_context_locked(jid) for jid in selected}
        traces, failures, deferrals = {}, [], {}
        statuses, retained_reads = {}, {}
        for jid in selected:
            try:
                binding = self.telemetry_binding
                if binding and binding.get("job_id") == jid:
                    from .projects import read_bound_telemetry
                    from .retained_telemetry import display_gpu_rows
                    retained = read_bound_telemetry(binding, files=self.files)
                    retained_reads[jid] = retained
                    traces[jid] = display_gpu_rows(retained["gpu"])
                    family_rows = {family: len(retained.get(family, ())) for family in ("cpu-memory", "gpu")}
                    errors = retained.get("errors", {})
                    reasons = [f"{family}: {reason}" for family, reason in errors.items()]
                    if not sum(family_rows.values()) and not reasons:
                        reasons = [item["reason"] for item in retained["index"]["sources"] if item.get("reason")]
                    status = {"path": binding["telemetry_index"],
                              "state": "partial" if errors and sum(family_rows.values()) else "error" if errors else "ready" if sum(family_rows.values()) else "empty",
                              "reason": "; ".join(reasons), "rows": len(traces[jid]),
                              "family_rows": family_rows, "sample_rows": sum(family_rows.values())}
                    statuses[jid] = status
                    continue
                path = self.trace_path(jid)
                status = {"path": path, "state": "missing_workdir", "reason": "The exact job has no valid absolute WorkDir.", "rows": 0}
                statuses[jid] = status
                if not path:
                    continue
                if not self.files.exists(path):
                    status.update(state="missing_file", reason="The job's optional NVIDIA trace file does not exist.")
                    continue
                rows = self.slurm.gpu_trace(path, self.files)
                if rows:
                    traces[jid] = rows
                    status.update(state="ready", reason="", rows=len(rows))
                else:
                    status.update(state="empty", reason="The trace is empty or contains no valid NVIDIA timestamp/index rows.")
            except (OSError, CommandError, ValueError) as exc:
                status = statuses.setdefault(jid, {})
                transient = exc if isinstance(exc, Deferred) else exc.__cause__ if isinstance(exc.__cause__, Deferred) else None
                status.update(state="deferred" if transient else "error", reason=str(exc))
                if transient:
                    deferrals[jid] = transient
                else:
                    failures.append(f"job {jid}: {status['reason']}")
            finally:
                self._sampling_complete("trace", jid)
        with self.store.lock:
            valid = {jid for jid in selected if contexts[jid] == self._trace_context_locked(jid)}
            retained = {jid: status for jid, status in self.trace_status.items() if jid in want}
            retained.update({jid: status for jid, status in statuses.items() if jid in valid})
            self.trace_status = retained
            self.retained_telemetry = {jid: value for jid, value in self.retained_telemetry.items()
                                       if jid in want and (jid not in valid or jid in deferrals)}
            self.retained_telemetry.update({jid: value for jid, value in retained_reads.items() if jid in valid})
            for jid, rows in traces.items():
                if jid in valid:
                    self.store.trace[jid] = rows
            # A fast tick for one requested job must retain untouched traces
            # and statuses for the other jobs on their original cadence.
            for k in [k for k in self.store.trace if k not in want or k in valid and k not in traces and k not in deferrals]:
                del self.store.trace[k]
        if failures:
            raise CommandError(f"Telemetry read failed for {len(failures)}/{len(want)} jobs: {failures[0]}")
        if deferrals:
            raise max(deferrals.values(), key=lambda value: value.retry_after)

    def _trace_context_locked(self, jid):
        record, _ = self.store.record_context(jid)
        return (self.store.job_attempt(jid), type(record), self._metric_attempt(record),
                getattr(record, "state", None), self.trace_path(jid),
                self._telemetry_identity(self.telemetry_binding))

    def src_fin_details(self):
        jid = self.want_fin
        if not jid or jid in self.store.fin_steps:
            return
        with self.store.lock:
            record = next((job for job in self.store.finished if job.id == jid), None)
            attempt = self.store.job_attempt(jid)
        reader = getattr(self.slurm, "record_fin_steps", None)
        steps = reader(record) if callable(reader) and record is not None else self.slurm.fin_steps(jid)
        with self.store.lock:
            if self.want_fin != jid or attempt != self.store.job_attempt(jid):
                return
            self.store.fin_steps[jid] = steps
            if len(self.store.fin_steps) > 50:
                self.store.fin_steps.pop(next(iter(self.store.fin_steps)))

    def src_details(self):
        jid = self.want_detail
        if not jid:
            return
        with self.store.lock:
            finished = next((job for job in self.store.finished if job.id == jid), None)
            selected_record = finished or next((job for job in self.store.jobs if job.id == jid), None)
            previous = dict(self.store.details.get(jid, {}))
            attempt = self.store.job_attempt(jid)
            queue_publication = self.store._queue_publication
        record_reader = getattr(self.slurm, "record_details", None)
        detail_identity = (attempt, *(getattr(selected_record, key, "") for key in ("submit", "start", "user", "cluster")))
        if finished is None:
            kv = record_reader(selected_record) if callable(record_reader) and selected_record else self.slurm.details(jid)
        else:
            # Successful completed-job paths are immutable. Missing accounting
            # paths can lag completion, so retry them on the normal cadence or a
            # manual refresh. Keep real paths fetched while the job was alive.
            kv = self._historical_details.get(jid)
            if self._historical_detail_identities.get(jid) != detail_identity:
                kv = None
            if kv is None:
                seed = dict(previous)
                verify = getattr(self.slurm, "detail_identity_error", None)
                if callable(verify) and verify(finished, seed):
                    seed = {}
                seed.pop("LogPathError", None)
                seed.update({"JobId": jid, "JobName": finished.name, "JobState": finished.state})
                if finished.workdir and not seed.get("WorkDir"):
                    seed["WorkDir"] = finished.workdir

                def known_path() -> bool:
                    return any(isinstance(seed.get(key), str) and seed[key] and
                               seed[key].lower() not in ("unknown", "n/a", "(null)", "none")
                               for key in ("StdOut", "StdErr"))

                if not known_path():
                    try:
                        control = record_reader(finished) if callable(record_reader) else self.slurm.details(jid)
                    except CommandError:
                        control = {}
                    seed.update(control)
                    if not known_path():
                        seed.update(record_reader(finished, historical=True) if callable(record_reader)
                                    else self.slurm.historical_details(jid))
                kv = seed
                if known_path():
                    self._historical_details[jid] = dict(kv)
                    self._historical_detail_identities[jid] = detail_identity
                    if len(self._historical_details) > 50:
                        self._historical_details.pop(next(iter(self._historical_details)))
                    self._historical_detail_identities = {key: value for key, value in self._historical_detail_identities.items()
                                                           if key in self._historical_details}
        with self.store.lock:
            # An in-flight fetch for an old cursor must not evict or overwrite
            # the newly selected job's details.
            if jid != self.want_detail:
                return
            if attempt != self.store.job_attempt(jid):
                self._historical_details.pop(jid, None)
                return
            if finished is not None and any(job.id == jid for job in self.store.jobs):
                # A historical fetch can finish after this ID has been requeued.
                # It must not replace the active attempt's metadata or paths.
                self._historical_details.pop(jid, None)
                return
            current_finished = next((job for job in self.store.finished if job.id == jid), None)
            current_record = current_finished or next((job for job in self.store.jobs if job.id == jid), None)
            if detail_identity != (attempt, *(getattr(current_record, key, "") for key in ("submit", "start", "user", "cluster"))):
                return
            if current_finished is not None:
                kv = dict(kv, JobState=current_finished.state)
            self.store.details[jid] = kv
            self.store.bind_series_scheduler(jid, kv, queue_publication=queue_publication)
            alive = {j.id for j in self.store.jobs}
            retained = set(self._historical_details) | self.store.preserved_detail_ids()
            for k in [k for k in self.store.details if k not in alive and k not in retained and k != jid]:
                del self.store.details[k]
        with self._schedule_lock:
            if self._details_refresh == (jid, attempt):
                self._details_refresh = None

    def select(self, jid: Optional[str]):
        """Fetch new selection promptly while preserving a failing source's deadline."""
        with self._schedule_lock:
            if jid != self.want_detail:
                self.want_detail = jid
                if not self.health("details").backoff:
                    self.last_run["details"] = 0.0
                self.kick.set()

    def select_fin(self, jid: Optional[str]):
        """A finished job whose steps the details overlay shows (sacct -j, once)."""
        with self._schedule_lock:
            if jid != self.want_fin:
                self.want_fin = jid
                if not self.health("fin_details").backoff:
                    self.last_run["fin_details"] = 0.0
                self.kick.set()

    def select_trace(self, jid: Optional[str]):
        with self._schedule_lock:
            if jid != self.want_trace:
                self.want_trace = jid
                self._trace_requested += 1
                self.kick.set()
