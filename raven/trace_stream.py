"""Bounded-memory, disk-backed traversal of a complete verified trace prefix.

The reader's existing page/identity/integrity checks remain authoritative.
Temporary rows let exporters order matched boundaries without retaining a
whole journal in Python or inventing closes at a page boundary.
"""

import json
import sqlite3
import tempfile
import time
from contextlib import contextmanager

from . import trace_protocol as P

MAX_SPOOL_BYTES = 256 << 20
MAX_OUTPUT_BYTES = 32 << 20


class TraceSpool:
    def __init__(self, path, *, managed=False):
        self.folder = tempfile.TemporaryDirectory(prefix="raven-trace-read-")
        self.db = sqlite3.connect(self.folder.name + "/events.sqlite")
        self.db.execute("PRAGMA journal_mode=OFF")
        self.db.execute("PRAGMA synchronous=OFF")
        self.db.execute("PRAGMA temp_store=FILE")
        self.db.execute("PRAGMA cache_size=-2048")
        self.db.executescript("""
            CREATE TABLE events(seq INTEGER PRIMARY KEY, mono INTEGER NOT NULL, thread TEXT NOT NULL, kind TEXT NOT NULL, data TEXT NOT NULL);
            CREATE TABLE frames(thread TEXT NOT NULL, depth INTEGER NOT NULL, seq INTEGER NOT NULL, frame TEXT NOT NULL, PRIMARY KEY(thread,depth));
            CREATE TABLE spans(thread TEXT NOT NULL, span TEXT NOT NULL, seq INTEGER NOT NULL, PRIMARY KEY(thread,span));
            CREATE TABLE pairs(opening INTEGER PRIMARY KEY, closing INTEGER UNIQUE NOT NULL, category TEXT NOT NULL);
        """)
        self.snapshot = None
        self.first = None
        self.last = None
        depths = {}
        sequence = 0
        until = time.monotonic() + 60
        try:
            # Compact frame references expand to at most three 8 KiB strings.
            # A 256-event page therefore fits the reader's 8 MiB retained cap
            # even for maximum-size code labels; no page can evict itself.
            reader = P.TraceReader(path, retain=256)
            for snapshot in reader.iter_pages(max_events=256, max_bytes=256 << 10):
                if time.monotonic() >= until:
                    raise ValueError(
                        "Trace traversal exceeded its 60 second work budget; retry on the recording host"
                    )
                if snapshot["status"] == "INVALID":
                    raise ValueError(
                        "Cannot export an invalid trace; inspect and recover its verified journal first"
                    )
                for event in snapshot["events"]:
                    if event["seq"] <= sequence:
                        continue
                    if event["seq"] != sequence + 1:
                        raise ValueError(
                            "Trace reader evicted unprocessed events; complete traversal refused"
                        )
                    sequence = event["seq"]
                    self.first = self.first or event
                    self.last = event
                    kind, data, thread = event["kind"], event["data"], event["thread"]
                    self.db.execute(
                        "INSERT INTO events VALUES(?,?,?,?,?)",
                        (
                            sequence,
                            event["mono_ns"],
                            thread,
                            kind,
                            json.dumps(data, separators=(",", ":")),
                        ),
                    )
                    if kind in {"gap", "lost", "drop", "loss", "dropped"}:
                        self.db.execute("DELETE FROM frames")
                        self.db.execute("DELETE FROM spans")
                        depths.clear()
                    elif kind == "stack_checkpoint":
                        self.db.execute("DELETE FROM frames WHERE thread=?", (thread,))
                        depths[thread] = 0
                    elif kind in {"call", "resume", "return", "yield", "unwind"}:
                        frame = data.get("frame")
                        depth = depths.get(thread, 0)
                        if (
                            not isinstance(frame, str)
                            or not frame
                            or (
                                kind in {"call", "resume"}
                                and not isinstance(data.get("function"), str)
                            )
                        ):
                            self.db.execute("DELETE FROM frames WHERE thread=?", (thread,))
                            depths[thread] = 0
                        elif kind in {"call", "resume"}:
                            self.db.execute(
                                "INSERT OR REPLACE INTO frames VALUES(?,?,?,?)",
                                (thread, depth, sequence, frame),
                            )
                            depths[thread] = depth + 1
                        elif depth:
                            opening = self.db.execute(
                                "SELECT seq,frame FROM frames WHERE thread=? AND depth=?",
                                (thread, depth - 1),
                            ).fetchone()
                            if opening is None or opening[1] != frame:
                                self.db.execute("DELETE FROM frames WHERE thread=?", (thread,))
                                depths[thread] = 0
                            else:
                                self.db.execute(
                                    "INSERT INTO pairs VALUES(?,?,?)",
                                    (opening[0], sequence, "python"),
                                )
                                self.db.execute(
                                    "DELETE FROM frames WHERE thread=? AND depth=?",
                                    (thread, depth - 1),
                                )
                                depths[thread] = depth - 1
                    elif kind in {"span_start", "span_end"} and isinstance(
                        data.get("span_id"), str
                    ):
                        span = data["span_id"]
                        if kind == "span_start":
                            self.db.execute(
                                "INSERT OR REPLACE INTO spans VALUES(?,?,?)",
                                (thread, span, sequence),
                            )
                        else:
                            opening = self.db.execute(
                                "SELECT seq FROM spans WHERE thread=? AND span=?", (thread, span)
                            ).fetchone()
                            if opening:
                                self.db.execute(
                                    "INSERT INTO pairs VALUES(?,?,?)",
                                    (opening[0], sequence, "cooperative"),
                                )
                                self.db.execute(
                                    "DELETE FROM spans WHERE thread=? AND span=?", (thread, span)
                                )
                self.snapshot = snapshot
                if len(depths) > 4096:
                    raise ValueError("Trace traversal exceeds 4096 active thread identities")
                size = (
                    self.db.execute("PRAGMA page_count").fetchone()[0]
                    * self.db.execute("PRAGMA page_size").fetchone()[0]
                )
                if size > MAX_SPOOL_BYTES:
                    raise ValueError("Trace traversal exceeds its 256 MiB temporary-storage budget")
            if self.first is None:
                raise ValueError("No verified events are available")
            self.db.execute("CREATE INDEX event_time ON events(mono,seq)")
            self.db.commit()
            self.snapshot = dict(
                self.snapshot,
                warnings=[
                    warning
                    for warning in self.snapshot["warnings"]
                    if not warning.startswith("View retains at most ")
                ],
            )
            frame_count = self.db.execute(
                "SELECT count(*) FROM events WHERE kind IN ('call','resume','return','yield','unwind')"
            ).fetchone()[0]
            matched = self.db.execute(
                "SELECT count(*) FROM pairs WHERE category='python'"
            ).fetchone()[0]
            self.omitted = frame_count - 2 * matched
            span_count = self.db.execute(
                "SELECT count(*) FROM events WHERE kind IN ('span_start','span_end')"
            ).fetchone()[0]
            span_matched = self.db.execute(
                "SELECT count(*) FROM pairs WHERE category='cooperative'"
            ).fetchone()[0]
            self.omitted_spans = span_count - 2 * span_matched
        except BaseException:
            self.close()
            raise

    def close(self):
        self.db.close()
        self.folder.cleanup()

    def events(self):
        identity = self.snapshot["identity"]
        for seq, stamp, thread, kind, data in self.db.execute(
            "SELECT seq,mono,thread,kind,data FROM events ORDER BY seq"
        ):
            yield dict(
                seq=seq,
                mono_ns=stamp,
                thread=thread,
                kind=kind,
                data=json.loads(data),
                pid=identity["pid"],
            )

    def pairs(self):
        query = """SELECT a.seq,a.mono,a.thread,a.kind,a.data,b.seq,b.mono,b.kind,p.category
                   FROM pairs p JOIN events a ON a.seq=p.opening JOIN events b ON b.seq=p.closing ORDER BY a.seq"""
        for first, start, thread, kind, data, last, end, exit_kind, category in self.db.execute(
            query
        ):
            yield (
                dict(seq=first, mono_ns=start, thread=thread, kind=kind, data=json.loads(data)),
                dict(seq=last, mono_ns=end, thread=thread, kind=exit_kind),
                category,
            )

    def provenance(self):
        from .trace_export import _provenance

        value = dict(self.snapshot, evicted=0)
        result = _provenance(value, self.omitted)
        result["limitations"][0] = (
            "All events in the verified committed prefix were traversed; unknown boundaries remain unknown."
        )
        result["first_seq"] = self.first["seq"]
        result["last_seq"] = self.last["seq"]
        return result


@contextmanager
def traverse(path, *, managed=False):
    value = TraceSpool(path, managed=managed)
    try:
        yield value
    finally:
        value.close()


def write_json_items(stream, items, *, maximum=MAX_OUTPUT_BYTES):
    total = 0
    for index, item in enumerate(items):
        raw = ("," if index else "") + json.dumps(
            item, ensure_ascii=True, allow_nan=False, separators=(",", ":")
        )
        total += len(raw.encode())
        if total > maximum:
            raise ValueError(
                "Trace output exceeds its 32 MiB artifact budget; no partial output was published"
            )
        stream.write(raw)
    return total
