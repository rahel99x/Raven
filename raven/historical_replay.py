"""Bounded immutable observation snapshots, never reconstructed live state.

Recording is explicit. A snapshot says when these values became available to
this observer, separately from scheduler/sample timestamps. Later arrivals can
only appear in a later snapshot, including observations whose source time is old.
"""

from __future__ import annotations

from collections import deque
import copy
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import math
import time

SCHEMA = "raven.asof/v1"
MAX_SNAPSHOT = 2 << 20
MAX_ARCHIVE = 7 << 20
MAX_SNAPSHOTS = 64
MAX_ROWS = 2000
MAX_SAMPLES = 10000


def encoded(value):
    return json.dumps(
        value, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode()


def capture(store, selected=None):
    """Background-only bounded copy of already-published data; no source reads."""
    from .model import to_plain

    with store.lock:
        jobs, finished = store.jobs[:MAX_ROWS], store.finished[:MAX_ROWS]
        visible_ids = {row.id for row in jobs + finished}
        identity = getattr(store, "_recording_identities", {}).get(selected)
        source = getattr(store, "series", {}).get(selected, ()) if selected else ()
        from itertools import islice

        samples = list(islice(source, max(0, len(source) - MAX_SAMPLES), None))
        # Freeze containers under the lock; conversion and JSON encoding can
        # traverse thousands of rows and belong outside the publication lock.
        captured = {
            "jobs": jobs,
            "history": finished,
            "observer_id": getattr(store, "_recording_session", "unknown"),
            "source_scope": getattr(store, "_recording_scope", "unknown"),
            "transitions": list(store.job_transitions)[-128:],
            "selected": selected,
            "identity": identity,
            "attempts": {
                jid: item
                for jid, item in getattr(store, "_recording_identities", {}).items()
                if jid in visible_ids
            },
            "samples": samples,
            "source_time": store.t_jobs,
            "source_health": {
                key: copy.copy(item) for key, item in store.health.items()
            },
            "coverage": {
                "jobs_omitted": max(0, len(store.jobs) - len(jobs)),
                "history_omitted": max(0, len(store.finished) - len(finished)),
                "samples_omitted": max(0, len(source) - MAX_SAMPLES),
                "series_scope": "selected job's already-published memory only; no archive reads",
            },
        }
    value = to_plain(captured)
    observed = time.time()
    # Future source clocks stay evidence, but cannot be drawn as observations
    # before their source timestamp. Preserve them with an explicit count.
    value.update(
        observed_at=observed,
        monotonic=time.monotonic(),
        clock="Unix UTC observer completion",
    )
    raw = encoded(value)
    if len(raw) > MAX_SNAPSHOT:
        raise ValueError(
            "Historical snapshot exceeds 2 MiB; previous snapshots remain available"
        )
    return raw


def _finite(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def validate(raw):
    from .evidence import loads

    if not isinstance(raw, bytes) or len(raw) > MAX_SNAPSHOT:
        raise ValueError("Historical snapshot exceeds its byte bound")
    value = loads(raw)
    if (
        not isinstance(value, dict)
        or not _finite(value.get("observed_at"))
        or not _finite(value.get("monotonic"))
        or not _finite(value.get("source_time"))
    ):
        raise ValueError(
            "Historical snapshot requires explicit finite observation/source clocks"
        )
    for field, maximum in (
        ("jobs", MAX_ROWS),
        ("history", MAX_ROWS),
        ("transitions", 128),
        ("samples", MAX_SAMPLES),
    ):
        rows = value.get(field)
        if (
            not isinstance(rows, list)
            or len(rows) > maximum
            or any(not isinstance(row, dict) for row in rows)
        ):
            raise ValueError("Historical " + field + " exceeds its record/type bound")
    if value.get("selected") is not None and (
        not isinstance(value["selected"], str) or len(value["selected"]) > 128
    ):
        raise ValueError("Invalid historical selection")
    for key in ("observer_id", "source_scope"):
        if not isinstance(value.get(key), str) or not 1 <= len(value[key]) <= 256:
            raise ValueError(
                "Historical snapshots need bounded observer and connection scope identities"
            )
    coverage = value.get("coverage")
    if (
        not isinstance(coverage, dict)
        or not isinstance(coverage.get("series_scope"), str)
        or any(
            type(coverage.get(key)) is not int or not 0 <= coverage[key] < 2**63
            for key in ("jobs_omitted", "history_omitted", "samples_omitted")
        )
        or not isinstance(value.get("source_health"), dict)
    ):
        raise ValueError(
            "Historical coverage and source health must be explicit bounded records"
        )
    attempts = value.get("attempts", {})
    if not isinstance(attempts, dict) or len(attempts) > MAX_ROWS * 2:
        raise ValueError("Invalid historical attempt inventory")
    for jid, item in attempts.items():
        if (
            not isinstance(jid, str)
            or not isinstance(item, dict)
            or item.get("job") != jid
        ):
            raise ValueError("Historical attempt inventory identity conflicts")
    identities = list(attempts.values()) + (
        [value["identity"]] if value.get("identity") is not None else []
    )
    for identity in identities:
        if (
            not isinstance(identity, dict)
            or set(identity) != {"job", "scope", "attempt", "verified"}
            or type(identity["verified"]) is not bool
            or any(
                not isinstance(identity[key], str) or len(identity[key]) > 256
                for key in ("job", "scope", "attempt")
            )
        ):
            raise ValueError("Invalid historical attempt identity")
        if identity["scope"] != value["source_scope"]:
            raise ValueError("Historical attempt belongs to a different source scope")
    if value.get("identity") is not None:
        if value["identity"]["job"] != value.get("selected"):
            raise ValueError("Historical selected job and sample attempt disagree")
        if (
            value.get("selected") in attempts
            and attempts[value["selected"]] != value["identity"]
        ):
            raise ValueError("Historical selected attempt conflicts with its inventory")
    return value


@dataclass(frozen=True)
class Prepared:
    """Validated internal publication; decoding and hashing happen off the UI."""

    raw: bytes
    observed_at: float
    monotonic: float
    observer_id: str
    source_scope: str
    sha256: str


def prepare(raw):
    value = validate(raw)
    return Prepared(
        raw,
        value["observed_at"],
        value["monotonic"],
        value["observer_id"],
        value["source_scope"],
        hashlib.sha256(raw).hexdigest(),
    )


def captured(store, selected=None):
    return prepare(capture(store, selected))


class Timeline:
    def __init__(self):
        self.frames = deque()
        self.bytes = 0
        self.next_sequence = 1
        self.evicted = 0
        self.clock_epoch = 0

    def __eq__(self, other):
        # Retained observations are presentation data, not a live service.
        # Value equality also lets report isolation checks detect mutations
        # after an independent deep copy, including retention metadata.
        if type(other) is not type(self):
            return NotImplemented
        return vars(self) == vars(other)

    def append(self, raw):
        return self.append_prepared(prepare(raw))

    def append_prepared(self, value):
        if type(value) is not Prepared:
            raise TypeError("Expected a validated internal historical publication")
        if self.frames and (
            value.observed_at < self.frames[-1]["observed_at"]
            or value.monotonic < self.frames[-1]["monotonic"]
            or value.observer_id != self.frames[-1]["observer_id"]
            or value.source_scope != self.frames[-1]["source_scope"]
        ):
            self.clock_epoch += 1
        frame = {
            "sequence": self.next_sequence,
            "clock_epoch": self.clock_epoch,
            "observed_at": value.observed_at,
            "monotonic": value.monotonic,
            "observer_id": value.observer_id,
            "source_scope": value.source_scope,
            "sha256": value.sha256,
            "raw": value.raw,
        }
        self.next_sequence += 1
        self.frames.append(frame)
        self.bytes += len(value.raw)
        while len(self.frames) > MAX_SNAPSHOTS or self.bytes > MAX_ARCHIVE:
            self.bytes -= len(self.frames.popleft()["raw"])
            self.evicted += 1
        return frame["sequence"]

    def clone(self):
        """O(retained frames), sharing only immutable bytes/scalar metadata."""
        result = Timeline()
        result.frames = deque(dict(frame) for frame in self.frames)
        result.bytes, result.next_sequence = self.bytes, self.next_sequence
        result.evicted, result.clock_epoch = self.evicted, self.clock_epoch
        return result

    def at(self, timestamp, *, clock_epoch=None):
        if not _finite(timestamp):
            raise ValueError(
                "Use a finite Unix timestamp or an ISO timestamp with an explicit timezone"
            )
        epochs = {frame["clock_epoch"] for frame in self.frames}
        if len(epochs) > 1 and clock_epoch is None:
            raise ValueError(
                "Observer, source scope or clock changed; choose an explicit clock epoch or navigate by sequence"
            )
        candidates = [
            frame
            for frame in self.frames
            if frame["observed_at"] <= timestamp
            and (clock_epoch is None or frame["clock_epoch"] == clock_epoch)
        ]
        if not candidates:
            return {
                "status": "unknown",
                "reason": "No observation retained at or before this time",
                "evicted": self.evicted,
            }
        frame = candidates[-1]
        return self.read(frame["sequence"], requested_at=timestamp)

    def read(self, sequence, *, requested_at=None):
        frame = next(
            (frame for frame in self.frames if frame["sequence"] == sequence), None
        )
        if frame is None:
            return {
                "status": "unknown",
                "reason": "Snapshot is absent or expired from bounded retention",
                "evicted": self.evicted,
            }
        value = validate(frame["raw"])
        return {
            "status": "observed",
            "snapshot": value,
            "sequence": sequence,
            "clock_epoch": frame["clock_epoch"],
            "sha256": frame["sha256"],
            "requested_at": requested_at,
            "evicted": self.evicted,
            "unknown_since": value["observed_at"]
            if requested_at is not None and requested_at > value["observed_at"]
            else None,
            "semantics": "Last retained observation; changes after observation time are unknown, not interpolated",
        }

    def document(self):
        return {
            "schema": SCHEMA,
            "evicted": self.evicted,
            "frames": [
                {key: value for key, value in frame.items() if key != "raw"}
                | {"snapshot": json.loads(frame["raw"])}
                for frame in self.frames
            ],
        }

    @classmethod
    def load(cls, raw):
        from .transport.framing import unique_object, finite_constant

        if len(raw) > MAX_ARCHIVE + (64 << 10):
            raise ValueError("Historical archive exceeds its byte bound")
        try:
            value = json.loads(
                raw, object_pairs_hook=unique_object, parse_constant=finite_constant
            )
        except (ValueError, UnicodeError, RecursionError) as exc:
            raise ValueError("Malformed bounded historical archive") from exc
        if (
            not isinstance(value, dict)
            or value.get("schema") != SCHEMA
            or not isinstance(value.get("frames"), list)
            or len(value["frames"]) > MAX_SNAPSHOTS
        ):
            raise ValueError("Unsupported historical archive schema or inventory")
        result = cls()
        previous = 0
        for item in value["frames"]:
            if (
                not isinstance(item, dict)
                or type(item.get("sequence")) is not int
                or not previous < item["sequence"] < 2**63
            ):
                raise ValueError("Historical sequence must increase exactly once")
            snapshot = encoded(item.get("snapshot"))
            if hashlib.sha256(snapshot).hexdigest() != item.get("sha256"):
                raise ValueError("Historical snapshot checksum mismatch")
            content = validate(snapshot)
            if (
                type(item.get("clock_epoch")) is not int
                or item["clock_epoch"] < 0
                or any(
                    item.get(key) != content[key]
                    for key in (
                        "observed_at",
                        "monotonic",
                        "observer_id",
                        "source_scope",
                    )
                )
            ):
                raise ValueError(
                    "Historical clock metadata differs from its retained observation"
                )
            if not result.frames:
                result.clock_epoch = item["clock_epoch"]
            result.next_sequence = item["sequence"]
            result.append(snapshot)
            if result.clock_epoch != item["clock_epoch"]:
                raise ValueError(
                    "Historical clock epochs disagree with observed clock changes"
                )
            previous = item["sequence"]
        omitted = value.get("evicted", 0)
        if type(omitted) is not int or omitted < 0:
            raise ValueError("Invalid historical retention count")
        result.evicted += omitted
        return result


def timestamp(text):
    try:
        number = float(text)
    except ValueError:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if moment.tzinfo is None:
            raise ValueError(
                "Historical ISO timestamps need an explicit timezone, for example +00:00"
            )
        number = moment.timestamp()
    if not math.isfinite(number):
        raise ValueError("Historical timestamp must be finite")
    return number


def difference(left, right):
    """Compare retained observations, binding attempt changes instead of job ID."""

    def rows(snapshot):
        result = {}
        ambiguous = set()
        for group in ("jobs", "history"):
            for row in snapshot[group]:
                identity = snapshot.get("attempts", {}).get(str(row.get("id", "")), {})
                exact = (
                    (str(identity.get("scope", "")), str(identity.get("attempt", "")))
                    if identity.get("verified")
                    else (
                        str(snapshot["source_scope"]),
                        "unverified@" + str(snapshot["observer_id"]),
                    )
                )
                key = (
                    str(row.get("cluster", "")),
                    str(row.get("id", "")),
                    str(row.get("submit", "")),
                    str(row.get("start", "")),
                    *exact,
                )
                if key in result:
                    ambiguous.add(key)
                result[key] = row
        for key in ambiguous:
            result.pop(key, None)
        return result, ambiguous

    a, ambiguous_a = rows(left)
    b, ambiguous_b = rows(right)
    ambiguous = ambiguous_a | ambiguous_b
    a = {key: row for key, row in a.items() if key not in ambiguous}
    b = {key: row for key, row in b.items() if key not in ambiguous}
    return {
        "appeared": sorted(b.keys() - a.keys()),
        "disappeared": sorted(a.keys() - b.keys()),
        "changed": [
            {
                "identity": key,
                "fields": {
                    name: [a[key].get(name), b[key].get(name)]
                    for name in a[key].keys() | b[key].keys()
                    if a[key].get(name) != b[key].get(name)
                },
            }
            for key in sorted(a.keys() & b.keys())
            if a[key] != b[key]
        ],
        "ambiguous": sorted(ambiguous),
        "limits": "Rows without verified attempt identity may describe different attempts; selected telemetry uses its exact attempt identity. Differences describe observations, not unobserved transitions.",
    }
