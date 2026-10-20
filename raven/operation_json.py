"""Canonical operation JSON, independent of transports and UI adapters."""

import hashlib
import json


def canonical(value):
    try:
        data = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
    except (ValueError, TypeError, RecursionError) as exc:
        raise ValueError("Operation data must be finite, bounded JSON.") from exc
    if len(data) > 16 << 20:
        raise ValueError("Operation data exceeds the 16 MiB limit.")
    return data


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()
