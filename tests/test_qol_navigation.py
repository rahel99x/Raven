"""Identity, editing, safe paste, settings, and actual navigation overlays."""
from __future__ import annotations

import copy
import curses
import json
from types import SimpleNamespace

import pytest

from raven import command_ui, navigation_tools as N, navigation_ui, refresh_rate as R, workbench
from raven.config import Config
from raven.controller import App
from raven.layout import Glyphs, row_text, vlen
from raven.model import Finished, GpuSample, Health, Job, Live, Node, NodeCell, Partition, Store
from raven.sampler import Sampler
from raven.views import Views


@pytest.fixture
def dashboard(tmp_path, monkeypatch):
    if "navigation_tools" not in workbench.FEATURES:
        monkeypatch.setattr(workbench, "FEATURES", workbench.FEATURES + ("navigation_tools",))
    workbench.modules.cache_clear()
    cfg = Config({"clipboard": {"osc52": False, "tools": False}, "log_lines": 0})
    store = Store(state_dir=str(tmp_path / "state"))
    store.apply_jobs([Job("9", "training 界 with a very long exact name", "main", "RUNNING", cpus=4, elapsed="00:02:00", limit="01:00:00", nodelist="node[001-120]", mem_req="16G"),
                      Job("10", "pending", "main", "PENDING", cpus=8)])
    store.finished = [Finished("123_2", "failed run", "FAILED", elapsed="00:02:00", cpus=4, cpu_time=60, req_mem=4096, rss=1024)]
    store.live["9"] = Live(rate=.5, avg=.25, rss=1024, cpu_time=120)
    store.gpu["9"] = [GpuSample("node1", 0, 20, 10, 100), GpuSample("node1", 1, 40, 10, 100)]
    store.health = {name: Health(name) for name in ("jobs", "live", "finished", "gpu")}
    app = App(store, None, None, cfg, "test")
    views = Views(Glyphs(False), cfg)
    app.views_ref = views
    views.compose(store.snapshot(), app, 120, 40)
    app.selected_id = "9"
    yield app, views, store
    if app.research:
        app.research.close()
    workbench.modules.cache_clear()


@pytest.fixture
def native_polling(dashboard):
    app, _, store = dashboard
    worker = Sampler(SimpleNamespace(), store, dict(app.cfg["intervals"]), [])
    app.sampler = worker
    R.set_multiplier(app, R.multiplier(app))
    try:
        yield app, worker
    finally:
        worker.shutdown()


def test_forward_restores_source_identity_and_cascades(dashboard):
    app, views, store = dashboard
    app.table_state["sorts"]["jobs"] = [("id", "asc")]
    app.enter_tab("history")
    app.selected_id = "123_2"
    app.open_log("123_2")
    app.logs.entry = {"path": "/tmp/failed attempt/stderr.log", "label": "Worker stderr"}
    app.logs.path, app.logs.top, app.logs.cursor = app.logs.entry["path"], 17, 19
    assert navigation_ui.back(app)
    assert app.tab == "history"
    assert navigation_ui.forward(app)
    assert app.tab == "log" and app.log_job == "123_2"
    assert app.logs.path == "/tmp/failed attempt/stderr.log" and app.logs.top == 17
    assert app.table_state["sorts"]["jobs"] == [("id", "asc")]
    navigation_ui.back(app)
    app.enter_tab("sources")
    assert not navigation_ui.forward(app)


def test_location_persists_exact_context_and_ignores_opaque_results(dashboard):
    app, views, store = dashboard
    app.filter = "training"
    app.table_state["sorts"]["jobs"] = [("name", "asc"), ("cpus", "desc")]
    app.analysis_state["comparison"] = object()
    app.run_command("location save 'training view'")
    assert app.command_ok
    payload = N.save(app)
    json.dumps(payload)
    saved = payload["locations"]["training view"]
    assert saved["filter"] == "training" and "comparison" not in saved.get("analysis_context", {})
    app.enter_tab("history")
    app.run_command("location open 'training view'")
    assert app.tab == "jobs" and app.filter == "training" and app.selected_id == "9"
    assert app.table_state["sorts"]["jobs"] == [["name", "asc"], ["cpus", "desc"]]
    reloaded = App(store, None, None, Config(), "test")
    try:
        assert "training view" in N.initialize(reloaded)["locations"]
    finally:
        if reloaded.research:
            reloaded.research.close()


@pytest.mark.parametrize("value", [{"tab": "bad"}, {"tab": "jobs", "logs": {"cursor": -1}}, {"tab": "jobs", "logs": "bad"}, {"tab": "jobs", "filter": "\x1b[31m"}])
def test_invalid_saved_location_is_ignored(dashboard, value):
    app, _, _ = dashboard
    N.restore(app, {"locations": {"unsafe": value}})
    assert "unsafe" not in N.initialize(app)["locations"]


def test_saved_location_profile_must_match(dashboard):
    app, _, _ = dashboard
    app.run_command("location save here")
    app.profile_name = "other-cluster"
    app.run_command("location open here")
    assert not app.command_ok and "profile" in app.message


def test_saved_log_location_rejects_changed_source_connection_before_navigation(dashboard):
    app, _, _ = dashboard
    app.tab = "log"
    app.run_command("location save remote")
    app.navigation_tools_state["locations"]["remote"]["source_target"] = "other-login-node"
    app.tab = "jobs"
    app.run_command("location open remote")
    assert not app.command_ok and app.tab == "jobs" and "connection" in app.message


def test_jump_search_indexes_every_declared_kind_and_uses_cache(dashboard, monkeypatch):
    app, _, _ = dashboard
    app.project_state["runs"] = [{"run_id": "run-42", "experiment_id": "test"}]
    app.logs.entries = [{"path": "/tmp/worker42.log", "label": "worker 42", "job_id": "9"}]
    app.table_state["views"]["my gpu failures"] = {}
    app.run_command("location save review")
    app.run_command("jump")
    entries = N.jump_matches(app)
    assert {"job", "run", "log", "view", "workspace", "command", "location"} <= {entry["kind"] for entry in entries}
    monkeypatch.setattr(app.store, "snapshot", lambda: pytest.fail("cached search must not sample"))
    app.navigation_tools_state["query"] = "9"
    assert N.jump_matches(app)[0]["value"] == "9"


def test_jump_command_opens_unexecuted_palette_and_exact_log(dashboard):
    app, _, _ = dashboard
    N._activate_jump(app, {"kind": "command", "value": "cancel"})
    assert app.mode == "palette" and app.palette_edit == "cancel "
    N._activate_jump(app, {"kind": "log", "value": "/tmp/attempt42/stderr.log", "entry": {"path": "/tmp/attempt42/stderr.log", "job_id": "123_2"}})
    assert app.tab == "log" and app.log_job == "123_2" and app.logs.entry["path"].endswith("stderr.log")


def test_settings_preview_cancel_and_apply_update_sampler(native_polling):
    app, worker = native_polling
    original_bases = dict(worker.intervals)
    original_theme = app.theme
    app.run_command("settings")
    N.handle_key(app, "right")
    assert app.theme != original_theme
    N.handle_key(app, "esc")
    assert app.theme == original_theme
    app.run_command("settings")
    keys = N._setting_keys(app)
    app.navigation_tools_state["cursor"] = keys.index("intervals.jobs")
    N.handle_key(app, "left")
    actual = R.poll_interval(R.poll_position(4))
    assert worker.effective_interval("jobs") == actual
    assert all(worker.effective_interval(source) == actual for source in ("live", "gpu", "trace"))
    assert worker.intervals == original_bases
    N.handle_key(app, "enter")
    assert N.save(app)["settings"]["intervals.jobs"] == actual
    assert app.mode == "main"


@pytest.mark.parametrize("settings", [{"intervals.jobs": 0}, {"intervals.jobs": .49}, {"intervals.jobs": 5.01},
    {"intervals.jobs": 10**1000}, {"intervals.jobs": float("nan")}, {"mouse": "false"}, {"theme": "invalid"}, {"intervals.jobs": True}])
def test_settings_restore_rejects_bad_preferences(dashboard, settings):
    app, _, _ = dashboard
    original = dict(N.save(app)["settings"])
    N.restore(app, {"settings": settings})
    assert N.save(app)["settings"] == original


def test_polling_settings_cancel_restores_exact_position_and_other_saves_keep_original(native_polling, dashboard):
    app, worker = native_polling
    _, _, store = dashboard
    R.set_multiplier(app, 20)
    original = worker.effective_interval("jobs")
    assert app.cfg["intervals"]["jobs"] == 2 and original != 2
    app.run_command("settings")
    state = N.initialize(app)
    assert state["draft"]["intervals.jobs"] == original
    state["cursor"] = N._setting_keys(app).index("intervals.jobs")
    N.handle_key(app, "left")
    assert R.multiplier(app) > 20 and worker.effective_interval("jobs") < original
    assert state["draft"]["intervals.jobs"] == worker.effective_interval("jobs")
    app.save()
    fresh = App(store, None, None, Config({"log_lines": 0}), "test")
    try:
        assert R.multiplier(fresh) == 20
        assert N._read_setting(fresh, "intervals.jobs") == original
    finally:
        if fresh.research:
            fresh.research.close()
    N.handle_key(app, "esc")
    assert R.multiplier(app) == 20 and worker.effective_interval("jobs") == original
    assert app.cfg["intervals"]["jobs"] == 2
    assert state["preview_backup"] is None and state["polling_preview_backup"] is None


def test_polling_settings_apply_survives_restart_using_actual_cadence(native_polling, dashboard):
    app, worker = native_polling
    _, _, store = dashboard
    app.run_command("settings")
    state = N.initialize(app)
    state["cursor"] = N._setting_keys(app).index("intervals.jobs")
    N.handle_key(app, "left")
    N.handle_key(app, "left")
    position, actual = R.multiplier(app), worker.effective_interval("jobs")
    N.handle_key(app, "enter")
    assert N.save(app)["settings"]["intervals.jobs"] == actual
    fresh = App(store, None, None, Config({"log_lines": 0}), "test")
    try:
        assert R.multiplier(fresh) == position
        assert N._read_setting(fresh, "intervals.jobs") == actual
        assert fresh.cfg["intervals"]["jobs"] == 2
    finally:
        if fresh.research:
            fresh.research.close()


def test_slow_source_settings_keep_their_base_with_restored_polling_preference(native_polling):
    app, worker = native_polling
    R.set_multiplier(app, 33)
    actual = worker.effective_interval("jobs")
    N.restore(app, {"settings": {"intervals.jobs": 2, "intervals.live": 60}})
    assert R.multiplier(app) == 33 and worker.effective_interval("jobs") == actual
    assert worker.intervals["jobs"] == 2 and worker.intervals["live"] == 60
    assert worker.effective_interval("live") == pytest.approx(60 * actual / 5)
    R.set_multiplier(app, 1)
    assert worker.effective_interval("live") == 60
    R.set_multiplier(app, 33)
    assert N.save(app)["settings"]["intervals.jobs"] == actual


def test_polling_settings_are_bounded_and_cancelled_defaults_restore_previous_position(native_polling):
    app, worker = native_polling
    R.set_multiplier(app, 17)
    app.run_command("settings")
    state = N.initialize(app)
    state["cursor"] = N._setting_keys(app).index("intervals.jobs")
    for _ in range(40):
        N.handle_key(app, "left")
        assert .5 <= state["draft"]["intervals.jobs"] <= 5
        assert state["draft"]["intervals.jobs"] == worker.effective_interval("jobs")
    assert R.multiplier(app) == 50 and worker.effective_interval("jobs") == .5
    for _ in range(40):
        N.handle_key(app, "right")
        assert .5 <= state["draft"]["intervals.jobs"] <= 5
    assert R.multiplier(app) == 1 and worker.effective_interval("jobs") == 5
    N.handle_key(app, "D")
    assert state["draft"]["intervals.jobs"] == 5
    N.handle_key(app, "esc")
    assert R.multiplier(app) == 17


def test_settings_exposes_one_native_polling_control_and_preserves_other_source_edits(native_polling):
    app, worker = native_polling
    keys = N._setting_keys(app)
    assert keys.count("intervals.jobs") == 1
    assert not {"intervals.live", "intervals.gpu", "intervals.trace"}.intersection(keys)
    app.run_command("settings")
    state = N.initialize(app)
    state["cursor"] = keys.index("intervals.starts")
    N.handle_key(app, "right")
    assert worker.intervals["starts"] == app.cfg["intervals"]["starts"] == 12.5
    assert R.multiplier(app) == 1
    N.handle_key(app, "esc")
    assert worker.intervals["starts"] == app.cfg["intervals"]["starts"] == 10


def test_settings_polling_caption_and_help_show_requested_domain(native_polling, dashboard):
    app, _ = native_polling
    _, views, store = dashboard
    R.set_multiplier(app, 20)
    app.run_command("settings")
    N.initialize(app)["cursor"] = N._setting_keys(app).index("intervals.jobs")
    rows = N.overlay(views, store.snapshot(), app, 140, 30)
    text = "\n".join(row_text(row) for _, _, row in rows)
    assert "Polling interval (seconds)" in text and "0.5 to 5 seconds" in text
    assert "2.05" in text
    assert not any("Sample interval · " + source in text for source in ("live", "gpu", "trace"))


def test_keybinding_conflicts_test_and_apply_are_real(dashboard):
    app, _, _ = dashboard
    app.run_command("keybindings")
    state = N.initialize(app)
    state["cursor"] = list(state["binding_draft"]).index("quit")
    N.handle_key(app, "e")
    state["edit"] = "j"
    N.handle_key(app, "enter")
    assert state["editing"] and "conflict" in app.message.lower()
    state["edit"] = "ctrl-q"
    N.handle_key(app, "enter")
    N.handle_key(app, "t")
    N.handle_key(app, "ctrl-q")
    assert not app.quit and "quit" in state["test_message"]
    N.handle_key(app, "a")
    assert app.keymap["ctrl-q"] == "quit" and "q" not in app.keymap
    assert app.mode == "main"


@pytest.mark.parametrize("key", ["ctrl-g", "ctrl-b", "ctrl-p", "ctrl-w", "f6", "z", "alt-left", "alt-right"])
def test_key_editor_rejects_reserved_navigation_bindings(dashboard, key):
    app, _, _ = dashboard
    app.run_command("keybindings")
    state = N.initialize(app)
    state.update(editing=True, edit=key)
    N.handle_key(app, "enter")
    assert state["editing"] and "reserved" in app.message.lower()


def test_word_editing_undo_and_redo_do_not_trigger_session_actions(dashboard):
    app, _, _ = dashboard
    command_ui.open_palette(app, "log 'a file' JOBID")
    command_ui.handle_key(app, "ctrl-left")
    assert app.command_state["cursor"] == 13
    command_ui.handle_key(app, "ctrl-w")
    assert app.palette_edit == "log 'a JOBID"
    command_ui.handle_key(app, "alt-u")
    assert app.palette_edit == "log 'a file' JOBID"
    command_ui.handle_key(app, "alt-r")
    assert app.palette_edit == "log 'a JOBID"
    command_ui.handle_key(app, "ctrl-a")
    assert app.command_state["cursor"] == 0 and app.mode == "palette"
    command_ui.handle_key(app, "ctrl-k")
    assert app.palette_edit == ""


def test_palette_retains_origin_mode_on_cancel_and_run(dashboard, monkeypatch):
    app, _, _ = dashboard
    app.mode = "log_tools_page"
    command_ui.open_palette(app, "copy all")
    command_ui.handle_key(app, "esc")
    assert app.mode == "log_tools_page"
    command_ui.open_palette(app, "copy all")
    called = []
    monkeypatch.setattr(app, "run_command", lambda line: called.append((app.mode, line)))
    command_ui.handle_key(app, "enter")
    assert called == [("log_tools_page", "copy all")]


def test_paste_preserves_quotes_unicode_and_waits_for_explicit_enter(dashboard, monkeypatch):
    app, _, _ = dashboard
    calls = []
    monkeypatch.setattr(app, "run_command", lambda line: calls.append(line))
    assert command_ui.paste(app, "project '/tmp/界 with spaces'\n")
    assert app.mode == "palette" and not calls
    assert "界" in app.palette_edit and "\n" in app.palette_edit
    command_ui.handle_key(app, "enter")
    assert calls == ["project '/tmp/界 with spaces'\n"]


@pytest.mark.parametrize("text", ["cancel 9\x1b[31m", "cancel\x00 9", "cancel\x07 9"])
def test_paste_rejects_terminal_controls(dashboard, text):
    app, _, _ = dashboard
    assert not command_ui.paste(app, text)
    assert app.mode == "main"


def test_paste_is_one_undo_unit_and_input_is_bounded(dashboard):
    app, _, _ = dashboard
    command_ui.open_palette(app, "log ")
    command_ui.paste(app, "9" * 5000)
    assert len(app.palette_edit) == command_ui.MAX_INPUT
    command_ui.handle_key(app, "ctrl-z")
    assert app.palette_edit == "log "


def test_invalid_quote_keeps_editable_command_in_palette(dashboard, monkeypatch):
    app, _, _ = dashboard
    command_ui.open_palette(app, "project '/tmp/a")
    monkeypatch.setattr(app, "run_command", lambda _: pytest.fail("unfinished quote must not run"))
    command_ui.handle_key(app, "enter")
    assert app.mode == "palette" and app.palette_edit == "project '/tmp/a"
    assert command_ui.validation(app)[0] == "error"


@pytest.mark.parametrize("text", ["theme invalid", "days -1", "log 999", "not-a-command"])
def test_inline_validation_warns_without_dispatching(dashboard, text):
    app, _, _ = dashboard
    assert command_ui.validation(app, text)[0] == "warning"


def test_explain_reports_formula_and_actual_source(dashboard):
    app, _, _ = dashboard
    app.run_command("explain eff")
    assert app.mode == "field_explanation"
    assert "allocated cores" in N.initialize(app)["value"]
    assert "Source: jobs" not in N.initialize(app)["value"]


@pytest.mark.parametrize("column,expected", [("name", "training 界 with a very long exact name"), ("id", "9"), ("where", "node[001-120]"), ("cpu%", 50), ("eff", 25), ("gpu%", 30), ("cpus", 4)])
def test_peek_uses_exact_record_and_raw_observations(dashboard, column, expected):
    app, _, _ = dashboard
    assert N.raw_field(app, "jobs", column) == expected


def test_peek_copy_uses_full_raw_value_and_fixed_identity(dashboard, monkeypatch):
    app, views, _ = dashboard
    from raven import clipboard
    copied = []
    monkeypatch.setattr(clipboard, "copy", lambda text, *_args, **_kwargs: copied.append(text) or "copied")
    app.run_command("peek name")
    app.selected_id = "10"
    N.handle_key(app, "y")
    assert copied == ["training 界 with a very long exact name"]


def test_resource_peeks_read_full_selected_resource_records(dashboard):
    app, _, store = dashboard
    node = Node("node1", "mixed", cpus=64, alloc=12, load=4, mem_total=8192, mem_free=1024, gres="gpu:a100:4(S:0-3)")
    partition = Partition("gpu-long-name", "up", "1-00:00:00", 32, "10/12/10/32", "100/200/300/600", {"a100": {"free": 3, "total": 4, "used": 1, "down": 0}})
    store.nodes[node.name] = node
    store.partitions = [partition]
    store.health["jobs"].error = "Complete diagnostic with exact path /tmp/界/failure.log"
    app.table_tools_state["resource_ids"] = {"nodes": [node.name], "cluster": [partition.name]}
    app.source_ids = ["jobs"]
    assert N.raw_field(app, "nodes", "gres") == "gpu:a100:4(S:0-3)"
    assert N.raw_field(app, "nodes", "mem") == 7168 * 1024**2
    assert N.raw_field(app, "cluster", "name") == "gpu-long-name"
    assert json.loads(N.raw_field(app, "cluster", "gpus"))["a100"]["free"] == 3
    assert N.raw_field(app, "sources", "error").endswith("/tmp/界/failure.log")
    store.nodes.clear()
    store.nodemap[node.name] = NodeCell(node.name, cpus=64, cpus_alloc=12)
    assert N.raw_field(app, "nodes", "cpus") == "12/64"


def test_source_polling_peek_and_explanation_use_effective_native_interval(native_polling):
    app, worker = native_polling
    app.source_ids = ["jobs"]
    app.cursor["sources"] = 0
    R.set_multiplier(app, 50)
    assert worker.intervals["jobs"] == 2
    assert N.raw_field(app, "sources", "every") == .5
    N._field_open(app, "sources", "every")
    description = N.initialize(app)["value"]
    assert "Source: jobs. Polling interval: 500ms" in description
    assert "Configured interval: 2" not in description
    assert "backoff" in description
    N._field_open(app, "sources", "every", peek=True)
    assert N.initialize(app)["raw_value"] == "0.5"


def test_saved_location_captures_all_independent_table_settings(dashboard):
    from raven.table_tools import parse_rule
    app, _, _ = dashboard
    app.table_state["filters"] = {"jobs": "training", "history": "failed"}
    app.table_state["order"]["jobs"] = ["name", "id"]
    app.table_state["widths"]["jobs"] = {"name": 25}
    app.table_tools_state["numeric"] = {"jobs": [parse_rule("cpus>=4")]}
    app.table_tools_state["dates"] = {"start": 100, "end": 200, "label": "custom"}
    app.table_tools_state["recents"] = {"count": 25, "window": 86400, "auto": True}
    app.run_command("location save complete")
    saved = N.save(app)["locations"]["complete"]
    N._valid_location(saved)
    assert saved["table_context"]["filters"] == {"jobs": "training", "history": "failed"}
    assert saved["table_context"]["widths"]["jobs"] == {"name": 25}
    assert saved["table_tools_context"]["numeric"]["jobs"][0][0] == "cpus"
    assert saved["table_tools_context"]["dates"]["start"] == 100
    assert saved["table_tools_context"]["recents"]["count"] == 25


def test_saved_run_location_restores_original_manual_sources_after_restart_and_clear(dashboard, tmp_path):
    from raven.research import ResearchHub
    app, _, store = dashboard
    root = tmp_path / "portable project"
    run = root / "runs" / "attempt-a"
    (run / "logs").mkdir(parents=True)
    (root / ".raven" / "contracts").mkdir(parents=True)
    (root / ".raven" / "contracts" / "outputs.v1.json").write_text(json.dumps({"version": 1, "outputs": [{"path": "run.json", "format": "json"}]}))
    (run / "run.json").write_text(json.dumps({"schema": "raven.run/v1", "run_id": "attempt-a", "experiment_id": "experiment", "attempt": 1, "state": "COMPLETED",
                                             "paths": {"metrics": "metrics.jsonl", "stdout": "logs/stdout.log"}}))
    (run / "metrics.jsonl").write_text('{"schema":"raven.metric/v1","t":1,"values":{"loss":0.2}}\n')
    (run / "logs" / "stdout.log").write_text("exact declared run output\n")
    app.research = ResearchHub(app.cfg)
    original = {"metrics_file": str(tmp_path / "manual metrics.jsonl"), "contract": str(tmp_path / "manual contract.json"),
                "workdir": str(tmp_path), "passport": str(tmp_path / "manual passport.json")}
    manifest = str(tmp_path / "manual log index.json")
    app.research.configure(**original)
    app.cfg["logs"]["manifest_file"] = manifest
    app.run_command("project " + repr(str(root)))
    app.research.future.result(timeout=5)
    app.tick()
    app.run_command("run select attempt-a")
    app.research.future.result(timeout=5)
    app.tick()
    assert app.project_state["binding"]["run_id"] == "attempt-a"
    app.run_command("location save chosen-run")
    assert app.command_ok, app.message
    app.save()
    fresh = App(store, None, None, Config({"log_lines": 0}), "test")
    fresh.research = ResearchHub(fresh.cfg)
    try:
        fresh.run_command("location open chosen-run")
        assert fresh.command_ok and fresh.project_state["binding"]["run_id"] == "attempt-a"
        assert fresh.project_state["binding_backup"]["settings"] == original
        fresh.run_command("run clear")
        assert fresh.command_ok and fresh.project_state["binding"] is None
        assert {key: fresh.research.settings[key] for key in original} == original
        assert fresh.cfg["logs"]["manifest_file"] == manifest
    finally:
        fresh.research.close()


def test_back_and_forward_restore_published_log_source_page(dashboard):
    app, _, _ = dashboard
    page = {"rows": [{"raw": b"actual\n", "offset": 0, "line": 1}], "start": 0, "snapshot": {"ident": (1, 2)}}
    app.tab, app.mode = "log", "log_tools_page"
    app.log_tools_state.update(page=page, page_source={"path": "/tmp/older.log"}, page_cursor=0, page_pan=9)
    navigation_ui.record(app, "history", force=True)
    app.tab, app.mode = "history", "main"
    assert navigation_ui.back(app)
    assert app.mode == "log_tools_page" and app.log_tools_state["page"] is page
    assert app.log_tools_state["page_pan"] == 9
    assert navigation_ui.forward(app) and app.tab == "history"


def test_named_log_page_stores_descriptor_and_reloads_guarded_identity(dashboard, monkeypatch):
    from raven import log_tools
    app, _, _ = dashboard
    app.tab, app.mode = "log", "log_tools_page"
    app.log_tools_state.update(page={"rows": [{"raw": b"actual\n", "offset": 128, "line": 14}], "start": 128, "snapshot": {"ident": (1, 2)}},
                               page_source={"path": "/tmp/older.log", "job_id": "9", "target": "local"}, page_cursor=0, page_pan=9)
    app.run_command("location save older")
    assert app.command_ok, app.message
    saved = N.save(app)["locations"]["older"]
    json.dumps(saved)
    assert "log_tools_context" not in saved
    assert saved["log_page_descriptor"]["offset"] == 128
    called = []
    monkeypatch.setattr(log_tools, "restore_location", lambda _app, context: called.append(context) or True)
    app.run_command("location open older")
    assert called[0]["identity"] == [1, 2] and called[0]["page_pan"] == 9


def test_settings_cancel_returns_to_originating_inspector(dashboard):
    app, _, _ = dashboard
    app.mode = "analysis"
    app.analysis_state["modal"] = "inspect"
    app.run_command("settings")
    N.handle_key(app, "esc")
    assert app.mode == "analysis" and app.analysis_state["modal"] == "inspect"


@pytest.mark.parametrize("command,key,expected", [("theme cb", "theme", "cb"), ("density focused", "workspace.density", "focused"),
                                                 ("gpu off", "gpu_sampling", False), ("bell on", "bell", True)])
def test_later_legacy_settings_commands_survive_a_real_app_restart(dashboard, command, key, expected):
    app, _, store = dashboard
    app.run_command("settings")
    N.handle_key(app, "enter")
    app.run_command(command)
    app.save()
    fresh = App(store, None, None, Config({"log_lines": 0}), "test")
    try:
        assert N._read_setting(fresh, key) == expected
        assert N.save(fresh)["settings"][key] == expected
    finally:
        if fresh.research:
            fresh.research.close()


@pytest.mark.parametrize("applied_first", [False, True])
def test_unaccepted_live_settings_preview_does_not_persist_during_other_saves(dashboard, applied_first):
    app, _, store = dashboard
    if applied_first:
        app.run_command("settings")
        N.handle_key(app, "enter")
    original = {key: N._read_setting(app, key) for key in N._setting_keys(app)}
    app.run_command("settings")
    for key in ("theme", "workspace.density", "bell", "gpu_sampling", "intervals.jobs"):
        app.navigation_tools_state["cursor"] = N._setting_keys(app).index(key)
        N.handle_key(app, "left" if key == "intervals.jobs" else "right")
    assert app.theme != original["theme"]
    app.save()
    fresh = App(store, None, None, Config({"log_lines": 0, "clipboard": {"osc52": False, "tools": False}}), "test")
    try:
        assert {key: N._read_setting(fresh, key) for key in original} == original
    finally:
        if fresh.research:
            fresh.research.close()
    N.handle_key(app, "esc")
    assert {key: N._read_setting(app, key) for key in original} == original


def test_cancelled_preview_retains_later_keyboard_settings(dashboard):
    app, _, store = dashboard
    app.run_command("settings")
    N.handle_key(app, "enter")
    app.run_command("theme cb")
    app.run_command("density focused")
    app.run_command("gpu off")
    app.run_command("bell on")
    app.run_command("settings")
    N.handle_key(app, "D")
    N.handle_key(app, "esc")
    app.save()
    fresh = App(store, None, None, Config({"log_lines": 0}), "test")
    try:
        assert fresh.theme == "cb" and fresh.layout_state.density == "focused"
        assert fresh.gpu is False and fresh.bell is True
    finally:
        if fresh.research:
            fresh.research.close()


def test_explicit_launch_settings_survive_restore_and_editor_defaults(dashboard):
    app, _, _ = dashboard
    app.cfg.set("color", False)
    app.cfg.set("intervals.jobs", .5)
    app.cfg.ui_locked_settings = {"color", "intervals.jobs"}
    app.sampler = SimpleNamespace(intervals=dict(app.cfg["intervals"]), gpu_sampling=True)
    R.set_multiplier(app, 50)
    N.restore(app, {"settings": {"color": True, "intervals.jobs": 60}})
    assert app.cfg["color"] is False and app.sampler.intervals["jobs"] == .5
    assert N.save(app)["settings"]["color"] is True
    assert "intervals.jobs" not in N.save(app)["settings"]
    app.run_command("settings")
    state = N.initialize(app)
    state["cursor"] = N._setting_keys(app).index("color")
    N.handle_key(app, "right")
    assert state["draft"]["color"] is False and "launch" in app.message
    N.handle_key(app, "D")
    assert state["draft"]["color"] is False and state["draft"]["intervals.jobs"] == .5
    N.handle_key(app, "enter")
    assert app.cfg["color"] is False and app.sampler.intervals["jobs"] == .5
    assert N.save(app)["settings"]["color"] is True


def test_every_accepted_key_name_can_be_reported_by_the_decoder():
    from raven import screen
    reported = set(screen._ESCAPE_KEYS.values())
    codes = list(range(128)) + list(range(curses.KEY_F0 + 1, curses.KEY_F0 + 25))
    codes += [curses.KEY_UP, curses.KEY_DOWN, curses.KEY_LEFT, curses.KEY_RIGHT, curses.KEY_NPAGE, curses.KEY_PPAGE,
              curses.KEY_HOME, curses.KEY_END, curses.KEY_BTAB, curses.KEY_ENTER, curses.KEY_BACKSPACE, curses.KEY_DC]
    reported.update(filter(None, (screen.key_name(code, curses) for code in codes)))
    candidates = list("abcdefghijklmnopqrstuvwxyz") + ["ctrl-" + chr(code) for code in range(ord("a"), ord("z") + 1)]
    candidates += ["alt-" + chr(code) for code in range(ord("a"), ord("z") + 1)] + ["f" + str(number) for number in range(1, 25)]
    named = ["up", "down", "left", "right", "home", "end", "pgup", "pgdn", "backspace", "delete", "tab", "enter", "esc", "space", "btab"]
    candidates += named + [prefix + key for prefix in ("ctrl-", "alt-") for key in named]
    assert all(name in reported for name in candidates if N._valid_key(name))
    assert all(not N._valid_key(name) for name in ("ctrl-h", "ctrl-i", "ctrl-j", "ctrl-m", "alt-enter", "alt-tab", "alt-esc", "ctrl-space", "alt-A", "f25"))


def test_metric_explanation_and_peek_use_published_exact_chart_sample(dashboard):
    app, _, _ = dashboard
    app.analysis_result = {"path": "/declared/metrics.jsonl", "series": {"loss": [{"t": 100, "value": .123456789}, {"t": 200, "value": None}]}}
    app.analysis_result_job, app.analysis_result_generation = "9", None
    app.analysis_state.update(modal="chart", metric="loss", chart_job="9", cursor=0)
    app.analysis_state["metric_display"]["loss"] = {"label": "Validation loss", "unit": "score"}
    app.mode = "analysis"
    app.run_command("explain")
    assert app.mode == "field_explanation"
    text = N.initialize(app)["value"]
    assert "Declared unit: score" in text and "Scope: job 9" in text and "t=100" in text and "unknown: 1" in text
    N.handle_key(app, "esc")
    assert app.mode == "analysis" and app.analysis_state["modal"] == "chart"
    app.run_command("peek metric loss")
    assert app.mode == "value_peek" and N.initialize(app)["raw_value"] == "0.123456789"


def test_stale_header_focus_does_not_override_current_table_default(dashboard):
    app, _, _ = dashboard
    app.table_tools_state.update(header={"table": "history", "column": "ce"}, modal="")
    app.mode = "main"
    app.run_command("explain")
    assert N.initialize(app)["field"] == "Job name"


@pytest.mark.parametrize("value", [{"tab": "jobs", "sort": []}, {"tab": "jobs", "table_context": {"sorts": {"jobs": [["id", "bad"]]}}},
                                   {"tab": "jobs", "table_context": {"widths": {"jobs": {"name": -1}}}}])
def test_malformed_location_preferences_are_rejected(dashboard, value):
    app, _, _ = dashboard
    N.restore(app, {"locations": {"bad": value}})
    assert "bad" not in N.save(app)["locations"]


@pytest.mark.parametrize("mode", ["jump", "settings", "keybindings", "location", "explain eff", "peek name"])
@pytest.mark.parametrize("ascii_", [False, True])
@pytest.mark.parametrize("width,height", [(80, 24), (160, 50), (30, 12)])
def test_navigation_overlays_fit_unicode_ascii_and_small_screens(dashboard, mode, ascii_, width, height):
    app, views, store = dashboard
    views.set_ascii(ascii_)
    app.run_command(mode)
    assert app.command_ok, app.message
    rows = N.overlay(views, store.snapshot(), app, width, height)
    assert rows is not None
    assert all(0 <= y < height and 0 <= x < width and vlen(row_text(row)) <= width - x for y, x, row in rows)
