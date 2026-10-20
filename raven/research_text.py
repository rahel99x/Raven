"""Pure bounded text normalization for research evidence and presentation."""


def clean(value: object, ascii_: bool = False, limit: int = 4096) -> str:
    """External file names and text never introduce terminal controls."""
    text = str(value)[:limit]
    text = "".join(ch if ch.isprintable() else " " for ch in text)
    return text.encode("ascii", "backslashreplace").decode("ascii") if ascii_ else text
