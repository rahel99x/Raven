"""Setup remains responsive, scopes account edits, and keeps allocation review."""
import copy
from types import SimpleNamespace

import pytest

from raven import layout as L, setup_ui as S, toolbar as T
from raven.config import Config
from raven.controller import App
from raven.model import Store
from raven.raven_config import DEFAULTS


class Background:
    demo = False

    def __init__(self):
        self.pending = None
        self.settings = {}

    def start_task(self, read, completed):
        if self.pending:
            return False
        self.pending = read, completed
        return True

    def finish(self):
        read, completed = self.pending
        self.pending = None
        try:
            value = read()
        except Exception as exc:
            value = exc
        completed(value)


class Service:
    def __init__(self, app):
        self.app, self.calls = app, []

    def discover(self, *, refresh=False):
        self.calls.append("refresh" if refresh else "discover")
        return {"username": "student", "cluster": "campus", "accounts": [
            {"account": "student", "cluster": "campus", "partition": "", "qos": [], "default": True},
            {"account": "labx", "cluster": "campus", "partition": "compute", "qos": ["normal"], "default": False}],
            "partitions": ["compute"], "errors": []}

    def stage(self, changes):
        self.calls.append(("stage", changes))
        return changes

    def save(self, changes):
        self.calls.append(("save", changes))
        data = copy.deepcopy(self.app.cfg["raven"])
        data["allocation"].update(changes)
        return {"allocation": data["allocation"], "resolved": object(), "warnings": []}


@pytest.fixture
def app(monkeypatch):
    cfg = Config()
    cfg.set("raven", copy.deepcopy(DEFAULTS))
    value = App(Store(persist=False), None, None, cfg, "student")
    value.width, value.height = 100, 36
    value.raven_resolved = object()
    value.research = Background()
    service = Service(value)
    value.test_service = service
    monkeypatch.setattr(S, "_service", lambda _: service)
    return value


def render(app):
    return S.overlay(SimpleNamespace(g=L.Glyphs(False)), {}, app, app.width, app.height)


def click(app, command):
    render(app)
    y, _, control = next(hit for hit in app.setup_ui_state["control_hits"] if hit[2]["action"] == ("command", command))
    S.handle_mouse(app, y, control["left"])


def open_allocation(app):
    S.run_command(app, ["setup", "allocation"])
    assert app.research.pending
    app.research.finish()


def action(app, command):
    index = next(index for index, item in enumerate(S._actions(app)) if item[1] == command)
    S.run_command(app, ["setup", "action", str(index)])


def test_setup_menu_contains_every_named_view_and_opens_internal_page(app):
    from raven.research import RESEARCH_VIEWS
    from raven.views import ANALYTICS_VIEWS
    items = T.menu_items(app, "Setup")
    commands = {item.command for item in items}
    assert all("setup research " + key in commands for key, _ in RESEARCH_VIEWS)
    assert all("setup analytics " + key in commands for key, _ in ANALYTICS_VIEWS)
    app.mode = "help"
    T._activate(app, next(item for item in items if item.command == "setup research experiment"))
    assert app.mode == "setup" and app.setup_ui_state["page"] == "research.experiment"
    assert not app.test_service.calls and not app.research.pending
    S.handle_key(app, "esc")
    assert app.mode == "help"


def test_account_discovery_is_background_only_and_paint_never_queries(app):
    S.run_command(app, ["setup", "allocation"])
    assert not app.test_service.calls
    for _ in range(3):
        render(app)
        S.handle_mouse(app, 10, 10, button="motion")
    assert not app.test_service.calls
    app.research.finish()
    assert app.test_service.calls == ["discover"]
    assert app.setup_ui_state["discovery"]["username"] == "student"


def test_explicit_discovery_refresh_reaches_backend_only_in_background(app):
    open_allocation(app)
    for _ in range(3):
        render(app)
    assert app.test_service.calls == ["discover"]
    action(app, "setup refresh")
    assert app.research.pending is not None
    assert app.test_service.calls == ["discover"]
    render(app)
    assert app.test_service.calls == ["discover"]
    app.research.finish()
    assert app.test_service.calls == ["discover", "refresh"]


def test_account_selection_save_updates_real_config_without_request(app):
    app.cfg.set("account", "monitored-lab")
    app.sampler = SimpleNamespace(account="monitored-lab")
    open_allocation(app)
    action(app, "setup accounts")
    click(app, "setup account 1")
    state = app.setup_ui_state
    assert state["form"]["account"] == "labx" and state["form"]["partition"] == "compute"
    assert app.cfg.get("raven.allocation.account") == ""
    action(app, "setup save")
    assert app.cfg.get("account") == "monitored-lab" and state["busy"] == "Saving settings"
    app.research.finish()
    assert app.cfg.get("account") == app.sampler.account == "monitored-lab"
    assert app.cfg.get("raven.allocation.account") == "labx"
    assert app.cfg.raven_resolved is app.raven_resolved
    assert not state["dirty"] and app.mode == "setup"
    assert "No allocation was requested" in app.message


def test_explicit_default_account_clears_billing_and_does_not_choose_first_association(app):
    app.cfg.set("raven.allocation.account", "labx")
    open_allocation(app)
    assert app.setup_ui_state["form"]["account"] == "labx"
    action(app, "setup accounts")
    action(app, "setup account default")
    assert app.setup_ui_state["form"]["account"] == ""
    assert app.cfg.get("raven.allocation.account") == "labx"


def test_foreign_cluster_association_is_visible_but_cannot_change_current_profile(app):
    open_allocation(app)
    app.setup_ui_state["discovery"]["accounts"].append({"account": "otherlab", "cluster": "other-campus",
        "partition": "gpu", "qos": []})
    action(app, "setup accounts")
    assert any("otherlab" in line and "switch site" in line for line, _ in S._document(app))
    S.run_command(app, ["setup", "account", "2"])
    assert app.setup_ui_state["form"]["account"] == ""
    assert "belongs to other-campus" in app.message


def test_account_selection_clears_qos_not_advertised_by_new_association(app):
    app.cfg.set("raven.allocation.qos", "personal-only")
    open_allocation(app)
    action(app, "setup accounts")
    S.run_command(app, ["setup", "account", "1"])
    assert app.setup_ui_state["form"]["qos"] == ""
    assert "qos" in app.setup_ui_state["dirty"]


def test_field_editor_uses_units_and_preserves_old_value_on_escape(app):
    open_allocation(app)
    click(app, "setup edit memory_mib")
    S.handle_key(app, "ctrl-u")
    for char in "8192":
        S.handle_key(app, char)
    S.handle_key(app, "enter")
    assert app.setup_ui_state["form"]["memory_mib"] == "8192"
    S.run_command(app, ["setup", "edit", "memory_mib"])
    S.handle_key(app, "ctrl-u")
    S.handle_key(app, "esc")
    assert app.setup_ui_state["form"]["memory_mib"] == "8192"
    assert app.cfg.get("raven.allocation.memory_mib") == 4096


def test_human_resource_units_stage_normalized_values_only_on_explicit_save(app):
    open_allocation(app)
    for field, text in (("memory_mib", "8 GiB"), ("walltime_seconds", "1h")):
        S.run_command(app, ["setup", "edit", field])
        S.handle_key(app, "ctrl-u")
        for char in text:
            S.handle_key(app, "space" if char == " " else char)
        S.handle_key(app, "enter")
        assert app.setup_ui_state["form"][field] == text
    assert app.cfg.get("raven.allocation.memory_mib") == 4096
    assert app.test_service.calls == ["discover"]
    action(app, "setup save")
    assert app.test_service.calls == ["discover"]
    app.research.finish()
    assert app.test_service.calls[-2:] == [
        ("stage", {"memory_mib": 8192, "walltime_seconds": 3600}),
        ("save", {"memory_mib": 8192, "walltime_seconds": 3600})]
    assert app.cfg.get("raven.allocation.memory_mib") == 8192
    assert app.cfg.get("raven.allocation.walltime_seconds") == 3600


@pytest.mark.parametrize("field,text", [("memory_mib", "8 XB"), ("walltime_seconds", "1 fortnight")])
def test_invalid_human_units_leave_form_without_starting_save(app, field, text):
    open_allocation(app)
    S.run_command(app, ["setup", "edit", field])
    S.handle_key(app, "ctrl-u")
    for char in text:
        S.handle_key(app, "space" if char == " " else char)
    S.handle_key(app, "enter")
    action(app, "setup save")
    assert app.research.pending is None
    assert app.test_service.calls == ["discover"]
    assert app.setup_ui_state["form"][field] == text
    assert field in app.setup_ui_state["dirty"]
    assert "Cannot save allocation settings" in app.message


def test_saving_blocks_new_account_edits_and_compute_review(app):
    open_allocation(app)
    action(app, "setup accounts")
    S.run_command(app, ["setup", "account", "1"])
    action(app, "raven request")
    assert app.mode == "setup" and "Save or discard" in app.message
    action(app, "setup save")
    S.run_command(app, ["setup", "accounts"])
    S.run_command(app, ["setup", "account", "0"])
    assert app.setup_ui_state["form"]["account"] == "labx"
    app.research.finish()
    assert app.cfg.get("raven.allocation.account") == "labx"


@pytest.mark.parametrize("review", ["job", "execution", "allocation"])
def test_setup_cannot_replace_pending_review(app, review):
    if review == "job":
        app.mode = "confirm"
    elif review == "execution":
        app.execution_state["pending_action"] = {"action": "submit"}
    else:
        app.raven_ui_state["review"] = {"action": "request"}
        app.mode = "raven"
    before = app.mode
    S.run_command(app, ["setup", "allocation"])
    assert app.mode == before and not app.research.pending
    assert "pending job review" in app.message


def test_stale_mouse_hit_after_resize_page_or_discovery_does_not_apply(app):
    open_allocation(app)
    render(app)
    y, _, control = next(hit for hit in app.setup_ui_state["control_hits"] if hit[2]["action"] == ("command", "setup edit account"))
    app.width += 1
    S.handle_mouse(app, y, control["left"])
    assert app.setup_ui_state["editing"] is None
    S.run_command(app, ["setup", "project"])
    S.handle_mouse(app, y, control["left"])
    assert app.setup_ui_state["editing"] is None and app.mode == "setup"


def test_discovery_does_not_publish_to_changed_connection(app):
    S.run_command(app, ["setup", "allocation"])
    app.raven_resolved = object()
    app.research.finish()
    assert app.setup_ui_state["discovery"] is None
    assert "Connection changed" in app.message


def test_demo_has_no_discovery_or_persistent_write(app):
    app.research.demo = True
    S.run_command(app, ["setup", "allocation"])
    assert app.research.pending is None and not app.test_service.calls
    app.setup_ui_state["dirty"].add("account")
    S.run_command(app, ["setup", "save"])
    assert app.research.pending is None and not app.test_service.calls


def test_standalone_desktop_explains_no_slurm_billing_and_has_no_save_form(app):
    app.cfg.set("raven.allocation.adapter", "desktop")
    open_allocation(app)
    commands = {command for _, command in S._document(app)}
    assert not any(command and command.startswith("setup edit") for command in commands)
    assert not any(item[1] == "setup save" for item in S._actions(app))
    assert any("no Slurm billing" in line for line in S._allocation_lines(app))


@pytest.mark.parametrize("width,height", [(0, 0), (1, 1), (3, 2), (10, 6), (24, 12), (80, 24), (120, 40)])
def test_setup_layout_and_hits_stay_inside_terminal(app, width, height):
    app.width, app.height = width, height
    S.run_command(app, ["setup", "research", "experiment"])
    for y, x, row in render(app):
        assert 0 <= y < height and 0 <= x < width
        assert x + L.vlen(L.row_text(row)) <= width
    for y, _, control in app.setup_ui_state["control_hits"]:
        assert 0 <= y < height and 0 <= control["left"] < control["right"] <= width


def test_keyboard_reaches_offscreen_fields_then_scrolls_full_recipe(app):
    app.height = 12
    open_allocation(app)
    for _ in range(17):
        S.handle_key(app, "down")
        render(app)
    assert app.setup_ui_state["top"] > 0
    S.run_command(app, ["setup", "project"])
    S.handle_key(app, "end")
    rows = render(app)
    assert app.setup_ui_state["top"] > 0 and rows


def test_setup_blurs_embedded_terminal_and_palette_action_returns_to_setup(app, monkeypatch):
    from raven import terminal_ui
    calls = []
    monkeypatch.setattr(terminal_ui, "focused", lambda _: True)
    monkeypatch.setattr(terminal_ui, "blur", lambda _: calls.append("blur"))
    S.run_command(app, ["setup", "project"])
    assert calls == ["blur"]
    action(app, "metrics ")
    assert app.mode == "palette" and app.palette_edit == "metrics "
    assert app.command_state["origin_mode"] == "setup"


@pytest.mark.parametrize("view", ["job", "history", "timeline", "advisor", "compare"])
def test_analytics_open_button_exposes_actual_destination(app, view):
    S.run_command(app, ["setup", "analytics", view])
    action(app, "view " + view)
    assert app.mode == "main" and app.tab == "analytics" and app.analytics_view == view
    assert S.overlay(SimpleNamespace(g=L.Glyphs(False)), {}, app, app.width, app.height) is None


def test_source_health_button_exposes_sources_without_setup_overlay(app):
    S.run_command(app, ["setup", "telemetry"])
    action(app, "tab sources")
    assert app.mode == "main" and app.tab == "sources"


def test_compute_controls_preserve_own_modal_and_return_to_setup(app):
    open_allocation(app)
    action(app, "raven status")
    assert app.mode == "raven" and app.raven_ui_state["return_mode"] == "setup"
    app.run_command("raven close")
    assert app.mode == "setup"


def test_unknown_topic_reports_error_without_leaving_page(app):
    S.run_command(app, ["setup", "research", "unknown"])
    assert app.mode == "main" and "Unknown Setup page" in app.message


def test_account_discovery_notice_does_not_clutter_another_guide(app):
    S.run_command(app, ["setup", "allocation"])
    S.run_command(app, ["setup", "research", "experiment"])
    app.research.finish()
    assert app.setup_ui_state["discovery"]["username"] == "student"
    assert not app.setup_ui_state["message"]
    S.run_command(app, ["setup", "allocation"])
    S._say(app, "Allocation preferences saved.")
    S.run_command(app, ["setup", "analytics", "job"])
    assert not app.setup_ui_state["message"]


@pytest.mark.parametrize("width,height", [(80,24), (120,40), (200,50)])
@pytest.mark.parametrize("ascii_", [False,True])
def test_all_setup_menu_and_allocation_controls_are_keyboard_reachable(app, width, height, ascii_):
    app.width, app.height = width, height
    views = SimpleNamespace(g=L.Glyphs(ascii_))
    T.render_bar(views, app, width)
    T.run_command(app, ["menu", "Setup"])
    seen = set()
    entries = T.menu_items(app, "Setup")
    for _ in entries:
        T.overlay(views, {}, app, width, height)
        seen.update(hit[3] for hit in app.toolbar_state["menu_hits"])
        T.handle_key(app, "down")
    assert seen == {item.key for item in entries}
    T._close(app)
    open_allocation(app)
    expected = {command for _, command in S._document(app) if command}
    seen = set()
    for _ in expected:
        S.handle_key(app, "down")
        S.overlay(views, {}, app, width, height)
        seen.update(hit[2]["action"][1] for hit in app.setup_ui_state["control_hits"])
    assert seen == expected
    S.handle_key(app, "end")
    rows = S.overlay(views, {}, app, width, height)
    text = "\n".join(L.row_text(row) for _, _, row in rows)
    assert "PgUp/PgDn: scroll" in text
