"""Descriptor consumers preserve dispatch, quoting, reviews and runtime context."""
from dataclasses import FrozenInstanceError
from types import SimpleNamespace

import pytest

from tower import command_registry as registry, command_ui, flagship_commands, operations, toolbar, workbench
from tower.config import Config
from tower.controller import App
from tower.model import Job, Store
from tower.palette import THEME_NAMES


@pytest.fixture
def app():
    store = Store(persist=False)
    store.apply_jobs([Job("21", "first run", "main", "RUNNING"),
                      Job("22", "second run", "main", "PENDING")])
    application = App(store, None, None, Config(), "test", ascii_=True)
    application.visible_ids = ["21", "22"]
    application.selected_id = "21"
    return application


def complete(app, text):
    command_ui.open_palette(app, text)
    return command_ui.suggestions(app)


def test_all_installed_handlers_are_discoverable_with_owned_metadata(app):
    advertised = list(dict.fromkeys(App.COMMANDS + workbench.command_names()))
    assert app.commands() == advertised
    assert set(advertised) == set(registry.BY_NAME)
    assert len(registry.COMMANDS) == len(registry.BY_NAME)
    for module in workbench.modules():
        for name in module.command_names():
            assert registry.command(name).owner != "controller"
            assert registry.command(name).description != "Tower command"
    assert flagship_commands.TASKS is registry.TASKS
    assert flagship_commands.command_rows() == registry.task_rows()


def test_command_help_uses_live_tab_profiles_and_registered_descriptions(app):
    app.tab = "jobs"
    app.palette_edit = "sort "
    assert "name" in app.palette_hint()
    app.tab = "history"
    assert "end" in app.palette_hint()
    app.days_options = [0.5, 7]
    app.palette_edit = "days "
    assert app.palette_hint() == "days <0.5|7>"
    rows = command_ui._help_entries(app)
    assert any(group == "Command reference" and "workers" in key and "background" in description
               for group, key, description in rows)
    assert rows[-1][0] == "Configuration"


@pytest.mark.parametrize("theme", THEME_NAMES + ("monokai", "gruvbox"))
def test_theme_completion_validation_execution_and_menu_agree(app, theme):
    candidates = {row["value"] for row in complete(app, "theme ")}
    assert candidates == set(THEME_NAMES)
    status, _ = command_ui.validation(app, "theme " + theme)
    assert not status
    app.run_command("theme " + theme)
    assert app.command_ok
    menu_commands = {item.command for item in toolbar.menu_items(app, "View")}
    assert "theme " + app.theme in menu_commands


def test_plugin_metadata_is_per_session_and_follows_dispatch_precedence(app):
    seen = []
    app.plugins = SimpleNamespace(commands={
        "custom": (lambda owner, args: seen.append(tuple(args)), "custom FILE --custom-option"),
        "theme": (lambda owner, args: seen.append(tuple(args)), "Plugin theme accepts arbitrary arguments"),
        "workbench": (lambda *_: pytest.fail("feature handler must win"), "do not show this hint"),
    })
    assert registry.command("theme", app).owner == "plugin"
    assert registry.command("workbench", app).owner == "ops_ui"
    app.palette_edit = "custom "
    assert app.palette_hint() == "custom FILE --custom-option"
    assert command_ui.validation(app, "theme 'my personal colors'")[0] == ""
    assert complete(app, "theme ") == []
    app.run_command("theme 'my personal colors'")
    assert seen == [("my personal colors",)]
    app.run_command("custom 'path with spaces' --custom-option")
    assert seen[-1] == ("path with spaces", "--custom-option")
    app.plugins = None
    assert "custom" not in app.commands()
    assert registry.command("theme", app).owner == "controller"
    assert {row["value"] for row in complete(app, "theme ")} == set(THEME_NAMES)


@pytest.mark.parametrize("text,expected", [
    (r'find ^worker\s+"failed"$', ["find", r'^worker\s+"failed"$']),
    ('note 21 an "unfinished quote', ['note', '21', 'an', '"unfinished', 'quote']),
    ('filter "raw value"', ['filter', '"raw', 'value"']),
    ('project "/path with spaces"', ['project', '/path with spaces']),
])
def test_raw_tail_and_quoted_path_contracts_are_preserved(text, expected):
    assert command_ui.parse_command_line(text) == expected


def test_operation_completions_reuse_specifications_and_refresh_after_registry_change(app, monkeypatch):
    original_registry = operations._registry()
    first = dict(original_registry)
    monkeypatch.setattr(operations, "_registry", lambda: first)
    calls = []
    original_catalog = operations.catalog
    def catalog():
        calls.append(True)
        return original_catalog()
    monkeypatch.setattr(operations, "catalog", catalog)
    for _ in range(3):
        rows = complete(app, "ops open causal")
        assert any(row["value"] == "causal-delay" for row in rows)
    assert len(calls) == 1
    second = dict(first)
    second["test-only"] = (None, {"key": "test-only", "title": "Optional test adapter", "summary": "New installed capability",
                                  "fields": [{"key": "action", "choices": ["inspect", "review"], "label": "Action"}]})
    monkeypatch.setattr(operations, "_registry", lambda: second)
    assert any(row["value"] == "test-only" for row in complete(app, "ops open test"))
    assert len(calls) == 2
    rows = complete(app, "ops run test-only action=")
    assert {row["value"] for row in rows} == {"action=inspect", "action=review"}
    assert not complete(app, "ops run test-only action=inspect ")


def test_completing_operation_field_leaves_cursor_inside_value(app):
    complete(app, "ops run numerical-stability manif")
    assert command_ui.complete(app)
    assert app.palette_edit == "ops run numerical-stability manifest="


def test_static_toolbar_controls_are_shared_but_dynamic_changes_do_not_leak(app):
    first = toolbar.menu_items(app, "File")
    second = toolbar.menu_items(app, "File")
    assert first is not second and all(a is b for a, b in zip(first, second))
    with pytest.raises(FrozenInstanceError):
        first[0].label = "modified"
    before = {item.key: item for item in toolbar.menu_items(app, "View")}
    app.table_tools_state["freeze"] = True
    after = {item.key: item for item in toolbar.menu_items(app, "View")}
    assert before["freeze"].label == "Pause inspection (sampling continues)"
    assert after["freeze"].label == "Resume inspection"
    assert before["back"] is after["back"]


def test_all_menu_commands_still_resolve_to_registered_handlers(app):
    known = set(app.commands())
    for menu in toolbar.MENUS:
        items = toolbar.menu_items(app, menu)
        assert len({item.key for item in items}) == len(items)
        for item in items:
            if item.command:
                assert command_ui.parse_command_line(item.command)[0] in known


def test_review_navigation_sequence_never_executes_on_discovery(app, monkeypatch):
    actions = []
    app.actions = SimpleNamespace(applicable=lambda *args: (True, ""))
    app.marks = {"21", "22"}
    monkeypatch.setattr(app, "execute_confirm", lambda: actions.append("applied"), raising=False)
    for _ in range(3):
        complete(app, "cancel ")
        registry.help_entries(app)
        toolbar.menu_items(app, "Edit")
    assert not actions and not app.confirm
    app.run_command("cancel marked")
    assert app.mode == "confirm"
    assert {job.id for job in app.confirm["jobs"]} == {"21", "22"}
    assert registry.command("cancel").review == "required"
    app.handle("esc")
    assert not actions and app.mode == "main"
    app.run_command("workbench investigate")
    assert app.mode == "operations" and not app.ops_state["running"]
    app.run_command("ops close")
    app.run_command("filter 'raw phrase'")
    assert app.filter == "'raw phrase'" and not actions
