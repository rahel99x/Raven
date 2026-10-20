"""Cell-accurate metric crosshairs and bounded, source-scoped box zoom.

The terminal pointer uses thin Braille strokes in the theme accent. These paths only
inspect published graph geometry. They do not sample, scan files, copy series,
or request scheduler work. Zoom bounds are transient display state.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import replace
import math
import time

from .ui.geometry import (
    Plot as Plot,
    plot_state as initialize,
    translate_plot_rect as _translate,
    plot_rect as _rect,
    intersect_plot_rect as _intersection,
    take_plots,
    put_plots,
    map_plots,
)
from . import charts, layout as L, selector_glyphs as G
from .ui.geometry import Rect

from .ui.chart_state import (
    ZoomIdentity as ZoomIdentity,
    _display_scope as _display_scope,
    _zoom_identity as _zoom_identity,
    clear_source_zoom as clear_source_zoom,
    _key as _key,
    key as key,
    _state as _state,
    _context as _context,
    _native as _native,
    _capture_context as _capture_context,
    _blocked as _blocked,
    _at as _at,
    cancel as cancel,
)
from .pointer_routing import capture_handler

# Public compatibility aliases preserve class/function identity for adapters.
take_since, put_records, map_records = take_plots, put_plots, map_plots

MAX_PLOTS = 96
MAX_ZOOMS = 128
MAX_UNDO = 16
MAX_KEY_PARTS = 16
MAX_KEY_TEXT = 512
MAX_OVERLAY_CELLS = 4096
MAX_FEEDBACK_ROWS = 128
CAPTURE_TIMEOUT = 15.0
CAPTURE_MARGIN = 3
# Use the active theme's accent, rather than the deliberately bright pointer
# token used by controls. Dots, intersections and drag edges share this style.
CROSSHAIR_STYLE = "accent"


def _finite(value):
    try:
        return (
            isinstance(value, (float, int))
            and not isinstance(value, bool)
            and math.isfinite(value)
        )
    except (ValueError, TypeError, OverflowError):
        return False


def begin_frame(app, width=None, height=None):
    """Start one outer frame. Nested renderers must not clear this registry."""
    state = initialize(app)
    if state["capture"] and state["capture"]["context"] != _capture_context(
        app, state["capture"]["plot"].key
    ):
        cancel(app)
    state["pending"], state["plots"], state["hovered"] = [], (), None
    state["frame_context"] = _context(app)
    state["size"] = (width, height)
    views = getattr(app, "views_ref", None) or getattr(app, "views", None)
    state["ascii"] = bool(getattr(getattr(views, "g", None), "ascii", False))


def mark(app):
    """Remember a native renderer's first pending graph before laying it out."""
    return len(initialize(app)["pending"])


def _bounds(value):
    return (
        tuple(value)
        if isinstance(value, (tuple, list))
        and len(value) == 2
        and all(_finite(item) for item in value)
        and value[0] < value[1]
        else None
    )


def _inspector_range(app, identity):
    analysis = _state(app, "analysis_state")
    return (
        getattr(app, "mode", "main") == "analysis"
        and analysis.get("modal") == "chart"
        and analysis.get("chart_interaction_key") == identity
    )


def _cropped(app, identity, scale):
    """Only an existing display crop makes empty geometry a reset target."""
    value = initialize(app)["zoom"].get(_zoom_identity(identity, _display_scope(app)))
    if value is not None and value.get("scale") == scale:
        return True
    if captured_bounds(app, identity, scale=scale) is not None:
        return True
    from .metric_live import canonical, initialize as live_state

    if live_state(app)["entries"].get(canonical(identity), {}).get("enabled"):
        return True
    analysis = _state(app, "analysis_state")
    return bool(
        _inspector_range(app, identity)
        and (
            analysis.get("zoom", 1) != 1
            or analysis.get("pan", 0) != 0
            or analysis.get("window")
        )
    )


def record(app, identity, metadata, *, row=0, column=0, scale="linear", layer=0):
    """Stage a chart in local cells, with reset-only empty cropped areas.

    ``metadata`` comes from ``charts.braille_chart`` or ``vbar_chart``. Use
    ``place_since`` after clipping and embedding the native document in panes.
    Bounds on a logarithmic plot remain log10 coordinates throughout.
    """
    state = initialize(app)
    identity = _key(identity)
    if (
        identity is None
        or not isinstance(metadata, dict)
        or not metadata.get("valid", True)
        or scale not in ("linear", "log")
        or any(
            not isinstance(item, int) or isinstance(item, bool)
            for item in (row, column, layer)
        )
        or len(state["pending"]) >= MAX_PLOTS
    ):
        return None
    empty = metadata.get("has_data", True) is False
    if empty and not _cropped(app, identity, scale):
        return None
    rect, x_bounds, y_bounds = (
        _rect(metadata.get("plot_rect")),
        _bounds(metadata.get("x_bounds")),
        _bounds(metadata.get("y_bounds")),
    )
    if (
        not rect
        or not x_bounds
        or not y_bounds
        or rect.right - rect.left < 3
        or rect.bottom - rect.top < 2
    ):
        return None
    # The raster itself is bounded. Reject impossible geometry from embedders.
    if (
        rect.right - rect.left > charts.MAX_COLUMNS
        or rect.bottom - rect.top > charts.MAX_HEIGHT
    ):
        return None
    rect = Rect(
        rect.top + row, rect.left + column, rect.bottom + row, rect.right + column
    )
    axes = _rect(metadata.get("axis_rect"))
    if axes is not None and not (
        axes.top == rect.top - row
        and axes.left <= rect.left - column
        and axes.right == rect.right - column
        and rect.bottom - row <= axes.bottom <= rect.bottom - row + 3
        and 0 < axes.right - axes.left <= charts.MAX_COLUMNS + 128
    ):
        axes = None
    axes = _translate(axes, row, column)
    plot = Plot(
        identity,
        rect,
        rect,
        x_bounds,
        y_bounds,
        scale,
        layer,
        kind="metric-empty" if empty else "metric",
        axes=axes,
        context=_capture_context(app, identity),
        pane=_display_scope(app),
    )
    state["pending"].append(plot)
    return plot


from .ui.chart_registry import place_since as place_since


def publish(app, width=None, height=None):
    """Freeze only currently painted geometry after all layout transforms."""
    state = initialize(app)
    width = width if isinstance(width, int) else getattr(app, "width", None)
    height = height if isinstance(height, int) else getattr(app, "height", None)
    plots, live_controls = [], []
    if not _blocked(app):
        layer = 1 if getattr(app, "mode", "main") == "analysis" else 0
        screen = (
            Rect(0, 0, height, width)
            if isinstance(width, int) and isinstance(height, int)
            else None
        )
        for plot in state["pending"]:
            if plot is None or plot.layer != layer:
                continue
            visible = (
                _intersection(plot.visible, screen)
                if screen is not None
                else plot.visible
            )
            if visible:
                viewport = plot.viewport
                if screen is not None:
                    viewport = _intersection(viewport, screen) if viewport else screen
                painted = replace(plot, visible=visible, viewport=viewport)
                (live_controls if plot.kind == "live-controls" else plots).append(
                    painted
                )
    from .metric_live import publish as publish_live

    publish_live(app, live_controls)
    state["plots"] = tuple(plots)
    state["frame_context"] = _context(app)
    tick(app)
    if state["pointer"]:
        hover(app, *state["pointer"])
    return state["plots"]


def hover(app, y, x):
    """Cosmetic feedback only; no chart is rasterized for a pointer report."""
    state = initialize(app)
    if any(not isinstance(value, int) or isinstance(value, bool) for value in (y, x)):
        return False
    state["pointer"] = (y, x)
    plot = _at(app, y, x)
    state["hovered"] = plot.key if plot else None
    capture = state["capture"]
    visual_plot = capture["plot"] if capture else plot
    if visual_plot is not None:
        _track_pointer(app, visual_plot, y, x)
    else:
        state["visual"] = None
    return plot is not None


def _motion_enabled(app, ascii_=None):
    state = initialize(app)
    ascii_ = state["ascii"] if ascii_ is None else ascii_
    return bool(
        not ascii_
        and getattr(app, "theme", "default") != "reader"
        and getattr(app, "animations_enabled", True)
        and getattr(app, "cfg", {}).get("animations", True)
    )


def _track_pointer(app, plot, y, x, *, snap=False, now=None):
    """Follow the newest event cell immediately, easing only its dot phase."""
    state = initialize(app)
    y, x = _clamp_pointer(plot, y, x)
    target = (y + 0.5, x + 0.5)
    visual = state.get("visual")
    now = time.monotonic() if now is None else now
    same = bool(visual and visual["key"] == plot.key and visual["rect"] == plot.visible)
    if same and visual["target"] == target and not snap:
        return
    start = target
    if same and not snap and _motion_enabled(app):
        previous = visual["target"]
        # Start at the entry edge of this *new* cell. An arbitrary burst or
        # reversal never draws the head in an older event's terminal cell.
        start = (
            y
            + (
                0.125
                if target[0] > previous[0]
                else 0.875
                if target[0] < previous[0]
                else 0.5
            ),
            x
            + (
                0.25
                if target[1] > previous[1]
                else 0.75
                if target[1] < previous[1]
                else 0.5
            ),
        )
    state["visual"] = {
        "key": plot.key,
        "rect": plot.visible,
        "start": start or target,
        "target": target,
        "started": now,
    }


def _visual_pointer(app, plot, *, ascii_=None, now=None):
    state = initialize(app)
    visual = state.get("visual")
    raw = state["capture"]["current"] if state["capture"] else state["pointer"]
    if not raw:
        return None
    y, x = _clamp_pointer(plot, *raw)
    target = (y + 0.5, x + 0.5)
    if not _motion_enabled(app, ascii_):
        state["visual"] = None
        return G.locate(*target)
    if not visual or visual["key"] != plot.key or visual["rect"] != plot.visible:
        state["visual"] = None
        return G.locate(*target)
    now = time.monotonic() if now is None else now
    position = (
        G.interpolate(
            visual["start"],
            visual["target"],
            now - visual["started"],
            duration=G.SUBCELL_DURATION,
        )
        or target
    )
    # Keep the visible row/column pinned to the exact latest event even if an
    # embedding adapter supplies stale visual state or a clock moves backwards.
    cell = G.locate(*position)
    return G.Cell(y, x, cell.y_slot, cell.x_slot) if cell else G.locate(*target)


def next_deadline(app, now=None):
    """Schedule brief cached-overlay animation without rebuilding the graph."""
    state = initialize(app)
    visual = state.get("visual")
    if not visual:
        return float("inf")
    if not _motion_enabled(app) or _blocked(app):
        state["visual"] = None
        return float("inf")
    capture = state["capture"]
    plot = (
        capture["plot"]
        if capture
        else _at(app, *state["pointer"])
        if state["pointer"]
        else None
    )
    if capture and (
        capture["context"] != _capture_context(app, plot.key)
        or not any(_same_plot(current, plot) for current in state["plots"])
    ):
        state["visual"] = None
        return float("inf")
    if plot is None or plot.key != visual["key"] or plot.visible != visual["rect"]:
        state["visual"] = None
        return float("inf")
    if visual["start"] == visual["target"]:
        return float("inf")
    now = time.monotonic() if now is None else now
    end = visual["started"] + G.SUBCELL_DURATION
    return min(end, now + 1 / 60) if now < end else float("inf")


def active(app):
    return initialize(app)["capture"] is not None


def _same_plot(current, original):
    return (
        current is not None
        and current.key == original.key
        and current.rect == original.rect
        and current.visible == original.visible
        and current.scale == original.scale
        and current.x_bounds == original.x_bounds
        and current.y_bounds == original.y_bounds
        and (
            current.kind == original.kind
            or {current.kind, original.kind} <= {"metric", "metric-empty"}
        )
        and current.viewport == original.viewport
        and current.axes == original.axes
        and current.context == original.context
        and current.pane == original.pane
    )


def capture_bounds(plot):
    """The active gesture's small tolerance, constrained by its real viewport.

    This does not expand hover, initial hit testing or right-click targets.
    Overlapping buffers never switch the source captured by the initial press.
    """
    axis = plot.axes or plot.visible
    axis = _intersection(axis, plot.viewport) if plot.viewport else axis
    if axis is None:
        return None
    expanded = Rect(
        axis.top - CAPTURE_MARGIN,
        axis.left - CAPTURE_MARGIN,
        axis.bottom + CAPTURE_MARGIN,
        axis.right + CAPTURE_MARGIN,
    )
    return _intersection(expanded, plot.viewport) if plot.viewport else expanded


def _clamp_pointer(plot, y, x):
    return (
        min(plot.visible.bottom - 1, max(plot.visible.top, y)),
        min(plot.visible.right - 1, max(plot.visible.left, x)),
    )


def tick(app, now=None):
    state = initialize(app)
    capture = state["capture"]
    if capture:
        current = next(
            (plot for plot in state["plots"] if _same_plot(plot, capture["plot"])), None
        )
        now = time.monotonic() if now is None else now
        if (
            _blocked(app)
            or capture["context"] != _capture_context(app, capture["plot"].key)
            or not _same_plot(current, capture["plot"])
            or now - capture["last"] > CAPTURE_TIMEOUT
        ):
            cancel(app)
    if (
        _blocked(app)
        or state["hovered"] is not None
        and state["pointer"]
        and _at(app, *state["pointer"]) is None
    ):
        state["hovered"] = None


def _say(app, message):
    callback = getattr(app, "say", None)
    if callable(callback):
        callback(message)


def _apply(app, plot, target):
    from .metric_live import stop_for_zoom

    stop_for_zoom(app, plot.key)
    state = initialize(app)
    zooms = state["zoom"]
    # Legacy/headless callers construct Plot directly without published pane
    # metadata. Bind those to the caller's presentation; published plots keep
    # their original scope even when the root App receives the pointer event.
    pane = (
        plot.pane
        if plot.context is not None or plot.pane is not None
        else _display_scope(app)
    )
    identity = _zoom_identity(plot.key, pane)
    old = zooms.get(identity)
    undo = list(old.get("undo", ())) if old else []
    undo.append(
        {"x": old["x"], "y": old["y"], "fit_y": old.get("fit_y", False)}
        if old
        else None
    )
    zooms[identity] = {**target, "scale": plot.scale, "undo": undo[-MAX_UNDO:]}
    zooms.move_to_end(identity)
    while len(zooms) > MAX_ZOOMS:
        zooms.popitem(last=False)
    state["last_key"] = plot.key
    state["revision"] += 1
    _say(
        app,
        (
            "Chart time range zoomed; Y fits observed data"
            if target.get("fit_y")
            else "Chart area zoomed"
        )
        + "; u undoes, 0 resets while over this graph",
    )


@capture_handler("chart_interaction", cancel=cancel)
def handle_mouse(app, y, x, button="left", shift=False):
    state = initialize(app)
    previous = state["capture"]
    if any(not isinstance(value, int) or isinstance(value, bool) for value in (y, x)):
        cancel(app)
        return previous is not None
    tick(app)
    capture = state["capture"]
    if button == "release" and capture is None and state.get("cancelled_release"):
        state["cancelled_release"] = False
        return True
    if button == "drag" and capture is None and state.get("cancelled_release"):
        return True
    if (
        previous is not None
        and capture is None
        and button in ("motion", "drag", "release")
    ):
        return True
    if button in ("motion", "drag"):
        hover(app, y, x)
        if capture:
            margin = capture_bounds(capture["plot"])
            if margin is None or not margin.contains(y, x):
                cancel(app)
                return True
            capture["current"] = _clamp_pointer(capture["plot"], y, x)
            capture["last"] = time.monotonic()
            return True
        return False
    if capture:
        if button == "release":
            state["capture"] = None
            plot = capture["plot"]
            release_pointer = (y, x)
            start_y, start_x = capture["start"]
            margin = capture_bounds(plot)
            if margin is None or not margin.contains(y, x):
                return True
            y, x = _clamp_pointer(plot, y, x)
            if (
                abs(x - start_x) < 2
                or capture.get("shift", False)
                and abs(y - start_y) < 1
            ):
                return True
            left, right = sorted((start_x, x))
            top, bottom = sorted((start_y, y))
            # Terminal cells represent the endpoints of the actual plotted axes.
            xf = lambda value: (
                (value - plot.rect.left) / (plot.rect.right - plot.rect.left - 1)
            )
            yf = lambda value: (
                1 - (value - plot.rect.top) / (plot.rect.bottom - plot.rect.top - 1)
            )
            target = {
                "x": (
                    charts._between(*plot.x_bounds, xf(left)),
                    charts._between(*plot.x_bounds, xf(right)),
                ),
                "y": (
                    charts._between(*plot.y_bounds, yf(bottom)),
                    charts._between(*plot.y_bounds, yf(top)),
                ),
                "fit_y": not capture.get("shift", False),
            }
            # A horizontal gesture is a valid time range. Keep a finite fallback
            # Y transform; the next renderer fits the actual observed curve.
            if target["fit_y"] and not _bounds(target["y"]):
                target["y"] = plot.y_bounds
            if _bounds(target["x"]) and _bounds(target["y"]):
                _apply(app, plot, target)
            hover(app, *release_pointer)
            return True
        cancel(app)
        # A second press starts a fresh gesture; wheels pass to native handlers.
    if button in ("press", "left"):
        # A deliberate new press supersedes a cancelled gesture's late release,
        # including when it starts an ordinary row selection outside the plot.
        state["cancelled_release"] = False
    plot = _at(app, y, x, allow_empty=button == "right")
    if button == "right" and plot and shift:
        state["last_key"] = plot.key
        from .ui.owner_ports import charts as chart_actions

        chart_actions(app).open_export(app, chart=True)
        return True
    if button == "right" and plot:
        from .metric_live import stop_for_zoom, cancel as cancel_live
        from .pane_drag import cancel as cancel_pane

        cancel_live(app)
        cancel_pane(app)
        selection = getattr(app, "job_selection_state", None)
        if isinstance(selection, dict):
            selection["capture"] = None
        history = getattr(app, "history_browser_state", None)
        if isinstance(history, dict):
            history["drag"] = None
        toolbar = getattr(app, "toolbar_state", None)
        if isinstance(toolbar, dict):
            toolbar.update(dragging=False, pressed=False, drag_width=None)
        stop_for_zoom(app, plot.key)
        reset(app, plot.key)
        if _inspector_range(app, plot.key):
            analysis = _state(app, "analysis_state")
            analysis.update(
                zoom=1.0,
                pan=0.0,
                cursor=0,
                preset="all",
                chart_box=None,
                chart_live_window=None,
            )
            analysis.pop("window", None)
            analysis.pop("chart_visible", None)
        state["last_key"] = plot.key
        hover(app, y, x)
        _say(app, "Chart reset to its full default view")
        # Even an already-full graph owns its right-click. Jobs selection and
        # underlying controls must not receive this same pointer action.
        return True
    if button in ("press", "left") and plot:
        state["last_key"] = plot.key
        if button == "press":
            state["cancelled_release"] = False
            state["capture"] = {
                "plot": plot,
                "context": _capture_context(app, plot.key),
                "start": (y, x),
                "current": (y, x),
                "last": time.monotonic(),
                "shift": bool(shift),
            }
        hover(app, y, x)
        if button == "press":
            _track_pointer(app, plot, y, x, snap=True)
        return True
    return False


def bounds(app, identity, *, scale=None):
    """Get source-specific coordinates without consulting source data."""
    state = initialize(app)
    identity = _key(identity)
    zoom = (
        state["zoom"].get(_zoom_identity(identity, _display_scope(app)))
        if identity
        else None
    )
    if zoom is None or scale is not None and zoom["scale"] != scale:
        return None
    return {"x": zoom["x"], "y": zoom["y"]}


def captured_bounds(app, identity, *, scale=None):
    """Hold painted axes during a drag without creating zoom notes or source work."""
    capture = initialize(app)["capture"]
    identity = _key(identity)
    if (
        not capture
        or identity is None
        or _blocked(app)
        or capture["plot"].key != identity
        or capture["plot"].pane != _display_scope(app)
        or scale is not None
        and capture["plot"].scale != scale
        or capture["context"] != _capture_context(app, identity)
    ):
        return None
    return {"x": capture["plot"].x_bounds, "y": capture["plot"].y_bounds}


def autofit(app, identity, *, scale=None):
    """Whether a time selection should fit the complete observed Y range.

    This is display state only. The renderer computes the actual fit from its
    already published samples, so input handling never reads or scans a source.
    ``bounds`` retains its historical X/Y mapping for explicit box zoom callers.
    """
    identity = _key(identity)
    value = (
        initialize(app)["zoom"].get(_zoom_identity(identity, _display_scope(app)))
        if identity
        else None
    )
    return bool(
        value and value.get("fit_y") and (scale is None or value["scale"] == scale)
    )


def undo(app, identity=None):
    state = initialize(app)
    identity = (
        _key(identity)
        if identity is not None
        else state["hovered"] or state["last_key"]
    )
    identity = _zoom_identity(identity, _display_scope(app))
    value = state["zoom"].get(identity)
    if not value:
        return False
    history = list(value.get("undo", ()))
    previous = history.pop() if history else None
    if previous is None:
        state["zoom"].pop(identity, None)
    else:
        state["zoom"][identity] = {**previous, "scale": value["scale"], "undo": history}
    state["revision"] += 1
    cancel(app)
    return True


def reset(app, identity=None):
    state = initialize(app)
    identity = (
        _key(identity)
        if identity is not None
        else state["hovered"] or state["last_key"]
    )
    identity = _zoom_identity(identity, _display_scope(app))
    if identity not in state["zoom"]:
        return False
    state["zoom"].pop(identity, None)
    state["revision"] += 1
    cancel(app)
    return True


def handle_key(app, key):
    state = initialize(app)
    if active(app):
        cancel(app)
        return key == "esc"
    if _blocked(app) or state["pointer"] is None or _at(app, *state["pointer"]) is None:
        return False
    if state["hovered"] is None:
        return False
    if key in ("u", "0"):
        (undo if key == "u" else reset)(app, state["hovered"])
        return True
    return False


def command_names():
    return ["chartzoom"]


def run_command(app, args):
    if not args or args[0] != "chartzoom":
        return False
    if len(args) != 2 or args[1] not in ("undo", "reset"):
        _say(app, "chartzoom undo|reset (last selected or hovered metric graph)")
    else:
        changed = (undo if args[1] == "undo" else reset)(app)
        _say(
            app,
            "Chart zoom "
            + ("updated" if changed else "is already at its original view"),
        )
    return True


def overlay(views, snap, app, width, height):
    return None


def _painted_cells(app, visible, y, layers):
    """Index already painted characters, with bounded reuse across reports.

    This reads terminal rows, never metric samples. Source row snapshots detect
    in-place edits too. Wide/combining annotations remain protected; a selector
    cannot split a character or turn its ink into invented measurement pixels.
    """
    state = initialize(app)
    cache = state.setdefault("ink_rows", OrderedDict())
    signature = tuple((offset, tuple(row)) for offset, row in layers)
    key = (y, visible.left, visible.right)
    cached = cache.get(key)
    if cached is not None and cached[0] == signature:
        cache.move_to_end(key)
        return cached[1]
    width = visible.right - visible.left
    cells = [None] * width
    for offset, row in signature:
        position, last = offset, None
        for text, style in row:
            if not isinstance(text, str):
                continue
            style = style if isinstance(style, str) else ""
            decorative = "chart-fill" in style.split("+")
            if position >= visible.right and (not text or L.vlen(text[0])):
                break
            if text.isascii():
                start = max(0, visible.left - position)
                end = min(len(text), visible.right - position)
                for index in range(start, max(start, end)):
                    glyph = text[index]
                    cell = position + index - visible.left
                    cells[cell] = (
                        glyph,
                        style,
                        not decorative and not glyph.isspace(),
                        1,
                    )
                    last = cell
                position += len(text)
                continue
            for glyph in text:
                size = L.vlen(glyph)
                if not size:
                    if last is not None and cells[last] is not None:
                        old = cells[last]
                        cells[last] = (old[0] + glyph, old[1], True, old[3])
                    continue
                if position >= visible.right:
                    break
                cell = position - visible.left
                if position + size > visible.left:
                    protected = size > 1 or (
                        not decorative and not glyph.isspace() and glyph != "⠀"
                    )
                    entry = (glyph, style, protected, size)
                    for column in range(max(0, cell), min(width, cell + size)):
                        cells[column] = entry
                    last = cell if 0 <= cell < width else None
                else:
                    last = None
                position += size
    result = tuple(cells)
    # A malformed embedding's enormous source row is not retained merely to
    # paint a small intersecting plot. Normal renderer rows fit the terminal.
    if (
        sum(
            len(text)
            for _, row in signature
            for text, _ in row
            if isinstance(text, str)
        )
        <= 16384
    ):
        cache[key] = (signature, result)
        cache.move_to_end(key)
        while len(cache) > MAX_FEEDBACK_ROWS:
            cache.popitem(last=False)
    return result


def _compact_feedback(cells):
    """Batch adjacent selector cells without changing their painted result.

    A terminal accepts a styled run as one operation. Wide annotations leave
    holes in the selector and must still split those runs; combining marks
    remain attached to the single cell that owns them. All input cells here
    have already passed the bounded plot and source-ink checks.
    """
    by_row = {}
    for y, x, row in cells:
        by_row.setdefault(y, []).append((x, row[0]))
    output = []
    for y, row_cells in by_row.items():
        start, previous, segments = None, None, []
        for x, (char, style) in sorted(row_cells):
            if previous is None or x != previous + 1:
                if segments:
                    output.append(
                        (y, start, [("".join(parts), ink) for parts, ink in segments])
                    )
                start, segments = x, []
            if segments and segments[-1][1] == style:
                segments[-1][0].append(char)
            else:
                segments.append(([char], style))
            previous = x
        if segments:
            output.append(
                (y, start, [("".join(parts), ink) for parts, ink in segments])
            )
    return output


def feedback(app, *, ascii_=None, rows=None, overlays=(), compact=False):
    """Follow the newest cell with thin strokes while retaining all plot ink.

    Terminals cannot alpha blend a glyph. When supplied, cached document rows
    and prior overlays provide actual glyphs, colours and background. A measured
    stroke or annotation has priority over selector ink; unchanged characters
    retain the curve's geometry and legend colour at intersections. Blank cells
    show the theme accent on their exact painted background. Cached character
    lookups are reused across reports; no graph is rasterized or source read.
    ``compact`` batches adjacent cells into row runs for terminal painting.
    """
    state = initialize(app)
    tick(app)
    capture = state["capture"]
    pointer = state["pointer"]
    plot = capture["plot"] if capture else _at(app, *pointer) if pointer else None
    if plot is None:
        state["visual"] = None
        return []
    ascii_ = state["ascii"] if ascii_ is None else ascii_
    ascii_ = bool(ascii_ or getattr(app, "theme", "default") == "reader")
    output, marks = [], {}
    painted, styles = {}, {}
    theme = getattr(app, "theme", "default")
    layers = {}
    if rows is not None:
        for y in range(plot.visible.top, min(plot.visible.bottom, len(rows))):
            layers[y] = [(0, rows[y])]
    for y, x, row in overlays:
        if plot.visible.top <= y < plot.visible.bottom:
            layers.setdefault(y, []).append((x, row))

    def cell_at(y, x):
        if not layers.get(y):
            return None
        if y not in painted:
            painted[y] = _painted_cells(app, plot.visible, y, layers.get(y, ()))
        return painted[y][x - plot.visible.left]

    def style_at(style):
        if style is None:
            return CROSSHAIR_STYLE
        if style not in styles:
            from .palette import cell_style, resolve

            resolved = resolve(cell_style(style, theme), theme)
            background = (
                resolved.foreground if "rev" in resolved.flags else resolved.background
            )
            styles[style] = (
                CROSSHAIR_STYLE
                + "+bg-raw:#"
                + "".join(f"{part:02x}" for part in background)
                if background is not None
                else CROSSHAIR_STYLE
            )
        return styles[style]

    def put(y, x, char):
        if (
            not plot.visible.contains(y, x)
            or (y, x) not in marks
            and len(marks) >= MAX_OVERLAY_CELLS
        ):
            return
        old = marks.get((y, x))
        if old is not None:
            if (
                not ascii_
                and 0x2800 <= ord(old) <= 0x28FF
                and 0x2800 <= ord(char) <= 0x28FF
            ):
                char = chr(0x2800 | (ord(old) - 0x2800) | (ord(char) - 0x2800))
            elif old == "+":
                char = old
        marks[(y, x)] = char

    cursor = _visual_pointer(app, plot, ascii_=ascii_)
    if cursor is None:
        return []
    if capture:
        sy, sx = capture["start"]
        start = G.locate(sy + 0.5, sx + 0.5)
        left, right = sorted((start.column, cursor.column))
        top, bottom = sorted((start.row, cursor.row))
        start_horizontal = G.glyph(
            start.y_slot, start.x_slot, horizontal=True, ascii_=ascii_
        )
        cursor_horizontal = G.glyph(
            cursor.y_slot, cursor.x_slot, horizontal=True, ascii_=ascii_
        )
        start_vertical = G.glyph(
            start.y_slot, start.x_slot, vertical=True, ascii_=ascii_
        )
        cursor_vertical = G.glyph(
            cursor.y_slot, cursor.x_slot, vertical=True, ascii_=ascii_
        )
        for x in range(left, right + 1):
            put(start.row, x, start_horizontal)
            put(cursor.row, x, cursor_horizontal)
        for y in range(top, bottom + 1):
            put(y, start.column, start_vertical)
            put(y, cursor.column, cursor_vertical)
        if ascii_:
            for y, x in {(top, left), (top, right), (bottom, left), (bottom, right)}:
                put(y, x, "+")
    elif pointer:
        horizontal = G.glyph(
            cursor.y_slot, cursor.x_slot, horizontal=True, ascii_=ascii_
        )
        vertical = G.glyph(cursor.y_slot, cursor.x_slot, vertical=True, ascii_=ascii_)
        for column in range(plot.visible.left, plot.visible.right):
            put(cursor.row, column, horizontal)
        for row in range(plot.visible.top, plot.visible.bottom):
            put(row, cursor.column, vertical)
        if ascii_:
            put(cursor.row, cursor.column, "+")
    for (y, x), char in marks.items():
        original = cell_at(y, x)
        if original is not None and original[2]:
            if original[3] == 1:
                output.append((y, x, [(original[0], original[1])]))
            # Wide annotations remain on the base canvas rather than being
            # duplicated or split by a one-cell overlay.
            continue
        output.append(
            (y, x, [(char, style_at(original[1] if original is not None else None))])
        )
    return _compact_feedback(output) if compact else output
