"""Indexed Advisor cards: measure metadata, derive only visible running advice."""
from __future__ import annotations

from bisect import bisect_right

from . import advisor
from .model import hms, human, secs
from .research import clean


def advice_key(items):
    return tuple((item.name, item.id, item.mem_peak, item.mem_req, item.mem_suggest,
                  item.cpus, item.cpus_suggest, item.cpu_eff, item.elapsed, item.limit,
                  item.time_suggest, item.wasted_core_hours, tuple(item.notes)) for item in items)


def running_key(jobs, live, groups):
    return tuple((job.id, job.name, job.state, job.cpus, job.mem_bytes, job.limit_s,
                  getattr(live.get(job.id), "avg", None),
                  max((secs(item.elapsed) or 0 for item in groups.get(job.name, ())
                       if item.state == "COMPLETED"), default=0),
                  tuple(advisor.running_notes(job, live.get(job.id), groups.get(job.name, ()))))
                 for job in jobs)


class AdvisorDocument:
    """Retain card boundaries instead of a whole rendered paragraph array."""

    def __init__(self, advice, running, width, ascii_, *, live=None, groups=None):
        live, groups = live or {}, groups or {}
        self.width, self.ascii = max(1, int(width)), bool(ascii_)
        self.key = (self.width, self.ascii, advice_key(advice),
                    running_key(running, live, groups))
        self.advice, self.running = list(advice), list(running)
        self.offsets = [0]
        for item in self.advice:
            self.offsets.append(self.offsets[-1] + sum(len(self._wrap(fact)) for fact in self._facts(item)))
        self.running_heading = bool(running)
        if running:
            self.offsets.append(self.offsets[-1] + 1)
        self.slots = []
        for job in self.running:
            # Slurm memory values are uint64. Reserve their maximum formatted
            # width without scanning series. Other values and every note can
            # be measured directly from the current job and cached history.
            previous = groups.get(job.name, ())
            longest = max((secs(item.elapsed) or 0 for item in previous
                           if item.state == "COMPLETED"), default=0)
            current = live.get(job.id)
            efficiency = getattr(current, "avg", None)
            notes = advisor.running_notes(job, current, previous)
            if job.state == "OUT_OF_MEMORY":
                position = len(notes) - (1 if notes and notes[-1] == "so far" else 0)
                notes.insert(position, "ran out of memory: doubled")
            if job.state == "TIMEOUT" and job.limit_s and longest:
                notes.append("timed out: doubled")
            candidates = (
                (" Peak memory unknown", " Peak memory " + human(2 ** 64)),
                (" Request " + (human(job.mem_bytes) if job.mem_bytes else "unknown"),),
                (" --mem unchanged", " --mem " + advisor.round_mem(2 ** 64 * advisor.MEM_HEADROOM)),
                (f" CPU {job.cpus} / eff {100 * efficiency:.0f}%" if efficiency is not None else f" CPU {job.cpus} / eff unknown",),
                (" --cpus-per-task unchanged", " --cpus-per-task " + str(job.cpus)),
                (" Limit " + (hms(job.limit_s) if job.limit_s else "unknown"),),
                (" --time needs past runs", " --time " + advisor.round_time(max(
                    longest * advisor.TIME_HEADROOM,
                    (job.limit_s or 0) * 2 if job.state == "TIMEOUT" else 0))),
                (" Notes " + (", ".join(notes) or "none"),))
            slots = [max(len(self._wrap((text, ""))) for text in options) for options in candidates]
            self.slots.append(slots)
            self.offsets.append(self.offsets[-1] + len(self._wrap((f" {job.id} / {job.name}", "cyan+bold"))) + sum(slots))
        self.count = self.offsets[-1]

    def bind(self, advice, running, derive):
        """Fresh published references do not invalidate equal metadata widths."""
        self.advice, self.running, self.derive = list(advice), list(running), derive

    def _facts(self, item):
        yield f" {item.name} / {item.id} runs", "bold"
        yield f" Memory peak {human(item.mem_peak) if item.mem_peak else '?'} / requested {human(item.mem_req) if item.mem_req else '?'} / suggested {item.mem_suggest or 'unchanged'}", ""
        yield (f" CPU {item.cpus} / suggested {item.cpus_suggest or 'unchanged'} / efficiency {100 * item.cpu_eff:.0f}%" if item.cpu_eff is not None else
               f" CPU {item.cpus} / suggested {item.cpus_suggest or 'unchanged'} / efficiency unavailable"), ""
        yield f" Time longest {hms(item.elapsed) if item.elapsed else '?'} / limit {hms(item.limit) if item.limit else '?'} / suggested {item.time_suggest or 'unchanged'}", ""
        yield f" Idle core-hours {item.wasted_core_hours:.1f} / notes {', '.join(item.notes) or 'none'}", "dim"
        yield " Flags " + (item.flags() or "nothing to change"), "cyan"

    def _wrap(self, fact):
        from .workspace_layout import _wrap_row
        text, style = fact
        return _wrap_row([(clean(text, self.ascii), style)], self.width)

    def render_card(self, index):
        """Render one measured card; running sample scans happen only here."""
        if index < len(self.advice):
            return [row for fact in self._facts(self.advice[index]) for row in self._wrap(fact)]
        index -= len(self.advice)
        if self.running_heading:
            if index == 0:
                return self._wrap((" Running jobs so far", "heading+bold"))[:1]
            index -= 1
        job = self.running[index]
        observed = self.derive(job)
        rows = self._wrap((f" {job.id} / {job.name}", "cyan+bold"))
        facts = ((" Peak memory " + (human(observed.mem_peak) if observed.mem_peak else "unknown"), ""),
                 (" Request " + (human(observed.mem_req) if observed.mem_req else "unknown"), ""),
                 (" --mem " + (observed.mem_suggest or "unchanged"), "cyan"),
                 (f" CPU {observed.cpus} / eff {100 * observed.cpu_eff:.0f}%" if observed.cpu_eff is not None else f" CPU {observed.cpus} / eff unknown", ""),
                 (" --cpus-per-task " + str(observed.cpus_suggest or "unchanged"), "cyan"),
                 (" Limit " + (hms(observed.limit) if observed.limit else "unknown"), ""),
                 (" --time " + (observed.time_suggest or "needs past runs"), "cyan"),
                 (" Notes " + (", ".join(observed.notes) or "none"), "dim"))
        for fact, slot in zip(facts, self.slots[index]):
            wrapped = self._wrap(fact)
            rows.extend(wrapped)
            rows.extend([] for _ in range(max(0, slot - len(wrapped))))
        return rows

    def window(self, top, page):
        top = max(0, min(max(0, self.count - max(0, page)), int(top)))
        end, rows = min(self.count, top + max(0, int(page))), []
        index = max(0, bisect_right(self.offsets, top) - 1)
        while index < len(self.offsets) - 1 and self.offsets[index] < end:
            card = self.render_card(index)
            measured = self.offsets[index + 1] - self.offsets[index]
            if len(card) > measured:
                # Corrupt/out-of-range scheduler values must remain readable.
                # Enlarge this one measured card instead of dropping text.
                growth = len(card) - measured
                for tail in range(index + 1, len(self.offsets)):
                    self.offsets[tail] += growth
                self.count += growth
                end = min(self.count, top + max(0, int(page)))
            start = max(0, top - self.offsets[index])
            stop = min(len(card), end - self.offsets[index])
            rows.extend(card[start:stop])
            index += 1
        return rows
