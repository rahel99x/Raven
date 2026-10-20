"""The data model: typed records for jobs, live statistics, GPUs, nodes, partitions and finished jobs; the
thread-safe store with histories, transitions, events, per-source health and persistence."""
from __future__ import annotations

import collections
import json
import math
import os
import re
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field, is_dataclass, replace
from functools import lru_cache
from typing import Deque, Dict, List, Optional, Sequence

from . import clock
from raven.recordings.archive import SeriesArchive
from .history_snapshots import HistoryDeque, immutable_histories
from raven.recordings.publication import SeriesDeque
from raven.recordings.cache_limits import MAX_SERIES_CACHE_ENTRIES, MAX_SERIES_CACHE_BYTES
from . import ui_state


# ------------------------------------------------------------------------------------------------ parsing helpers
def secs(s: Optional[str]) -> Optional[float]:
    """[DD-]HH:MM:SS[.f] | MM:SS | 'UNLIMITED' -> seconds (None if unknown)."""
    if type(s) is str and len(s) <= 256:
        return _cached_secs(s)
    return _parse_secs(s)


@lru_cache(maxsize=8192)
def _cached_secs(s):
    return _parse_secs(s)


def _parse_secs(s):
    if not s or s in ("UNLIMITED", "N/A", "INVALID", "Unknown", "-", "NOT_SET", "PARTITION_TIME_LIMIT"):
        return None
    d = 0
    try:
        if "-" in s:
            d, s = s.split("-", 1)
            d = int(d)
        parts = [float(p) for p in s.split(":")]
    except ValueError:
        return None
    if len(parts) == 3:
        return int(d) * 86400 + parts[0] * 3600 + parts[1] * 60 + parts[2]
    if len(parts) == 2:
        return int(d) * 86400 + parts[0] * 60 + parts[1]
    return parts[0]


def hms(seconds: Optional[float]) -> str:
    if seconds is None:
        return "?"
    seconds = int(seconds)
    d, r = divmod(seconds, 86400)
    h, r = divmod(r, 3600)
    m, s = divmod(r, 60)
    return (f"{d}-" if d else "") + f"{h:02d}:{m:02d}:{s:02d}"


def short_duration(seconds: Optional[float]) -> str:
    """1d 02h | 3h 04m | 12m | 45s."""
    if seconds is None:
        return "?"
    seconds = int(max(0, seconds))
    d, r = divmod(seconds, 86400)
    h, r = divmod(r, 3600)
    m, s = divmod(r, 60)
    if d:
        return f"{d}d {h:02d}h"
    if h:
        return f"{h}h {m:02d}m"
    if m:
        return f"{m}m {s:02d}s"
    return f"{s}s"


def compact(seconds: Optional[float]) -> str:
    """12m | 3h04m | 1d03h | 45s: one or two units."""
    if seconds is None:
        return "?"
    seconds = int(max(0, seconds))
    d, r = divmod(seconds, 86400)
    h, r = divmod(r, 3600)
    m, s = divmod(r, 60)
    if d:
        return f"{d}d{h:02d}h"
    if h:
        return f"{h}h{m:02d}m"
    if m:
        return f"{m}m"
    return f"{s}s"


def nbytes(s: Optional[str]) -> float:
    """12345K, 1.5G, 800M, 32G, 64Gn, 4000Mc -> bytes."""
    if not s:
        return 0.0
    unit = "".join(c for c in s.upper() if c.isalpha())[:1]
    try:
        v = float("".join(c for c in s if c.isdigit() or c == "."))
    except ValueError:
        return 0.0
    return v * {"K": 1024, "M": 1024 ** 2, "G": 1024 ** 3, "T": 1024 ** 4}.get(unit, 1)


def human(b: float) -> str:
    for unit, div in (("TB", 1024 ** 4), ("GB", 1024 ** 3), ("MB", 1024 ** 2)):
        if b >= div:
            return f"{b / div:.1f} {unit}"
    return f"{b / 1024:.0f} KB"


def fnum(s) -> float:
    try:
        return float(s)
    except (TypeError, ValueError):
        return 0.0


def fint(s) -> int:
    try:
        return int(float(s))
    except (TypeError, ValueError):
        return 0


def stamp(s: Optional[str]) -> Optional[float]:
    """Slurm time 'YYYY-MM-DDTHH:MM:SS' -> epoch seconds."""
    if type(s) is str and len(s) <= 256:
        # mktime interprets scheduler dates in the current local timezone.
        # Include both TZ and the effective libc/Python timezone state so
        # tzset or a runtime environment change cannot reuse old epochs.
        zone = (os.environ.get("TZ"), time.tzname, time.timezone,
                getattr(time, "altzone", time.timezone), time.daylight)
        return _cached_stamp(s, zone)
    return _parse_stamp(s)


@lru_cache(maxsize=8192)
def _cached_stamp(s, zone):
    return _parse_stamp(s)


def _parse_stamp(s):
    try:
        if isinstance(s, str) and (s.endswith("Z") or "+" in s[10:] or "-" in s[10:]):
            from datetime import datetime
            return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
        return time.mktime(time.strptime(s, "%Y-%m-%dT%H:%M:%S"))
    except (TypeError, ValueError):
        return None


def when(s: Optional[str]) -> str:
    """'YYYY-MM-DDTHH:MM:SS' -> 'HH:MM' today, else 'MM-DD HH:MM'."""
    if not s or s in ("N/A", "Unknown", "NONE"):
        return "n/a"
    try:
        day, clock_ = s.split("T")
        return clock_[:5] if day == clock.today() else f"{day[5:]} {clock_[:5]}"
    except ValueError:
        return s


def gres_gpus(s: Optional[str]):
    """Read GPU GRES and TRES, including typed, aggregate and mixed resources.

    An aggregate ``gres/gpu=`` is already the job total. Its typed TRES
    entries describe that same allocation and must not be counted twice.
    Socket/index annotations describe placement, rather than extra GPUs.
    """
    if not s or len(s) > 65536:
        return ("", 0)
    total, aggregate, types = 0, None, set()
    for piece in re.sub(r"\([^)]*\)", "", s).split(","):
        match = re.fullmatch(r"(?:gres[/:])?gpu(?P<body>(?::[^\s,=]+)?)(?:=(?P<count>[0-9]+))?", piece.strip())
        if match is None:
            continue
        bits = match.group("body").lstrip(":").split(":") if match.group("body") else []
        raw = match.group("count")
        if raw is None and bits and bits[-1].isascii() and bits[-1].isdigit():
            raw = bits.pop()
        if raw is None:
            if len(bits) != 1 or bits[0].startswith(("-", "+")):
                continue
            count = 1
        elif len(raw) > 20:
            continue
        else:
            count = int(raw)
        kind = ":".join(bits)
        if kind and count:
            types.add(kind)
        if match.group("count") is not None and not kind:
            aggregate = count
        else:
            total += count
    return (next(iter(types)) if len(types) == 1 else "mixed" if types else "", aggregate if aggregate is not None else total)


def gpus_in_tres(tres: str) -> int:
    """Count allocated GPUs even when Slurm exposes only typed GPU TRES."""
    return gres_gpus(tres)[1]


def mem_request_bytes(mem_req: str, cpus: int, nodes: int) -> float:
    req = nbytes(mem_req)
    if req and mem_req.endswith("c"):
        req *= max(cpus, 1)
    elif req and mem_req.endswith("n"):
        req *= max(nodes, 1)
    return req


# ------------------------------------------------------------------------------------------------ records
@dataclass
class Job:
    id: str
    name: str
    partition: str
    state: str                       # RUNNING PENDING COMPLETING CONFIGURING SUSPENDED ...
    elapsed: str = "0:00"
    limit: str = ""
    nodes: int = 1
    cpus: int = 0
    gpu_type: str = ""
    gpus: int = 0                    # over all nodes
    nodelist: str = ""
    mem_req: str = ""
    start: str = ""
    submit: str = ""
    reason: str = ""
    priority: int = 0
    dependency: str = ""
    account: str = ""
    qos: str = ""
    end: str = ""                    # expected end
    command: str = ""
    est_start: str = ""              # squeue --start
    hosts: List[str] = field(default_factory=list)
    user: str = ""                   # set for the account-wide listing
    workdir: str = ""                # squeue WorkDir; trailing fields preserve positional callers
    comment: str = ""                # optional explicit launch marker from squeue
    cluster: str = ""
    restarts: Optional[int] = None   # explicit scheduler counter, never inferred zero
    collection_stale: bool = False

    @property
    def pending(self) -> bool:
        return self.state == "PENDING"

    @property
    def held(self) -> bool:
        return self.reason in ("JobHeldUser", "JobHeldAdmin")

    @property
    def elapsed_s(self) -> Optional[float]:
        return secs(self.elapsed)

    @property
    def limit_s(self) -> Optional[float]:
        return secs(self.limit)

    @property
    def mem_bytes(self) -> float:
        return mem_request_bytes(self.mem_req, self.cpus, self.nodes)

    @property
    def gpu_text(self) -> str:
        return f"{self.gpus}x{self.gpu_type or 'gpu'}" if self.gpus else ""


@dataclass
class Live:
    cpu_time: Optional[float] = None     # observed CPU seconds: sum of sstat AveCPU * NTasks across application steps
    rss: Optional[float] = None          # peak resident set, bytes
    avg: Optional[float] = None          # CPU efficiency so far: cpu_time / (elapsed * cpus)
    rate: Optional[float] = None         # CPU rate between the last two samples, per core
    t: float = 0.0


@dataclass
class GpuSample:
    node: str
    index: int
    util: Optional[float]                # percent; None when the device does not expose the counter
    used: Optional[float]                # MiB
    total: Optional[float]               # MiB
    name: str = ""
    vendor: str = ""                    # nvidia / amd / intel; blank in legacy recordings
    uuid: str = ""
    bdf: str = ""
    partition: str = ""

    @property
    def device_key(self) -> str:
        """Stable archive identity without changing legacy-recording keys."""
        if not self.vendor:
            return f"{self.node}:{self.index}"
        identity = self.uuid or self.bdf or str(self.index)
        key = f"{self.node}:{self.vendor}:{identity}:{self.partition}"
        if len(key) <= 128 and ":" not in str(self.node) and ":" not in self.partition:
            return key
        # Research snapshots bound keys to 128 characters. Preserve identity
        # under that bound instead of allowing truncation to merge devices.
        import hashlib
        encoded = json.dumps([self.node, self.vendor, identity, self.partition], separators=(",", ":"))
        return f"{self.vendor}:sha256:{hashlib.sha256(encoded.encode()).hexdigest()}"


@dataclass
class Node:
    name: str
    state: str = ""
    cpus: int = 0
    alloc: int = 0
    load: Optional[float] = None          # unknown is distinct from an idle node
    mem_total: float = 0.0               # MiB
    mem_free: Optional[float] = None      # MiB; zero is a real, exhausted-memory reading
    gres: str = ""
    gres_used: str = ""
    partitions: str = ""


@dataclass
class Partition:
    name: str
    avail: str = ""
    limit: str = ""
    nodes: int = 0
    nodes_aiot: str = ""                 # allocated/idle/other/total
    cpus_aiot: str = ""
    gpus: Dict[str, Dict[str, int]] = field(default_factory=dict)   # type -> total/used/down/free


@dataclass
class Finished:
    id: str
    name: str = ""
    state: str = ""
    elapsed: str = ""
    cpus: int = 0
    nodes: int = 1
    gpus: int = 0
    cpu_time: Optional[float] = None
    req_mem: float = 0.0
    rss: float = 0.0
    start: str = ""
    end: str = ""
    partition: str = ""
    exit: str = ""
    nodelist: str = ""
    submit: str = ""
    workdir: str = ""
    limit: str = ""
    user: str = ""                   # persisted accounting provenance for cold-start grouping
    account: str = ""
    cluster: str = ""
    comment: str = ""
    command: str = ""                # retained/imported provenance; sacct need not expose it
    restarts: Optional[int] = None
    collection_stale: bool = False

    @property
    def cpu_eff(self) -> Optional[float]:
        el = secs(self.elapsed) or 0
        return (self.cpu_time / (el * self.cpus)) if (self.cpu_time is not None and el and self.cpus) else None

    @property
    def mem_eff(self) -> Optional[float]:
        return (self.rss / self.req_mem) if self.req_mem else None

    @property
    def core_hours(self) -> float:
        return (secs(self.elapsed) or 0) * self.cpus / 3600

    @property
    def gpu_hours(self) -> float:
        return (secs(self.elapsed) or 0) * self.gpus / 3600


@dataclass
class Step:
    """One step of a running job (sstat) or of a finished one (sacct -j)."""
    id: str                          # 12345.batch, 12345.0 ...
    name: str = ""
    cpu_time: Optional[float] = None
    rss: float = 0.0                 # peak over tasks, bytes
    rss_task: str = ""               # the task and node holding the peak
    rss_node: str = ""
    ntasks: int = 0
    min_cpu: Optional[float] = None  # the least CPU time of any task: the slowest rank
    min_cpu_task: str = ""
    min_cpu_node: str = ""
    ave_rss: float = 0.0
    state: str = ""
    elapsed: str = ""
    exit: str = ""
    nodelist: str = ""


@dataclass
class NodeCell:
    """One node of the cluster-wide map (sinfo -N)."""
    name: str
    partitions: List[str] = field(default_factory=list)
    state: str = ""
    cpus_alloc: int = 0
    cpus_idle: int = 0
    cpus_other: int = 0
    cpus: int = 0
    mem: float = 0.0                 # MiB
    mem_alloc: float = 0.0
    gpu_type: str = ""
    gpus: int = 0
    gpus_used: int = 0

    @property
    def down(self) -> bool:
        return any(k in self.state for k in ("down", "drain", "drng", "fail", "maint", "inval")) or self.state.endswith("*")


@dataclass
class Health:
    name: str
    enabled: bool = True
    calls: int = 0
    errors: int = 0
    last_ok: float = 0.0
    last_try: float = 0.0
    latency_ms: float = 0.0
    error: str = ""
    backoff: float = 0.0
    inflight: bool = False


def to_plain(obj):
    if is_dataclass(obj):
        return asdict(obj)
    if isinstance(obj, dict):
        return {str(k): to_plain(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, collections.deque)):
        return [to_plain(v) for v in obj]
    return obj


# Only these accounting states establish that an individual attempt ended. Queue
# disappearance, requeue states, and unknown future Slurm states do not.
TERMINAL_STATES = frozenset(("COMPLETED", "FAILED", "TIMEOUT", "OUT_OF_MEMORY", "NODE_FAIL", "BOOT_FAIL",
                             "PREEMPTED", "CANCELLED", "DEADLINE", "REVOKED"))
COMPLETION_KEEP = 256
TRANSITION_KEEP = 128


def terminal_state(state: str) -> str:
    return str(state or "").split()[0].rstrip("+").upper() if str(state or "").strip() else ""


# ------------------------------------------------------------------------------------------------ the store
class Store:
    """Everything the screen reads; updated by the sampler under a lock.  ``snapshot()`` is a shallow copy."""

    def _notify_ui(self):
        # Called under the Store lock: admit only a nonblocking, callback-free
        # wake primitive, never arbitrary embedding or plugin code.
        if self.ui_wakeup is None:
            return
        from .ui_wakeup import notify
        notify(self)

    def __init__(self, state_dir: Optional[str] = None, persist: bool = True, max_events: int = 200, series_keep: int = 4000, *, raven_limits=None, read_only=False, strict_owner=False, source_scope=None, series_storage="jsonl"):
        self.raven_limits = raven_limits
        self._ui_revision = 0
        self.started_seq = 0
        if series_storage not in ("jsonl", "blocks-v1"):
            raise ValueError("Series storage must be jsonl or blocks-v1")
        self.series_storage = series_storage
        self._series_format_cache = {}
        self._storage_cancel = threading.Event()
        self.ui_wakeup = None
        self.lock = threading.RLock()
        self.jobs: List[Job] = []
        self.live: Dict[str, Live] = {}
        self.gpu: Dict[str, Optional[List[GpuSample]]] = {}
        self.nodes: Dict[str, Node] = {}
        self.partitions: List[Partition] = []
        self.gpu_inventory: Dict[str, Dict[str, int]] = {}
        self.finished: List[Finished] = []
        # An exact copy of the last queue observation, never a synthetic COMPLETED
        # record. These IDs remain usable for selection and logs while sacct lags.
        self.departed_jobs: Dict[str, Job] = {}
        self.job_transitions: Deque[dict] = collections.deque(maxlen=TRANSITION_KEEP)
        self.history_revision = 0
        self._transition_seq = 0
        self._jobs_loaded = False
        self._accounting_jobs: Dict[str, Finished] = {}
        self._recent_finished: Dict[str, Finished] = {}
        self._stale_finished: Dict[str, tuple] = {}
        self._job_attempts: Dict[str, int] = {}
        self.share: List[dict] = []
        self.account: dict = {}
        self.group: List[Job] = []                         # everyone's jobs in the account
        self.weather: List[dict] = []                      # queue weather probes (partition, gres, pending ahead, projected start)
        self.pending_ahead: Dict[str, dict] = {}           # partition -> pending jobs / cpus / gpus cluster-wide
        self.budget: dict = {}                             # the account's allocation: used, limit, burn rate
        self.nodemap: Dict[str, NodeCell] = {}             # every node of the cluster
        self.steps: Dict[str, List[Step]] = {}             # per running job, from sstat
        self.fin_steps: Dict[str, List[Step]] = {}         # per finished job, from sacct -j (on demand)
        self.trace: Dict[str, List[dict]] = {}             # per job, the GPU trace CSV written inside the job (t, index, util, mem)
        self.details: Dict[str, dict] = {}
        self.health: Dict[str, Health] = {}
        self.events: Deque[dict] = collections.deque(maxlen=max_events)
        self.hist_cpu: Dict[str, Deque[float]] = collections.defaultdict(HistoryDeque)
        self.hist_gpu: Dict[str, Deque[float]] = collections.defaultdict(HistoryDeque)
        self.series: Dict[str, Deque[dict]] = collections.defaultdict(lambda: SeriesDeque(maxlen=series_keep, lock=self.lock))
        self.series_keep = series_keep
        self._series_scope = object()
        from raven.recordings.identity import default_scope, fingerprint
        self._recording_scope = fingerprint(source_scope if source_scope is not None else default_scope(state_dir))
        self._recording_session = uuid.uuid4().hex
        self._recording_identities = {}
        self._recording_observations = {}
        self._recording_evidence = {}
        self._recording_bindings = {}
        self._queue_publication = 0
        self._sample_recording_proofs = {}
        self._isolated_recording_paths = {}
        self._series_aliases = {}
        self._series_rekeys = collections.deque()
        self._publications = collections.OrderedDict()
        self._series_loaded: set = set()
        self.gpu_mean: Dict[str, List[float]] = collections.defaultdict(lambda: [0.0, 0])
        self.prev_cpu: Dict[str, tuple] = {}
        self.seen: Dict[str, str] = {}                     # id -> last state seen (for transitions)
        self.names: Dict[str, str] = {}
        self.t_jobs = 0.0
        self.error = ""
        self.tags: Dict[str, dict] = {}                   # job id -> {"tags": [...], "pinned": bool, "note": ""}
        self.alerts = None                                 # the AlertEngine (set by the cli)
        self.state_dir = state_dir
        if self.raven_limits is not None and self.raven_limits.state_dir is None:
            self.raven_limits.state_dir = state_dir
        self.persist = persist and bool(state_dir)
        from .state_ownership import StateOwner
        self._state_owner = StateOwner(state_dir, read_only=read_only, strict=strict_owner) if self.persist else None
        self.state_status = self._state_owner.status if self._state_owner else {"state": "disabled", "message": ""}
        self._tags_io_lock = threading.Lock()
        self._tags_dirty = False
        self._tags_revision = 0
        self._tags_last_try = 0.0
        self.tags_status = {"state": "ready", "message": ""}
        self._tags_load_error = ""
        self._ui_io_lock = threading.Lock()
        self.ui_state_status = {"state": "unread", "message": ""}
        self._series_archive = SeriesArchive(self)
        # Publication order is captured under Store.lock. The IO lock and queue
        # lock never acquire Store.lock, so slow storage cannot block readers.
        self._persist_queue = collections.deque()
        self._persist_event_queue = collections.deque()
        self._persist_queue_lock = threading.Lock()
        self._persist_io_lock = threading.Lock()
        self._persist_event_io_lock = threading.Lock()
        self._persist_submit = None
        self._persist_writer_lock = threading.RLock()
        self._persist_writer_future = None
        self._persist_writer_pending = False
        self._series_last_flush = time.monotonic()
        self._series_pending_counts = {}
        self._series_batch_ready = False
        self._record_lookup = {}
        self._record_lookup_signature = None
        self._events_path = os.path.join(state_dir, "events.jsonl") if self.persist else None
        if self.persist:
            self._load_events()
            self._load_tags()
        self._sync_pinned_limits()

    def _sync_pinned_limits(self):
        if self.raven_limits:
            self.raven_limits.pinned_jobs = frozenset(jid for jid, value in self.tags.items() if value.get('pinned'))

    # ---- tags, pins, notes ---------------------------------------------------------------------------
    def _load_tags(self):
        try:
            with open(os.path.join(self.state_dir, "tags.json")) as f:
                data = json.loads(f.read((2 << 20) + 1))
                if f.tell() > 2 << 20:
                    raise ValueError("Tags exceed the 2 MiB read limit")
            if isinstance(data, dict):
                self.tags = {k: v for k, v in data.items() if isinstance(v, dict)}
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as exc:
            self._tags_load_error = "Existing tags could not be read; preserved without replacement: " + str(exc)[:400]
            self.tags_status = {"state": "error", "message": self._tags_load_error}
            self._state_health("state tags", self._tags_load_error)

    @property
    def writable(self):
        return bool(self.persist and self._state_owner and self._state_owner.writable)

    def close(self):
        self._storage_cancel.set()
        try:
            self.configure_persistence_writer(None)
            # A finite maintenance child owns this lock until it is reaped.
            # Read-only sessions also wait; no helper outlives session close.
            with self._persist_io_lock:
                pass
            self.flush_persistence()
            if self._tags_dirty:
                self._save_tags()
        finally:
            if self._state_owner:
                self._state_owner.close()

    def _state_health(self, name, error=""):
        with self.lock:
            health = self.health.setdefault(name, Health(name))
            health.calls += 1
            health.last_try = clock.now()
            health.error = error
            if error:
                health.errors += 1
            else:
                health.last_ok = health.last_try
            self._ui_revision += 1
            self._notify_ui()

    def _save_tags(self):
        if not self.persist:
            return
        # Serialize first, then capture the newest publication. A delayed
        # older caller can never overwrite a newer snapshot. IO owns no Store lock.
        with self._tags_io_lock:
            self._tags_last_try = time.monotonic()
            try:
                if not self.writable:
                    raise OSError(self.state_status["message"])
                if self._tags_load_error:
                    raise OSError(self._tags_load_error)
                with self.lock:
                    # Check an upper bound before copying/serializing a large
                    # user note or imported document under the foreground lock.
                    size = 0
                    for key, value in self.tags.items():
                        size += len(key) + len(value.get("note", "")) + sum(len(tag) for tag in value.get("tags", ())) + 64
                        if size > 2 << 20:
                            raise ValueError("Tags, pins and notes exceed the 2 MiB persistence limit; in-memory edits are retained.")
                    snapshot = {key: dict(value, tags=list(value.get("tags", ()))) for key, value in self.tags.items()}
                    revision = self._tags_revision
                payload = json.dumps(snapshot, ensure_ascii=True, allow_nan=False).encode("utf-8")
                if len(payload) > 2 << 20:
                    raise ValueError("Tags, pins and notes exceed the 2 MiB persistence limit; in-memory edits are retained.")
                ui_state._atomic(os.path.join(self.state_dir, "tags.json"), payload)
                with self.lock:
                    if self._tags_revision == revision:
                        self._tags_dirty = False
                self.tags_status = {"state": "ready", "message": ""}
                self._state_health("state tags")
            except (OSError, ValueError, TypeError, RecursionError) as exc:
                message = "Could not save tags, pins and notes: " + str(exc)[:480] + "; edits remain in memory. Retry an edit or close after fixing storage."
                self.tags_status = {"state": "error", "message": message}
                self._state_health("state tags", message)

    def tag(self, jid: str, *tags: str, remove: bool = False) -> List[str]:
        with self.lock:
            self._tags_dirty = True
            self._tags_revision += 1
            self._ui_revision += 1
            self._notify_ui()
            rec = self.tags.setdefault(jid, dict(tags=[], pinned=False, note=""))
            for t in tags:
                t = t.lstrip("#").strip()
                if not t:
                    continue
                if remove:
                    rec["tags"] = [x for x in rec["tags"] if x != t]
                elif t not in rec["tags"]:
                    rec["tags"].append(t)
            if not rec["tags"] and not rec["pinned"] and not rec["note"]:
                del self.tags[jid]
                rec = dict(tags=[])
        self._request_persistence()
        return list(rec["tags"])

    def pin(self, jid: str, value: Optional[bool] = None) -> bool:
        with self.lock:
            self._tags_dirty = True
            self._tags_revision += 1
            self._ui_revision += 1
            self._notify_ui()
            rec = self.tags.setdefault(jid, dict(tags=[], pinned=False, note=""))
            rec["pinned"] = (not rec["pinned"]) if value is None else bool(value)
            out = rec["pinned"]
            self._sync_pinned_limits()
            if not rec["tags"] and not rec["pinned"] and not rec["note"]:
                del self.tags[jid]
        self._request_persistence()
        return out

    def note(self, jid: str, text: str) -> str:
        with self.lock:
            self._tags_dirty = True
            self._tags_revision += 1
            self._ui_revision += 1
            self._notify_ui()
            rec = self.tags.setdefault(jid, dict(tags=[], pinned=False, note=""))
            rec["note"] = text.strip()
            if not rec["tags"] and not rec["pinned"] and not rec["note"]:
                del self.tags[jid]
        self._request_persistence()
        return text.strip()

    def tag_many(self, ids, *tags, remove=False):
        """Apply one user edit atomically and persist its newest publication."""
        values = {}
        with self.lock:
            for jid in dict.fromkeys(ids):
                rec = self.tags.setdefault(jid, dict(tags=[], pinned=False, note=""))
                for tag in tags:
                    tag = tag.lstrip("#").strip()
                    if tag:
                        if remove:
                            rec["tags"] = [value for value in rec["tags"] if value != tag]
                        elif tag not in rec["tags"]:
                            rec["tags"].append(tag)
                values[jid] = list(rec["tags"])
                if not rec["tags"] and not rec["pinned"] and not rec["note"]:
                    self.tags.pop(jid)
            self._tags_dirty = True; self._tags_revision += 1; self._ui_revision += 1
            self._notify_ui()
        self._request_persistence()
        return values

    def pin_many(self, ids, value=None):
        ids = tuple(dict.fromkeys(ids))
        with self.lock:
            target = not all(self.tags.get(jid, {}).get("pinned", False) for jid in ids) if value is None else bool(value)
            for jid in ids:
                rec = self.tags.setdefault(jid, dict(tags=[], pinned=False, note=""))
                rec["pinned"] = target
                if not rec["tags"] and not rec["pinned"] and not rec["note"]:
                    self.tags.pop(jid)
            self._sync_pinned_limits()
            self._tags_dirty = True; self._tags_revision += 1; self._ui_revision += 1
            self._notify_ui()
        self._request_persistence()
        return target

    def configure_persistence_writer(self, submit):
        """Use the shared worker lane for interactive writes; no private pool."""
        if submit is None:
            with self._persist_writer_lock:
                future, self._persist_submit = self._persist_writer_future, None
            if future is not None:
                try:
                    future.result()
                except Exception as exc:
                    self._state_health('state persistence', 'Background writer stopped; draining pending edits: ' + str(exc)[:200])
            self.flush_persistence()
            if self._tags_dirty:
                self._save_tags()
        else:
            with self._persist_writer_lock:
                self._persist_submit = submit

    def _request_persistence(self, *, events_only=False):
        with self._persist_writer_lock:
            submit = self._persist_submit
            if submit is not None:
                self._persist_writer_pending = True
                if self._persist_writer_future is not None and not self._persist_writer_future.done():
                    return
                try:
                    self._persist_writer_future = submit(self._background_persistence)
                except RuntimeError as exc:
                    self.tags_status = {"state": "pending", "message": "Edits await a persistence worker: " + str(exc)[:200]}
                return
        if self._tags_dirty:
            self._save_tags()
        self.flush_persistence(_events_only=events_only)

    def _background_persistence(self):
        while True:
            with self._persist_writer_lock:
                if not self._persist_writer_pending:
                    self._persist_writer_future = None
                    return
                self._persist_writer_pending = False
            if self._tags_dirty:
                self._save_tags()
            self.flush_persistence()

    def tags_of(self, jid: str) -> List[str]:
        return list(self.tags.get(jid, {}).get("tags", []))

    def pinned(self, jid: str) -> bool:
        return bool(self.tags.get(jid, {}).get("pinned"))

    def tag_map(self) -> Dict[str, List[str]]:
        return {k: list(v.get("tags", [])) for k, v in self.tags.items()}

    # ---- events ------------------------------------------------------------------------------------
    def event(self, kind: str, text: str, job: Optional[Job] = None, *, _persist: bool = True, **extra) -> dict:
        ev = dict(t=clock.now(), kind=kind, text=text, job=(job.id if job else extra.pop("job_id", "")),
                  name=(job.name if job else extra.pop("name", "")), **extra)
        with self.lock:
            self.events.append(ev)
            if kind == "started":
                self.started_seq += 1
            self._ui_revision += 1
            self._notify_ui()
            self._queue_persistence(os.path.join(self.state_dir, "events.jsonl") if self.persist else None, ev)
        if _persist:
            self._request_persistence(events_only=True)
        return ev

    def _load_events(self):
        path = os.path.join(self.state_dir, "events.jsonl")
        if self.raven_limits:
            from raven.recordings.archive import read_tail
            events,status=read_tail(path,self.events.maxlen,max_bytes=2<<20,max_line=64<<10)
            self.events.extend(dict(event,old=True) for event in events)
            if status.get('status')=='limited': self.raven_limits.coverage['events']=status
            return
        try:
            with open(path) as f:
                lines = collections.deque(f, maxlen=self.events.maxlen)
        except OSError:
            return
        for line in lines:
            try:
                ev = json.loads(line)
                if not isinstance(ev, dict):
                    continue
                ev["old"] = True
                self.events.append(ev)
            except ValueError:
                pass

    # ---- transitions -------------------------------------------------------------------------------
    def apply_jobs(self, jobs: Sequence[Job], *, _persist: bool = True) -> List[dict]:
        """Store a fresh squeue listing; returns the transition events (queued, started, finished, held, released)."""
        out = []
        with self.lock:
            self._queue_publication += 1
            self._ui_revision += 1
            self._notify_ui()
            limited = False
            if self.raven_limits:
                jobs, limited = self.raven_limits.rows(jobs, 'jobs')
            now = {j.id: j for j in jobs}
            first = not self._jobs_loaded
            previous_jobs = {j.id: j for j in self.jobs}
            self._jobs_loaded = True
            for j in jobs:
                if j.id in self.departed_jobs or j.id in self._recent_finished:
                    prior = self._accounting_jobs.get(j.id) or self._recent_finished.get(j.id)
                    if prior is not None:
                        self._stale_finished[j.id] = self._finished_identity(prior)
                        self._bound(self._stale_finished)
                    self.departed_jobs.pop(j.id, None)
                    self._recent_finished.pop(j.id, None)
                    self._accounting_jobs.pop(j.id, None)
                    transition = self._transition("returned", j, j.state)
                    self._job_attempts[j.id] = transition["seq"]
                    # Paths and metadata belong to an attempt, even when Slurm
                    # reuses the ID. Keep them through departure/confirmation,
                    # then require a fresh scheduler record when it returns.
                    self.details.pop(j.id, None)
                    self.fin_steps.pop(j.id, None)
                prev = self.seen.get(j.id)
                if prev is None and not first:
                    out.append(self.event("queued" if j.pending else "started", f"{'queued' if j.pending else 'started'} {j.id} {j.name}", j, _persist=False))
                elif prev == "PENDING" and not j.pending:
                    out.append(self.event("started", f"started {j.id} {j.name}", j, _persist=False))
                elif prev is not None and prev != j.state and not j.pending and j.state in ("COMPLETING", "SUSPENDED"):
                    out.append(self.event("state", f"{j.state.lower()} {j.id} {j.name}", j, _persist=False))
                if prev is not None:
                    was_held = self.seen.get(j.id + ":held") == "1"
                    if j.held and not was_held:
                        out.append(self.event("held", f"held {j.id} {j.name}", j, _persist=False))
                    elif was_held and not j.held:
                        out.append(self.event("released", f"released {j.id} {j.name}", j, _persist=False))
                self.seen[j.id + ":held"] = "1" if j.held else "0"
            for jid, st in list(self.seen.items()):
                if ":" in jid:
                    continue
                if jid not in now:
                    if limited:
                        # A truncated observation cannot establish departure.
                        self.seen.pop(jid,None); self.seen.pop(jid+':held',None)
                        continue
                    kind = "finished" if st != "PENDING" else "left"
                    # Keep the historic event kind for plugins, but mark absence
                    # unconfirmed and never describe it as successful completion.
                    out.append(self.event(kind, f"left queue {jid} {self.names.get(jid, '')}", job_id=jid,
                                          name=self.names.get(jid, ""), confirmed=False, state="ACCOUNTING", _persist=False))
                    previous = previous_jobs.get(jid)
                    # Compressed pending array ranges are presentation groups, not
                    # accounting job identities; splitting a range is not a finish.
                    if previous is not None and "[" not in jid:
                        self.departed_jobs[jid] = replace(previous, hosts=list(previous.hosts))
                        self._bound(self.departed_jobs)
                        self._transition("departed", previous, "ACCOUNTING")
                    del self.seen[jid]
                    self.seen.pop(jid + ":held", None)
                    self.live.pop(jid, None)
                    self.gpu.pop(jid, None)
                    self.prev_cpu.pop(jid, None)
            for j in jobs:
                self.seen[j.id] = j.state
                self.names[j.id] = j.name
            # keep the projected starts already known
            old = {j.id: j.est_start for j in self.jobs}
            # Caller-owned records can still back an earlier render.
            # Publication never mutates those record objects.
            jobs = [replace(j, est_start=old.get(j.id, '')) if not j.est_start and old.get(j.id) else j for j in jobs]
            self.jobs = list(jobs)
            self._record_lookup_signature = None
            for job in jobs:
                self.capture_series_identity(job.id, record=job)
            if self.raven_limits:
                active={path for job in jobs if (path:=self._series_identity_path(self._recording_identities[job.id]))}
                active.update(self._series_identity_path(identity) for jid,identity in self._isolated_recording_paths.items() if jid in now)
                self.raven_limits.active_paths=frozenset(active)
                self._sync_pinned_limits()
            confirmed = False
            for jid in list(self.departed_jobs):
                candidate = self._accounting_jobs.get(jid)
                if candidate is not None:
                    confirmed = self._confirm_departure(jid, candidate) or confirmed
            # Accounting can arrive before the next queue poll. Active IDs stay
            # exclusively in Jobs until the scheduler actually removes them.
            # Do not rebuild/sort potentially large history on unchanged 2s polls.
            if confirmed or now.keys() != previous_jobs.keys():
                self._publish_finished()
            retained_attempts = set(now) | set(self.departed_jobs) | set(self._recent_finished)
            for jid in self._job_attempts.keys() - retained_attempts:
                del self._job_attempts[jid]
            if self.raven_limits:
                retained=retained_attempts | set(self.series) | {job.id for job in self.finished}
                for mapping in (self.names,self.live,self.gpu,self.steps,self.prev_cpu,
                                self.hist_cpu,self.trace,self.details,self.fin_steps):
                    for jid in mapping.keys()-retained:
                        del mapping[jid]
                # GPU histories use JOBID:DEVICE keys; preserve the device
                # identity while checking the owning job's retained lifetime.
                for mapping in (self.gpu_mean,self.hist_gpu):
                    for key in list(mapping):
                        if key.split(':',1)[0] not in retained:
                            del mapping[key]
            retained = retained_attempts | set(self.series) | {job.id for job in self.finished}
            for mapping in (self._recording_identities, self._recording_observations, self._recording_evidence, self._recording_bindings,
                            self._sample_recording_proofs, self._isolated_recording_paths):
                for jid in mapping.keys() - retained:
                    del mapping[jid]
            self.t_jobs = clock.now()
        if _persist:
            self.flush_persistence(_events_only=True)
        return out

    def job_attempt(self, jid: str) -> int:
        """Generation used to reject detail responses from a prior queue attempt."""
        with self.lock:
            return self._job_attempts.get(jid, 0)

    @staticmethod
    def _bound(mapping: dict) -> None:
        while len(mapping) > COMPLETION_KEEP:
            del mapping[next(iter(mapping))]

    @staticmethod
    def _finished_identity(job: Finished) -> tuple:
        return job.id, job.submit, job.start, job.end

    def _transition(self, kind: str, job, state: str, confirmed: bool = False) -> dict:
        self._transition_seq += 1
        event = dict(seq=self._transition_seq, kind=kind, job=job.id, name=job.name,
                     state=state, t=clock.now(), mono=time.monotonic(), confirmed=confirmed)
        self.job_transitions.append(event)
        return event

    def _matches_attempt(self, previous: Job, finished: Finished) -> bool:
        if previous.id != finished.id or terminal_state(finished.state) not in TERMINAL_STATES:
            return False
        if self._stale_finished.get(finished.id) == self._finished_identity(finished):
            return False
        # A requeued job keeps its ID. A prior attempt's accounting must not
        # complete the new attempt; pending StartTime can be a future estimate.
        end = stamp(finished.end)
        lower = stamp(previous.submit)
        if not previous.pending:
            started = stamp(previous.start)
            lower = max(lower or 0, started or 0) or None
        if end is not None and lower is not None and end < lower:
            return False
        started = stamp(previous.start) if not previous.pending else None
        actual = stamp(finished.start)
        return not (started is not None and actual is not None and actual < started)

    def _confirm_departure(self, jid: str, finished: Finished) -> bool:
        previous = self.departed_jobs.get(jid)
        if previous is None or not self._matches_attempt(previous, finished):
            return False
        del self.departed_jobs[jid]
        self._recent_finished[jid] = finished
        self._bound(self._recent_finished)
        self.history_revision += 1
        self._transition("history", finished, terminal_state(finished.state), confirmed=True)
        if jid in self.details:
            self.details[jid] = dict(self.details[jid], JobState=finished.state)
        return True

    def _publish_finished(self) -> None:
        active = {job.id for job in self.jobs}
        combined = {job.id: job for job in self.finished}
        combined.update(self._recent_finished)
        combined.update(self._accounting_jobs)
        self.finished = sorted((job for jid, job in combined.items() if jid not in active),
                               key=lambda job: job.end, reverse=True)
        if self.raven_limits:
            self.finished=self.finished[:self.raven_limits.values['max_history_rows']]
        self._record_lookup_signature = None

    def apply_finished(self, jobs: Sequence[Finished], history_days: Optional[float] = None) -> None:
        """Reconcile an accounting snapshot with observed departures under one lock.

        Initial history never animates. Confirmed live transitions increment
        ``history_revision`` once, even if sacct wins the race with squeue. Recent
        confirmations survive temporarily incomplete accounting snapshots.
        """
        with self.lock:
            self._ui_revision += 1
            self._notify_ui()
            if self.raven_limits:
                jobs, _ = self.raven_limits.rows(jobs, 'history')
            active = {job.id: job for job in self.jobs}
            incoming = {}
            for job in jobs:
                state = terminal_state(job.state)
                if state not in TERMINAL_STATES or "[" in job.id:
                    continue
                job = replace(job, state=state)
                previous = active.get(job.id) or self.departed_jobs.get(job.id)
                if previous is not None and not self._matches_attempt(previous, job):
                    continue
                if self._stale_finished.get(job.id) == self._finished_identity(job):
                    continue
                incoming[job.id] = job
            if history_days is not None:
                cutoff = clock.now() - max(0.0, history_days) * 86400
                for jid, job in list(self._recent_finished.items()):
                    ended = stamp(job.end)
                    if ended is not None and ended < cutoff:
                        del self._recent_finished[jid]
            self._accounting_jobs = incoming
            for jid in list(self.departed_jobs):
                if jid in incoming:
                    self._confirm_departure(jid, incoming[jid])
            # Keep fresh metrics/corrected state for confirmations already known.
            for jid in self._recent_finished:
                if jid in incoming:
                    self._recent_finished[jid] = incoming[jid]
            self.finished = []
            self._publish_finished()

    def preserved_detail_ids(self) -> set:
        """Bounded IDs whose original scheduler paths must survive queue removal."""
        with self.lock:
            return set(self.departed_jobs) | set(self._recent_finished)

    def apply_starts(self, starts: Dict[str, str]):
        with self.lock:
            self._ui_revision += 1
            self._notify_ui()
            self.jobs = [replace(job, est_start=starts[job.id]) if job.id in starts else job for job in self.jobs]
            self._record_lookup_signature = None

    # ---- per-job time series -----------------------------------------------------------------------
    def capture_series_identity(self, jid, *, record=None):
        """Capture before acquisition; the immutable token follows delayed writes."""
        from raven.recordings.identity import SeriesIdentity, fingerprint, scheduler_attempt
        with self.lock:
            if record is None:
                record, details = self.record_context(jid)
            else:
                details = self.details.get(jid, {})
            observation = (self._job_attempts.get(jid, 0), getattr(record, "submit", ""),
                           getattr(record, "start", ""), getattr(record, "cluster", ""), getattr(record, "restarts", None))
            binding = self._recording_bindings.get(jid)
            if binding and binding[0] != observation:
                self._recording_bindings.pop(jid, None)
                binding = None
            evidence = observation + tuple(details.get(key) for key in ("JobId", "ArrayJobId", "ArrayTaskId", "HetJobId", "HetJobOffset", "SubmitTime", "StartTime", "Restarts", "Cluster")) + (binding[1] if binding else None,)
            if self._recording_evidence.get(jid) == evidence and jid in self._recording_identities:
                return self._recording_identities[jid]
            verified = binding[1] if binding else scheduler_attempt(record, details)
            # An in-session observation change also invalidates a previously
            # verified identity; missing new details never reuse an old token.
            identity = SeriesIdentity(jid, self._recording_scope,
                verified or fingerprint((self._recording_session, jid, observation)), bool(verified))
            previous = self._recording_identities.get(jid)
            if previous is not None and previous != identity:
                prior_observation = self._recording_observations.get(jid)
                # An explicit zero restart count proves that the new detail
                # cannot refer to a same-second requeue. Other ambiguous
                # promotions remain isolated rather than relabelled.
                restart = getattr(record, 'restarts', None)
                restart = restart if restart is not None else details.get('Restarts')
                promote = (not previous.verified and identity.verified and prior_observation is not None and prior_observation[:4] == observation[:4]
                           and restart in (0, '0') and getattr(record, 'cluster', '') == observation[3])
                if promote:
                    old_path, new_path = self._series_identity_path(previous), self._series_identity_path(identity)
                    if old_path and new_path and old_path != new_path:
                        with self._persist_queue_lock:
                            self._series_aliases[old_path] = new_path
                            self._series_rekeys.append((old_path, new_path))
                else:
                    # Equal second-resolution timestamps alone cannot prove
                    # that a rapid requeue did not occur between observations.
                    self.series.pop(jid, None)
                    self.prev_cpu.pop(jid, None)
                    self.live.pop(jid, None)
                    self.gpu.pop(jid, None)
                    self.hist_cpu.pop(jid, None)
                    for key in tuple(self.hist_gpu):
                        if key.startswith(jid + ":"):
                            self.hist_gpu.pop(key, None)
                            self.gpu_mean.pop(key, None)
                    self._series_loaded.discard(jid)
                    self._series_archive.limited.discard(jid)
                    self._series_archive.states.pop(jid, None)
                    publication = self._publications.pop(jid, None)
                    if publication:
                        publication[0].release_publication()
            self._recording_identities[jid] = identity
            self._recording_observations[jid] = observation
            self._recording_evidence[jid] = evidence
            return identity

    def bind_series_scheduler(self, jid, details, *, queue_publication=None):
        """Called only after acquisition's queue-generation guard succeeded."""
        with self.lock:
            if queue_publication is not None and queue_publication != self._queue_publication:
                return False
            current = self.details.get(jid)
            if current != details:
                return False
            identity = self.capture_series_identity(jid)
            if identity.verified:
                self._sample_recording_proofs[jid] = self._queue_publication, identity
            return identity.verified

    def sample_recording_identity(self, jid, *, identity=None):
        """Separate fresh write authority from the previously verified graph.

        Legacy queue rows omit restart counters. A cached detail remains useful
        for viewing, but cannot authorize new verified appends after another
        queue publication; deferred samples have an explicit session-only file.
        """
        from raven.recordings.identity import SeriesIdentity, fingerprint
        with self.lock:
            identity = identity or self.capture_series_identity(jid)
            if not identity.verified:
                return identity
            record, _ = self.record_context(jid)
            structured = (record is not None and type(getattr(record,'restarts',None)) is int and 0 <= record.restarts <= 9999999999
                          and bool(getattr(record,'cluster','')))
            proof = self._sample_recording_proofs.get(jid)
            if structured or proof == (self._queue_publication,identity):
                return identity
            isolated = SeriesIdentity(jid,self._recording_scope,
                fingerprint((self._recording_session,jid,self._recording_observations.get(jid),'awaiting-fresh-proof',identity.attempt)),False)
            self._isolated_recording_paths[jid] = isolated
            if self.raven_limits:
                self.raven_limits.active_paths=self.raven_limits.active_paths|{self._series_identity_path(isolated)}
            return isolated

    def bind_series_attempt(self, jid, binding):
        """Bind a previously verified retained launch identity, never a job ID.

        The caller must have verified the manifest against its scheduler job.
        Required identity fields prevent accidental job-number-only binding.
        """
        from raven.recordings.identity import fingerprint
        required = ("run_root", "run_id", "attempt", "job_id", "submission_token", "cluster")
        if (not isinstance(binding, dict) or any(not isinstance(binding.get(key), str) or not binding[key] for key in required)
                or binding["job_id"] != jid):
            raise ValueError("A verified retained run requires run root, run ID, attempt, job ID, submission token and cluster.")
        with self.lock:
            self.capture_series_identity(jid)
            self._recording_bindings[jid] = (self._recording_observations[jid], fingerprint({key: binding[key] for key in required}))
            identity = self.capture_series_identity(jid)
            # The caller has just verified this exact retained-run binding.
            # That proof authorizes this publication, not future legacy queue
            # observations that could hide a same-second scheduler restart.
            self._sample_recording_proofs[jid] = self._queue_publication, identity
            return identity

    def _series_identity_path(self, identity):
        if not self.persist or not identity.filename:
            return None
        filename = identity.filename[:-6] + ".rvb" if self.series_storage == "blocks-v1" else identity.filename
        return os.path.join(self.state_dir, "series", filename)

    def _series_path(self, jid: str) -> Optional[str]:
        return self._series_identity_path(self.capture_series_identity(jid))

    def legacy_series(self, jid):
        """Explicit recovery read, never merged or attributed to a current run."""
        safe = "".join(c if c.isalnum() or c in "_-" else "_" for c in jid)
        from raven.recordings.archive import read_tail
        if not self.persist:
            return [], {"status": "missing", "message": "Persistence is disabled."}
        values, status = read_tail(os.path.join(self.state_dir, "series", safe + ".jsonl"), self.series_keep,
                                   max_bytes=64 << 20, max_line=1 << 20)
        return values, dict(status, attribution="legacy-ambiguous", message="Legacy job-number-only samples are unattributed; original file preserved.")

    def _queue_persistence(self, path, sample):
        if path is not None and self.writable:
            with self._persist_queue_lock:
                queue = self._persist_event_queue if path == self._events_path else self._persist_queue
                before = len(queue)
                if self.raven_limits:
                    self.raven_limits.enqueue(queue,path,sample)
                else:
                    queue.append((path, sample))
                if queue is self._persist_queue and len(queue) != before:
                    count = self._series_pending_counts.get(path, 0) + 1
                    self._series_pending_counts[path] = count
                    self._series_batch_ready = self._series_batch_ready or count >= 256

    def flush_persistence(self, *, _events_only=False, _series_only=False, _defer_series=False):
        """Write queued observations in publication order without Store.lock.

        Worker sources call this after publishing their entire atomic update.
        Separate event and metric writers drain batches; another source can
        publish while storage is slow, with per-file order preserved. A copy
        event never waits for an unrelated metrics append.
        """
        if not self.writable:
            return
        if self._tags_dirty and time.monotonic() - self._tags_last_try >= 5:
            self._save_tags()
        if not _series_only:
            self._drain_persistence(self._persist_event_queue, self._persist_event_io_lock)
        if not _events_only:
            if _defer_series:
                with self._persist_queue_lock:
                    pressure = (self.raven_limits is not None and self.raven_limits.pending_bytes >= self.raven_limits.values['persistence_queue_bytes'] * .75)
                    if not self._series_batch_ready and not pressure and len(self._persist_queue) < 4096 and time.monotonic() - self._series_last_flush < 1.:
                        return
            self._drain_persistence(self._persist_queue, self._persist_io_lock)
            self._series_last_flush = time.monotonic()

    def _drain_persistence(self, queue, io_lock):
        if queue is self._persist_queue:
            self._drain_series_rekeys()
        if self.raven_limits:
            return self.raven_limits.drain(self,queue,io_lock)
        with io_lock:
            while True:
                with self._persist_queue_lock:
                    if not queue:
                        return
                    batch = list(queue)
                    queue.clear()
                    if queue is self._persist_queue:
                        self._series_pending_counts.clear(); self._series_batch_ready = False
                paths = {}
                for path, sample in batch:
                    path = self._series_aliases.get(path, path)
                    paths.setdefault(path, []).append(sample)
                for path, samples in paths.items():
                    try:
                        if path != self._events_path:
                            from raven.recordings.storage import append as append_recordings
                            if append_recordings(path, samples, cache=self._series_format_cache):
                                continue
                        from raven.recordings.storage import append_jsonl
                        append_jsonl(path, samples)
                    except (OSError, ValueError) as exc:
                        self._state_health("state recordings", str(exc))

    def _drain_series_rekeys(self):
        if not self._series_rekeys:
            return
        from raven.recordings.storage import rekey
        with self._persist_io_lock:
            while self._series_rekeys:
                with self._persist_queue_lock:
                    old, new = self._series_rekeys[0]
                try:
                    rekey(old, new)
                except (OSError, ValueError) as exc:
                    self._state_health('state recordings', 'Attempt promotion awaits storage: ' + str(exc))
                    return
                with self._persist_queue_lock:
                    pending = list(self._persist_queue)
                    self._persist_queue.clear()
                    self._persist_queue.extend((new if path == old else path, value) for path, value in pending)
                    self._series_aliases.pop(old, None)
                    self._series_rekeys.popleft()

    def record(self, jid: str, sample: dict, *, _persist: bool = True, identity=None):
        """Append a captured sample; defer IO when inside a larger publication."""
        with self.lock:
            current = self.capture_series_identity(jid)
            if identity is not None and identity != current:
                return None
            identity = current
            write_identity = self.sample_recording_identity(jid, identity=identity)
            if self.raven_limits and not write_identity.verified:
                self.raven_limits.note_session_path(self._series_identity_path(write_identity))
            self._ui_revision += 1
            self._notify_ui()
            series = self.series[jid]
            captured = series.append_owned(sample) if type(series) is SeriesDeque else None
            if type(series) is not SeriesDeque:
                series.append(sample)
            if write_identity.verified and sample.get("k") in ("live", "gpu"):
                from .stream_anomalies import Monitor
                monitor = getattr(self, "anomalies", None)
                if monitor is None:
                    monitor = self.anomalies = Monitor()
                monitor.observe(write_identity, sample)
            if self.raven_limits:
                from raven.collection.retention import retained_size
                self.raven_limits.retain_series(self,jid,retained_size(sample,self.raven_limits.values['series_bytes']))
            self._queue_persistence(self._series_identity_path(write_identity), captured if captured is not None else sample)
        if _persist:
            self.flush_persistence(_series_only=True)
        return sample

    def configure_series_loader(self, submit):
        """Use the sampler executor for bounded interactive archive requests."""
        self._series_archive.configure(submit)

    def series_of(self, jid: str) -> List[dict]:
        """Restore retained history synchronously for reports and headless use."""
        return self._series_archive.read(jid)

    def series_view(self, jid: str) -> List[dict]:
        """Read published samples immediately; request saved history off-thread."""
        return self._series_archive.read(jid, background=True)

    def series_publication(self, jid: str, *, background=True, memory_only=False):
        """Reuse an owned source revision across chart and advisor consumers."""
        from raven.recordings.publication import PublishedSeries
        if memory_only:
            with self.lock:
                source = self.series.get(jid, ())
                publication = source.publication() if type(source) is SeriesDeque else list(source)
        else:
            publication = self._series_archive.read(jid, background=background, publication=True)
        if type(publication) is PublishedSeries:
            with self.lock:
                source = self.series.get(jid)
                if type(source) is SeriesDeque and source._source is publication:
                    previous = self._publications.pop(jid, None)
                    if previous and previous[0] is not source:
                        previous[0].release_publication()
                    self._publications[jid] = source, publication.byte_size
                    size = sum(entry[1] for entry in self._publications.values())
                    while len(self._publications) > MAX_SERIES_CACHE_ENTRIES or size > MAX_SERIES_CACHE_BYTES:
                        _, (expired, cost) = self._publications.popitem(last=False)
                        size -= cost
                        expired.release_publication()
        return publication

    def series_status(self, jid: str) -> dict:
        """Explain whether saved history is loading, missing, limited or ready."""
        status = self._series_archive.status(jid)
        if jid in self._isolated_recording_paths:
            path = self._series_identity_path(self._isolated_recording_paths[jid])
            status = dict(status, attribution='session-observations-present', message=status.get('message','') +
                ' Some current-session observations await a fresh scheduler restart proof; their session-only recording is separate: ' + str(path),
                session_recording_path=path)
        return status

    def series_jobs(self) -> List[str]:
        """Job IDs with a series on disk or in memory; synchronous inventory."""
        return self._series_archive.jobs()

    def series_jobs_view(self) -> List[str]:
        """Return cached IDs and refresh the filesystem inventory off-thread."""
        return self._series_archive.jobs(background=True)

    def apply_live(self, jid: str, lv: Live, *, _persist: bool = True, identity=None):
        with self.lock:
            if identity is not None and identity != self.capture_series_identity(jid):
                return None
            self._ui_revision += 1
            self._notify_ui()
            self.live[jid] = lv
            value = lv.rate if lv.rate is not None else lv.avg
            if value is not None:
                self.hist_cpu[jid].append(value)
            sample = None
            # RSS is independently observed; missing CPU accounting must not
            # suppress a valid memory history or replace unknown CPU with zero.
            memory_measured = False
            if type(lv.rss) in (int, float):
                try:
                    memory_measured = math.isfinite(lv.rss) and lv.rss >= 0
                except OverflowError:
                    pass
            if lv.cpu_time is not None or memory_measured:
                sample = self.record(jid, dict(t=lv.t, k="live", cpu=lv.rate, eff=lv.avg, rss=lv.rss, cpu_time=lv.cpu_time), _persist=False)
        if _persist:
            self.flush_persistence(_series_only=True)
        return sample

    def apply_gpu(self, jid: str, samples: Optional[List[GpuSample]], *, _persist: bool = True, identity=None):
        with self.lock:
            if identity is not None and identity != self.capture_series_identity(jid):
                return None
            self._ui_revision += 1
            self._notify_ui()
            self.gpu[jid] = samples
            for s in samples or []:
                key = f"{jid}:{s.device_key}"
                if s.util is None:
                    continue
                self.hist_gpu[key].append(s.util / 100.0)
                m = self.gpu_mean[key]
                m[0] += s.util
                m[1] += 1
            sample = None
            if samples:
                sample = self.record(jid, dict(t=clock.now(), k="gpu", gpu={s.device_key: [s.util, s.used, s.total] for s in samples}), _persist=False)
        if _persist and sample is not None:
            self.flush_persistence(_series_only=True)
        return sample

    def gpu_mean_of(self, key: str) -> Optional[float]:
        m = self.gpu_mean.get(key)
        return (m[0] / m[1]) if m and m[1] else None

    def job(self, jid: str) -> Optional[Job]:
        with self.lock:
            entry = self._record_entry(jid)
            return entry[0] if entry is not None and entry[1] is self.jobs else None
        return None

    def _refresh_record_lookup(self, *, force=False):
        signature = (id(self.jobs), len(self.jobs), id(self.finished), len(self.finished), id(self.departed_jobs), len(self.departed_jobs))
        if signature != self._record_lookup_signature or force:
            self._record_lookup = {jid: (job, self.departed_jobs, jid) for jid, job in self.departed_jobs.items()}
            self._record_lookup.update((job.id, (job, self.finished, index)) for index, job in enumerate(self.finished))
            self._record_lookup.update((job.id, (job, self.jobs, index)) for index, job in enumerate(self.jobs))
            self._record_lookup_signature = signature
            self._record_lookup_sources = (self.jobs, self.finished, self.departed_jobs)

    def _record_entry(self, jid):
        self._refresh_record_lookup()
        entry = self._record_lookup.get(jid)
        if entry is None and jid:
            # A public-row replacement can introduce a new ID without changing
            # list size. Do not cache absence without a source mutation token:
            # a later in-place insertion must be visible on the next lookup.
            # Existing sampled IDs retain their constant-time slot guard.
            self._refresh_record_lookup(force=True)
            entry = self._record_lookup.get(jid)
        if entry is not None:
            record, source, position = entry
            # Native sources replace entire publications. Embedding adapters
            # may replace one public row in place: validate just this indexed
            # slot so asynchronous identity checks still see that correction.
            try:
                current = source[position]
            except (KeyError, IndexError):
                current = None
            if current is not record or record.id != jid:
                self._refresh_record_lookup(force=True)
                entry = self._record_lookup.get(jid)
        return entry

    @property
    def render_revision(self):
        with self.lock:
            return self._ui_revision

    def record_context(self, jid: Optional[str], *, include_group: bool = False):
        """Read one exact job and its scheduler paths without copying histories.

        The record has the same shallow-copy semantics as ``snapshot``. The
        details dictionary is copied while holding the lock, so a reader sees
        one publication and cannot alter the store's path mapping. This lookup
        uses a publication index with an exact-slot guard: departures,
        accounting corrections and row replacement are visible on the next call.
        """
        with self.lock:
            entry = self._record_entry(jid)
            record = entry[0] if entry is not None else None
            if jid:
                if record is None and include_group:
                    record = next((job for job in self.group if job.id == jid), None)
            return record, dict(self.details.get(jid, {})) if jid else {}

    def render_snapshot(self) -> dict:
        """Reuse immutable native scalar histories; other snapshot rules remain."""
        with self.lock:
            snapshot=self.snapshot(_immutable_history=True)
            snapshot['_ui_publication_token']=self.ui_publication_token()
            return snapshot

    def ui_publication_token(self):
        """Bounded metadata only; no job/series walk, copy, hash, or file read.

        Source health covers collectors which publish directly under Store.lock.
        Native mutation methods carry an explicit revision; container identity
        and size also detect replacing lists in embedding fixtures. Unmanaged
        and unknown producer setups retain the original refresh fallback.
        """
        with self.lock:
            if (len(self.health)>32 or
                    any(type(value) is not Health for value in self.health.values())):
                return None
            health=tuple((name,h.calls,h.errors,h.enabled,h.inflight,h.last_ok,h.error,h.backoff)
                         for name,h in self.health.items())
            collections=(self.jobs,self.finished,self.details,self.live,self.gpu,self.steps,self.fin_steps,
                         self.trace,self.nodes,self.partitions,self.tags,self.events)
            return (self._ui_revision,self.t_jobs,self.history_revision,health,
                    tuple((id(value),len(value)) for value in collections),
                    bool(self._series_archive.worker),
                    tuple((key,value.get('limited')) for key,value in self.raven_limits.coverage.copy().items()) if self.raven_limits else ())

    def snapshot(self, *, _immutable_history=False) -> dict:
        with self.lock:
            return dict(jobs=list(self.jobs), live=dict(self.live), gpu=dict(self.gpu), nodes=dict(self.nodes), partitions=list(self.partitions),
                        gpu_inventory=dict(self.gpu_inventory), finished=list(self.finished), share=list(self.share), account=dict(self.account),
                        departed_jobs=dict(self.departed_jobs), job_transitions=[dict(e) for e in self.job_transitions],
                        history_revision=self.history_revision, started_seq=self.started_seq,
                        coverage={key:dict(value) for key,value in self.raven_limits.coverage.copy().items()} if self.raven_limits else {},
                        details=dict(self.details), health={k: replace(v) for k, v in self.health.items()}, events=list(self.events),
                        group=list(self.group), weather=list(self.weather), pending_ahead=dict(self.pending_ahead), budget=dict(self.budget),
                        nodemap=dict(self.nodemap), steps=dict(self.steps), fin_steps=dict(self.fin_steps), trace=dict(self.trace),
                        tags={k: dict(v) for k, v in self.tags.items()},
                        hist_cpu=immutable_histories(self.hist_cpu) if _immutable_history else {k: list(v) for k, v in self.hist_cpu.items()},
                        hist_gpu=immutable_histories(self.hist_gpu) if _immutable_history else {k: list(v) for k, v in self.hist_gpu.items()},
                        gpu_mean={k: (v[0] / v[1] if v[1] else None) for k, v in self.gpu_mean.items()}, t_jobs=self.t_jobs, error=self.error)

    # ---- ui state persistence ----------------------------------------------------------------------
    def load_ui(self) -> dict:
        if not self.persist:
            return {}
        with self._ui_io_lock:
            value, self.ui_state_status = ui_state.read(os.path.join(self.state_dir, "ui.json"))
            migrated = getattr(self, "raven_ui_import", None)
            if self.ui_state_status["state"] == "missing" and migrated is not None:
                import copy
                value, self.ui_state_status = copy.deepcopy(migrated)
            return value

    def save_ui(self, data: dict):
        if not self.persist:
            return
        if not self.writable:
            self.ui_state_status = {"state": "read-only", "message": self.state_status["message"]}
            return False
        with self._ui_io_lock:
            self.ui_state_status = ui_state.write(os.path.join(self.state_dir, "ui.json"), data)
            return self.ui_state_status["state"] == "ready"
