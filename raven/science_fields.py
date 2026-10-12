"""Bounded declared scientific matrices and volume slices for terminal views."""
from __future__ import annotations

import math

from .operations import digest
from .runtime_contracts import integer, number, object_, sequence, text

MAX_CELLS = 262144


def inspect_field(document, *, axis=0, index=0, bins=16, width=64, height=24):
    object_(document, {"schema", "identity", "shape", "values", "units"}, {"axes"})
    if document["schema"] != "raven.scientific-field/v1":
        raise ValueError("Expected raven.scientific-field/v1")
    text(document["identity"], "dataset identity", 512)
    text(document["units"], "units", 64)
    shape = sequence(document["shape"], "shape", 3, 1)
    for size in shape:
        integer(size, "axis size", 1, MAX_CELLS)
    cells = math.prod(shape)
    if cells > MAX_CELLS:
        raise ValueError(f"Field exceeds {MAX_CELLS} cells; declare a smaller extracted slice")
    values = sequence(document["values"], "flattened row-major values", MAX_CELLS)
    if len(values) != cells:
        raise ValueError("Field shape does not match flattened values")
    for value in values:
        if value is not None:
            number(value, "field value", low=-1e100)
    axes = document.get("axes", [f"axis{n}" for n in range(len(shape))])
    sequence(axes, "axis labels", 3)
    if len(axes) != len(shape):
        raise ValueError("Axis label count differs from dimensions")
    for label in axes:
        text(label, "axis label", 64)
    integer(bins, "histogram bins", 1, 128)
    integer(width, "view width", 1, 256)
    integer(height, "view height", 1, 128)
    integer(axis, "slice axis", 0, len(shape) - 1)
    integer(index, "slice index", 0, shape[axis] - 1)
    selected = []
    stride = math.prod(shape[axis + 1:])
    for flat, value in enumerate(values):
        if len(shape) < 3 or (flat // stride) % shape[axis] == index:
            selected.append((flat, value))
    view_shape = [size for n, size in enumerate(shape) if len(shape) < 3 or n != axis]
    rows, columns = (1, view_shape[0]) if len(view_shape) == 1 else view_shape
    out_rows, out_columns = min(rows, height), min(columns, width)
    tiles = [[[] for _ in range(out_columns)] for _ in range(out_rows)]
    for offset, (flat, value) in enumerate(selected):
        row, column = divmod(offset, columns)
        tiles[row * out_rows // rows][column * out_columns // columns].append((flat, value))
    def summary(items):
        present = [(flat, value) for flat, value in items if value is not None]
        lo = min(present, key=lambda pair: pair[1]) if present else None
        hi = max(present, key=lambda pair: pair[1]) if present else None
        return {"count": len(present), "gaps": len(items) - len(present),
                "min": lo[1] if lo else None, "max": hi[1] if hi else None,
                "min_flat_index": lo[0] if lo else None, "max_flat_index": hi[0] if hi else None,
                "mean": math.fsum(value / len(present) for _, value in present) if present else None}
    full = summary(list(enumerate(values)))
    current = summary(selected)
    histogram = [0] * bins
    if current["count"]:
        low, high = current["min"], current["max"]
        for _, value in selected:
            if value is not None:
                bucket = 0 if low == high else min(bins - 1, int((value - low) / (high - low) * bins))
                histogram[bucket] += 1
    return {"schema": "raven.field-view/v1", "source_digest": digest(document), "identity": document["identity"],
            "units": document["units"], "shape": shape, "axes": axes, "slice": {"axis": axis, "index": index} if len(shape) == 3 else None,
            "full": full, "selected": current, "tiles": [[summary(tile) for tile in row] for row in tiles],
            "histogram": {"counts": histogram, "min": current["min"], "max": current["max"]},
            "limitations": ["Declared bounded row-major JSON only; extract larger or non-JSON datasets with their native reader.",
                            "Tiles retain extrema and gap counts; means are not substituted for missing observations."]}


def terminal_rows(view, *, unicode=True):
    palette = " ▁▂▃▄▅▆▇█" if unicode else " .:-=+*#@"
    lo, hi = view["selected"]["min"], view["selected"]["max"]
    rows = []
    for row in view["tiles"]:
        line = ""
        for tile in row:
            if not tile["count"]:
                line += "·" if unicode else "?"
            elif tile["gaps"]:
                line += "░" if unicode else "?"
            else:
                offset = len(palette) - 1 if lo == hi else round((tile["mean"] - lo) / (hi - lo) * (len(palette) - 1))
                line += palette[max(0, min(len(palette) - 1, offset))]
        rows.append(line)
    return rows
