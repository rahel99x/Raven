"""Owner-declared allocation/coverage context for recorded resource metrics.

The descriptor supplies denominators, not capture or scheduler authority. It
cannot make missing, partial or unaligned measurements complete. Epochs state
effective logical-CPU capacity explicitly, including changes over time.
"""

from __future__ import annotations

import json

SCHEMA = "raven.trace-resource-context/v1"
MAX_BYTES = 64 << 10
MAX_EPOCHS = 128
MAX_NS = (1 << 64) - 1


def _text(value, label, maximum=1024):
    if (
        not isinstance(value, str)
        or not 0 < len(value) <= maximum
        or not value.isprintable()
    ):
        raise ValueError(
            "Resource context " + label + " must be bounded printable text"
        )
    return value


def _integer(value, label, maximum=MAX_NS, minimum=0):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError("Invalid resource context " + label)
    return value


def validate_context(value):
    """Validate one bounded data-only context; reject unknown semantic fields."""
    if not isinstance(value, dict) or value.get("schema") != SCHEMA:
        raise ValueError("Unsupported trace resource context schema")
    if set(value) - {"schema", "identity", "provenance", "epochs", "description"}:
        raise ValueError("Unsupported trace resource context field")
    if len(json.dumps(value, ensure_ascii=True, allow_nan=False).encode()) > MAX_BYTES:
        raise ValueError("Trace resource context exceeds 64 KiB")
    identity = value.get("identity")
    if not isinstance(identity, dict) or set(identity) - {
        "cluster",
        "job_id",
        "attempt",
        "run_id",
    }:
        raise ValueError("Resource context requires one exact run identity")
    identity = {
        name: _text(identity.get(name), name, 256)
        for name in ("cluster", "job_id", "attempt")
    } | (
        {"run_id": _text(identity["run_id"], "run ID", 256)}
        if "run_id" in identity
        else {}
    )
    epochs = value.get("epochs")
    if not isinstance(epochs, list) or not 1 <= len(epochs) <= MAX_EPOCHS:
        raise ValueError("Resource context requires 1–128 effective allocation epochs")
    result = {
        "schema": SCHEMA,
        "identity": identity,
        "provenance": _text(value.get("provenance"), "provenance"),
        "epochs": [],
    }
    if "description" in value:
        result["description"] = _text(value["description"], "description", 4096)
    previous = {}
    for epoch in epochs:
        allowed = {
            "clock_id",
            "start_ns",
            "end_ns",
            "logical_cpu_count",
            "unit",
            "coverage_complete",
            "gpu_coverage_complete",
            "source_ids",
            "provenance",
        }
        if not isinstance(epoch, dict) or set(epoch) != allowed:
            raise ValueError(
                "Each resource epoch requires its exact clock, range, effective capacity, source scope and coverage fields"
            )
        clock = _text(epoch["clock_id"], "clock ID", 256)
        first, last = (
            _integer(epoch["start_ns"], "epoch start"),
            _integer(epoch["end_ns"], "epoch end"),
        )
        if first >= last or first < previous.get(clock, -1):
            raise ValueError(
                "Resource epochs must be ordered, nonempty and nonoverlapping within each clock"
            )
        previous[clock] = last
        if epoch["unit"] != "logical-cpu":
            raise ValueError(
                "Effective CPU capacity uses explicit logical-cpu units; physical cores/SMT are not inferred"
            )
        if any(
            type(epoch[name]) is not bool
            for name in ("coverage_complete", "gpu_coverage_complete")
        ):
            raise ValueError("Resource coverage flags must be explicit booleans")
        sources = epoch["source_ids"]
        if (
            not isinstance(sources, list)
            or not 1 <= len(sources) <= 512
            or any(not isinstance(item, str) for item in sources)
            or len(set(sources)) != len(sources)
        ):
            raise ValueError(
                "Each resource epoch needs 1–512 distinct exact source IDs"
            )
        sources = [_text(item, "source ID", 256) for item in sources]
        result["epochs"].append(
            dict(
                epoch,
                clock_id=clock,
                start_ns=first,
                end_ns=last,
                source_ids=sources,
                logical_cpu_count=_integer(
                    epoch["logical_cpu_count"], "logical CPU count", 8192, 1
                ),
                provenance=_text(epoch["provenance"], "epoch provenance"),
            )
        )
    return result


def _bounds(query):
    result = {}
    for row in (*query.get("bounds", ()), *query.get("aligned_bounds", ())):
        clock, first, last = row.get("clock_id"), row.get("start_ns"), row.get("end_ns")
        if (
            isinstance(clock, str)
            and type(first) is int
            and type(last) is int
            and first <= last
        ):
            lower, upper = result.get(clock, (first, last))
            result[clock] = min(lower, first), max(upper, last)
    return result


def _capacity(query, context, source_ids):
    identity = query.get("identity") or {}
    if any(identity.get(name) != value for name, value in context["identity"].items()):
        return {}, "Resource context identity differs from this exact run attempt"
    selected = query.get("range") or {}
    bounds = _bounds(query)
    if selected.get("clock_id"):
        bounds = {
            clock: value
            for clock, value in bounds.items()
            if clock == selected["clock_id"]
        }
    if not bounds:
        return (
            {},
            "No indexed clock bounds are available for the declared resource context",
        )
    catalog = {source.get("id"): source for source in query.get("sources", ())}
    requested = set(source_ids) if source_ids is not None else set(catalog)
    result, issues = {}, []
    for clock, (first, last) in bounds.items():
        first = (
            selected.get("start_ns") if selected.get("start_ns") is not None else first
        )
        last = selected.get("end_ns") if selected.get("end_ns") is not None else last
        if last <= first:
            continue
        known = {
            identifier
            for identifier, source in catalog.items()
            if identifier in requested
            and (
                (source.get("clock") or {}).get("id") == clock
                or ((source.get("clock") or {}).get("calibration") or {}).get(
                    "reference_id"
                )
                == clock
            )
        }
        if not known:
            known = {
                row.get("source_id")
                for row in (*query.get("intervals", ()), *query.get("markers", ()))
                if row.get("clock_id") == clock and row.get("source_id") in requested
            }
        epochs = [
            epoch
            for epoch in context["epochs"]
            if epoch["clock_id"] == clock
            and epoch["start_ns"] < last
            and epoch["end_ns"] > first
        ]
        cursor, integral, cpu_complete, gpu_complete, scope_complete = (
            first,
            0,
            True,
            True,
            True,
        )
        retained = []
        for epoch in epochs:
            left, right = max(first, epoch["start_ns"]), min(last, epoch["end_ns"])
            valid_scope = bool(known) and set(epoch["source_ids"]) == known
            contiguous = left == cursor
            scope_complete = scope_complete and contiguous and valid_scope
            cpu_complete = (
                cpu_complete
                and contiguous
                and valid_scope
                and epoch["coverage_complete"]
            )
            gpu_complete = (
                gpu_complete
                and contiguous
                and valid_scope
                and epoch["gpu_coverage_complete"]
            )
            integral += (right - left) * epoch["logical_cpu_count"]
            retained.append(dict(epoch, start_ns=left, end_ns=right))
            cursor = right
        cpu_complete = bool(retained) and cpu_complete and cursor == last
        gpu_complete = bool(retained) and gpu_complete and cursor == last
        if not retained or cursor != last or not cpu_complete or not gpu_complete:
            issues.append(
                clock
                + ": effective epochs, source scope or per-domain coverage are incomplete; unavailable fractions retain their reasons"
            )
        capacity = dict(
            clock_id=clock,
            start_ns=first,
            end_ns=last,
            allocation_epochs=retained,
            coverage_complete=bool(retained) and scope_complete and cursor == last,
            cpu_coverage_complete=cpu_complete,
            gpu_coverage_complete=gpu_complete,
            provenance=context["provenance"],
            source_ids=sorted(known),
        )
        counts = {epoch["logical_cpu_count"] for epoch in retained}
        if integral:
            capacity["allocated_logical_cpu_ns"] = integral
        if len(counts) == 1:
            capacity["logical_cpu_count"] = next(iter(counts))
        result[clock] = capacity
    return result, "; ".join(issues) or None


def metrics(query, *, context=None, source_ids=None):
    """Defined busy metrics plus observed sample shares, never inferred CPU time."""
    from .workspace_model import resource_metrics, sample_statistics

    capacities, reason = {}, None
    if context is not None:
        try:
            capacities, reason = _capacity(query, validate_context(context), source_ids)
        except (ValueError, TypeError, OverflowError) as exc:
            reason = str(exc)
    result = resource_metrics(query, capacity=capacities or None)
    predicates = {
        name: value
        for name, value in (query.get("filters") or {}).items()
        if name != "source_ids" and value is not None and value != ""
    }
    if predicates:
        reason = "Execution/search filters remove part of the measured numerator; this resource context declares source/range coverage, not a facet-scoped denominator"
        for item in result["metrics"]:
            if item.get("unit") == "fraction":
                item.update(value=None, availability="unavailable", reason=reason)
    result["sample_statistics"] = sample_statistics(query)
    result["context_status"] = dict(
        status="provided" if capacities else "unavailable",
        reason=reason
        or (
            "No owner-registered allocation and coverage context was supplied"
            if context is None
            else None
        ),
        schema=SCHEMA,
        provenance=context.get("provenance") if isinstance(context, dict) else None,
        qualification="Denominators and coverage are explicit owner declarations, not independently verified scheduler/capture facts",
    )
    if reason:
        for item in result["metrics"]:
            if item.get("value") is None and item.get("unit") == "fraction":
                item["reason"] = (item.get("reason") or "") + "; " + reason
    return result
