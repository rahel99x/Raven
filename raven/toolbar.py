"""Compact navigation, task-oriented setup, and persistent view favorites.

Menus retain the underlying modal and execute nothing while browsing. Every
item delegates to an existing command, opens an editable command prompt, or
performs a local presentation action. Hit rectangles come from the last paint.
"""
from __future__ import annotations

from . import layout as L
from .command_registry import Control as Item, toolbar_controls

from .pointer_routing import capture_handler

MENUS = ("File", "Edit", "View", "Setup", "Help")
BAR_STYLE = "text+bg:surface-raised"
PRIMARY_TABS = ("jobs", "history", "analytics", "research", "log")
MAX_FAVORITES = 24
SUBMENUS = {"connections": "Connections and accounts", "recordings": "Recordings and metrics",
            "performance": "Refresh and performance", "installation": "Installation and diagnostics",
            "infrastructure": "Cluster and diagnostics", "research": "Research workspaces",
            "analytics": "Analytics views", "favorites": "Favorite views", "advanced": "Advanced preferences",
            "display": "Display and navigation"}


def initialize(app):
    state = getattr(app, "toolbar_state", None)
    if not isinstance(state, dict):
        state = dict(menu=None, cursor=0, top=0, page=1, focus="", dragging=False,
                     panel=None, panel_scroll=0, hits=[], menu_hits=[], width=0,
                     bar_y=0, painted_menu=None, menu_token=None, menu_rect=None,
                     menu_source="", menu_disabled={}, hover=None, pressed=False,
                     drag_width=None)
        app.toolbar_state = state
    # Public state may have been constructed by a previous renderer or a test.
    for key, value in (("menu_rect", None), ("menu_source", ""), ("menu_disabled", {}),
                       ("hover", None), ("pressed", False), ("drag_width", None),
                       ("submenu", None), ("menu_stack", []), ("favorites", [])):
        state.setdefault(key, value)
    return state


def _destinations():
    from .views import TABS, ANALYTICS_VIEWS
    from .research import RESEARCH_VIEWS
    return {**{"tab:" + key: (title, ("tab " + key,)) for key, title in TABS},
            **{"research:" + key: ("Research: " + title, ("workspace " + key,)) for key, title in RESEARCH_VIEWS},
            **{"analytics:" + key: ("Analytics: " + title.title(), ("tab analytics", "view " + key)) for key, title in ANALYTICS_VIEWS}}


def _valid_favorites(values):
    known = _destinations()
    return list(dict.fromkeys(value for value in values[:MAX_FAVORITES]
                             if isinstance(value, str) and value in known)) if isinstance(values, (list, tuple)) else []


def restore(app, data):
    initialize(app)["favorites"] = _valid_favorites(data.get("favorites")) if isinstance(data, dict) else []


def save(app):
    # Only destination identities persist. No selected run, path, or account.
    return {"favorites": _valid_favorites(initialize(app)["favorites"])}


def visible_tabs(app):
    """A small default set, favorites, and the current destination, in O(n)."""
    from .views import TABS
    favorites = initialize(app)["favorites"]
    selected = set(PRIMARY_TABS)
    selected.update(value.split(":", 1)[1] for value in favorites if value.startswith("tab:"))
    selected.add(getattr(app, "tab", "jobs"))
    # Plugin tabs remain discoverable without requiring a previously saved pin.
    selected.update(key for key, _ in TABS if key not in
                    ("jobs", "history", "analytics", "research", "log", "cluster", "nodes", "group", "deps", "sources"))
    names = dict(TABS)
    order = list(dict.fromkeys(PRIMARY_TABS + tuple(key for key, _ in TABS if key in selected)))
    return [(key, names[key]) for key in order if key in names]


def visible_research_views(app):
    from .research import RESEARCH_VIEWS
    selected = {"experiment", "arrays", "evidence", "artifacts", getattr(app, "research_view", "experiment")}
    selected.update(value.split(":", 1)[1] for value in initialize(app)["favorites"] if value.startswith("research:"))
    return [(key, title) for key, title in RESEARCH_VIEWS if key in selected]


def _destination(app):
    tab = getattr(app, "tab", "jobs")
    if tab == "research":
        return "research:" + getattr(app, "research_view", "experiment")
    if tab == "analytics":
        return "analytics:" + getattr(app, "analytics_view", "job")
    return "tab:" + tab


def _toggle_favorite(app):
    state = initialize(app)
    target = _destination(app)
    if target not in _destinations():
        app.fail("This page does not provide a saved navigation destination.")
        return
    pins = _valid_favorites(state["favorites"])
    if target in pins:
        pins.remove(target)
        message = "Removed this view from Favorites."
    elif len(pins) >= MAX_FAVORITES:
        app.fail("Favorites is full. Unpin a view first.")
        return
    else:
        pins.append(target)
        message = "Pinned this view in View > Favorites."
    state["favorites"] = pins
    if callable(getattr(app, "save", None)):
        app.save()
    app.say(message)


def _polling_label(app, value=None, *, ascii_=None):
    from . import refresh_rate
    from .metric_sampling import format_interval
    if ascii_ is None:
        cfg = getattr(app, "cfg", None)
        ascii_ = initialize(app).get("ascii", cfg.get("ascii", False) if cfg else False)
    seconds = (refresh_rate.cadence(app, "jobs") if value is None else
               refresh_rate.candidate_cadence(app, "jobs", value))
    return format_interval(seconds, ascii_=ascii_)


def command_names():
    return ["menu", "about", "preferences", "favorite"]


def run_command(app, args):
    if not args or args[0] not in command_names():
        return False
    if args[0] == "favorite" and len(args) <= 2 and (len(args) == 1 or args[1] in ("toggle", "list", "clear")):
        action = args[1] if len(args) == 2 else "toggle"
        if action == "toggle":
            _toggle_favorite(app)
        elif action == "list":
            _open(app, MENUS.index("View"))
            _open_submenu(app, "favorites")
        else:
            initialize(app)["favorites"] = []
            if callable(getattr(app, "save", None)):
                app.save()
            app.say("Navigation favorites cleared; job groups and saved locations are unchanged.")
    elif args[0] == "preferences" and len(args) == 1:
        _open(app, MENUS.index("Setup"))
        _open_submenu(app, "advanced")
    elif args[0] == "about" and len(args) == 1:
        _close(app)
        initialize(app).update(panel="about", panel_scroll=0)
    elif args[0] == "menu" and (len(args) == 1 or len(args) == 2 and args[1].title() in MENUS):
        _open(app, MENUS.index(args[1].title()) if len(args) == 2 else 0)
    else:
        app.fail({"favorite": "Use favorite [toggle|list|clear].",
                  "preferences": "Use preferences without arguments.",
                  "about": "Use about without arguments.",
                  "menu": "Use menu [File|Edit|View|Setup|Help]."}[args[0]])
    return True


def menu_items(app, menu):
    """Stable item identities; context changes are checked again on activation."""
    if isinstance(menu, int):
        menu = MENUS[menu % len(MENUS)]
    state = initialize(app)
    if state["menu"] is not None and MENUS[state["menu"]] == menu and state["submenu"]:
        return [Item("submenu-back", "Back", local="submenu-back")] + submenu_items(app, state["submenu"])
    if menu == "File":
        return toolbar_controls("File")
    if menu == "Edit":
        from .editor_yank import mode as copy_mode
        array_attached = bool(getattr(app, "array_manifest_state", {}).get("manifest"))
        return toolbar_controls("Edit", {
            "copy-destination": {"label": "Switch to clipboard copying" if copy_mode(app) == "yank" else "Switch to Vim/Neovim yanking"},
            "array-map": {"command": "arraymap inspect" if array_attached else "arraymap"},
        })
    if menu == "View":
        from .views import TABS
        return ([Item("tab-" + key, title, "tab " + key) for key, title in TABS if key in PRIMARY_TABS] +
                [Item("run-home", "Run home", "runhome"),
                 Item("research-views", "Research workspaces", local="submenu:research"),
                 Item("analytics-views", "Analytics views", local="submenu:analytics"),
                 Item("infrastructure", "Cluster and diagnostics", local="submenu:infrastructure"),
                 Item("favorites", "Favorites", local="submenu:favorites"),
                 Item("pinned-tools", "Pinned tools", "workbench pinned"),
                 Item("display", "Display and navigation", local="submenu:display")])
    if menu == "Setup":
        return toolbar_controls("Setup")
    return toolbar_controls("Help")


def submenu_items(app, name):
    """Nested menus reuse existing commands and never collect data while painting."""
    if name in ("connections", "recordings", "performance", "installation"):
        return toolbar_controls(name)
    if name == "infrastructure":
        from .views import TABS
        return [Item("tab-" + key, title, "tab " + key) for key, title in TABS if key not in PRIMARY_TABS]
    if name == "research":
        from .research import RESEARCH_VIEWS
        return [Item("research-" + key, title, "workspace " + key) for key, title in RESEARCH_VIEWS]
    if name == "analytics":
        from .views import ANALYTICS_VIEWS
        return [Item("analytics-" + key, title.title(), local="destination:analytics:" + key) for key, title in ANALYTICS_VIEWS]
    if name == "favorites":
        known = _destinations()
        pinned = _destination(app) in initialize(app)["favorites"]
        return [Item("favorite-toggle", "Unpin this view" if pinned else "Pin this view", "favorite")] + [
            Item("favorite-" + key, known[key][0], local="destination:" + key)
            for key in _valid_favorites(initialize(app)["favorites"])] + [
                Item("pinned-tools", "Pinned tools", "workbench pinned")]
    all_items = toolbar_controls("View", {
        "freeze": {"label": "Resume inspection" if getattr(app, "table_tools_state", {}).get("freeze") else "Pause inspection (sampling continues)"}})
    if name == "display":
        keys = {"back", "forward", "jump", "comfortable", "compact", "focused", "focus-main", "focus-details", "terminal", "terminal-details", "terminal-tools", "focus-buttons", "maximize", "layouts", "freeze", "follow", "wrap"}
        return [item for item in all_items if item.key in keys] + [Item("preferences", "Advanced preferences", local="submenu:advanced")]
    if name == "advanced":
        from .startup import enabled as startup_enabled
        from .scrolling import enabled as smoothscroll_enabled
        from .worker_ui import status as worker_status
        from .palette import THEME_NAMES
        workers = worker_status(app)
        desired = workers["target"] if workers else "multi"
        toggle = "Background workers: switch to " + ("multi" if desired == "single" else "single")
        if workers and workers["pending"]:
            toggle += " (switch pending)"
        controls = toolbar_controls("View", {
            "workers-toggle": {"label": toggle},
            "smooth-scroll": {"label": "Disable smooth scrolling" if smoothscroll_enabled(app) else "Enable smooth scrolling"},
            "startup-toggle": {"label": "Disable startup animation" if startup_enabled(app) else "Enable startup animation"}})
        keys = {"workers-toggle", "workers-single", "workers-multi", "workers-status", "smooth-scroll", "startup-toggle", "startup-preview", "reader"}
        return [Item("settings", "Terminal, key, and clipboard preferences", "settings")] + [item for item in controls if item.key in keys] + [
            Item("theme-" + name, "Theme: " + name.replace("-", " ").title(), "theme " + name) for name in THEME_NAMES if name != "reader"]
    return []


def _blocked(app, item):
    pending_execution = getattr(app, "execution_state", {}).get("pending_action")
    pending_allocation = getattr(app, "raven_ui_state", {}).get("review")
    if (getattr(app, "mode", "main") == "confirm" or pending_execution or pending_allocation) and item.local not in ("quit", "about", "rate", "workers"):
        return "Close the pending job review first."
    if item.context == "workers":
        from .worker_ui import status
        workers = status(app)
        if workers is None:
            return "Background worker controls are unavailable in this session."
        if workers["closed"] and item.command != "workers status":
            return "Background workers are stopped."
    tab = getattr(app, "tab", "")
    if item.context == "log" and (tab != "log" or getattr(getattr(app, "logs", None), "browser", True)):
        return "Open a log file first."
    if item.context == "table" and tab not in ("jobs", "history"):
        return "Open Jobs or History first."
    if item.context == "job" and not (getattr(app, "selected_id", None) or getattr(app, "marks", set())):
        return "Select or mark a job first."
    if item.context in ("project", "binding"):
        state = getattr(app, "project_state", {})
        if not state.get("binding" if item.context == "binding" else "root"):
            return "Choose a project run first." if item.context == "binding" else "Open a project first."
    return ""


def _close(app):
    initialize(app).update(menu=None, focus="", dragging=False, menu_hits=[], panel=None,
                           painted_menu=None, menu_token=None, menu_rect=None,
                           menu_source="", menu_disabled={}, drag_width=None,
                           submenu=None, menu_stack=[])


def _open(app, menu=0, source="keyboard"):
    initialize(app).update(menu=menu % len(MENUS), cursor=0, top=0, focus="menu", dragging=False,
                           panel=None, menu_hits=[], painted_menu=None, menu_token=None,
                           menu_rect=None, menu_source=source, menu_disabled={}, drag_width=None,
                           submenu=None, menu_stack=[])


def _open_submenu(app, name):
    if name not in SUBMENUS:
        return
    state = initialize(app)
    state["menu_stack"].append((state["submenu"], state["cursor"], state["top"]))
    state.update(submenu=name, cursor=0, top=0, menu_hits=[], menu_token=None,
                 menu_rect=None, menu_disabled={}, dragging=False)


def _back_submenu(app):
    state = initialize(app)
    if state["menu_stack"]:
        parent, cursor, top = state["menu_stack"].pop()
        state.update(submenu=parent, cursor=cursor, top=top, menu_hits=[],
                     menu_token=None, menu_rect=None, menu_disabled={})
    else:
        _close(app)


def _menu_token(app):
    state = initialize(app)
    return (getattr(app, "mode", "main"), getattr(app, "tab", ""), state["menu"],
            state["submenu"], _destination(app), tuple(state["favorites"]))


def _activate(app, item):
    reason = _blocked(app, item)
    if reason:
        app.say(reason)
        return
    state = initialize(app)
    # The dropdown remains available while local options and views are changed.
    # A fresh paint must publish new context-sensitive targets before another
    # click can execute; an old rectangle never invokes a different command.
    state.update(menu_hits=[], menu_token=None, menu_disabled={})
    action = item.local
    if action.startswith("submenu:"):
        _open_submenu(app, action.split(":", 1)[1])
    elif action == "submenu-back":
        _back_submenu(app)
    elif action.startswith("destination:"):
        target = action.split(":", 1)[1]
        entry = _destinations().get(target)
        if entry:
            _close(app)
            app.mode = "main"
            # Both commands are fixed registered destinations, never user text.
            for command in entry[1]:
                app.run_command(command)
    elif action == "quit":
        _close(app)
        app.quit = True
    elif action == "about":
        _close(app)
        initialize(app).update(panel="about", panel_scroll=0)
    elif action == "rate":
        _open_refresh(app)
    elif action == "clear-selection":
        from .text_selection import clear
        clear(app)
        app.sel_anchor = None
        app.logs.clear_selection()
        app.log_selection_expected = False
        app.say("Line selection cleared.")
    elif action == "copy-mode":
        from .editor_yank import toggle
        toggle(app)
    elif action == "workers":
        # Admission controls are local presentation settings. Preserve any
        # pending review, selection, and page while switching background work.
        from .worker_ui import run_command
        run_command(app, item.command.split())
    elif item.command.startswith("setup"):
        # Setup owns its modal and restores the previous page on close.
        _close(app)
        from .setup_ui import run_command
        run_command(app, item.command.split())
    elif action in ("select", "select-all", "follow"):
        app.mode = "main"
        app.handle_action({"select": "visual", "select-all": "visual_all", "follow": "follow"}[action])
    elif item.prompt:
        from .command_ui import open_palette
        open_palette(app, item.command)
    elif item.command in ("profiles", "gettingstarted", "launchjob", "launchsetup", "refreshsettings",
                          "connectdata", "runhome", "exportdata", "exportdata chart"):
        _close(app)
        app.run_command(item.command)
    else:
        app.mode = "main"
        app.run_command(item.command)


def _open_refresh(app):
    _close(app)
    from .telemetry_settings_ui import run_command
    run_command(app, ["refreshsettings"])


def _refresh_summary(app, ascii_=False):
    from .telemetry_settings_ui import summary
    return summary(app, ascii_=ascii_)


def render_bar(views, app, width, y=0):
    """One 80-column-first row: menus, launch, compute status, refresh summary."""
    from . import raven_ui
    state = initialize(app)
    width = max(0, int(width))
    if state["width"] != width or state["bar_y"] != y:
        state.update(menu_hits=[], menu_token=None, menu_rect=None, menu_disabled={})
    state.update(width=width, bar_y=y, hits=[], ascii=bool(views.g.ascii),
                 frame_context=_frame_context(app), dragging=False, drag_width=None)
    if not width:
        return []
    row, x = [], 0

    def append(text, style=BAR_STYLE, kind=None, key=None):
        nonlocal x
        if kind and state.get("hover") == (kind, key):
            style = ("danger" if kind == "quit" else "accent") + "+bold+bg:surface-sunken"
        visible = L.truncate(text, max(0, width - x))
        if visible:
            row.append((visible, style))
            end = x + L.vlen(visible)
            if kind:
                state["hits"].append((y, x, end, kind, key))
            x = end

    if width < 8:
        append("x", "danger+bold+bg:surface-raised", "quit")
        if width >= 4:
            append(" M", kind="menu", key=0)
        append(" " * (width-x))
        return row
    summary = _refresh_summary(app, views.g.ascii)
    managed = isinstance(getattr(app, "cfg", {}).get("raven"), dict) or getattr(app, "raven_allocation_manager", None) is not None
    compute = raven_ui.button(app, 12) if managed else None
    compute_width = L.vlen(compute[0]) if compute else 0
    # Named menus and Launch take precedence; Heavy remains visible at 80 cols.
    forms = [("x ", [name + " " for name in MENUS]),
             ("x ", [name[0] + " " for name in MENUS]), ("x ", ["Menu "])]
    reserve = (9 if width >= 24 else 0) + (compute_width if width >= 60 else 0)
    quit_label, labels = next((form for form in forms
        if L.vlen(form[0] + "".join(form[1])) + reserve + 4 <= width), forms[-1])
    append(quit_label, "danger+bold+bg:surface-raised", "quit")
    for index, label in enumerate(labels):
        selected = state["menu"] == index if len(labels) == len(MENUS) else state["menu"] is not None
        append(label, "accent+bold+bg:surface-sunken" if selected else BAR_STYLE, "menu", index)
    if width >= 24 and width-x >= 9:
        append("[Launch] ", "accent+bold+bg:surface-raised", "launch-job")
    if compute and width >= 60 and width-x >= compute_width+4:
        append(*compute, "raven")
    space = width-x
    refresh = summary if L.vlen(summary)+1 <= space else "Refresh" if space >= 8 else "R" if space else ""
    if refresh:
        append(" " * max(0, space-L.vlen(refresh)-1))
        append(refresh+" ", "accent+bg:surface-raised", "refresh-settings")
    else:
        append(" " * space)
    return L.fill_row(row, width, BAR_STYLE)


def handle_key(app, key):
    state = initialize(app)
    if key == "f10":
        _close(app) if state["menu"] is not None else _open(app)
        return True
    if state["panel"]:
        if key in ("esc", "q", "enter"):
            _close(app)
        elif key in ("up", "down", "pgup", "pgdn", "home", "end"):
            state["panel_scroll"] = max(0, {"up": state["panel_scroll"] - 1, "down": state["panel_scroll"] + 1,
                "pgup": state["panel_scroll"] - state["page"], "pgdn": state["panel_scroll"] + state["page"], "home": 0, "end": 1000}[key])
        return True
    if state["menu"] is None:
        return False
    entries = menu_items(app, state["menu"])
    state["cursor"] = max(0, min(len(entries)-1, state["cursor"]))
    if key in ("esc", "q"):
        _back_submenu(app) if state["submenu"] else _close(app)
    elif key == "left" and state["submenu"]:
        _back_submenu(app)
    elif key == "right" and entries[state["cursor"]].local.startswith("submenu:"):
        _activate(app, entries[state["cursor"]])
    elif key in ("left", "right", "tab", "btab"):
        _open(app, state["menu"] + (-1 if key in ("left", "btab") else 1))
    elif key in ("up", "down", "pgup", "pgdn", "home", "end"):
        count = len(menu_items(app, state["menu"]))
        current = state["cursor"]
        state["cursor"] = max(0, min(count - 1, {"up": current - 1, "down": current + 1,
            "pgup": current - state["page"], "pgdn": current + state["page"], "home": 0, "end": count - 1}[key]))
        state["menu_hits"] = []
    elif key in ("enter", "space"):
        entries = menu_items(app, state["menu"])
        _activate(app, entries[max(0, min(len(entries) - 1, state["cursor"]))])
    elif len(key) == 1 and key.lower() in "fevsh":
        _open(app, "fevsh".index(key.lower()))
    return True


def _frame_context(app):
    return (getattr(app, "mode", "main"), getattr(app, "tab", ""),
            getattr(app, "width", None), getattr(app, "height", None))


def _menu_corridor(state, y, x):
    """Borders and toolbar padding belong to the open dropdown's hover area."""
    rect = state.get("menu_rect")
    if rect and rect[0] <= y < rect[2] and rect[1] <= x < rect[3]:
        return True
    labels = [hit for hit in state["hits"] if hit[3] == "menu"]
    return bool(labels and y == state["bar_y"] and labels[0][1] <= x < labels[-1][2])


def handle_interval_reset(app, y, x):
    """Legacy controller hook: refresh changes live in the settings panel."""
    return False


@capture_handler("toolbar")
def handle_mouse(app, y, x, button="left", shift=False):
    if any(type(value) is not int for value in (y, x)):
        return False
    state = initialize(app)
    current_context = _frame_context(app)
    painted_context = state.get("frame_context", current_context)
    if painted_context != current_context:
        # Context changes can precede the next paint. The old rectangles must
        # never continue a drag or activate a dropdown item in the new page.
        # Global toolbar buttons keep their fixed geometry over dialogs; in
        # particular, Quit remains available during a pending confirmation.
        state.update(dragging=False, drag_width=None, menu_hits=[],
                     menu_token=None, hover=None, frame_context=current_context)
        if painted_context[2:] != current_context[2:]:
            state["hits"] = []
    if button == "release":
        # The pointer may be released over another page or outside the bar.
        # A release ends capture; it does not activate whatever is underneath.
        cancelled = state.get("discard_release", False)
        state["discard_release"] = False
        consumed = state["dragging"] or state["pressed"] or cancelled
        state.update(dragging=False, pressed=False, drag_width=None)
        return bool(consumed)
    hit = next((hit for hit in state["hits"] if hit[0] == y and hit[1] <= x < hit[2]), None)
    if button in ("motion", "drag"):
        state["hover"] = tuple(hit[3:]) if hit else None
        if state["menu"] is not None:
            if hit and hit[3] == "menu":
                if state["menu"] != hit[4]:
                    _open(app, hit[4], source="mouse")
                else:
                    state["menu_source"] = "mouse"
                return True
            if _menu_corridor(state, y, x):
                state["menu_source"] = "mouse"
                token = _menu_token(app)
                target = next((item for item in state["menu_hits"] if item[0] == y and item[1] <= x < item[2]), None)
                if target and state["menu_token"] == token:
                    entries = menu_items(app, state["menu"])
                    index = next((i for i, item in enumerate(entries) if item.key == target[3]), None)
                    if index is not None:
                        state["cursor"] = index
                return True
            if state["menu_source"] == "mouse":
                _close(app)
                return True
            # A keyboard-opened menu is not dismissed merely because the
            # terminal reports a stationary pointer elsewhere on the screen.
            return True
        return False
    if button == "press":
        state["pressed"] = bool(hit or state["menu"] is not None or state["panel"])
    if hit:
        kind, key = hit[3:]
        if button in ("wheel-up", "wheel-down"):
            # Scrolling over navigation must not change polling or hidden rows.
            return True
        if button not in ("left", "press"):
            return True
        if kind == "quit":
            _close(app)
            app.quit = True
        elif kind == "menu":
            # Repeated presses keep the menu available. Moving onto another
            # label switches menus; Esc/F10 remain explicit dismissal controls.
            if state["menu"] != key:
                _open(app, key, source="mouse")
            else:
                state["menu_source"] = "mouse"
        elif kind == "refresh-settings":
            _open_refresh(app)
        elif kind == "export-data":
            _close(app)
            from .data_export_ui import open_export
            open_export(app)
        elif kind == "launch-job":
            _close(app)
            from .launch_wizard_ui import open_launch
            open_launch(app)
        elif kind == "copy-mode":
            from .editor_yank import toggle
            toggle(app)
        elif kind == "workers":
            from .worker_ui import run_command
            run_command(app, ["workers", "toggle"])
        elif kind == 'raven':
            _close(app)
            from .raven_ui import run_command
            run_command(app, ['raven', 'status'])
        return True
    if state["menu"] is not None:
        if button in ("wheel-up", "wheel-down"):
            handle_key(app, "up" if button == "wheel-up" else "down")
            return True
        if button in ("left", "press"):
            token = _menu_token(app)
            target = next((hit for hit in state["menu_hits"] if hit[0] == y and hit[1] <= x < hit[2]), None)
            if target and state["menu_token"] == token:
                item = next((item for item in menu_items(app, state["menu"]) if item.key == target[3]), None)
                if item is not None:
                    _activate(app, item)
                    return True
            # No click-through, including stale targets and dropdown borders.
            # Only real pointer motion out of the hover area dismisses it.
        return True
    if state["panel"]:
        if button in ("left", "press"):
            _close(app)
        elif button in ("wheel-up", "wheel-down"):
            handle_key(app, "up" if button == "wheel-up" else "down")
        return True
    return False


def control_descriptors(app):
    """Published controls for shared hover and spatial keyboard navigation.

    Rectangles use (top, left, bottom, right) with exclusive bottom/right.
    Disabled menu entries are visible controls with an explanatory reason.
    """
    state = initialize(app)
    controls = []
    labels = {"quit": "Quit", "launch-job": "Launch job",
              "refresh-settings": "Refresh settings: " + _refresh_summary(app)}
    if any(hit[3] == 'raven' for hit in state['hits']):
        from .raven_ui import summary
        labels['raven'] = summary(app)
    for y, left, right, kind, key in state["hits"]:
        label = MENUS[key] if kind == "menu" else labels.get(kind, kind)
        controls.append(dict(id="toolbar:" + kind + (":" + str(key) if key is not None else ""),
                             label=label, rect=(y, left, y + 1, right),
                             action=(kind, key), disabled=False, reason=""))
    if state["menu"] is not None:
        entries = {item.key: item for item in menu_items(app, state["menu"])}
        for y, left, right, key in state["menu_hits"]:
            item = entries.get(key)
            if item is not None:
                reason = _blocked(app, item)
                controls.append(dict(id="toolbar:menu:" + str(state["menu"]) + ":" + key,
                                     label=item.label, rect=(y, left, y + 1, right),
                                     action=("item", key), disabled=bool(reason), reason=reason))
    return controls


def _about_lines(app):
    from . import __version__, refresh_rate
    return [
        [(" Raven " + __version__, "accent+bold")],
        [(" Terminal workspace for Slurm, project runs, and research evidence.", "text")],
        [(" Menus: F10; Left/Right changes menu; Up/Down chooses; Enter opens.", "text")],
        [(" Mouse menus stay open for repeated choices; move outside or Esc closes.", "text")],
        [(" Refresh settings explain queue polling, recording reads, and data freshness.", "text")],
        [(" View > Favorites pins destinations; Setup groups configuration by task.", "text")],
        [(" " + refresh_rate.cadence_summary(app), "warning")],
        [(" Faster polling retains source minimum intervals and error backoff.", "dim")],
        [(" Shorter polling intervals update observations; job execution is unchanged.", "dim")],
        [(" Menu browsing never submits, cancels, or changes a scheduler job.", "dim")],
        [(" Parameterized actions open an editable command before execution.", "dim")],
        [(" Project run conventions: docs/PROJECT_STANDARD.md in the repository.", "dim")],
        [(" Esc closes this panel and returns to your previous screen.", "accent")],
    ]


def overlay(views, snap, app, width, height):
    """Absolute dropdown coordinates, always below row zero, with exact hits."""
    state = initialize(app)
    if state["menu"] is None and not state["panel"]:
        return None
    state.update(menu_hits=[], menu_rect=None, menu_disabled={}, ascii=bool(views.g.ascii))
    width, capacity = max(0, int(width)), max(0, int(height) - 1)
    if width == 0 or capacity == 0:
        return []
    ascii_ = views.g.ascii
    about = bool(state["panel"])
    entries = [] if about else menu_items(app, state["menu"])
    lines = _about_lines(app) if about else None
    title = "About Raven" if about else SUBMENUS.get(state["submenu"], MENUS[state["menu"]])
    desired = max(36, max((L.vlen(item.label) + 7 for item in entries), default=68))
    box_width = min(width, desired)
    menu_hits = [hit for hit in state["hits"] if hit[3] == "menu" and hit[4] == state["menu"]]
    left = min(max(0, width - box_width), menu_hits[0][1] if menu_hits else 0)
    bordered = box_width >= 4 and capacity >= 3
    overhead = 3 if bordered and capacity >= 5 else 2 if bordered else 0
    page = max(1, capacity - overhead)
    state["page"] = page
    count = len(lines) if about else len(entries)
    cursor = min(count - 1, max(0, state["panel_scroll"] if about else state["cursor"]))
    top = min(max(0, count - page), state["top"])
    if cursor < top:
        top = cursor
    elif cursor >= top + page:
        top = cursor - page + 1
    state["top"] = top
    if about:
        state["panel_scroll"] = cursor
    else:
        state["cursor"] = cursor
    frame = []
    top_left, top_right, bottom_left, bottom_right, horizontal, vertical = views.g.box
    if bordered:
        caption = L.cut(" " + title + " ", box_width - 2, ascii_)
        frame.append([(top_left + caption + horizontal * (box_width - 2 - L.vlen(caption)) + top_right, "accent+bold+bg:surface")])
    item_width = max(1, box_width - (2 if bordered else 0))
    for index in range(top, min(count, top + page)):
        if about:
            content = L.fill_row(lines[index], item_width, "bg:surface")
        else:
            item = entries[index]
            disabled = _blocked(app, item)
            state["menu_disabled"][item.key] = disabled
            marker = (">" if ascii_ else "▸") if index == cursor else " "
            selected_style = "sel" if getattr(app, "theme", "default") in ("terminal", "mono", "reader") else "accent+bold+bg:surface-raised"
            style = "muted+bg:surface" if disabled else selected_style if index == cursor else "text+bg:surface"
            label = item.label + ((" >" if ascii_ else " ›") if item.local.startswith("submenu:") else "")
            content = L.fill_row([(marker + " " + L.cut(label, max(0, item_width - 2), ascii_), style)], item_width, style)
            state["menu_hits"].append((1 + len(frame), left + int(bordered), left + int(bordered) + item_width, item.key))
        frame.append([(vertical, "border+bg:surface")] + content + [(vertical, "border+bg:surface")] if bordered else content)
    if bordered and overhead == 3:
        reason = "" if about else _blocked(app, entries[cursor])
        hint = "Enter opens; Left/Esc back" if state["submenu"] else "Enter opens; Esc closes"
        footer = " " + reason if reason else f" {top + 1}-{min(count, top + page)}/{count}  {hint}"
        frame.append([(vertical, "border")] + L.fill_row([(L.cut(footer, item_width, ascii_), "muted+bg:surface")], item_width, "") + [(vertical, "border")])
    if bordered:
        frame.append([(bottom_left + horizontal * (box_width - 2) + bottom_right, "border+bg:surface")])
    state["painted_menu"] = state["menu"]
    state["menu_token"] = _menu_token(app)
    state["menu_rect"] = (1, left, 1 + min(len(frame), capacity), left + box_width)
    return [(1 + index, left, L.fill_row(row, box_width, "text+bg:surface"))
            for index, row in enumerate(frame[:capacity])]
