"""Immutable frame geometry and bounded publication storage.

The UI adapters own gestures; these records and transforms are usable by
composers without importing any gesture handler or application owner.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, replace
from typing import Any, Iterable

MAX_PLOTS = 96
MAX_PANES = 128


@dataclass(frozen=True)
class Rect:
    """Display-cell rectangle with exclusive bottom and right edges."""

    top: int
    left: int
    bottom: int
    right: int

    def contains(self, y: int, x: int) -> bool:
        return self.top <= y < self.bottom and self.left <= x < self.right

    def intersects(self, other: "Rect") -> bool:
        return (
            self.top < other.bottom
            and other.top < self.bottom
            and self.left < other.right
            and other.left < self.right
        )

    def clip(self, width: int, height: int) -> Rect | None:
        result = Rect(
            max(0, self.top),
            max(0, self.left),
            min(height, self.bottom),
            min(width, self.right),
        )
        return (
            result
            if result.top < result.bottom and result.left < result.right
            else None
        )

    @property
    def center(self) -> tuple[float, float]:
        return ((self.top + self.bottom - 1) / 2, (self.left + self.right - 1) / 2)


@dataclass(frozen=True)
class Plot:
    key: tuple[Any, ...]
    rect: Rect
    visible: Rect
    x_bounds: tuple[float, float]
    y_bounds: tuple[float, float]
    scale: str = "linear"
    layer: int = 0
    kind: str = "metric"
    payload: tuple[Any, ...] = ()
    viewport: Rect | None = None
    axes: Rect | None = None
    context: tuple[Any, ...] | None = None
    pane: str | None = None


def plot_state(app: Any) -> dict[str, Any]:
    state = getattr(app, "chart_interaction_state", None)
    if not isinstance(state, dict):
        state = {
            "pending": [],
            "plots": (),
            "pointer": None,
            "hovered": None,
            "capture": None,
            "zoom": OrderedDict(),
            "last_key": None,
            "revision": 0,
            "frame_context": None,
            "ascii": False,
            "cancelled_release": False,
        }
        app.chart_interaction_state = state
    return state


def translate_plot_rect(rect: Rect | None, dy: int = 0, dx: int = 0) -> Rect | None:
    return (
        Rect(rect.top + dy, rect.left + dx, rect.bottom + dy, rect.right + dx)
        if rect
        else None
    )


def plot_rect(value: Any) -> Rect | None:
    if isinstance(value, Rect):
        return value
    if (
        isinstance(value, (tuple, list))
        and len(value) == 4
        and all(isinstance(item, int) and not isinstance(item, bool) for item in value)
    ):
        return Rect(*value)
    return None


def intersect_plot_rect(a: Rect, b: Rect) -> Rect | None:
    result = Rect(
        max(a.top, b.top),
        max(a.left, b.left),
        min(a.bottom, b.bottom),
        min(a.right, b.right),
    )
    return result if result.top < result.bottom and result.left < result.right else None


def take_plots(app: Any, first: Any) -> tuple[Plot, ...]:
    """Detach a candidate layout so discarded width probes publish no plots."""
    state = plot_state(app)
    if (
        not isinstance(first, int)
        or isinstance(first, bool)
        or not 0 <= first <= len(state["pending"])
    ):
        return ()
    records = tuple(plot for plot in state["pending"][first:] if plot is not None)
    del state["pending"][first:]
    return records


def put_plots(app: Any, records: Iterable[Plot]) -> None:
    """Restore only the final candidate's already transformed geometry."""
    state = plot_state(app)
    for plot in records:
        if len(state["pending"]) >= MAX_PLOTS:
            break
        if isinstance(plot, Plot):
            state["pending"].append(plot)


def map_plots(
    records: Iterable[Plot], mapping: Any, *, dx: int = 0, dy: int = 0, clip: Any = None
) -> tuple[Plot, ...]:
    """Map native plot rows through wrapping/sticky-header layout.

    The renderer maps each original row to its actual final row. A curve is
    dropped if its visible rows become noncontiguous or change relative order.
    Missing rows at the top/bottom merely clip the gesture's visible region.
    """
    if any(not isinstance(item, int) or isinstance(item, bool) for item in (dx, dy)):
        return ()
    lookup = mapping if callable(mapping) else getattr(mapping, "get", None)
    if not callable(lookup):
        return ()
    clip = plot_rect(clip) if clip is not None else None
    output = []
    for plot in records:
        if not isinstance(plot, Plot):
            continue
        pairs = []
        for row in range(plot.visible.top, plot.visible.bottom):
            mapped = lookup(row)
            if isinstance(mapped, int) and not isinstance(mapped, bool):
                pairs.append((row, mapped))
        if not pairs or any(
            b[0] != a[0] + 1 or b[1] != a[1] + 1 for a, b in zip(pairs, pairs[1:])
        ):
            continue
        offset = pairs[0][1] - pairs[0][0] + dy
        rect = Rect(
            plot.rect.top + offset,
            plot.rect.left + dx,
            plot.rect.bottom + offset,
            plot.rect.right + dx,
        )
        visible = Rect(
            pairs[0][1] + dy,
            plot.visible.left + dx,
            pairs[-1][1] + dy + 1,
            plot.visible.right + dx,
        )
        viewport = translate_plot_rect(plot.viewport, offset, dx)
        clipped_visible = (
            intersect_plot_rect(visible, clip) if clip is not None else visible
        )
        if clip is not None:
            viewport = intersect_plot_rect(viewport, clip) if viewport else clip
        if clipped_visible:
            output.append(
                replace(
                    plot,
                    rect=rect,
                    visible=clipped_visible,
                    viewport=viewport,
                    axes=translate_plot_rect(plot.axes, offset, dx),
                )
            )
        if len(output) >= MAX_PLOTS:
            break
    return tuple(output)


@dataclass(frozen=True)
class Pane:
    key: str
    rect: Rect
    count: int
    page: int
    target: int
    painted: int
    setter: object
    context: object = ()
    header: tuple[int, int, int] | None = None
    layer: int = 0
    absolute: bool = False

    @property
    def limit(self) -> int:
        return max(0, self.count - self.page)

    @property
    def signature(self) -> tuple[Any, ...]:
        return self.key, self.rect, self.page, self.context, self.layer, self.header


def pane_state(app: Any) -> dict[str, Any]:
    owner = getattr(app, "_chart_owner", app)
    state = getattr(owner, "scrollbar_state", None)
    if not isinstance(state, dict):
        state = {
            "staged": [],
            "panes": (),
            "manual": OrderedDict(),
            "capture": None,
            "token": None,
            "blocked": (),
            "discard_release": False,
        }
        owner.scrollbar_state = state
    app.scrollbar_state = state
    return state


def pane_rect(value: Any) -> Rect | None:
    if isinstance(value, Rect):
        return value
    if (
        isinstance(value, (tuple, list))
        and len(value) == 4
        and all(isinstance(v, int) for v in value)
    ):
        return Rect(*value)
    return None


def clip_pane_rect(rect: Rect, clip: Rect | None) -> Rect | None:
    if clip is None:
        return rect
    result = Rect(
        max(rect.top, clip.top),
        max(rect.left, clip.left),
        min(rect.bottom, clip.bottom),
        min(rect.right, clip.right),
    )
    return result if result.bottom > result.top and result.right > result.left else None


def take_panes(app: Any, first: int) -> tuple[Pane, ...]:
    staged = pane_state(app)["staged"]
    records = tuple(staged[max(0, int(first)) :])
    del staged[max(0, int(first)) :]
    return records


def put_panes(app: Any, records: Iterable[Pane]) -> None:
    staged = pane_state(app)["staged"]
    staged.extend(item for item in records if isinstance(item, Pane))
    del staged[MAX_PANES:]


def map_panes(
    records: Iterable[Pane],
    mapping: Any = None,
    *,
    dy: int = 0,
    dx: int = 0,
    row_map: Any = None,
    clip: Any = None,
) -> tuple[Pane, ...]:
    mapping = row_map if row_map is not None else mapping
    lookup = mapping if callable(mapping) else getattr(mapping, "get", None)
    clip = pane_rect(clip) if clip is not None else None
    result = []
    for pane in records:
        if pane.absolute:
            result.append(pane)
            continue
        rect, header = pane.rect, pane.header
        if callable(lookup):
            mapped = [lookup(y) for y in range(rect.top, rect.bottom)]
            mapped = [y for y in mapped if isinstance(y, int)]
            if not mapped:
                continue
            top, bottom = min(mapped), max(mapped) + 1
            header_y = lookup(header[0]) if header else None
        else:
            top, bottom = rect.top, rect.bottom
            header_y = header[0] if header else None
        moved = clip_pane_rect(
            Rect(top + dy, rect.left + dx, bottom + dy, rect.right + dx), clip
        )
        if moved is None:
            continue
        if header and isinstance(header_y, int):
            header = (header_y + dy, header[1] + dx, header[2] + dx)
            if clip and not (
                clip.top <= header[0] < clip.bottom
                and header[1] >= clip.left
                and header[1] + 4 <= clip.right
            ):
                header = None
        else:
            header = None
        result.append(replace(pane, rect=moved, header=header))
    return tuple(result)
