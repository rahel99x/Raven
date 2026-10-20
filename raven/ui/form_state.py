"""Pure form text wrapping and pending-review query shared by setup forms."""

from __future__ import annotations
import re
from .. import layout as L


def _wrap(text, width):
    """Keep prose words intact while still fitting long paths and JSON."""
    rows, line, used = [], "", 0
    for chunk in re.findall(r"\s+|\S+", text):
        size = L.vlen(chunk)
        if line and not chunk.isspace() and size <= width and used + size > width:
            rows.append(line.rstrip())
            line, used = "", 0
        for char in chunk:
            size = L.vlen(char)
            if line and used + size > width:
                rows.append(line.rstrip())
                line, used = "", 0
            if not line and char.isspace():
                continue
            line += char
            used += size
    return rows + ([line] if line else []) or [""]


def _pending_review(app):
    return (
        getattr(app, "mode", "main") == "confirm"
        or bool(getattr(app, "execution_state", {}).get("pending_action"))
        or bool(getattr(app, "raven_ui_state", {}).get("review"))
    )
