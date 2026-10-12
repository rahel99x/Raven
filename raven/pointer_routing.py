"""Single pointer ownership for native input and direct UI dispatch.

A lease owns only a gesture's lifetime. Each control retains its published
geometry, attempt identity, preview, rollback, and commit rules. No source
collection, document copies, or hit-map rebuilds run in this module.
"""
from dataclasses import dataclass
from functools import wraps
from importlib import import_module

BUTTONS = frozenset(("press", "left", "right", "motion", "drag", "release", "wheel-up", "wheel-down"))
CONTINUATIONS = frozenset(("motion", "drag", "release"))


@dataclass(frozen=True)
class CaptureOwner:
    """Adapter to one existing control state machine, in dispatch priority."""
    name: str
    state: str
    field: str = "capture"
    marker: str = "discard_release"
    modifiers: bool = False


OWNERS = (
    CaptureOwner("column_resize", "column_resize_state"),
    CaptureOwner("group_queue_drag", "group_queue_drag_state", modifiers=True),
    CaptureOwner("job_group_drag", "job_group_drag_state", modifiers=True),
    CaptureOwner("scrollbars", "scrollbar_state"),
    CaptureOwner("toolbar", "toolbar_state", "dragging"),
    CaptureOwner("text_selection", "text_selection_state"),
    CaptureOwner("job_selection", "job_selection_state"),
    CaptureOwner("pane_drag", "pane_drag_state"),
    CaptureOwner("history_browser", "history_browser_state", "drag"),
    CaptureOwner("metric_live", "metric_live_state", marker="cancelled_release"),
    CaptureOwner("chart_interaction", "chart_interaction_state", marker="cancelled_release"),
)
_BY_NAME = {owner.name: owner for owner in OWNERS}
RELEASE_MARKERS = tuple((owner.state, owner.marker) for owner in OWNERS if owner.marker)


@dataclass(frozen=True)
class PointerEvent:
    y: int
    x: int
    button: str
    shift: bool = False
    alt: bool = False

    @property
    def valid(self):
        return type(self.y) is int and type(self.x) is int and type(self.button) is str and self.button in BUTTONS

    @property
    def fresh(self):
        return self.button in ("press", "left")


@dataclass(frozen=True)
class PointerLease:
    """Small scalar token; completed gestures never retain large row ranges."""
    owner: CaptureOwner
    generation: int
    context: tuple
    capture_token: int


def _root(app):
    return getattr(app, "_pointer_owner_app", app)


def _state(app):
    app = _root(app)
    value = getattr(app, "pointer_routing_state", None)
    if not isinstance(value, dict):
        value = {"lease": None, "generation": 0, "last_cancel": None}
        app.pointer_routing_state = value
    return value


def _context(app):
    return (getattr(app, "mode", "main"), getattr(app, "tab", ""),
            getattr(app, "width", None), getattr(app, "height", None))


def _payload(app, owner):
    state = getattr(app, owner.state, None)
    if not isinstance(state, dict):
        return None
    if owner.name == "toolbar":
        return state if state.get("dragging") or state.get("pressed") else None
    return state.get(owner.field)


def _marked(app, owner):
    return bool(owner.marker and (getattr(app, owner.state, None) or {}).get(owner.marker))


def _clear_marker(app, owner):
    state = getattr(app, owner.state, None)
    if owner.marker and isinstance(state, dict):
        state[owner.marker] = False


def _cancel_owner(app, owner):
    """Preserve committed ranges; restore previews using their own rules."""
    state = getattr(app, owner.state, None)
    if not isinstance(state, dict):
        return
    if owner.name in ("job_selection", "text_selection"):
        if state.get("capture"):
            state["capture"] = None
            state["discard_release"] = True
    elif owner.name == "toolbar":
        if state.get("dragging") or state.get("pressed"):
            state["discard_release"] = True
        state.update(dragging=False, pressed=False, drag_width=None)
    elif owner.name == "history_browser":
        import_module("raven.history_browser")._cancel_drag(state)
    else:
        import_module("raven." + owner.name).cancel(app)


def active(app):
    """Bounded read-only capture check, also usable by byte-prefix protection."""
    app = _root(app)
    return any(_payload(app, owner) is not None for owner in OWNERS)


def cancel_capture(app, *, reason="cancelled"):
    """End unfinished gestures without synthesizing releases or actions.

    Used for a decoded unheld report; bare curses motion remains inconclusive.
    Selection ranges are kept. Preview controls apply their existing rollback.
    """
    app = _root(app)
    state = _state(app)
    for owner in OWNERS:
        if _payload(app, owner) is not None:
            _cancel_owner(app, owner)
    state["last_cancel"] = reason


def begin_gesture(app):
    """A fresh press supersedes stale owners and their delayed releases.

    Native input can then reach App.click, so this is deliberately idempotent
    when no control has claimed the new gesture yet.
    """
    app = _root(app)
    state = _state(app)
    if state["lease"] is not None or active(app):
        cancel_capture(app, reason="superseded")
    state["lease"] = None
    for owner in OWNERS:
        _clear_marker(app, owner)


def _claim(app, owner, capture, *, fresh=False):
    state = _state(app)
    previous = state["lease"]
    if not fresh and previous and previous.owner == owner and previous.capture_token == id(capture):
        return
    # Legacy callers can invoke control handlers directly. Their new capture
    # must be just as exclusive as one entered through the screen/controller.
    for other in OWNERS:
        if other != owner and _payload(app, other) is not None:
            _cancel_owner(app, other)
    for other in OWNERS:
        _clear_marker(app, other)
    state["generation"] += 1
    state["lease"] = PointerLease(owner, state["generation"], _context(app), id(capture))


def capture_handler(name):
    """Bind a control's existing mouse handler to the shared lifetime protocol."""
    owner = _BY_NAME[name]
    def decorate(handler):
        @wraps(handler)
        def wrapped(app, y, x, button="left", shift=False, **options):
            root = _root(app)
            before = _payload(root, owner)
            try:
                return handler(app, y, x, button=button, shift=shift, **options)
            except BaseException:
                if _payload(root, owner) is not None:
                    _cancel_owner(root, owner)
                _state(root)["last_cancel"] = "handler-error"
                raise
            finally:
                after = _payload(root, owner)
                if after is not None:
                    _claim(root, owner, after, fresh=before is not after)
                else:
                    state = getattr(root, "pointer_routing_state", None)
                    lease = state.get("lease") if isinstance(state, dict) else None
                    if lease and lease.owner == owner and (before is None or id(before) == lease.capture_token):
                        # A cancelled owner keeps a tombstone until release or
                        # a fresh press. A completed release has no tombstone.
                        if not _marked(root, owner):
                            state["lease"] = None
        return wrapped
    return decorate


def route_capture(app, event):
    """Continue exactly one capture, before unrelated hit-testing can run."""
    app = _root(app)
    if not event.valid:
        return True
    if event.fresh:
        return False
    state = _state(app)
    lease = state["lease"]
    if lease is None:
        # Compatibility with controls populated before this protocol or by a
        # headless renderer. Normal claims are registered by their handlers.
        owner = next((owner for owner in OWNERS if _payload(app, owner) is not None), None)
        if owner is not None:
            _claim(app, owner, _payload(app, owner))
            lease = state["lease"]
    if lease is not None:
        owner = lease.owner
        capture = _payload(app, owner)
        if capture is not None and lease.context != _context(app):
            _cancel_owner(app, owner)
            state["last_cancel"] = "context-changed"
            capture = None
        if capture is None:
            if _marked(app, owner) and event.button in ("drag", "release"):
                if event.button == "release":
                    _clear_marker(app, owner)
                    state["lease"] = None
                return True
            if not _marked(app, owner):
                state["lease"] = None
        elif event.button in CONTINUATIONS or owner.name in ("column_resize", "group_queue_drag", "job_group_drag"):
            # Publish cosmetic position once, with no control activation.
            # Ownership must not leave global hover stuck at the initial press.
            if isinstance(getattr(app, "interaction_state", None), dict):
                from .interaction import handle_mouse as pointer_motion
                pointer_motion(app, event.y, event.x, button="motion", shift=event.shift)
            options = {"alt": event.alt} if owner.modifiers else {}
            handled = import_module("raven." + owner.name).handle_mouse(
                app, event.y, event.x, button=event.button, shift=event.shift, **options)
            # A continuation belongs to its original owner even if validation
            # cancelled it. It cannot activate a control beneath the release.
            return event.button in CONTINUATIONS or handled
    # Untracked old high-priority cancellation markers retain their established
    # behavior; all registered new gestures clear these at acquisition.
    for owner in OWNERS[:3]:
        if _marked(app, owner):
            options = {"alt": event.alt} if owner.modifiers else {}
            if import_module("raven." + owner.name).handle_mouse(
                    app, event.y, event.x, button=event.button, shift=event.shift, **options):
                return True
    return False
