"""Typed composition keeps handlers explicit and shared state below UI owners."""

from dataclasses import FrozenInstanceError
from types import SimpleNamespace

import pytest

from raven.ui import owner_ports
from raven.workbench import bind_actions


def test_effect_ports_fail_closed_without_application_composition():
    for accessor in (
        owner_ports.selection,
        owner_ports.interaction,
        owner_ports.panels,
        owner_ports.charts,
        owner_ports.lists,
    ):
        with pytest.raises(RuntimeError, match="composed by the application"):
            accessor(SimpleNamespace())


def test_effect_record_rejects_noncallable_and_is_immutable():
    with pytest.raises(TypeError, match="activate must be callable"):
        owner_ports.PanelActions(activate=None)
    value = owner_ports.PanelActions(activate=lambda *_: True)
    with pytest.raises(FrozenInstanceError):
        value.activate = lambda *_: False


def test_composition_preserves_late_owner_injection_and_exact_arguments(monkeypatch):
    from raven import (
        history_browser,
        chart_interaction,
        data_export_ui,
        job_panels,
        table_ui,
    )

    app = SimpleNamespace()
    bind_actions(app)
    observed = []
    monkeypatch.setattr(
        history_browser,
        "activate",
        lambda *a, **kw: observed.append(("history", a, kw)),
    )
    monkeypatch.setattr(
        chart_interaction,
        "handle_mouse",
        lambda *a, **kw: observed.append(("chart", a, kw)),
    )
    monkeypatch.setattr(
        data_export_ui,
        "open_export",
        lambda *a, **kw: observed.append(("export", a, kw)),
    )
    monkeypatch.setattr(
        job_panels, "_activate", lambda *a, **kw: observed.append(("panel", a, kw))
    )
    monkeypatch.setattr(
        table_ui, "run_command", lambda *a, **kw: observed.append(("table", a, kw))
    )
    owner_ports.selection(app).history_activate(app, "123")
    owner_ports.selection(app).graph_mouse(app, 4, 8, button="right", shift=True)
    owner_ports.charts(app).open_export(app, chart=True)
    owner_ports.panels(app).activate(app, "trace", focus=False)
    owner_ports.lists(app).table_command(app, ["savedview", "jobs", "load", "demo"])
    assert observed == [
        ("history", (app, "123"), {}),
        ("chart", (app, 4, 8), {"button": "right", "shift": True}),
        ("export", (app,), {"chart": True}),
        ("panel", (app, "trace"), {"focus": False}),
        ("table", (app, ["savedview", "jobs", "load", "demo"]), {}),
    ]


def test_state_and_registration_facades_preserve_function_and_class_identity():
    from raven import (
        chart_interaction,
        job_panels,
        pane_drag,
        scrollbars,
        workspace_layout,
    )
    from raven.ui import (
        chart_registry,
        divider_registry,
        divider_state,
        scrollbar_state,
        workspace_actions,
    )

    assert pane_drag.initialize is divider_state.initialize
    assert pane_drag.blur is divider_state.blur
    for name in ("Divider", "register", "control_hit", "paint"):
        assert getattr(pane_drag, name) is getattr(divider_registry, name)
    for name in ("register", "mark", "manual", "set_manual", "resume", "begin_frame"):
        assert getattr(scrollbars, name) is getattr(scrollbar_state, name)
    assert chart_interaction.place_since is chart_registry.place_since
    assert job_panels.focus_main is workspace_actions.focus_main
    assert workspace_layout._scroll is workspace_actions._scroll
    assert workspace_layout._focus_main_content is workspace_actions._focus_main_content
