"""Session recording and replay.  ``RecordingBackend`` wraps any backend and appends every command and its answer
(or error) to a JSONL file (gzipped when the name ends in .gz); ``ReplayBackend`` answers the same commands from
such a file at the recording's own time, driven by a ``ReplayClock`` that can pause, scrub and run faster.  A
recording is also a test fixture: the whole dashboard runs against it without a cluster."""
from __future__ import annotations

import bisect
import gzip
import json
import os
import threading
import time
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


class RecordingBackend(Backend):
    def __init__(self, inner: Backend, path: str, meta: Optional[dict] = None):
        self.inner, self.path = inner, path
        self.lock = threading.Lock()
        self.n = 0
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        self.f = _open(path, "a")
        self._write(dict(kind="header", t=time.time(), **(meta or {})))

    def _write(self, rec: dict):
        with self.lock:
            if self.f.closed:
                return
            self.f.write(json.dumps(rec, default=str) + "\n")
            self.f.flush()
            self.n += 1

    def run(self, cmd: Sequence[str], timeout: float = 8.0) -> Tuple[str, float]:
        t = time.time()
        try:
            out, dt = self.inner.run(cmd, timeout)
        except CommandError as e:
            self._write(dict(t=t, cmd=list(cmd), err=str(e)))
            raise
        self._write(dict(t=t, cmd=list(cmd), out=out, dt=round(dt, 4)))
        return out, dt

    def call(self, cmd: Sequence[str], timeout: float = 15.0) -> Tuple[bool, str]:
        t = time.time()
        ok, out = self.inner.call(cmd, timeout)
        self._write(dict(t=t, call=list(cmd), ok=ok, out=out))
        return ok, out

    def close(self):
        with self.lock:
            try:
                self.f.close()
            except OSError:
                pass


def read_recording(path: str) -> Tuple[dict, List[dict]]:
    """(header, entries) of a recording file; entries are sorted by time."""
    header, entries = {}, []
    with _open(path, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if not isinstance(rec, dict):
                continue
            if rec.get("kind") == "header":
                header = header or rec
            else:
                entries.append(rec)
    entries.sort(key=lambda r: r.get("t", 0))
    return header, entries


class ReplayClock:
    """The recording's time, advancing at ``speed`` × the wall clock from where it was last set; pausable."""

    def __init__(self, t0: float, t1: float, speed: float = 1.0, paused: bool = False):
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
        self.generation += 1
        self._anchor(t)

    def _anchor(self, t: float):
        self.base_t, self.base_wall = min(max(t, self.t0), self.t1), time.monotonic()

    def skip(self, seconds: float):
        self.seek(self.now() + seconds)

    def set_speed(self, speed: float):
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
    before it, else the first one); actions are refused."""

    def __init__(self, path: str, speed: float = 1.0, paused: bool = False):
        self.path = path
        self.header, self.entries = read_recording(path)
        if not self.entries:
            raise CommandError(f"{path}: an empty recording")
        self.by_key: Dict[Tuple[str, ...], List[Tuple[float, dict]]] = {}
        for rec in self.entries:
            if "cmd" in rec:
                self.by_key.setdefault(tuple(rec["cmd"]), []).append((rec["t"], rec))
        self._times = {key: [t for t, _ in values] for key, values in self.by_key.items()}
        self.t0, self.t1 = self.entries[0]["t"], self.entries[-1]["t"]
        self.clock = ReplayClock(self.t0, self.t1, speed=speed, paused=paused)
        self.calls: List[List[str]] = []
        self.user = self.header.get("user", "")

    def _pick(self, key: Tuple[str, ...], now: float) -> Optional[dict]:
        lst = self.by_key.get(key)
        if not lst:
            return None
        index = max(0, bisect.bisect_right(self._times[key], now) - 1)
        return lst[index][1]

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
            raise CommandError(f"{os.path.basename(cmd[0])}: not in the recording")
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
        return f"{n} answers to {cmds} commands over {span / 60:.1f} min from {time.strftime('%Y-%m-%d %H:%M', time.localtime(self.t0))}"
