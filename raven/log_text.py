"""Safe display text for logs; original source bytes remain in LogBuffer.

Normalize at read time, rather than re-parsing terminal controls on every frame.
Only complete escape sequences are discarded. Malformed or unfinished sequences
remain visible so an incomplete log write cannot hide the following text.
"""
from __future__ import annotations

import re


_CONTROLS = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_ANSI = re.compile(
    # CSI, including the eight-bit form and private/intermediate parameters.
    r"(?:\x1b\[|\x9b)[0-?]*[ -/]*[@-~]"
    # OSC titles/hyperlinks end in BEL or ST. A new string introducer or ESC
    # stops an unterminated match, keeping repeated malformed inputs linear.
    r"|(?:\x1b\]|\x9d)[^\x1b\x07\n\x90\x98\x9c-\x9f]*(?:\x07|\x1b\\|\x9c)"
    # DCS, SOS, PM and APC payloads end in ST.
    r"|(?:\x1b[PX^_]|[\x90\x98\x9e\x9f])[^\x1b\n\x90\x98\x9c-\x9f]*(?:\x1b\\|\x9c)"
    # Character-set designations and standard short terminal controls. Unknown
    # bare ESC+text is kept visible instead of consuming a printable prefix.
    r"|\x1b(?:[ -/]+[0-~]|[78=>DEHMNOZcno|}~\\])"
)


def _visible_control(match):
    char = match.group()
    value = ord(char)
    if char == "\t":
        return "    "
    if value < 32:
        return "^" + chr(value + 64)
    if value == 127:
        return "^?"
    return f"\\x{value:02x}"


def display_text(text: str) -> str:
    """Remove complete terminal commands and expose other controls safely.

    The fast path reuses ordinary text. Callers trim completed CRLF endings
    before normalization; embedded carriage returns remain visible as ``^M``.
    """
    if text.isprintable():
        return text
    if "\x1b" in text or any(char in text for char in "\x90\x98\x9b\x9d\x9e\x9f"):
        text = _ANSI.sub("", text)
        if text.isprintable():
            return text
    return _CONTROLS.sub(_visible_control, text)
