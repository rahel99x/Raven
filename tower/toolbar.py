"""Always-visible terminal menus and a bounded, connection-safe update slider.

Menus retain the underlying modal and execute nothing while browsing. Every
item delegates to an existing command, opens an editable command prompt, or
performs a local presentation action. Hit rectangles come from the last paint.
"""
from __future__ import annotations

from . import layout as L
from .command_registry import Control as Item, toolbar_controls

from .pointer_routing import capture_handler

MENUS = ("File", "Edit", "View", "Help")
BAR_STYLE = "text+bg:surface-raised"


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
                       ("hover", None), ("pressed", False), ("drag_width", None)):
        state.setdefault(key, value)
    return state


def _polling_label(app, value=None, *, ascii_=None):
    from . import refresh_rate
    from .metric_sampling import format_interval
    if ascii_ is None:
        cfg = getattr(app, "cfg", None)
        ascii_ = initialize(app).get("ascii", cfg.get("ascii", False) if cfg else False)
    seconds = (refresh_rate.cadence(app, "jobs") if value is None else
               refresh_rate.candidate_cadence(app, "jobs", value))
    return format_interval(seconds, ascii_=ascii_)


def _polling_field_width(app, ascii_):
    """Keep capture geometry stable as interval labels change their units."""
    state = initialize(app)
    from . import refresh_rate
    token = (bool(ascii_), refresh_rate.candidate_cadence(app, 'jobs', 1),
             refresh_rate.candidate_cadence(app, 'jobs', 50))
    cached = state.get("polling_label_width")
    if cached is None or cached[0] != token:
        # Compute only when character mode changes. Labels
        # between the endpoints can need more cells than either endpoint.
        size = max(L.vlen(_polling_label(app, value, ascii_=ascii_)) for value in range(1, 51))
        cached = (token, max(3, size))
        state["polling_label_width"] = cached
    return cached[1]


def _rate_help(app):
    slowest, fastest = _polling_label(app, 1), _polling_label(app, 50)
    return (f"Update slider: arrows change one step; Home {slowest}; End {fastest}; "
            f"right-click resets to {slowest}; Esc returns.")


def command_names():
    return ["menu", "about"]


def run_command(app, args):
    if not args or args[0] not in command_names():
        return False
    if args[0] == "about" and len(args) == 1:
        _close(app)
        initialize(app).update(panel="about", panel_scroll=0)
    elif args[0] == "menu" and (len(args) == 1 or len(args) == 2 and args[1].title() in MENUS):
        _open(app, MENUS.index(args[1].title()) if len(args) == 2 else 0)
    else:
        app.fail("Use menu [File|Edit|View|Help] or about without arguments.")
    return True


def menu_items(app, menu):
    """Stable item identities; context changes are checked again on activation."""
    if isinstance(menu, int):
        menu = MENUS[menu % len(MENUS)]
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
        from .research import RESEARCH_VIEWS
        from .startup import enabled as startup_enabled
        from .scrolling import enabled as smoothscroll_enabled
        from .worker_ui import status as worker_status
        from .palette import THEME_NAMES
        workers = worker_status(app)
        desired = workers["target"] if workers else "multi"
        worker_toggle = "Switch background workers to " + ("multi" if desired == "single" else "single")
        if workers and workers["pending"]:
            worker_toggle += " (switch pending)"
        entries = [Item("tab-" + key, label + " page", "tab " + key) for key, label in TABS]
        entries += [Item("research-" + key, "Research: " + label, "workspace " + key)
                    for key, label in RESEARCH_VIEWS]
        entries += toolbar_controls("View", {
            "freeze": {"label": "Resume inspection" if getattr(app, "table_tools_state", {}).get("freeze") else "Pause inspection (sampling continues)"},
            "rate-reset": {"label": "Reset queue polling to " + _polling_label(app, 1)},
            "workers-toggle": {"label": worker_toggle},
            "smooth-scroll": {"label": "Disable smooth scrolling" if smoothscroll_enabled(app) else "Enable smooth scrolling"},
            "startup-toggle": {"label": "Disable startup animation" if startup_enabled(app) else "Enable startup animation"},
        })
        labels = {"darcula": "Darcula", "modnokai": "Modnokai", "gruvbox-dark": "Gruvbox Dark"}
        entries += [Item("theme-" + name, "Theme: " + labels.get(name, name), "theme " + name)
                    for name in THEME_NAMES if name != "reader"]
        return entries
    return toolbar_controls("Help")


def _blocked(app, item):
    pending_execution = getattr(app, "execution_state", {}).get("pending_action")
    if (getattr(app, "mode", "main") == "confirm" or pending_execution) and item.local not in ("quit", "about", "rate", "workers"):
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
                           menu_source="", menu_disabled={}, drag_width=None)


def _open(app, menu=0, source="keyboard"):
    initialize(app).update(menu=menu % len(MENUS), cursor=0, top=0, focus="menu", dragging=False,
                           panel=None, menu_hits=[], painted_menu=None, menu_token=None,
                           menu_rect=None, menu_source=source, menu_disabled={}, drag_width=None)


def _rate(app, value=None, delta=None, *, announce=True):
    from . import refresh_rate
    before = refresh_rate.multiplier(app)
    after = (max(refresh_rate.MIN_MULTIPLIER, min(refresh_rate.MAX_MULTIPLIER, before + delta))
             if delta is not None else refresh_rate.validate_multiplier(value))
    if after != before:
        refresh_rate.set_multiplier(app, after)
    if announce and before != after:
        app.say(refresh_rate.cadence_summary(app))
    return after


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
    if action == "quit":
        _close(app)
        app.quit = True
    elif action == "about":
        _close(app)
        initialize(app).update(panel="about", panel_scroll=0)
    elif action == "rate":
        _close(app)
        initialize(app)["focus"] = "rate"
        app.say(_rate_help(app))
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
    elif action in ("select", "select-all", "follow"):
        app.mode = "main"
        app.handle_action({"select": "visual", "select-all": "visual_all", "follow": "follow"}[action])
    elif item.prompt:
        from .command_ui import open_palette
        open_palette(app, item.command)
    else:
        app.mode = "main"
        app.run_command(item.command)


def render_bar(views, app, width, y=0):
    """One topmost row; x bounds are exact even in a one-cell terminal."""
    from . import refresh_rate, worker_ui, raven_ui
    state = initialize(app)
    width = max(0, int(width))
    if state["width"] != width or state["bar_y"] != y:
        state.update(menu_hits=[], menu_token=None, menu_rect=None, menu_disabled={})
    if state["dragging"] and state.get("drag_width") != width:
        # Resize changes the coordinate space. Cancel capture instead of
        # interpreting an old terminal coordinate against a newly moved track.
        state.update(dragging=False, drag_width=None)
    state.update(width=width, bar_y=y, hits=[], ascii=bool(views.g.ascii),
                 frame_context=_frame_context(app))
    if width == 0:
        return []
    value = _polling_label(app, ascii_=views.g.ascii)
    field_width = _polling_field_width(app, views.g.ascii)
    row = []
    x = 0

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
        if width >= len(value) + 1:
            append(" " * (width - x - len(value)))
            append(value, "accent+bold+bg:surface-raised", "rate")
        else:
            append(" " * (width - x))
        return row
    forms = [(" x ", [" File ", " Edit ", " View ", " Help "]),
             ("x", [" F ", " E ", " V ", " H "]), ("x", [" M "]),
             ("x", ["M"])]
    # Reserve the widest effective interval before choosing a menu form.
    # Changing the requested rate must never move menu labels or the track.
    minimum_slider = field_width + 1 + (9 if width >= 44 else 0)
    if width >= 80:
        minimum_slider=max(minimum_slider,27)  # Retain the Updates label.
    has_workers=worker_ui.status(app) is not None
    worker_space = (11 if width >= 80 else 5) if has_workers and width >= minimum_slider + 7 else 0
    managed=isinstance(getattr(app,'cfg',{}).get('raven'),dict) or getattr(app,'raven_allocation_manager',None) is not None
    compute_space = 10 if managed and width >= minimum_slider + worker_space + 12 else 0
    copy_space=4 if width >= 80 else 0
    quit_label, labels = next((form for form in forms if len(form[0]) + sum(map(len, form[1])) <= width - minimum_slider - worker_space - compute_space), forms[-1])
    append(quit_label, "danger+bold+bg:surface-raised", "quit")
    for index, label in enumerate(labels):
        selected = state["menu"] == index if len(labels) == 4 else state["menu"] is not None
        append(label, "accent+bold+bg:surface-sunken" if selected else BAR_STYLE, "menu", index)
    compute_button = raven_ui.button(app, width-x-minimum_slider-worker_space-copy_space) if managed else None
    if compute_button is not None:
        append(*compute_button, 'raven')
    worker_button = worker_ui.button(app, width - x - minimum_slider-copy_space, ascii_=views.g.ascii)
    if worker_button is not None:
        append(*worker_button, "workers")
    from .editor_yank import mode as copy_mode
    destination = copy_mode(app)
    # Keep an update-rate value visible at every width. A compact switch
    # appears when the long form cannot fit beside that permanent control.
    switch = " [Copy ◉] " if destination == "copy" else " [Yank ◉] "
    if views.g.ascii:
        switch = " [Copy *] " if destination == "copy" else " [Yank *] "
    if width - x >= len(switch) + minimum_slider:
        append(switch, "accent+bold+bg:surface-sunken", "copy-mode")
    elif width - x >= 4 + minimum_slider:
        append(" [C]" if destination == "copy" else " [Y]", "accent+bold+bg:surface-sunken", "copy-mode")
    space = width - x
    rate_field = value.rjust(field_width)
    if space < len(rate_field) + 14:
        append(" " * max(0, space - len(value) - 1))
        append((" " if space > len(value) else "") + value, "accent+bold+bg:surface-sunken" if state["focus"] == "rate" else "accent+bold+bg:surface-raised", "rate")
        return row
    caption = " Updates " if space >= 27 else " "
    suffix = " [+] " + rate_field + " "
    track_size = min(18, max(3, space - len(caption) - 4 - len(suffix)))
    used = len(caption) + 4 + track_size + len(suffix)
    append(" " * max(0, space - used))
    append(caption, "muted+bg:surface-raised", "rate")
    append("[-] ", "accent+bold+bg:surface-raised", "minus")
    position = round((refresh_rate.multiplier(app) - 1) * (track_size - 1) / 49)
    for index in range(track_size):
        glyph = ("|" if index == position else "=" if index < position else "-") if views.g.ascii else ("▌" if index == position else "█" if index < position else "░")
        style = "accent+bold+bg:surface-sunken" if index <= position else "track+bg:surface-sunken"
        append(glyph, style, "track", (index, track_size))
    append(" ")
    append("[+]", "accent+bold+bg:surface-raised", "plus")
    append(" " + rate_field + " ", "accent+bold+bg:surface-sunken" if state["focus"] == "rate" else "accent+bold+bg:surface-raised", "rate")
    return row


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
    if state["focus"] == "rate":
        state["dragging"] = False
        if key in ("left", "down", "-", "right", "up", "+", "home", "end"):
            if key in ("home", "end"):
                _rate(app, 1 if key == "home" else 50)
            else:
                _rate(app, delta=-1 if key in ("left", "down", "-") else 1)
            return True
        if key in ("esc", "enter", "tab", "btab"):
            state["focus"] = ""
            return True
        # A key intended for the underlying dialog remains available.
        state["focus"] = ""
        return False
    if state["menu"] is None:
        return False
    if key in ("esc", "q"):
        _close(app)
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
    elif len(key) == 1 and key.lower() in "fevh":
        _open(app, "fevh".index(key.lower()))
    return True


def _track_value(state, x):
    track = [hit for hit in state["hits"] if hit[3] == "track"]
    if not track:
        return None
    start, end = track[0][1], track[-1][2] - 1
    return 1 + round(49 * (max(start, min(end, x)) - start) / max(1, end - start))


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
    """Claim only a right-click on a published polling control."""
    state = getattr(app, "toolbar_state", {})
    if not isinstance(state, dict):
        return False
    if not any(hit[0] == y and hit[1] <= x < hit[2]
               and hit[3] in ("track", "rate", "minus", "plus") for hit in state.get("hits", ())):
        return False
    return handle_mouse(app, y, x, button="right")


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
        if state["dragging"]:
            value = _track_value(state, x)
            if value is not None:
                _rate(app, value)
        state.update(dragging=False, pressed=False, drag_width=None)
        return bool(consumed)
    hit = next((hit for hit in state["hits"] if hit[0] == y and hit[1] <= x < hit[2]), None)
    if button in ("motion", "drag"):
        state["hover"] = tuple(hit[3:]) if hit else None
        if state["dragging"]:
            value = _track_value(state, x)
            if value is not None:
                _rate(app, value)
            return True
        if state["menu"] is not None:
            if hit and hit[3] == "menu":
                if state["menu"] != hit[4]:
                    _open(app, hit[4], source="mouse")
                else:
                    state["menu_source"] = "mouse"
                return True
            if _menu_corridor(state, y, x):
                state["menu_source"] = "mouse"
                token = (getattr(app, "mode", "main"), getattr(app, "tab", ""), state["menu"])
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
        if button == "right" and kind in ("track", "rate", "minus", "plus"):
            # A reset ends an in-flight drag and never falls through to job
            # deselection or the underlying modal.
            _close(app)
            state["pressed"] = False
            _rate(app, 1, announce=False)
            app.say("Queue polling reset to " + _polling_label(app) + ".")
            return True
        if button in ("wheel-up", "wheel-down"):
            if kind in ("track", "rate", "minus", "plus"):
                _close(app)
                state["focus"] = "rate"
                _rate(app, delta=1 if button == "wheel-up" else -1)
                return True
            return False
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
        else:
            _close(app)
            state["focus"] = "rate"
            if kind in ("minus", "plus"):
                _rate(app, delta=-1 if kind == "minus" else 1)
            elif kind == "track":
                _rate(app, _track_value(state, x))
                # CLICKED is an already completed gesture. Capturing it would
                # cause unrelated later pointer motion to change the rate.
                state["dragging"] = button == "press"
                state["drag_width"] = state["width"] if state["dragging"] else None
            else:
                app.say(_rate_help(app))
        return True
    if state["menu"] is not None:
        if button in ("wheel-up", "wheel-down"):
            handle_key(app, "up" if button == "wheel-up" else "down")
            return True
        if button in ("left", "press"):
            token = (getattr(app, "mode", "main"), getattr(app, "tab", ""), state["menu"])
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
    if state["focus"] == "rate":
        state["focus"], state["dragging"] = "", False
    return False


def control_descriptors(app):
    """Published controls for shared hover and spatial keyboard navigation.

    Rectangles use (top, left, bottom, right) with exclusive bottom/right.
    Disabled menu entries are visible controls with an explanatory reason.
    The track is one control rather than one focus stop per character cell.
    """
    state = initialize(app)
    controls = []
    track = []
    rate_hits = [hit for hit in state["hits"] if hit[3] == "rate"]
    polling_label = _polling_label(app)
    labels = {"quit": "Quit", "rate": "Queue polling: " + polling_label,
              "minus": "Increase polling interval", "plus": "Decrease polling interval",
              "copy-mode": "Switch clipboard copy / editor yank"}
    if any(hit[3] == "workers" for hit in state["hits"]):
        from .worker_ui import summary
        labels["workers"] = summary(app)
    if any(hit[3] == 'raven' for hit in state['hits']):
        from .raven_ui import summary
        labels['raven'] = summary(app)
    for y, left, right, kind, key in state["hits"]:
        if kind == "track":
            track.append((y, left, right))
            continue
        if kind == "rate" and rate_hits and (y, left, right, kind, key) != rate_hits[-1]:
            # The descriptive Updates caption invokes the same rate focus as
            # the value; it does not need a duplicate keyboard focus stop.
            continue
        label = MENUS[key] if kind == "menu" else labels.get(kind, kind)
        controls.append(dict(id="toolbar:" + kind + (":" + str(key) if key is not None else ""),
                             label=label, rect=(y, left, y + 1, right),
                             action=(kind, key), disabled=False, reason=""))
    if track:
        controls.append(dict(id="toolbar:track", label="Queue polling slider: " + polling_label,
                             rect=(track[0][0], track[0][1], track[-1][0] + 1, track[-1][2]),
                             action=("track", None), disabled=False, reason=""))
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
        [(" Project Raven " + __version__, "accent+bold")],
        [(" Terminal workspace for Slurm, project runs, and research evidence.", "text")],
        [(" Menus: F10; Left/Right changes menu; Up/Down chooses; Enter opens.", "text")],
        [(" Mouse menus stay open for repeated choices; move outside or Esc closes.", "text")],
        [(" Slider: click or drag the track; +/- or wheel changes one step.", "text")],
        [(" " + _rate_help(app), "text")],
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
    title = "About Raven" if about else MENUS[state["menu"]]
    desired = max(36, max((L.vlen(item.label) + 5 for item in entries), default=68))
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
            content = L.fill_row([(marker + " " + L.cut(item.label, max(0, item_width - 2), ascii_), style)], item_width, style)
            state["menu_hits"].append((1 + len(frame), left + int(bordered), left + int(bordered) + item_width, item.key))
        frame.append([(vertical, "border+bg:surface")] + content + [(vertical, "border+bg:surface")] if bordered else content)
    if bordered and overhead == 3:
        reason = "" if about else _blocked(app, entries[cursor])
        footer = " " + reason if reason else f" {top + 1}-{min(count, top + page)}/{count}  Enter applies; Esc closes"
        frame.append([(vertical, "border")] + L.fill_row([(L.cut(footer, item_width, ascii_), "muted+bg:surface")], item_width, "") + [(vertical, "border")])
    if bordered:
        frame.append([(bottom_left + horizontal * (box_width - 2) + bottom_right, "border+bg:surface")])
    state["painted_menu"] = state["menu"]
    state["menu_token"] = (getattr(app, "mode", "main"), getattr(app, "tab", ""), state["menu"])
    state["menu_rect"] = (1, left, 1 + min(len(frame), capacity), left + box_width)
    return [(1 + index, left, L.fill_row(row, box_width, "text+bg:surface"))
            for index, row in enumerate(frame[:capacity])]
