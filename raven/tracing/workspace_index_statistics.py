"""Incremental density/function statistics over source-scoped derived evidence."""

import hashlib
import json
import time

from .workspace_index_storage import _json, _limit, _time
from .workspace_index_filters import validate_filters


class IndexStatistics:
    def _note_interval_statistics(self, row, *, namespace=""):
        if namespace not in {"", "aligned_"}:
            raise ValueError("Unsupported trace statistics namespace")
        for level in range(0, 65, 4):
            key = (
                namespace,
                row["source_id"],
                row["clock_id"],
                level,
                _time(row["start_ns"] >> level),
            )
            self._overview_pending[key] = self._overview_pending.get(key, 0) + 1
        scope = dict(source_id=row["source_id"])
        producer = row.get("data", {}).get("source_pid")
        if isinstance(producer, (str, int)) and not isinstance(producer, bool):
            scope.update(
                reported_pid=str(producer)[:256],
                reported_node=row.get("node", "unknown"),
            )
            for field in (
                "source_process_start",
                "source_process_uuid",
                "process_generation",
            ):
                value = row.get("data", {}).get(field)
                if isinstance(value, (str, int)) and not isinstance(value, bool):
                    scope[field] = str(value)[:256]
        if row["kind"].startswith("gpu_"):
            if isinstance(row.get("device"), dict):
                scope["reported_device"] = row["device"]
            if isinstance(row.get("context"), (str, int)) and not isinstance(
                row.get("context"), bool
            ):
                scope["reported_context"] = str(row["context"])[:256]
        symbol = hashlib.sha256(
            json.dumps(
                [scope, row["kind"], row["name"], row.get("file", ""), row.get("line")],
                sort_keys=True,
                ensure_ascii=True,
                allow_nan=False,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        row["symbol_key"], row["symbol_scope"] = symbol, scope
        if row["kind"] == "scheduling":
            return
        key = namespace, row["source_id"], symbol
        pending = self._function_pending.get(key)
        duration = row["duration_ns"]
        if pending is None:
            pending = self._function_pending[key] = dict(
                clock=row["clock_id"],
                name=row["name"],
                kind=row["kind"],
                file=row.get("file", ""),
                line=row.get("line"),
                count=0,
                total=0,
                self_total=0,
                self_known=0,
                min=duration,
                max=duration,
                scope=scope,
            )
        pending["count"] += 1
        pending["total"] += duration
        pending["min"] = min(pending["min"], duration)
        pending["max"] = max(pending["max"], duration)
        if row.get("self_ns") is not None:
            pending["self_total"] += row["self_ns"]
            pending["self_known"] += 1

    def _flush_statistics(self):
        self._flush_filters()
        for namespace in ("", "aligned_"):
            self.db.executemany(
                f"INSERT INTO {namespace}overview_counts VALUES(?,?,?,?,?) ON CONFLICT(source,clock,level,bucket) DO UPDATE SET count=count+excluded.count",
                [
                    (*key[1:], value)
                    for key, value in self._overview_pending.items()
                    if key[0] == namespace
                ],
            )
        self._overview_pending.clear()
        for (namespace, source, symbol), item in self._function_pending.items():
            prior = self.db.execute(
                f"SELECT count,total,self_total,self_known,min,max FROM {namespace}function_stats WHERE source=? AND symbol=?",
                (source, symbol),
            ).fetchone()
            count, total, self_total, known, low, high = (
                (0, 0, 0, 0, item["min"], item["max"])
                if prior is None
                else (int(value) for value in prior)
            )
            self.db.execute(
                f"INSERT OR REPLACE INTO {namespace}function_stats(source,symbol,clock,name,search,kind,file,line,count,total,self_total,self_known,min,max,scope) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    source,
                    symbol,
                    item["clock"],
                    item["name"],
                    item["name"].casefold(),
                    item["kind"],
                    item["file"],
                    item["line"],
                    count + item["count"],
                    str(total + item["total"]),
                    str(self_total + item["self_total"]),
                    known + item["self_known"],
                    _time(min(low, item["min"])),
                    _time(max(high, item["max"])),
                    _json(item["scope"]),
                ),
            )
        self._function_pending.clear()

    def overview(self, *, bins=256, source_ids=None, clock_id=None):
        """Read bounded multiresolution interval-start density, not utilization.

        Seventeen base-16 levels are incremented as verified intervals close.
        Queries read at most the requested number of aligned aggregate buckets;
        exact duration/overlap remains available from the range index.
        """
        _limit(bins, 1024)
        with self.lock, self._query_budget():
            catalog = self.catalog()
            signature = tuple(
                (
                    row["id"],
                    row["read_seq"],
                    row["status"],
                    row["validated"],
                    row.get("error"),
                )
                for row in catalog["sources"]
            )
            key = (
                bins,
                tuple(sorted(source_ids)) if source_ids is not None else None,
                clock_id,
                signature,
            )
            if key in self._overview_cache:
                return json.loads(self._overview_cache[key])
            where, values = self._where("b", source_ids=source_ids, clock_id=clock_id)
            bounds = self.db.execute(
                f"SELECT b.clock,min(b.start),max(b.end) FROM {self._alignment_view('source_bounds', clock_id)} b JOIN sources s ON s.id=b.source WHERE {where} GROUP BY b.clock",
                values,
            ).fetchall()
            # The overview budget belongs to the whole run, not separately
            # to each node. Keep at least one bucket per raw clock domain.
            domain_bins = max(1, bins // max(1, len(bounds)))
            domains = []
            for domain, first, last in bounds:
                start, end = int(first), int(last)
                level = 0
                while (
                    level < 64 and (end >> level) - (start >> level) + 1 > domain_bins
                ):
                    level += 4
                quantum = 1 << level
                low, high = start >> level, end >> level
                where, values = self._where("o", source_ids=source_ids, clock_id=domain)
                rows = self.db.execute(
                    f"SELECT o.bucket,sum(o.count) FROM {self._alignment_view('overview_counts', clock_id)} o JOIN sources s ON s.id=o.source WHERE {where} AND o.level=? AND o.bucket>=? AND o.bucket<=? GROUP BY o.bucket ORDER BY o.bucket",
                    (*values, level, _time(low), _time(high)),
                ).fetchall()
                counts = {int(bucket): count for bucket, count in rows}
                density = [
                    dict(
                        start_ns=max(start, bucket * quantum),
                        end_ns=min(end, (bucket + 1) * quantum),
                        count=counts.get(bucket, 0),
                    )
                    for bucket in range(low, high + 1)
                ]
                domains.append(
                    dict(
                        clock_id=domain,
                        start_ns=start,
                        end_ns=end,
                        quantum_ns=quantum,
                        bins=density,
                    )
                )
            result = dict(
                schema="raven.trace-workspace-overview/v1",
                domains=domains,
                bins=bins,
                total_bins=sum(len(domain["bins"]) for domain in domains),
                bucket_limit=max(bins, len(domains)),
                scope="Exact matched-interval START counts within the declared time buckets; density is not CPU utilization, idle time or continuous coverage",
            )
            if len(self._overview_cache) >= 8:
                self._overview_cache.clear()
            self._overview_cache[key] = _json(result)
            return result

    def functions(
        self,
        *,
        source_ids=None,
        start_ns=None,
        end_ns=None,
        limit=100,
        clock_id=None,
        thread=None,
        search="",
        **filters,
    ):
        _limit(limit, 1000)
        filters = validate_filters(**filters)
        with self.lock, self._query_budget():
            if clock_id is None and (start_ns is not None or end_ns is not None):
                selected, parameters = self._where("b", source_ids=source_ids)
                domains = self.db.execute(
                    f"SELECT count(DISTINCT b.clock) FROM source_bounds b JOIN sources s ON s.id=b.source WHERE {selected}",
                    parameters,
                ).fetchone()[0]
                if domains > 1:
                    raise ValueError(
                        "Choose a clock domain for range statistics; unrelated clocks cannot share a window"
                    )
            if start_ns is None and end_ns is None and thread is None and not filters:
                where, values = self._where(
                    "f", source_ids=source_ids, clock_id=clock_id, search=search
                )
                raw = self.db.execute(
                    f"SELECT f.name,f.kind,f.file,f.line,f.count,f.total,f.self_total,f.min,f.max,f.self_known,f.source,f.scope FROM {self._alignment_view('function_stats', clock_id)} f JOIN sources s ON s.id=f.source WHERE {where} ORDER BY f.count DESC,f.name,f.source LIMIT ?",
                    (*values, limit + 1),
                )
            else:
                where, values = self._where(
                    "i",
                    source_ids=source_ids,
                    start_ns=start_ns,
                    end_ns=end_ns,
                    clock_id=clock_id,
                    thread=thread,
                    search=search,
                    **filters,
                )
                where += " AND i.kind!='scheduling'"
                raw = self.db.execute(
                    f"SELECT i.name,i.kind,i.file,i.line,count(*),raven_sum(i.duration),raven_sum(i.self),min(i.duration),max(i.duration),count(i.self),i.source,i.symbol_scope FROM {self._alignment_view('intervals', clock_id)} i JOIN sources s ON s.id=i.source WHERE {where} GROUP BY i.source,i.symbol,i.name,i.kind,i.file,i.line ORDER BY count(*) DESC,i.name,i.source LIMIT ?",
                    (*values, limit + 1),
                )
            rows, payload_bytes, truncated = [], 0, False
            for row in raw:
                if len(rows) >= limit:
                    truncated = True
                    break
                if self._query_deadlines and time.monotonic() >= min(
                    self._query_deadlines
                ):
                    raise ValueError(
                        "Trace query work budget exceeded; narrow the range or sources"
                    )
                value = dict(
                    name=row[0],
                    kind=row[1],
                    file=row[2],
                    line=row[3],
                    count=row[4],
                    inclusive_ns=int(row[5]),
                    self_ns=int(row[6]) if row[9] == row[4] else None,
                    min_ns=int(row[7]),
                    max_ns=int(row[8]),
                    self_complete=row[9] == row[4],
                    source_id=row[10],
                    symbol_scope="Recorded labels scoped to source and reported process/device; binary equality is not assumed",
                    reported_identity=json.loads(row[11]),
                )
                amount = len(_json(value))
                if payload_bytes + amount > 8 << 20:
                    truncated = True
                    break
                payload_bytes += amount
                rows.append(value)
            return dict(
                schema="raven.trace-workspace-functions/v1",
                functions=rows,
                truncated=truncated,
                payload_bytes=payload_bytes,
                byte_limit=8 << 20,
                units="nanoseconds",
                clock_id=clock_id,
                filters=self._filter_scope(
                    filters, source_ids=source_ids, thread=thread, search=search
                ),
                alignment="producer-declared calibrated axis; interval uncertainty retained"
                if self._has_alignment(clock_id)
                else "raw recorded clocks; unrelated domains are not aligned",
                scope="Whole matched calls whose recorded ranges or calibrated uncertainty envelopes intersect the selection; concurrent totals are not job makespan",
                limitations=[
                    "Recorded elapsed time includes waits and instrumentation effects",
                    "Self elapsed time is available only for matched synchronous Python nesting",
                    "GPU activities are device durations, not occupancy, utilization or complete device stacks",
                    "Calibrated durations are affine clock estimates; drift-adjusted self elapsed remains unavailable, and statistics do not certify producer calibration",
                    "A CPU filter selects calls overlapping recorded execution on that CPU; inclusive totals remain whole-call elapsed, not on-CPU time",
                ],
            )
