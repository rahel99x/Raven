"""Session recording and replay.  ``RecordingBackend`` wraps any backend and appends every command and its answer
(or error) to a JSONL file (gzipped when the name ends in .gz); ``ReplayBackend`` answers the same commands from
such a file at the recording's own time, driven by a ``ReplayClock`` that can pause, scrub and run faster.  A
recording is also a test fixture: the whole dashboard runs against it without a cluster."""
from __future__ import annotations

import bisect
import gzip
import json
import math
import os
import threading
import time
import uuid
from typing import Dict, List, Optional, Sequence, Tuple

from .slurm import Backend, CommandError, GROUP_FMT, JOB_FMT, SACCT_FIELDS, SACCT_LEGACY_FIELDS

# These are released wire formats, not prefixes of the current format. Each
# optional-field upgrade must retain the exact older command keys in recordings.
_LEGACY_FORMATS = {
    "squeue": {
        JOB_FMT: (
            "%i|%j|%P|%T|%M|%l|%D|%C|%b|%N|%m|%S|%V|%r|%Q|%E|%a|%q|%e|%o|%Z",
            "%i|%j|%P|%T|%M|%l|%D|%C|%b|%N|%m|%S|%V|%r|%Q|%E|%a|%q|%e|%o",
        ),
        GROUP_FMT: (
            "%i|%u|%j|%P|%T|%M|%l|%D|%C|%b|%r|%Q|%N|%V|%S|%a|%o|%Z",
            "%i|%u|%j|%P|%T|%M|%l|%D|%C|%b|%r|%Q|%N|%V|%S",
        ),
    },
    "sacct": {SACCT_FIELDS: (SACCT_LEGACY_FIELDS,)},
}


def _open(path: str, mode: str):
    return gzip.open(path, mode + "t", encoding="utf-8") if path.endswith(".gz") else open(path, mode, encoding="utf-8")


def _finite(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def _json_float(text):
    value = float(text)
    if not math.isfinite(value):
        raise ValueError("Recording contains a nonfinite JSON number")
    return value


class RecordingBackend(Backend):
    def __init__(self, inner: Backend, path: str, meta: Optional[dict] = None):
        self.inner, self.path = inner, path
        self.probe_json = getattr(inner, "probe_json", False)
        self.lock = threading.Lock()
        self.n = 0
        self.observer_id = uuid.uuid4().hex
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        self.f = _open(path, "a")
        self._write(dict(meta or {}, kind="header", schema="raven.session-recording/v3",
                         timestamp_semantics="observer publication completion; requested_at retained separately"))

    def can_probe_json(self, command: str) -> bool:
        check = getattr(self.inner, "can_probe_json", None)
        return bool(check(command)) if callable(check) else self.probe_json

    def _write(self, rec: dict):
        with self.lock:
            if self.f.closed:
                return
            # Stamp publication under the same lock as file order. Concurrent
            # command completions cannot reorder a single observer's sequence.
            rec = dict(rec, observer_id=self.observer_id, sequence=self.n,
                       monotonic=time.monotonic(), t=time.time())
            if rec.get("kind") != "header":
                rec["observed_at"] = rec["t"]
            self.f.write(json.dumps(rec, default=str) + "\n")
            self.f.flush()
            self.n += 1

    def run(self, cmd: Sequence[str], timeout: float = 8.0) -> Tuple[str, float]:
        t = time.time()
        try:
            out, dt = self.inner.run(cmd, timeout)
        except CommandError as e:
            self._write(dict(requested_at=t, cmd=list(cmd), err=str(e)))
            raise
        self._write(dict(requested_at=t, cmd=list(cmd), out=out, dt=round(dt, 4)))
        return out, dt

    def call(self, cmd: Sequence[str], timeout: float = 15.0) -> Tuple[bool, str]:
        t = time.time()
        ok, out = self.inner.call(cmd, timeout)
        self._write(dict(requested_at=t, call=list(cmd), ok=ok, out=out))
        return ok, out

    def close(self):
        with self.lock:
            try:
                self.f.close()
            except OSError:
                pass


def read_recording(path: str) -> Tuple[dict, List[dict]]:
    """Bounded records, checking observer/file order before timestamp sorting.

    Legacy and ambiguous recordings remain useful reconstructions. Only one
    internally consistent v3 observer/clock segment supports known-at-time.
    These checks establish consistency, not independent producer authenticity.
    """
    from .transport.framing import unique_object, finite_constant
    header, entries = {}, []
    total, omitted = 0, 0
    headers, observer, previous_wall, previous_mono, previous_sequence = 0, None, None, None, None
    reasons = set()
    with _open(path, "r") as f:
        while True:
            try:
                line = f.readline((8 << 20) + 1)
            except (UnicodeError, EOFError) as exc:
                raise CommandError("Recording is not readable UTF-8 JSONL: " + str(exc)[:160]) from exc
            if not line:
                break
            line_bytes = len(line.encode("utf-8"))
            total += line_bytes
            if line_bytes > 8 << 20 or total > 64 << 20 or len(entries) >= 200000:
                raise CommandError("Recording exceeds bounded replay input; select a smaller retained recording")
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line, object_pairs_hook=unique_object, parse_constant=finite_constant,
                                 parse_float=_json_float)
            except (ValueError, RecursionError):
                omitted += 1
                continue
            if not isinstance(rec, dict):
                omitted += 1
                continue
            if rec.get("kind") == "header":
                headers += 1
                user = rec.get("user", "")
                if (not isinstance(user, str) or len(user) > 256
                        or any(ord(ch) < 32 or ord(ch) == 127 or 0xD800 <= ord(ch) <= 0xDFFF for ch in user)):
                    raise CommandError("Recording header user must be a bounded printable string")
                header = header or rec
                candidate = rec.get("observer_id")
                if headers > 1:
                    reasons.add("multiple observer headers")
                if (rec.get("schema") != "raven.session-recording/v3"
                        or not isinstance(candidate, str) or len(candidate) != 32
                        or any(ch not in "0123456789abcdef" for ch in candidate)
                        or type(rec.get("sequence")) is not int or rec["sequence"] != 0
                        or not _finite(rec.get("monotonic")) or not _finite(rec.get("t"))):
                    reasons.add("legacy or missing observer/clock provenance")
                else:
                    observer = candidate
                    previous_wall, previous_mono, previous_sequence = rec["t"], rec["monotonic"], 0
            else:
                timestamp = rec.get("observed_at", rec.get("t"))
                command = rec.get("cmd", rec.get("call"))
                if (not _finite(timestamp) or not -62135596800 <= timestamp <= 253402300799
                        or not isinstance(command, list) or not 1 <= len(command) <= 512
                        or any(not isinstance(part, str) or len(part) > 65536 for part in command)
                        or "out" in rec and not isinstance(rec["out"], str)
                        or "err" in rec and not isinstance(rec["err"], str)
                        or not _finite(rec.get("dt", 0)) or rec.get("dt", 0) < 0):
                    omitted += 1
                    continue
                if previous_wall is not None and timestamp < previous_wall:
                    reasons.add("wall clock moved backward in file order")
                mono, sequence = rec.get("monotonic"), rec.get("sequence")
                if (headers != 1 or observer is None or rec.get("observer_id") != observer
                        or type(sequence) is not int or previous_sequence is None or sequence != previous_sequence + 1
                        or not _finite(mono) or previous_mono is None or mono < previous_mono
                        or "observed_at" not in rec or not _finite(rec.get("t")) or rec["t"] != timestamp
                        or not _finite(rec.get("requested_at")) or rec["requested_at"] > timestamp):
                    reasons.add("legacy or inconsistent observer/clock provenance")
                previous_wall = timestamp
                previous_mono = mono if _finite(mono) else None
                previous_sequence = sequence if type(sequence) is int else None
                rec["t"] = timestamp
                entries.append(rec)
    if headers != 1:
        reasons.add("legacy or missing single observer header")
    if omitted:
        reasons.add("invalid records omitted; observation coverage is incomplete")
    entries.sort(key=lambda r: r.get("t", 0))
    header = dict(header, replay_read_diagnostics={"omitted_invalid_records": omitted, "decoded_bytes": total,
        "known_at_time": not reasons, "reconstruction_reasons": sorted(reasons), "observer_headers": headers})
    return header, entries


class ReplayClock:
    """The recording's time, advancing at ``speed`` × the wall clock from where it was last set; pausable."""

    def __init__(self, t0: float, t1: float, speed: float = 1.0, paused: bool = False):
        if any(not _finite(value) for value in (t0, t1, speed)) or speed < 0:
            raise ValueError("Replay clocks require finite timestamps and a nonnegative finite speed")
        self.t0, self.t1, self.speed = t0, max(t1, t0), speed
        self.paused = paused
        self.base_t, self.base_wall = t0, time.monotonic()
        self.loops = 0
        self.generation = 0

    def now(self) -> float:
        if self.paused:
            return self.base_t
        t = self.base_t + (time.monotonic() - self.base_wall) * self.speed
        return min(max(t, self.t0), self.t1)

    def seek(self, t: float):
        if type(t) not in (int, float) or not math.isfinite(t):
            raise ValueError("Replay seek requires a finite timestamp")
        self.generation += 1
        self._anchor(t)

    def _anchor(self, t: float):
        self.base_t, self.base_wall = min(max(t, self.t0), self.t1), time.monotonic()

    def skip(self, seconds: float):
        self.seek(self.now() + seconds)

    def set_speed(self, speed: float):
        if type(speed) not in (int, float) or not math.isfinite(speed) or speed < 0:
            raise ValueError("Replay speed must be finite and nonnegative")
        self._anchor(self.now())
        self.speed = max(0.0, speed)

    def toggle_pause(self) -> bool:
        self._anchor(self.now())
        self.paused = not self.paused
        return self.paused

    @property
    def frac(self) -> float:
        span = self.t1 - self.t0
        return 0.0 if span <= 0 else (self.now() - self.t0) / span

    @property
    def at_end(self) -> bool:
        return not self.paused and self.now() >= self.t1


class ReplayBackend(Backend):
    """Answers each command with what the recording holds for it at the clock's time (the latest answer at or
    before it). Unknown earlier intervals stay unknown; actions are refused."""

    def __init__(self, path: str, speed: float = 1.0, paused: bool = False):
        self.path = path
        self.header, self.entries = read_recording(path)
        if not self.entries:
            omitted = self.header["replay_read_diagnostics"]["omitted_invalid_records"]
            raise CommandError(f"{path}: an empty recording ({omitted} invalid records omitted)")
        self.by_key: Dict[Tuple[str, ...], List[Tuple[float, dict]]] = {}
        for rec in self.entries:
            if "cmd" in rec:
                self.by_key.setdefault(tuple(rec["cmd"]), []).append((rec["t"], rec))
        self._times = {key: [t for t, _ in values] for key, values in self.by_key.items()}
        self.t0, self.t1 = self.entries[0]["t"], self.entries[-1]["t"]
        self.clock = ReplayClock(self.t0, self.t1, speed=speed, paused=paused)
        self.calls: List[List[str]] = []
        self.user = self.header.get("user", "")
        diagnostics = self.header["replay_read_diagnostics"]
        self.replay_mode = "known-at-time" if diagnostics["known_at_time"] else "reconstructed legacy/ambiguous observations"
        self.replay_diagnostics = diagnostics

    def can_probe_json(self, command: str) -> bool:
        # Replay has no live capabilities. An older recording can only answer
        # its recorded format; a new recording may capture each help probe.
        return (command, "--help") in self.by_key

    def _pick(self, key: Tuple[str, ...], now: float) -> Optional[dict]:
        lst = self.by_key.get(key)
        if not lst:
            return None
        index = bisect.bisect_right(self._times[key], now) - 1
        return lst[index][1] if index >= 0 else None

    def run(self, cmd: Sequence[str], timeout: float = 8.0) -> Tuple[str, float]:
        key, now = tuple(cmd), self.clock.now()
        rec = self._pick(key, now)
        # Only known optional-field upgrades can read a legacy response. Keep
        # every other argument (including user, account, and history interval)
        # exact. A recorded current-format result or error always takes priority.
        if rec is None and key not in self.by_key and key and "-o" in key:
            index = key.index("-o") + 1
            formats = _LEGACY_FORMATS.get(os.path.basename(key[0]), {})
            legacy_formats = formats.get(key[index], ()) if index < len(key) else ()
            for legacy in legacy_formats:
                rec = self._pick(key[:index] + (legacy,) + key[index + 1:], now)
                if rec is not None:
                    break
        if rec is None:
            reason = "no observation retained at or before this replay time" if key in self.by_key else "not in the recording at or before this replay time"
            raise CommandError(f"{os.path.basename(cmd[0])}: {reason}")
        if "err" in rec:
            raise CommandError(rec["err"])
        return rec.get("out", ""), float(rec.get("dt", 0.0))

    def call(self, cmd: Sequence[str], timeout: float = 15.0) -> Tuple[bool, str]:
        self.calls.append(list(cmd))
        return False, "replay: actions are disabled"

    def summary(self) -> str:
        n = len(self.entries)
        cmds = len(self.by_key)
        span = self.t1 - self.t0
        detail = "; ".join(self.replay_diagnostics["reconstruction_reasons"])
        if self.replay_diagnostics["omitted_invalid_records"]:
            detail += f"; {self.replay_diagnostics['omitted_invalid_records']} invalid records omitted"
        return f"{self.replay_mode}: {n} answers to {cmds} commands over {span / 60:.1f} min from {time.strftime('%Y-%m-%d %H:%M', time.localtime(self.t0))}" + ("; " + detail if detail else "")
