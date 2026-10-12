"""Thin selector strokes on the terminal's portable 2-by-4 Braille lattice.

Mouse reports identify whole cells. The helpers below improve the *visual*
motion of an already received pointer; they never add measurement precision or
change the event coordinates used for selection. Braille supplies two dot
columns at roughly one-quarter/three-quarter cell width and four dot rows.
Their exact pixel positions remain a property of the terminal font.

This module returns characters and immutable geometry only. The caller owns
clipping, theme accent, the actual painted background, animation deadlines and
pointer state. No raster, source, clock read, terminal escape or IO is used.
"""
from __future__ import annotations

from dataclasses import dataclass
import math


HORIZONTAL_GLYPHS = "⠉⠒⠤⣀"
VERTICAL_GLYPHS = "⡇⢸"
SMOOTH_DURATION = 0.08
MAX_SMOOTH_DURATION = 0.2
# Pointer tracking eases only the dot phase inside the latest reported cell.
# Whole-cell movement must never wait behind a cosmetic animation.
SUBCELL_DURATION = 0.024
_HORIZONTAL_BITS = (0x09, 0x12, 0x24, 0xC0)
_VERTICAL_BITS = (0x47, 0xB8)


@dataclass(frozen=True)
class Cell:
    """A visible character cell and its quantized within-cell dot position."""

    row: int
    column: int
    y_slot: int
    x_slot: int


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def locate(y, x):
    """Map finite cell-edge coordinates to a 2-column, 4-row dot position.

    A mouse event at ``(row, column)`` has a visual center at
    ``(row + .5, column + .5)``. The caller must retain the original event for
    selection math. Invalid coordinates return ``None``; negative coordinates
    retain their correct cell so normal caller clipping can reject them.
    """
    y, x = _number(y), _number(x)
    if y is None or x is None:
        return None
    row, column = math.floor(y), math.floor(x)
    return Cell(row, column,
                min(3, max(0, int((y - row) * 4))),
                min(1, max(0, int((x - column) * 2))))


def glyph(y_slot=2, x_slot=1, *, horizontal=False, vertical=False,
          ascii_=False, fine=True):
    """Return one narrow stroke glyph; crossings merge both dot patterns.

    Fine Unicode uses four dots per vertical cell and two per horizontal cell.
    ASCII and ``fine=False`` retain the established dot/plus fallback. Callers
    can disable cosmetic animation without changing their selection controls.
    Invalid slots/flags return a blank rather than an unsafe replacement.
    """
    if (not isinstance(y_slot, int) or isinstance(y_slot, bool) or not 0 <= y_slot < 4
            or not isinstance(x_slot, int) or isinstance(x_slot, bool) or not 0 <= x_slot < 2
            or any(type(flag) is not bool for flag in (horizontal, vertical, ascii_, fine))):
        return " "
    if not horizontal and not vertical:
        return " "
    if ascii_ or not fine:
        return "+" if horizontal and vertical else "." if ascii_ else "·"
    bits = (_HORIZONTAL_BITS[y_slot] if horizontal else 0) | (_VERTICAL_BITS[x_slot] if vertical else 0)
    return chr(0x2800 + bits)


def _point(value):
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return None
    y, x = _number(value[0]), _number(value[1])
    return (y, x) if y is not None and x is not None else None


def interpolate(start, target, elapsed, *, duration=SMOOTH_DURATION):
    """Ease two received visual positions without overshoot or hidden state.

    The caller supplies monotonic elapsed seconds and owns feedback scheduling.
    At most 200ms of smoothing is permitted; the default settles in 80ms.
    Zero/negative duration snaps to the target. Negative elapsed retains the
    start, and malformed/nonfinite data returns ``None`` for a caller fallback.
    Retargeting should start from the last painted position, never replay an
    input queue or defer the exact mouse-event selection bounds.
    """
    start, target = _point(start), _point(target)
    elapsed, duration = _number(elapsed), _number(duration)
    if start is None or target is None or elapsed is None or duration is None:
        return None
    if start == target:
        # An already still pointer must retain its exact dot slot. A weighted
        # sum of equal floats can drift one ULP across a quarter-cell boundary.
        return target
    if duration <= 0:
        return target
    if elapsed <= 0:
        return start
    period = min(duration, MAX_SMOOTH_DURATION)
    if elapsed >= period:
        return target
    amount = elapsed / period
    amount = amount * amount * (3 - 2 * amount)
    # A weighted sum remains finite even when target-start would overflow.
    result = tuple((1 - amount) * first + amount * last for first, last in zip(start, target))
    return result if all(math.isfinite(value) for value in result) else (start if amount < .5 else target)
