"""Rebuildable project evidence index; retained export bundles remain authoritative.

One transaction admits a complete, checksum-verified immutable export. Queries
use a separate read-only connection, an authorizer and finite execution budgets.
No SQL expression is substituted for Raven's existing expression evaluator.
"""
from __future__ import annotations

from contextlib import contextmanager
from collections import deque
from decimal import Decimal, InvalidOperation
from functools import lru_cache
import csv
import hashlib
import io
import json
import math
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time

from . import data_export as D
from .state_ownership import StateOwner

SCHEMA = "raven.evidence-index/v2"
DATABASE = "evidence.sqlite3"
MAX_SOURCE = 8 << 20
MAX_TOTAL = 64 << 20
MAX_ROWS = 200_000
MAX_SQL = 8192
MAX_INDEX = 64 << 20
IDENTITY = ("project", "host", "run_id", "attempt", "cluster", "job_id", "submission_token", "submit_time", "script_sha256")
DDL = """
CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE captures (digest TEXT PRIMARY KEY, export_id TEXT NOT NULL, manifest_path TEXT NOT NULL,
  manifest_json TEXT NOT NULL, committed_at REAL NOT NULL);
CREATE TABLE sources (id INTEGER PRIMARY KEY, capture TEXT NOT NULL REFERENCES captures(digest),
  path TEXT NOT NULL, sha256 TEXT NOT NULL, bytes INTEGER NOT NULL, family TEXT NOT NULL,
  UNIQUE(capture,path));
CREATE TABLE attempts (capture TEXT NOT NULL REFERENCES captures(digest), attempt_key TEXT NOT NULL,
  identity_json TEXT NOT NULL, state TEXT NOT NULL, snapshot_json TEXT NOT NULL, PRIMARY KEY(capture,attempt_key));
CREATE TABLE log_lines (source_id INTEGER NOT NULL REFERENCES sources(id), line INTEGER NOT NULL,
  attempt_key TEXT NOT NULL, byte_offset INTEGER NOT NULL, text TEXT NOT NULL, PRIMARY KEY(source_id,line));
CREATE TABLE transitions (source_id INTEGER NOT NULL REFERENCES sources(id), source_row INTEGER NOT NULL,
  attempt_key TEXT NOT NULL, kind TEXT NOT NULL, timestamp_text TEXT, previous_state TEXT,
  state TEXT NOT NULL, evidence_json TEXT NOT NULL, PRIMARY KEY(source_id,source_row));
CREATE TABLE measurements (source_id INTEGER NOT NULL REFERENCES sources(id), source_row INTEGER NOT NULL,
  attempt_key TEXT NOT NULL, identity_json TEXT NOT NULL, family TEXT NOT NULL, metric TEXT NOT NULL,
  timestamp REAL, value REAL, value_text TEXT NOT NULL, numeric_status TEXT NOT NULL,
  unit TEXT NOT NULL, scope TEXT NOT NULL, quality TEXT NOT NULL,
  status TEXT NOT NULL, reason TEXT NOT NULL, attributes_json TEXT NOT NULL,
  PRIMARY KEY(source_id,source_row,metric));
CREATE INDEX measurement_window ON measurements(attempt_key,metric,timestamp);
CREATE INDEX measurement_family ON measurements(family,metric,timestamp);
CREATE INDEX measurement_order ON measurements(family,attempt_key,metric,timestamp,source_row);
CREATE INDEX log_attempt ON log_lines(attempt_key,source_id,line);
CREATE INDEX transition_attempt ON transitions(attempt_key,source_id,source_row);
"""
VIEWS = {
    "sources": "SELECT sources.*, captures.export_id FROM sources JOIN captures ON capture=captures.digest ORDER BY id",
    "application": "SELECT * FROM measurements WHERE family='application' ORDER BY attempt_key,metric,timestamp,source_row",
    "cpu-memory": "SELECT * FROM measurements WHERE family='cpu-memory' ORDER BY attempt_key,metric,timestamp,source_row",
    "gpu": "SELECT * FROM measurements WHERE family='gpu' ORDER BY attempt_key,metric,timestamp,source_row",
    "attempts": "SELECT * FROM attempts ORDER BY capture,attempt_key",
    "logs": "SELECT * FROM log_lines ORDER BY attempt_key,source_id,line",
    "transitions": "SELECT * FROM transitions ORDER BY attempt_key,source_id,source_row",
}


def _cancel(cancel):
    if cancel is not None and cancel.is_set():
        raise ValueError("Evidence operation cancelled; the last committed index remains usable")


def _canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"), allow_nan=False)


def _identity(item):
    result = {}
    for key in IDENTITY:
        value = item["identity"].get(key)
        if value is not None and (type(value) not in (str, int) or isinstance(value, str) and len(value) > 4096):
            raise ValueError("Attempt identity fields must be bounded strings or exact integers")
        result[key] = "" if value is None else str(value)
    if not result["run_id"] or not result["attempt"] or result["project"] != item["project"]:
        raise ValueError("Attempt inventory needs a matching project and exact run-attempt identity")
    return _canonical(result)


def _read(path, limit):
    from .source_budget import source_limit, source_read
    limit = source_limit(limit)
    fd = D._open(str(Path(path).absolute()))
    try:
        before = os.fstat(fd)
        if before.st_size > limit:
            raise ValueError(f"Evidence source exceeds {limit} bytes: {path}")
        source_read(before.st_size)
        data = bytearray()
        while len(data) <= limit:
            chunk = os.read(fd, min(65536, limit + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
        if len(data) > limit or D._signature(before) != D._signature(os.fstat(fd)):
            raise ValueError("Evidence changed or exceeded its byte bound during reading")
        return bytes(data)
    finally:
        os.close(fd)


def inspect_bundle(bundle, project):
    """Inspect publication metadata; ingestion also verifies every selected file."""
    bundle = str(Path(bundle).absolute())
    raw = _read(Path(bundle) / "manifest.json", 8 << 20)
    manifest = D._decode(raw)
    marker = D._decode(_read(Path(bundle) / "COMPLETE.json", 1 << 20))
    digest = hashlib.sha256(raw).hexdigest()
    if (not isinstance(manifest, dict) or manifest.get("schema") != "raven.export-manifest/v1"
            or not isinstance(marker, dict) or marker.get("schema") != "raven.export-complete/v1"
            or marker.get("manifest_sha256") != digest or marker.get("export_id") != manifest.get("export_id")):
        raise ValueError("Use a completed export with a matching manifest checksum")
    attempts = manifest.get("attempts")
    if not isinstance(attempts, list) or not 1 <= len(attempts) <= 256:
        raise ValueError("Export has no bounded attempt inventory")
    if any(not isinstance(item, dict) or not isinstance(item.get("identity"), dict)
           or not isinstance(item.get("project"), str) for item in attempts):
        raise ValueError("Export attempt entries require project and identity objects")
    allowed = {str(item.get("project", "")) for item in attempts if isinstance(item, dict)}
    if not isinstance(project, str) or not project or project not in allowed:
        raise ValueError("Select the exact project path from the export's attempt inventory")
    if allowed != {project}:
        raise ValueError("A project-scoped index requires an export containing only that project; export its attempts separately")
    files = manifest.get("files")
    if not isinstance(files, list) or len(files) > 8192:
        raise ValueError("Export file inventory exceeds its bound")
    selected, seen, total = [], set(), 0
    for entry in files:
        if not isinstance(entry, dict):
            raise ValueError("Invalid export file inventory")
        family = entry.get("family")
        if family not in {*D.COLUMNS, "summary", "logs"}:
            continue
        path, size, sha = entry.get("path"), entry.get("bytes"), entry.get("sha256")
        if (not isinstance(path, str) or not path or type(size) is not int or not 0 <= size <= MAX_SOURCE
                or not isinstance(sha, str) or len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha)):
            raise ValueError("Expected bounded evidence paths, sizes and SHA-256 provenance")
        full = D._under(bundle, path)
        if ((family in D.COLUMNS and not path.endswith(".csv")) or path in seen or type(size) is not int or not 0 <= size <= MAX_SOURCE
                or not isinstance(sha, str) or len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha)):
            raise ValueError("Expected unique bounded CSV evidence with SHA-256 provenance")
        total += size
        if total > MAX_TOTAL:
            raise ValueError("Capture exceeds the 64 MiB ingestion budget; export a smaller selection")
        seen.add(path)
        selected.append({**entry, "absolute_path": full})
    identities = {_identity(item)
                  for item in attempts if item.get("project") == project}
    prefixes = {}
    for item in attempts:
        prefix = "" if len(attempts) == 1 else "attempts/" + D._attempt_name(item) + "/"
        identity = _identity(item)
        if prefix in prefixes:
            raise ValueError("Export has ambiguous attempt source prefixes")
        prefixes[prefix] = {"project": item["project"], "identity": identity, "key": hashlib.sha256(identity.encode()).hexdigest(), "snapshot": item}
    return {"bundle": bundle, "project": project, "digest": digest, "manifest": manifest, "identities": identities,
            "manifest_json": raw.decode("utf-8"), "files": selected, "source_bytes": total, "attempt_prefixes": prefixes}


def _number(value, label):
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("Invalid numeric " + label) from exc
    if not math.isfinite(number):
        raise ValueError("Nonfinite " + label)
    return number


def _measurement_number(value, label):
    """Keep exact integral decimal values out of binary64 when they would round.

    Decimal compares scientific notation without expanding its exponent into a
    potentially enormous Python integer. Ordinary fractions keep the declared
    binary64 semantics; overflow and underflow remain available as original text.
    """
    if value in (None, ""):
        return None, "unavailable"
    try:
        exact = Decimal(value)
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError("Invalid numeric " + label) from exc
    if not exact.is_finite():
        raise ValueError("Nonfinite " + label)
    numeric = float(exact)
    if exact == exact.to_integral_value():
        if not math.isfinite(numeric) or Decimal.from_float(numeric) != exact:
            return None, "integer not exactly representable; use value_text"
    if not math.isfinite(numeric) or numeric == 0 and exact != 0:
        return None, "outside binary64 range; use value_text"
    return numeric, "binary64"


def _rows(raw, family, project, identities):
    reader = csv.DictReader(io.StringIO(raw.decode("utf-8"), newline=""))
    fields = reader.fieldnames or []
    if len(fields) != len(set(fields)) or not set(IDENTITY) <= set(fields) or len(fields) > 128:
        raise ValueError("Measurement CSV needs unique columns and complete attempt identity fields")
    for line, row in enumerate(reader, 2):
        if line > MAX_ROWS + 1 or None in row or any(value is None or len(value) > 65536 for value in row.values()):
            raise ValueError("Measurement CSV exceeds its row/field bounds or has ragged rows")
        if row["project"] != project:
            raise ValueError("Measurement project differs from the published project inventory")
        if not row["run_id"] or not row["attempt"]:
            raise ValueError("Measurement row has no exact run-attempt identity")
        identity = {key: row[key] for key in IDENTITY}
        identity_json = _canonical(identity)
        if identity_json not in identities:
            raise ValueError("Measurement identity differs from the published attempt inventory")
        attempt = hashlib.sha256(identity_json.encode()).hexdigest()
        metrics = [(row.get("metric"), row.get("value"), row.get("unit") or "unspecified")] if family == "application" else [
            (key, row[key], D.UNITS[key]) for key in D.OBSERVATIONS[family] if key in row]
        for metric, value, unit in metrics:
            if not metric or len(metric) > 512:
                raise ValueError("Measurement needs a bounded metric name")
            numeric, numeric_status = _measurement_number(value, metric)
            yield (line, attempt, identity_json, family, metric, _number(row.get("timestamp"), "timestamp"),
                   numeric, value or "", numeric_status, unit, row.get("scope", ""), row.get("quality", ""), row.get("status", ""),
                   row.get("reason", ""), _canonical(row))


def _database(index):
    return Path(index).absolute() / DATABASE


def _limits(connection):
    if hasattr(connection, "setlimit"):
        for name, maximum in (("LENGTH", 1 << 20), ("SQL_LENGTH", MAX_SQL), ("COLUMN", 64),
                              ("EXPR_DEPTH", 32), ("COMPOUND_SELECT", 16), ("VARIABLE_NUMBER", 64),
                              ("ATTACHED", 0), ("VDBE_OP", 10000)):
            connection.setlimit(getattr(sqlite3, "SQLITE_LIMIT_" + name), maximum)
    if hasattr(connection, "enable_load_extension"):
        connection.enable_load_extension(False)
    connection.execute("PRAGMA trusted_schema=OFF")


def _catalog(connection):
    return connection.execute("SELECT type,name,tbl_name,CASE WHEN length(sql)<=8192 THEN sql ELSE NULL END FROM sqlite_master ORDER BY type,name LIMIT 65").fetchall()


@lru_cache(maxsize=2)
def _expected_catalog(legacy=False):
    connection = sqlite3.connect(":memory:")
    try:
        connection.executescript(DDL.replace('CREATE INDEX measurement_order ON measurements(family,attempt_key,metric,timestamp,source_row);','') if legacy else DDL)
        return _catalog(connection)
    finally:
        connection.close()


def _verify_writer(connection):
    deadline = time.monotonic() + 1
    connection.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
    try:
        if _catalog(connection) != _expected_catalog():
            raise ValueError("Existing index has an unrecognized schema, view or trigger; rebuild in a new directory")
    finally:
        connection.set_progress_handler(None, 0)


def _source_attempt(capture, source):
    candidates = [value for prefix, value in capture["attempt_prefixes"].items() if source["path"].startswith(prefix)]
    if len(candidates) != 1:
        raise ValueError("Export source is not bound to exactly one published attempt")
    return candidates[0]


def _index_auxiliary(connection, source_id, source, raw, attempt, cancel):
    if source["family"] == "logs":
        offset = 0
        for line, data in enumerate(raw.splitlines(keepends=True), 1):
            if line > MAX_ROWS or len(data) > 65536:
                raise ValueError("Log index exceeds line/record budgets; export a narrower log selection")
            if line % 128 == 0:
                _cancel(cancel)
            text = data.decode("utf-8").rstrip("\r\n")
            connection.execute("INSERT INTO log_lines VALUES (?,?,?,?,?)", (source_id, line, attempt["key"], offset, text))
            offset += len(data)
        return
    document = D._decode(raw)
    if not isinstance(document, dict):
        raise ValueError("Retained summary must be a JSON object")
    # A final state is a snapshot, not proof of a transition timestamp. When a
    # producer supplies explicit transition events, preserve those separately.
    events = document.get("transitions", [])
    if not isinstance(events, list) or len(events) > 20000:
        raise ValueError("Retained transitions exceed their count bound")
    if document.get("state"):
        if not isinstance(document["state"], str) or len(document["state"]) > 256:
            raise ValueError("Retained state must be a bounded string")
        connection.execute("INSERT INTO transitions VALUES (?,?,?,?,?,?,?,?)",
            (source_id, 0, attempt["key"], "state-snapshot", None, None, str(document["state"]), _canonical(document)))
    for row, event in enumerate(events, 1):
        if (not isinstance(event, dict) or not isinstance(event.get("state"), str) or len(event["state"]) > 256
                or type(event.get("timestamp")) not in (str, int, float)
                or event.get("previous_state") is not None and not isinstance(event["previous_state"], str)):
            raise ValueError("Transition requires an explicit state and source timestamp")
        if isinstance(event["timestamp"], (int, float)):
            _number(event["timestamp"], "transition timestamp")
        connection.execute("INSERT INTO transitions VALUES (?,?,?,?,?,?,?,?)",
            (source_id, row, attempt["key"], "producer-declared-transition", str(event["timestamp"]),
             event.get("previous_state"), event["state"], _canonical(event)))


@contextmanager
def _writer(index):
    index = Path(index).absolute()
    owner = StateOwner(str(index), strict=True)
    connection = None
    try:
        path = _database(index)
        if path.is_symlink() or path.exists() and not stat.S_ISREG(path.stat().st_mode):
            raise ValueError("Index must be a regular local database")
        if path.exists():
            size = path.stat().st_size
            if size > MAX_INDEX:
                raise ValueError("Index exceeds the 64 MiB update budget")
            from .source_budget import source_read
            source_read(size)
        connection = sqlite3.connect(path, timeout=1)
        _limits(connection)
        path.chmod(0o600)
        connection.execute("PRAGMA journal_mode=DELETE")  # no WAL sidecar/NFS assumptions
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA synchronous=FULL")
        yield connection
    except (sqlite3.Error, sqlite3.Warning) as exc:
        raise ValueError("Evidence index update rejected: " + str(exc)) from exc
    finally:
        if connection is not None:
            connection.close()
        owner.close()


def ingest(index, bundle, project, *, cancel=None, expected_digest=None):
    capture = inspect_bundle(bundle, project)
    if expected_digest is not None and capture["digest"] != expected_digest:
        raise ValueError("Reviewed export manifest changed; inspect it again")
    with _writer(index) as connection:
        page_size = connection.execute("PRAGMA page_size").fetchone()[0]
        connection.execute("PRAGMA max_page_count=" + str(MAX_INDEX // page_size))
        tables = connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        if not tables:
            connection.executescript("BEGIN IMMEDIATE;" + DDL)
            with connection:
                connection.executemany("INSERT INTO metadata VALUES (?,?)", [("schema", SCHEMA), ("project", project)])
        elif _catalog(connection)==_expected_catalog(legacy=True):
            old=dict(connection.execute('SELECT key,value FROM metadata'))
            if old=={'schema':'raven.evidence-index/v1','project':project}:
                with connection:
                    connection.execute('CREATE INDEX measurement_order ON measurements(family,attempt_key,metric,timestamp,source_row)')
                    connection.execute("UPDATE metadata SET value=? WHERE key='schema'",(SCHEMA,))
        _verify_writer(connection)
        metadata = dict(connection.execute("SELECT key,value FROM metadata WHERE length(key)<=64 AND length(value)<=8192 LIMIT 3"))
        if metadata != {"schema": SCHEMA, "project": project}:
            raise ValueError("Index belongs to another project or schema; build a new index directory")
        if connection.execute("SELECT 1 FROM captures WHERE digest=?", (capture["digest"],)).fetchone():
            return {"status": "already-indexed", "capture": capture["digest"], "inserted": 0}
        if connection.execute("SELECT count(*) FROM captures").fetchone()[0] >= 256:
            raise ValueError("Index capture budget exhausted; rebuild a smaller selection in a new directory")
        count = 0
        with connection:
            connection.execute("INSERT INTO captures VALUES (?,?,?,?,?)", (capture["digest"], capture["manifest"]["export_id"],
                               str(Path(capture["bundle"]) / "manifest.json"), capture["manifest_json"], time.time()))
            for attempt in capture["attempt_prefixes"].values():
                if attempt["project"] == project:
                    connection.execute("INSERT INTO attempts VALUES (?,?,?,?,?)", (capture["digest"], attempt["key"],
                        attempt["identity"], str(attempt["snapshot"].get("state", "UNKNOWN")), _canonical(attempt["snapshot"])))
            for source in capture["files"]:
                _cancel(cancel)
                raw = _read(source["absolute_path"], MAX_SOURCE)
                if len(raw) != source["bytes"] or hashlib.sha256(raw).hexdigest() != source["sha256"]:
                    raise ValueError("Retained source differs from its immutable export checksum: " + source["path"])
                source_id = connection.execute("INSERT INTO sources(capture,path,sha256,bytes,family) VALUES (?,?,?,?,?)",
                    (capture["digest"], source["path"], source["sha256"], source["bytes"], source["family"])).lastrowid
                attempt = _source_attempt(capture, source)
                if source["family"] not in D.COLUMNS:
                    if attempt["project"] == project:
                        _index_auxiliary(connection, source_id, source, raw, attempt, cancel)
                    continue
                for row in _rows(raw, source["family"], project, {attempt["identity"]}):
                    count += 1
                    if count > MAX_ROWS:
                        raise ValueError("Capture exceeds the 200000 measurement ingestion budget")
                    if count % 128 == 0:
                        _cancel(cancel)
                    connection.execute("INSERT INTO measurements VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (source_id, *row))
            _cancel(cancel)
            if inspect_bundle(bundle, project)["digest"] != capture["digest"]:
                raise ValueError("Export publication changed while indexing")
        return {"status": "indexed", "capture": capture["digest"], "inserted": count,
                "source_bytes": capture["source_bytes"], "database_bytes": _database(index).stat().st_size}


_FUNCTIONS = frozenset({"abs", "avg", "coalesce", "count", "ifnull", "lag", "lead", "length", "max", "min", "nullif", "round", "sum", "total", "typeof"})


def _authorize(action, first, second, database, trigger):
    allowed = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_RECURSIVE}
    if action == sqlite3.SQLITE_FUNCTION:
        return sqlite3.SQLITE_OK if str(second).lower() in _FUNCTIONS else sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK if action in allowed else sqlite3.SQLITE_DENY


@contextmanager
def reader(index, *, expected_project=None):
    if expected_project is not None and (not isinstance(expected_project, str) or not 0 < len(expected_project) <= 4096):
        raise ValueError("Expected project must be an explicit bounded path")
    path = _database(index)
    # Verify all path components without following symlinks before SQLite opens
    # a read-only URI. Mutable local state belongs to this user's trusted scope.
    fd = D._open(str(path))
    try:
        size = os.fstat(fd).st_size
        if size > MAX_INDEX:
            raise ValueError("Index exceeds the 64 MiB query budget")
        from .source_budget import source_read
        source_read(size)
    finally:
        os.close(fd)
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=.25)
    try:
        _limits(connection)
        connection.execute("PRAGMA query_only=ON")
        connection.set_authorizer(_authorize)
        deadline = time.monotonic() + 1
        connection.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
        # Metadata is a writer-owned ordinary table. Do not evaluate a view or
        # hostile expression before the caller's query budgets are installed.
        schema = connection.execute("SELECT type,sql FROM sqlite_master WHERE name='metadata'").fetchall()
        if schema != [("table", "CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")]:
            raise ValueError("Unsupported evidence metadata table; rebuild from retained exports")
        metadata = dict(connection.execute("SELECT key,value FROM metadata WHERE length(key)<=64 AND length(value)<=8192 LIMIT 3"))
        if set(metadata) != {"schema", "project"} or metadata["schema"] not in {SCHEMA,'raven.evidence-index/v1'}:
            raise ValueError("Unsupported evidence index schema; rebuild from retained exports")
        if expected_project is not None and metadata["project"] != expected_project:
            raise ValueError("Evidence index belongs to a different authorized project")
        yield connection
    except (sqlite3.Error, sqlite3.Warning) as exc:
        raise ValueError("Evidence database rejected: " + str(exc)) from exc
    finally:
        connection.close()


def query(index, sql=None, **options):
    """Every untrusted query has private temporary storage and OS limits."""
    return _process_query(index, sql, options)


def _process_query(index, sql, options):
    cancel = options.pop("cancel", None)
    if cancel is not None and cancel.is_set():
        return {"schema": "raven.query-result/v1", "status": "partial", "reason": "cancelled", "columns": [], "rows": [], "bytes": 0}
    payload = _canonical({"index": str(index), "sql": sql, "options": options}).encode()
    if len(payload) > 16384:
        raise ValueError("Query process request exceeds 16 KiB")
    # Account for the source on the admitted parent; the child inherits no
    # execution authority and only opens this index read-only.
    from .source_budget import source_read
    source_read(_database(index).stat().st_size)
    temporary=tempfile.TemporaryDirectory(prefix='raven-sql-')
    environment=dict(os.environ,SQLITE_TMPDIR=temporary.name)
    try:
        process = subprocess.Popen([sys.executable, "-m", "raven.evidence_index"], stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,env=environment,start_new_session=True)
    except BaseException:
        temporary.cleanup()
        raise
    deadline = time.monotonic() + 6
    try:
        while True:
            if cancel is not None and cancel.is_set():
                return {"schema": "raven.query-result/v1", "status": "partial", "reason": "cancelled", "columns": [], "rows": [], "bytes": 0}
            try:
                output, _ = process.communicate(input=payload, timeout=.05)
                # A reply may arrive inside this poll while cancellation was
                # requested. Observe it before publishing the child's result.
                if cancel is not None and cancel.is_set():
                    return {"schema": "raven.query-result/v1", "status": "partial", "reason": "cancelled", "columns": [], "rows": [], "bytes": 0}
                break
            except subprocess.TimeoutExpired:
                payload = None
                if time.monotonic() >= deadline:
                    raise ValueError("Isolated SQL process exceeded its six-second startup/query budget")
        if process.returncode or len(output) > (5 << 20):
            raise ValueError("Isolated SQL process exhausted its CPU/memory or output budget")
        result = json.loads(output)
        if cancel is not None and cancel.is_set():
            return {"schema": "raven.query-result/v1", "status": "partial", "reason": "cancelled", "columns": [], "rows": [], "bytes": 0}
        if "error" in result:
            raise ValueError(result["error"])
        return result
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()
        temporary.cleanup()


def _query(index, sql=None, *, view="sources", parameters=(), max_rows=1000, max_bytes=1 << 20,
          timeout=1., vm_steps=200_000, cancel=None, expected_project=None):
    sql = sql or VIEWS.get(view)
    if not isinstance(sql, str) or not sql or len(sql) > MAX_SQL:
        raise ValueError("Choose a named view or one SQL SELECT of at most 8192 characters")
    if (type(max_rows) is not int or not 1 <= max_rows <= 10000 or type(max_bytes) is not int or not 1 <= max_bytes <= 4 << 20
            or not 0 < timeout <= 5 or type(vm_steps) is not int or not 1000 <= vm_steps <= 2_000_000):
        raise ValueError("Query budgets exceed allowed row, byte, time or VM-step limits")
    started, ticks, rows, reason = time.monotonic(), 0, [], ""
    def check_budget():
        nonlocal reason
        if cancel is not None and cancel.is_set(): reason = "cancelled"
        elif time.monotonic() - started >= timeout: reason = "time budget exhausted"
        elif ticks >= vm_steps: reason = "VM-step budget exhausted"
        return int(bool(reason))
    def progress():
        nonlocal ticks, reason
        ticks += 100
        return check_budget()
    columns, used = [], 0
    with reader(index, expected_project=expected_project) as connection:
        connection.set_progress_handler(progress, 100)
        try:
            if check_budget():
                return {"schema": "raven.query-result/v1", "status": "partial", "reason": reason, "columns": [], "rows": [], "bytes": 0}
            cursor = connection.execute(sql, parameters)
            columns = [item[0] for item in cursor.description or ()]
            if len(columns) > 64 or any(len(name) > 256 for name in columns):
                raise ValueError("Query column metadata exceeds its bound")
            used = len(_canonical(columns).encode())
            if used > max_bytes:
                reason = "column metadata exceeds byte budget"
                return {"schema": "raven.query-result/v1", "status": "partial", "reason": reason, "columns": [], "rows": [], "bytes": 0}
            for row in cursor:
                if check_budget(): break
                if len(rows) == max_rows:
                    reason = "row budget exhausted"; break
                if any(isinstance(value, bytes) or isinstance(value, float) and not math.isfinite(value) for value in row):
                    raise ValueError("Query produced unsupported binary or nonfinite values")
                values=list(row)
                encoded=json.dumps(values,ensure_ascii=False,allow_nan=False,separators=(',',':')).encode()
                size=len(encoded)
                if used + size > max_bytes:
                    if not rows and max_bytes-used>=256:
                        full_sha=hashlib.sha256(encoded).hexdigest()
                        remaining=max_bytes-used-192
                        clipped=[]
                        for value in values:
                            raw=json.dumps(value,ensure_ascii=False,allow_nan=False).encode()
                            allowance=max(0,remaining//max(1,len(values)-len(clipped)))
                            if len(raw)>allowance:
                                text=value if isinstance(value,str) else json.dumps(value,ensure_ascii=False,allow_nan=False)
                                value=text.encode()[:max(0,allowance-8)].decode('utf-8','ignore')+'…'
                            clipped.append(value)
                            remaining-=len(json.dumps(value,ensure_ascii=False,allow_nan=False).encode())
                        clipped_size=len(json.dumps(clipped,ensure_ascii=False,allow_nan=False,separators=(',',':')).encode())
                        if used+clipped_size<=max_bytes:
                            rows.append(clipped);used+=clipped_size
                            reason='oversized row truncated'
                            oversized={'row':0,'original_bytes':size,'sha256':full_sha}
                            break
                    reason = "byte budget exhausted"; break
                rows.append(list(row)); used += size
        except (sqlite3.Error, sqlite3.Warning, MemoryError) as exc:
            if not reason:
                raise ValueError("Read-only evidence query rejected: " + str(exc)) from exc
    return {"schema": "raven.query-result/v1", "status": "partial" if reason else "complete", "reason": reason,
            "columns": columns, "rows": rows, "bytes": used, "vm_steps_upper_bound": ticks + 100,
            "oversized_rows":[oversized] if 'oversized' in locals() else [],
            "seconds": time.monotonic() - started, "provenance": "source_id/source_row join to immutable capture and source digests"}


def rates(rows, *, max_gap, unit):
    """Counter deltas per second; no interpolation or counter-reset guesswork."""
    if not math.isfinite(max_gap) or max_gap <= 0 or len(rows) > 10000:
        raise ValueError("Rate needs a positive maximum gap and at most 10000 ordered samples")
    result, previous = [], None
    for stamp, value in rows:
        stamp, value = _number(stamp, "timestamp"), _number(value, "counter")
        reason, rate = "first sample", None
        if stamp is None or value is None:
            reason = "missing observation"
        elif previous is not None:
            elapsed, delta = stamp - previous[0], value - previous[1]
            if not math.isfinite(elapsed) or not math.isfinite(delta): reason = "counter delta exceeds finite numeric range"
            elif elapsed <= 0: reason = "non-increasing timestamp"
            elif elapsed > max_gap: reason = "gap exceeds maximum"
            elif delta < 0: reason = "counter reset"
            else:
                rate = delta / elapsed
                if not math.isfinite(rate): rate, reason = None, "counter delta exceeds finite numeric range"
                else: reason = ""
        result.append({"timestamp": stamp, "value": rate, "unit": unit + "/s", "reason": reason})
        previous = (stamp, value) if stamp is not None and value is not None else None
    return result


STREAM_FIELDS = ("clock", "scope", "rank", "node", "provider", "device_uuid", "device_index", "device_bdf", "partition", "source")


def series(index, *, source_id, attempt, metric, selector=None, cancel=None):
    """Select one capture/source/attempt and explicit stream; never mix exports."""
    if type(source_id) is not int or source_id <= 0 or not attempt or not metric:
        raise ValueError("Select a positive source ID, exact attempt key and metric")
    selector = {} if selector is None else selector
    if (not isinstance(selector, dict) or not set(selector) <= set(STREAM_FIELDS)
            or any(not isinstance(value, str) or len(value) > 4096 for value in selector.values())):
        raise ValueError("Stream selector must map supported dimension names to exact strings")
    result = query(index, "SELECT timestamp,value,unit,attributes_json,source_row FROM measurements WHERE source_id=? AND attempt_key=? AND metric=? ORDER BY timestamp,source_row",
                   parameters=(source_id, attempt, metric), max_rows=10000, max_bytes=4 << 20, cancel=cancel)
    if result["status"] != "complete":
        raise ValueError("Series selection is partial: " + result["reason"] + "; choose a smaller source")
    selected, dimensions, units = [], set(), set()
    for stamp, value, unit, attributes, source_row in result["rows"]:
        attrs = json.loads(attributes)
        if not all(attrs.get(key, "") == value for key, value in selector.items()):
            continue
        dimensions.add(tuple(attrs.get(key, "") for key in STREAM_FIELDS))
        units.add(unit)
        selected.append((stamp, value, source_row))
    if len(dimensions) != 1:
        raise ValueError("Select exactly one stream using rank/node/device/source dimensions; matching streams: " + str(len(dimensions)))
    if len(units) != 1 or units & {"", "unspecified", "unknown"}:
        raise ValueError("Series requires one known unit; no units are inferred")
    return {"samples": selected, "unit": units.pop(), "dimensions": dict(zip(STREAM_FIELDS, next(iter(dimensions)))),
            "source_id": source_id, "attempt_key": attempt, "metric": metric,
            "provenance": "One source in one immutable capture; repeated exports are never combined"}


def transform(selected, *, operation, max_gap, window_seconds=None):
    """Linear bounded transforms with explicit reset, gap and missing semantics."""
    if operation not in {"rate", "delta", "window"}:
        raise ValueError("Choose rate, delta or window")
    samples, unit = selected["samples"], selected["unit"]
    raw = [(stamp, value) for stamp, value, _ in samples]
    output = rates(raw, max_gap=max_gap, unit=unit)
    if operation == "delta":
        for index, row in enumerate(output):
            row["unit"] = unit
            if row["value"] is not None:
                row["value"] = samples[index][1] - samples[index - 1][1]
    if operation == "window":
        if window_seconds is None or not math.isfinite(window_seconds) or not 0 < window_seconds <= 86400:
            raise ValueError("Window needs an explicit positive duration of at most one day")
        pending, total, correction, previous = deque(), 0., 0., None
        updates = 0
        def add(value):
            nonlocal total, correction, updates
            combined = total + value
            correction += ((total - combined) + value) if abs(total) >= abs(value) else ((value - combined) + total)
            total = combined
            updates += 1
        for index, (stamp, value) in enumerate(raw):
            stamp, value = _number(stamp, "timestamp"), _number(value, "value")
            reason = ""
            if stamp is None or value is None:
                pending.clear(); total = correction = 0.; previous = None
                reason = "missing observation"
            else:
                if previous is not None and (stamp <= previous or stamp - previous > max_gap):
                    pending.clear(); total = correction = 0.
                    reason = "non-increasing timestamp" if stamp <= previous else "gap exceeds maximum"
                while pending and pending[0][0] <= stamp - window_seconds:
                    add(-pending.popleft()[1])
                pending.append((stamp, value)); add(value); previous = stamp
            try:
                # Compensated additions/removals preserve small observations
                # after a large spike leaves. Periodic bounded rebasing limits
                # accumulated roundoff while retaining linear rolling work.
                if pending and (updates >= 256 or not math.isfinite(total + correction)):
                    total = math.fsum(item[1] for item in pending); correction = 0.; updates = 0
                average = (total + correction) / len(pending) if pending and not reason else None
            except OverflowError:
                average,reason=None,'window exceeds finite numeric range'
            if average is not None and not math.isfinite(average):
                average, reason = None, "window exceeds finite numeric range"
                pending.clear(); total = correction = 0.; previous = None
            output[index] = {"timestamp": stamp, "value": average, "unit": unit, "reason": reason,
                             "count": len(pending), "window_seconds": window_seconds}
    for row, sample in zip(output, samples):
        row["source_row"] = sample[2]
    return {**selected, "samples": output, "operation": operation,
            "semantics": "No interpolation; counter reset abstention for rates/deltas; windows average observed samples in (t-window,t] with gap barriers"}


def export_snapshot(index, destination, *, cancel=None):
    """SQLite's backup API supplies a consistent snapshot, including live readers."""
    from . import science_files as SF
    destination = SF.destination(destination)
    started = time.monotonic()
    def progress(status, remaining, total):
        _cancel(cancel)
        if total * 4096 > MAX_INDEX or time.monotonic() - started > 5:
            raise ValueError("SQLite backup exceeds its byte/time budget")
    with tempfile.TemporaryDirectory(prefix="raven-index-export-") as temporary:
        target = Path(temporary) / DATABASE
        with reader(index) as source:
            with sqlite3.connect(target) as backup:
                source.backup(backup, pages=128, progress=progress, sleep=.01)
        record, _ = SF.snapshot(str(target), cancel=cancel, limit=MAX_INDEX)
        return SF.publish(destination, copies=[(record, DATABASE)], cancel=cancel,
                          receipt={"schema": "raven.sqlite-export/v1", "sha256": record["sha256"],
                                   "source_index": str(_database(index)), "fidelity": "Exact transactional SQLite backup; original evidence references retained"})


def _apply_query_limits():
    import resource

    # Own process limits are the 3.10 compatibility boundary. No global SQLite
    # heap limit is changed in the dashboard or another connection. The SQL
    # subprocess also inherits its admitted task's limits, which it cannot
    # raise (the MCP task, for example, already has a five-second hard CPU cap).
    for kind, requested in (
        (resource.RLIMIT_AS, (256 << 20, 256 << 20)),
        (resource.RLIMIT_CPU, (5, 6)),
        (resource.RLIMIT_FSIZE, (16 << 20, 16 << 20)),
    ):
        inherited = resource.getrlimit(kind)
        soft, hard = (
            wanted if ceiling == resource.RLIM_INFINITY else min(wanted, ceiling)
            for wanted, ceiling in zip(requested, inherited)
        )
        resource.setrlimit(kind, (min(soft, hard), hard))


def _child():
    try:
        _apply_query_limits()
        raw = sys.stdin.buffer.read(16385)
        if len(raw) > 16384:
            raise ValueError("Query process input exceeds its bound")
        request = json.loads(raw)
        result = _query(request["index"], request["sql"], **request["options"])
    except (OSError, ValueError, TypeError, KeyError, MemoryError, sqlite3.DatabaseError) as exc:
        result = {"error": str(exc) or "Query process exhausted its memory budget"}
    print(json.dumps(result,ensure_ascii=False,allow_nan=False,separators=(',',':')))


if __name__ == "__main__":
    _child()
