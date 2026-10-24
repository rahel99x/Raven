"""Materialized reference-clock ranges over immutable raw index payloads.

A producer-declared affine calibration is optional alternate evidence. Raw
queries remain raw by default, and out-of-validity rows are not extrapolated.
"""

from __future__ import annotations

import json
import re
import hashlib

from .workspace_index_storage import _json, _time

ALIGNMENT_DDL = """
CREATE TABLE IF NOT EXISTS aligned_bounds(source TEXT NOT NULL,clock TEXT NOT NULL,start TEXT NOT NULL,end TEXT NOT NULL,PRIMARY KEY(source,clock));
"""


def _quote(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-z_][a-z0-9_]{0,127}", value):
        raise ValueError("Unsupported trace index column identity")
    return '"' + value + '"'


class AlignmentQueries:
    def _alignment_initialize(self):
        self.db.executescript(ALIGNMENT_DDL)
        self._alignment_columns = {}
        self._alignment_clocks = {}
        self._alignment_warnings = {}
        self._alignment_dirty_bounds = set()
        for table in ("overview_counts", "function_stats"):
            self.db.execute(
                f"CREATE TABLE IF NOT EXISTS aligned_{table} AS SELECT * FROM {table} WHERE 0"
            )
        self.db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS aligned_overview_key ON aligned_overview_counts(source,clock,level,bucket)"
        )
        self.db.execute(
            "CREATE INDEX IF NOT EXISTS aligned_overview_domain ON aligned_overview_counts(clock,level,bucket,source)"
        )
        self.db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS aligned_function_key ON aligned_function_stats(source,symbol)"
        )
        for table in ("intervals", "markers"):
            columns = [
                row[1]
                for row in self.db.execute(f"PRAGMA table_info({table})")
                if row[1] != "key"
            ]
            if not columns or not {
                "id",
                "source",
                "clock",
                "start",
                "end",
                "payload",
            } <= set(columns):
                raise ValueError("Unsupported trace index schema for calibrated ranges")
            names = ",".join(_quote(column) for column in columns)
            self.db.execute(
                f"CREATE TABLE IF NOT EXISTS aligned_{table} AS SELECT {names} FROM {table} WHERE 0"
            )
            actual = [
                row[1] for row in self.db.execute(f"PRAGMA table_info(aligned_{table})")
            ]
            if actual != columns:
                raise ValueError(
                    "Calibrated trace index schema changed; rebuild the local index"
                )
            self.db.execute(
                f"CREATE UNIQUE INDEX IF NOT EXISTS aligned_{table}_id ON aligned_{table}(id)"
            )
            self.db.execute(
                f"CREATE INDEX IF NOT EXISTS aligned_{table}_range ON aligned_{table}(clock,start,end)"
            )
            self.db.execute(
                f"CREATE INDEX IF NOT EXISTS aligned_{table}_source ON aligned_{table}(source,thread,start)"
            )
            self._alignment_columns[table] = columns

    def _alignment_clock(self, source):
        if source not in self._alignment_clocks:
            record = self.db.execute(
                "SELECT clock FROM sources WHERE id=?", (source,)
            ).fetchone()
            self._alignment_clocks[source] = json.loads(record[0]) if record else {}
        return self._alignment_clocks[source]

    def _aligned_row(self, row):
        from .workspace_model import align_interval

        source = row["source_id"]
        clock = self._alignment_clock(source)
        if not clock.get("calibration"):
            return None
        calibration = clock["calibration"]
        if calibration["reference_id"] == row["clock_id"]:
            self._alignment_warnings[source] = (
                "Calibration reference aliases its raw source clock; alternate alignment is unavailable"
            )
            return None
        try:
            aligned = align_interval(row, clock)
        except ValueError as exc:
            self._alignment_warnings[source] = (
                "Calibrated projection unavailable for some rows: " + str(exc)[:256]
            )
            return None
        result = dict(
            row,
            raw_start_ns=row["start_ns"],
            raw_end_ns=row["end_ns"],
            raw_duration_ns=row.get("duration_ns", row["end_ns"] - row["start_ns"]),
            raw_self_ns=row.get("self_ns"),
            raw_clock_id=row["clock_id"],
            clock_id=aligned["aligned_clock_id"],
            start_ns=aligned["aligned_start_ns"],
            end_ns=aligned["aligned_end_ns"],
            duration_ns=aligned["aligned_end_ns"] - aligned["aligned_start_ns"],
            alignment_uncertainty_ns=aligned["alignment_uncertainty_ns"],
            alignment_provenance=aligned["alignment_provenance"],
            alignment_status="producer-declared calibration; not independently qualified",
        )
        if calibration["rate_num"] != calibration["rate_den"]:
            result["self_ns"] = None
        if "lane_id" in result:
            result["lane_id"] = (
                "lane:"
                + hashlib.sha256(
                    _json([row["lane_id"], result["clock_id"]]).encode()
                ).hexdigest()
            )
        return result

    def _store_alignment(self, row, marker=False):
        normalized = self._aligned_row(row)
        source = row["source_id"]
        if not marker:
            removed = self.db.execute(
                "DELETE FROM aligned_markers WHERE id=?", (row["id"],)
            ).rowcount
            if removed and normalized is None:
                clock = self._alignment_clock(source)
                reference = clock.get("calibration", {}).get("reference_id")
                if reference:
                    self._alignment_dirty_bounds.add((source, reference))
        table = "markers" if marker else "intervals"
        if normalized is None:
            return None
        columns = self._alignment_columns[table]
        replacements = {
            "clock": normalized["clock_id"],
            "start": _time(
                max(0, normalized["start_ns"] - normalized["alignment_uncertainty_ns"])
            ),
            "end": _time(
                min(
                    (1 << 64) - 1,
                    normalized["end_ns"] + normalized["alignment_uncertainty_ns"],
                )
            ),
            "payload": _json(normalized),
        }
        if not marker:
            replacements.update(
                duration=_time(normalized["duration_ns"]),
                self=None
                if normalized.get("self_ns") is None
                else _time(normalized["self_ns"]),
            )
        fields, arguments = [], []
        for column in columns:
            if column in replacements:
                fields.append("?")
                arguments.append(replacements[column])
            else:
                fields.append("r." + _quote(column))
        names = ",".join(_quote(column) for column in columns)
        self.db.execute(
            f"INSERT OR REPLACE INTO aligned_{table}({names}) SELECT {','.join(fields)} FROM {table} r WHERE r.id=?",
            (*arguments, row["id"]),
        )
        self.db.execute(
            "INSERT INTO aligned_bounds VALUES(?,?,?,?) ON CONFLICT(source,clock) DO UPDATE SET start=min(start,excluded.start),end=max(end,excluded.end)",
            (
                source,
                normalized["clock_id"],
                replacements["start"],
                replacements["end"],
            ),
        )
        if not marker:
            self._note_interval_statistics(normalized, namespace="aligned_")
        return normalized

    def _clear_alignment(self, source):
        for table in (
            "aligned_intervals",
            "aligned_markers",
            "aligned_bounds",
            "aligned_overview_counts",
            "aligned_function_stats",
        ):
            self.db.execute(f"DELETE FROM {table} WHERE source=?", (source,))
        self._alignment_clocks.pop(source, None)
        self._alignment_warnings.pop(source, None)
        self._alignment_dirty_bounds = {
            item for item in self._alignment_dirty_bounds if item[0] != source
        }

    def _refresh_alignment_bounds(self):
        pending = self._alignment_dirty_bounds
        self._alignment_dirty_bounds = set()
        for source, clock in pending:
            bounds = self.db.execute(
                "SELECT min(start),max(end) FROM (SELECT start,end FROM aligned_intervals WHERE source=? AND clock=? UNION ALL SELECT start,end FROM aligned_markers WHERE source=? AND clock=?)",
                (source, clock, source, clock),
            ).fetchone()
            self.db.execute(
                "DELETE FROM aligned_bounds WHERE source=? AND clock=?", (source, clock)
            )
            if bounds and bounds[0] is not None:
                self.db.execute(
                    "INSERT INTO aligned_bounds VALUES(?,?,?,?)",
                    (source, clock, *bounds),
                )

    def _alignment_bounds(self):
        self._refresh_alignment_bounds()
        return [
            dict(
                clock_id=clock,
                start_ns=int(start),
                end_ns=int(end),
                alignment="producer-declared calibration; per-interval uncertainty retained",
            )
            for clock, start, end in self.db.execute(
                "SELECT b.clock,min(b.start),max(b.end) FROM aligned_bounds b JOIN sources s ON s.id=b.source WHERE s.validated=1 AND s.status!='INVALID' GROUP BY b.clock"
            )
        ]

    def _has_alignment(self, clock_id):
        return (
            clock_id is not None
            and self.db.execute(
                "SELECT 1 FROM aligned_bounds WHERE clock=? LIMIT 1", (clock_id,)
            ).fetchone()
            is not None
        )

    def _alignment_view(self, table, clock_id):
        if table not in {
            "intervals",
            "markers",
            "source_bounds",
            "overview_counts",
            "function_stats",
        }:
            raise ValueError("Unsupported calibrated trace table")
        if not self._has_alignment(clock_id):
            return table
        self._refresh_alignment_bounds()
        if table in {"source_bounds", "overview_counts", "function_stats"}:
            alternate = (
                "aligned_bounds" if table == "source_bounds" else "aligned_" + table
            )
            return f"(SELECT * FROM {table} UNION ALL SELECT * FROM {alternate})"
        columns = self._alignment_columns[table]
        raw = ",".join("r." + _quote(column) for column in columns)
        aligned = ",".join("a." + _quote(column) for column in columns)
        if table == "intervals":
            raw = "r.key AS key," + raw
            aligned = "NULL AS key," + aligned
        return f"(SELECT {raw} FROM {table} r UNION ALL SELECT {aligned} FROM aligned_{table} a WHERE NOT EXISTS(SELECT 1 FROM {table} original WHERE original.id=a.id AND original.clock=a.clock))"
