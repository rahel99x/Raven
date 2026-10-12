"""Pinned expert tools retain identities, never stale jobs or reviewed actions."""
from threading import Event, get_ident

import pytest

from raven import layout, ops_ui, ui_persistence, workbench
from raven.model import Store
from test_operations_sequences import operation_dashboard


def test_pin_toggle_keeps_form_context_and_never_runs_operation(operation_dashboard, monkeypatch):
    app = operation_dashboard.app
    selected = app.selected_id
    app.run_command("ops pending-edit")
    app.ops_state["values"]["value"] = "30"
    values = dict(app.ops_state["values"])
    saved = []
    monkeypatch.setattr(app, "save", lambda: saved.append(ops_ui.save(app)))
    def forbidden(*args, **kwargs):
        raise AssertionError("Pinning executed an operation")
    monkeypatch.setattr(ops_ui.O, "run", forbidden)
    app.handle("p")
    assert saved == [{"pinned": ["pending-edit"]}]
    assert app.ops_state["values"] == values and app.selected_id == selected
    app.handle("p")
    assert saved[-1] == {"pinned": []}
    assert app.ops_state["view"] == "form" and app.tab == "jobs"


def test_pin_button_and_release_act_once_on_exact_painted_feature(operation_dashboard):
    app = operation_dashboard.app
    app.run_command("ops pending-edit")
    operation_dashboard.draw()
    y, _, hit = next(hit for hit in app.ops_state["control_hits"] if hit[2]["id"] == "ops:pin")
    assert hit["action"] == ("command", "ops pin pending-edit")
    ops_ui.handle_mouse(app, y, hit["left"], button="press")
    ops_ui.handle_mouse(app, y, hit["left"], button="release")
    ops_ui.handle_mouse(app, y, hit["left"], button="left")
    assert app.ops_state["pinned"] == ["pending-edit"]  # stale frame is inert
    operation_dashboard.draw()
    y, _, hit = next(hit for hit in app.ops_state["control_hits"] if hit[2]["id"] == "ops:pin")
    ops_ui.handle_mouse(app, y, hit["left"])
    assert app.ops_state["pinned"] == []


def test_graph_shortcut_pins_focused_tool_instead_of_catalog_index(operation_dashboard):
    app = operation_dashboard.app
    selected = app.selected_id
    app.run_command("ops")
    operation_dashboard.draw()
    tools = [control for control in app.interaction_state["graph"].controls
             if control.id.startswith("ops-item:ops open ")]
    assert len(tools) > 2
    target = tools[2]
    app.interaction_state.update(active=True, focused=target.id)
    app.handle("p")
    assert app.ops_state["pinned"] == [target.id.removeprefix("ops-item:ops open ")]
    assert app.ops_state["view"] == "catalog" and app.selected_id == selected
    # A second key before repaint must unpin that tool, not pin index zero.
    app.handle("p")
    assert app.ops_state["pinned"] == []


def test_pin_button_keyboard_action_is_an_exact_two_word_command(operation_dashboard):
    app = operation_dashboard.app
    app.run_command("ops pending-edit")
    state = app.ops_state
    state.update(focus="buttons", button=next(i for i, item in enumerate(ops_ui._controls(state)) if item[0] == "pin"))
    app.handle("enter")
    assert state["pinned"] == ["pending-edit"]


def test_search_and_field_editor_own_p_and_graph_enter(operation_dashboard):
    app = operation_dashboard.app
    app.run_command("ops")
    operation_dashboard.draw()
    app.interaction_state.update(active=True, focused=app.interaction_state["graph"].controls[0].id)
    app.run_command("ops find")
    app.handle("p")
    app.handle("enter")
    assert app.ops_state["catalog_query"] == "p"
    assert app.ops_state["view"] == "catalog" and not app.ops_state["pinned"]
    app.run_command("ops pending-edit")
    app.run_command("ops field value")
    app.handle("p")
    app.handle("enter")
    assert app.ops_state["values"]["value"] == "p"
    assert not app.ops_state["pinned"]


def test_pinned_filter_removal_and_catalog_switch_keep_current_context(operation_dashboard):
    app = operation_dashboard.app
    selected = app.selected_id
    app.run_command("ops pin pending-edit")
    app.run_command("ops pin slurm-doctor")
    app.run_command("workbench pinned")
    assert {spec["key"] for spec in ops_ui._catalog_specs(app.ops_state)} == {"pending-edit", "slurm-doctor"}
    app.ops_state.update(index=1, top=1)
    removed = ops_ui._selected_tool(app.ops_state)
    app.handle("p")
    assert len(app.ops_state["pinned"]) == 1 and removed not in app.ops_state["pinned"]
    assert app.ops_state["index"] == app.ops_state["top"] == 0
    app.ops_state["catalog_query"] = "no-such-tool"
    assert not ops_ui._catalog_specs(app.ops_state)
    app.handle("p")
    assert len(app.ops_state["pinned"]) == 1
    app.ops_state["catalog_query"] = ""
    app.handle("p")
    assert not ops_ui._catalog_specs(app.ops_state)
    assert "No matching tools" in "\n".join(layout.row_text(row) for _, _, row in operation_dashboard.draw()[2])
    app.run_command("workbench all")
    assert len(ops_ui._catalog_specs(app.ops_state)) > 20
    assert app.selected_id == selected and app.tab == "jobs"


def test_pin_commands_are_idempotent_and_unknown_tools_are_rejected(operation_dashboard, monkeypatch):
    app = operation_dashboard.app
    saved = []
    monkeypatch.setattr(app, "save", lambda: saved.append(ops_ui.save(app)))
    for command in ("ops pin pending-edit", "ops pin pending-edit", "ops unpin slurm-doctor", "ops pin nonexistent"):
        app.run_command(command)
    assert saved == [{"pinned": ["pending-edit"]}]


@pytest.mark.parametrize("blocked", ["running", "review", "editing", "catalog_search"])
def test_pins_cannot_invalidate_active_inspection_or_review(operation_dashboard, blocked):
    app = operation_dashboard.app
    app.run_command("ops pending-edit")
    if blocked == "review":
        app.ops_state["view"] = "review"
    else:
        app.ops_state[blocked] = "value" if blocked == "editing" else True
    generation = app.ops_state["generation"]
    app.run_command("ops pin pending-edit")
    assert not app.ops_state["pinned"]
    assert app.ops_state["generation"] == generation
    app.ops_state["running"] = False


@pytest.mark.parametrize("stored", [None, "pending-edit", {"pending-edit": True}, [[], {}, "unknown", "pending-edit", "pending-edit"]])
def test_restore_rejects_malformed_and_retired_keys(operation_dashboard, stored):
    app = operation_dashboard.app
    workbench.restore(app, {"ops_ui": {"pinned": stored}})
    expected = ["pending-edit"] if isinstance(stored, list) else []
    assert workbench.save(app)["ops_ui"] == {"pinned": expected}


def test_pins_coalesce_on_worker_and_restart_opens_current_job(operation_dashboard, tmp_path, monkeypatch):
    app = operation_dashboard.app
    app.store = Store(str(tmp_path / "preferences"))
    app.interactive = True
    writer = ui_persistence.bind(app, operation_dashboard.scheduler)
    entered, release = Event(), Event()
    main_thread, save_threads = get_ident(), []
    original = app.store.save_ui
    def slow_save(value):
        save_threads.append(get_ident())
        if len(save_threads) == 1:
            entered.set()
            assert release.wait(3)
        original(value)
    monkeypatch.setattr(app.store, "save_ui", slow_save)
    try:
        app.run_command("ops pin pending-edit")
        assert entered.wait(1)
        app.run_command("ops pin slurm-doctor")
        app.run_command("ops unpin pending-edit")
        app.run_command("ops pin pending-edit")
        assert len(save_threads) == 1
        release.set()
        ui_persistence.close(app)
        assert writer.completed == writer.sequence
        assert main_thread not in save_threads
        persisted = app.store.load_ui()["workbench"]
        ops_ui.restore(app, {})
        workbench.restore(app, persisted)
        assert set(app.ops_state["pinned"]) == {"pending-edit", "slurm-doctor"}
        assert set(persisted["ops_ui"]) == {"pinned"}
        app.selected_id = "81"
        app.cursor["jobs"] = app.visible_ids.index("81")
        app.run_command("workbench pinned")
        app.ops_state["index"] = next(i for i, spec in enumerate(ops_ui._catalog_specs(app.ops_state)) if spec["key"] == "pending-edit")
        app.handle("enter")
        assert app.ops_state["values"]["job_id"] == "81"
    finally:
        release.set()
        ui_persistence.close(app)


def test_render_and_hover_never_save_or_inspect_pins(operation_dashboard, monkeypatch):
    app = operation_dashboard.app
    app.run_command("ops pin pending-edit")
    app.run_command("workbench pinned")
    def forbidden(*args, **kwargs):
        raise AssertionError("Paint/hover attempted I/O")
    monkeypatch.setattr(app, "save", forbidden)
    monkeypatch.setattr(ops_ui.O, "run", forbidden)
    monkeypatch.setattr(ops_ui.O, "read_bytes", forbidden)
    for width, height in ((28, 12), (70, 24), (150, 45)):
        positioned = operation_dashboard.draw(width, height)[2]
        for y, x, row in positioned:
            assert layout.vlen(layout.row_text(row)) <= width
            ops_ui.handle_mouse(app, y, x, button="motion")
    assert app.ops_state["pinned"] == ["pending-edit"]
