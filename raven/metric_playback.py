"""Small, bounded display buffers for already received metric observations.

This module advances a presentation clock, not a collector. Callers keep the
actual observations (including discontinuities) and interpolate only between
received neighbours. The playhead never predicts a value or passes the latest
received timestamp. The UI thread owns this transient state.
"""

from __future__ import annotations

from collections import OrderedDict, deque
from dataclasses import dataclass, field
import math

MAX_STREAMS = 128
MAX_INTERVALS = 7
MAX_DELAY = 30.0
DEFAULT_INTERVAL = 1.0
CATCHUP_RATE = 1.25
# Returning to a hidden graph after a long interval establishes a fresh buffer.
# Explicit generation changes handle shorter replay seeks and source epochs.
RESET_GAP = 60.0


@dataclass(frozen=True)
class Playback:
    end: float
    delay: float
    lag: float
    interval: float
    status: str


@dataclass
class _Track:
    generation: object
    now: float
    newest: float
    end: float
    intervals: deque = field(default_factory=lambda: deque(maxlen=MAX_INTERVALS))


@dataclass
class _Presentation:
    now: float
    end: float
    geometry: object
    edge_signature: object
    force_token: object
    oldest: float | None
    status: str


def _number(value):
    if type(value) not in (int, float):
        return None
    try:
        value = float(value)
    except (OverflowError, ValueError, TypeError):
        return None
    return value if math.isfinite(value) else None


def _state(app):
    owner = getattr(app, "_chart_owner", app)
    state = getattr(owner, "metric_playback_state", None)
    if not isinstance(state, OrderedDict):
        state = OrderedDict()
        owner.metric_playback_state = state
    return state


def _presentations(app):
    owner = getattr(app, "_chart_owner", app)
    state = getattr(owner, "metric_presentation_state", None)
    if not isinstance(state, OrderedDict):
        state = OrderedDict()
        owner.metric_presentation_state = state
    return state


def reset(app, identity=None):
    """Forget one source, or all sources on an explicit replay discontinuity."""
    for state in (_state(app), _presentations(app)):
        if identity is None:
            state.clear()
        else:
            try:
                state.pop(identity, None)
            except (TypeError, ValueError):
                pass


def reset_presentation(app, identity=None):
    """Leave a full-history gate without restarting the adaptive source clock."""
    state = _presentations(app)
    if identity is None:
        state.clear()
    else:
        try:
            state.pop(identity, None)
        except (TypeError, ValueError):
            pass


def present(app, identity, playback, *, now, geometry, x_interval,
            edge_signature, force_token=None, oldest=None, reveal_changed=False):
    """Publish useful full-history motion without rebuilding unchanged rasters.

    Callers opt in only for automatic full-history views. ``identity`` belongs
    to this presentation: a filled companion and line plot use distinct keys,
    even when they share the underlying source clock. Geometry, edge signature
    and force token must be small immutable hashable values.

    ``x_interval`` is one horizontal raster subcolumn in seconds. The edge
    signature contains the candidate edge's Y pixel, its exact acquired
    bracketing timestamps/indices, and continuity/validity. Changing a bracket
    publishes every newly revealed observation, even a spike whose endpoint
    returns to the same Y pixel. Callers force publication for changing Y
    bounds and include rate/mode/scale/theme/replay/attempt in the force token.

    The source clock continues to advance independently. This result's end and
    actual lag must be used together by the raster, axes, input map and label.
    Source corrections retain their existing exact raster-cache invalidation.
    Unsupported gate inputs bypass the optimization and discard this gate.
    Keeping an older published endpoint after a bypass would rewind the next
    valid frame, since the bypass may already have painted a newer endpoint.
    """
    try:
        hash(identity)
    except (TypeError, ValueError):
        return playback

    def bypass():
        _presentations(app).pop(identity, None)
        return playback

    if not isinstance(playback, Playback):
        return bypass()
    now, end, x_interval = _number(now), _number(playback.end), _number(x_interval)
    if (now is None or end is None or end > now or x_interval is None
            or x_interval <= 0 or not math.isfinite(now - end)):
        return bypass()
    try:
        hash((identity, geometry, edge_signature, force_token))
    except (TypeError, ValueError):
        return bypass()
    oldest = _number(oldest)
    state = _presentations(app)
    previous = state.get(identity)
    changed = (previous is None or reveal_changed
               or geometry != previous.geometry or force_token != previous.force_token
               or oldest != previous.oldest or playback.status != previous.status
               or now < previous.now or now - previous.now > RESET_GAP
               or end < previous.end or end - previous.end >= x_interval
               or edge_signature != previous.edge_signature)
    if changed:
        state[identity] = _Presentation(now, end, geometry, edge_signature,
                                        force_token, oldest, playback.status)
    else:
        end = previous.end
        previous.now = now
    state.move_to_end(identity)
    while len(state) > MAX_STREAMS:
        state.popitem(last=False)
    return Playback(end, playback.delay, now - end, playback.interval, playback.status)


def advance(app, identity, *, now, newest, previous=None, poll_interval=None,
            generation=None, oldest=None):
    """Advance one exact source/attempt without source I/O or history scans.

    ``newest`` and optional ``previous`` are the latest two distinct received
    timestamps at or before ``now``. ``oldest`` is optional available-history
    metadata; it prevents startup from beginning before the first observation.
    Pass a changed ``generation`` for replay seeks or source replacement.

    Two collection intervals absorb normal publication jitter. The estimate is
    the larger of effective polling and the median of seven observed intervals,
    with a 30-second maximum delay. A slower source increases the buffer by
    holding the playhead, not reversing it. Recovery and faster polling catch
    up at at most 1.25 times dashboard time. Gaps and absent history remain gaps.

    Invalid/future latest timestamps return ``None`` without changing state.
    A backward clock/source timestamp, changed generation, or more than one
    minute between visits resets this source's presentation clock. If retained
    history moves beyond the playhead, advance to its oldest available sample
    and buffer again without discarding the collection-cadence estimate.
    """
    try:
        hash(identity)
    except (TypeError, ValueError):
        return None
    now, newest = _number(now), _number(newest)
    if (now is None or newest is None or newest > now
            or not math.isfinite(now - newest)):
        return None
    previous, oldest = _number(previous), _number(oldest)
    if previous is not None and previous >= newest:
        previous = None
    if oldest is not None and oldest > newest:
        oldest = None
    poll_interval = _number(poll_interval)
    if poll_interval is not None and poll_interval <= 0:
        poll_interval = None

    state = _state(app)
    track = state.get(identity)
    elapsed = now - track.now if track is not None else 0.0
    if (track is None or track.generation != generation or elapsed < 0
            or elapsed > RESET_GAP or newest < track.newest):
        track = None
    fresh = track is None
    if fresh:
        track = _Track(generation, now, newest, newest)
        state[identity] = track
    elif newest > track.newest and previous is None:
        # A caller without an index can still measure distinct publications.
        previous = track.newest
    if (fresh or newest > track.newest) and previous is not None:
        interval = newest - previous
        if math.isfinite(interval) and interval > 0:
            track.intervals.append(interval)
    samples = sorted(track.intervals)
    count = len(samples)
    observed = (samples[count // 2] if count % 2 else
                (samples[count // 2 - 1] / 2 + samples[count // 2] / 2)
                if count else None)
    interval = max(poll_interval or 0.0, observed or 0.0) or DEFAULT_INTERVAL
    delay = min(MAX_DELAY, 2 * interval)
    target = min(now - delay, newest)
    if fresh:
        end = max(oldest, target) if oldest is not None else target
    else:
        # Cap the step, including after a publication resumes. In particular,
        # never jump straight to the new newest observation after an outage.
        end = max(track.end, min(target, track.end + CATCHUP_RATE * elapsed))
        if oldest is not None:
            # A truncated or replaced retained history cannot supply the old
            # playhead. Resume at the first actual observation, just as on
            # startup, rather than spending minutes behind absent history.
            end = max(oldest, end)
    end = min(end, newest, now)
    if oldest is not None and (newest == oldest or end <= oldest):
        status = "buffering"
    elif newest <= end or target < end:
        status = "held"
    elif not fresh and end == track.end and elapsed > 0:
        status = "held"
    else:
        status = "live"
    track.now, track.newest, track.end = now, newest, end
    state.move_to_end(identity)
    while len(state) > MAX_STREAMS:
        state.popitem(last=False)
    return Playback(end, delay, now - end, interval, status)
