"""Bounded, connection-scoped alerts with cached snapshots and local guidance.

Publication sanitizes each message once. Repaints read immutable records; they
never run a diagnostic, contact Slurm, or hash all retained messages. Condition
keys remember dismissals until the observed failure clears or changes.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, replace
import hashlib
import re
import threading
import time
from types import MappingProxyType

from .log_text import display_text


SEVERITIES = ("critical", "moderate", "mild")
MAX_MESSAGE_BYTES = 65536
MAX_NOTICES = 256
MAX_RETAINED_BYTES = 1048576
MAX_CONDITIONS = 256
CONDITION_INLINE_CHARS = 256
_UNSET = object()
_RECOVERABLE = re.compile(
    r"^(?:raven(?::\s*|\s+))?(?:collection deferred|rolling (?:steps|command) budget exhausted|"
    r"no running jobs are available|waiting for (?:the first sample|governed)|"
    r"gpu (?:counters are|telemetry is) unavailable|source disabled)(?=$|[\s:;.])"
)
_HARD_FAILURE = re.compile(
    r"\b(?:denied|failed|failure|invalid|malformed|unauthorized|unverified|forbidden|error)\b|"
    r"not permitted|permission problem|authentication required|outside the verified allocation|"
    r"verification mismatch|identity mismatch|no such file|not found|timed out|timeout|"
    r"connection refused|connection reset|could not|cannot"
)


def _access_failure(folded):
    return any(token in folded for token in (
        "permission denied", "access denied", "not permitted", "authentication failed",
        "authentication failure", "authorization failed", "authorization failure", "unauthorized",
        "authentication required",
    ))


@dataclass(frozen=True)
class Diagnostic:
    issue: str
    causes: tuple[str, ...]
    fixes: tuple[str, ...]


@dataclass(frozen=True)
class Notification:
    id: str
    severity: str
    text: str
    source: str
    job: str
    task: str
    path: str
    first_seen: float
    last_seen: float
    occurrences: int
    truncated: bool
    diagnosis: Diagnostic
    run: tuple = ()
    setting: str = ""
    resolved_at: float = 0.0
    resolution: str = ""


@dataclass(frozen=True)
class NotificationSnapshot:
    revision: int
    notices: tuple[Notification, ...]
    counts: object
    retained_bytes: int
    evicted: int


def message_text(value, limit=MAX_MESSAGE_BYTES):
    """Preserve line breaks while removing terminal escapes, within a byte cap."""
    raw = str(value)
    clipped = len(raw) > limit
    raw = raw[:limit]
    if raw.isascii() and raw.isprintable():
        return raw, clipped
    # Normalize CRLF before exposing remaining controls. Splitting only on LF
    # preserves intentional paragraphs and prevents escape payloads from hiding
    # unrelated later lines. Terminal controls never reach the renderer.
    text = "\n".join(display_text(line) for line in raw.replace("\r\n", "\n").split("\n"))
    text = "".join(char if char == "\n" or char.isprintable() else "?" for char in text)
    encoded = text.encode("utf-8", "replace")
    clipped = clipped or len(encoded) > limit
    if len(encoded) > limit:
        text = encoded[:limit].decode("utf-8", "ignore")
    return text, clipped


def severity_for(text, level="error"):
    """Only explicit recoverable admission/availability evidence lowers severity."""
    if level not in ("error", "warning", *SEVERITIES):
        return None
    if level in SEVERITIES:
        return level
    folded = text.casefold().lstrip()
    # A benign phrase inside a path, quotation, or a harder failure is not an
    # availability diagnosis. Only a leading observation without contradictory
    # failure evidence may lower the producer's original error/warning level.
    if _RECOVERABLE.match(folded) and not _HARD_FAILURE.search(folded):
        return "mild"
    return "critical" if level == "error" else "moderate"


def diagnose(text, source=""):
    """Describe observed evidence and possible next steps, never a claimed probe."""
    folded = text.casefold()
    if _access_failure(folded):
        return Diagnostic(
            "The operation reported an access, authentication, or permission failure.",
            ("The connected user may lack permission on the named file, directory, scheduler account, or service.",
             "Ownership, directory traversal permissions, or the selected remote identity may differ from the expected configuration."),
            ("Check the full message for the affected resource and confirm the connected username in Setup.",
             "Review the resource's permissions or ask its owner or cluster administrator for the required access."))
    if (any(token in folded for token in ("collection deferred", "budget exhausted", "rate limit", "quota exceeded"))
            and not _HARD_FAILURE.search(folded)):
        return Diagnostic(
            "The reported operation reached a collection or admission limit.",
            ("Recent requests may have consumed the configured rolling collection budget.",
             "An allocation or site policy may impose an additional limit; this message alone does not identify which one."),
            ("Allow the reported retry interval to pass and check Sources for a successful refresh.",
             "Review Setup and Raven collection diagnostics before changing budgets; repeated refreshes can consume the same limit."))
    if any(token in folded for token in ("timed out", "timeout", "connection refused", "connection reset", "could not resolve", "unable to contact")):
        return Diagnostic(
            "The operation reported a timeout or connection failure.",
            ("The scheduler, remote host, or network may be unavailable or slower than the configured timeout.",
             "The message does not establish whether the cause is local, network-related, or on the service."),
            ("Check Sources and the configured host in Setup for the affected connection.",
             "Retry after the service recovers; consult the cluster's status information if the failure persists."))
    if any(token in folded for token in ("invalid account", "invalid qos", "invalid partition", "account/partition", "association")):
        return Diagnostic(
            "Slurm reported an account, partition, QoS, or association problem.",
            ("The selected account may not have an association with this user, cluster, partition, or QoS.",
             "A lab account can require a different partition or QoS than the user's default account."),
            ("Open Setup > Connections and accounts, then Heavy allocation settings, and refresh the available account associations.",
             "Select an allowed account, partition, and QoS combination; ask the lab or cluster administrator if the required association is missing."))
    if any(token in folded for token in ("outside the verified allocation", "verified allocation", "worker host", "allocation expired")):
        return Diagnostic(
            "The worker could not satisfy the reported allocation verification requirement.",
            ("The allocation may have ended, or Slurm's node identity may differ from the worker's reported host.",),
            ("Inspect Raven compute diagnostics and the allocation's current state.",
             "Review the Heavy target and node configuration in Setup before requesting another allocation."))
    if any(token in folded for token in ("gpu", "nvml", "nvidia-smi", "rocm", "gpumem", "gpuutil")):
        return Diagnostic(
            "The message concerns GPU collection or a GPU metric source.",
            ("The selected job may not expose GPU accounting counters or an existing GPU trace.",
             "Driver access, an unavailable Slurm GPU plugin, or a missing trace can produce different failures; the exact cause is not established here."),
            ("Open Setup > Recordings and metrics and inspect the job's available CPU, memory, and GPU sources.",
             "Check the job's GPU allocation and existing trace configuration. Cluster GPU accounting changes require the cluster administrator."))
    if any(token in folded for token in ("configuration", "config", "invalid json", "toml", "schema")):
        return Diagnostic(
            "The message reports a configuration or data-format problem.",
            ("A setting may be unsupported, malformed, or inconsistent with the active profile.",),
            ("Use the full message to locate the affected field or file and compare it with Setup and the configuration reference.",
             "Review the active profile before saving a correction; relaunch if the configuration changed outside this session."))
    if any(token in folded for token in ("squeue", "sacct", "sstat", "slurm", "scontrol")):
        return Diagnostic(
            "A Slurm command or scheduler operation reported a failure.",
            ("The command may be unavailable, rejected by the scheduler, or using unsupported options.",
             "The full error and source identify the failing operation; no additional diagnostic command has run."),
            ("Inspect the affected source in Sources and verify the connection and allocation settings in Setup.",
             "If the same scheduler error persists, share its full text with the cluster administrator."))
    return Diagnostic(
        "Raven retained the reported issue; its cause has not been diagnosed.",
        ("The message alone is insufficient to determine a specific cause.",),
        ("Read the full message and its source or job context before retrying the operation.",
         "Use Setup and Sources to inspect the relevant configuration and collection status; retain the full message when reporting the issue."))


class NotificationCenter:
    """O(1) indexed publication/dismissal with bounded, revision-cached snapshots."""
    def __init__(self, *, scope_provider=None):
        self._lock = threading.RLock()
        self._scope_provider = scope_provider
        self._scope = _UNSET
        self._notices = OrderedDict()
        self._index = {}
        self._keys = {}
        self._sizes = {}
        self._conditions = OrderedDict()
        self._condition_ids = {}
        self._condition_observed = {}
        self._counts = dict.fromkeys(SEVERITIES, 0)
        self._bytes = 0
        self._sequence = 0
        self._revision = 0
        self._evicted = 0
        self._snapshot = None

    def _changed(self):
        self._revision += 1
        self._snapshot = None

    def scope(self, value):
        with self._lock:
            if self._scope is _UNSET:
                self._scope = value
            elif self._scope != value:
                self._scope = value
                self._notices.clear()
                self._index.clear()
                self._keys.clear()
                self._sizes.clear()
                self._conditions.clear()
                self._condition_ids.clear()
                self._condition_observed.clear()
                self._counts = dict.fromkeys(SEVERITIES, 0)
                self._bytes = self._evicted = 0
                self._changed()

    def _ensure_scope(self):
        if self._scope_provider is not None:
            self.scope(self._scope_provider())

    def _remove(self, identity):
        notice = self._notices.pop(identity, None)
        if notice is None:
            return False
        self._index.pop(self._keys.pop(identity), None)
        self._bytes -= self._sizes.pop(identity)
        if not notice.resolved_at:
            self._counts[notice.severity] -= 1
        self._changed()
        return True

    def post(self, text, level="error", *, source="", job="", task="", path="", run=(), setting="", now=None):
        if level not in ("error", "warning", *SEVERITIES):
            return None
        safe, truncated = message_text(text)
        severity = severity_for(safe, level)
        if severity is None or not safe.strip():
            return None
        source, job, task, path = (message_text(value, limit)[0] for value, limit in
                                   ((source, 256), (job, 256), (task, 256), (path, 4096)))
        run = tuple(run) if isinstance(run, (tuple, list)) and len(run) == 6 else ()
        if run and (any(not isinstance(value, (str, int, type(None))) for value in run)
                    or any(isinstance(value, str) and (len(value) > 4096 or not value.isprintable()) for value in run)):
            run = ()
        setting = setting if setting in ("connections", "allocation", "refresh", "recordings", "diagnostics") else ""
        # The full message is stored once. Fixed-size keys avoid a second copy
        # and keep the index bounded even for unusually large scheduler errors.
        digest = hashlib.blake2b(safe.encode("utf-8"), digest_size=20).digest()
        key = severity, source, job, task, path, run, setting, digest
        timestamp = time.time() if now is None else now
        with self._lock:
            self._ensure_scope()
            identity = self._index.get(key)
            if identity is not None:
                previous = self._notices[identity]
                if previous.resolved_at:
                    self._counts[previous.severity] += 1
                    self._bytes -= len(previous.resolution.encode("utf-8"))
                    self._sizes[identity] -= len(previous.resolution.encode("utf-8"))
                self._notices[identity] = replace(previous, last_seen=timestamp,
                                                 occurrences=previous.occurrences + 1,
                                                 truncated=previous.truncated or truncated,
                                                 resolved_at=0.0, resolution="")
                self._notices.move_to_end(identity)
                self._changed()
                return identity
            self._sequence += 1
            identity = f"notice-{self._sequence}"
            notice = Notification(identity, severity, safe, source, job, task, path,
                                  timestamp, timestamp, 1, truncated, diagnose(safe, source), run, setting)
            size = len(safe.encode("utf-8")) + sum(len(v.encode("utf-8")) for v in (source, job, task, path, setting))
            size += sum(len(str(v).encode("utf-8")) for v in run)
            self._notices[identity] = notice
            self._index[key], self._keys[identity], self._sizes[identity] = identity, key, size
            self._counts[severity] += 1
            self._bytes += size
            while len(self._notices) > MAX_NOTICES or self._bytes > MAX_RETAINED_BYTES:
                self._remove(next(iter(self._notices)))
                self._evicted += 1
            self._changed()
            return identity

    def condition(self, key, text, *, level="error", source="", job="", task="", run=(), setting="", observed_at=None):
        """Publish a changed observed condition; keep a dismissed failure quiet."""
        # Ordinary unchanged errors compare immutable strings without formatting
        # or hashing. Unusually large messages use fixed-size keys instead of a
        # second retained copy; source inspection is capped at 65,536 characters.
        raw = str(text or "")
        raw = raw[:MAX_MESSAGE_BYTES]
        evidence = raw if len(raw) <= CONDITION_INLINE_CHARS else hashlib.blake2b(
            raw.encode("utf-8", "replace"), digest_size=20).digest()
        signature = evidence, level, str(source)[:256], str(job)[:256], str(task)[:256], tuple(run), setting
        with self._lock:
            self._ensure_scope()
            if not raw.strip():
                self._conditions.pop(key, None)
                self._condition_ids.pop(key, None)
                self._condition_observed.pop(key, None)
                return None
            previous = self._conditions.get(key)
            if previous == signature:
                if observed_at is not None and observed_at > self._condition_observed.get(key, 0):
                    self._condition_observed[key] = observed_at
                    # Count a newly observed failed attempt once, not a repaint.
                    identity = self._condition_ids.get(key)
                    if identity in self._notices:
                        return self.post(text, level, source=source, job=job, task=task,
                                         run=run, setting=setting, now=observed_at)
                return None
            self._conditions[key] = signature
            self._conditions.move_to_end(key)
            while len(self._conditions) > MAX_CONDITIONS:
                expired, _ = self._conditions.popitem(last=False)
                self._condition_ids.pop(expired, None)
                self._condition_observed.pop(expired, None)
            identity = self.post(text, level, source=source, job=job, task=task, run=run, setting=setting)
            self._condition_ids[key] = identity
            self._condition_observed[key] = observed_at or time.time()
            return identity

    def resolve_condition(self, key, evidence, *, observed_at):
        """Only a producer's newer successful observation resolves its episode."""
        if not evidence or not isinstance(observed_at, (int, float)):
            return False
        with self._lock:
            self._ensure_scope()
            identity = self._condition_ids.get(key)
            item = self._notices.get(identity)
            if observed_at <= self._condition_observed.get(key, 0):
                return False
            if item is not None and not item.resolved_at:
                resolution = message_text(evidence, 4096)[0]
                self._notices[identity] = replace(item, resolved_at=observed_at, resolution=resolution)
                size = len(resolution.encode("utf-8"))
                self._sizes[identity] += size
                self._bytes += size
                self._counts[item.severity] -= 1
                while self._bytes > MAX_RETAINED_BYTES:
                    self._remove(next(iter(self._notices)))
                    self._evicted += 1
                self._changed()
            self._conditions.pop(key, None)
            self._condition_ids.pop(key, None)
            self._condition_observed.pop(key, None)
            return item is not None

    def retain_conditions(self, keys):
        """A vanished/disabled source ends its episode, allowing a future alert."""
        with self._lock:
            for key in tuple(self._conditions):
                if key not in keys:
                    del self._conditions[key]
                    self._condition_ids.pop(key, None)
                    self._condition_observed.pop(key, None)

    def snapshot(self):
        with self._lock:
            self._ensure_scope()
            if self._snapshot is None:
                self._snapshot = NotificationSnapshot(
                    self._revision, tuple(reversed(self._notices.values())),
                    MappingProxyType(dict(self._counts)), self._bytes, self._evicted)
            return self._snapshot

    def get(self, identity):
        with self._lock:
            self._ensure_scope()
            return self._notices.get(identity)

    def dismiss(self, identity):
        with self._lock:
            self._ensure_scope()
            return self._remove(identity)

    def clear(self, severity=None):
        with self._lock:
            self._ensure_scope()
            for identity, notice in tuple(self._notices.items()):
                if severity is None or notice.severity == severity:
                    self._remove(identity)


def _scope(app):
    backend = getattr(getattr(getattr(app, "actions", None), "slurm", None), "b", None)
    return id(getattr(app, "store", None)), id(backend), str(getattr(app, "profile_name", ""))


def initialize(app):
    activity = getattr(app, "activity", None)
    center = getattr(activity, "alerts", None) if activity is not None else getattr(app, "notifications", None)
    if not isinstance(center, NotificationCenter):
        center = NotificationCenter()
        if activity is not None:
            activity.alerts = center
    if getattr(center, "_bound_app", None) is not app:
        center._bound_app = app
        center._scope_provider = lambda: _scope(app)
    app.notifications = center
    center.scope(_scope(app))
    return center


def sync(app, snap):
    """Ingest cached source, log, and compute errors without any external I/O."""
    center = initialize(app)
    keys = set()
    health = snap.get("health", {}) if isinstance(snap, dict) else {}
    for index, (name, item) in enumerate(health.items()):
        if index >= 128:
            break
        value = item.get if isinstance(item, dict) else lambda key, default: getattr(item, key, default)
        key = ("health", str(name)[:256])
        keys.add(key)
        enabled, error = value("enabled", True), value("error", "")
        if enabled and not error and value("last_ok", 0) and value("last_ok", 0) >= value("last_try", 0):
            center.resolve_condition(key, "The source completed a newer successful refresh.", observed_at=value("last_ok", 0))
        if enabled and error:
            center.condition(key, error, source=str(name), setting="refresh",
                             observed_at=value("last_try", 0) or None)
        else:
            center.condition(key, "")  # End suppression; this does not claim recovery.
    other = (("collection", snap.get("error", "") if isinstance(snap, dict) else ""),
             ("logs", getattr(getattr(app, "logs", None), "error", "")),
             ("compute", getattr(app, "raven_ui_state", {}).get("snapshot", {}).get("error", "")))
    for name, error in other:
        key = ("cached", name)
        keys.add(key)
        setting = "allocation" if name == "compute" else "connections" if name == "collection" else ""
        run = ()
        if name == "logs":
            binding = getattr(app, "project_state", {}).get("binding") or {}
            if binding and binding.get("job_id") == getattr(app, "log_job", None):
                from .run_home import identity
                run = identity(binding)
        center.condition(key, error, source=name, setting=setting, run=run)
    engine = getattr(getattr(app, "store", None), "alerts", None)
    store = getattr(app, "store", None)
    monitor = getattr(store, "anomalies", None)
    # Detection runs during bounded sample publication, never during painting.
    # Each evidence episode has a stable attempt-bound id and immutable text.
    if monitor is not None:
        with store.lock:
            episodes = tuple(monitor.recent)
        for episode in episodes:
            key = ("telemetry-change", episode["id"])
            keys.add(key)
            center.condition(key, episode["text"], level="mild", source="Telemetry changes (experimental)")
    # The rule engine already evaluated conditions and retained its last fifty
    # events. Reading these records never evaluates rules or sends notifications.
    # Event time distinguishes a later firing of the same rule after dismissal.
    for event in getattr(engine, "recent", ())[-50:]:
        if not isinstance(event, dict):
            continue
        rule, job = str(event.get("rule", ""))[:256], str(event.get("job", ""))[:256]
        key = ("rule-event", event.get("t"), rule, job)
        keys.add(key)
        center.condition(key, event.get("text", ""), level="warning", source="alert: " + rule, job=job)
    for index, (name, error) in enumerate(dict(getattr(engine, "errors", {})).items()):
        if index >= 64:
            break
        key = ("rule-error", str(name)[:256])
        keys.add(key)
        center.condition(key, error, source="alert rule: " + str(name))
    # Other producers retain their own exact-run episodes. Source disappearance
    # or disabling is not evidence of recovery and must not mark alerts resolved.
    keys.update(key for key in center._conditions if isinstance(key, tuple) and key[0] in ("run-home", "run-home-source"))
    center.retain_conditions(keys)
    return center.snapshot()
