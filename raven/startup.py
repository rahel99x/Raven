"""A brief terminal welcome driven by the existing event loop.

The welcome owns no threads, reads no files, and never waits for a scheduler
source. Its monotonic deadline is independent of painting or incoming data.
Only an explicit interactive ``begin`` admits it; ordinary renders stay pure.
"""
from __future__ import annotations

import math
import time

from . import layout as L

DURATION = 0.95
ASCII_DURATION = 0.55
MIN_WIDTH = 24
MIN_HEIGHT = 6
FRAME_INTERVAL = 0.04

_LETTERS = {
    "R": ("████ ", "█   █", "████ ", "█  █ ", "█   █"),
    "A": (" ███ ", "█   █", "█████", "█   █", "█   █"),
    "V": ("█   █", "█   █", "█   █", " █ █ ", "  █  "),
    "E": ("█████", "█    ", "████ ", "█    ", "█████"),
    "N": ("█   █", "██  █", "█ █ █", "█  ██", "█   █"),
}
_LOGO = tuple(" ".join(_LETTERS[letter][row] for letter in "RAVEN") for row in range(5))


def _preference(value, default=True):
    # Text such as "false" must not silently enable an accessibility setting.
    return value if type(value) is bool else default


def initialize(app):
    state = getattr(app, "startup_state", None)
    if not isinstance(state, dict):
        cfg = getattr(app, "cfg", None)
        state = dict(enabled=_preference(cfg.get("startup_animation", True) if cfg else True),
                     begun=False, running=False, start=0.0, duration=DURATION,
                     preview=False, ascii=False)
        app.startup_state = state
    return state


def enabled(app):
    return initialize(app)["enabled"]


def _set_enabled(app, value):
    state = initialize(app)
    state["enabled"] = value
    cfg = getattr(app, "cfg", None)
    setter = getattr(cfg, "set", None)
    if callable(setter):
        setter("startup_animation", value)
    elif isinstance(cfg, dict):
        cfg["startup_animation"] = value
    if not value:
        dismiss(app)


def restore(app, data):
    initialize(app)
    if isinstance(data, dict) and type(data.get("enabled")) is bool:
        _set_enabled(app, data["enabled"])


def save(app):
    # A saved frame must never resume when the next process starts.
    return {"enabled": enabled(app)}


def _motion_allowed(app):
    cfg = getattr(app, "cfg", None)
    return (bool(getattr(app, "interactive", False))
            and _preference(cfg.get("animations", True) if cfg else True)
            and getattr(app, "theme", "default") != "reader")


def _ascii(app):
    views = getattr(app, "views_ref", None)
    glyphs = getattr(views, "g", None)
    cfg = getattr(app, "cfg", None)
    return bool(getattr(glyphs, "ascii", False) or getattr(app, "_ascii_cfg", False)
                or (cfg.get("ascii", False) if cfg else False))


def _clock(now):
    value = time.monotonic() if now is None else now
    if type(value) not in (int, float):
        raise ValueError("Startup clock must be finite.")
    try:
        value = float(value)
    except OverflowError as exc:
        raise ValueError("Startup clock must be finite.") from exc
    if not math.isfinite(value):
        raise ValueError("Startup clock must be finite.")
    return value


def begin(app, now=None, *, preview=False):
    """Admit one welcome per interactive launch, after preferences restore.

    ``preview`` deliberately replays it without enabling the next launch. A
    reduced-motion or noninteractive process never admits even a preview.
    """
    state = initialize(app)
    if state["begun"] and not preview:
        return False
    state["begun"] = True
    if not _motion_allowed(app) or (not state["enabled"] and not preview):
        state["running"] = False
        return False
    ascii_ = _ascii(app)
    state.update(running=True, start=_clock(now), duration=ASCII_DURATION if ascii_ else DURATION,
                 preview=bool(preview), ascii=ascii_)
    return True


def dismiss(app):
    initialize(app)["running"] = False


def active(app, now=None):
    """Return whether another frame is due; never extend the deadline."""
    state = initialize(app)
    if not state["running"]:
        return False
    if (not _motion_allowed(app) or (not state["enabled"] and not state["preview"])
            or _clock(now) - state["start"] >= state["duration"]):
        state["running"] = False
        return False
    return True


def tick(app, now=None):
    active(app, now)


def command_names():
    return ["startup"]


def run_command(app, args):
    if not args or args[0] != "startup":
        return False
    if len(args) == 1:
        app.say("Startup welcome " + ("on" if enabled(app) else "off") + ".")
    elif len(args) == 2 and args[1] in ("on", "off", "toggle"):
        _set_enabled(app, not enabled(app) if args[1] == "toggle" else args[1] == "on")
        app.say("Startup welcome " + ("on" if enabled(app) else "off") + ".")
    elif args == ["startup", "preview"]:
        if not begin(app, preview=True):
            app.say("Welcome preview needs an interactive terminal with animations on; reader stays static.")
    else:
        app.fail("Use startup [on|off|toggle|preview].")
    return True


def handle_key(app, key):
    # Dismiss without swallowing the user's first navigation, command, or quit.
    dismiss(app)
    return False


def handle_mouse(app, y, x, button="left", shift=False):
    # Hover is incidental. Clicks, drags, and scrolling retain their own action.
    if button not in ("move", "motion", "hover", "release"):
        dismiss(app)
    return False


def _center(row, width):
    row = L.clip_row(row, width)
    size = L.vlen(L.row_text(row))
    left = max(0, (width - size) // 2)
    return [(" " * left, "bg:surface")] + row + [(" " * (width - size - left), "bg:surface")]


def _glow(text, fraction):
    """One slow light sweep over a solid logo, with no flashing or false meter."""
    front = fraction * (len(text) + 8) - 4
    out = []
    previous = None
    for column, char in enumerate(text):
        distance = abs(column - front)
        style = ("text+bold" if distance < 1.5 else "accent+bold" if distance < 5
                 else "info+bold" if column < front else "muted") + "+bg:surface"
        if style == previous:
            out[-1] = (out[-1][0] + char, style)
        else:
            out.append((char, style))
            previous = style
    return out


def overlay(views, snap, app, width, height):
    """Absolute, bounded body rows. Toolbar row zero is always untouched."""
    if not active(app):
        return None
    width, height = max(0, int(width)), max(0, int(height))
    if width < MIN_WIDTH or height < MIN_HEIGHT:
        dismiss(app)
        return None
    state = initialize(app)
    ascii_ = bool(views.g.ascii or state["ascii"])
    fraction = min(1.0, max(0.0, (_clock(None) - state["start"]) / state["duration"]))
    box_width = min(64, width - 2)
    inner = box_width - 2
    capacity = height - 1
    logo = _LOGO if inner >= L.vlen(_LOGO[0]) and capacity >= 11 and not ascii_ else ()
    contents = [[("RAVEN", "accent+bold+bg:surface")]]
    if logo:
        contents = [_glow(line, fraction) for line in logo]
        contents += [[("SLURM RESEARCH WORKSPACE", "muted+bg:surface")]]
    elif not ascii_ and capacity >= 8:
        contents.insert(0, _glow("▂▃▅▇█▇▅▃▂", fraction))
    if capacity >= len(contents) + 5:
        contents += [[("", "bg:surface")], [("A clearer view of your work.", "text+bg:surface")]]
    contents += [[("Any key continues", "muted+bg:surface")]]
    # The smallest usable terminal gets three text rows and two border rows.
    contents = contents[:max(1, capacity - 2)]
    glyphs = views.g if views.g.ascii == ascii_ else L.Glyphs(ascii_)
    tl, tr, bl, br, rule, side = glyphs.box
    rows = [[(tl + rule * inner + tr, "border+bg:surface")]]
    rows += [[(side, "border+bg:surface")] + _center(line, inner) + [(side, "border+bg:surface")]
             for line in contents]
    rows += [[(bl + rule * inner + br, "border+bg:surface")]]
    x = max(0, (width - box_width) // 2)
    y = max(1, 1 + (height - 1 - len(rows)) // 2)
    return [(y + index, x, row) for index, row in enumerate(rows) if y + index < height]
