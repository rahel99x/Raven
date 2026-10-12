"""GPU provider preferences and terminal controls; changing them never starts I/O."""
from __future__ import annotations

from . import layout as L
from .control_rows import buttons, place_hits

PROVIDERS = ("auto", "nvidia", "amd", "intel")
LABELS = {"auto": "Auto", "nvidia": "NVIDIA", "amd": "AMD", "intel": "Intel"}


def initialize(app):
    state = getattr(app, "gpu_provider_state", None)
    if not isinstance(state, dict):
        state = {"cursor": 0, "return_mode": "main", "control_hits": []}
        app.gpu_provider_state = state
    return state


def provider(app):
    value = getattr(app, "cfg", {}).get("gpu_provider", "auto")
    return value if value in PROVIDERS else "auto"


def _preference(app, value):
    cfg = getattr(app, "cfg", {})
    setter = getattr(cfg, "set", None)
    if callable(setter):
        setter("gpu_provider", value)
    else:
        cfg["gpu_provider"] = value


def restore(app, data):
    if "gpu_provider" in getattr(getattr(app, "cfg", None), "ui_locked_settings", ()):
        return
    value = data.get("provider") if isinstance(data, dict) else None
    if value in PROVIDERS:
        _preference(app, value)


def save(app):
    return {"provider": provider(app)}


def bind(app):
    """Apply restored preferences once the session has attached its sampler."""
    sampler = getattr(app, "sampler", None)
    setter = getattr(sampler, "set_gpu_provider", None)
    if callable(setter):
        setter(provider(app))


def command_names():
    return ["gpuprovider"]


def run_command(app, args):
    if not args or args[0] != "gpuprovider":
        return False
    state = initialize(app)
    if len(args) > 2 or len(args) == 2 and args[1] not in (*PROVIDERS, "close"):
        app.fail("Use gpuprovider [auto|nvidia|amd|intel].")
        return True
    if len(args) == 1:
        if app.mode != "gpu_provider":
            state["return_mode"] = app.mode if app.mode not in ("palette", "confirm") else "main"
        state["cursor"] = PROVIDERS.index(provider(app))
        state["control_hits"] = []
        app.mode = "gpu_provider"
    elif args[1] == "close":
        if app.mode == "gpu_provider":
            app.mode = state["return_mode"]
        state["control_hits"] = []
    else:
        value = args[1]
        sampler = getattr(app, "sampler", None)
        setter = getattr(sampler, "set_gpu_provider", None)
        try:
            if callable(setter):
                setter(value)
            else:
                slurm = getattr(getattr(app, "actions", None), "slurm", None)
                setter = getattr(slurm, "set_gpu_provider", None)
                if callable(setter):
                    setter(value)
        except (ValueError, RuntimeError) as exc:
            app.fail("GPU provider: " + str(exc))
            return True
        _preference(app, value)
        state["cursor"] = PROVIDERS.index(value)
        app.command_ok = True
        app.say("GPU provider: " + LABELS[value] + "; applies to the next allocation sample.")
    return True


def handle_key(app, key):
    if getattr(app, "mode", None) != "gpu_provider":
        return False
    state = initialize(app)
    if key in ("esc", "q"):
        run_command(app, ["gpuprovider", "close"])
    elif key in ("left", "up", "btab", "right", "down", "tab"):
        step = -1 if key in ("left", "up", "btab") else 1
        state["cursor"] = (state["cursor"] + step) % len(PROVIDERS)
    elif key in ("enter", "space"):
        run_command(app, ["gpuprovider", PROVIDERS[state["cursor"]]])
    elif key == ":":
        return False
    return True


def handle_mouse(app, y, x, button="left", shift=False):
    if getattr(app, "mode", None) != "gpu_provider":
        return False
    if button not in ("left", "press"):
        return True
    for row, kind, item in initialize(app)["control_hits"]:
        if row == y and item["left"] <= x < item["right"]:
            run_command(app, item["action"][1].split())
            break
    return True


def overlay(views, snap, app, width, height):
    if getattr(app, "mode", None) != "gpu_provider":
        return None
    state = initialize(app)
    state["control_hits"] = []
    inner = max(1, width - 8)
    rows, hits = buttons(views.g, inner,
                        [(key, LABELS[key], ("command", "gpuprovider " + key)) for key in PROVIDERS],
                        selected=PROVIDERS[state["cursor"]], group="gpu-provider", prefix="gpu-provider:")
    rows += [[(" Active source: " + LABELS[provider(app)], "accent+bold")],
             [(" Auto detects supported tools inside each allocation.", "text")],
             [(" Missing tools or unverified device ownership stay unavailable.", "dim")],
             [(" Changes preserve history. GPU sampling must be enabled.", "dim")],
             [(" Arrows choose; Enter applies; Esc returns.", "dim")]]
    close, close_hits = buttons(views.g, inner, [("close", "Close", ("command", "gpuprovider close"))],
                               group="gpu-provider", prefix="gpu-provider:")
    hits += [(y + len(rows), kind, item) for y, kind, item in close_hits]
    rows += close
    rendered = L.box(views.g, rows, width, height, "GPU source")
    state["control_hits"] = place_hits(hits, rendered[1:-1])
    return rendered
