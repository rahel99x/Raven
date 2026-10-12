"""The dashboard's clock: ``now()`` is the wall clock unless a replay drives it (then it is the recording's time,
possibly paused, scrubbed or sped up).  Everything that stamps or ages data reads this; the sampler's own
scheduling stays on the wall clock."""
from __future__ import annotations

import time
from typing import Callable

_source: Callable[[], float] = time.time


def now() -> float:
    return _source()


def set_source(fn: Callable[[], float]) -> None:
    global _source
    _source = fn


def reset() -> None:
    global _source
    _source = time.time


def today() -> str:
    return time.strftime("%Y-%m-%d", time.localtime(now()))
