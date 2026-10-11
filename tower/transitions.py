"""Short, monotonic terminal feedback for observed queue/accounting transitions."""
from __future__ import annotations

from collections import OrderedDict
import time


def paint_completion_trails(rows, hits, moves, *, main, width, body_top,
                            comfortable=False, ascii_=False):
    """Paint departures in Main's reserved left margin, never over controls.

    The fold button and mark occupy the first two compact table cells. The
    following gap is safe for feedback; comfortable layouts have an additional
    outer padding cell. Read only this short prefix and at most nine trail cells.
    The pristine rows and hit map remain usable for copying and pointer input.
    """
    if main is None or not moves or width <= 0 or main.width <= 0 or main.height <= 0:
        return rows
    from . import layout as L
    from .pane_drag import _replace

    padding = int(comfortable and main.width >= 8 and main.height >= 7)
    x = main.x if padding else main.x + 2
    top = max(body_top, main.y + 1 + padding)
    bottom = min(len(rows), main.y + main.height - padding)
    if not max(0, main.x) <= x < min(width, main.x + main.width) or top >= bottom:
        return rows
    destinations, blocked = {}, set()
    for y, kind, value in hits:
        if not top <= y < bottom:
            continue
        if kind == "recent":
            destinations[value] = y
        elif kind == "sort_header":
            # Header feedback must never cover a label or resize grip.
            blocked.add(y)
        elif kind == "control" and isinstance(value, dict):
            left, right = value.get("left"), value.get("right")
            if isinstance(left, int) and isinstance(right, int) and left <= x < right:
                blocked.add(y)
    output = rows
    for item in moves[-3:]:
        destination = destinations.get(item["job"])
        if destination is None:
            continue
        source = item.get("source")
        source = max(top, min(destination, source if source is not None else top))
        progress = 1 - (1 - item["progress"]) ** 3
        position = round(source + (destination - source) * progress)
        for trail in range(3):
            y = position - trail
            if not top <= y < bottom or y in blocked:
                continue
            prefix = L.row_text(L.clip_row(rows[y], x + 1))
            # Skip occupied cells, short rows and any half of a wide glyph.
            if L.vlen(prefix) != x + 1 or not prefix.endswith(" "):
                continue
            if output is rows:
                output = list(rows)
            glyph = ("v" if ascii_ else "◆") if trail == 0 else (":" if ascii_ else "│")
            output[y] = _replace(output[y], x, glyph, "cyan+bold" if trail == 0 else "dim",
                                 width, fitted=True)
    return output


class CompletionFeedback:
    MOVE_SECONDS = 1.6
    FLASH_SECONDS = 1.4

    def __init__(self, snapshot=None):
        snapshot = snapshot or {}
        self.sequence = max((item.get("seq", 0) for item in snapshot.get("job_transitions", [])), default=0)
        self.revision = snapshot.get("history_revision", 0)
        self.moves = OrderedDict()
        self.sources = OrderedDict()
        self.flash_started = None
        self.new_history = 0

    def update(self, snapshot, now=None):
        now = time.monotonic() if now is None else now
        for item in snapshot.get("job_transitions", []):
            seq = item.get("seq", 0)
            if seq <= self.sequence:
                continue
            self.sequence = seq
            jid = item.get("job")
            if not jid:
                continue
            if item.get("kind") == "departed":
                # Delayed frames never replay an old animation after an overlay/pager.
                observed = item.get("mono", now)
                if 0 <= now - observed < self.MOVE_SECONDS:
                    self.moves[jid] = dict(item, started=observed, source=self.sources.get(jid))
                    while len(self.moves) > 8:
                        self.moves.popitem(last=False)
            elif item.get("kind") == "returned":
                self.moves.pop(jid, None)
        revision = snapshot.get("history_revision", self.revision)
        if revision > self.revision:
            self.new_history += revision - self.revision
            self.flash_started = now
        self.revision = revision
        self.moves = OrderedDict((jid, item) for jid, item in self.moves.items()
                                 if 0 <= now - item["started"] < self.MOVE_SECONDS)

    def remember(self, hits):
        for y, kind, jid in hits:
            if kind == "job":
                self.sources[jid] = y
                self.sources.move_to_end(jid)
        while len(self.sources) > 256:
            self.sources.popitem(last=False)

    def moving(self, now=None):
        now = time.monotonic() if now is None else now
        return [dict(item, progress=max(0.0, min(1.0, (now - item["started"]) / self.MOVE_SECONDS)))
                for item in self.moves.values() if 0 <= now - item["started"] < self.MOVE_SECONDS]

    def flash_on(self, now=None):
        if self.flash_started is None:
            return False
        age = (time.monotonic() if now is None else now) - self.flash_started
        return 0 <= age < .35 or .7 <= age < 1.05

    @property
    def active(self):
        now = time.monotonic()
        return bool(self.moving(now)) or (self.flash_started is not None and 0 <= now - self.flash_started < self.FLASH_SECONDS)

    def acknowledge(self):
        self.new_history = 0
