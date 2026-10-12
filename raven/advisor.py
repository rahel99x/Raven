"""The resource advisor: what a job should have asked for, from what it used.

A finished job: its peak memory against the request, its CPU efficiency, its elapsed against the limit (what
``seff`` shows, turned into flags).  A running job: the same so far, plus the history of jobs with the same name
(how long they took, what they peaked at), so the projection has a basis before the job ends.  A job name over
the window: the runs aggregated, with the core-hours the over-request wasted."""
from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from typing import Dict, List, Optional, Sequence

from .model import Finished, Job, Live, hms, human, secs

MEM_HEADROOM = 1.25            # suggested memory = peak × this, rounded up to a round number
TIME_HEADROOM = 1.3            # suggested limit = the longest run × this, rounded up to 15 minutes
CPU_TARGET = 0.7               # suggested cores = cpu time / (elapsed × this)

MAX_HISTORY_CACHE_ENTRIES = 2
MAX_HISTORY_CACHE_RECORDS = 50_000


def round_mem(b: float) -> str:
    """Bytes -> a round sbatch --mem value: 1G steps above 4G, 512M below, never under 1G."""
    gb = b / 1024 ** 3
    if gb >= 4:
        return f"{int(math.ceil(gb))}G"
    if gb >= 1:
        return f"{int(math.ceil(gb * 2)) / 2:g}G"
    return "1G"


def round_time(s: float) -> str:
    """Seconds -> HH:MM:SS rounded up to 15 minutes, at least 15 minutes."""
    q = 900
    s = max(q, q * math.ceil(s / q))
    return hms(s)


@dataclass
class Advice:
    id: str
    name: str = ""
    state: str = ""
    runs: int = 1                            # runs the advice rests on (same name, completed)
    cpus: int = 0
    cpus_suggest: Optional[int] = None
    cpu_eff: Optional[float] = None
    mem_req: float = 0.0                     # bytes
    mem_peak: float = 0.0
    mem_suggest: str = ""
    limit: Optional[float] = None            # seconds
    elapsed: Optional[float] = None          # this run so far, or the longest completed run
    time_suggest: str = ""
    notes: List[str] = field(default_factory=list)
    wasted_core_hours: float = 0.0           # over the runs: (1 - eff) × core-hours
    partial: bool = False                    # a running job: so far

    def flags(self) -> str:
        """The sbatch flags to copy."""
        out = []
        if self.mem_suggest:
            out.append(f"--mem={self.mem_suggest}")
        if self.cpus_suggest and self.cpus_suggest != self.cpus:
            out.append(f"--cpus-per-task={self.cpus_suggest}")
        if self.time_suggest:
            out.append(f"--time={self.time_suggest}")
        return " ".join(out)

    def summary(self, dot: str = "·") -> str:
        bits = []
        if self.mem_suggest:
            bits.append(f"--mem {self.mem_suggest} (peak {human(self.mem_peak)} of {human(self.mem_req) if self.mem_req else '?'})")
        if self.cpus_suggest and self.cpus_suggest != self.cpus:
            bits.append(f"--cpus-per-task {self.cpus_suggest} (eff {100 * (self.cpu_eff or 0):.0f}% on {self.cpus})")
        if self.time_suggest:
            bits.append(f"--time {self.time_suggest} ({'longest of ' + str(self.runs) + ' runs ' if self.runs > 1 else ''}{hms(self.elapsed) if self.elapsed else '?'} of {hms(self.limit) if self.limit else '?'})")
        return f" {dot} ".join(bits + self.notes)


def _suggest(a: Advice) -> Advice:
    """Fill the suggestions from the measurements already on ``a``."""
    if a.mem_peak > 0:
        want = a.mem_peak * MEM_HEADROOM
        if a.state == "OUT_OF_MEMORY":
            want = max(want, a.mem_req * 2 if a.mem_req else a.mem_peak * 2)
            a.notes.append("ran out of memory: doubled")
        s = round_mem(want)
        if not a.mem_req or abs(want - a.mem_req) / a.mem_req > 0.15 or a.state == "OUT_OF_MEMORY":
            a.mem_suggest = s
    if a.cpu_eff is not None and a.cpus:
        if a.cpu_eff < 0.5 and not a.partial:
            a.cpus_suggest = max(1, int(math.ceil(a.cpu_eff * a.cpus / CPU_TARGET)))
        elif a.cpu_eff < 0.3 and a.partial:
            a.cpus_suggest = max(1, int(math.ceil(a.cpu_eff * a.cpus / CPU_TARGET)))
            a.notes.append("so far")
    if a.elapsed:
        want = a.elapsed * TIME_HEADROOM
        if a.state == "TIMEOUT" and a.limit:
            want = max(want, a.limit * 2)
            a.notes.append("timed out: doubled")
        if a.limit and (want < a.limit * 0.6 or want > a.limit):
            a.time_suggest = round_time(want)
        elif not a.limit:
            a.time_suggest = round_time(want)
    return a


def advise_finished(f: Finished, runs: Sequence[Finished] = ()) -> Advice:
    """One finished job, with the completed runs of the same name (``runs``) widening the memory and time figures."""
    same = [r for r in runs if r.name == f.name and r.state in ("COMPLETED", "TIMEOUT", "OUT_OF_MEMORY", "FAILED")] or [f]
    a = Advice(id=f.id, name=f.name, state=f.state, runs=len(same), cpus=f.cpus, cpu_eff=f.cpu_eff, mem_req=f.req_mem, limit=secs(f.limit) if f.limit else None)
    a.mem_peak = max(r.rss for r in same)
    a.elapsed = max((secs(r.elapsed) or 0) for r in same)
    effs = [r.cpu_eff for r in same if r.cpu_eff is not None]
    a.cpu_eff = sum(effs) / len(effs) if effs else None
    a.wasted_core_hours = sum((1 - r.cpu_eff) * r.core_hours for r in same if r.cpu_eff is not None)
    return _suggest(a)


def running_notes(j: Job, lv: Optional[Live], finished: Sequence[Finished]) -> List[str]:
    """Measure the existing running warnings without reading metric series."""
    same = [r for r in finished if r.name == j.name and r.state == "COMPLETED"]
    notes = []
    if same:
        elapsed = max(secs(r.elapsed) or 0 for r in same)
        if j.limit_s and elapsed > j.limit_s:
            notes.append(f"earlier runs took longer than this limit: {hms(elapsed)}")
        current = j.elapsed_s or 0
        if j.limit_s and current > elapsed * 1.2 and current > 600:
            notes.append(f"already {hms(current - elapsed)} past the longest completed run")
    if lv and lv.avg is not None and lv.avg < .3 and j.cpus:
        notes.append("so far")
    return notes


def advise_running(j: Job, lv: Optional[Live], series: Sequence[dict], finished: Sequence[Finished], limit_hint: Optional[float] = None) -> Advice:
    """A running job: peak memory and CPU efficiency so far, the limit against the completed runs of the same name."""
    a = Advice(id=j.id, name=j.name, state=j.state, cpus=j.cpus, mem_req=j.mem_bytes, limit=j.limit_s, partial=True)
    peak = max([s.get("rss") or 0 for s in series if s.get("k") == "live"] + [lv.rss if lv and lv.rss else 0])
    a.mem_peak = peak
    a.cpu_eff = lv.avg if lv and lv.avg is not None else None
    ended = [r for r in finished if r.name == j.name and r.state in ("COMPLETED", "TIMEOUT", "OUT_OF_MEMORY", "FAILED")]
    same = [r for r in ended if r.state == "COMPLETED"]
    a.runs = len(same) + 1
    if ended:
        a.mem_peak = max(a.mem_peak, max(r.rss for r in ended))
    if same:
        a.elapsed = max(secs(r.elapsed) or 0 for r in same)
    else:
        a.elapsed = None
    # Historical warnings come first. The shared suggestion rules append
    # memory, CPU and timeout notes in their original order, including a
    # terminal state observed before the queue refresh removes this record.
    a.notes = running_notes(j, None, finished)
    out = _suggest(a)
    if not same:
        out.time_suggest = ""
    return out


def advise_names(finished: Sequence[Finished]) -> List[Advice]:
    """One Advice per job name over the history, sorted by the core-hours the over-request wasted."""
    by: Dict[str, List[Finished]] = {}
    for f in finished:
        by.setdefault(f.name, []).append(f)
    out = []
    for name, runs in by.items():
        done = [r for r in runs if r.state in ("COMPLETED", "TIMEOUT", "OUT_OF_MEMORY", "FAILED")]
        if not done:
            continue
        oom = [r for r in done if r.state == "OUT_OF_MEMORY"]
        tmo = [r for r in done if r.state == "TIMEOUT"]
        last = max(oom or tmo or done, key=lambda r: r.end)   # a failure sets the doubling
        a = advise_finished(last, done)
        a.id = f"{len(done)} run{'s' if len(done) != 1 else ''}"
        req_hours = sum((secs(r.elapsed) or 0) * r.cpus for r in done) / 3600
        a.wasted_core_hours = sum((1 - r.cpu_eff) * r.core_hours for r in done if r.cpu_eff is not None)
        a.notes = [n for n in a.notes if "doubled" not in n]
        if any(r.state == "OUT_OF_MEMORY" for r in done):
            a.notes.append(f"{sum(r.state == 'OUT_OF_MEMORY' for r in done)} out of memory")
        if any(r.state == "TIMEOUT" for r in done):
            a.notes.append(f"{sum(r.state == 'TIMEOUT' for r in done)} timed out")
        out.append(a)
    out.sort(key=lambda a: -a.wasted_core_hours)
    return out


@dataclass
class _HistoryAdviceEntry:
    key: tuple
    groups: dict
    advice: Optional[tuple] = None


class HistoryAdviceCache:
    """Reuse advice for exact accounting content, without retaining job objects.

    Callers still filter their accounting window on every frame. Keys include
    record order, every field consumed by the advisor, and suggestion settings,
    so corrections and equal-time tie choices cannot reuse an older result.
    Only two entries and 50,000 records in total may be retained. Larger inputs
    use the ordinary uncached calculation.
    """

    def __init__(self, max_entries=MAX_HISTORY_CACHE_ENTRIES, max_records=MAX_HISTORY_CACHE_RECORDS):
        self.max_entries = max(0, min(MAX_HISTORY_CACHE_ENTRIES, int(max_entries)))
        self.max_records = max(0, min(MAX_HISTORY_CACHE_RECORDS, int(max_records)))
        self._entries = []
        self._record_count = 0

    @property
    def entry_count(self):
        return len(self._entries)

    @property
    def record_count(self):
        return self._record_count

    def _entry(self, finished):
        count = len(finished)
        if not self.max_entries or count > self.max_records:
            return None
        key = ((MEM_HEADROOM, TIME_HEADROOM, CPU_TARGET), tuple(
            (f.id, f.name, f.state, f.end, f.elapsed, f.cpus, f.cpu_time, f.req_mem, f.rss, f.limit)
            for f in finished))
        # Two exact-content entries do not need repeated hashes of every record.
        # Moving a hit to the end keeps the bounded list in LRU order.
        for index, entry in enumerate(self._entries):
            if entry.key == key:
                if index != len(self._entries) - 1:
                    self._entries.append(self._entries.pop(index))
                return entry
        grouped = {}
        for index, record in enumerate(key[1]):
            grouped.setdefault(record[1], []).append(index)
        entry = _HistoryAdviceEntry(key, {name: tuple(indices) for name, indices in grouped.items()})
        while self._entries and (len(self._entries) >= self.max_entries
                                 or self._record_count + count > self.max_records):
            self._record_count -= len(self._entries.pop(0).key[1])
        self._entries.append(entry)
        self._record_count += count
        return entry

    def names(self, finished: Sequence[Finished]) -> List[Advice]:
        """Return fresh advice and notes, reusing only the private calculation."""
        entry = self._entry(finished)
        if entry is None:
            return advise_names(finished)
        if entry.advice is None:
            # Capture trusted dataclass fields rather than a mutable source
            # reference. An accounting correction during calculation cannot
            # attach a changed result to the preceding exact-content key.
            captured = [Finished(id=value[0], name=value[1], state=value[2], end=value[3],
                                 elapsed=value[4], cpus=value[5], cpu_time=value[6],
                                 req_mem=value[7], rss=value[8], limit=value[9])
                        for value in entry.key[1]]
            entry.advice = tuple(advise_names(captured))
        return [replace(advice, notes=list(advice.notes)) for advice in entry.advice]

    def groups(self, finished: Sequence[Finished]) -> Dict[str, tuple]:
        """Group current records once so running jobs inspect only their name.

        Cached groups contain indices. Every returned record comes from this
        call's sequence, including equal-value objects from a newer snapshot.
        """
        entry = self._entry(finished)
        if entry is not None:
            return {name: tuple(finished[index] for index in indices)
                    for name, indices in entry.groups.items()}
        grouped = {}
        for record in finished:
            grouped.setdefault(record.name, []).append(record)
        return {name: tuple(records) for name, records in grouped.items()}
