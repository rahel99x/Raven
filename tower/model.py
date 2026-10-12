"""The data model: typed records for jobs, live statistics, GPUs, nodes, partitions and finished jobs; the
thread-safe store with histories, transitions, events, per-source health and persistence."""
from __future__ import annotations

import collections
import json
import os
import re
import threading
import time
from dataclasses import asdict, dataclass, field, is_dataclass, replace
from functools import lru_cache
from typing import Any, Deque, Dict, List, Optional, Sequence

from . import clock
from .series_archive import SeriesArchive
from .history_snapshots import HistoryDeque, immutable_histories
from .series_publication import SeriesDeque
from .series_cache_limits import MAX_SERIES_CACHE_ENTRIES, MAX_SERIES_CACHE_BYTES
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
    cpu_time: Optional[float] = None     # seconds of CPU consumed (sstat AveCPU of the batch step)
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

    def __init__(self, state_dir: Optional[str] = None, persist: bool = True, max_events: int = 200, series_keep: int = 4000, *, raven_limits=None):
        self.raven_limits = raven_limits
        self._ui_revision = 0
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
        self._events_path = os.path.join(state_dir, "events.jsonl") if self.persist else None
        if self.persist:
            os.makedirs(state_dir, exist_ok=True)
            self._load_events()
            self._load_tags()

    # ---- tags, pins, notes ---------------------------------------------------------------------------
    def _load_tags(self):
        try:
            with open(os.path.join(self.state_dir, "tags.json")) as f:
                data = json.loads(f.read(2<<20)) if self.raven_limits else json.load(f)
            if isinstance(data, dict):
                self.tags = {k: v for k, v in data.items() if isinstance(v, dict)}
        except (OSError, ValueError):
            pass

    def _save_tags(self):
        if not self.persist:
            return
        try:
            with open(os.path.join(self.state_dir, "tags.json"), "w") as f:
                json.dump(self.tags, f)
        except OSError:
            pass

    def tag(self, jid: str, *tags: str, remove: bool = False) -> List[str]:
        with self.lock:
            self._ui_revision += 1
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
        self._save_tags()
        return list(rec["tags"])

    def pin(self, jid: str, value: Optional[bool] = None) -> bool:
        with self.lock:
            self._ui_revision += 1
            rec = self.tags.setdefault(jid, dict(tags=[], pinned=False, note=""))
            rec["pinned"] = (not rec["pinned"]) if value is None else bool(value)
            out = rec["pinned"]
            if not rec["tags"] and not rec["pinned"] and not rec["note"]:
                del self.tags[jid]
        self._save_tags()
        return out

    def note(self, jid: str, text: str) -> str:
        with self.lock:
            self._ui_revision += 1
            rec = self.tags.setdefault(jid, dict(tags=[], pinned=False, note=""))
            rec["note"] = text.strip()
            if not rec["tags"] and not rec["pinned"] and not rec["note"]:
                del self.tags[jid]
        self._save_tags()
        return text.strip()

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
            self._ui_revision += 1
            self._queue_persistence(os.path.join(self.state_dir, "events.jsonl") if self.persist else None, ev)
        if _persist:
            self.flush_persistence(_events_only=True)
        return ev

    def _load_events(self):
        path = os.path.join(self.state_dir, "events.jsonl")
        if self.raven_limits:
            from .series_archive import read_tail
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
            self._ui_revision += 1
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
            for j in jobs:
                if not j.est_start:
                    j.est_start = old.get(j.id, "")
            self.jobs = list(jobs)
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

    def apply_finished(self, jobs: Sequence[Finished], history_days: Optional[float] = None) -> None:
        """Reconcile an accounting snapshot with observed departures under one lock.

        Initial history never animates. Confirmed live transitions increment
        ``history_revision`` once, even if sacct wins the race with squeue. Recent
        confirmations survive temporarily incomplete accounting snapshots.
        """
        with self.lock:
            self._ui_revision += 1
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
            for j in self.jobs:
                if j.id in starts:
                    j.est_start = starts[j.id]

    # ---- per-job time series -----------------------------------------------------------------------
    def _series_path(self, jid: str) -> Optional[str]:
        if not self.persist:
            return None
        safe = "".join(c if c.isalnum() or c in "_-" else "_" for c in jid)
        return os.path.join(self.state_dir, "series", f"{safe}.jsonl")

    def _queue_persistence(self, path, sample):
        if path is not None:
            with self._persist_queue_lock:
                queue = self._persist_event_queue if path == self._events_path else self._persist_queue
                if self.raven_limits:
                    self.raven_limits.enqueue(queue,path,sample)
                else:
                    queue.append((path, sample))

    def flush_persistence(self, *, _events_only=False, _series_only=False):
        """Write queued observations in publication order without Store.lock.

        Worker sources call this after publishing their entire atomic update.
        Separate event and metric writers drain batches; another source can
        publish while storage is slow, with per-file order preserved. A copy
        event never waits for an unrelated metrics append.
        """
        if not self.persist:
            return
        if not _series_only:
            self._drain_persistence(self._persist_event_queue, self._persist_event_io_lock)
        if not _events_only:
            self._drain_persistence(self._persist_queue, self._persist_io_lock)

    def _drain_persistence(self, queue, io_lock):
        if self.raven_limits:
            return self.raven_limits.drain(self,queue,io_lock)
        with io_lock:
            while True:
                with self._persist_queue_lock:
                    if not queue:
                        return
                    batch = list(queue)
                    queue.clear()
                paths = {}
                for path, sample in batch:
                    paths.setdefault(path, []).append(sample)
                for path, samples in paths.items():
                    try:
                        os.makedirs(os.path.dirname(path), exist_ok=True)
                        with open(path, "a") as stream:
                            for sample in samples:
                                stream.write(json.dumps(sample) + "\n")
                    except OSError:
                        pass

    def record(self, jid: str, sample: dict, *, _persist: bool = True):
        """Append a captured sample; defer IO when inside a larger publication."""
        with self.lock:
            self._ui_revision += 1
            series = self.series[jid]
            captured = series.append_owned(sample) if type(series) is SeriesDeque else None
            if type(series) is not SeriesDeque:
                series.append(sample)
            if self.raven_limits:
                from .raven_limits import retained_size
                self.raven_limits.retain_series(self,jid,retained_size(sample,self.raven_limits.values['series_bytes']))
            self._queue_persistence(self._series_path(jid), captured if captured is not None else sample)
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
        from .series_publication import PublishedSeries
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
        return self._series_archive.status(jid)

    def series_jobs(self) -> List[str]:
        """Job IDs with a series on disk or in memory; synchronous inventory."""
        return self._series_archive.jobs()

    def series_jobs_view(self) -> List[str]:
        """Return cached IDs and refresh the filesystem inventory off-thread."""
        return self._series_archive.jobs(background=True)

    def apply_live(self, jid: str, lv: Live, *, _persist: bool = True):
        with self.lock:
            self._ui_revision += 1
            self.live[jid] = lv
            value = lv.rate if lv.rate is not None else lv.avg
            if value is not None:
                self.hist_cpu[jid].append(value)
            sample = None
            if lv.cpu_time is not None:
                sample = self.record(jid, dict(t=lv.t, k="live", cpu=lv.rate, eff=lv.avg, rss=lv.rss, cpu_time=lv.cpu_time), _persist=False)
        if _persist:
            self.flush_persistence(_series_only=True)
        return sample

    def apply_gpu(self, jid: str, samples: Optional[List[GpuSample]], *, _persist: bool = True):
        with self.lock:
            self._ui_revision += 1
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
            for j in self.jobs:
                if j.id == jid:
                    return j
        return None

    def record_context(self, jid: Optional[str], *, include_group: bool = False):
        """Read one exact job and its scheduler paths without copying histories.

        The record has the same shallow-copy semantics as ``snapshot``. The
        details dictionary is copied while holding the lock, so a reader sees
        one publication and cannot alter the store's path mapping. This lookup
        deliberately keeps no cache: departures, accounting corrections and
        changed working directories are visible on the next call.
        """
        with self.lock:
            record = None
            if jid:
                for records in (self.jobs, self.finished):
                    record = next((job for job in records if job.id == jid), None)
                    if record is not None:
                        break
                if record is None:
                    record = self.departed_jobs.get(jid)
                if record is None and include_group:
                    record = next((job for job in self.group if job.id == jid), None)
            return record, dict(self.details.get(jid, {})) if jid else {}

    def render_snapshot(self) -> dict:
        """Reuse immutable native scalar histories; other snapshot rules remain."""
        with self.lock:
            snapshot=self.snapshot(_immutable_history=True)
            if self.raven_limits is not None:
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
            if (self.raven_limits is None or len(self.health)>32 or
                    any(type(value) is not Health for value in self.health.values())):
                return None
            health=tuple((name,h.calls,h.errors,h.enabled,h.inflight,h.last_ok,h.error,h.backoff)
                         for name,h in self.health.items())
            collections=(self.jobs,self.finished,self.details,self.live,self.gpu,self.steps,self.fin_steps,
                         self.trace,self.nodes,self.partitions,self.tags,self.events)
            return (self._ui_revision,self.t_jobs,self.history_revision,health,
                    tuple((id(value),len(value)) for value in collections),
                    bool(self._series_archive.worker),
                    tuple((key,value.get('limited')) for key,value in self.raven_limits.coverage.copy().items()))

    def snapshot(self, *, _immutable_history=False) -> dict:
        with self.lock:
            return dict(jobs=list(self.jobs), live=dict(self.live), gpu=dict(self.gpu), nodes=dict(self.nodes), partitions=list(self.partitions),
                        gpu_inventory=dict(self.gpu_inventory), finished=list(self.finished), share=list(self.share), account=dict(self.account),
                        departed_jobs=dict(self.departed_jobs), job_transitions=[dict(e) for e in self.job_transitions],
                        history_revision=self.history_revision,
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
        with self._ui_io_lock:
            self.ui_state_status = ui_state.write(os.path.join(self.state_dir, "ui.json"), data)
            return self.ui_state_status["state"] == "ready"
