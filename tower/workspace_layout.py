"""Adaptive, independently scrollable workspace panels without terminal I/O.

The compact default delegates to the established tab renderer unchanged. A
custom workspace asks it for a bounded larger source canvas, partitions semantic
section headings, and fits that source into independent panels. All hit rows are
remapped from source rows; IDs never depend on visible line text.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
import copy
import math
import re
from typing import Callable, Optional

from . import layout as L

DENSITIES = ("comfortable", "compact", "focused")
PANELS = ("main", "details")
MAX_SOURCE_ROWS = 256
MAX_REFLOW_ROWS = 2048
MAX_LAYOUTS = 16
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,47}\Z")


@dataclass(frozen=True)
class Rect:
    x: int
    y: int
    width: int
    height: int


@dataclass
class LayoutState:
    density: str = "compact"
    focus: str = "main"
    maximized: bool = False
    ratio: int = 60
    named: dict = field(default_factory=dict)
    scroll: dict = field(default_factory=dict)
    sizes: dict = field(default_factory=dict)
    available: tuple = ("main",)
    selected: dict = field(default_factory=dict)
    overlay_top: int = 0
    overlay_count: int = 0
    overlay_page: int = 1
    interactive_panels: dict = field(default_factory=dict)
    reflow_cache: dict = field(default_factory=dict, repr=False)


def initialize(app) -> LayoutState:
    state = getattr(app, "layout_state", None)
    if not isinstance(state, LayoutState):
        state = LayoutState()
        cfg = getattr(app, "cfg", {})
        settings = cfg.get("workspace", {}) if hasattr(cfg, "get") else {}
        if isinstance(settings, dict):
            if settings.get("density") in DENSITIES:
                state.density = settings["density"]
            if isinstance(settings.get("split"), int) and not isinstance(settings["split"], bool):
                state.ratio = max(20, min(80, settings["split"]))
        app.layout_state = state
    return state


def _settings(state: LayoutState) -> dict:
    return dict(density=state.density, focus=state.focus, maximized=state.maximized, split=state.ratio)


def _apply(state: LayoutState, data: dict) -> None:
    if data.get("density") in DENSITIES:
        state.density = data["density"]
    if data.get("focus") in PANELS:
        state.focus = data["focus"]
    if isinstance(data.get("maximized"), bool):
        state.maximized = data["maximized"]
    if isinstance(data.get("split"), int) and not isinstance(data["split"], bool):
        state.ratio = max(20, min(80, data["split"]))


def restore(app, data) -> None:
    state = initialize(app)
    if not isinstance(data, dict) or data.get("version", 1) != 1:
        return
    _apply(state, data)
    named = data.get("named", {})
    if isinstance(named, dict):
        for name, value in list(named.items())[:MAX_LAYOUTS]:
            if isinstance(name, str) and _NAME.fullmatch(name) and isinstance(value, dict):
                clean = LayoutState()
                _apply(clean, value)
                state.named[name] = _settings(clean)
    offsets = data.get("scroll", {})
    if isinstance(offsets, dict):
        state.scroll = {key: max(0, min(MAX_SOURCE_ROWS, value))
                        for key, value in list(offsets.items())[:100]
                        if isinstance(key, str) and len(key) <= 100
                        and isinstance(value, int) and not isinstance(value, bool)}


def save(app) -> dict:
    state = initialize(app)
    return dict(version=1, **_settings(state), named=dict(state.named), scroll=dict(state.scroll))


def command_names() -> list[str]:
    return ["density", "focus", "maximize", "layout", "panel-scroll"]


def _say(app, message: str, failure: bool = False) -> None:
    method = getattr(app, "fail" if failure else "say", None)
    if callable(method):
        method(message)


def _persist(app) -> None:
    method = getattr(app, "save", None)
    if callable(method):
        method()


def _key(app, panel: Optional[str] = None) -> str:
    state = initialize(app)
    return str(getattr(app, "tab", "jobs")) + ":" + (panel or state.focus)


def _focus_main_content(app) -> None:
    """An explicit Main focus leaves nested button and browser navigation."""
    panel = getattr(app, "job_panel_state", None)
    if isinstance(panel, dict):
        panel["focus"] = ""
    interaction = getattr(app, "interaction_state", None)
    if isinstance(interaction, dict):
        interaction["active"], interaction["focused"] = False, None
        interaction["pending_focus"] = None
    browser = getattr(app, "history_browser_state", None)
    if isinstance(browser, dict):
        browser["focused"] = False
    from .pane_drag import blur
    blur(app)


def _scroll(app, action: str) -> None:
    state = initialize(app)
    key = _key(app)
    count, page = state.sizes.get(key, (0, 1))
    page = max(1, page)
    top = state.scroll.get(key, 0)
    if action == "home":
        top = 0
    elif action == "end":
        top = max(0, count - page)
    else:
        top += {"up": -1, "down": 1, "pgup": -page, "pgdn": page,
                "page-up": -page, "page-down": page}.get(action, 0)
    state.scroll[key] = max(0, min(max(0, count - page), top))


def handle_key(app, key: str) -> bool:
    if getattr(app, "mode", "main") == "layout":
        state = initialize(app)
        if key in ("esc", "q", "enter"):
            app.mode = "main"
        elif key in ("up", "down", "pgup", "pgdn", "home", "end"):
            page = max(1, state.overlay_page)
            limit = max(0, state.overlay_count - page)
            if key == "home":
                state.overlay_top = 0
            elif key == "end":
                state.overlay_top = limit
            else:
                state.overlay_top = max(0, min(limit, state.overlay_top + {"up": -1, "down": 1, "pgup": -page, "pgdn": page}[key]))
        return True
    if getattr(app, "mode", "main") != "main":
        return False
    state = initialize(app)
    if key in ("ctrl-w", "ctrl_w", "f6"):
        panels = state.available if len(state.available) > 1 else PANELS
        state.focus = panels[(panels.index(state.focus) + 1) % len(panels)] if state.focus in panels else panels[0]
        if state.focus == "main":
            _focus_main_content(app)
        _say(app, f"Focus: {state.focus}; arrows scroll Details, z maximizes")
        return True
    if key == "z" and getattr(app, "tab", "") != "log":
        state.maximized = not state.maximized
        _persist(app)
        _say(app, f"{state.focus.title()} {'maximized' if state.maximized else 'restored'}")
        return True
    if key == "esc" and state.maximized:
        state.maximized = False
        _persist(app)
        return True
    if (getattr(app, "tab", "") not in ("log", "research") and state.focus == "details" and
            "details" in state.available and key in ("up", "down", "pgup", "pgdn", "home", "end")):
        _scroll(app, key)
        return True
    if state.focus == "main" and enabled(app) and getattr(app, "tab", "") not in ("log", "research") and _key(app) in state.sizes:
        if not state.interactive_panels.get(_key(app), False) and key in ("up", "down", "pgup", "pgdn", "home", "end"):
            _scroll(app, key)
            return True
    return False


def run_command(app, args: list[str]) -> bool:
    if not args or args[0] not in command_names():
        return False
    state = initialize(app)
    command, rest = args[0], args[1:]
    if command == "density":
        if len(rest) == 1 and rest[0] in DENSITIES:
            state.density = rest[0]
            _say(app, "Density: " + state.density)
        else:
            _say(app, "density <comfortable|compact|focused>", True)
            return True
    elif command == "focus":
        if len(rest) > 1 or (rest and rest[0] not in (*PANELS, "next")):
            _say(app, "focus [main|details|next]", True)
            return True
        target = rest[0] if rest else "next"
        if target == "next":
            panels = state.available if len(state.available) > 1 else PANELS
            target = panels[(panels.index(state.focus) + 1) % len(panels)] if state.focus in panels else panels[0]
        state.focus = target
        if target == "main":
            _focus_main_content(app)
        _say(app, "Focus: " + target)
    elif command == "maximize":
        if len(rest) > 1 or (rest and rest[0] not in ("on", "off")):
            _say(app, "maximize [on|off]", True)
            return True
        state.maximized = not state.maximized if not rest else rest[0] == "on"
        _say(app, f"{state.focus.title()} {'maximized' if state.maximized else 'restored'}")
    elif command == "panel-scroll":
        if len(rest) != 1 or rest[0] not in ("up", "down", "page-up", "page-down", "home", "end"):
            _say(app, "panel-scroll <up|down|page-up|page-down|home|end>", True)
            return True
        _scroll(app, rest[0])
    elif command == "layout":
        if rest == ["list"] or not rest:
            app.mode = "layout"
            state.overlay_top = 0
            return True
        if len(rest) != 2 or rest[0] not in ("save", "load", "delete", "split"):
            _say(app, "layout <save|load|delete> <name> | layout split <20..80> | layout list", True)
            return True
        action, name = rest
        if action == "split":
            try:
                value = int(name)
            except ValueError:
                value = 0
            if not 20 <= value <= 80:
                _say(app, "layout split <20..80> (percentage for Main)", True)
                return True
            state.ratio = value
            if state.density == "compact":
                state.density = "comfortable"
        elif not _NAME.fullmatch(name):
            _say(app, "Layout names use 1-48 letters, numbers, dots, dashes, or underscores", True)
            return True
        elif action == "save":
            if name not in state.named and len(state.named) >= MAX_LAYOUTS:
                _say(app, f"At most {MAX_LAYOUTS} saved layouts; delete one first", True)
                return True
            state.named[name] = _settings(state)
        elif name not in state.named:
            _say(app, "No saved layout named " + name, True)
            return True
        elif action == "load":
            _apply(state, state.named[name])
        else:
            del state.named[name]
        _say(app, f"Layout {action}: {name}")
    _persist(app)
    return True


def enabled(app) -> bool:
    state = initialize(app)
    return state.density != "compact" or state.focus != "main" or state.maximized


def geometry(app, width: int, height: int, *, has_details: bool = True) -> dict[str, Rect]:
    """Fit independent panels, always respecting even a one-cell terminal."""
    state = initialize(app)
    width, height = max(0, int(width)), max(0, int(height))
    if not width or not height:
        return {}
    if not has_details:
        return {"main": Rect(0, 0, width, height)}
    native_jobs = (getattr(app, "tab", "") in ("jobs", "history") and
                   isinstance(getattr(app, "job_panel_state", None), dict))
    if state.maximized or state.density == "focused" or height < 8 or (width < 48 and not native_jobs):
        return {state.focus: Rect(0, 0, width, height)}
    if width >= 110:
        main_width = max(24, min(width - 25, (width - 1) * state.ratio // 100))
        return {"main": Rect(0, 0, main_width, height),
                "details": Rect(main_width + 1, 0, width - main_width - 1, height)}
    main_height = max(3, min(height - 4, (height - 1) * state.ratio // 100))
    return {"main": Rect(0, 0, width, main_height),
            "details": Rect(0, main_height + 1, width, height - main_height - 1)}


def _content_origin(app, width, height):
    browser = getattr(app, "history_browser_state", {})
    frame = browser.get("frame") if isinstance(browser, dict) else None
    rect = getattr(app, "history_browser_content_rect", None)
    if (isinstance(frame, dict) and rect is not None and frame.get("tab") == getattr(app, "tab", "") and
            frame.get("mode") == getattr(app, "mode", "main") == "main" and
            frame.get("geometry") == (getattr(app, "width", None), getattr(app, "height", None)) and
            getattr(rect, "width", None) == width and getattr(rect, "height", None) == height):
        return rect.x, rect.y
    return 0, getattr(app, "body_origin", 0)


def _main_geometry(app, rects):
    """Publish the queue's actual budget before its source rows are prepared."""
    state = initialize(app)
    rect = rects.get("main")
    width = max((value.x + value.width for value in rects.values()), default=0)
    height = max((value.y + value.height for value in rects.values()), default=0)
    origin_x, origin_y = _content_origin(app, width, height)
    app.workspace_main_rect = (Rect(rect.x + origin_x, rect.y + origin_y, rect.width, rect.height)
                               if rect else None)
    if rect is None:
        app.workspace_main_usable_height = 0
        return
    native_jobs = (getattr(app, "tab", "") in ("jobs", "history") and
                   isinstance(getattr(app, "job_panel_state", None), dict))
    padding = 1 if (state.density == "comfortable" and rect.width >= 8 and rect.height >= 5
                    and not (native_jobs and rect.height < 7)) else 0
    app.workspace_main_usable_height = max(0, rect.height - 1 - padding * 2)


def _divider(app, rects, width, height):
    """Register the actual reserved gap, preserving exact panel hit geometry."""
    if len(rects) != 2 or "main" not in rects or "details" not in rects:
        return None
    from . import pane_drag
    state = initialize(app)
    main, details = rects["main"], rects["details"]
    origin_x, origin_y = _content_origin(app, width, height)
    key = "workspace:" + str(getattr(app, "tab", "jobs"))
    if details.x > main.x:
        extent = max(1, width - 1)
        minimum = max(20, math.ceil(24 * 100 / extent))
        maximum = min(80, (width - 25) * 100 // extent)
        values = ("vertical", origin_x + main.x + main.width, origin_y, 1, height, origin_x, extent)
    else:
        extent = max(1, height - 1)
        minimum = max(20, math.ceil(3 * 100 / extent))
        maximum = min(80, (height - 4) * 100 // extent)
        values = ("horizontal", origin_x, origin_y + main.y + main.height, width, 1, origin_y, extent)
    if minimum > maximum:
        return None
    return pane_drag.register(app, key, *values, state.ratio, lambda value: setattr(state, "ratio", value),
                              minimum=minimum, maximum=maximum, full_vertical=values[0] == "vertical",
                              label="Resize Main and Details")


def _section_title(row: L.Row) -> Optional[str]:
    text = L.row_text(row).lstrip()
    if not text.startswith(("──", "--")):
        return None
    title = text.lstrip("─- ").rstrip("─- ").strip()
    return title.lower() if title else None


def partition(body, hits, tab: str = "jobs", *, chart_records=(), scroll_records=()) -> dict:
    """Separate supporting sections while retaining their source hit records."""
    groups = {name: {"rows": [], "hits": []} for name in PANELS}
    panel = "main"
    source_map = {}
    from .interaction import ROW_KINDS
    data_rows = {y for y, kind, _ in hits if kind in ROW_KINDS}
    recent_separators = {y - 1 for y, kind, value in hits if kind == "sort_header"
                         and isinstance(value, tuple) and value[0] == "recent"}
    recent_rows = [y for y, kind, value in hits if kind == "recent"]
    if recent_rows:
        recent_separators.add(min(recent_rows) - 2)
    limit = MAX_SOURCE_ROWS + MAX_REFLOW_ROWS if tab == "jobs" else MAX_SOURCE_ROWS
    for y, row in enumerate(body[:limit]):
        title = None if y in data_rows else _section_title(row)
        # Inline Research/Analytics headings belong to Details even when they
        # say "history" or "dependencies". Only the queue's outer separator
        # changes which workspace owns a row.
        if title and not (tab == "jobs" and panel == "details" and y not in recent_separators):
            if title.startswith(("selected", "details", "resources", "evidence", "advice", "events", "recent events")):
                panel = "details"
            elif title.startswith(("jobs", "recent", "history", "nodes", "dependencies", "sources")):
                panel = "main"
            elif panel == "main" and y > 2 and not groups["details"]["rows"] and tab not in ("log", "research"):
                panel = "details"
        source_map[y] = (panel, len(groups[panel]["rows"]))
        groups[panel]["rows"].append(list(row))
    for y, kind, key in hits:
        if y in source_map:
            panel, index = source_map[y]
            groups[panel]["hits"].append((index, kind, key))
    if chart_records:
        from .chart_interaction import map_records
        for panel, group in groups.items():
            mapping = {y: index for y, (target, index) in source_map.items() if target == panel}
            group["charts"] = map_records(chart_records, mapping)
    if scroll_records:
        from .scrollbars import map_records
        for panel, group in groups.items():
            mapping = {y: index for y, (target, index) in source_map.items() if target == panel}
            group["scrollbars"] = map_records(scroll_records, row_map=mapping)
    return groups


@lru_cache(maxsize=4096)
def _surface_style(style):
    resolved = style.replace("rev", "sel") if "rev" in style.split("+") else style
    return resolved, "sel" in resolved.split("+")


def _style_row(row, width: int) -> L.Row:
    # Existing reverse-video rows become the same deliberate selected-row style
    # in each workspace. Cell colours remain meaningful on all other rows.
    out, selected = [], False
    for text, style in row:
        resolved, active = (_surface_style(style) if len(style) <= 512
                            else _surface_style.__wrapped__(style))
        out.append((text, resolved))
        selected |= active
    return L.fill_row(out, width, "text+bg:surface" + ("+sel" if selected else ""))


def _join_characters(characters) -> L.Row:
    segments = []
    text, style = [], None
    for character, character_style in characters:
        if style is not None and character_style != style:
            segments.append(("".join(text), style))
            text = []
        text.append(character)
        style = character_style
    if text:
        segments.append(("".join(text), style or ""))
    return segments


def _wrap_row(row: L.Row, width: int) -> list[L.Row]:
    """Wrap metadata at spaces while preserving its exact semantic cell styles."""
    if width <= 0:
        return [[]]
    if L.vlen(L.row_text(row)) <= width:
        return [list(row)]
    lines, current, used, last_space = [], [], 0, -1
    for text, style in row:
        for character in text[:131072]:
            cell_width = 1 if character.isascii() else L.vlen(character)
            if cell_width > width:
                character, cell_width = "?", 1
            if character == "\n":
                lines.append(_join_characters(current))
                current, used, last_space = [], 0, -1
                continue
            if used + cell_width > width:
                if last_space >= 0:
                    lines.append(_join_characters(current[:last_space]))
                    current = current[last_space + 1:]
                else:
                    lines.append(_join_characters(current))
                    current = []
                used = sum(L.vlen(char) for char, _ in current)
                last_space = max((n for n, (char, _) in enumerate(current) if char.isspace()), default=-1)
            current.append((character, style))
            used += cell_width
            if character.isspace():
                last_space = len(current) - 1
            if len(lines) >= MAX_REFLOW_ROWS:
                return lines
    if current or not lines:
        lines.append(_join_characters(current))
    return lines


def _reflow(rows, hits, width: int, mapping=None):
    interactive = {y for y, kind, _ in hits if kind != "job_panel_action"}
    mapped, ends, result = {}, {}, []
    for y, row in enumerate(rows):
        mapped[y] = len(result)
        if y in interactive or _section_title(row) is not None or any("┌" in text or "└" in text or "│" in text for text, _ in row):
            result.append(L.clip_row(row, width))
        else:
            result.extend(_wrap_row(row, width))
        ends[y] = len(result)
        if len(result) >= MAX_REFLOW_ROWS:
            result = result[:MAX_REFLOW_ROWS]
            break
    if mapping is not None:
        mapping.update(mapped)
    remapped = []
    for y, kind, value in hits:
        if y in mapped and mapped[y] < len(result):
            indices = range(mapped[y], min(ends[y], len(result))) if kind == "job_panel_action" else (mapped[y],)
            remapped.extend((index, kind, value) for index in indices)
    return result, remapped


def _cached_reflow(state, panel, rows, hits, width, mapping):
    """Reuse wrapping across scroll frames without caching published data.

    Only the two current panels are retained. Immutable row text and a detached
    hit map participate in equality, so changed evidence, paths, styles, or
    column bounds invalidate even when a reader updates a result in place.
    """
    text = tuple(tuple(row) for row in rows)
    entry = state.reflow_cache.get(panel)
    if entry and entry["width"] == width and entry["input"] == text and entry["input_hits"] == hits:
        mapping.update(entry["mapping"])
        return [list(row) for row in entry["rows"]], list(entry["hits"])
    result, remapped = _reflow(rows, hits, width, mapping)
    try:
        detached_hits, detached_output = copy.deepcopy(hits), copy.deepcopy(remapped)
    except (TypeError, ValueError, RecursionError):
        # Third-party renderers may publish an opaque target that cannot be
        # copied. Their existing rendering contract remains available.
        state.reflow_cache.pop(panel, None)
    else:
        state.reflow_cache[panel] = dict(width=width, input=text, input_hits=detached_hits,
                                         rows=tuple(tuple(row) for row in result),
                                         hits=detached_output, mapping=dict(mapping))
    return result, remapped


def _preserve_titles(fitted, original) -> None:
    originals = {}
    from .interaction import ROW_KINDS
    original_data = {y for y, kind, _ in original["hits"] if kind in ROW_KINDS}
    fitted_data = {y for y, kind, _ in fitted["hits"] if kind in ROW_KINDS}
    for index, row in enumerate(original["rows"]):
        if index in original_data:
            continue
        title = _section_title(row)
        if title:
            originals.setdefault(tuple(title.split()[:2]), (title, row))
    for index, row in enumerate(fitted["rows"]):
        if index in fitted_data:
            continue
        title = _section_title(row)
        if not title:
            continue
        key = tuple(title.split()[:2])
        complete = originals.get(key)
        if complete and title != complete[0] and ("…" in title or "~" in title):
            # A narrow separator must not erase meaningful totals or states.
            fitted["rows"][index] = [(" " + complete[0], "heading+bold")]


def _preserve_metadata(fitted, original) -> None:
    """Prefer a longer source paragraph when a narrow view clipped it early."""
    def chart_rows(group):
        # Charts have already been laid out at the panel's real width. A
        # wide source canvas can put several titles side by side; restoring
        # that row into one narrow card wraps it and moves its frozen axes as
        # soon as the title changes (for example when retained samples expire).
        # Preserve the renderer's fitted title, plot and axis rows verbatim.
        count = len(group["rows"])
        return {row for plot in group.get("charts", ())
                for row in range(max(0, plot.rect.top - 1),
                                 min(count, (plot.axes or plot.rect).bottom))}

    originals = {}
    interactive = {y for y, _, _ in original["hits"]} | chart_rows(original)
    for index, row in enumerate(original["rows"]):
        text = L.row_text(row).strip()
        if index not in interactive and text and _section_title(row) is None:
            originals.setdefault(text.split()[0], []).append(row)
    occurrences = {}
    interactive = {y for y, _, _ in fitted["hits"]} | chart_rows(fitted)
    for index, row in enumerate(fitted["rows"]):
        text = L.row_text(row).strip()
        if index in interactive or not text or _section_title(row) is not None:
            continue
        prefix = text.split()[0]
        occurrence = occurrences.get(prefix, 0)
        occurrences[prefix] = occurrence + 1
        source = originals.get(prefix, [])
        if ("…" in text or "~" in text) and occurrence < len(source) and L.vlen(L.row_text(source[occurrence])) > L.vlen(text):
            fitted["rows"][index] = list(source[occurrence])


def _virtual_details(app, state, rect, group, document, glyphs, padding):
    """Use a measured card index without expanding the complete Details source."""
    from . import scrollbars, scrolling
    from .job_panels import _view_key
    inline = getattr(app, "job_panel_state", {})
    key = _key(app, "details")
    width = max(1, rect.width - padding * 2 - 1)
    fixed, hits = _reflow(group["rows"], group["hits"], width)
    capacity = max(0, rect.height - 1 - padding * 2)
    fixed = fixed[:max(0, capacity - 1)]
    page = max(0, capacity - len(fixed))
    context = (getattr(app, "selected_id", None), rect.width, page, inline.get("mode"),
               inline.get("research_view"), inline.get("analytics_view"))
    target = max(0, min(max(0, document.count - page), state.scroll.get(key, 0)))
    painted = scrolling.viewport(app, "workspace:" + key, target, document.count, page, context=context)
    visible = document.window(painted, page)
    state.scroll[key], state.sizes[key] = target, (document.count, page)
    state.selected[key], state.interactive_panels[key] = getattr(app, "selected_id", None), False
    inline["scrolls"][_view_key(inline)] = target
    scrollable = document.count > page and page > 0
    title_width = max(0, rect.width - 4) if scrollable and rect.width >= 6 else rect.width
    rows = [([("    ", "")] if title_width != rect.width else []) +
            L.panel_title(glyphs, "Details", title_width, state.focus == "details",
                          (painted, min(document.count, painted + page), document.count))]
    if padding:
        rows.append(L.fill_row([], rect.width))
    rows.extend(_style_row([(" " * padding, "")] + row, rect.width) for row in fixed + visible)
    rows.extend(L.fill_row([], rect.width) for _ in range(max(0, rect.height - len(rows))))
    mapped_hits = []
    for y, kind, value in hits:
        if y >= len(fixed):
            continue
        if kind in ("job_panel_tab", "job_panel_view", "job_panel_file", "job_panel_action"):
            target_value, left, right = value
            value = (target_value, rect.x + padding + left, rect.x + padding + min(right, width))
        elif kind == "control" and isinstance(value, dict):
            value = {**value, "left": rect.x + padding + value["left"],
                     "right": rect.x + padding + min(width, value["right"])}
        mapped_hits.append((rect.y + 1 + padding + y, kind, value))
    def seek(value):
        state.scroll[key] = value
        inline["scrolls"][_view_key(inline)] = value
    if page and rect.width >= 2:
        scrollbars.register(app, "workspace:" + key,
            (rect.y + 1 + padding + len(fixed), rect.x + padding,
             rect.y + rect.height - padding, rect.x + rect.width - padding),
            document.count, page, target, painted, seek, context=context,
            header=(rect.y, rect.x, rect.x + rect.width) if scrollable and rect.width >= 6 else None)
    return rows, mapped_hits


def transform_body(app, body, hits, width: int, height: int, *, ascii_: bool = False, groups=None):
    state = initialize(app)
    native_jobs = (getattr(app, "tab", "") in ("jobs", "history") and
                   isinstance(getattr(app, "job_panel_state", None), dict))
    width, height = max(0, width), max(0, height)
    groups = partition(body, hits, getattr(app, "tab", "jobs")) if groups is None else groups
    has_details = bool(groups["details"]["rows"])
    state.available = PANELS if has_details else ("main",)
    if state.focus not in state.available:
        state.focus = "main"
    rects = geometry(app, width, height, has_details=has_details)
    _main_geometry(app, rects)
    canvas = [[(" " * width, "text+bg:canvas")] for _ in range(height)]
    # Keep columns as style segments, rather than individual character cells.
    rendered, output_hits = {}, []
    g = L.Glyphs(ascii_)
    for panel, rect in rects.items():
        from . import terminal_ui
        if panel == "details" and terminal_ui.showing(app):
            origin_x, origin_y = _content_origin(app, width, height)
            rendered[panel], terminal_hits = terminal_ui.render(app, rect.width, rect.height,
                x=origin_x + rect.x, y=origin_y + rect.y)
            output_hits.extend((y + rect.y, kind,
                {**value, "left": value["left"] + rect.x, "right": value["right"] + rect.x})
                for y, kind, value in terminal_hits)
            continue
        source, source_hits = groups[panel]["rows"], groups[panel]["hits"]
        key = _key(app, panel)
        padding = 1 if (state.density == "comfortable" and rect.width >= 8 and rect.height >= 5
                        and not (native_jobs and panel == "main" and rect.height < 7)) else 0
        if panel == "details" and groups[panel].get("virtual_document") is not None:
            rendered[panel], mapped = _virtual_details(app, state, rect, groups[panel],
                groups[panel]["virtual_document"], g, padding)
            output_hits.extend(mapped)
            continue
        raw_mapping = {}
        source, source_hits = _cached_reflow(state, panel, source, source_hits,
                                            max(0, rect.width - padding * 2), raw_mapping)
        chart_mapping = dict(raw_mapping)
        scroll_mapping, scroll_sticky = dict(raw_mapping), {}
        # Column headers are buttons, not selectable data rows. Header-only
        # tables must keep their normal panel-scrolling controls.
        drill_buttons = {"sort_header", "node_row", "node_cell", "partition_row", "user_drill", "control", "job_panel_tab", "job_panel_view", "job_panel_file", "job_panel_action"}
        data_hits = [(y, kind, value) for y, kind, value in source_hits if kind not in drill_buttons]
        state.interactive_panels[key] = bool(data_hits)
        page = max(0, rect.height - 1 - padding * 2)
        sticky = []
        sticky_hits = []
        if panel == "details" and page >= 1:
            # Details mode buttons remain reachable while long inspections or
            # evidence scroll. Their horizontal hit bounds survive resizing.
            indices = sorted({y for y, kind, _ in source_hits if kind in ("job_panel_tab", "job_panel_view")})
            # Leave at least one content row whenever the pane can show two.
            indices = indices[:max(1, page - 1)]
            if indices:
                sticky = [source[index] for index in indices]
                sticky_map = {index: position for position, index in enumerate(indices)}
                sticky_hits = [(sticky_map[y], kind, value) for y, kind, value in source_hits
                               if kind in ("job_panel_tab", "job_panel_view") and y in sticky_map]
                removed = set(indices)
                remap, remaining = {}, []
                for index, row in enumerate(source):
                    if index not in removed:
                        remap[index] = len(remaining)
                        remaining.append(row)
                source = remaining
                source_hits = [(remap[y], kind, value) for y, kind, value in source_hits if y in remap]
                chart_mapping = {raw: remap[fitted] for raw, fitted in chart_mapping.items() if fitted in remap}
                scroll_sticky = {raw: sticky_map[fitted] for raw, fitted in scroll_mapping.items() if fitted in sticky_map}
                scroll_mapping = {raw: remap[fitted] for raw, fitted in scroll_mapping.items() if fitted in remap}
                page -= len(sticky)
        if panel == "main" and source_hits and page >= 2:
            headers = [y for y, kind, _ in source_hits if kind == "sort_header"]
            first = min(y for y, _, _ in data_hits) if data_hits else min(headers) + 1 if headers else 0
            # Keep the page's primary summary and table column names visible
            # while large resource charts and individual rows scroll beneath.
            indices = sorted({index for index in (0, first - 2, first - 1) if 0 <= index < first})
            indices = indices[:max(0, min(3, page - 1))]
            sticky = [source[index] for index in indices]
            sticky_map = {index: position for position, index in enumerate(indices)}
            sticky_hits = [(sticky_map[y], kind, value) for y, kind, value in source_hits
                           if kind in ("sort_header", "control") and y in sticky_map]
            removed = set(indices)
            remap, remaining = {}, []
            for index, row in enumerate(source):
                if index not in removed:
                    remap[index] = len(remaining)
                    remaining.append(row)
            source = remaining
            source_hits = [(remap[y], kind, value) for y, kind, value in source_hits if y in remap]
            chart_mapping = {raw: remap[fitted] for raw, fitted in chart_mapping.items() if fitted in remap}
            scroll_sticky = {raw: sticky_map[fitted] for raw, fitted in scroll_mapping.items() if fitted in sticky_map}
            scroll_mapping = {raw: remap[fitted] for raw, fitted in scroll_mapping.items() if fitted in remap}
            page -= len(sticky)
        state.sizes[key] = (len(source), page)
        top = state.scroll.get(key, 0)
        # A changing selected row stays visible; explicit panel scrolling remains
        # respected while the actual job identity has not changed.
        selected = getattr(app, "selected_id", None)
        if panel == "details" and selected != state.selected.get(key):
            top = 0
            state.selected[key] = selected
        active_hit = next(((y, kind, value) for y, kind, value in source_hits if kind not in drill_buttons and value == selected), None)
        if active_hit is None or getattr(app, "tab", "jobs") not in ("jobs", "history"):
            active_hit = next(((y, kind, value) for y, kind, value in source_hits
                               if kind not in drill_buttons and any("rev" in style.split("+") or "sel" in style.split("+") for _, style in source[y])), None)
        selected_key = active_hit[1:] if active_hit else selected
        if panel == "main" and selected_key and state.selected.get(key) != selected_key:
            row = active_hit[0] if active_hit else None
            if row is not None:
                if row < top:
                    top = row
                elif row >= top + max(1, page):
                    top = row - max(1, page) + 1
            state.selected[key] = selected_key
        logical_top = max(0, min(max(0, len(source) - page), top))
        native_table = panel == "main" and bool(groups[panel].get("scrollbars"))
        if native_table:
            logical_top = 0
        from .scrolling import viewport
        inline = getattr(app, "job_panel_state", {})
        context = (getattr(app, "selected_id", None), rect.width, page,
                   inline.get("mode"), inline.get("research_view"), inline.get("analytics_view"))
        painted_top = viewport(app, "workspace:" + key, logical_top, len(source), page, context=context)
        visible, mapped_hits, top = L.scroll_window(source, source_hits, max(0, rect.width - padding * 2), page, painted_top)
        if groups[panel].get("charts"):
            from .chart_interaction import map_records, put_records
            mapping = {raw: fitted - top for raw, fitted in chart_mapping.items() if top <= fitted < top + page}
            put_records(app, map_records(groups[panel]["charts"], mapping,
                dy=rect.y + 1 + padding + len(sticky), dx=rect.x + padding,
                clip=(rect.y + 1 + padding + len(sticky), rect.x + padding,
                      rect.y + rect.height - padding, rect.x + rect.width - padding)))
        if groups[panel].get("scrollbars"):
            from . import scrollbars
            mapping = {raw: fitted - top + len(sticky) for raw, fitted in scroll_mapping.items()
                       if top <= fitted < top + page}
            mapping.update(scroll_sticky)
            scrollbars.put_records(app, scrollbars.map_records(groups[panel]["scrollbars"], row_map=mapping,
                dy=rect.y + 1 + padding, dx=rect.x + padding,
                clip=(rect.y + 1 + padding, rect.x + padding,
                      rect.y + rect.height - padding, rect.x + rect.width - padding)))
        state.scroll[key] = logical_top
        if native_jobs and panel == "details":
            from .job_panels import _view_key
            view_key = _view_key(inline)
            inline["scrolls"][view_key] = logical_top
            if (inline.get("mode") == "research" or
                    inline.get("mode") == "analytics" and inline.get("analytics_view") == "job"):
                usable_width = max(0, rect.width - padding * 2 - 1)
                header = inline["document_headers"].get((view_key, usable_width), 0)
                document_key = (view_key, usable_width)
                inline["document_maps"][document_key] = {
                    "mapping": raw_mapping, "header": header, "sticky": len(sticky)}
                for cache_name in ("document_maps", "document_windows", "document_headers"):
                    while len(inline[cache_name]) > 24:
                        inline[cache_name].pop(next(iter(inline[cache_name])))
                # Map the next visible reflowed window back to raw document
                # rows. Metric charts outside it reserve blank rows only.
                lower, upper = top + len(sticky), top + len(sticky) + page
                candidates = [raw for raw, fitted in raw_mapping.items()
                              if lower - 16 <= fitted <= upper + 16]
                if candidates:
                    inline["document_windows"][(view_key, usable_width)] = (
                        max(0, min(candidates) - header - 16),
                        max(0, max(candidates) - header + 16))
        title = panel.title() + ("  [z restore]" if state.maximized else "")
        scrollable = not native_table and not (native_jobs and panel == "main") and len(source) > page and page > 0
        title_width = max(0, rect.width - 4) if scrollable and rect.width >= 6 else rect.width
        rows = [([("    ", "")] if title_width != rect.width else []) +
                L.panel_title(g, title, title_width, state.focus == panel, (top, min(len(source), top + page), len(source)))]
        if not native_table and not (native_jobs and panel == "main") and page > 0 and rect.width >= 2:
            from . import scrollbars
            def seek(value, target_key=key):
                state.scroll[target_key] = value
            scrollbars.register(app, "workspace:" + key,
                (rect.y + 1 + padding + len(sticky), rect.x + padding,
                 rect.y + rect.height - padding, rect.x + rect.width - padding),
                len(source), page, logical_top, top, seek, context=context,
                header=(rect.y, rect.x, rect.x + rect.width) if scrollable and rect.width >= 6 else None)
        if padding:
            rows.append(L.fill_row([], rect.width))
        rows.extend(_style_row([(" " * padding, "")] + row, rect.width) for row in sticky)
        for row in visible:
            rows.append(_style_row([(" " * padding, "")] + row, rect.width))
        rows.extend(L.fill_row([], rect.width) for _ in range(max(0, rect.height - len(rows))))
        rendered[panel] = rows
        def add_hit(y, kind, value):
            if kind == "sort_header":
                tab, column, left, right = value
                usable_width = max(0, rect.width - padding * 2)
                right = min(right, usable_width)
                if left >= right:
                    return
                value = (tab, column, rect.x + padding + left, rect.x + padding + right)
            elif kind == "control" and isinstance(value, dict):
                left = value.get("left", 0)
                right = min(value.get("right", 0), max(0, rect.width - padding * 2))
                if left >= right:
                    return
                value = {**value, "left": rect.x + padding + left,
                         "right": rect.x + padding + right}
            elif kind in ("node_cell", "job_panel_tab", "job_panel_view", "job_panel_file", "job_panel_action"):
                name, left, right = value
                right = min(right, max(0, rect.width - padding * 2))
                if left >= right:
                    return
                value = (name, rect.x + padding + left, rect.x + padding + right)
            output_hits.append((y, kind, value))
        for y, kind, value in sticky_hits:
            add_hit(rect.y + 1 + padding + y, kind, value)
        for y, kind, value in mapped_hits:
            add_hit(rect.y + 1 + padding + len(sticky) + y, kind, value)
    from . import terminal_ui
    if terminal_ui.enabled(app) and not terminal_ui.showing(app) and "details" in rendered:
        rect = rects["details"]
        origin_x, origin_y = _content_origin(app, width, height)
        offset = 4 if rect.width >= 28 else 0
        host, host_hits = terminal_ui.header(app, max(0, rect.width - offset),
            x=origin_x + rect.x + offset, y=origin_y + rect.y)
        previous = rendered["details"][0]
        rendered["details"][0] = terminal_ui._slice_row(previous, 0, offset) + host
        initialize_terminal = terminal_ui.initialize(app)
        initialize_terminal.geometry = (getattr(app, "width", None), getattr(app, "height", None))
        output_hits.extend((y + rect.y, kind,
            {**value, "left": value["left"] + rect.x + offset, "right": value["right"] + rect.x + offset})
            for y, kind, value in host_hits)
    for y in range(height):
        spans = []
        for panel, rect in rects.items():
            if rect.y <= y < rect.y + rect.height:
                spans.append((rect.x, rect.width, rendered[panel][y - rect.y]))
        row, x = [], 0
        for start, span_width, segments in sorted(spans):
            if start > x:
                row.append((" " * (start - x), "bg:canvas"))
            row.extend(segments)
            x = start + span_width
        if x < width:
            row.append((" " * (width - x), "bg:canvas"))
        canvas[y] = L.clip_row(row, width)
    divider = _divider(app, rects, width, height)
    if divider:
        from . import pane_drag
        origin_x, origin_y = _content_origin(app, width, height)
        pane_drag.paint(canvas, app, divider.key, origin_x=origin_x, origin_y=origin_y, ascii_=ascii_)
        hit = pane_drag.control_hit(app, divider.key, origin_y=origin_y)
        if hit and isinstance(getattr(app, "interaction_state", None), dict):
            if origin_x:
                y, kind, descriptor = hit
                hit = (y, kind, {**descriptor, "left": descriptor["left"] - origin_x,
                                 "right": descriptor["right"] - origin_x})
            output_hits.append(hit)
    # A scalar row-only hit cannot describe two selectable items in side-by-side
    # columns. Keep interactive table hits in Main; Details is scrollable text.
    if getattr(app, "tab", "") in ("jobs", "history"):
        from .job_panels import context as panel_context
        app.job_panel_context = panel_context(app)
        rect = rects.get("details")
        app.job_panel_rect = (Rect(rect.x, rect.y + getattr(app, "body_origin", 0), rect.width, rect.height)
                              if rect else None)
    return canvas, output_hits


def render_body(views, snap, app, width: int, height: Optional[int], actions,
                default_renderer: Callable[[int, Optional[int]], tuple]):
    from . import terminal_ui
    if terminal_ui.enabled(app) and getattr(app, "tab", "") not in ("jobs", "history"):
        return terminal_ui.wrap_workspace(app, width, height,
            lambda w, h: _render_body(views, snap, app, w, h, actions, default_renderer),
            ascii_=getattr(views.g, "ascii", False))
    return _render_body(views, snap, app, width, height, actions, default_renderer)


def _render_body(views, snap, app, width: int, height: Optional[int], actions,
                 default_renderer: Callable[[int, Optional[int]], tuple]):
    """Render callback ``(width, height)`` with bounded independent panel scroll."""
    if width <= 0 or (height is not None and height <= 0):
        return [], []
    if getattr(app, "tab", "") == "research" and height is not None:
        # Research already virtualizes metric cards and long analysis views.
        # A larger source canvas would rasterize charts outside the viewport.
        body, hits = default_renderer(width, height)
        return [_style_row(row, width) for row in body], hits
    if (getattr(app, "tab", "") in ("jobs", "history")
            and isinstance(getattr(app, "job_panel_state", None), dict)):
        from .job_panels import initialize as panel_state
        panel_state(app)
    native_jobs = (getattr(app, "tab", "") in ("jobs", "history") and height is not None and
                   isinstance(getattr(app, "job_panel_state", None), dict))
    if height is None or (not enabled(app) and not native_jobs) or getattr(app, "tab", "") == "log":
        return default_renderer(width, height)
    if native_jobs:
        # The default Jobs page uses its Details column without requiring a
        # density preference. Narrow screens stack the same independent panes.
        rects = geometry(app, width, height, has_details=True)
        _main_geometry(app, rects)
        detail_rect = rects.get("details")
        app.job_panel_target_height = max(1, detail_rect.height - 2) if detail_rect else max(1, height - 2)
        # Rendering the whole Jobs source at terminal, Main, and Details widths
        # rebuilt every chart and accounting aggregation three times. Resolve
        # queue selection once, then construct the independent document only
        # for the width where its rows will actually be displayed.
        state = initialize(app)
        def usable(rect):
            padding = 1 if (state.density == "comfortable" and rect.width >= 8 and rect.height >= 5
                            and not (rect is rects.get("main") and rect.height < 7)) else 0
            return max(0, rect.width - padding * 2)
        main_rect = rects.get("main")
        main_width = usable(main_rect) if main_rect else width
        previous_deferred = getattr(app, "job_panel_defer_content", False)
        previous_canvas = getattr(app, "job_panel_source_canvas", False)
        app.job_panel_defer_content = app.job_panel_source_canvas = True
        from . import chart_interaction
        chart_mark = chart_interaction.mark(app)
        from . import scrollbars
        scroll_mark = scrollbars.mark(app)
        try:
            main_height = (max(1, app.workspace_main_usable_height)
                           if getattr(app, "tab", "") == "history" else max(height, MAX_SOURCE_ROWS))
            body, hits = default_renderer(main_width, main_height)
        finally:
            app.job_panel_defer_content = previous_deferred
            app.job_panel_source_canvas = previous_canvas
        records = chart_interaction.take_since(app, chart_mark)
        scroll_records = scrollbars.take_since(app, scroll_mark)
        queue = partition(body, hits, getattr(app, "tab", "jobs"), chart_records=records, scroll_records=scroll_records)["main"]
        groups = {"main": queue, "details": {"rows": [], "hits": []}}
        from . import terminal_ui
        if detail_rect and terminal_ui.showing(app):
            groups["details"] = {"rows": [[("Terminal", "")]], "hits": []}
        elif detail_rect:
            from .job_panels import render as render_details
            job = app.job_record(getattr(app, "selected_id", None), snap)
            chart_mark = chart_interaction.mark(app)
            scroll_mark = scrollbars.mark(app)
            detail_width = max(0, usable(detail_rect) - (0 if getattr(app, "job_panel_state", {}).get("mode") == "logs" else 1))
            detail_rows, detail_hits = render_details(views, snap, app, job, detail_width,
                                                      app.job_panel_target_height)
            groups["details"] = {"rows": detail_rows, "hits": detail_hits,
                                 "charts": chart_interaction.take_since(app, chart_mark),
                                 "scrollbars": scrollbars.take_since(app, scroll_mark),
                                 "virtual_document": getattr(app, "job_panel_state", {}).get("virtual_document")}
        return transform_body(app, body, hits, width, height,
                              ascii_=getattr(views.g, "ascii", False), groups=groups)
    # Native tab renderers already limit their work to their requested height.
    # A larger bounded source keeps lower sections reachable rather than clipped.
    table_tab = getattr(app, "tab", "") in ("history", "group", "sources", "deps")
    source_height = height if table_tab else max(height, MAX_SOURCE_ROWS)
    previous_canvas = getattr(app, "job_panel_source_canvas", False)
    if native_jobs:
        app.job_panel_source_canvas = True
    from . import chart_interaction
    chart_mark = chart_interaction.mark(app)
    from . import scrollbars
    scroll_mark = scrollbars.mark(app)
    previous_probe = getattr(app, "scrollbar_probe", False)
    app.scrollbar_probe = table_tab
    try:
        body, hits = default_renderer(width, source_height)
    finally:
        app.job_panel_source_canvas = previous_canvas
        app.scrollbar_probe = previous_probe
    records = chart_interaction.take_since(app, chart_mark)
    scroll_records = scrollbars.take_since(app, scroll_mark)
    groups = partition(body, hits, getattr(app, "tab", "jobs"), chart_records=records, scroll_records=scroll_records)
    originals = {panel: {"rows": list(group["rows"]), "hits": list(group["hits"]),
                         "charts": group.get("charts", ())} for panel, group in groups.items()}
    rects = geometry(app, width, height, has_details=bool(groups["details"]["rows"]))
    # Refit columns and plots to their actual panel widths instead of cutting
    # away the right half of a table that was composed for a whole terminal.
    by_width = {(width, source_height): groups}
    for panel, rect in rects.items():
        padding = 1 if (initialize(app).density == "comfortable" and rect.width >= 8 and rect.height >= 5
                        and not (native_jobs and panel == "main" and rect.height < 7)) else 0
        usable_width = max(0, rect.width - padding * 2 - (1 if panel == "details" or not table_tab else 0))
        render_height = max(1, rect.height - 1 - padding * 2) if table_tab and panel == "main" else source_height
        fitted_key = (usable_width, render_height)
        if fitted_key not in by_width:
            if native_jobs:
                app.job_panel_source_canvas = True
            chart_mark = chart_interaction.mark(app)
            scroll_mark = scrollbars.mark(app)
            previous_probe = getattr(app, "scrollbar_probe", False)
            app.scrollbar_probe = table_tab and panel != "main"
            try:
                fitted_body, fitted_hits = default_renderer(usable_width, render_height)
            finally:
                app.job_panel_source_canvas = previous_canvas
                app.scrollbar_probe = previous_probe
            records = chart_interaction.take_since(app, chart_mark)
            scroll_records = scrollbars.take_since(app, scroll_mark)
            by_width[fitted_key] = partition(fitted_body, fitted_hits, getattr(app, "tab", "jobs"), chart_records=records, scroll_records=scroll_records)
        groups[panel] = by_width[fitted_key][panel]
        _preserve_titles(groups[panel], originals[panel])
        if not table_tab or panel != "main":
            _preserve_metadata(groups[panel], originals[panel])
        if native_jobs and panel == "details" and groups[panel]["rows"] and _section_title(groups[panel]["rows"][0]) == "selected":
            groups[panel] = {"rows": groups[panel]["rows"][1:],
                             "hits": [(y - 1, kind, key) for y, kind, key in groups[panel]["hits"] if y > 0]}
    return transform_body(app, body, hits, width, height, ascii_=getattr(views.g, "ascii", False), groups=groups)


def overlay(views, snap, app, width: int, height: int):
    if getattr(app, "mode", "main") != "layout":
        return None
    state = initialize(app)
    lines = [[(f" Density: {state.density}   Focus: {state.focus}   Split: {state.ratio}% Main", "bold")],
             [(" :density comfortable|compact|focused", "secondary")],
             [(" :focus main|details   :maximize   :layout split 20..80", "secondary")],
             [(" Ctrl-W: change panel; arrows: scroll Details; z: maximize", "dim")],
             [(" Saved layouts", "heading+bold")]]
    lines.extend([(f" {name:<20} {value['density']} / {value['focus']} / {value['split']}%", "text")]
                 for name, value in sorted(state.named.items()))
    if not state.named:
        lines.append([(" Save your current workspace with :layout save <name>", "dim")])
    lines.append([(" Esc: close   :layout load <name>   :layout delete <name>", "muted")])
    state.overlay_count = len(lines)
    state.overlay_page = max(1, height - 5)
    state.overlay_top = max(0, min(state.overlay_top, max(0, len(lines) - state.overlay_page)))
    visible = lines[state.overlay_top:state.overlay_top + state.overlay_page]
    if len(lines) > state.overlay_page:
        visible.append([(f" {state.overlay_top + 1}-{min(len(lines), state.overlay_top + state.overlay_page)}/{len(lines)}  Up/Down/PgUp/PgDn scroll", "muted")])
    return L.box(views.g, visible, width, height, "Workspace layouts", min_width=52)
