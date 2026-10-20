"""Bounded plain-text terminal summaries without dispatch or session effects."""

from __future__ import annotations
import re

MAX_TOTAL = 2 << 20


def clean(text, limit=4096):
    return "".join(
        char if char.isprintable() or char in "\n\t" else " "
        for char in str(text)[:limit]
    )


def recording_text(events):
    """Safe plain-text replay summary. Strip complete escape/control sequences."""
    data = b"".join(event["data"] for event in events if event["event"] == "output")
    text = data.decode("utf-8", "replace")
    text = re.sub(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\|$)", "", text)
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
    clipped = len(text) > MAX_TOTAL
    value = clean(text, MAX_TOTAL)
    return value + (
        "\n[Replay text summary truncated at 2 MiB; the recording retains additional output.]"
        if clipped
        else ""
    )
