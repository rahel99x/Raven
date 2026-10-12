"""Rank/phase skew from bounded, explicit profiler evidence."""
from __future__ import annotations

from collections import defaultdict
from itertools import islice
import statistics

from .scale_common import document, identity, items, number, text, display

FIELDS = ("elapsed_s", "cpu_s", "mpi_s", "io_s", "read_bytes", "write_bytes")


def analyze(value, *, job_id=""):
    document(value, "raven.profiler/v1")
    cluster, job, attempt = identity(value)
    if job_id and job != job_id:
        raise ValueError(f"Profiler belongs to job {job}, not selected job {job_id}")
    records = items(value.get("records"), "records", empty=False)
    expected = value.get("expected_ranks")
    if expected is not None:
        if not isinstance(expected, list) or not expected or len(expected) > 20000 or any(isinstance(rank, bool) or not isinstance(rank, int) or rank < 0 for rank in expected) or len(set(expected)) != len(expected):
            raise ValueError("expected_ranks must contain unique nonnegative rank integers")
        expected = set(expected)
    seen, phases, ranks = set(), defaultdict(list), defaultdict(list)
    normalized = []
    for record in records:
        rank = record.get("rank")
        if isinstance(rank, bool) or not isinstance(rank, int) or rank < 0:
            raise ValueError("rank must be a nonnegative integer")
        phase = text(record.get("phase"), "phase")
        node = text(record.get("node", "unknown"), "node")
        key = (rank, phase)
        if key in seen:
            raise ValueError(f"Duplicate rank/phase: {rank}/{phase}; export exclusive phase records")
        seen.add(key)
        if expected is not None and rank not in expected:
            raise ValueError("Observed rank is absent from expected_ranks")
        row = dict(rank=rank, phase=phase, node=node)
        for field in FIELDS:
            row[field] = number(record.get(field), field, optional=True)
        for field in ("mpi_s", "io_s"):
            if row[field] is not None and row["elapsed_s"] is not None and row[field] > row["elapsed_s"]:
                raise ValueError(f"{field} exceeds rank elapsed time; use exclusive wall durations")
        row["io_bytes_per_s"] = ((row["read_bytes"] + row["write_bytes"]) / row["io_s"]
                                 if row["io_s"] and row["read_bytes"] is not None and row["write_bytes"] is not None else None)
        phases[phase].append(row)
        ranks[rank].append(row)
        normalized.append(row)
    summaries, rows = [], []
    universe = sorted(expected if expected is not None else ranks)
    for phase, samples in sorted(phases.items()):
        elapsed = [r for r in samples if r["elapsed_s"] is not None]
        slowest = max(elapsed, key=lambda r: r["elapsed_s"]) if elapsed else None
        median = statistics.median(r["elapsed_s"] for r in elapsed) if elapsed else None
        peak = slowest["elapsed_s"] if slowest else None
        skew = peak / median if median else None
        present = {r["rank"] for r in samples}
        missing_count = len(universe) - len(present)
        summary = {"phase": phase, "ranks": len(samples), "measured_ranks": len(elapsed),
                   "max_s": peak, "median_s": median, "skew": skew,
                   "slowest_rank": slowest["rank"] if slowest else None,
                   "missing_rank_count": missing_count,
                   "missing_ranks_sample": list(islice((rank for rank in universe if rank not in present), 32)) if missing_count else []}
        for field in ("mpi_s", "io_s"):
            eligible = [r for r in elapsed if r[field] is not None and r["elapsed_s"] > 0]
            summary[field + "_fraction"] = (sum(r[field] for r in eligible) / sum(r["elapsed_s"] for r in eligible)) if eligible else None
            summary[field + "_measured_ranks"] = len(eligible)
        summaries.append(summary)
        rows.append(f"{phase}: {len(samples)} ranks; max {display(peak, 's')}; median {display(median, 's')}; skew {display(skew, 'x')}; slowest rank {summary['slowest_rank'] if slowest else 'unknown'}")
        rows.append(f"  MPI {display(summary['mpi_s_fraction'] * 100 if summary['mpi_s_fraction'] is not None else None, '%')} ({summary['mpi_s_measured_ranks']} measured ranks) | I/O {display(summary['io_s_fraction'] * 100 if summary['io_s_fraction'] is not None else None, '%')} ({summary['io_s_measured_ranks']} measured ranks)")
    warnings = ["Rank skew and MPI/I/O time are evidence, not proof of network or filesystem contention.",
                "CPU time may exceed wall time for threaded ranks. MPI and I/O fractions can overlap; do not add them."]
    if any(p["measured_ranks"] != len(expected if expected is not None else ranks) for p in summaries):
        warnings.append("Incomplete phase coverage: missing ranks or wall durations are not treated as zero.")
    if expected is None:
        warnings.append("Expected rank set is not declared; full rank coverage cannot be verified.")
    barrier_bound = None
    if value.get("ordered_barrier_phases") is True and expected is not None and all(p["measured_ranks"] == len(expected) for p in summaries):
        barrier_bound = sum(p["max_s"] for p in summaries)
        rows.append(f"Declared ordered barrier-phase envelope: {barrier_bound:.4g}s")
    return {"cluster": cluster, "job_id": job, "attempt": attempt, "phases": summaries,
            "records": normalized, "rank_count": len(ranks), "barrier_envelope_s": barrier_bound,
            "rows": rows, "warnings": warnings}


def import_darshan(raw, *, cluster, job_id, attempt):
    """Read darshan-parser POSIX counter output; no binary parser is executed.

    POSIX aggregate rank -1 records cannot reveal rank skew and are rejected.
    Missing counters remain unknown. Per-file counters are added per rank.
    """
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > 4 * 1024 * 1024:
        raise ValueError("Darshan text exceeds 4 MiB")
    counters = {"POSIX_BYTES_READ": "read_bytes", "POSIX_BYTES_WRITTEN": "write_bytes",
                "POSIX_F_READ_TIME": "read_s", "POSIX_F_WRITE_TIME": "write_s", "POSIX_F_META_TIME": "meta_s"}
    ranks, seen, file_counters = defaultdict(dict), set(), defaultdict(dict)
    for line in raw.splitlines():
        parts = line.split()
        if not parts or parts[0].startswith("#") or parts[0] != "POSIX":
            continue
        if len(parts) < 5:
            raise ValueError("Malformed Darshan POSIX record")
        try:
            rank = int(parts[1])
        except ValueError as exc:
            raise ValueError("Malformed Darshan rank") from exc
        if rank < 0:
            raise ValueError("Darshan shared-file rank -1 cannot provide per-rank attribution; export unaggregated rank counters")
        if parts[3] not in counters:
            continue
        key = (rank, parts[2], parts[3])
        if key in seen:
            raise ValueError("Duplicate Darshan record/counter")
        seen.add(key)
        if len(seen) > 60000 or len(ranks) > 20000:
            raise ValueError("Darshan counter limit exceeded")
        try:
            value = float(parts[4])
        except ValueError as exc:
            raise ValueError("Malformed Darshan value") from exc
        number(value, parts[3])
        field = counters[parts[3]]
        ranks[rank][field] = ranks[rank].get(field, 0) + value
        file_counters[(rank, parts[2])][field] = value
    records = []
    rank_files = defaultdict(list)
    for (rank, _), counter in file_counters.items():
        rank_files[rank].append(counter)
    for rank, values in sorted(ranks.items()):
        files = rank_files[rank]
        known = {field for field in counters.values() if all(field in counter for counter in files)}
        row = {"rank": rank, "phase": "POSIX", "node": "unknown", "read_bytes": values.get("read_bytes") if "read_bytes" in known else None, "write_bytes": values.get("write_bytes") if "write_bytes" in known else None}
        if all(field in known for field in ("read_s", "write_s", "meta_s")):
            row["io_s"] = values["read_s"] + values["write_s"] + values["meta_s"]
        records.append(row)
    return {"schema": "raven.profiler/v1", "cluster": cluster, "job_id": job_id, "attempt": attempt,
            "records": records, "producer": "darshan-parser POSIX text"}
