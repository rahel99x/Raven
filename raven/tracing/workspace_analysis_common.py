"""Shared validation for bounded investigation data; no collection or I/O."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
import math

from .workspace_model_clocks import integer, key, text, transform_time
from .workspace_model_topology import _data, _items, _sources

MAX_GRAPH_NODES = 8192
MAX_GRAPH_EDGES = 8192
MAX_ISSUES = 128
LOSS_KINDS = frozenset({"gap", "lost", "drop", "loss", "dropped"})


def label(value, default="unknown", maximum=512):
    from ..job_logs.text import display_text

    return display_text(value[:maximum]) if isinstance(value, str) else default


def validate_rows(query):
    intervals, markers = _items(query)
    sources = _sources(query)
    rows = {}
    for row in (*intervals, *markers):
        identifier = text(row.get("id"), "analysis event ID", 1024)
        if identifier in rows:
            raise ValueError("Duplicate workspace analysis event ID")
        text(row.get("source_id"), "analysis source ID")
        text(row.get("clock_id"), "analysis clock domain", 1024)
        start = integer(row.get("start_ns"), "analysis event start")
        end = integer(row.get("end_ns", start), "analysis event end")
        if end < start:
            raise ValueError("Analysis interval end precedes its start")
        if (
            row.get("duration_ns") is not None
            and integer(row["duration_ns"], "analysis duration") != end - start
        ):
            raise ValueError("Analysis duration disagrees with recorded boundaries")
        _data(row)
        rows[identifier] = row
    return rows, sources


def issue(items, reason, *, evidence_ids=(), kind="unknown", **extra):
    if len(items) < MAX_ISSUES:
        items.append(
            dict(
                kind=kind, reason=reason, evidence_ids=list(evidence_ids)[:16], **extra
            )
        )


def partial(query):
    _, markers = _items(query)
    return (
        bool(query.get("truncated"))
        or query.get("caught_up") is False
        or any(
            row.get("status") in {"INVALID", "TRUNCATED", "QUOTA"}
            for row in _sources(query).values()
        )
        or any(row.get("kind") in LOSS_KINDS for row in markers)
    )


def clocks(sources):
    result, conflicted = {}, set()
    for source in sources.values():
        clock = source.get("clock")
        if not isinstance(clock, dict) or not isinstance(clock.get("id"), str):
            continue
        identifier = clock["id"]
        signature = (clock.get("calibration"), clock.get("resolution_ns"))
        if identifier in result and signature != (
            result[identifier].get("calibration"),
            result[identifier].get("resolution_ns"),
        ):
            conflicted.add(identifier)
        else:
            result[identifier] = clock
    for identifier in conflicted:
        result.pop(identifier, None)
    return result, conflicted


def time_options(row, stamp, catalog):
    clock_id = row["clock_id"]
    uncertainty = integer(
        _data(row).get("timestamp_uncertainty_ns", 0), "event timestamp uncertainty"
    )
    uncertainty += integer(
        row.get("alignment_uncertainty_ns", 0), "event alignment uncertainty"
    )
    result = {
        clock_id: dict(
            time_ns=stamp,
            uncertainty_ns=uncertainty,
            provenance="Recorded clock coordinates; not independent timing qualification",
        )
    }
    clock = catalog.get(clock_id)
    if isinstance(clock, dict) and "calibration" in clock:
        try:
            value = transform_time(stamp, clock["calibration"])
        except ValueError:
            # An out-of-range calibration cannot map this point, but does not
            # invalidate its original same-clock coordinates.
            pass
        else:
            value["uncertainty_ns"] += uncertainty
            result[value["clock_id"]] = value
    return result


def aligned_points(points, sources):
    catalog, conflicted = clocks(sources)
    if any(row["clock_id"] in conflicted for row, _ in points):
        return None, "Conflicting calibration metadata for one clock domain"
    choices = []
    for row, stamp in points:
        try:
            choices.append(time_options(row, stamp, catalog))
        except ValueError as exc:
            return None, str(exc)
    shared = set(choices[0]) if choices else set()
    for value in choices[1:]:
        shared.intersection_update(value)
    if not shared:
        return (
            None,
            "Clock domains lack a common valid calibration; causal ordering does not establish elapsed time",
        )
    raw = {row["clock_id"] for row, _ in points}
    domain = (
        next(iter(raw))
        if len(raw) == 1 and next(iter(raw)) in shared
        else sorted(shared)[0]
    )
    return (domain, [value[domain] for value in choices]), None


def numeric(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    raw = str(value)
    if not 0 < len(raw) <= 128:
        return None
    try:
        number = Decimal(raw)
    except InvalidOperation:
        return None
    if (
        not number.is_finite()
        or len(number.as_tuple().digits) > 100
        or abs(number.adjusted()) > 10_000
    ):
        return None
    return number


def metric(
    name,
    value,
    *,
    unit="fraction",
    reason=None,
    numerator=None,
    denominator=None,
    **extra,
):
    return dict(
        name=name,
        value=value,
        unit=unit,
        availability="available" if value is not None else "unavailable",
        reason=reason,
        numerator=numerator,
        denominator=denominator,
        **extra,
    )


def scope_identity(query):
    identity = query.get("identity", {})
    if identity is None:
        return {}
    if not isinstance(identity, dict):
        raise ValueError("Analysis run identity must be an object")
    return {
        name: label(identity[name], maximum=256)
        for name in ("job_id", "attempt", "cluster", "run_id")
        if name in identity
    }


__all__ = [
    "MAX_GRAPH_NODES",
    "MAX_GRAPH_EDGES",
    "LOSS_KINDS",
    "validate_rows",
    "issue",
    "partial",
    "aligned_points",
    "numeric",
    "metric",
    "scope_identity",
    "label",
    "key",
    "text",
    "integer",
    "_data",
    "_items",
    "_sources",
]
