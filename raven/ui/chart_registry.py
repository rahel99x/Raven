"""Published chart placement, independent of chart input and rendering."""

from dataclasses import replace
from .geometry import (
    plot_state as initialize,
    Rect,
    plot_rect as _rect,
    translate_plot_rect as _translate,
    intersect_plot_rect as _intersection,
)


def place_since(app, first, *, dy=0, dx=0, clip=None):
    """Translate and clip nested plots without changing their axis mapping.

    A clip is in the translated coordinate system. Repeated placements compose
    correctly for Research scrolling, Jobs Details, and a docked history pane.
    """
    state = initialize(app)
    if (
        not isinstance(first, int)
        or isinstance(first, bool)
        or not 0 <= first <= len(state["pending"])
        or any(not isinstance(item, int) or isinstance(item, bool) for item in (dy, dx))
    ):
        return
    clip = _rect(clip) if clip is not None else None
    for i in range(first, len(state["pending"])):
        plot = state["pending"][i]
        if plot is None:
            continue
        rect = Rect(
            plot.rect.top + dy,
            plot.rect.left + dx,
            plot.rect.bottom + dy,
            plot.rect.right + dx,
        )
        visible = Rect(
            plot.visible.top + dy,
            plot.visible.left + dx,
            plot.visible.bottom + dy,
            plot.visible.right + dx,
        )
        viewport = _translate(plot.viewport, dy, dx)
        if clip is not None:
            visible = _intersection(visible, clip)
            viewport = _intersection(viewport, clip) if viewport else clip
        state["pending"][i] = (
            replace(
                plot,
                rect=rect,
                visible=visible,
                viewport=viewport,
                axes=_translate(plot.axes, dy, dx),
            )
            if visible
            else None
        )
