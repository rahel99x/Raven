"""In-memory workspace focus and scroll transitions."""

from typing import Optional
from .workspace_state import initialize
from .divider_state import blur


def _key(app, panel: Optional[str] = None) -> str:
    state = initialize(app)
    return str(getattr(app, "tab", "jobs")) + ":" + (panel or state.focus)


def _focus_main_content(app) -> None:
    """An explicit Main focus leaves nested button and browser navigation."""
    panel = getattr(app, "job_panel_state", None)
    if isinstance(panel, dict):
        panel["focus"] = ""
    interaction = getattr(app, "interaction_state", None)
    if isinstance(interaction, dict):
        interaction["active"], interaction["focused"] = False, None
        interaction["pending_focus"] = None
    browser = getattr(app, "history_browser_state", None)
    if isinstance(browser, dict):
        browser["focused"] = False
    blur(app)


def _scroll(app, action: str) -> None:
    state = initialize(app)
    key = _key(app)
    count, page = state.sizes.get(key, (0, 1))
    page = max(1, page)
    top = state.scroll.get(key, 0)
    if action == "home":
        top = 0
    elif action == "end":
        top = max(0, count - page)
    else:
        top += {
            "up": -1,
            "down": 1,
            "pgup": -page,
            "pgdn": page,
            "page-up": -page,
            "page-down": page,
        }.get(action, 0)
    state.scroll[key] = max(0, min(max(0, count - page), top))


def focus_main(app):
    initialize(app).focus = "main"
    _focus_main_content(app)
