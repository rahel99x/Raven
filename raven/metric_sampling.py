"""Compatibility sampling API; pure helpers live in cadence.

New collector code imports cadence directly. The sync adapter retains the
historic UI entry point and passes its explicit state to the runtime owner.
"""

from __future__ import annotations

from .cadence import (
    GPU_FLOOR,
    LOCAL_FILE_FLOOR,
    MAX_RATE,
    MAX_REQUESTS,
    MIN_RATE,
    NATIVE_SOURCES,
    REMOTE_FILE_FLOOR,
    SOURCE_FLOOR,
    SOURCE_FLOORS,
    attempt,
    file_interval,
    format_interval,
    interval,
    matches,
    poll_interval,
    research_matches,
    source,
    source_interval,
    validate_multiplier,
    validate_rate,
)
from .metric_sampling_runtime import cadence


def sync(app):
    from . import metric_live
    from .metric_sampling_runtime import sync as publish

    owner = getattr(app, "_chart_owner", app)
    return publish(app, state=metric_live.initialize(owner), eligible=metric_live._eligible)


# Explicit compatibility re-exports preserve the historical flat API.
__all__ = [
    "sync",
    "NATIVE_SOURCES",
    "SOURCE_FLOOR",
    "SOURCE_FLOORS",
    "LOCAL_FILE_FLOOR",
    "REMOTE_FILE_FLOOR",
    "poll_interval",
    "source_interval",
    "file_interval",
    "validate_multiplier",
    "MIN_RATE",
    "MAX_RATE",
    "MAX_REQUESTS",
    "GPU_FLOOR",
    "validate_rate",
    "interval",
    "format_interval",
    "source",
    "attempt",
    "matches",
    "research_matches",
    "cadence",
]
