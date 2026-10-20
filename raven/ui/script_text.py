"""Bounded display-safe script and command text layout; no feature owners."""

from __future__ import annotations
from ..research_text import clean
from ..layout import vlen


def _wrap(text, width, *, ascii_=False):
    """Bounded, display-safe command wrapping with no terminal escape output."""
    value = clean(text, ascii_=ascii_, limit=128 << 10)
    width = max(1, width)
    rows, current, used = [], [], 0
    for character in value:
        amount = vlen(character)
        if current and used + amount > width:
            rows.append("".join(current))
            current, used = [], 0
        current.append(character)
        used += amount
    if current:
        rows.append("".join(current))
    return rows or [""]
