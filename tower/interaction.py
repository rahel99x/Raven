"""A bounded, cell-accurate interaction graph for the painted terminal frame.

Renderers publish controls as data. Graph construction and pointer feedback
never read files or request samples. Explicit keyboard row focus binds only a
published real job ID; activation uses the controller and its confirmations.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from itertools import islice
from types import MappingProxyType
from . import layout as L

MAX_CONTROLS = 4096
MAX_OVERLAY_ROWS = 4096
MAX_HIT_ITEMS = MAX_CONTROLS * 64
POINTER_STYLE = "bg:surface-raised+under+bold"
FOCUS_STYLE = "bg:track+under+bold"
ROW_KINDS = frozenset(("job", "recent", "fin", "source", "group", "dep", "log_file",
                       "log_group", "research_array", "research_evidence", "research_metric",
                       "partition_row", "node_row", "user_drill"))
BOUNDED_KINDS = frozenset(("node_cell", "job_panel_tab", "job_panel_file",
                           "job_panel_view", "job_panel_action"))


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
        return (self.top < other.bottom and other.top < self.bottom
                and self.left < other.right and other.left < self.right)

    def clip(self, width: int, height: int):
        result = Rect(max(0, self.top), max(0, self.left), min(height, self.bottom), min(width, self.right))
        return result if result.top < result.bottom and result.left < result.right else None

    @property
    def center(self):
        return ((self.top + self.bottom - 1) / 2, (self.left + self.right - 1) / 2)


@dataclass(frozen=True)
class Control:
    """Immutable semantic control; action arguments contain only scalar data."""

    id: str
    label: str
    rect: Rect
    action: tuple
    group: str = "buttons"
    enabled: bool = True
    reason: str = ""
    button: bool = True
    layer: int = 0


@dataclass(frozen=True)
class _SpanIndex:
    """Sparse vertical interval index for rails and other tall controls.

    Each control is stored once. Looking up a terminal row visits one branch,
    rather than testing the tall controls from every other visible pane.
    """

    center: int
    entries: tuple
    before: object = None
    after: object = None

    @classmethod
    def build(cls, entries):
        if not entries:
            return None
        centers = sorted((entry[1].rect.top + entry[1].rect.bottom - 1) // 2 for entry in entries)
        center = centers[len(centers) // 2]
        before, crossing, after = [], [], []
        for entry in entries:
            rect = entry[1].rect
            (before if rect.bottom <= center else after if rect.top > center else crossing).append(entry)
        return cls(center, tuple(sorted(crossing, key=lambda entry: entry[0], reverse=True)),
                   cls.build(before), cls.build(after))

    def at(self, y, x):
        best = next((entry for entry in self.entries if entry[1].rect.contains(y, x)), None)
        branch = self.before if y < self.center else self.after if y > self.center else None
        candidate = branch.at(y, x) if branch is not None else None
        return candidate if candidate and (best is None or candidate[0] > best[0]) else best


@dataclass(frozen=True)
class Graph:
    controls: tuple[Control, ...]
    token: tuple
    width: int
    height: int
    generation: int
    observed_geometry: tuple = ()
    regions: tuple = ()
    scope: tuple = ()
    _identities: object = field(init=False, repr=False, compare=False)
    _rows: object = field(init=False, repr=False, compare=False)
    _spanning: tuple = field(init=False, repr=False, compare=False)
    _span_index: object = field(init=False, repr=False, compare=False)
    _regions: object = field(init=False, repr=False, compare=False)
    _ordered: tuple = field(init=False, repr=False, compare=False)
    _order_indices: object = field(init=False, repr=False, compare=False)

    def __post_init__(self):
        identities, rows, spanning, regions = {}, {}, [], {}
        for index, control in enumerate(self.controls):
            identities.setdefault(control.id, control)
            rect = control.rect
            priority = (control.layer, control.button,
                        -((rect.right - rect.left) * (rect.bottom - rect.top)), -index)
            entry = (priority, control)
            if rect.bottom == rect.top + 1:
                rows.setdefault(rect.top, []).append(entry)
            else:
                # Multi-row controls do not multiply storage by screen height.
                # Native buttons and table rows take the constant-time row path.
                spanning.append(entry)
            if control.group == "pane-dividers":
                region = "divider"
            elif control.group.startswith("toolbar") or control.group == "tabs":
                region = "global:" + control.group
            else:
                region = next((name for name, bounds in self.regions
                               if bounds.top <= rect.top and rect.bottom <= bounds.bottom
                               and bounds.left <= rect.left and rect.right <= bounds.right), "")
            regions[control.id] = region
        object.__setattr__(self, "_identities", MappingProxyType(identities))
        object.__setattr__(self, "_rows", MappingProxyType({
            y: tuple(sorted(entries, key=lambda entry: entry[0], reverse=True))
            for y, entries in rows.items()}))
        object.__setattr__(self, "_spanning", tuple(sorted(spanning, key=lambda entry: entry[0], reverse=True)))
        object.__setattr__(self, "_span_index", _SpanIndex.build([
            entry for entry in spanning if entry[1].rect.top < entry[1].rect.bottom]))
        object.__setattr__(self, "_regions", MappingProxyType(regions))
        ordered = tuple(sorted((control for control in self.controls if control.enabled),
                               key=lambda control: (control.rect.top, control.rect.left, control.id)))
        object.__setattr__(self, "_ordered", ordered)
        object.__setattr__(self, "_order_indices", MappingProxyType({control.id: index for index, control in enumerate(ordered)}))

    def get(self, identity):
        try:
            return self._identities.get(identity)
        except TypeError:
            return None

    def at(self, y, x):
        # Later / smaller controls win when a selectable row also contains a button.
        local = next((entry for entry in self._rows.get(y, ()) if entry[1].rect.contains(y, x)), None)
        spanning = self._span_index.at(y, x) if self._span_index is not None else None
        if local is None:
            return spanning[1] if spanning else None
        return spanning[1] if spanning and spanning[0] > local[0] else local[1]

    def region(self, control):
        return self._regions.get(control.id, "")


def initialize(app):
    state = getattr(app, "interaction_state", None)
    if not isinstance(state, dict):
        state = {}
        app.interaction_state = state
    for key, value in (("graph", None), ("pointer", None), ("hovered", None), ("focused", None),
                       ("active", False), ("routing", False), ("generation", 0), ("pending_focus", None),
                       ("frame_required", False)):
        state.setdefault(key, value)
    return state


def command_names():
    return ["focusbuttons"]


def overlay(views, snap, app, width, height):
    return None


def _state(app, name):
    value = getattr(app, name, {})
    return value if isinstance(value, dict) else {}


def hit_token(hits):
    """Copy native hit payloads into a bounded, immutable frame identity.

    Controller activation must compare the registry that was painted, rather
    than a mutable ``last_hits`` alias. Unsupported or oversized payloads fail
    closed; this work runs at publication and explicit clicks, never hover.
    """
    remaining = MAX_HIT_ITEMS

    def freeze(value, depth=0):
        nonlocal remaining
        remaining -= 1
        if remaining < 0 or depth > 8:
            raise ValueError("oversized hit payload")
        if isinstance(value, (str, int, float, bool, type(None))):
            return (type(value).__name__, value)
        if isinstance(value, (tuple, list)) and len(value) <= 128:
            return tuple(freeze(item, depth + 1) for item in value)
        if isinstance(value, dict) and len(value) <= 128:
            entries = tuple((freeze(key, depth + 1), freeze(item, depth + 1))
                            for key, item in value.items())
            return ("mapping", tuple(sorted(entries, key=lambda item: item[0])))
        raise ValueError("unsupported hit payload")

    try:
        token = tuple(freeze(hit) for hit in islice(hits or (), MAX_CONTROLS + 1))
        return token if len(token) <= MAX_CONTROLS else None
    except (TypeError, ValueError, RecursionError):
        return None


def _scope(app):
    """Overlay identities are distinct from ordinary viewport/row changes."""
    toolbar = _state(app, "toolbar_state")
    analysis = _state(app, "analysis_state")
    mode = getattr(app, "mode", "main")
    return (mode, getattr(app, "tab", ""), toolbar.get("menu"), toolbar.get("panel"),
            analysis.get("modal"), _state(app, "table_tools_state").get("modal"),
            id(getattr(app, "confirm", None)) if mode == "confirm" else None,
            id(_state(app, "execution_state").get("review")) if mode == "execution" else None,
            tuple(_state(app, "ops_state").get(key) for key in ("generation", "view", "editing"))
            if mode == "operations" else None)


def _viewport(app):
    """Bounded preference/offset identity; never inspect jobs or chart data."""
    tab = getattr(app, "tab", "")
    top, cursor = _state(app, "top"), _state(app, "cursor")
    table = _state(app, "table_state")
    layout = getattr(app, "layout_state", None)
    scroll = getattr(layout, "scroll", {})
    browser = _state(app, "history_browser_state").get("views", {}).get(tab, {})
    if not isinstance(browser, dict):
        browser = {}
    logs = getattr(app, "logs", None)
    recent = getattr(app, "recent_history_state", None)
    panel = _state(app, "job_panel_state")
    session = panel.get("session")
    analysis = _state(app, "analysis_state")
    project = _state(app, "project_state")
    from .table_ui import fingerprint
    tables = (tab, "recent") if tab == "jobs" else (tab,)
    return (tuple((name, top.get(name), cursor.get(name),
                   _state(app, "sort").get(name), _state(app, "reverse").get(name),
                   fingerprint(app, name),
                   tuple(islice(table.get("hidden", {}).get(name, ()), 64)),
                   tuple(islice(table.get("order", {}).get(name, ()), 64)),
                   tuple(sorted(islice(table.get("widths", {}).get(name, {}).items(), 64))))
                  for name in tables),
            getattr(app, "filter", ""), table.get("groups"), tuple(islice(table.get("collapsed", ()), 256)),
            getattr(app, "manual_job_groups_revision", 0),
            getattr(layout, "density", None), getattr(layout, "ratio", None),
            getattr(layout, "maximized", None),
            scroll.get(tab + ":main"), scroll.get(tab + ":details"),
            browser.get("top"), browser.get("dock"), browser.get("ratio"),
            browser.get("enabled"), browser.get("selected"), browser.get("explicit"),
            getattr(recent, "ratio", None), getattr(recent, "manual_split", None),
            getattr(app, "research_scroll", None), getattr(app, "research_task_offset", None),
            getattr(app, "deps_scope_top", None), getattr(logs, "top", None),
            getattr(logs, "browser_top", None), _state(app, "analytics_document_state").get("top"),
            panel.get("file_id"), getattr(session, "path", None), getattr(session, "top", None),
            tuple(analysis.get(name) for name in ("scroll", "cursor", "section", "evidence_cursor", "zoom", "pan",
                                                  "window", "preset", "chart_box", "chart_live_window")),
            tuple(project.get(name) for name in ("generation", "run_cursor", "run_top", "output_cursor", "output_top",
                                                 "preview_scroll", "preview_page", "preview_column", "filter",
                                                 "notices_open", "notices_scroll")))


def _context(app):
    """Reject actions after a page, overlay, or selected-job identity changes."""
    toolbar = _state(app, "toolbar_state")
    analysis = _state(app, "analysis_state")
    panel = _state(app, "job_panel_state")
    project = _state(app, "project_state")
    table_tools = _state(app, "table_tools_state")
    execution = _state(app, "execution_state")
    binding = project.get("binding")
    binding = binding if isinstance(binding, dict) else {}
    logs = getattr(app, "logs", None)
    return (getattr(app, "mode", "main"), getattr(app, "tab", ""),
            getattr(app, "selected_id", None), getattr(app, "nodes_view", None),
            getattr(app, "analytics_view", None), getattr(app, "research_view", None),
            analysis.get("modal"), analysis.get("job"), analysis.get("chart_job"), analysis.get("metric"), panel.get("mode"),
            panel.get("view"), panel.get("research_view"), panel.get("analytics_view"), project.get("root"),
            getattr(logs, "browser", None), getattr(logs, "path", None),
            toolbar.get("menu"), toolbar.get("panel"), table_tools.get("modal"),
            table_tools.get("tab"), _state(app, "table_state").get("tab") if getattr(app, "mode", "main") == "columns" else None,
            id(getattr(app, "confirm", None)) if getattr(app, "mode", "main") == "confirm" else None,
            id(execution.get("review")) if getattr(app, "mode", "main") == "execution" else None,
            repr(execution.get("pending_action"))[:512] if getattr(app, "mode", "main") == "execution" else None,
            _viewport(app),
            getattr(app, "analytics_job", None), getattr(app, "research_job_id", None), getattr(app, "log_job", None),
            tuple(binding.get(name) for name in ("project_root", "run_id", "job_id", "attempt")),
            tuple(_state(app, "ops_state").get(key) for key in ("generation", "view", "editing"))
            if getattr(app, "mode", "main") == "operations" else None)


def _current(app):
    state = initialize(app)
    graph = state["graph"]
    if graph is None or graph.token != _context(app):
        return None
    # Screen size is optional for small embedders and headless renderers.
    for name, expected in zip(("width", "height"), graph.observed_geometry):
        actual = getattr(app, name, None)
        if actual != expected:
            return None
    return graph


def controls(app):
    graph = _current(app)
    return graph.controls if graph is not None else ()


def _integer(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _rect(value):
    if isinstance(value, Rect):
        return value if all(_integer(item) for item in (value.top, value.left, value.bottom, value.right)) else None
    if isinstance(value, (tuple, list)) and len(value) == 4 and all(_integer(item) for item in value):
        return Rect(*value)
    return None


def _action(value):
    if not isinstance(value, (tuple, list)) or not value:
        return None
    if value[0] not in ("click", "command", "key", "set", "row", "scrollbar"):
        return None
    if any(not isinstance(item, (str, int, float, bool, type(None))) for item in value):
        return None
    return tuple(value)


def _descriptor(value, *, layer=0):
    if isinstance(value, Control):
        value = {"id": value.id, "label": value.label, "rect": value.rect, "action": value.action,
                 "group": value.group, "enabled": value.enabled, "reason": value.reason,
                 "button": value.button}
    if not isinstance(value, dict):
        return None
    identity = value.get("id")
    rect = _rect(value.get("rect"))
    action = _action(value.get("action"))
    if not isinstance(identity, str) or not identity or len(identity) > 512 or not rect or not action:
        return None
    return Control(identity, str(value.get("label", identity))[:512], rect, action,
                   str(value.get("group", "buttons"))[:128],
                   bool(value.get("enabled", not value.get("disabled", False))),
                   str(value.get("reason", ""))[:512], bool(value.get("button", True)), layer)


def _overlay_spans(overlays, width, height):
    spans = {}
    for item in islice(overlays or (), MAX_OVERLAY_ROWS):
        if not isinstance(item, (tuple, list)) or len(item) != 3:
            continue
        y, x, row = item
        if not _integer(y) or not _integer(x) or not 0 <= y < height:
            continue
        try:
            rect = Rect(y, x, y + 1, x + L.vlen(L.row_text(row))).clip(width, height)
        except (TypeError, ValueError):
            continue
        if rect:
            spans.setdefault(y, []).append((rect, row))
    return spans


def _row_rect(rows, y, width, height, *, overlays=None, main_right=None):
    if not _integer(y) or not 0 <= y < height:
        return None
    if overlays and y in overlays:
        rect, row = overlays[y][-1]
        # The overlay border and its margins do not activate list entries.
        return Rect(y, rect.left + 1, y + 1, rect.right - 1).clip(width, height)
    if not 0 <= y < len(rows):
        return None
    text = L.row_text(rows[y])
    length = L.vlen(text)
    right = min(width, length, width if main_right is None else main_right)
    left = min(right, length - L.vlen(text.lstrip()))
    return Rect(y, left, y + 1, right).clip(width, height)


def _hit_controls(app, rows, hits, width, height, *, layer=0, spans=None):
    for hit in islice(hits or (), MAX_CONTROLS):
        if not isinstance(hit, (tuple, list)) or len(hit) != 3:
            continue
        y, kind, value = hit
        if not _integer(y) or not 0 <= y < height:
            continue
        if kind == "control" and isinstance(value, dict):
            descriptor = dict(value)
            descriptor["rect"] = (y, value.get("left"), y + 1, value.get("right"))
            control = _descriptor(descriptor, layer=layer)
            if control:
                yield control
        elif kind == "sort_header" and isinstance(value, (tuple, list)) and len(value) == 4:
            table, column, left, right = value
            rect = _rect((y, left, y + 1, right))
            if rect:
                yield Control(f"sort:{table}:{column}", str(column), rect,
                              ("click", y, left), f"sort:{table}", layer=layer)
        elif kind in BOUNDED_KINDS and isinstance(value, (tuple, list)) and len(value) == 3:
            target, left, right = value
            rect = _rect((y, left, y + 1, right))
            if rect:
                identity = str(target)
                if kind == "job_panel_action" and isinstance(target, (tuple, list)) and len(target) == 2:
                    action_kind, payload = target
                    identity = str(action_kind) + ":" + str(payload.get("id", "") if isinstance(payload, dict) else payload)
                yield Control(f"{kind}:{identity}", identity, rect, ("click", y, left),
                              kind, button=kind != "node_cell", layer=layer)
        elif kind in ROW_KINDS:
            right = None
            if getattr(app, "tab", "") in ("jobs", "history"):
                panel = getattr(app, "job_panel_rect", None)
                if panel is not None and panel.y <= y < panel.y + panel.height and panel.x:
                    right = panel.x - 1
            rect = _row_rect(rows, y, width, height, overlays=spans, main_right=right)
            if rect and getattr(app, "tab", "") in ("jobs", "history") and kind in ("job", "recent", "fin"):
                main = _pane_rect(getattr(app, "workspace_main_rect", None), width, height)
                if main and main.top <= y < main.bottom:
                    # A table row remains selectable in its leading cells.
                    # Progress text can start farther right (" wait " / --);
                    # that padding must not become the divider's drag buffer.
                    # Keep the outer structural margin free for resizing.
                    rect = Rect(rect.top, min(rect.left, main.left + 1), rect.bottom,
                                min(rect.right, main.right))
            content = getattr(app, "history_browser_content_rect", None)
            if rect and getattr(app, "tab", "") in ("analytics", "deps", "log", "research") and content is not None:
                rect = _rect((max(rect.top, content.y), max(rect.left, content.x),
                              min(rect.bottom, content.y + content.height), min(rect.right, content.x + content.width)))
            if rect:
                yield Control(f"{kind}:{value}", str(value), rect, ("row", y, rect.left),
                              kind, button=False, layer=layer)


def _toolbar_controls(app):
    state = _state(app, "toolbar_state")
    if not state:
        return
    tracks = []
    for hit in islice(state.get("hits", ()), MAX_CONTROLS):
        if not isinstance(hit, (tuple, list)) or len(hit) != 5:
            continue
        y, left, right, kind, key = hit
        rect = _rect((y, left, y + 1, right))
        if not rect:
            continue
        if kind == "track":
            tracks.append(rect)
        else:
            yield Control(f"toolbar:{kind}:{key}", str(key if kind == "menu" else kind), rect,
                          ("click", y, left), "toolbar", layer=3)
    if tracks:
        rect = Rect(tracks[0].top, min(r.left for r in tracks), tracks[-1].bottom,
                    max(r.right for r in tracks))
        yield Control("toolbar:track", "Update rate slider", rect,
                      ("click", rect.top, round(rect.center[1])), "toolbar", layer=3)
    if state.get("menu") is not None:
        disabled = state.get("menu_disabled", {})
        for hit in islice(state.get("menu_hits", ()), MAX_CONTROLS):
            if not isinstance(hit, (tuple, list)) or len(hit) != 4:
                continue
            y, left, right, key = hit
            rect = _rect((y, left, y + 1, right))
            if rect:
                reason = disabled.get(key, "")
                yield Control(f"toolbar:menu:{state['menu']}:{key}", str(key), rect,
                              ("click", y, left), "toolbar-menu", not bool(reason),
                              str(reason), layer=4)


def _modal_controls(app, rows, width, height, spans):
    mode = getattr(app, "mode", "main")
    from .job_group_menu import active as group_menu_active, controls as group_menu_controls
    if group_menu_active(app):
        yield from group_menu_controls(app)
        return
    from .history_log_export import active, controls
    if active(app):
        yield from controls(app)
        return
    state_names = {"analysis": "analysis_state", "project_runs": "project_state",
                   "project_outputs": "project_state", "project_preview": "project_state",
                   "log_compare": "log_workbench_state", "log_diff": "log_workbench_state",
                   "execution": "execution_state", "columns": "table_state",
                   "telemetry": "telemetry_state", "shell_checks": "shell_checks_state",
                   "arraymap": "array_manifest_state", "gpu_provider": "gpu_provider_state",
                   "operations": "ops_state"}
    # The log workbench uses additional mode names; only current modal data may
    # contribute controls, never a previous hidden panel's registries.
    state_name = state_names.get(mode)
    if state_name:
        yield from _hit_controls(app, rows, _state(app, state_name).get("control_hits", ()),
                                 width, height, layer=2, spans=spans)
    if mode == "main" and getattr(app, "tab", "") == "log":
        yield from _hit_controls(app, rows, _state(app, "log_workbench_state").get("control_hits", ()),
                                 width, height, layer=2, spans=spans)
    if mode == "table_tools":
        state = _state(app, "table_tools_state")
        for hit in islice(state.get("hits", ()), MAX_CONTROLS):
            if isinstance(hit, (tuple, list)) and len(hit) == 4:
                y, left, right, index = hit
                rect = _rect((y, left, y + 1, right))
                if rect:
                    yield Control(f"table-tools:{state.get('modal')}:{index}", str(index), rect,
                                  ("click", y, left), "table-tools", layer=2)
    if mode == "confirm":
        for hit in islice(_state(app, "command_state").get("confirm_hits", ()), MAX_CONTROLS):
            if isinstance(hit, (tuple, list)) and len(hit) == 4:
                y, left, right, choice = hit
                rect = _rect((y, left, y + 1, right))
                if rect:
                    yield Control(f"confirm:{choice}", str(choice), rect, ("click", y, left),
                                  "confirmation", layer=2)
    row_registries = []
    if mode == "palette":
        row_registries.append(("palette", _state(app, "command_state").get("result_hits", ())))
    navigation = _state(app, "navigation_tools_state")
    if mode in ("jump_picker", "locations_picker", "bindings_editor", "settings_editor"):
        row_registries.append(("navigation:" + mode, navigation.get("hits", ())))
    for prefix, registry in row_registries:
        for hit in islice(registry, MAX_CONTROLS):
            if isinstance(hit, (tuple, list)) and len(hit) == 2:
                y, index = hit
                rect = _row_rect(rows, y, width, height, overlays=spans)
                if rect:
                    yield Control(f"{prefix}:{index}", str(index), rect, ("row", y, rect.left),
                                  prefix, button=False, layer=2)
    if mode == "session_inbox":
        registry = _state(app, "session_tools_state").get("mouse_rows", {})
        prefix = "inbox"
    elif mode == "exports":
        registry = getattr(getattr(app, "activity", None), "mouse_rows", {})
        prefix = "export"
    elif mode in ("log_tools_results", "log_tools_marks"):
        registry = _state(app, "log_tools_state").get("mouse_rows", {})
        prefix = mode
    else:
        registry, prefix = {}, ""
    for y, value in islice(registry.items(), MAX_CONTROLS):
        if isinstance(value, (tuple, list)) and len(value) == 3:
            identity, left, right = value
            rect = _rect((y, left, y + 1, right))
            if rect:
                yield Control(f"{prefix}:{identity}", str(identity), rect,
                              ("row", y, left), prefix, button=False, layer=2)


def _pane_rect(value, width, height):
    if value is None:
        return None
    parts = tuple(getattr(value, name, None) for name in ("y", "x", "height", "width"))
    if not all(_integer(item) for item in parts):
        return None
    y, x, h, w = parts
    return Rect(y, x, y + h, x + w).clip(width, height)


def _regions(app, width, height):
    """Use painted pane bounds, rather than the center of a wide table row."""
    if getattr(app, "mode", "main") != "main":
        return (("modal", Rect(0, 0, height, width)),)
    tab = getattr(app, "tab", "")
    browser = _state(app, "history_browser_state").get("frame")
    browsing = (tab in ("analytics", "deps", "log", "research") and isinstance(browser, dict)
                and browser.get("tab") == tab and browser.get("mode") == "main"
                and browser.get("geometry") == (getattr(app, "width", None), getattr(app, "height", None)))
    body = (_pane_rect(getattr(app, "history_browser_content_rect", None), width, height)
            if browsing else None)
    body = body or Rect(max(0, getattr(app, "body_origin", 0)), 0, max(0, height - 1), width)
    regions = []
    if browsing:
        rect = _pane_rect(getattr(app, "history_browser_rect", None), width, height)
        if rect:
            regions.append(("page:history", rect))
    dividers = _state(app, "pane_drag_state").get("dividers", {})
    # Research and Logs render their own native documents. A Main rectangle
    # retained from Jobs must not invent a hidden Details pane on those pages.
    workspace = tab in ("jobs", "history") or "workspace:" + tab in dividers
    main = _pane_rect(getattr(app, "workspace_main_rect", None), width, height) if workspace else None
    panel = _pane_rect(getattr(app, "job_panel_rect", None), width, height) if tab in ("jobs", "history") else None
    layout = getattr(app, "layout_state", None)
    has_details = "details" in getattr(layout, "available", ())
    if main and body.top <= main.top and main.bottom <= body.bottom and body.left <= main.left and main.right <= body.right:
        if panel:
            regions.append(("page:details", panel))
        elif has_details and main.right < body.right:
            regions.append(("page:details", Rect(body.top, main.right + 1, body.bottom, body.right)))
        elif has_details and main.bottom < body.bottom:
            regions.append(("page:details", Rect(main.bottom + 1, body.left, body.bottom, body.right)))
        regions.append(("page:main", main))
    elif panel:
        regions.append(("page:details", panel))
    # The broader content rectangle is deliberately last; native split bounds
    # win, and pages without a workspace still have a stable content domain.
    regions.append(("page:main", body))
    return tuple((name, rect) for name, rect in regions if rect.top < rect.bottom and rect.left < rect.right)


def _recover_focus(app, previous, graph, current):
    """Reanchor a removed row within its current table, never activate it."""
    if not previous or not current or previous.scope != graph.scope:
        return None
    if not current.button and current.group in ("job", "recent", "fin"):
        migrated = next((control for control in graph.controls if control.enabled and not control.button
                         and control.group in ("job", "recent", "fin") and control.label == current.label
                         and graph.region(control) == previous.region(current)), None)
        if migrated:
            return migrated
    candidates = [control for control in graph.controls if control.enabled
                  and control.group == current.group and graph.region(control) == previous.region(current)]
    if not candidates:
        return None
    selected = str(getattr(app, "selected_id", ""))
    return next((control for control in candidates if not control.button and control.label == selected),
                min(candidates, key=lambda control: (abs(control.rect.center[0] - current.rect.center[0]),
                                                       abs(control.rect.center[1] - current.rect.center[1]),
                                                       control.rect.top, control.rect.left, control.id)))


def publish(app, rows, hits, width, height, overlays=None, extra_controls=()):
    """Publish one immutable graph from the final visible frame's metadata.

    ``rows`` contain normal display rows; ``overlays`` contain absolute
    ``(y, x, row)`` triples. Explicit controls may be ``Control`` instances or
    dictionaries with ``id, rect, action, group, label``. Existing three-item
    hits and modal registries are adapted without invoking any renderer.
    """
    state = initialize(app)
    hits = tuple(islice(hits or (), MAX_CONTROLS))
    state["published_hits"] = hits
    state["published_hit_token"] = hit_token(hits)
    width = max(0, width) if _integer(width) else 0
    height = max(0, height) if _integer(height) else len(rows)
    spans = _overlay_spans(overlays, width, height)
    toolbar_state = _state(app, "toolbar_state")
    menu_rect = _rect(toolbar_state.get("menu_rect")) if toolbar_state.get("menu") is not None else None
    candidates = []
    if getattr(app, "mode", "main") == "main":
        candidates.extend(_hit_controls(app, rows, hits, width, height))
        for hit in islice(getattr(app, "tab_hits", ()), MAX_CONTROLS):
            if isinstance(hit, (tuple, list)) and len(hit) == 4:
                y, left, right, key = hit
                rect = _rect((y, left, y + 1, right))
                if rect:
                    candidates.append(Control(f"tab:{key}", str(key), rect, ("click", y, left), "tabs"))
        chip_y = getattr(app, "table_chip_y", None)
        chips = getattr(app, "table_chip_cells", ("", ()))
        if _integer(chip_y) and isinstance(chips, (tuple, list)) and len(chips) == 2:
            for chip in islice(chips[1], MAX_CONTROLS):
                if isinstance(chip, (tuple, list)) and len(chip) == 3:
                    key, left, right = chip
                    rect = _rect((chip_y, left, chip_y + 1, right))
                    if rect:
                        candidates.append(Control(f"filter-chip:{chips[0]}:{key}", str(key), rect,
                                                  ("click", chip_y, left), "filter-chips"))
    candidates.extend(_modal_controls(app, rows, width, height, spans))
    candidates.extend(_toolbar_controls(app))
    for descriptor in islice(extra_controls or (), MAX_CONTROLS):
        control = _descriptor(descriptor, layer=2 if spans else 0)
        if control:
            candidates.append(control)
    visible, identities, primary_rects = [], set(), {}
    for control in candidates:
        if len(visible) >= MAX_CONTROLS:
            break
        rect = control.rect.clip(width, height)
        if not rect:
            continue
        if menu_rect is not None and control.layer < 4 and rect.intersects(menu_rect):
            continue
        identity = control.id
        if identity in identities:
            # Inline cited-file links intentionally repeat their native action
            # on each wrapped line. Keep each painted fragment selectable;
            # ordinary duplicate semantic metadata still contributes one node.
            if identity.startswith("job_panel_action:") and primary_rects.get(identity) != rect:
                identity += f":fragment:{rect.top}:{rect.left}:{rect.right}"
            if identity in identities:
                continue
        # A dropdown masks controls directly underneath its painted rectangle.
        # Generic modals omit all underlying controls even beyond their border.
        if control.layer == 0 and any(rect.intersects(span)
                                      for y in range(rect.top, rect.bottom)
                                      for span, _ in spans.get(y, ())):
            continue
        action = control.action
        if action[0] in ("click", "row") and len(action) == 3:
            action = (action[0], max(rect.top, min(rect.bottom - 1, action[1])),
                      max(rect.left, min(rect.right - 1, action[2])))
        visible.append(Control(identity, control.label, rect, action, control.group,
                               control.enabled, control.reason, control.button, control.layer))
        identities.add(identity)
        primary_rects.setdefault(control.id, rect)
    token = _context(app)
    geometry = (getattr(app, "width", None), getattr(app, "height", None))
    visible = tuple(visible)
    graph = previous = state["graph"]
    previous_focus = previous.get(state["focused"]) if previous else None
    regions, scope = _regions(app, width, height), _scope(app)
    if (graph is None or graph.token != token or graph.width != width or graph.height != height
            or graph.observed_geometry != geometry or graph.controls != visible or graph.regions != regions):
        state["generation"] += 1
        graph = Graph(visible, token, width, height, state["generation"], geometry, regions, scope)
    state["graph"] = graph
    state["frame_required"] = False
    focused = graph.get(state["focused"])
    if focused is None or not focused.enabled:
        recovered = _recover_focus(app, previous, graph, previous_focus) if state["active"] else None
        state["focused"] = recovered.id if recovered else None
        state["active"] = bool(recovered)
        if recovered:
            _bind_row(app, recovered)
    elif previous and previous.scope != scope:
        # The same incidental ID on another page/modal is not the same focus.
        state["focused"], state["active"] = None, False
    pointer = state["pointer"]
    hovered = graph.at(*pointer) if pointer else None
    state["hovered"] = hovered.id if hovered else None
    if state.get("pending_focus"):
        state["pending_focus"] = None
        _focus(app)
    return graph


def nearest(graph: Graph, current: Control, direction: str):
    """Find a directional neighbor in O(n), without a quadratic edge matrix.

    Same-row/column controls win. Aligned controls in the same semantic group
    then win over diagonal shortcuts into a different pane. Stable screen
    order breaks ties, including controls packed into wrapped button rows.
    """
    if direction not in ("left", "right", "up", "down"):
        return None
    vertical = direction in ("up", "down")
    sign = -1 if direction in ("left", "up") else 1
    cy, cx = current.rect.center
    region = graph.region(current)
    best = None
    # Table rows span every column. A header to the right of their center is
    # still in the same pane; it must not steal a requested pane crossing.
    row_crossing = (not current.button or current.group == "job-history" and ":job:" in current.id) and direction in ("left", "right")
    pane_candidates = []
    # At the left edge of Details, return to the exact selected table row.
    # Otherwise a short header fragment near the top could win over Recents.
    if direction == "left" and region == "page:details":
        siblings = [control for control in graph.controls if control.enabled
                    and graph.region(control) == region
                    and control.rect.top < current.rect.bottom and current.rect.top < control.rect.bottom
                    and control.rect.center[1] < cx]
        if not siblings:
            pane_candidates = [control for control in graph.controls if control.enabled
                               and not control.button and graph.region(control) == "page:main"]
            if pane_candidates:
                selected = graph.token[2] if len(graph.token) > 2 else None
                return next((control for control in pane_candidates if control.label == selected),
                            min(pane_candidates, key=lambda control: (abs(control.rect.center[0] - cy),
                                                                     control.rect.top, control.rect.left, control.id)))
    if row_crossing and region:
        bounds = dict(reversed(graph.regions))
        wanted = "page:details" if region == "page:main" and direction == "right" and "page:details" in bounds else "page:main" if region == "page:details" and direction == "left" else None
        if wanted is None and region in ("page:main", "page:history") and "page:history" in bounds:
            other = "page:history" if region == "page:main" else "page:main"
            delta = bounds[other].center[1] - bounds[region].center[1]
            if delta * sign > 0 or delta == 0 and region == "page:history" and direction == "left":
                wanted = other
        if wanted:
            pane_candidates = [control for control in graph.controls if control.enabled and graph.region(control) == wanted]
    if pane_candidates:
        if region == "page:main" and direction == "right" and len(graph.token) > 10:
            active_tab = graph.get("job_panel_tab:" + str(graph.token[10]))
            if active_tab in pane_candidates:
                return active_tab
        return min(pane_candidates, key=lambda control: (abs(control.rect.center[0] - cy),
                                                         abs(control.rect.center[1] - cx),
                                                         control.rect.top, control.rect.left, control.id))
    for candidate in graph.controls:
        if candidate.id == current.id or not candidate.enabled:
            continue
        ny, nx = candidate.rect.center
        forward = (ny - cy if vertical else nx - cx) * sign
        if forward <= 0:
            continue
        orthogonal = abs(nx - cx if vertical else ny - cy)
        overlap = (current.rect.left < candidate.rect.right and candidate.rect.left < current.rect.right
                   if vertical else current.rect.top < candidate.rect.bottom and candidate.rect.top < current.rect.bottom)
        # A row's own cells are a single semantic target, not independent
        # column controls. Only inline controls on the same painted row apply.
        if row_crossing and graph.region(candidate) == region and not overlap:
            continue
        same_group = candidate.group == current.group
        same_region = bool(region and graph.region(candidate) == region)
        rank = (0 if same_region else 1 if region else 0,
                0 if overlap else 1, 0 if same_group else 1,
                forward + 2 * orthogonal, orthogonal, forward,
                candidate.rect.top, candidate.rect.left, candidate.id)
        if best is None or rank < best[0]:
            best = rank, candidate
    return best[1] if best else None


def needs_frame(app):
    """Stop a key burst before another event can use displaced row geometry."""
    return bool(initialize(app)["frame_required"])


def _bind_row(app, control):
    """Bind explicit job-row focus using existing published real identities."""
    if (getattr(app, "mode", "main") != "main" or getattr(app, "tab", "") not in ("jobs", "history")
            or control.button or control.group not in ("job", "recent", "fin")):
        return False
    tab = getattr(app, "tab", "jobs")
    ids = ((getattr(app, "last_history_ids", ()) or ()) if tab == "history" else
           getattr(app, "visible_ids", ()) if control.group == "job" else getattr(app, "recent_ids", ()))
    if control.label not in ids or not isinstance(getattr(app, "cursor", None), dict):
        return False
    index = ids.index(control.label) + (len(getattr(app, "visible_ids", ())) if control.group == "recent" else 0)
    changed = getattr(app, "selected_id", None) != control.label or app.cursor.get(tab) != index
    from .job_selection import resume
    resume(app, tab)
    app.cursor[tab], app.selected_id = index, control.label
    layout = getattr(app, "layout_state", None)
    if layout:
        layout.focus = "main"
    panel = _state(app, "job_panel_state")
    panel["focus"] = ""
    if changed:
        initialize(app)["frame_required"] = True
    return changed


def _advance_row(app, graph, current, direction):
    """At a table viewport edge, admit the next actual scheduler row."""
    if (direction not in ("up", "down") or current.button or current.group not in ("job", "recent", "fin")
            or getattr(app, "mode", "main") != "main" or getattr(app, "tab", "") not in ("jobs", "history")
            or not callable(getattr(app, "move", None))):
        return False
    cy = current.rect.center[0]
    sign = 1 if direction == "down" else -1
    if any(control.enabled and control.group == current.group and graph.region(control) == graph.region(current)
           and (control.rect.center[0] - cy) * sign > 0 for control in graph.controls):
        return False
    _bind_row(app, current)
    old = getattr(app, "selected_id", None)
    from .recent_history import navigate
    tab = getattr(app, "tab", "jobs")
    if tab != "jobs" or not navigate(app, direction):
        app.move(direction)
        ids = ((getattr(app, "last_history_ids", ()) or ()) if tab == "history" else
               list(getattr(app, "visible_ids", ())) + list(getattr(app, "recent_ids", ())))
        cursor = getattr(app, "cursor", {}).get(tab, 0)
        if 0 <= cursor < len(ids):
            app.selected_id = ids[cursor]
    selected = getattr(app, "selected_id", None)
    if selected == old or not selected:
        return False
    kind = "fin" if tab == "history" else "job" if selected in getattr(app, "visible_ids", ()) else "recent"
    state = initialize(app)
    state["focused"], state["active"], state["frame_required"] = kind + ":" + selected, True, True
    return True


def _focus(app, enabled=True):
    state = initialize(app)
    graph = _current(app)
    if not enabled:
        state["active"] = False
        state["pending_focus"] = None
        return True
    if graph is None:
        state["pending_focus"] = True
        return True
    target = graph.get(state["hovered"]) or graph.get(state["focused"])
    if target is None or not target.enabled:
        target = next((control for control in graph.controls if control.enabled and control.button),
                      next((control for control in graph.controls if control.enabled), None))
    if target is None:
        return False
    state["focused"], state["active"] = target.id, True
    # F8 explicitly transfers keyboard ownership from a clicked job browser.
    # Its native arrow handler runs before this graph in the shared workbench.
    browser = _state(app, "history_browser_state")
    browser["focused"] = False
    _bind_row(app, target)
    return True


def run_command(app, args):
    if not args or args[0] != "focusbuttons":
        return False
    if len(args) > 2 or len(args) == 2 and args[1] not in ("on", "off"):
        if hasattr(app, "fail"):
            app.fail("focusbuttons [on|off]")
        return True
    enabled = args[1] != "off" if len(args) == 2 else not initialize(app)["active"]
    if not _focus(app, enabled) and hasattr(app, "say"):
        app.say("No visible controls yet; draw this page and press F8.")
    elif hasattr(app, "say"):
        app.say("Buttons: arrows move; Enter activates; Esc returns to content." if enabled else "Content keys restored.")
    return True


def _activate(app, control, *, keyboard=False):
    state = initialize(app)
    graph = _current(app)
    if graph is None or graph.get(control.id) != control or state["routing"]:
        return False
    if not control.enabled:
        if hasattr(app, "say") and control.reason:
            app.say(control.reason)
        return True
    action = control.action
    previous_active = state["active"]
    state["routing"], state["active"] = True, False
    try:
        if action[0] in ("click", "row") and len(action) == 3:
            app.click(action[1], action[2], getattr(app, "last_hits", ()))
            if keyboard and action[0] == "row" and getattr(app, "mode", "main") == graph.token[0]:
                app.handle("enter")
        elif action[0] == "command" and len(action) == 2 and isinstance(action[1], str):
            app.run_command(action[1])
        elif action[0] == "key" and len(action) == 2 and isinstance(action[1], str):
            app.handle(action[1])
        elif action[0] == "set" and len(action) == 3 and action[1] in ("nodes_view", "analytics_view", "research_view"):
            setattr(app, action[1], action[2])
        elif action[0] == "scrollbar" and len(action) == 3:
            from .scrollbars import activate
            return activate(app, action[1], action[2])
        else:
            return False
    finally:
        state["routing"] = False
        state["active"] = previous_active and graph.token == _context(app)
        if previous_active and control.button and graph.token != _context(app) and getattr(app, "mode", "main") == graph.token[0]:
            state["pending_focus"] = True
    return True


def handle_key(app, key):
    state = initialize(app)
    if state["routing"]:
        return False
    if (getattr(app, "mode", "main") == "operations"
            and (_state(app, "ops_state").get("editing") is not None
                 or _state(app, "ops_state").get("catalog_search"))):
        # Fields and catalog search own their text keys. Returning focus to
        # the graph here would turn Home/Enter or typed shortcuts into actions.
        state["active"] = False
        state["pending_focus"] = None
        return False
    if key == "f8":
        _focus(app, not state["active"])
        return True
    if not state["active"]:
        return False
    if key == "esc":
        state["active"] = False
        state["pending_focus"] = None
        if getattr(app, "mode", "main") == "main" and getattr(app, "tab", "") in ("jobs", "history"):
            # Clicking a Details button starts graph traversal and native tab
            # focus together. One Escape releases both owners, so Jobs arrows
            # do not unexpectedly continue cycling Details after graph exit.
            from .job_panels import focus_main
            focus_main(app)
        return True
    graph = _current(app)
    if graph is None:
        if state["frame_required"] and state.get("graph") and state["graph"].scope == _scope(app):
            # The terminal loop paints before the next queued event. Small
            # embedders that call again sooner still cannot activate old data.
            if key in ("up", "down", "left", "right", "home", "end", "tab", "btab", "enter", "space"):
                return True
        state["active"] = False
        return False
    current = graph.get(state["focused"])
    if current is None:
        state["active"] = False
        return False
    target = None
    if key in ("up", "down", "left", "right"):
        if _advance_row(app, graph, current, key):
            return True
        target = nearest(graph, current, key)
    elif key in ("home", "end", "tab", "btab"):
        ordered = graph._ordered
        if ordered:
            index = graph._order_indices.get(current.id, 0)
            index = 0 if key == "home" else len(ordered) - 1 if key == "end" else (index + (1 if key == "tab" else -1)) % len(ordered)
            target = ordered[index]
    elif key in ("enter", "space"):
        return _activate(app, current, keyboard=True)
    elif key == "p" and getattr(app, "mode", "main") == "operations":
        from .ops_ui import pin_focused
        return pin_focused(app, current)
    else:
        # Typing a content shortcut exits graph mode and reaches its existing
        # handler. No hidden keymap or chart/log selection mode is replaced.
        state["active"] = False
        return False
    if target:
        state["focused"] = target.id
        _bind_row(app, target)
    return True


def handle_mouse(app, y, x, button="left", shift=False, **kwargs):
    state = initialize(app)
    if state["routing"]:
        return False
    if not _integer(y) or not _integer(x):
        return False
    state["pointer"] = (y, x)
    graph = _current(app)
    target = graph.at(y, x) if graph else None
    state["hovered"] = target.id if target else None
    if button in ("motion", "drag", "release", "press"):
        # Captured slider / job selection motion still reaches its owner.
        return False
    if button in ("wheel-up", "wheel-down", "wheel_up", "wheel_down"):
        state["active"] = False
        return False
    if button != "left" or shift:
        return False
    if target is None or not target.button:
        state["active"] = False
        return False
    state["focused"], state["active"] = target.id, True
    return _activate(app, target)


@lru_cache(maxsize=512)
def _feedback_style(style, feedback):
    # Native cursor selection retains its semantic text and background. Hover
    # and keyboard focus add accents without making the selected job resemble
    # an ordinary hovered row. Explicit yank ranges are protected by decorate.
    source = style.split("+")
    if "sel" in source:
        accents = [token for token in feedback.split("+") if token in ("under", "bold")]
        return "+".join(dict.fromkeys(source + accents))
    # Other controls keep their semantic foreground while replacing their
    # ordinary background and removing dim for readable feedback in each theme.
    tokens = [token for token in style.split("+") if token not in ("dim", "sel", "rev", "under", "bold") and not token.startswith("bg:")]
    return "+".join(tokens + feedback.split("+"))


def _decorate_row(row, ranges):
    if all(text.isascii() for text, _ in row):
        output, position = [], 0
        for text, style in row:
            end = position + len(text)
            edges = sorted({position, end} | {max(position, min(end, edge))
                           for left, right, _ in ranges for edge in (left, right)})
            for left, right in zip(edges, edges[1:]):
                feedback = next((feedback for start, stop, feedback in reversed(ranges)
                                 if left < stop and right > start), None)
                painted_style = _feedback_style(style, feedback) if feedback else style
                fragment = text[left - position:right - position]
                if output and output[-1][1] == painted_style:
                    output[-1] = (output[-1][0] + fragment, painted_style)
                else:
                    output.append((fragment, painted_style))
            position = end
        return output
    output, position, previous_style, pieces = [], 0, "", []
    for text, style in row:
        for char in text:
            size = L.vlen(char)
            # A wide character intersecting a region receives one whole style;
            # combining marks inherit its base. Never split a grapheme's cells.
            feedback = next((feedback for left, right, feedback in reversed(ranges)
                             if position < right and position + size > left), None) if size else None
            new_style = _feedback_style(style, feedback) if feedback else previous_style if not size and (pieces or output) else style
            if pieces and previous_style != new_style:
                output.append(("".join(pieces), previous_style))
                pieces = []
            pieces.append(char)
            previous_style = new_style
            position += size
    if pieces:
        output.append(("".join(pieces), previous_style))
    return output


def _targets(app):
    state = initialize(app)
    graph = _current(app)
    if graph is None:
        return ()
    targets = []
    hovered = graph.get(state["hovered"])
    focused = graph.get(state["focused"]) if state["active"] else None
    if hovered:
        targets.append((hovered, POINTER_STYLE))
    if focused:
        targets.append((focused, FOCUS_STYLE))
    return targets


def feedback_rows(app):
    """Visible physical rows affected by the current pointer or button focus.

    A painter can repaint the union of the previous and new sets to erase old
    feedback and draw its replacement without rebuilding the document.
    """
    return frozenset(y for control, _ in _targets(app)
                     for y in range(control.rect.top, control.rect.bottom))


def decorate(app, rows, origin=(0, 0)):
    """Style controls while preserving the explicit screen-line yank range."""
    targets = _targets(app)
    if not targets:
        return rows
    y0, x0 = origin
    output = list(rows)
    indices = {y - y0 for control, _ in targets
               for y in range(max(y0, control.rect.top), min(y0 + len(rows), control.rect.bottom))}
    for index in sorted(indices):
        y = index + y0
        anchor, end = getattr(app, "sel_anchor", None), getattr(app, "sel_end", None)
        if _integer(anchor) and _integer(end) and min(anchor, end) <= y <= max(anchor, end):
            # Explicit yank selection has stronger visual precedence than a
            # passive pointer or arrow-focus background. Native table cursors
            # still receive normal feedback outside this selected line range.
            # Menus retain their own feedback in decorate_overlays below.
            continue
        ranges = [(control.rect.left - x0, control.rect.right - x0, style)
                  for control, style in targets if control.rect.top <= y < control.rect.bottom]
        if ranges:
            output[index] = _decorate_row(rows[index], ranges)
    return output


def decorate_overlays(app, overlays):
    """Apply identical feedback to absolute overlay triples."""
    if overlays is None:
        return None
    targets = _targets(app)
    if not targets:
        return overlays
    output = []
    for y, x, row in overlays:
        ranges = [(control.rect.left - x, control.rect.right - x, style)
                  for control, style in targets if control.rect.top <= y < control.rect.bottom]
        output.append((y, x, _decorate_row(row, ranges) if ranges else row))
    return output
