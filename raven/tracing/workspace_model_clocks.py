"""Recorded clock domains and explicitly declared calibration transforms.

These functions validate metadata; they do not measure clocks or certify a
producer's calibration. Original integer timestamps always remain available.
"""

from __future__ import annotations

import hashlib
import json

CLOCK_SCHEMA = "raven.trace-clock/v1"
CALIBRATION_SCHEMA = "raven.trace-clock-calibration/v1"
MAX_NS = (1 << 64) - 1
_UNKNOWN = frozenset({"", "unknown", "?", "none", "unavailable"})


def key(prefix, *parts):
    raw = json.dumps(parts, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return prefix + ":" + hashlib.sha256(raw.encode()).hexdigest()


def text(value, name, maximum=256, *, optional=False):
    if optional and value is None:
        return None
    if (
        not isinstance(value, str)
        or not 0 < len(value) <= maximum
        or not value.isprintable()
    ):
        raise ValueError(f"Trace {name} must be bounded printable text")
    return value


def integer(value, name, *, maximum=MAX_NS, minimum=0):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"Invalid trace {name}")
    return value


def known(value):
    return isinstance(value, str) and value.casefold() not in _UNKNOWN


def validate_calibration(value):
    """Validate a bounded integer affine transform and its stated error bound."""
    if not isinstance(value, dict) or value.get("schema") != CALIBRATION_SCHEMA:
        raise ValueError("Unsupported trace clock calibration schema")
    result = {
        "schema": CALIBRATION_SCHEMA,
        "reference_id": text(value.get("reference_id"), "reference clock ID"),
        "source_anchor_ns": integer(
            value.get("source_anchor_ns"), "source clock anchor"
        ),
        "reference_anchor_ns": integer(
            value.get("reference_anchor_ns"), "reference clock anchor"
        ),
        "rate_num": integer(
            value.get("rate_num", 1),
            "clock rate numerator",
            maximum=(1 << 63) - 1,
            minimum=1,
        ),
        "rate_den": integer(
            value.get("rate_den", 1),
            "clock rate denominator",
            maximum=(1 << 63) - 1,
            minimum=1,
        ),
        "uncertainty_ns": integer(value.get("uncertainty_ns"), "clock uncertainty"),
        "rate_error_ppb": integer(
            value.get("rate_error_ppb", 0), "clock drift bound", maximum=1_000_000_000
        ),
        "valid_start_ns": integer(value.get("valid_start_ns"), "calibration start"),
        "valid_end_ns": integer(value.get("valid_end_ns"), "calibration end"),
        "provenance": text(
            value.get("provenance", "producer-declared; not independently qualified"),
            "calibration provenance",
            1024,
        ),
    }
    if (
        not result["valid_start_ns"]
        <= result["source_anchor_ns"]
        <= result["valid_end_ns"]
    ):
        raise ValueError("Trace clock anchor is outside its calibration validity range")
    return result


def source_clock(identity, metadata=None):
    """Name v1 monotonic domains without implying cross-node synchronization.

    Known node/boot pairs share the protocol's monotonic domain. Unknown hosts
    or boots are isolated by process/stream. A producer report's common domain
    is scoped by its input digest, never by a device ordinal or bare clock name.
    Explicit calibrations are returned as available evidence, not applied here.
    """
    if not isinstance(identity, dict):
        raise ValueError("Trace source identity must be an object")
    stream = text(identity.get("stream"), "stream identity")
    node = text(identity.get("node", "unknown"), "node identity")
    boot = text(identity.get("boot_id", "unknown"), "boot identity")
    cluster = text(identity.get("cluster", "unknown"), "cluster identity")
    source = key(
        "source",
        cluster,
        node,
        boot,
        identity.get("pid"),
        identity.get("process_start"),
        stream,
    )
    value = metadata
    if isinstance(value, dict) and "clock" in value:
        value = value["clock"]
    if value is not None and not isinstance(value, dict):
        raise ValueError("Trace clock metadata must be an object")
    if value and value.get("schema") != CLOCK_SCHEMA:
        raise ValueError("Unsupported trace clock metadata schema")
    value = value or {}
    kind = value.get("kind", "monotonic")
    if kind not in {
        "monotonic",
        "producer-report",
        "producer-session",
        "device",
        "unknown",
    }:
        raise ValueError("Unsupported trace clock kind")
    name = text(value.get("id", "monotonic"), "clock ID")
    if kind == "producer-report":
        digest = value.get("report_sha256")
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(c not in "0123456789abcdef" for c in digest)
        ):
            raise ValueError("A producer report clock requires its input SHA256")
        domain = key("clock", "report", digest, name)
        alignment = "producer-report"
        explanation = "Shared clock declared by this one imported report; exporter calibration is not independently qualified"
    elif kind == "producer-session":
        session = text(value.get("session_id"), "producer clock session ID")
        producer = value.get("producer")
        if not isinstance(producer, dict):
            raise ValueError("A producer session clock requires its process lifetime")
        producer = dict(
            node=text(producer.get("node"), "producer node"),
            boot_id=text(producer.get("boot_id"), "producer boot identity"),
            pid=integer(
                producer.get("pid"), "producer PID", minimum=1, maximum=(1 << 31) - 1
            ),
            process_start=text(
                producer.get("process_start"), "producer process lifetime"
            ),
        )
        lifetime_known = all(
            known(producer[field]) for field in ("node", "boot_id", "process_start")
        )
        domain = key(
            "clock",
            "producer-session",
            cluster,
            session,
            producer,
            name,
            None if lifetime_known else source,
        )
        alignment = "producer-session" if lifetime_known else "source-local"
        explanation = (
            "Common report clock declared by one producer process lifetime/session; exporter continuity is not independently qualified"
            if lifetime_known
            else "Producer process lifetime is incomplete; the declared session clock remains isolated to this source"
        )
    elif kind == "monotonic" and known(node) and known(boot) and known(cluster):
        domain = key("clock", cluster, node, boot, kind, name)
        alignment = "same-host-monotonic"
        explanation = "Protocol monotonic timestamps share this recorded node/boot domain; other nodes remain unaligned"
    else:
        domain = key("clock", source, kind, name)
        alignment = "source-local"
        explanation = "Clock alignment is unavailable outside this source"
    result = dict(
        id=domain,
        kind=kind,
        name=name,
        node=node,
        boot_id=boot,
        alignment=alignment,
        provenance=explanation,
    )
    if kind == "producer-session":
        result.update(session_id=session, producer=producer)
    if "resolution_ns" in value:
        result["resolution_ns"] = integer(
            value["resolution_ns"], "clock resolution", minimum=1
        )
    if "calibration" in value:
        result["calibration"] = validate_calibration(value["calibration"])
        result["calibration_status"] = (
            "producer-declared; available but not applied to raw timestamps"
        )
    return result


def transform_time(value, calibration):
    """Return a mapped timestamp and conservative declared uncertainty.

    This is an opt-in calculation. A result outside calibration validity is
    refused; no extrapolated precision or producer qualification is invented.
    """
    value = integer(value, "timestamp")
    cal = validate_calibration(calibration)
    if not cal["valid_start_ns"] <= value <= cal["valid_end_ns"]:
        raise ValueError("Trace timestamp is outside clock calibration validity")
    delta = value - cal["source_anchor_ns"]
    numerator = delta * cal["rate_num"]
    quotient, remainder = divmod(numerator, cal["rate_den"])
    mapped = integer(cal["reference_anchor_ns"] + quotient, "mapped timestamp")
    drift = (abs(delta) * cal["rate_error_ppb"] + 999_999_999) // 1_000_000_000
    return {
        "time_ns": mapped,
        "clock_id": cal["reference_id"],
        "uncertainty_ns": cal["uncertainty_ns"] + drift + bool(remainder),
        "provenance": cal["provenance"],
    }


def align_interval(interval, clock):
    """Add an explicit alternate alignment while preserving the original row."""
    if not isinstance(clock, dict) or not isinstance(interval, dict):
        raise ValueError("Trace interval and clock must be objects")
    if interval.get("clock_id") != clock.get("id"):
        raise ValueError("Trace interval clock does not match calibration source")
    if "calibration" not in clock:
        raise ValueError("No trace clock calibration is available")
    first = transform_time(interval.get("start_ns"), clock["calibration"])
    last = transform_time(interval.get("end_ns"), clock["calibration"])
    if last["time_ns"] < first["time_ns"]:
        raise ValueError("Trace clock transform reversed an interval")
    return dict(
        interval,
        aligned_start_ns=first["time_ns"],
        aligned_end_ns=last["time_ns"],
        aligned_clock_id=first["clock_id"],
        alignment_uncertainty_ns=max(first["uncertainty_ns"], last["uncertainty_ns"]),
        alignment_provenance=first["provenance"],
    )
