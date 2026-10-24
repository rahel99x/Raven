"""Private, rebuildable range index over verified trace journals.

The index is derived evidence, not a replacement integrity boundary. New index
owners replay registered sources from sequence one before exposing their rows;
no serialized Python reader or unverified seek checkpoint is trusted. Within an
owner, readers advance only through committed verified pages. SQLite and reader
access are serialized so a read-only browser may share the owned index.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import sqlite3
import stat
import threading
import time
from contextlib import contextmanager
from itertools import islice
from pathlib import Path

from .workspace_index_alignment import AlignmentQueries
from .workspace_index_filters import IndexFilters, FILTER_NAMES, validate_filters
from .workspace_index_statistics import IndexStatistics
from .workspace_index_storage import (
    INDEX_SCHEMA,
    MAX_SOURCES,
    SCHEMA,
    _existing_schema,
    _IntegerSum,
    _json,
    _limit,
    _private_path,
    _text,
    _time,
)

_PAGE_EVENTS = 128
_LOSS = {"gap", "lost", "drop", "loss", "dropped"}
_FRAME = {"call", "resume", "return", "yield", "unwind"}
_ACTIVITIES = {
    "gpu_kernel",
    "gpu_copy",
    "gpu_sync",
    "scheduling",
    "host",
    "cooperative",
}


class TraceIndex(IndexStatistics, AlignmentQueries, IndexFilters):
    """One owned writer and bounded serialized queries over local derived data."""

    def __init__(self, path, *, max_bytes=512 << 20):
        if type(max_bytes) is not int or not 1 << 20 <= max_bytes <= 8 << 30:
            raise ValueError("Trace index database budget must be 1 MiB–8 GiB")
        self.path, created = _private_path(path)
        lock_path, _ = _private_path(str(self.path) + ".lock")
        self._lock_fd = os.open(
            lock_path, os.O_RDWR | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            lock_info = os.fstat(self._lock_fd)
            if (
                not stat.S_ISREG(lock_info.st_mode)
                or lock_info.st_uid != os.getuid()
                or lock_info.st_mode & 0o077
                or lock_info.st_nlink != 1
            ):
                raise OSError("Unsafe trace index owner lock")
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(self._lock_fd)
            raise ValueError(
                "Trace index is in use or exclusive locking is unavailable"
            ) from exc
        try:
            oversized = self.path.stat().st_size > max_bytes
        except BaseException:
            fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
            os.close(self._lock_fd)
            raise
        if oversized:
            fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
            os.close(self._lock_fd)
            raise ValueError("Existing trace index exceeds its database budget")
        try:
            if self.path.stat().st_size:
                _existing_schema(self.path)
            else:
                created = True
        except BaseException:
            fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
            os.close(self._lock_fd)
            raise
        self.max_bytes = max_bytes
        self.lock = threading.RLock()
        self.readers = {}
        self._initialized = set()
        self._errors = {}
        self._invalid_sources = set()
        self._storage_error = None
        self.closed = False
        self._query_deadlines = []
        self._path_ids = {}
        self._discovery_errors = {}
        self._pending_source_paths = set()
        self._discovery_round = 0
        self._round = 0
        self._overview_pending = {}
        self._function_pending = {}
        self._overview_cache = {}
        self._query_round = 0
        try:
            self.db = sqlite3.connect(
                str(self.path), check_same_thread=False, timeout=1
            )
        except BaseException:
            fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
            os.close(self._lock_fd)
            raise
        try:
            self.db.execute("PRAGMA trusted_schema=OFF")
            self.db.execute("PRAGMA foreign_keys=ON")
            self.db.execute("PRAGMA journal_mode=DELETE")
            self.db.execute("PRAGMA temp_store=FILE")
            self.db.execute("PRAGMA cache_size=-2048")
            page_size = self.db.execute("PRAGMA page_size").fetchone()[0]
            self.db.execute(f"PRAGMA max_page_count={max_bytes // page_size}")
            self.db.create_aggregate("raven_sum", 1, _IntegerSum)
            if (
                not created
                and self.path.stat().st_size
                and self.db.execute(
                    "SELECT value FROM metadata WHERE key='schema'"
                ).fetchone()
                != (INDEX_SCHEMA,)
            ):
                raise ValueError(
                    "Unsupported trace workspace index; choose a new rebuildable cache"
                )
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS sources(id TEXT PRIMARY KEY,path TEXT UNIQUE NOT NULL,identity TEXT NOT NULL,clock TEXT NOT NULL,status TEXT NOT NULL,validated INTEGER NOT NULL,payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS clocks(id TEXT PRIMARY KEY,origin TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS function_stats(source TEXT NOT NULL,symbol TEXT NOT NULL,clock TEXT NOT NULL,name TEXT NOT NULL,search TEXT NOT NULL,kind TEXT NOT NULL,file TEXT NOT NULL,line INTEGER,count INTEGER NOT NULL,total TEXT NOT NULL,self_total TEXT NOT NULL,self_known INTEGER NOT NULL,min TEXT NOT NULL,max TEXT NOT NULL,scope TEXT NOT NULL,PRIMARY KEY(source,symbol));
                CREATE INDEX IF NOT EXISTS function_rank ON function_stats(count DESC,name,source);
                CREATE TABLE IF NOT EXISTS overview_counts(source TEXT NOT NULL,clock TEXT NOT NULL,level INTEGER NOT NULL,bucket TEXT NOT NULL,count INTEGER NOT NULL,PRIMARY KEY(source,clock,level,bucket));
                CREATE INDEX IF NOT EXISTS overview_domain ON overview_counts(clock,level,bucket,source);
                CREATE TABLE IF NOT EXISTS source_bounds(source TEXT NOT NULL,clock TEXT NOT NULL,start TEXT NOT NULL,end TEXT NOT NULL,PRIMARY KEY(source,clock));
                CREATE TABLE IF NOT EXISTS intervals(key INTEGER PRIMARY KEY,id TEXT UNIQUE NOT NULL,source TEXT NOT NULL,clock TEXT NOT NULL,thread TEXT NOT NULL,depth INTEGER NOT NULL,kind TEXT NOT NULL,name TEXT NOT NULL,search TEXT NOT NULL,start TEXT NOT NULL,end TEXT NOT NULL,duration TEXT NOT NULL,self TEXT,file TEXT NOT NULL,line INTEGER,first_seq INTEGER NOT NULL,last_seq INTEGER NOT NULL,symbol TEXT NOT NULL,symbol_scope TEXT NOT NULL,payload TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS interval_range ON intervals(clock,start,end);
                CREATE INDEX IF NOT EXISTS interval_source ON intervals(source,thread,start);
                CREATE TABLE IF NOT EXISTS markers(id TEXT PRIMARY KEY,source TEXT NOT NULL,clock TEXT NOT NULL,thread TEXT NOT NULL,kind TEXT NOT NULL,name TEXT NOT NULL,search TEXT NOT NULL,start TEXT NOT NULL,end TEXT NOT NULL,payload TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS marker_range ON markers(clock,start,end);
                CREATE TABLE IF NOT EXISTS frames(source TEXT NOT NULL,thread TEXT NOT NULL,depth INTEGER NOT NULL,event TEXT NOT NULL,children TEXT NOT NULL,PRIMARY KEY(source,thread,depth));
                CREATE TABLE IF NOT EXISTS spans(source TEXT NOT NULL,thread TEXT NOT NULL,span TEXT NOT NULL,depth INTEGER NOT NULL,event TEXT NOT NULL,PRIMARY KEY(source,thread,span));
            """)
            for table, column, default in (
                ("intervals", "symbol", "''"),
                ("intervals", "symbol_scope", "'{}'"),
                ("function_stats", "scope", "'{}'"),
            ):
                columns = {
                    row[1] for row in self.db.execute(f"PRAGMA table_info({table})")
                }
                if column not in columns:
                    self.db.execute(
                        f"ALTER TABLE {table} ADD COLUMN {column} TEXT NOT NULL DEFAULT {default}"
                    )
            self.db.execute(
                "INSERT OR REPLACE INTO metadata VALUES('schema',?)", (INDEX_SCHEMA,)
            )
            self._filters_initialize()
            self._alignment_initialize()
            self.db.execute("UPDATE sources SET validated=0,status='REVALIDATING'")
            try:
                self.db.execute(
                    "CREATE VIRTUAL TABLE IF NOT EXISTS range_index USING rtree(key,start,end)"
                )
                self.rtree = True
            except sqlite3.OperationalError:
                self.rtree = False
            self.db.commit()
        except BaseException:
            self.db.close()
            fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
            os.close(self._lock_fd)
            if created:
                self.path.unlink(missing_ok=True)
            raise

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self):
        with self.lock:
            if not self.closed:
                self.db.close()
                self.readers.clear()
                fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
                os.close(self._lock_fd)
                self.closed = True

    @contextmanager
    def _query_budget(self, seconds=1.0):
        if self.closed:
            raise ValueError("Trace workspace index is closed")
        self._query_deadlines.append(time.monotonic() + seconds)
        self.db.set_progress_handler(
            lambda: time.monotonic() >= min(self._query_deadlines), 1000
        )
        try:
            yield
        except sqlite3.OperationalError as exc:
            if "interrupt" in str(exc).lower():
                raise ValueError(
                    "Trace query work budget exceeded; narrow the range or sources"
                ) from exc
            raise
        finally:
            self._query_deadlines.pop()
            if self._query_deadlines:
                self.db.set_progress_handler(
                    lambda: time.monotonic() >= min(self._query_deadlines), 1000
                )
            else:
                self.db.set_progress_handler(None, 0)

    def _clear_source(self, source):
        if self.rtree:
            self.db.execute(
                "DELETE FROM range_index WHERE key IN (SELECT key FROM intervals WHERE source=?)",
                (source,),
            )
        for table in (
            "intervals",
            "markers",
            "frames",
            "spans",
            "source_bounds",
            "overview_counts",
            "function_stats",
        ):
            self.db.execute(f"DELETE FROM {table} WHERE source=?", (source,))
        self._clear_alignment(source)
        self._clear_filters(source)

    def _source(self, path, expected):
        from .protocol import TraceReader, read_json, validate_identity
        from .workspace_model import source_clock

        path = Path(path).absolute()
        if path.is_dir():
            path = path / "manifest.json"
        if len(str(path)) > 4096:
            raise ValueError("Trace source path exceeds its bound")
        if str(path) in self._path_ids:
            source = self._path_ids[str(path)]
            identity = json.loads(
                self.db.execute(
                    "SELECT identity FROM sources WHERE id=?", (source,)
                ).fetchone()[0]
            )
            if expected and any(
                identity.get(key) != value for key, value in expected.items()
            ):
                raise ValueError(
                    "Trace source does not match the expected exact run attempt"
                )
            return source
        manifest = read_json(path)
        if (
            not isinstance(manifest, dict)
            or manifest.get("schema") != "raven.trace/v1"
            or not isinstance(manifest.get("coverage"), dict)
        ):
            raise ValueError("Expected a bounded raven.trace/v1 manifest and coverage")
        identity = validate_identity(manifest.get("identity"))
        if expected:
            if set(expected) - {"job_id", "attempt", "cluster", "run_id"}:
                raise ValueError("Unsupported expected trace identity field")
            if any(identity.get(key) != value for key, value in expected.items()):
                raise ValueError(
                    "Trace source does not match the expected exact run attempt"
                )
        common = self.db.execute(
            "SELECT value FROM metadata WHERE key='identity'"
        ).fetchone()
        binding = {key: identity[key] for key in ("job_id", "attempt", "cluster")}
        if identity.get("run_id"):
            binding["run_id"] = identity["run_id"]
        if common and json.loads(common[0]) != binding:
            raise ValueError(
                "Trace sources from different job attempts or clusters cannot share a workspace"
            )
        source = hashlib.sha256(
            _json(dict(sorted(identity.items()))).encode()
        ).hexdigest()
        existing = self.db.execute(
            "SELECT path FROM sources WHERE id=?", (source,)
        ).fetchone()
        if existing and existing[0] != str(path):
            raise ValueError(
                "Duplicate trace stream identity points to different source paths"
            )
        if (
            not existing
            and self.db.execute("SELECT count(*) FROM sources").fetchone()[0]
            >= MAX_SOURCES
        ):
            raise ValueError("Trace workspace exceeds its 512-source catalog bound")
        if source not in self._initialized:
            clock = source_clock(identity, manifest.get("coverage", {}).get("clock"))
            self._clear_source(source)
            payload = dict(
                id=source,
                path=str(path),
                identity=identity,
                clock=clock,
                coverage=manifest.get("coverage", {}),
                status="INDEXING",
                warnings=[],
                read_seq=0,
                committed_seq=manifest.get("committed_seq", 0),
                sha256="",
                caught_up=False,
            )
            self.db.execute(
                "INSERT OR REPLACE INTO sources VALUES(?,?,?,?,?,?,?)",
                (
                    source,
                    str(path),
                    _json(identity),
                    _json(clock),
                    "INDEXING",
                    0,
                    _json(payload),
                ),
            )
            self.db.execute(
                "INSERT OR IGNORE INTO metadata VALUES('identity',?)", (_json(binding),)
            )
            self.readers[source] = TraceReader(
                path, retain=_PAGE_EVENTS, retain_bytes=8 << 20
            )
            self._initialized.add(source)
        self._path_ids[str(path)] = source
        return source

    def refresh(
        self, paths, *, expected=None, max_events=4096, max_seconds=0.05, cancel=None
    ):
        """Advance verified cursors fairly, with persistent pairing across pages.

        A source failing integrity is quarantined, while independent sources
        remain readable. I/O failures preserve its prior verified prefix and
        retry on the next ordinary call. Database quota errors are explicit.
        """
        if isinstance(paths, (str, bytes, Path)):
            paths = [paths]
        try:
            paths = list(islice(iter(paths), MAX_SOURCES + 1))
        except TypeError as exc:
            raise ValueError(
                "Choose a bounded collection of trace source paths"
            ) from exc
        if len(paths) > MAX_SOURCES:
            raise ValueError("Trace workspace exceeds its 512-source input bound")
        _limit(max_events, 1_000_000)
        if (
            type(max_seconds) not in (float, int)
            or not math.isfinite(max_seconds)
            or not 0.001 <= max_seconds <= 60
        ):
            raise ValueError("Trace refresh work slice must be 1 ms–60 s")
        until = time.monotonic() + max_seconds
        if expected is not None and (
            not isinstance(expected, dict)
            or set(expected) - {"job_id", "attempt", "cluster", "run_id"}
        ):
            raise ValueError("Unsupported expected trace identity fields")
        with self.lock:
            if self.closed:
                raise ValueError("Trace workspace index is closed")
            if self._storage_error is not None:
                raise ValueError(self._storage_error)
            requested_paths = {str(path) for path in paths}
            self._discovery_errors = {
                path: reason
                for path, reason in self._discovery_errors.items()
                if path in requested_paths
            }
            if paths:
                offset = self._discovery_round % len(paths)
                paths = paths[offset:] + paths[:offset]
            examined = 0
            ids = []
            for path in paths:
                if cancel is not None and cancel.is_set():
                    break
                if time.monotonic() >= until:
                    break
                examined += 1
                try:
                    source = self._source(path, expected)
                except OSError as exc:
                    self._discovery_errors[str(path)] = _text(str(exc), 2000)
                    continue
                self._discovery_errors.pop(str(path), None)
                if source not in ids:
                    ids.append(source)
            self._discovery_round += max(1, examined)
            self._pending_source_paths = (
                requested_paths - self._path_ids.keys() - self._discovery_errors.keys()
            )
            self.db.commit()
            if ids:
                position = self._round % len(ids)
                ids = ids[position:] + ids[:position]
                self._round += 1
            used = 0
            advanced = True
            while advanced and used < max_events and time.monotonic() < until:
                advanced = False
                for source in ids:
                    if (
                        used >= max_events
                        or time.monotonic() >= until
                        or cancel is not None
                        and cancel.is_set()
                    ):
                        break
                    if source in self._invalid_sources:
                        continue
                    reader = self.readers[source]
                    previous = reader.sequence
                    try:
                        snapshot = reader.refresh(
                            max_events=min(_PAGE_EVENTS, max_events - used),
                            max_bytes=256 << 10,
                            max_seconds=max(0.001, min(1.0, until - time.monotonic())),
                            cancel=cancel,
                        )
                    except OSError as exc:
                        self._errors[source] = _text(str(exc), 2000)
                        continue
                    except ValueError as exc:
                        self._quarantine(source, str(exc))
                        continue
                    if snapshot["status"] == "INVALID":
                        self._quarantine(source, "; ".join(snapshot["warnings"]))
                        continue
                    from .workspace_model import source_clock

                    try:
                        clock = source_clock(
                            snapshot["identity"], snapshot["covered"].get("clock")
                        )
                        original_clock = json.loads(
                            self.db.execute(
                                "SELECT clock FROM sources WHERE id=?", (source,)
                            ).fetchone()[0]
                        )
                        if clock != original_clock:
                            raise ValueError(
                                "Trace clock or calibration changed within a stream; use a new source generation"
                            )
                    except ValueError as exc:
                        self._quarantine(source, str(exc))
                        continue
                    try:
                        for event in snapshot["events"]:
                            if event["seq"] > previous:
                                self._event(source, snapshot, event)
                        payload = dict(
                            id=source,
                            path=str(reader.path),
                            identity=snapshot["identity"],
                            clock=clock,
                            coverage=snapshot["covered"],
                            status=snapshot["status"],
                            warnings=[
                                item
                                for item in snapshot["warnings"]
                                if not item.startswith("View retains at most")
                            ],
                            read_seq=snapshot["read_seq"],
                            committed_seq=snapshot["committed_seq"],
                            sha256=snapshot["sha256"],
                            caught_up=snapshot["caught_up"],
                        )
                        self._flush_statistics()
                        self.db.execute(
                            "UPDATE sources SET payload=?,status=?,clock=?,validated=1 WHERE id=?",
                            (_json(payload), payload["status"], _json(clock), source),
                        )
                        self.db.commit()
                    except ValueError as exc:
                        self.db.rollback()
                        self._overview_pending.clear()
                        self._function_pending.clear()
                        self._facet_pending.clear()
                        self._facet_truncated_pending.clear()
                        self._quarantine(
                            source, "Invalid indexed event semantics: " + str(exc)
                        )
                        continue
                    except sqlite3.DatabaseError as exc:
                        self.db.rollback()
                        self._overview_pending.clear()
                        self._function_pending.clear()
                        self._facet_pending.clear()
                        self._facet_truncated_pending.clear()
                        self._initialized.discard(source)
                        self.readers.pop(source, None)
                        self._path_ids = {
                            path: sid
                            for path, sid in self._path_ids.items()
                            if sid != source
                        }
                        self._storage_error = (
                            "Trace index database budget or storage failed; previous committed index data remains: "
                            + _text(str(exc), 500)
                            + "; reopen the index after correcting storage or increasing its budget"
                        )
                        raise ValueError(self._storage_error) from exc
                    self._errors.pop(source, None)
                    count = reader.sequence - previous
                    if count:
                        self._overview_cache.clear()
                    used += count
                    advanced |= count > 0 and not snapshot["caught_up"]
                    # Retained event copies are no longer needed after their page
                    # is committed. Integrity/code/stack state remains untouched.
                    reader.events.clear()
                    reader._event_sizes.clear()
                    reader.retained_bytes = 0
            return self.catalog()

    def _quarantine(self, source, reason):
        self._invalid_sources.add(source)
        row = self.db.execute(
            "SELECT payload FROM sources WHERE id=?", (source,)
        ).fetchone()
        payload = json.loads(row[0])
        payload.update(status="INVALID", caught_up=False)
        payload["warnings"] = ["Source quarantined: " + _text(reason, 2000)]
        self.db.execute(
            "UPDATE sources SET status='INVALID',validated=0,payload=? WHERE id=?",
            (_json(payload), source),
        )
        self.db.commit()
        self._errors[source] = payload["warnings"][0]

    def _base(self, source, snapshot, event, category="event"):
        clock = json.loads(
            self.db.execute(
                "SELECT clock FROM sources WHERE id=?", (source,)
            ).fetchone()[0]
        )
        data = event["data"]
        return dict(
            id=f"{source}:{category}:{event['seq']}",
            source_id=source,
            clock_id=clock["id"],
            thread=event["thread"],
            depth=0,
            kind=event["kind"],
            name=_text(data.get("function") or data.get("name"), default=event["kind"]),
            file=_text(data.get("file")),
            line=data.get("line") if type(data.get("line")) is int else None,
            start_ns=event["mono_ns"],
            end_ns=event["mono_ns"],
            duration_ns=0,
            self_ns=None,
            first_seq=event["seq"],
            last_seq=event["seq"],
            node=_text(data.get("source_node"), 256) or snapshot["identity"]["node"],
            data=data,
        )

    def _bounds(self, row):
        self.db.execute(
            "INSERT INTO source_bounds VALUES(?,?,?,?) ON CONFLICT(source,clock) DO UPDATE SET start=min(start,excluded.start),end=max(end,excluded.end)",
            (
                row["source_id"],
                row["clock_id"],
                _time(row["start_ns"]),
                _time(row["end_ns"]),
            ),
        )

    def _marker(self, row):
        self._bounds(row)
        self.db.execute(
            "INSERT OR REPLACE INTO markers(id,source,clock,thread,kind,name,search,start,end,payload) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                row["id"],
                row["source_id"],
                row["clock_id"],
                row["thread"],
                row["kind"],
                row["name"],
                row["name"].casefold(),
                _time(row["start_ns"]),
                _time(row["end_ns"]),
                _json(row),
            ),
        )
        self._store_filter_tags("markers", row)
        self._store_alignment(row, marker=True)

    def _interval(self, row):
        self._bounds(row)
        self._note_interval_statistics(row)
        data = row.get("data", {})
        lane_scope = [
            row["source_id"],
            row["clock_id"],
            row["kind"],
            row["thread"],
            row["depth"],
            row.get("node"),
            row.get("device"),
            row.get("context"),
            row.get("queue"),
            {
                key: data[key]
                for key in (
                    "source_pid",
                    "source_tid",
                    "thread_generation",
                    "source_process_start",
                    "source_process_uuid",
                    "process_generation",
                    "context_generation",
                    "queue_generation",
                )
                if key in data
            },
        ]
        row["lane_id"] = (
            "lane:" + hashlib.sha256(_json(lane_scope).encode()).hexdigest()
        )
        self.db.execute(
            "INSERT INTO intervals(id,source,clock,thread,depth,kind,name,search,start,end,duration,self,file,line,first_seq,last_seq,symbol,symbol_scope,payload) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                row["id"],
                row["source_id"],
                row["clock_id"],
                row["thread"],
                row["depth"],
                row["kind"],
                row["name"],
                row["name"].casefold(),
                _time(row["start_ns"]),
                _time(row["end_ns"]),
                _time(row["duration_ns"]),
                None if row.get("self_ns") is None else _time(row["self_ns"]),
                row.get("file", ""),
                row.get("line"),
                row["first_seq"],
                row["last_seq"],
                row["symbol_key"],
                _json(row["symbol_scope"]),
                _json(row),
            ),
        )
        if self.rtree:
            self.db.execute(
                "INSERT OR IGNORE INTO clocks VALUES(?,?)",
                (row["clock_id"], _time(row["start_ns"])),
            )
            origin = int(
                self.db.execute(
                    "SELECT origin FROM clocks WHERE id=?", (row["clock_id"],)
                ).fetchone()[0]
            )
            key = self.db.execute(
                "SELECT key FROM intervals WHERE id=?", (row["id"],)
            ).fetchone()[0]
            self.db.execute(
                "INSERT INTO range_index VALUES(?,?,?)",
                (key, float(row["start_ns"] - origin), float(row["end_ns"] - origin)),
            )
        self.db.execute("DELETE FROM markers WHERE id=?", (row["id"],))
        self._store_filter_tags("intervals", row)
        self._store_alignment(row)

    def _event(self, source, snapshot, event):
        kind, data, thread = event["kind"], event["data"], event["thread"]
        lifetime = {
            key: data[key]
            for key in (
                "thread_generation",
                "source_node",
                "source_pid",
                "source_process_start",
                "source_process_uuid",
                "process_generation",
            )
            if key in data and data[key] is not None
        }
        if any(
            field in data for field in ("logical_task_id", "task_id", "task_generation")
        ):
            from .workspace_model_topology import _logical_task

            task, generation = _logical_task(data)
            if task is not None:
                lifetime["logical_task_id"] = task
            if generation is not None:
                lifetime["task_generation"] = generation
        # Pairing keys preserve the user-facing thread label in the payload,
        # but separate explicitly declared producer lifetimes. The raw-thread
        # prefix also lets a checkpoint invalidate every generation safely
        # when the checkpoint itself lacks lifetime metadata.
        thread_prefix = "thread:" + hashlib.sha256(thread.encode()).hexdigest() + ":"
        thread = thread_prefix + hashlib.sha256(_json(lifetime).encode()).hexdigest()
        if "cpu_observation" in data:
            from .cpu_timeline import build

            observed = build({"events": [event], "covered": {}}, 0)
            for item in observed["observations"]:
                point = self._base(source, snapshot, event, "cpu")
                point.update(
                    kind="cpu_observation",
                    start_ns=item["start_ns"],
                    end_ns=item["end_ns"],
                    duration_ns=item["end_ns"] - item["start_ns"],
                    logical_cpu=item["logical_cpu"],
                    socket_id=item["socket_id"],
                    core_id=item["core_id"],
                    data=data["cpu_observation"],
                )
                self._marker(point)
        if kind in _LOSS:
            self.db.execute("DELETE FROM frames WHERE source=?", (source,))
            self.db.execute("DELETE FROM spans WHERE source=?", (source,))
        elif kind == "stack_checkpoint":
            if lifetime:
                self.db.execute(
                    "DELETE FROM frames WHERE source=? AND thread=?", (source, thread)
                )
            else:
                self.db.execute(
                    "DELETE FROM frames WHERE source=? AND thread GLOB ?",
                    (source, thread_prefix + "*"),
                )
        elif kind in {"thread_start", "thread_end"}:
            for table in ("frames", "spans"):
                self.db.execute(
                    f"DELETE FROM {table} WHERE source=? AND thread GLOB ?",
                    (source, thread_prefix + "*"),
                )
        if kind in _FRAME:
            frame = data.get("frame")
            if (
                not isinstance(frame, str)
                or not frame
                or kind in {"call", "resume"}
                and not isinstance(data.get("function"), str)
            ):
                self.db.execute(
                    "DELETE FROM frames WHERE source=? AND thread=?", (source, thread)
                )
            elif kind in {"call", "resume"}:
                top = self.db.execute(
                    "SELECT depth FROM frames WHERE source=? AND thread=? ORDER BY depth DESC LIMIT 1",
                    (source, thread),
                ).fetchone()
                row = self._base(source, snapshot, event, "python")
                row["depth"] = top[0] + 1 if top else 0
                if row["depth"] >= 4096:
                    raise ValueError(
                        "Trace index pairing exceeds 4096 active call levels; raw recording is unchanged"
                    )
                self.db.execute(
                    "INSERT INTO frames VALUES(?,?,?,?,?)",
                    (source, thread, row["depth"], _json(row), "0"),
                )
                row["kind"] = "unmatched_entry"
                self._marker(row)
                return
            else:
                top = self.db.execute(
                    "SELECT depth,event,children FROM frames WHERE source=? AND thread=? ORDER BY depth DESC LIMIT 1",
                    (source, thread),
                ).fetchone()
                if top:
                    row = json.loads(top[1])
                    self.db.execute(
                        "DELETE FROM frames WHERE source=? AND thread=? AND depth=?",
                        (source, thread, top[0]),
                    )
                    if (
                        row["data"].get("frame") == frame
                        and event["mono_ns"] >= row["start_ns"]
                    ):
                        row.update(
                            kind="python",
                            end_ns=event["mono_ns"],
                            duration_ns=event["mono_ns"] - row["start_ns"],
                            last_seq=event["seq"],
                            exit=kind,
                        )
                        row["self_ns"] = max(0, row["duration_ns"] - int(top[2]))
                        self._interval(row)
                        parent = self.db.execute(
                            "SELECT depth,children FROM frames WHERE source=? AND thread=? ORDER BY depth DESC LIMIT 1",
                            (source, thread),
                        ).fetchone()
                        if parent:
                            self.db.execute(
                                "UPDATE frames SET children=? WHERE source=? AND thread=? AND depth=?",
                                (
                                    str(int(parent[1]) + row["duration_ns"]),
                                    source,
                                    thread,
                                    parent[0],
                                ),
                            )
                        return
                    self.db.execute(
                        "DELETE FROM frames WHERE source=? AND thread=?",
                        (source, thread),
                    )
        elif (
            kind in {"span_start", "span_end"}
            and isinstance(data.get("span_id"), str)
            and data["span_id"]
        ):
            span = data["span_id"]
            if kind == "span_start":
                if (
                    self.db.execute(
                        "SELECT count(*) FROM spans WHERE source=?", (source,)
                    ).fetchone()[0]
                    >= 4096
                ):
                    raise ValueError(
                        "Trace index pairing exceeds 4096 active spans; raw recording is unchanged"
                    )
                occupied = {
                    row[0]
                    for row in self.db.execute(
                        "SELECT depth FROM spans WHERE source=? AND thread=?",
                        (source, thread),
                    )
                }
                depth = next(
                    index for index in range(len(occupied) + 1) if index not in occupied
                )
                row = self._base(source, snapshot, event, "cooperative")
                row.update(depth=depth, kind="unmatched_entry")
                self.db.execute(
                    "INSERT OR REPLACE INTO spans VALUES(?,?,?,?,?)",
                    (source, thread, span, depth, _json(row)),
                )
                self._marker(row)
                return
            opening = self.db.execute(
                "SELECT event FROM spans WHERE source=? AND thread=? AND span=?",
                (source, thread, span),
            ).fetchone()
            if opening:
                self.db.execute(
                    "DELETE FROM spans WHERE source=? AND thread=? AND span=?",
                    (source, thread, span),
                )
                row = json.loads(opening[0])
                if event["mono_ns"] >= row["start_ns"]:
                    row.update(
                        kind="cooperative",
                        end_ns=event["mono_ns"],
                        duration_ns=event["mono_ns"] - row["start_ns"],
                        last_seq=event["seq"],
                    )
                    self._interval(row)
                    return
        elif kind == "activity":
            activity = data.get("activity_kind")
            try:
                start, end = data["start_ns"], data["end_ns"]
                _time(start)
                _time(end)
                if (
                    activity not in _ACTIVITIES
                    or end < start
                    or data.get("duration_ns", end - start) != end - start
                ):
                    raise ValueError("Invalid activity duration")
            except (KeyError, ValueError):
                row = self._base(source, snapshot, event)
                row.update(
                    kind="invalid_activity",
                    name="Invalid activity boundaries; duration unknown",
                )
                self._marker(row)
                return
            row = self._base(source, snapshot, event, "activity")
            row.update(
                kind=activity,
                name=_text(data.get("name"), default="unnamed activity"),
                start_ns=start,
                end_ns=end,
                duration_ns=end - start,
            )
            if (
                data.get("boundary_fidelity") == "matched-producer-boundaries"
                and type(data.get("depth")) is int
                and 0 <= data["depth"] < 4096
            ):
                row["depth"] = data["depth"]
            for key in (
                "device",
                "context",
                "queue",
                "correlation_id",
                "source_pid",
                "source_tid",
            ):
                if key in data:
                    row[key] = data[key]
            self._interval(row)
            return
        self._marker(self._base(source, snapshot, event))

    def catalog(self):
        with self.lock, self._query_budget():
            sources = []
            warnings = []
            catalog_bytes = 0
            for source, valid, status, payload in self.db.execute(
                "SELECT id,validated,status,payload FROM sources ORDER BY id"
            ):
                catalog_bytes += len(payload)
                if catalog_bytes > 8 << 20:
                    raise ValueError(
                        "Trace catalog metadata exceeds its 8 MiB budget; narrow registered sources"
                    )
                row = json.loads(payload)
                row.update(validated=bool(valid), status=status)
                if source in self._errors:
                    row["error"] = self._errors[source]
                    row["read_status"] = (
                        "retry-later" if status != "INVALID" else "invalid"
                    )
                    row["caught_up"] = False
                    warnings.append("Source read error: " + row["error"])
                sources.append(row)
                warnings.extend(row.get("warnings", ()))
            bounds = []
            for clock, start, end in self.db.execute(
                "SELECT b.clock,min(b.start),max(b.end) FROM source_bounds b JOIN sources s ON s.id=b.source WHERE s.validated=1 AND s.status!='INVALID' GROUP BY b.clock"
            ):
                bounds.append(
                    dict(clock_id=clock, start_ns=int(start), end_ns=int(end))
                )
            warnings.extend(
                "Source unavailable: " + _text(path, 512) + ": " + reason
                for path, reason in self._discovery_errors.items()
            )
            warnings.extend(self._alignment_warnings.values())
            if self._storage_error:
                warnings.append(self._storage_error)
            identity = self.db.execute(
                "SELECT value FROM metadata WHERE key='identity'"
            ).fetchone()
            dimensions, dimensions_truncated = self._filter_dimensions()
            return dict(
                schema=SCHEMA,
                identity=json.loads(identity[0]) if identity else {},
                sources=sources,
                bounds=bounds,
                aligned_bounds=self._alignment_bounds(),
                dimensions=dimensions,
                dimensions_truncated=dimensions_truncated,
                catalog_metadata_byte_limit=8 << 20,
                warnings=list(dict.fromkeys(warnings))[:64],
                caught_up=bool(sources)
                and not self._discovery_errors
                and not self._pending_source_paths
                and self._storage_error is None
                and all(row.get("caught_up") and row["validated"] for row in sources),
                discovery=dict(
                    pending_count=len(self._pending_source_paths),
                    pending_paths=sorted(self._pending_source_paths)[:64],
                ),
                index=dict(
                    schema=INDEX_SCHEMA,
                    database_bytes=self.db.execute("PRAGMA page_count").fetchone()[0]
                    * self.db.execute("PRAGMA page_size").fetchone()[0],
                    max_bytes=self.max_bytes,
                    range_backend="rtree-exact-filter"
                    if self.rtree
                    else "bounded-sql-range",
                    revalidation="New owners replay source integrity before exposing indexed rows",
                    state="storage-blocked" if self._storage_error else "available",
                    error=self._storage_error,
                ),
            )

    def _where(
        self,
        alias,
        *,
        start_ns=None,
        end_ns=None,
        source_ids=None,
        thread=None,
        search="",
        clock_id=None,
        **filters,
    ):
        clauses = ["s.validated=1", "s.status!='INVALID'"]
        values = []
        if start_ns is not None:
            clauses.append(f"{alias}.end>=?")
            values.append(_time(start_ns))
        if end_ns is not None:
            clauses.append(f"{alias}.start<=?")
            values.append(_time(end_ns))
        if start_ns is not None and end_ns is not None and end_ns < start_ns:
            raise ValueError("Trace range ends before it starts")
        if source_ids is not None:
            if (
                not isinstance(source_ids, (tuple, list))
                or len(source_ids) > MAX_SOURCES
                or any(
                    not isinstance(source, str)
                    or len(source) > 256
                    or not source.isprintable()
                    for source in source_ids
                )
            ):
                raise ValueError("Choose a bounded list of source IDs")
            clauses.append(
                f"{alias}.source IN ({','.join('?' for _ in source_ids)})"
                if source_ids
                else "0"
            )
            values.extend(source_ids)
        if clock_id is not None:
            if not isinstance(clock_id, str) or len(clock_id) > 1024:
                raise ValueError("Invalid clock domain")
            clauses.append(f"{alias}.clock=?")
            values.append(clock_id)
        if thread is not None:
            if not isinstance(thread, str) or len(thread) > 512:
                raise ValueError("Invalid thread filter")
            clauses.append(f"({alias}.thread=? OR {alias}.tag_thread=?)")
            values.extend((thread, thread))
        if not isinstance(search, str) or len(search) > 256:
            raise ValueError("Search exceeds its 256-character bound")
        if search:
            clauses.append(f"instr({alias}.search,?)>0")
            values.append(search.casefold())
        facet_clauses, facet_values = self._filter_where(
            alias, filters, clock_id=clock_id
        )
        clauses.extend(facet_clauses)
        values.extend(facet_values)
        return " AND ".join(clauses), values

    def _payload_rows(self, cursor, limit, byte_budget):
        rows, used = [], 0
        for (payload,) in cursor:
            if len(rows) >= limit or used + len(payload) > byte_budget:
                return rows, True, used
            if self._query_deadlines and time.monotonic() >= min(self._query_deadlines):
                raise ValueError(
                    "Trace query work budget exceeded; narrow the range or sources"
                )
            rows.append(json.loads(payload))
            used += len(payload)
        return rows, False, used

    def query(
        self,
        *,
        start_ns=None,
        end_ns=None,
        source_ids=None,
        thread=None,
        search="",
        limit=2000,
        clock_id=None,
        **filters,
    ):
        return self._query(
            start_ns=start_ns,
            end_ns=end_ns,
            source_ids=source_ids,
            thread=thread,
            search=search,
            limit=limit,
            clock_id=clock_id,
            **filters,
        )

    def query_many(self, requests):
        """Query explicit independent clock ranges with one catalog/work budget.

        The count budget is at most 2,000 rows per collection (intervals and
        markers), and the combined serialized row payload is capped at 8 MiB.
        Unqueried ranges are explicit; partial batches rotate their first range
        on the next request rather than starving a later node indefinitely.
        """
        allowed = {
            "start_ns",
            "end_ns",
            "source_ids",
            "thread",
            "search",
            "limit",
            "clock_id",
        } | FILTER_NAMES
        if not isinstance(requests, (list, tuple)) or len(requests) > MAX_SOURCES:
            raise ValueError("Choose at most 512 explicit clock-range requests")
        total = 0
        common_filters = None
        for request in requests:
            if (
                not isinstance(request, dict)
                or set(request) - allowed
                or not isinstance(request.get("clock_id"), str)
            ):
                raise ValueError(
                    "Batch requests need an explicit clock domain and supported query fields"
                )
            total += _limit(request.get("limit", 2000), 2000)
            selected_filters = self._filter_scope(
                validate_filters(
                    **{
                        key: request[key]
                        for key in FILTER_NAMES
                        if request.get(key) is not None
                    }
                ),
                source_ids=request.get("source_ids"),
                thread=request.get("thread"),
                search=request.get("search", ""),
            )
            if common_filters is None:
                common_filters = selected_filters
            elif common_filters != selected_filters:
                common_filters = {
                    key: value
                    for key, value in common_filters.items()
                    if key != "per_range"
                    and key in selected_filters
                    and selected_filters[key] == value
                }
                common_filters["per_range"] = True
        if total > 2000:
            raise ValueError("Trace batch query exceeds its total 2,000-row budget")
        with self.lock, self._query_budget():
            catalog = self.catalog()
            intervals, markers, lanes = {}, {}, {}
            ranges, pending = [], []
            payload_bytes, clipped = 0, False
            if requests:
                offset = self._query_round % len(requests)
                ordered = list(requests[offset:]) + list(requests[:offset])
            else:
                ordered = []
            for position, request in enumerate(ordered):
                if (
                    time.monotonic() >= min(self._query_deadlines)
                    or payload_bytes >= 8 << 20
                ):
                    pending.extend(ordered[position:])
                    break
                budget = max(
                    1, ((8 << 20) - payload_bytes) // max(1, len(ordered) - position)
                )
                try:
                    part = self._query(**request, catalog=catalog, byte_budget=budget)
                except ValueError as exc:
                    if "work budget exceeded" not in str(exc):
                        raise
                    pending.extend(ordered[position:])
                    break
                for row in part["intervals"]:
                    intervals[(row["clock_id"], row["id"])] = row
                for row in part["markers"]:
                    markers[(row["clock_id"], row["id"])] = row
                lanes.update((row["id"], row) for row in part["lanes"])
                ranges.append(dict(part["range"], filters=part["filters"]))
                payload_bytes += part["payload_bytes"]
                clipped |= part["truncated"]
            if pending:
                self._query_round += max(1, len(ranges))
            else:
                self._query_round = 0
            result = dict(catalog)
            warnings = list(catalog["warnings"])
            if pending:
                warnings.append(
                    f"Trace batch work budget left {len(pending)} clock ranges unqueried; missing lanes are not idle"
                )
            result.update(
                intervals=list(intervals.values()),
                markers=list(markers.values()),
                lanes=list(lanes.values()),
                ranges=ranges,
                unqueried_ranges=[dict(request) for request in pending],
                range=dict(clock_id=None, start_ns=None, end_ns=None),
                separate_clocks=True,
                truncated=clipped or bool(pending),
                payload_bytes=payload_bytes,
                byte_limit=8 << 20,
                warnings=warnings,
                filters=common_filters or {},
            )
            return result

    def _query(
        self,
        *,
        start_ns=None,
        end_ns=None,
        source_ids=None,
        thread=None,
        search="",
        limit=2000,
        clock_id=None,
        catalog=None,
        byte_budget=8 << 20,
        **filters,
    ):
        _limit(limit)
        filters = validate_filters(**filters)
        with self.lock, self._query_budget():
            where, values = self._where(
                "i",
                start_ns=start_ns,
                end_ns=end_ns,
                source_ids=source_ids,
                thread=thread,
                search=search,
                clock_id=clock_id,
                **filters,
            )
            join = ""
            interval_table = self._alignment_view("intervals", clock_id)
            if (
                self.rtree
                and interval_table == "intervals"
                and clock_id is not None
                and (start_ns is not None or end_ns is not None)
            ):
                origin = self.db.execute(
                    "SELECT origin FROM clocks WHERE id=?", (clock_id,)
                ).fetchone()
                if origin:
                    join = " JOIN range_index r ON r.key=i.key"
                    if start_ns is not None:
                        where += " AND r.end>=?"
                        values.append(float(start_ns - int(origin[0])))
                    if end_ns is not None:
                        where += " AND r.start<=?"
                        values.append(float(end_ns - int(origin[0])))
            cursor = self.db.execute(
                f"SELECT i.payload FROM {interval_table} i JOIN sources s ON s.id=i.source{join} WHERE {where} ORDER BY i.clock,i.start,i.id LIMIT ?",
                (*values, limit + 1),
            )
            intervals, clipped_intervals, used = self._payload_rows(
                cursor, limit, byte_budget * 3 // 4
            )
            where, values = self._where(
                "m",
                start_ns=start_ns,
                end_ns=end_ns,
                source_ids=source_ids,
                thread=thread,
                search=search,
                clock_id=clock_id,
                **filters,
            )
            cursor = self.db.execute(
                f"SELECT m.payload FROM {self._alignment_view('markers', clock_id)} m JOIN sources s ON s.id=m.source WHERE {where} ORDER BY m.clock,m.start,m.id LIMIT ?",
                (*values, limit + 1),
            )
            markers, clipped_markers, marker_bytes = self._payload_rows(
                cursor, limit, byte_budget - used
            )
            lanes = {}
            for row in intervals:
                key = row["lane_id"]
                lanes.setdefault(
                    key,
                    dict(
                        id=key,
                        lane_id=key,
                        source_id=row["source_id"],
                        clock_id=row["clock_id"],
                        thread=row["thread"],
                        kind=row["kind"],
                        depth=row["depth"],
                        label=f"{row['node']} / thread {row['thread']} / {row['kind']} {row['depth']}",
                        node=row["node"],
                    ),
                )
            result = dict(catalog) if catalog is not None else self.catalog()
            result.update(
                intervals=intervals,
                markers=markers,
                lanes=list(lanes.values()),
                range=dict(start_ns=start_ns, end_ns=end_ns, clock_id=clock_id),
                truncated=clipped_intervals or clipped_markers,
                payload_bytes=used + marker_bytes,
                byte_limit=byte_budget,
                filters=self._filter_scope(
                    filters, source_ids=source_ids, thread=thread, search=search
                ),
                filter_prerequisites=[
                    "Selected-CPU scheduling intervals and overlapping same-actor calls are retained to reconstruct CPU stacks; these calls are intersected with observed execution, not charged in full to that CPU"
                ]
                if filters.get("logical_cpu") is not None
                else [],
            )
            return result

    def detail(self, interval_id):
        if not isinstance(interval_id, str) or len(interval_id) > 1024:
            raise ValueError("Invalid interval ID")
        with self.lock, self._query_budget():
            for table in ("intervals", "markers"):
                row = self.db.execute(
                    f"SELECT i.payload FROM {table} i JOIN sources s ON s.id=i.source WHERE i.id=? AND s.validated=1 AND s.status!='INVALID'",
                    (interval_id,),
                ).fetchone()
                if row:
                    return json.loads(row[0])
            return None
