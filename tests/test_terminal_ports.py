"""Terminal effects are explicit capabilities above reusable session services."""

from dataclasses import fields
import importlib
from types import SimpleNamespace

import pytest

from raven import terminal_fusion, terminal_ui
from raven.terminal import evidence, ports, recording, session_state, sessions


@pytest.mark.parametrize(
    "name",
    [
        "ui",
        "fusion",
        "workflows",
        "playback",
        "diagnostics",
        "bridge",
        "child",
        "raster",
        "sync",
        "service",
        "broker",
        "emulator",
        "recording",
    ],
)
def test_terminal_family_legacy_alias_is_the_exact_implementation_module(
    name, monkeypatch
):
    legacy = importlib.import_module("raven.terminal_" + name)
    canonical = importlib.import_module("raven.terminal." + name)
    assert legacy is canonical
    marker = object()
    monkeypatch.setattr(canonical, "_alias_identity_probe", marker, raising=False)
    assert legacy._alias_identity_probe is marker


def test_extracted_terminal_state_and_actions_preserve_public_callable_identity():
    assert terminal_ui.TerminalState is session_state.TerminalState
    assert terminal_ui.initialize is session_state.initialize
    assert terminal_ui.open_session is sessions.open_session
    assert terminal_ui.prepare_input is sessions.prepare_input
    assert terminal_ui.show_report is sessions.show_report
    assert terminal_fusion.FusionStore is evidence.FusionStore
    assert terminal_fusion.register_session is evidence.register_session
    assert terminal_fusion.recording_text is recording.recording_text


@pytest.mark.parametrize(
    "record", [ports.TerminalNavigation, ports.TerminalPresentation]
)
def test_terminal_ports_require_each_named_capability(record):
    callbacks = {field.name: lambda *args, **kwargs: None for field in fields(record)}
    value = record(**callbacks)
    for field in fields(record):
        assert getattr(value, field.name) is callbacks[field.name]
        invalid = {**callbacks, field.name: None}
        with pytest.raises(TypeError, match=field.name):
            record(**invalid)


def test_uncomposed_navigation_and_presentation_fail_explicitly():
    owner = SimpleNamespace()
    with pytest.raises(ValueError, match="navigation has not been bound"):
        ports.navigation(owner)
    with pytest.raises(ValueError, match="presentation has not been bound"):
        ports.presentation(owner)


def test_canonical_session_state_preserves_selected_session_without_ui():
    owner = SimpleNamespace(tab="jobs")
    state = session_state.initialize(owner)
    state.active = "verified-session"
    state.modes["jobs"] = "terminal"
    assert session_state.showing(owner)
    assert session_state.initialize(owner) is state
    owner.tab = "history"
    assert not session_state.showing(owner)
    assert state.active == "verified-session"


def test_allocation_navigation_updates_form_before_existing_inspection(monkeypatch):
    from raven import ops_ui, workbench

    owner = SimpleNamespace()
    state = {"values": {}}
    events = []
    monkeypatch.setattr(
        ops_ui, "_open", lambda app, feature: events.append(("open", app, feature))
    )
    monkeypatch.setattr(ops_ui, "initialize", lambda app: state)
    monkeypatch.setattr(
        ops_ui,
        "_inspect",
        lambda app: events.append(("inspect", app, dict(state["values"]))),
    )
    workbench.bind_terminal(owner)
    selected = {"job_id": "42_7", "mode": "attach", "step_id": "3", "task": "2"}
    ports.navigation(owner).open_allocation(owner, "allocation-shell", selected)
    assert events == [("open", owner, "allocation-shell"), ("inspect", owner, selected)]
