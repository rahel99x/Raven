"""A virtualized, dockable job browser shared by data workspaces.

The browser paints records already present in the supplied snapshot. Pointer
movement, layout changes and list scrolling do not inspect files or poll Slurm.
An explicit job activation delegates to the existing page's source selector.
"""
from __future__ import annotations

from dataclasses import dataclass
import shlex

from . import layout as L
from .log_text import display_text
from .model import stamp
from . import scrollbars

TABS = ("analytics", "deps", "log", "research")
DOCKS = ("auto", "right", "bottom", "left", "top")
_STYLES = {"RUNNING": "green", "COMPLETING": "cyan", "PENDING": "yellow",
           "COMPLETED": "green", "FAILED": "red", "OUT_OF_MEMORY": "red",
           "TIMEOUT": "red", "CANCELLED": "dim"}


@dataclass(frozen=True)
class Rect:
    x: int
    y: int
    width: int
    height: int

    def contains(self, y, x):
        return self.x <= x < self.x + self.width and self.y <= y < self.y + self.height


@dataclass(frozen=True)
class _Item:
    record: object
    meta: object = None

    @property
    def group(self):
        return self.meta.group if self.meta is not None and self.meta.header else None

    @property
    def collapsed(self):
        return self.meta.collapsed if self.meta is not None else False

    @property
    def records(self):
        return self.meta.records if self.meta is not None else ()


def initialize(app):
    state = getattr(app, "history_browser_state", None)
    if not isinstance(state, dict):
        state = {"views": {}, "frame": None, "drag": None, "focused": False,
                 "discard_release": False,
                 "records": (), "record_key": None, "items": (), "index": 0}
        app.history_browser_state = state
    state.setdefault("discard_release", False)
    return state


def _cancel_drag(state):
    if state.get("drag"):
        state["drag"] = None
        state["discard_release"] = True


def _view(app):
    return initialize(app)["views"].setdefault(getattr(app, "tab", ""),
        {"dock": "auto", "enabled": True, "ratio": 25, "top": 0, "selected": None, "explicit": False})


def restore(app, data):
    if not isinstance(data, dict) or data.get("version", 1) != 1:
        return
    state = initialize(app)
    for tab in TABS:
        source = data.get("views", {}).get(tab, {}) if isinstance(data.get("views"), dict) else {}
        if not isinstance(source, dict):
            continue
        view = {"dock": source.get("dock") if source.get("dock") in DOCKS else "auto",
                "enabled": source.get("enabled") is not False,
                "ratio": 25, "top": 0, "selected": None, "explicit": False}
        for key, lower, upper in (("ratio", 15, 55), ("top", 0, 1000000)):
            value = source.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                view[key] = max(lower, min(upper, value))
        if isinstance(source.get("selected"), str) and len(source["selected"]) <= 256:
            view["selected"] = source["selected"]
            view["explicit"] = source.get("explicit") is True
        state["views"][tab] = view


def save(app):
    state = initialize(app)
    return {"version": 1, "views": {tab: dict(view) for tab, view in state["views"].items() if tab in TABS}}


def _persist(app):
    callback = getattr(app, "save", None)
    if callable(callback):
        callback()


def _say(app, message, *, failure=False):
    callback = getattr(app, "fail" if failure else "say", None)
    if callable(callback):
        callback(message)


def _records(snap, state):
    """Cache ordering by exact display fields, including in-place publication."""
    sources = (snap.get("jobs", ()), snap.get("finished", ()), snap.get("departed_jobs", ()))
    if isinstance(sources[2], dict):
        sources = (sources[0], sources[1], tuple(sources[2].values()))
    key = tuple(tuple((id(record), getattr(record, "id", ""), getattr(record, "name", ""),
                       getattr(record, "state", ""), getattr(record, "end", ""),
                       getattr(record, "submit", ""), getattr(record, "start", ""),
                       getattr(record, "reason", ""), getattr(record, "dependency", ""))
                      for record in source) for source in sources)
    if key != state["record_key"]:
        by_id = {}
        for source in sources:
            for record in source:
                if isinstance(getattr(record, "id", None), str):
                    by_id.setdefault(record.id, record)
        def order(record):
            timestamp = next((value for name in ("submit", "start", "end")
                              if (value := stamp(getattr(record, name, ""))) is not None), 0)
            job_id = record.id.split("_", 1)[0].split(".", 1)[0]
            return timestamp, int(job_id) if job_id.isdigit() else 0, record.id
        state["records"] = tuple(sorted(by_id.values(), key=order, reverse=True))
        state["record_key"] = key
    # Update object references even if a producer replaces equal display values.
    return state["records"]


def _selected(app, view):
    from .job_selection import cleared
    if cleared(app):
        return None
    field = {"analytics": "analytics_job", "log": "log_job", "research": "research_job_id", "deps": "selected_id"}
    current = getattr(app, field.get(getattr(app, "tab", ""), "selected_id"), None)
    if getattr(app, "tab", "") == "deps":
        if view.get("explicit"):
            return view["selected"]
        return current if current in getattr(app, "dep_ids", ()) else None
    return current or view["selected"] or getattr(app, "selected_id", None)


def _items(app, snap, records):
    # The shared deduction registry owns stable group identities and folding.
    from . import job_groups
    state = initialize(app)
    context = "history:" + app.tab
    registry = job_groups.registry(app)
    enabled = bool(getattr(app, "table_state", {}).get("groups", False))
    index = job_groups.frame_index(app, snap) if enabled else registry.index
    cache_key = (app.tab, id(records), id(index), tuple(sorted(registry.collapsed)),
                 enabled)
    cached = state.get("item_cache")
    if cached is not None and cached[0] == cache_key:
        state["indices"] = cached[4]
        state["selection_ids"] = cached[5]
        return cached[1]
    projected = job_groups.project_records(app, snap, records, tab=context, index=index)
    items = tuple(_Item(record, job_groups.metadata_for_record(app, context, record.id)) for record in projected)
    indices = {item.record.id: index for index, item in enumerate(items)}
    state["indices"] = indices
    state["selection_ids"] = tuple(indices)
    # Keep the source identity objects alive; cache keys cannot alias recycled
    # Python object IDs when a producer replaces its inference index.
    state["item_cache"] = (cache_key, items, index, records, indices, state["selection_ids"])
    return items


def _item_id(item):
    return getattr(getattr(item, "record", None), "id", None)


def _header(item):
    return getattr(item, "group", None)


def activate(app, job_id):
    """Select the exact ID in this page; no running-job fallback is permitted."""
    state = initialize(app)
    if getattr(app, "tab", "") not in TABS:
        return False
    record = next((record for record in state["records"] if record.id == job_id), None)
    lookup = getattr(app, "job_record", None)
    if callable(lookup):
        record = lookup(job_id)
    if record is None:
        _say(app, "That job is no longer available in the current snapshot", failure=True)
        return False
    from .job_selection import resume
    resume(app)
    app.selected_id = job_id
    _view(app).update(selected=job_id, explicit=True)
    state["focused"] = True
    if app.tab == "analytics":
        app.analytics_job, app.analytics_view = job_id, "job"
    elif app.tab == "log":
        app.open_log(job_id)
    elif app.tab == "research":
        from .research import select_job
        select_job(app, job_id)
    else:
        app.selected_id = job_id
        if job_id in getattr(app, "dep_ids", ()):
            app.cursor["deps"] = app.dep_ids.index(job_id)
    return True


def _geometry(width, height, dock, ratio):
    if dock == "auto":
        dock = "right" if width >= 110 and height >= 12 else "top"
    if dock in ("left", "right") and (width < 56 or height < 8):
        dock = "top"
    if dock in ("left", "right"):
        size = max(20, min(width - 30, round((width - 1) * ratio / 100)))
        main = width - size - 1
        browser = Rect(0 if dock == "left" else main + 1, 0, size, height)
        content = Rect(size + 1 if dock == "left" else 0, 0, main, height)
        divider = Rect(size if dock == "left" else main, 0, 1, height)
    else:
        size = max(2, min(max(2, height - 4), round((height - 1) * ratio / 100)))
        size = min(size, max(0, height - 2))
        main = max(0, height - size - 1)
        browser = Rect(0, 0 if dock == "top" else main + 1, width, size)
        content = Rect(0, size + 1 if dock == "top" else 0, width, main)
        divider = Rect(0, size if dock == "top" else main, width, 1)
    return dock, browser, content, divider


def _shift_hit(y, kind, value, rect):
    if kind == "control" and isinstance(value, dict):
        value = dict(value, left=rect.x + value["left"], right=rect.x + value["right"])
    elif kind == "sort_header" and isinstance(value, (tuple, list)) and len(value) == 4:
        value = (*value[:2], rect.x + value[2], rect.x + value[3])
    elif kind in ("node_cell", "job_panel_tab", "job_panel_file", "job_panel_view", "job_panel_action") and isinstance(value, (tuple, list)) and len(value) == 3:
        value = (value[0], rect.x + value[1], rect.x + value[2])
    return rect.y + y, kind, value


def _control(tab, key, label, y, left, right, command):
    return (y, "control", {"id": "history:" + tab + ":" + key,
        "label": label, "left": left, "right": right,
        "action": ("command", command), "group": "job-history"})


def _text(value, ascii_):
    text = display_text(str(value))
    return text.encode("ascii", "backslashreplace").decode("ascii") if ascii_ else text


def _browser_rows(views, app, rect, items, selected, view, dock):
    width, height, ascii_ = rect.width, rect.height, views.g.ascii
    rows = [[] for _ in range(height)]
    hits, tab = [], app.tab
    if not height or not width:
        return rows, hits, 0, 1
    handle = "::" if ascii_ else "⠿"
    jump_space = 4 if width >= 10 else 0
    dock_label = " Dock " + dock.title() + " " if width >= 30 else " Dock " if width >= 24 else " D "
    close = " x "
    fixed = L.vlen(handle + " " + dock_label + close) + jump_space
    title = L.cut("Jobs " + str(len(initialize(app)["records"])), max(0, width - fixed), ascii_)
    header = " " * jump_space + handle + " " + title
    header = L.pad(header, max(0, width - len(dock_label) - len(close)))
    rows[0] = [(header, "cyan+bold"), (dock_label, "accent+bg:surface"), (close, "dim+bg:surface")]
    dock_x = width - len(dock_label) - len(close)
    hits.append(_control(tab, "drag", "Drag job history to an edge", 0, jump_space,
                         min(width, jump_space + L.vlen(handle)), "history-focus"))
    if dock_x >= 0:
        hits.append(_control(tab, "dock", "Change job-history dock", 0, dock_x, dock_x + len(dock_label), "history-dock next"))
        hits.append(_control(tab, "off", "Hide job history", 0, width - len(close), width, "history-browser off"))
    columns = max(1, width // 26) if dock in ("top", "bottom") else 1
    data_height = max(0, height - 2) if height >= 4 else max(0, height - 1)
    page = data_height * columns
    top = max(0, min(max(0, len(items) - page), view["top"])) if page else 0
    state = initialize(app)
    if state.get("reveal") and page:
        index = max(0, min(len(items) - 1, state["index"]))
        if index < top:
            top = index
        elif index >= top + page:
            top = max(0, index - page + 1)
    state["reveal"] = False
    view["top"] = top
    from .scrolling import viewport
    painted = viewport(app, "history-browser:" + tab, top, len(items), page,
                       context=(dock, width, height, columns))
    logical_top, top = top, painted
    content_width = max(1, width - 1)
    cell_width = max(1, content_width // columns)
    seen_groups = set()
    for position, item in enumerate(items[top:top + page]):
        y, column = 1 + position // columns, position % columns
        x = column * cell_width
        size = content_width - x if column == columns - 1 else cell_width
        group = _header(item)
        meta = item.meta
        disclosure = meta is not None and meta.group.id not in seen_groups
        if meta is not None:
            seen_groups.add(meta.group.id)
        if group is not None:
            from .job_group_ui import fold_icon, fold_command
            icon = fold_icon(item.collapsed, ascii_=ascii_)
            contains_selected = item.collapsed and selected in group.members
            selected_note = f" [>{_text(selected, ascii_)}]" if contains_selected and selected != item.record.id else ""
            label = f"{icon} {_text(item.record.id, ascii_)}{selected_note} {_text(group.label, ascii_)} ({len(item.records)})"
            text = L.pad(L.cut(label, size, ascii_), size)
            style = "sel+bold" if item.record.id == selected or contains_selected else "accent+bold"
            if item.collapsed:
                from .job_group_ui import summary_row
                prefix = f"{icon} {_text(item.record.id, ascii_)}{selected_note} {len(item.records)} rec "
                prefix = L.cut(prefix, size, ascii_)
                segments = [(prefix, style)] + summary_row(item.meta.stats or item.records, max(0, size - L.vlen(prefix)),
                    ascii_=ascii_, selected=item.record.id == selected or contains_selected)
            else:
                segments = [(text, style)]
            command = "history-job " + shlex.quote(item.record.id)
            ident = "job:" + item.record.id
            from .job_groups import summary
            states = summary(item.meta.stats or item.records)
            label += "; " + states + "; representative job " + item.record.id
        else:
            record = item.record
            if disclosure:
                from .job_group_ui import fold_icon
                icon = fold_icon(meta.collapsed, ascii_=ascii_) + " "
            else:
                icon = ">" if record.id == selected else " "
            label = f"{icon}{_text(record.id, ascii_)} {_text(record.state, ascii_)} {_text(record.name, ascii_)}"
            text = L.pad(L.cut(label, size, ascii_), size)
            style = "sel+bold" if record.id == selected else _STYLES.get(record.state, "dim")
            segments = [(text, style)]
            command = "history-job " + shlex.quote(record.id)
            ident = "job:" + record.id
        if disclosure:
            from .job_group_ui import fold_icon, fold_command
            from .pane_drag import _replace
            from .job_groups import summary
            # A scrolled continuation keeps the actual member's ID and row
            # action. Only its reserved leading cell gains the group button.
            arrow_style = "accent+bold+sel" if "sel" in style.split("+") else "accent+bold"
            segments = _replace(segments, 0, fold_icon(meta.collapsed, ascii_=ascii_), arrow_style, size)
            hits.append(_control(tab, "group:" + meta.group.id,
                                 ("Expand " if meta.collapsed else "Collapse ") + meta.group.label + "; " + summary(meta.stats or meta.records),
                                 y, x, min(x + size, x + 2), fold_command(meta.group.id, meta.collapsed)))
        if item.record.id in getattr(app, "marks", ()) and size > 2:
            from .pane_drag import _replace
            segments = _replace(segments, size - 1, "*" if ascii_ else "•", "warning+bold", size)
            label += "; marked"
        rows[y].extend(segments)
        hits.append(_control(tab, ident, label, y, x, x + size, command))
    if not items and data_height:
        rows[1] = [(L.cut(" No job records yet", width, ascii_), "dim")]
    if height >= 4:
        arrow_up, arrow_down = (" ^ ", " v ") if ascii_ else (" ↑ ", " ↓ ")
        text = f" {top + 1 if items else 0}-{min(len(items), top + page)}/{len(items)} "
        limit = max(0, width - 6)
        rows[-1] = [(L.pad(L.cut(text, limit, ascii_), limit), "dim"), (arrow_up + arrow_down, "cyan")]
        if width >= 6:
            hits.extend([_control(tab, "previous", "Previous history page", height - 1, width - 6, width - 3, "history-scroll page-up"),
                         _control(tab, "next", "Next history page", height - 1, width - 3, width, "history-scroll page-down")])
    if data_height and width >= 2:
        scrollbars.register(app, "history-browser:" + tab, (1, 0, 1 + data_height, width),
            len(items), page, logical_top, top, lambda value: view.__setitem__("top", value),
            context=(tab, dock, width, height, columns), header=(0, 0, width) if jump_space else None)
    return [L.clip_row(row, width) for row in rows], hits, page, columns


def wrap_render(views, snap, app, width, height, content_renderer):
    """Build native content once at its final dimensions; return body-relative hits.

    Geometry registries use ``app.body_origin`` immediately and are absolute.
    ``offset_hits`` can be called after final composition; it is idempotent.
    """
    if getattr(app, "tab", "") not in TABS or height is None:
        return content_renderer(width, height)
    state, view = initialize(app), _view(app)
    previous_frame = state.get("frame")
    if previous_frame and previous_frame["tab"] != app.tab:
        state["focused"] = False
    width, height = max(0, width), max(0, height)
    origin = int(getattr(app, "body_origin", 0))
    if not width or not height:
        state["frame"] = None
        app.history_browser_content_rect = None
        return content_renderer(width, height)
    records = _records(snap, state)
    selected = _selected(app, view)
    view["selected"] = selected
    items = _items(app, snap, records)
    state["items"] = items
    state["reveal"] = selected != state.get("last_selected")
    selected_index = state["indices"].get(selected)
    if selected_index is not None:
        state["index"] = selected_index
    elif selected:
        from . import job_groups
        meta = job_groups.metadata_for_record(app, "history:" + app.tab, selected)
        representative = state["indices"].get(meta.representative_id) if meta is not None else None
        state["index"] = (representative if representative is not None else
                          min(state["index"], max(0, len(items) - 1)))
    else:
        state["index"] = min(state["index"], max(0, len(items) - 1))
    state["last_selected"] = selected
    if not view["enabled"] or height <= 2 or width < 8:
        browser, content, divider, dock = Rect(0, 0, width, 1), Rect(0, 1, width, max(0, height - 1)), None, "off"
        bar = L.cut(" Job history + ", width, views.g.ascii)
        browser_rows = [[(bar, "cyan+bg:surface")]]
        browser_hits = [_control(app.tab, "show", "Show job history", 0, 0, L.vlen(bar), "history-browser on")]
        page, columns = 0, 1
    else:
        dock, browser, content, divider = _geometry(width, height, view["dock"], view["ratio"])
        browser_mark = scrollbars.mark(app)
        browser_rows, browser_hits, page, columns = _browser_rows(views, app, browser, items, selected, view, dock)
        scrollbars.place_since(app, browser_mark, dy=browser.y, dx=browser.x,
            clip=(browser.y, browser.x, browser.y + browser.height, browser.x + browser.width))
    state["frame"] = {"tab": app.tab, "mode": getattr(app, "mode", "main"), "width": width, "height": height,
        "origin": origin, "browser": browser, "content": content, "divider": divider, "dock": dock,
        "page": page, "columns": columns, "hits": browser_hits,
        "geometry": (getattr(app, "width", None), getattr(app, "height", None)),
        "preference": (view["dock"], view["enabled"], view["ratio"])}
    app.history_browser_content_rect = Rect(content.x, content.y + origin, content.width, content.height)
    app.history_browser_rect = Rect(browser.x, browser.y + origin, browser.width, browser.height)
    from . import chart_interaction
    chart_mark = chart_interaction.mark(app)
    scroll_mark = scrollbars.mark(app)
    rows, hits = content_renderer(content.width, content.height)
    chart_interaction.place_since(app, chart_mark, dy=content.y, dx=content.x,
        clip=(content.y, content.x, content.y + content.height, content.x + content.width))
    scrollbars.place_since(app, scroll_mark, dy=content.y, dx=content.x,
        clip=(content.y, content.x, content.y + content.height, content.x + content.width))
    canvas = [[(" " * width, "bg:canvas")] for _ in range(height)]
    for y in range(height):
        chunks = []
        for rect, source in ((content, rows), (browser, browser_rows)):
            if rect.y <= y < rect.y + rect.height:
                line = source[y - rect.y] if y - rect.y < len(source) else []
                chunks.append((rect.x, L.fill_row(L.clip_row(line, rect.width), rect.width, "bg:canvas")))
        assembled, x = [], 0
        for left, line in sorted(chunks, key=lambda chunk: chunk[0]):
            assembled.append((" " * max(0, left - x), "bg:canvas"))
            assembled.extend(line)
            x = left + L.vlen(L.row_text(line))
        canvas[y] = L.fill_row(L.clip_row(assembled, width), width, "bg:canvas")
    result_hits = [_shift_hit(y, kind, value, content) for y, kind, value in hits if 0 <= y < content.height]
    result_hits += [_shift_hit(y, kind, value, browser) for y, kind, value in browser_hits if 0 <= y < browser.height]
    if divider is not None:
        _register_divider(app)
        from . import pane_drag
        pane_drag.paint(canvas, app, "history:" + app.tab, origin_y=origin, ascii_=views.g.ascii)
        hit = pane_drag.control_hit(app, "history:" + app.tab, origin_y=origin)
        if hit:
            result_hits.append(hit)
    drag = state.get("drag")
    if drag and not _drag_valid(app, drag):
        _cancel_drag(state)
        drag = None
    if drag and _drag_valid(app, drag):
        preview = drag.get("preview")
        if preview:
            text = L.cut(" Drop job history: " + preview + " ", width, views.g.ascii)
            line = height - 1 if preview == "bottom" else 0
            canvas[line] = L.fill_row([(text, "accent+bold+bg:surface-raised")], width, "bg:surface-raised")
            if preview in ("left", "right"):
                from .pane_drag import _replace
                edge = 0 if preview == "left" else width - 1
                for index in range(1, height):
                    canvas[index] = _replace(canvas[index], edge, "|" if views.g.ascii else "┃", "accent+bold", width)
    return canvas, result_hits


def _register_divider(app):
    state, view = initialize(app), _view(app)
    frame = state["frame"]
    divider, dock = frame["divider"], frame["dock"]
    if divider is None:
        return
    from . import pane_drag
    vertical = dock in ("left", "right")
    first = dock in ("left", "top")
    def resize(value):
        view["ratio"] = max(15, min(55, value if first else 100 - value))
    return pane_drag.register(app, "history:" + app.tab, "vertical" if vertical else "horizontal",
        divider.x, frame["origin"] + divider.y, divider.width, divider.height,
        0 if vertical else frame["origin"], (frame["width"] if vertical else frame["height"]) - 1,
        view["ratio"] if first else 100 - view["ratio"], resize,
        minimum=15 if first else 45, maximum=55 if first else 85,
        full_vertical=vertical, label="Resize job history")


def offset_hits(app, body_origin):
    """Idempotently refresh absolute geometry when the outer header changes."""
    state = initialize(app)
    frame = state.get("frame")
    if not frame or frame["tab"] != getattr(app, "tab", ""):
        return
    frame["origin"] = body_origin
    for name in ("content", "browser"):
        rect = frame[name]
        setattr(app, "history_browser_" + ("content_rect" if name == "content" else "rect"),
                Rect(rect.x, rect.y + body_origin, rect.width, rect.height))
    _register_divider(app)


def _current(app):
    frame = initialize(app).get("frame")
    toolbar = getattr(app, "toolbar_state", {})
    if (not frame or frame["tab"] != getattr(app, "tab", "") or
            frame["mode"] != getattr(app, "mode", "main") or frame["mode"] != "main" or
            frame["geometry"] != (getattr(app, "width", None), getattr(app, "height", None)) or
            isinstance(toolbar, dict) and (toolbar.get("menu") is not None or toolbar.get("panel"))):
        return None
    view = _view(app)
    if frame.get("preference", (view["dock"], view["enabled"], view["ratio"])) != (view["dock"], view["enabled"], view["ratio"]):
        return None
    return frame


def _drag_valid(app, drag):
    frame = _current(app)
    toolbar = getattr(app, "toolbar_state", {})
    return bool(frame and (frame["tab"], frame["width"], frame["height"], frame["origin"]) == drag["context"]
                and not (isinstance(toolbar, dict) and (toolbar.get("menu") or toolbar.get("panel"))))


def tick(app):
    """Cancel hidden or resized captures even without another mouse report."""
    state = initialize(app)
    drag = state.get("drag")
    if drag and not _drag_valid(app, drag):
        _cancel_drag(state)


def _drop(frame, y, x):
    y -= frame["origin"]
    if not 0 <= x < frame["width"] or not 0 <= y < frame["height"]:
        return None
    distances = {"left": x / max(1, frame["width"]), "right": (frame["width"] - 1 - x) / max(1, frame["width"]),
                 "top": y / max(1, frame["height"]), "bottom": (frame["height"] - 1 - y) / max(1, frame["height"])}
    edge = min(distances, key=distances.get)
    return edge if distances[edge] <= .22 else None


def header_control_contains(app, y, x):
    """Give visible header buttons priority over an adjacent splitter buffer."""
    frame = _current(app)
    rect = getattr(app, "history_browser_rect", None)
    if not frame or not rect or y != rect.y or not rect.contains(y, x):
        return False
    local_x = x - rect.x
    return any(hy == 0 and value["left"] <= local_x < value["right"]
               for hy, kind, value in frame["hits"])


def handle_mouse(app, y, x, button="left", shift=False):
    if any(type(value) is not int for value in (y, x)):
        return False
    state = initialize(app)
    from .job_selection import handle_mouse as selection_mouse
    if (button in ("motion", "drag", "release")
            and getattr(app, "job_selection_state", {}).get("capture")):
        if selection_mouse(app, y, x, button=button, shift=shift):
            return True
    drag = state.get("drag")
    frame = _current(app)
    if button in ("press", "left"):
        state["discard_release"] = False
    if button == "release" and not drag and state["discard_release"]:
        state["discard_release"] = False
        return True
    if drag:
        if not _drag_valid(app, drag):
            _cancel_drag(state)
            if button in ("release", "press", "left"):
                state["discard_release"] = False
            return button in ("motion", "drag", "release")
        if button in ("motion", "drag", "release"):
            start_y, start_x = drag["point"]
            if abs(y - start_y) + abs(x - start_x) >= 2:
                drag["moved"] = True
            drag["preview"] = _drop(frame, y, x) if drag["moved"] else None
            if button == "release":
                target = drag["preview"]
                state["drag"] = None
                if target is not None and _view(app)["dock"] != target:
                    _view(app)["dock"] = target
                    _persist(app)
            return True
        _cancel_drag(state)
        if button in ("press", "left"):
            state["discard_release"] = False
    if frame is None:
        return False
    browser = getattr(app, "history_browser_rect", None)
    if not browser or not browser.contains(y, x):
        if button in ("left", "press"):
            state["focused"] = False
        return False
    if button in ("wheelup", "wheeldown", "wheel_up", "wheel_down", "wheel-up", "wheel-down"):
        _scroll(app, "up" if "up" in button else "down", lines=3)
        return True
    if button in ("press", "left") and selection_mouse(app, y, x, button=button, shift=shift):
        return True
    if button in ("right", "left", "press") and shift or button == "right":
        return True
    if button not in ("left", "press"):
        return False
    local_y, local_x = y - browser.y, x - browser.x
    hit = next((value for hy, kind, value in frame["hits"]
                if hy == local_y and value["left"] <= local_x < value["right"]), None)
    state["focused"] = True
    if hit:
        if hit["id"].endswith(":drag") and button == "press":
            state["drag"] = {"context": (frame["tab"], frame["width"], frame["height"], frame["origin"]),
                             "preview": None, "point": (y, x), "moved": False}
            state["discard_release"] = False
        else:
            arguments = shlex.split(hit["action"][1])
            if arguments[0] == "jobgroup":
                app.run_command(hit["action"][1])
            else:
                run_command(app, arguments)
    return True


def _scroll(app, action, *, lines=1):
    state, view = initialize(app), _view(app)
    frame = _current(app)
    if not frame:
        return
    page = max(1, frame["page"])
    total = len(state["items"])
    limit = max(0, total - page)
    top = view["top"]
    value = (0 if action == "home" else limit if action == "end" else
             top + {"up": -lines, "down": lines, "page-up": -page, "page-down": page}.get(action, 0))
    view["top"] = max(0, min(limit, value))


def handle_key(app, key):
    state = initialize(app)
    if state.get("drag") and key == "esc":
        _cancel_drag(state)
        return True
    frame = _current(app)
    if not frame or frame["dock"] == "off" or not state["focused"]:
        return False
    if key in ("esc", "tab", "f6", "ctrl-w", "ctrl_w"):
        state["focused"] = False
        return key == "esc"
    items = state["items"]
    if not items:
        return key in ("up", "down", "left", "right", "pgup", "pgdn", "home", "end", "enter")
    index = max(0, min(len(items) - 1, state["index"]))
    if key in ("left", "right") and items[index].meta is not None:
        from . import job_groups
        meta = items[index].meta
        job_groups.fold(app, meta.group.id, collapsed=key == "left")
        if key == "left":
            state["index"] = state["indices"].get(meta.representative_id, index)
        _persist(app)
        return True
    steps = {"up": -frame["columns"], "down": frame["columns"], "left": -1, "right": 1,
             "pgup": -max(1, frame["page"]), "pgdn": max(1, frame["page"])}
    if key in steps or key in ("home", "end"):
        index = 0 if key == "home" else len(items) - 1 if key == "end" else max(0, min(len(items) - 1, index + steps[key]))
        state["index"] = index
        view = _view(app)
        if index < view["top"]:
            view["top"] = index
        elif index >= view["top"] + max(1, frame["page"]):
            view["top"] = max(0, index - max(1, frame["page"]) + 1)
        job_id = _item_id(items[index])
        if job_id:
            activate(app, job_id)
        return True
    if key == "space":
        from .job_selection import handle_key as selection_key
        return selection_key(app, key)
    if key == "enter":
        group = _header(items[index])
        if group:
            from . import job_groups
            job_groups.fold(app, group.id)
            _persist(app)
        else:
            activate(app, _item_id(items[index]))
        return True
    return False


def command_names():
    return ["history-dock", "history-browser", "history-job", "history-scroll", "history-focus"]


def run_command(app, args):
    if not args or args[0] not in command_names():
        return False
    if getattr(app, "tab", "") not in TABS:
        _say(app, "Job history is available in Analytics, Deps, Log and Research", failure=True)
        return True
    state, view = initialize(app), _view(app)
    command, values = args[0], args[1:]
    if command == "history-focus" and not values:
        if not view["enabled"]:
            view["enabled"] = True
            _persist(app)
        state["focused"] = True
        _say(app, "Job history: arrows select; Space marks; g groups; u ungroups; PgUp/PgDn page; Esc returns to data")
        return True
    if command == "history-job" and len(values) == 1:
        activate(app, values[0])
        return True
    if command == "history-scroll" and len(values) == 1 and values[0] in ("up", "down", "page-up", "page-down", "home", "end"):
        _scroll(app, values[0])
        return True
    if command == "history-dock" and len(values) == 1 and values[0] in (*DOCKS, "next", "off"):
        if values[0] == "off":
            view["enabled"] = False
            state["focused"] = False
        else:
            view["enabled"] = True
            if values[0] == "next":
                frame = _current(app)
                current = frame["dock"] if view["dock"] == "auto" and frame else view["dock"]
                choices = DOCKS[1:]
                view["dock"] = choices[(choices.index(current) + 1) % len(choices)] if current in choices else choices[0]
            else:
                view["dock"] = values[0]
        _cancel_drag(state)
        _persist(app)
        return True
    if command == "history-browser" and len(values) <= 1 and (not values or values[0] in ("on", "off")):
        view["enabled"] = not view["enabled"] if not values else values[0] == "on"
        if not view["enabled"]:
            state["focused"] = False
        _cancel_drag(state)
        _persist(app)
        return True
    _say(app, "history-dock <auto|left|right|top|bottom|off>; history-browser [on|off]; history-job JOBID; history-scroll <up|down|page-up|page-down|home|end>", failure=True)
    return True


def overlay(views, snap, app, width, height):
    return None
