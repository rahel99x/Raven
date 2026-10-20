"""Published pane registration and manual-scroll state, without input handlers."""

from .geometry import Pane, pane_state as initialize, pane_rect as _rect

MAX_PANES = 128
MAX_MANUAL = 128


def begin_frame(app, width=None, height=None):
    state = initialize(app)
    state["staged"] = []


def register(
    app,
    key,
    rect,
    count,
    page,
    target,
    painted,
    setter,
    *,
    context=(),
    header=None,
    layer=0,
    absolute=False,
):
    state = initialize(app)
    rect = _rect(rect)
    if (
        rect is None
        or rect.right <= rect.left
        or rect.bottom <= rect.top
        or not callable(setter)
    ):
        return None
    count, page = max(0, int(count)), max(0, int(page))
    if page <= 0 or count <= 0 or len(state["staged"]) >= MAX_PANES:
        return None
    limit = max(0, count - page)
    if header is not None:
        if len(header) != 3 or header[2] - header[1] < 4:
            header = None
        else:
            header = tuple(header)
    pane = Pane(
        str(key),
        rect,
        count,
        page,
        max(0, min(limit, int(target))),
        max(0, min(limit, int(painted))),
        setter,
        context,
        header,
        int(layer),
        bool(absolute),
    )
    state["staged"].append(pane)
    return pane


def mark(app):
    return len(initialize(app)["staged"])


def manual(app, key, *, context=None):
    state = initialize(app)
    entry = state["manual"].get(str(key))
    if entry is None:
        return None
    if context is not None and entry["context"] != context:
        state["manual"].pop(str(key), None)
        return None
    return entry["target"]


def set_manual(app, key, target, *, context=()):
    manual_state = initialize(app)["manual"]
    manual_state[str(key)] = {
        "target": max(0, int(target)),
        "context": context,
        "cursor": getattr(app, "cursor", {}).get(
            "jobs" if str(key) == "recent" else str(key)
        ),
    }
    manual_state.move_to_end(str(key))
    while len(manual_state) > MAX_MANUAL:
        manual_state.popitem(last=False)


def resume(app, key=None):
    state = initialize(app)
    if key is None:
        state["manual"].clear()
    else:
        state["manual"].pop(str(key), None)
