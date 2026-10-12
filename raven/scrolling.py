"""Bounded wheel motion for painted viewports, independent of selection and IO.

The controller retains one target, never a queue of wheel events. Renderers
keep their logical offsets and ask ``viewport`` for a painted offset instead.
Keyboard input and selection cancel interpolation before changing state.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import math
import time

FRAME_SECONDS = 1 / 60
MAX_CONTROLLERS = 64
MAX_DT = .05
MAX_DURATION = .24
MAX_SPEED = 1200.0
MAX_ACCELERATION = 72000.0
_SUBSTEP = 1 / 240
_EPSILON = .035


@dataclass
class ScrollPID:
    """Critically damped PID with bounded integral, dt and actuator output.

    Motion cannot cross its current target. Direction changes discard stored
    integral and opposing velocity, so an old gesture cannot delay reversal.
    A deadline snaps the final fraction of a terminal row exactly into place.
    """
    position: float = 0.0
    target: float = 0.0
    velocity: float = 0.0
    integral: float = 0.0
    updated: float | None = None
    changed: float | None = None
    lower: float = 0.0
    upper: float = 0.0
    kp: float = 1600.0
    ki: float = 32.0
    kd: float = 80.0

    def snap(self, target, now=None, *, lower=None, upper=None):
        if lower is not None:
            self.lower = float(lower)
        if upper is not None:
            self.upper = max(self.lower, float(upper))
        target = float(target)
        if not math.isfinite(target):
            target = self.lower
        self.position = self.target = max(self.lower, min(self.upper, target))
        self.velocity = self.integral = 0.0
        self.updated = self.changed = now
        return self.position

    def set_target(self, target, now, *, lower=0, upper=None):
        self.lower = float(lower)
        self.upper = max(self.lower, float(target if upper is None else upper))
        target = float(target)
        if not math.isfinite(target):
            target = self.lower
        target = max(self.lower, min(self.upper, target))
        self.position = max(self.lower, min(self.upper, self.position))
        if self.updated is None:
            return self.snap(target, now)
        if target != self.target:
            old_direction = self.target - self.position
            new_direction = target - self.position
            if old_direction * new_direction <= 0:
                self.integral = 0.0
            if self.velocity * new_direction < 0:
                self.velocity = 0.0
            self.target, self.changed = target, now
        return self.position

    @property
    def active(self):
        return self.position != self.target

    def step(self, now):
        if not math.isfinite(now):
            return self.position
        if self.updated is None:
            self.updated = now
            return self.position
        elapsed = now - self.updated
        if elapsed <= 0:
            return self.position
        self.updated = now
        if not self.active:
            return self.position
        if elapsed > MAX_DURATION or (self.changed is not None and now - self.changed >= MAX_DURATION):
            return self.snap(self.target, now)
        dt = min(MAX_DT, elapsed)
        steps = max(1, min(12, math.ceil(dt / _SUBSTEP)))
        dt /= steps
        for _ in range(steps):
            error = self.target - self.position
            # Conditional integration prevents actuator saturation from storing
            # a large future impulse. The bounded integral adds only a small
            # endpoint correction to the critically damped PD response.
            requested = self.kp * error + self.ki * self.integral - self.kd * self.velocity
            if abs(requested) < MAX_ACCELERATION:
                self.integral = max(-.25, min(.25, self.integral + error * dt))
            acceleration = max(-MAX_ACCELERATION, min(MAX_ACCELERATION, requested))
            velocity = max(-MAX_SPEED, min(MAX_SPEED, self.velocity + acceleration * dt))
            if velocity * error < 0:
                velocity = 0.0
            candidate = self.position + velocity * dt
            if (self.target - candidate) * error <= 0 or abs(self.target - candidate) < _EPSILON:
                return self.snap(self.target, now)
            self.position, self.velocity = candidate, velocity
        return self.position


def initialize(app):
    state = getattr(app, "scrolling_state", None)
    if not isinstance(state, dict):
        cfg = getattr(app, "cfg", {})
        value = cfg.get("smooth_scrolling", True)
        state = {"enabled": value if isinstance(value, bool) else True,
                 "controllers": OrderedDict(), "armed_until": 0.0,
                 "active": False, "touched": set()}
        app.scrolling_state = state
    return state


def enabled(app):
    return initialize(app)["enabled"]


def reduced_motion(app):
    return not getattr(app, "animations_enabled", True) or getattr(app, "theme", "") == "reader"


def _set_enabled(app, value):
    initialize(app)["enabled"] = value
    cfg = getattr(app, "cfg", None)
    setter = getattr(cfg, "set", None)
    if callable(setter):
        setter("smooth_scrolling", value)
    elif isinstance(cfg, dict):
        cfg["smooth_scrolling"] = value


def restore(app, data):
    initialize(app)
    if isinstance(data, dict) and isinstance(data.get("enabled"), bool):
        _set_enabled(app, data["enabled"])
        cancel(app)


def save(app):
    return {"enabled": enabled(app)}


def command_names():
    return ["smoothscroll"]


def cancel(app):
    state = getattr(app, "scrolling_state", None)
    if not isinstance(state, dict):
        return
    state["armed_until"], state["active"] = 0.0, False
    for entry in state["controllers"].values():
        entry["pid"].snap(entry["pid"].target)


def run_command(app, args):
    if not args or args[0] != "smoothscroll":
        return False
    if len(args) > 2 or len(args) == 2 and args[1] not in ("on", "off", "toggle"):
        app.fail("smoothscroll [on|off|toggle]")
        return True
    state = initialize(app)
    _set_enabled(app, not state["enabled"] if len(args) == 1 or args[1] == "toggle" else args[1] == "on")
    cancel(app)
    method = getattr(app, "save", None)
    if callable(method):
        method()
    suffix = "; reduced motion keeps wheel updates immediate" if reduced_motion(app) else ""
    app.say("Smooth scrolling " + ("on" if state["enabled"] else "off") + suffix)
    return True


def note_input(app, kind, *, now=None):
    """Only wheels arm viewport interpolation; all meaningful other input snaps."""
    if kind in ("motion", "hover"):
        return
    if kind != "wheel":
        cancel(app)
        return
    state = initialize(app)
    now = time.monotonic() if now is None else now
    if math.isfinite(now):
        state["armed_until"] = now + MAX_DURATION


def begin_frame(app):
    state = initialize(app)
    state["touched"].clear()
    state["active"] = False


def viewport(app, key, target, count, page, *, context=(), immediate=False, now=None):
    """Return a bounded display offset without writing logical offset or cursor.

    Include job/file/view/width identity in ``context``. A new document, first
    paint, reduced motion, follow mode or non-wheel input is always immediate.
    ``count`` and ``page`` refer to the same rows the caller will slice.
    """
    state = initialize(app)
    now = time.monotonic() if now is None else now
    limit = max(0, int(count) - max(0, int(page)))
    target = max(0, min(limit, int(target)))
    controllers = state["controllers"]
    entry = controllers.get(key)
    identity = (context, int(page))
    if entry is None or entry["context"] != identity:
        if len(controllers) >= MAX_CONTROLLERS:
            controllers.popitem(last=False)
        pid = ScrollPID(lower=0, upper=limit)
        pid.snap(target, now)
        entry = {"pid": pid, "context": identity}
        controllers[key] = entry
    else:
        controllers.move_to_end(key)
    pid = entry["pid"]
    state["touched"].add(key)
    animate = (state["enabled"] and not reduced_motion(app) and not immediate and
               math.isfinite(now) and now <= state["armed_until"])
    if not animate:
        pid.snap(target, now, lower=0, upper=limit)
    else:
        pid.set_target(target, now, upper=limit)
        pid.step(now)
    state["active"] = state["active"] or pid.active
    # The float controller advances at a regular frame cadence; terminal rows
    # round once at the paint boundary. The final row is exact.
    return max(0, min(limit, int(math.floor(pid.position + .5))))


def finish_frame(app):
    state = initialize(app)
    state["active"] = any(state["controllers"][key]["pid"].active for key in state["touched"])
    return state["active"]


def published_position(app, key, fallback, *, context=None, now=None):
    """Peek at the last painted offset for bounded virtualized renderers.

    This does not advance motion or alter controller state. ``fallback`` is
    the current logical offset when no wheel transition owns this viewport.
    Renderers can prepare only the nearby visual range before calling
    ``viewport`` at the final slice boundary.
    """
    state = getattr(app, "scrolling_state", None)
    now = time.monotonic() if now is None else now
    if (not isinstance(state, dict) or not state["enabled"] or reduced_motion(app) or
            not math.isfinite(now) or now > state["armed_until"]):
        return int(fallback)
    entry = state["controllers"].get(key)
    if entry is None or context is not None and entry["context"][0] != context:
        return int(fallback)
    pid = entry["pid"]
    # Non-wheel input clears the controller clock. Even if a later wheel
    # report rearms motion, set_target will snap this uninitialized controller
    # to the new logical offset. Virtualized sources must prepare that same
    # range before viewport performs the final slice.
    if (pid.updated is None or now - pid.updated > MAX_DURATION or
            int(fallback) == int(pid.target) and pid.changed is not None and now - pid.changed >= MAX_DURATION):
        return int(fallback)
    return int(math.floor(pid.position + .5))


def active(app):
    state = getattr(app, "scrolling_state", None)
    return bool(isinstance(state, dict) and state.get("active") and state.get("enabled") and not reduced_motion(app))


def timeout_ms(app, idle=200):
    return min(idle, max(1, round(FRAME_SECONDS * 1000))) if active(app) else idle


def handle_wheel(app, y, x, delta):
    """Use published data for common viewports; never admit work or perform IO.

    Other panes keep their existing semantic wheel handlers. Exact retained log
    line navigation uses the already published buffer rather than preparing a
    new snapshot for each row in a burst.
    """
    if getattr(app, "mode", "main") != "main":
        return False
    if getattr(app, "tab", "") == "research" and hasattr(app, "research_rows"):
        app.research_scroll = max(0, min(max(0, app.research_rows - 1), app.research_scroll + delta * 3))
        return True
    logs = getattr(app, "logs", None)
    if getattr(app, "tab", "") == "log" and logs is not None and not logs.browser:
        action = "up" if delta < 0 else "down"
        if getattr(app, "keymap", {}).get(action) not in (action, None):
            return False
        buf = getattr(logs, "buffers", {}).get(logs.path)
        if buf is not None and callable(getattr(logs, "move_cursor", None)):
            app.click_row = None
            for _ in range(3):
                logs.move_cursor(action, buf)
            return True
    return False


def handle_key(app, key):
    return False


def handle_mouse(app, y, x, button="left", shift=False):
    return False


def overlay(views, snap, app, width, height):
    return None
