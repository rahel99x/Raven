"""Worker preferences and terminal controls; the UI never joins the worker pool."""
from __future__ import annotations

MODES = ("single", "multi")


def initialize(app):
    state = getattr(app, "worker_ui_state", None)
    if not isinstance(state, dict):
        configured = getattr(app, "cfg", {}).get("worker_mode", "multi")
        state = {"desired": configured if configured in MODES else "multi", "status": None, "error": ""}
        app.worker_ui_state = state
    return state


def _preference(app, mode):
    state = initialize(app)
    state["desired"] = mode
    cfg = getattr(app, "cfg", None)
    setter = getattr(cfg, "set", None)
    if callable(setter):
        setter("worker_mode", mode)
    elif isinstance(cfg, dict):
        cfg["worker_mode"] = mode


def status(app):
    """Read bounded scheduler metadata without collecting or admitting work."""
    state = initialize(app)
    scheduler = getattr(app, "worker_scheduler", None)
    read = getattr(scheduler, "status", None)
    if not callable(read):
        state["status"] = None
        return None
    try:
        value = read()
        if not isinstance(value, dict) or value.get("mode") not in MODES or value.get("target") not in MODES:
            raise ValueError("invalid worker status")
        result = {key: value[key] for key in ("mode", "target")}
        result.update({key: bool(value.get(key, False)) for key in ("pending", "closed")})
        for key in ("running", "queued", "draining", "limit", "target_limit", "generation"):
            number = value.get(key, 0)
            result[key] = number if type(number) is int and 0 <= number <= 10**9 else 0
        state.update(status=result, error="")
        return result
    except Exception as exc:
        state.update(status=None, error=str(exc)[:256])
        return None


def bind(app, scheduler):
    """Attach admission control after UI restore; explicit CLI mode wins."""
    app.worker_scheduler = scheduler
    state = initialize(app)
    desired = getattr(app, "cfg", {}).get("worker_mode", state["desired"])
    if desired not in MODES:
        desired = "multi"
    state["desired"] = desired
    current = status(app)
    if current is not None and not current["closed"] and current["target"] != desired:
        scheduler.request_mode(desired)
    return status(app)


def token(app):
    current = status(app)
    return tuple(current[key] for key in ("mode", "target", "pending", "closed", "draining")) if current else None


def summary(app):
    current = status(app)
    if current is None:
        return "Background worker controls are unavailable in this session."
    if current["closed"]:
        return "Background workers are stopped."
    text = (f"Background workers: {current['mode']}; {current['running']} running, "
            f"{current['queued']} queued; limit {current['limit']}.")
    if current["pending"]:
        text += f" Switching to {current['target']}; waiting for {current['draining']} active task(s)."
    return text


def button(app, available, *, ascii_=False):
    """Fixed-width forms prevent controls moving while a switch drains."""
    current = status(app)
    if current is None or available < 5:
        return None
    pending = current["pending"]
    title = current["target"] if pending else current["mode"]
    style = ("muted" if current["closed"] else "warning" if pending else "accent") + "+bold+bg:surface-sunken"
    if available >= 11:
        label = ((">" if ascii_ else "→") if pending else "") + title.title()
        return " [" + label.ljust(7) + "] ", style
    return " [" + ("~" if pending else "") + title[0].upper() + ("" if pending else " ") + "]", style


def restore(app, data):
    _state = initialize(app)
    cfg = getattr(app, "cfg", {})
    if "worker_mode" in getattr(cfg, "ui_locked_settings", ()):
        return
    if isinstance(data, dict) and data.get("mode") in MODES:
        _preference(app, data["mode"])


def save(app):
    return {"mode": initialize(app)["desired"]}


def tick(app):
    status(app)


def command_names():
    return ["workers"]


def run_command(app, args):
    if not args or args[0] != "workers":
        return False
    if len(args) > 2 or len(args) == 2 and args[1] not in (*MODES, "toggle", "status"):
        app.fail("Use workers [single|multi|toggle|status].")
        return True
    current = status(app)
    if current is None:
        app.fail("Background worker controls are unavailable in this session.")
        return True
    choice = args[1] if len(args) == 2 else "status"
    if choice == "status":
        app.say(summary(app))
        return True
    if current["closed"]:
        app.fail("Background workers are stopped; restart Raven to collect data.")
        return True
    target = ("single" if current["target"] == "multi" else "multi") if choice == "toggle" else choice
    try:
        app.worker_scheduler.request_mode(target)
        _preference(app, target)
        app.say(summary(app))
    except Exception as exc:
        app.fail("Cannot change background worker mode: " + str(exc)[:256])
    return True


def handle_key(app, key):
    return False


def handle_mouse(app, y, x, button="left", shift=False):
    return False


def overlay(views, snap, app, width, height):
    return None
