"""Foreground ownership prevents modal input reaching covered job actions."""

import pytest

from raven import workbench
from raven.config import Config
from raven.controller import App
from raven.model import Store
from raven.ui.focus import FocusStack, Frame, MAX_DEPTH, Owner


def test_only_top_frame_receives_even_unhandled_input():
    calls = []
    covered = Owner("jobs", lambda app, key: calls.append(("jobs", key)))
    foreground = Owner(
        "dialog",
        lambda app, key: calls.append(("dialog", key)) or False,
        lambda app, y, x, **kwargs: calls.append(("mouse", y, x)) or False,
    )
    stack = FocusStack()
    stack.publish((Frame(covered, ("jobs",)), Frame(foreground, ("dialog", 1))))
    assert stack.key(None, "c")
    assert stack.mouse(None, 3, 4)
    assert calls == [("dialog", "c"), ("mouse", 3, 4)]
    revision = stack.revision
    stack.publish(stack.frames)
    assert stack.revision == revision
    with pytest.raises(ValueError, match="bounded"):
        stack.publish([Frame(Owner(str(i)), ()) for i in range(MAX_DEPTH + 1)])


def test_toolbar_covers_and_reveals_same_modal_without_key_fallthrough(monkeypatch):
    app = App(Store(persist=False), None, None, Config(), "alex", ascii_=True)
    calls = []
    registered = dict(workbench.owners())
    registered["notification_ui"] = Owner(
        "notification_ui", lambda owner, key: calls.append(("notice", key)) or False
    )

    def toolbar(owner, key):
        calls.append(("toolbar", key))
        owner.toolbar_state["menu"] = None

    registered["toolbar"] = Owner("toolbar", toolbar)
    monkeypatch.setattr(workbench, "owners", lambda: registered)
    app.mode = "notifications"
    app.toolbar_state["menu"] = 0
    app.handle("esc")
    assert calls == [("toolbar", "esc")]
    assert app.mode == "notifications"
    app.handle("c")
    assert calls == [("toolbar", "esc"), ("notice", "c")]
    assert app.mode == "notifications" and not app.confirm
    assert app.focus_stack.top.owner.name == "notification_ui"


def test_registry_alias_resolves_explicit_legacy_grammar_owner():
    from raven.command_registry import command_owner

    assert command_owner("notifications", ["critical"]) == "notification_ui"
    assert command_owner("notifications", ["level", "error"]) == "activity_ui"
    assert command_owner("not-an-installed-command") == ""
