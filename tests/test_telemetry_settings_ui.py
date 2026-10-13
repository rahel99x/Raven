"""Telemetry controls apply live, isolate drafts, and use exact painted targets."""
import copy
from types import SimpleNamespace

import pytest

from raven import layout as L, palette, telemetry_settings_ui as UI
from raven.config import Config
from raven.controller import App
from raven.model import Store


FIELDS = tuple(dict(key=key, label=label, unit="s", description=description,
                    default=default, ultra=ultra, min=.5, max=3600, type="float")
               for key, label, description, default, ultra in (
                   ("queue_seconds", "Queue updates", "Refresh running and pending jobs.", 5, .5),
                   ("live_seconds", "CPU, memory and GPU", "Read existing job measurements.", 2, .5),
                   ("research_seconds", "Research evidence", "Refresh attached report files.", 1, .5)))


class Model:
    FIELDS = FIELDS
    PRESETS = ("default", "medium", "high", "ultra", "custom")

    def __init__(self):
        self.custom = {field["key"]: field["default"] for field in FIELDS}
        self.preset = "default"
        self.heavy = True
        self.calls = []
        self.write_error = None

    def settings_snapshot(self, app):
        effective = self.custom if self.preset == "custom" else {
            field["key"]: field["ultra"] if self.preset == "ultra" else field["default"] for field in FIELDS}
        return dict(preset=self.preset, custom=dict(self.custom), effective=effective, heavy=self.heavy,
                    config_path="/home/student/.config/raven/config.json", limits=["Scheduler measurement cadence still applies."])

    def apply_settings(self, app, preset, custom=None, persist=False):
        self.calls.append(("apply", preset, custom, persist))
        if custom is not None:
            self.validate_custom(custom)
            self.custom = dict(custom)
        self.preset = preset
        return {"message": preset.title() + " settings applied live."}

    def validate_custom(self, values):
        for field in FIELDS:
            if not field["min"] <= values[field["key"]] <= field["max"]:
                raise ValueError(field["label"] + " is outside the allowed range")
        return dict(values)

    def value_fraction(self, key, value):
        field = next(field for field in FIELDS if field["key"] == key)
        return max(0, min(1, (field["default"] - value) / (field["default"] - field["ultra"])))

    def prepare_save(self, app):
        return self.settings_snapshot(app)

    def save_settings(self, request):
        self.calls.append(("write", copy.deepcopy(request)))
        if self.write_error:
            raise self.write_error
        return {"message": "saved"}

    def saved(self, app, request, value):
        self.calls.append(("saved", value))


class Background:
    demo = False

    def __init__(self):
        self.pending = None
        self.settings = {}

    def start_task(self, work, completed):
        if self.pending:
            return False
        self.pending = work, completed
        return True

    def finish(self):
        work, completed = self.pending
        self.pending = None
        try:
            value = work()
        except Exception as exc:
            value = exc
        completed(value)


@pytest.fixture
def app(monkeypatch):
    value = App(Store(persist=False), None, None, Config(), "student")
    value.width, value.height = 100, 36
    value.research = Background()
    value.test_settings = Model()
    monkeypatch.setattr(UI, "_model", lambda: value.test_settings)
    return value


def render(app, ascii_=False):
    return UI.overlay(SimpleNamespace(g=L.Glyphs(ascii_)), {}, app, app.width, app.height)


def text(rows):
    return "\n".join(L.row_text(row) for _, _, row in rows)


def command(app, *args):
    assert UI.run_command(app, [UI.COMMAND, *args])


def click(app, tail):
    render(app)
    y, _, item = next(hit for hit in app.telemetry_settings_ui_state["control_hits"]
                      if hit[2]["action"] == ("command", UI.COMMAND + " " + tail))
    UI.handle_mouse(app, y, item["left"])


def edit(app, key, value):
    command(app, "edit", key)
    UI.handle_key(app, "ctrl-u")
    for char in value:
        UI.handle_key(app, char)


def custom(app):
    command(app)
    command(app, "preset", "custom")
    command(app, "custom")


def test_menu_and_command_discoverability(app):
    from raven import command_registry, toolbar, workbench
    items = toolbar.menu_items(app, "Setup")
    item = next(item for item in items if item.command == "setup updates")
    toolbar._activate(app, item)
    assert app.mode == UI.MODE
    assert UI.COMMAND in workbench.command_names()
    assert command_registry.command(UI.COMMAND).owner == "telemetry_settings_ui"
    assert UI.MODE in workbench.INSPECTION_MODES


@pytest.mark.parametrize("origin", ["main", "setup", "settings_editor", "help"])
def test_close_restores_origin_without_reverting_live_profile(app, origin):
    app.mode = origin
    command(app)
    command(app, "preset", "high")
    UI.handle_key(app, "esc")
    assert app.mode == origin
    assert app.test_settings.preset == "high"


def test_presets_apply_immediately_without_io_and_custom_starts_default(app):
    command(app)
    click(app, "preset ultra")
    assert app.test_settings.preset == "ultra"
    assert app.test_settings.calls == [("apply", "ultra", None, False)]
    assert app.research.pending is None
    click(app, "preset custom")
    assert app.test_settings.custom == {field["key"]: field["default"] for field in FIELDS}
    assert any(item[2]["action"] == ("command", UI.COMMAND + " custom")
               for item in (render(app) and app.telemetry_settings_ui_state["control_hits"]))


def test_custom_gear_only_activates_when_custom_selected(app):
    command(app)
    command(app, "custom")
    assert app.telemetry_settings_ui_state["page"] == "presets"
    assert "Select Custom" in app.telemetry_settings_ui_state["message"]
    render(app)
    assert all(item[2]["action"] != ("command", UI.COMMAND + " custom")
               for item in app.telemetry_settings_ui_state["control_hits"])
    click(app, "preset custom")
    click(app, "custom")
    assert app.telemetry_settings_ui_state["page"] == "custom"


def test_typed_values_recolor_before_apply_and_invalid_values_are_not_applied(app):
    custom(app)
    edit(app, "queue_seconds", "0.5")
    assert app.test_settings.custom["queue_seconds"] == 5
    rows = render(app)
    styles = [style for _, _, row in rows for value, style in row if "0.5_ s" in value]
    assert styles and palette.resolve(styles[0]).foreground == (255, 105, 180)
    UI.handle_key(app, "enter")
    command(app, "apply")
    assert app.test_settings.custom["queue_seconds"] == .5
    edit(app, "queue_seconds", "nan")
    assert UI.value_style(FIELDS[0], "nan")[1] == "invalid"
    UI.handle_key(app, "enter")
    command(app, "apply")
    assert app.telemetry_settings_ui_state["error"]
    assert app.test_settings.custom["queue_seconds"] == .5


def test_custom_draft_cancel_and_switch_preserve_last_applied_values(app):
    custom(app)
    edit(app, "queue_seconds", "3")
    UI.handle_key(app, "enter")
    command(app, "apply")
    command(app, "preset", "ultra")
    assert app.telemetry_settings_ui_state["page"] == "presets"
    command(app, "preset", "custom")
    assert app.test_settings.custom["queue_seconds"] == 3
    command(app, "custom")
    edit(app, "queue_seconds", "2")
    UI.handle_key(app, "esc")
    assert app.telemetry_settings_ui_state["draft"]["queue_seconds"] == "3"
    edit(app, "queue_seconds", "1")
    UI.handle_key(app, "enter")
    command(app, "cancel")
    assert app.test_settings.custom["queue_seconds"] == 3
    assert app.telemetry_settings_ui_state["draft"]["queue_seconds"] == "3"


def test_config_save_is_deferred_and_blocks_profile_changes_until_completion(app):
    custom(app)
    edit(app, "queue_seconds", "2.5")
    UI.handle_key(app, "enter")
    command(app, "save")
    assert app.test_settings.custom["queue_seconds"] == 2.5
    assert app.research.pending
    assert not any(call[0] == "write" for call in app.test_settings.calls)
    command(app, "preset", "ultra")
    assert app.test_settings.preset == "custom"
    app.research.finish()
    assert [call[0] for call in app.test_settings.calls][-2:] == ["write", "saved"]
    assert not app.telemetry_settings_ui_state["busy"]


def test_save_failure_preserves_live_settings_and_reports_full_issue(app):
    command(app)
    command(app, "preset", "high")
    app.test_settings.write_error = OSError("Read-only configuration destination")
    command(app, "save")
    app.research.finish()
    assert app.test_settings.preset == "high"
    assert "Read-only configuration destination" in app.telemetry_settings_ui_state["error"]


def test_busy_background_lane_does_not_reapply_or_write_on_paint(app):
    command(app)
    app.research.pending = (None, None)
    command(app, "save")
    assert not app.telemetry_settings_ui_state["busy"]
    assert "background reader is busy" in app.telemetry_settings_ui_state["error"]
    for _ in range(4):
        render(app)
        UI.handle_mouse(app, 2, 4, button="motion")
    assert app.test_settings.calls == []


def test_stale_mouse_hits_are_invalidated_after_preset_and_resize(app):
    command(app)
    render(app)
    y, _, item = next(hit for hit in app.telemetry_settings_ui_state["control_hits"]
                      if hit[2]["action"] == ("command", UI.COMMAND + " preset high"))
    app.width = 60
    UI.handle_mouse(app, y, item["left"])
    assert app.test_settings.preset == "default"
    render(app)
    command(app, "preset", "medium")
    assert not app.telemetry_settings_ui_state["control_hits"]
    UI.handle_mouse(app, y, item["left"])
    assert app.test_settings.preset == "medium"


@pytest.mark.parametrize("width,height", [(100,36), (80,24), (40,15), (20,8), (5,5), (1,1), (0,0)])
@pytest.mark.parametrize("ascii_", [False, True])
def test_small_terminals_never_publish_offscreen_controls(app, width, height, ascii_):
    app.width, app.height = width, height
    custom(app)
    rows = render(app, ascii_)
    for y, x, row in rows:
        assert 0 <= y < height and 0 <= x < width
        assert x + L.vlen(L.row_text(row)) <= width
        if ascii_:
            assert L.row_text(row).isascii()
    for y, _, item in app.telemetry_settings_ui_state["control_hits"]:
        assert 0 <= y < height
        assert 0 <= item["left"] < item["right"] <= width


def test_keyboard_focus_scrolls_to_fields_and_wheel_is_consumed(app):
    app.height = 16
    custom(app)
    command(app, "edit", "research_seconds")
    render(app)
    assert app.telemetry_settings_ui_state["top"] > 0
    assert "Research evidence" in text(render(app))
    UI.handle_key(app, "esc")
    assert UI.handle_mouse(app, 1, 1, button="wheel-down")
    assert UI.handle_mouse(app, 1, 1, button="release")
    assert UI.handle_mouse(app, 1, 1, button="motion")


def test_light_mode_and_limits_are_explicit(app):
    app.test_settings.heavy = False
    command(app)
    contents = text(render(app))
    assert "Light mode: Heavy rates are staged" in contents
    assert "Scheduler measurement cadence still applies." in contents
    command(app, "preset", "ultra")
    assert app.test_settings.preset == "ultra"


def test_pending_review_blocks_open_and_editor_typing_does_not_trigger_selection(app):
    app.mode = "confirm"
    command(app)
    assert app.mode == "confirm"
    app.mode = "main"
    custom(app)
    from raven.text_selection import handle_key
    assert not handle_key(app, "v")


def test_gradient_is_continuous_directional_and_theme_accessible(app):
    white = palette.resolve(UI.value_style(FIELDS[0], "5")[0]).foreground
    middle = palette.resolve(UI.value_style(FIELDS[0], "2.75")[0]).foreground
    pink = palette.resolve(UI.value_style(FIELDS[0], ".5")[0]).foreground
    assert white == palette.resolve("white").foreground
    assert white != middle != pink
    assert pink == (255, 105, 180)
    assert UI.value_style(FIELDS[0], "10")[1] == "0% intensity"
    assert UI.value_style(FIELDS[0], "")[1] == "invalid"
    assert palette.resolve(UI.value_style(FIELDS[0], "5")[0], "light").foreground == palette.resolve("white", "light").foreground


def test_real_model_roundtrip_preserves_budget_integers_and_unrelated_configuration(app, monkeypatch, tmp_path):
    import json
    from raven import raven_config, telemetry_settings as model
    path = tmp_path / "raven.json"
    document = {"schema": raven_config.SCHEMA, "profile": "default-slurm",
                "allocation": {"account": "professor-lab", "partition": "shared"}}
    path.write_text(json.dumps(document), encoding="utf-8")
    app.cfg.set("raven", {**copy.deepcopy(raven_config.DEFAULTS), "managed": True})
    app.raven_resolved = raven_config.resolve(str(path), env={}, system_policy_paths=())
    app.raven_ui_state["snapshot"] = {"state": "heavy", "mode": "heavy"}
    app.research.set_refresh_interval = lambda seconds, **kwargs: setattr(app.research, "refresh_seconds", seconds)
    monkeypatch.setattr(UI, "_model", lambda: model)
    custom(app)
    expected = model.settings_snapshot(app)["custom"]
    assert UI._parsed(app.telemetry_settings_ui_state) == expected
    assert app.telemetry_settings_ui_state["draft"]["file_bytes_per_minute"] == "16777216"
    command(app, "apply")
    assert model.settings_snapshot(app)["custom"] == expected
    edit(app, "live_seconds", ".75")
    UI.handle_key(app, "enter")
    command(app, "save")
    assert model.settings_snapshot(app)["effective"]["live_seconds"] == .75
    assert json.loads(path.read_text()) == document
    app.research.finish()
    saved = json.loads(path.read_text())
    assert saved["allocation"] == document["allocation"]
    assert saved["telemetry"]["custom"]["live_seconds"] == .75
    assert saved["telemetry"]["custom"]["file_bytes_per_minute"] == 16777216
    assert saved["telemetry"]["preset"] == "custom"


def test_unmanaged_session_reports_configuration_need_without_crashing(app, monkeypatch):
    from raven import telemetry_settings as model
    monkeypatch.setattr(UI, "_model", lambda: model)
    command(app)
    assert app.mode == "main"
    assert "managed site profile" in app.telemetry_settings_ui_state["error"]


def test_saving_preset_never_silently_applies_an_unfinished_custom_draft(app):
    custom(app)
    edit(app, "queue_seconds", "3")
    UI.handle_key(app, "enter")
    command(app, "preset", "high")
    command(app, "save")
    app.research.finish()
    assert app.test_settings.preset == "high"
    request = next(call[1] for call in app.test_settings.calls if call[0] == "write")
    assert request["preset"] == "high"
    assert request["custom"]["queue_seconds"] == 5
    assert app.telemetry_settings_ui_state["draft"]["queue_seconds"] == "3"


def test_render_and_typing_do_not_read_or_write_files(app, monkeypatch):
    from pathlib import Path
    from raven import raven_config, telemetry_settings as model
    app.cfg.set("raven", {**copy.deepcopy(raven_config.DEFAULTS), "managed": True})
    app.research.set_refresh_interval = lambda seconds, **kwargs: None
    monkeypatch.setattr(UI, "_model", lambda: model)
    custom(app)
    def forbidden(*args, **kwargs):
        raise AssertionError("UI paint or typing attempted filesystem I/O")
    with monkeypatch.context() as guard:
        guard.setattr("builtins.open", forbidden)
        guard.setattr(Path, "open", forbidden)
        guard.setattr(Path, "stat", forbidden)
        for width, height in ((100, 32), (80, 24), (40, 15)):
            app.width, app.height = width, height
            render(app)
        edit(app, "queue_seconds", "1.25")
        render(app)
    assert app.telemetry_settings_ui_state["editing"] == ("queue_seconds", "1.25")


def test_toml_formatting_notice_and_persistence_warnings_are_visible(app, monkeypatch):
    original = app.test_settings.settings_snapshot
    monkeypatch.setattr(app.test_settings, "settings_snapshot", lambda owner: {
        **original(owner), "config_path": "/home/student/.config/raven/config.toml"})
    monkeypatch.setattr(app.test_settings, "save_settings", lambda request: {
        "message": "saved", "warnings": ["Directory durability could not be confirmed."]})
    command(app)
    assert "rewrites comments and formatting" in text(render(app))
    command(app, "save")
    app.research.finish()
    assert "Directory durability could not be confirmed." in app.telemetry_settings_ui_state["message"]


def test_compact_editor_opens_with_visible_focused_field_and_value(app):
    app.width, app.height = 40, 15
    custom(app)
    contents = text(render(app))
    assert "> Queue updates" in contents
    assert "5 s" in contents
    UI.handle_key(app, "enter")
    assert app.telemetry_settings_ui_state["editing"][0] == "queue_seconds"
