"""Typed, bounded trace facets and predicates applied before result limits."""

from __future__ import annotations

import hashlib
import json

from .workspace_index_storage import _json

TAG_COLUMNS = {
    "tag_node": "TEXT",
    "tag_process": "TEXT",
    "tag_thread": "TEXT",
    "tag_cpu": "INTEGER",
    "tag_device_key": "TEXT",
    "tag_device_uuid": "TEXT",
    "tag_device_id": "TEXT",
    "tag_device_ordinal": "TEXT",
    "tag_task": "TEXT",
    "tag_rank": "TEXT",
    "tag_actor": "TEXT",
}
FILTER_NAMES = {
    "node",
    "process",
    "logical_cpu",
    "device_id",
    "kind",
    "kinds",
    "task",
    "rank",
}


def _identifier(value, field):
    if type(value) is int and 0 <= value < 1 << 64:
        return str(value)
    if isinstance(value, str) and 0 < len(value) <= 512 and value.isprintable():
        return value
    raise ValueError(
        "Trace "
        + field
        + " filter must be bounded printable text or a nonnegative integer"
    )


def validate_filters(**values):
    if set(values) - FILTER_NAMES:
        raise ValueError("Unsupported trace facet filter")
    result = {}
    for field, value in values.items():
        if value is None:
            continue
        if field == "logical_cpu":
            if type(value) is not int or not 0 <= value < 1 << 31:
                raise ValueError(
                    "Trace logical CPU filter must be a nonnegative integer"
                )
            result[field] = value
        elif field == "kinds":
            if not isinstance(value, (list, tuple)) or not 1 <= len(value) <= 16:
                raise ValueError("Choose 1–16 trace event kinds")
            if any(not isinstance(item, str) for item in value):
                raise ValueError("Trace event kind filters must be text")
            result[field] = [_identifier(item, "kind") for item in value]
        else:
            if field in {"node", "device_id", "kind"} and not isinstance(value, str):
                raise ValueError("Trace " + field + " filter must be text")
            result[field] = _identifier(value, field)
    if "kind" in result and "kinds" in result:
        raise ValueError("Choose kind or kinds, not both")
    return result


class IndexFilters:
    def _filter_scope(self, filters, *, source_ids=None, thread=None, search=""):
        result = dict(filters)
        if source_ids is not None:
            result["source_ids"] = (
                list(source_ids)
                if isinstance(source_ids, (list, tuple))
                else source_ids
            )
        if thread is not None:
            result["thread"] = thread
        if search:
            result["search"] = search
        return result

    def _filters_initialize(self):
        self._filter_contexts = {}
        self._facet_values = {}
        self._facet_pending = set()
        self._facet_truncated_pending = set()
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS facets(source TEXT NOT NULL,facet TEXT NOT NULL,value TEXT NOT NULL,PRIMARY KEY(source,facet,value));
            CREATE INDEX IF NOT EXISTS facet_values ON facets(facet,value,source);
            CREATE TABLE IF NOT EXISTS facet_limits(source TEXT NOT NULL,facet TEXT NOT NULL,PRIMARY KEY(source,facet));
        """)
        for table in ("intervals", "markers"):
            existing = {
                row[1] for row in self.db.execute(f"PRAGMA table_info({table})")
            }
            for column, kind in TAG_COLUMNS.items():
                if column not in existing:
                    self.db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")
            for column in (
                "tag_node",
                "tag_process",
                "tag_cpu",
                "tag_device_key",
                "tag_device_uuid",
                "tag_device_id",
                "tag_task",
                "tag_rank",
            ):
                self.db.execute(f"DROP INDEX IF EXISTS {table}_{column}")
                self.db.execute(
                    f"CREATE INDEX IF NOT EXISTS {table}_{column}_present ON {table}({column},clock,start,end) WHERE {column} IS NOT NULL"
                )
            self.db.execute(f"DROP INDEX IF EXISTS {table}_actor")
        self.db.execute(
            "CREATE INDEX IF NOT EXISTS intervals_scheduling_actor ON intervals(source,clock,tag_actor,tag_cpu,start,end) WHERE kind='scheduling' AND tag_cpu IS NOT NULL"
        )
        for facet, value in self.db.execute(
            "SELECT DISTINCT f.facet,f.value FROM facets f JOIN sources s ON s.id=f.source WHERE s.validated=1 AND s.status!='INVALID'"
        ):
            self._facet_values.setdefault(facet, set()).add(value)

    def _clear_filters(self, source):
        for table in ("facets", "facet_limits"):
            self.db.execute(f"DELETE FROM {table} WHERE source=?", (source,))
        self._filter_contexts.pop(source, None)
        self._facet_values = {}
        for facet, value in self.db.execute(
            "SELECT DISTINCT f.facet,f.value FROM facets f JOIN sources s ON s.id=f.source WHERE s.validated=1 AND s.status!='INVALID'"
        ):
            self._facet_values.setdefault(facet, set()).add(value)

    def _filter_context(self, source):
        if source not in self._filter_contexts:
            identity, clock = self.db.execute(
                "SELECT identity,clock FROM sources WHERE id=?", (source,)
            ).fetchone()
            self._filter_contexts[source] = dict(
                id=source,
                identity=json.loads(identity),
                clock=json.loads(clock),
            )
        return self._filter_contexts[source]

    def _row_tags(self, row):
        from .workspace_model_topology import (
            _logical_task,
            _producer_location,
            device_identity,
        )
        from .workspace_model_clocks import key

        source_id, data = row["source_id"], row.get("data", {})
        source = self._filter_context(source_id)
        identity = source["identity"]
        location = _producer_location(row, source, identity)

        def value(item):
            if item is None:
                return None
            try:
                return _identifier(item, "reported identity")
            except ValueError:
                return None

        cpu = row.get("logical_cpu", data.get("logical_cpu"))
        cpu = cpu if type(cpu) is int and 0 <= cpu < 1 << 31 else None
        process = value(data.get("source_pid", identity.get("pid")))
        thread = value(data.get("source_tid", row.get("thread")))
        generation = tuple(
            data.get(field)
            for field in (
                "source_process_start",
                "source_process_uuid",
                "process_generation",
            )
        )
        task, task_generation = _logical_task(data)
        tags = dict(
            tag_node=value(location["node"]),
            tag_process=process,
            tag_thread=thread,
            tag_cpu=cpu,
            tag_device_key=None,
            tag_device_uuid=None,
            tag_device_id=None,
            tag_device_ordinal=None,
            tag_task=value(task),
            tag_rank=value(data.get("source_rank", identity.get("rank"))),
            tag_actor=hashlib.sha256(
                _json(
                    [
                        source_id,
                        location["id"],
                        process,
                        generation,
                        thread,
                        data.get("thread_generation"),
                        task,
                        task_generation,
                    ]
                ).encode()
            ).hexdigest(),
        )
        if row["kind"].startswith("gpu_"):
            device = row.get("device", data.get("device", {}))
            device = device if isinstance(device, dict) else {}
            scope = (
                key(
                    "device-source",
                    source_id,
                    data.get("source_node"),
                    data.get("source_pid"),
                    generation,
                )
                if "source_pid" in data
                else source_id
            )
            try:
                observed = device_identity(
                    device,
                    source_id=scope,
                    node=location["node"],
                    boot_id=location["boot_id"],
                    cluster=identity.get("cluster", "unknown"),
                )
            except ValueError:
                observed = {}
            tags.update(
                tag_device_key=observed.get("id"),
                tag_device_uuid=value(device.get("uuid")),
                tag_device_id=value(device.get("id")),
                tag_device_ordinal=value(device.get("ordinal")),
            )
        facets = {
            "node": tags["tag_node"],
            "process": process,
            "thread": thread,
            "logical_cpu": cpu,
            "device_id": tags["tag_device_key"],
            "kind": row["kind"],
            "task": tags["tag_task"],
            "rank": tags["tag_rank"],
        }
        for facet, item in facets.items():
            if item is None:
                continue
            item = str(item)
            values = self._facet_values.setdefault(facet, set())
            if item in values or len(values) < 256:
                values.add(item)
                self._facet_pending.add((source_id, facet, item))
            else:
                self._facet_truncated_pending.add((source_id, facet))
        return tags

    def _store_filter_tags(self, table, row):
        tags = self._row_tags(row)
        self.db.execute(
            f"UPDATE {table} SET {','.join(column + '=?' for column in TAG_COLUMNS)} WHERE id=?",
            (*(tags[column] for column in TAG_COLUMNS), row["id"]),
        )

    def _flush_filters(self):
        self.db.executemany(
            "INSERT OR IGNORE INTO facets VALUES(?,?,?)", self._facet_pending
        )
        self.db.executemany(
            "INSERT OR IGNORE INTO facet_limits VALUES(?,?)",
            self._facet_truncated_pending,
        )
        self._facet_pending.clear()
        self._facet_truncated_pending.clear()

    def _filter_dimensions(self):
        result = {}
        for facet, value in self.db.execute(
            "SELECT DISTINCT f.facet,f.value FROM facets f JOIN sources s ON s.id=f.source WHERE s.validated=1 AND s.status!='INVALID' ORDER BY f.facet,f.value"
        ):
            values = result.setdefault(facet, [])
            if len(values) < 256:
                values.append(int(value) if facet == "logical_cpu" else value)
        truncated = sorted(
            {
                row[0]
                for row in self.db.execute(
                    "SELECT DISTINCT f.facet FROM facet_limits f JOIN sources s ON s.id=f.source WHERE s.validated=1 AND s.status!='INVALID'"
                )
            }
        )
        if "logical_cpu" in result:
            result["logical_cpu"].sort()
        return result, truncated

    def _filter_where(self, alias, filters, *, clock_id=None):
        filters = validate_filters(**filters)
        clauses, values = [], []
        for field, column in (
            ("node", "tag_node"),
            ("process", "tag_process"),
            ("task", "tag_task"),
            ("rank", "tag_rank"),
        ):
            if field in filters:
                clauses.append(f"{alias}.{column}=?")
                values.append(filters[field])
        if "device_id" in filters:
            clauses.append(
                "("
                + " OR ".join(
                    f"{alias}.{column}=?"
                    for column in (
                        "tag_device_key",
                        "tag_device_uuid",
                        "tag_device_id",
                        "tag_device_ordinal",
                    )
                )
                + ")"
            )
            values.extend([filters["device_id"]] * 4)
        if "logical_cpu" in filters:
            cpu = filters["logical_cpu"]
            predicate = f"{alias}.tag_cpu=?"
            values.append(cpu)
            if alias == "i":
                schedule = self._alignment_view("intervals", clock_id)
                predicate += f" OR ({alias}.kind IN ('python','host') AND EXISTS(SELECT 1 FROM {schedule} q WHERE q.source={alias}.source AND q.clock={alias}.clock AND q.tag_actor={alias}.tag_actor AND q.kind='scheduling' AND q.tag_cpu=? AND q.start<={alias}.end AND q.end>={alias}.start))"
                values.append(cpu)
            clauses.append("(" + predicate + ")")
        kinds = filters.get("kinds", [filters["kind"]] if "kind" in filters else None)
        if kinds:
            if (
                "logical_cpu" in filters
                and alias == "i"
                and {"python", "host"}.intersection(kinds)
            ):
                kinds = list(dict.fromkeys([*kinds, "scheduling"]))
            clauses.append(f"{alias}.kind IN ({','.join('?' for _ in kinds)})")
            values.extend(kinds)
        return clauses, values
