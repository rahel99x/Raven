"""Array mapping controls share pinned identity without stealing job navigation."""
import json
from types import SimpleNamespace

import pytest

from raven import array_manifest as M, array_manifest_ui as UI, arrays, job_panels, layout as L
from raven.config import Config
from raven.controller import App
from raven.model import Job, Finished, Store
from raven.remote import LocalFiles
from raven.research import ResearchHub
from raven.views import Views


def document(label="Sample A"):
    return {"schema": M.SCHEMA, "cluster": "", "array_id": "52", "entries": [
        {"index": 2, "id": "sample-A", "label": label, "parameters": {"seed": 7}},
        {"index": 19, "id": "sample-B", "inputs": ["not-opened.csv"]}]}


@pytest.fixture
def dashboard(tmp_path):
    cfg = Config({"log_lines": 0, "animations": False, "startup_animation": False,
                  "smooth_scrolling": False, "workspace": {"density": "compact", "split": 40}})
    store = Store(persist=False)
    store.jobs = [Job("52_2", "sampleA", "cpu", "RUNNING"), Job("52_19", "sampleB", "cpu", "PENDING"),
                  Job("52_7", "unmapped", "cpu", "RUNNING")]
    store.finished = [Finished("52_1", "old", "FAILED")]
    app = App(store, None, None, cfg, "test", interactive=False)
    views = Views(L.Glyphs(False), cfg)
    app.views_ref = views
    hub = ResearchHub(cfg, LocalFiles())
    app.research, app.files = hub, hub.files
    app.tab, app.research_view = "research", "arrays"
    source = tmp_path / "input map.json"
    source.write_text(json.dumps(document()))
    state = SimpleNamespace(app=app, store=store, views=views, hub=hub, source=source)
    state.render = lambda: views.compose(store.snapshot(), app, 240, 64)
    state.attach = lambda: app.run_command("arraymap load " + json.dumps(str(source)))
    hub.request(hub.context(store.snapshot(), app), wait=True)
    yield state
    hub.close()


def test_commands_registered_and_attach_detach_without_navigating(dashboard):
    d = dashboard
    before = d.app.tab, d.app.selected_id
    d.attach()
    state = UI.initialize(d.app)
    assert state["manifest"].array_id == "52"
    assert (d.app.tab, d.app.selected_id) == before
    assert d.app.command_ok
    d.app.run_command("arraymap clear")
    assert state["manifest"] is None and not state["path"]


def test_arrays_task_rows_show_mapping_only_for_exact_index_and_scope(dashboard):
    d = dashboard
    d.attach()
    d.render()
    d.app.run_command("array open 52")
    rows, hits = d.render()
    text = L.to_text(rows, 240)
    assert "sample-A / Sample A" in text and "unmapped index" in text
    mapped = [value for _, kind, value in hits if kind == "control" and value["id"].startswith("arraymap-task:")]
    assert len(mapped) == 2
    manifest = UI.initialize(d.app)["manifest"]
    assert UI.label(d.app, {"id": "52", "cluster": "other"}, 2) is None
    assert UI.label(d.app, {"id": "53", "cluster": ""}, 2) is None
    assert UI.label(d.app, {"id": "52", "cluster": ""}, 7) is None
    d.app.run_command(mapped[0]["action"][1])
    assert d.app.mode == "arraymap"
    assert UI.initialize(d.app)["view"] == "detail"
    assert UI.initialize(d.app)["manifest"] is manifest


@pytest.mark.parametrize("event", ["motion", "drag", "release", "right"])
def test_hover_motion_and_release_never_open_mapping_control(dashboard, event):
    d = dashboard
    d.attach()
    rows, hits = d.render()
    y, _, value = next(hit for hit in hits if hit[1] == "control" and hit[2]["id"] == "arraymap:inspect")
    before = d.app.tab, d.app.mode, d.app.selected_id
    d.app.click(y, value["left"], hits, button=event)
    assert (d.app.tab, d.app.mode, d.app.selected_id) == before


@pytest.mark.parametrize("tab", ["jobs", "history"])
def test_inline_buttons_open_owner_modal_without_changing_selected_job(dashboard, tab):
    d = dashboard
    d.attach()
    d.app.tab = tab
    if tab == "history":
        d.app.selected_id = "52_1"
    job_panels.initialize(d.app).update(mode="research", research_view="arrays")
    rows, hits = d.render()
    found = None
    for y, kind, value in hits:
        if kind == "job_panel_action":
            (inner_kind, payload), left, right = value
            if inner_kind == "control" and payload["id"] == "arraymap:inspect":
                found = y, left
                break
    assert found is not None
    selected = d.app.selected_id
    d.app.click(*found, hits)
    assert d.app.mode == "arraymap" and d.app.tab == tab and d.app.selected_id == selected
    UI.handle_key(d.app, "esc")
    assert d.app.mode == "main" and d.app.tab == tab


def test_browser_search_keys_and_mouse_have_same_exact_entry(dashboard):
    d = dashboard
    d.attach()
    d.app.run_command("arraymap search sample-b")
    state = UI.initialize(d.app)
    assert len(state["matches"]) == 1 and state["matches"][0].index == 19
    UI.handle_key(d.app, "enter")
    rows = UI.overlay(d.views, d.store.snapshot(), d.app, 80, 30)
    assert "52_19" in " ".join(L.row_text(row) for _, _, row in rows)
    UI.handle_key(d.app, "esc")
    d.app.run_command("arraymap search")
    UI.overlay(d.views, d.store.snapshot(), d.app, 80, 30)
    y, _, control = next(hit for hit in state["control_hits"] if hit[2].get("index") == 1)
    UI.handle_mouse(d.app, y, control["left"])
    assert state["view"] == "detail" and state["matches"][state["cursor"]].index == 19


@pytest.mark.parametrize("width,height,ascii_", [(160, 40, False), (60, 14, True), (12, 6, False), (1, 1, True)])
def test_browser_narrow_unicode_ascii_controls_stay_in_box(dashboard, width, height, ascii_):
    d = dashboard
    d.attach()
    d.views.set_ascii(ascii_)
    for command in ("arraymap inspect", "arraymap", "arraymap search absent"):
        d.app.run_command(command)
        rows = UI.overlay(d.views, d.store.snapshot(), d.app, width, height)
        assert rows is not None
        for y, _, control in UI.initialize(d.app)["control_hits"]:
            assert 0 <= y < height and 0 <= control["left"] < control["right"] <= width


def test_stale_task_control_rejected_after_explicit_revision_change(dashboard):
    d = dashboard
    d.attach()
    original = UI.initialize(d.app)["manifest"]
    d.source.write_text(json.dumps(document(label="new revision")))
    d.app.run_command("arraymap reload")
    d.app.run_command(f"arraymap select {original.revision} 2")
    assert not d.app.command_ok and "changed" in d.app.message
    assert d.app.mode == "main"


def test_background_source_change_retains_pinned_entries_until_reload(dashboard, monkeypatch):
    d = dashboard
    d.attach()
    state = UI.initialize(d.app)
    original = state["manifest"]
    d.source.write_text(json.dumps(document(label="new revision")))
    state["last"] = 0
    UI.tick(d.app)
    d.hub.future.result(timeout=3)
    d.hub.poll_task()
    assert state["manifest"] is original and "Source changed" in state["notice"]
    assert state["manifest"].by_index[2].label == "Sample A"
    d.app.run_command("arraymap reload")
    assert state["manifest"].by_index[2].label == "new revision" and not state["notice"]


def test_many_frames_never_read_or_parse_source(dashboard, monkeypatch):
    d = dashboard
    d.attach()
    state = UI.initialize(d.app)
    d.hub.request = lambda _: {"status": "ok", "groups": arrays.summarize(d.store.jobs, d.store.finished)}
    monkeypatch.setattr(M, "read", lambda *args: pytest.fail("file read in render"))
    monkeypatch.setattr(M, "parse", lambda *args: pytest.fail("parse in render"))
    for _ in range(20):
        d.render()
        UI.tick(d.app)
    assert state["manifest"] is not None


@pytest.mark.parametrize("change", ["job", "project", "detach"])
def test_background_attachment_cannot_publish_into_new_context(dashboard, monkeypatch, change):
    d = dashboard
    d.app.interactive = True
    pending = []
    monkeypatch.setattr(d.hub, "start_task", lambda worker, complete: pending.append((worker, complete)) or True)
    d.attach()
    worker, complete = pending[0]
    if change == "job":
        d.app.selected_id = "other"
    elif change == "project":
        d.app.project_state["generation"] += 1
    else:
        d.app.run_command("arraymap clear")
    complete(worker())
    assert UI.initialize(d.app)["manifest"] is None


def test_missing_source_notice_retains_original_mapping_and_recovers(dashboard):
    d = dashboard
    d.attach()
    state = UI.initialize(d.app)
    original = state["manifest"]
    d.source.unlink()
    state["last"] = 0
    UI.tick(d.app)
    with pytest.raises(OSError):
        d.hub.future.result(timeout=3)
    d.hub.poll_task()
    assert state["manifest"] is original and "unavailable" in state["notice"]
    d.source.write_text(json.dumps(document()))
    state["last"] = 0
    UI.tick(d.app)
    d.hub.future.result(timeout=3)
    d.hub.poll_task()
    assert state["manifest"] is original and state["notice"] == ""


def test_raw_press_opens_once_release_does_not_click_hidden_content(dashboard):
    d = dashboard
    d.attach()
    d.app.run_command("arraymap inspect")
    UI.overlay(d.views, d.store.snapshot(), d.app, 100, 30)
    state = UI.initialize(d.app)
    y, _, control = next(hit for hit in state["control_hits"] if hit[2].get("index") == 1)
    d.app.click(y, control["left"], [], button="press")
    assert state["view"] == "detail" and state["cursor"] == 1
    d.app.click(y, control["left"], [], button="release")
    assert state["view"] == "detail" and state["cursor"] == 1
    assert d.app.mode == "arraymap"


def test_wheel_in_modal_keeps_underlying_job_selection(dashboard):
    d = dashboard
    d.attach()
    d.app.run_command("arraymap inspect")
    selected = d.app.selected_id
    UI.overlay(d.views, d.store.snapshot(), d.app, 100, 30)
    d.app.click(0, 0, [], button="wheel-down")
    assert UI.initialize(d.app)["cursor"] == 1
    assert d.app.selected_id == selected and d.app.mode == "arraymap"


def test_connection_swap_disables_scientific_labels_and_retry_capture(dashboard):
    d = dashboard
    d.attach()
    d.app.research = SimpleNamespace(files=LocalFiles(), pending=None)
    try:
        assert UI.label(d.app, {"id": "52", "cluster": ""}, 2) is None
        with pytest.raises(ValueError, match="connection"):
            UI.capture(d.app)
        UI.tick(d.app)
        assert "Connection changed" in UI.initialize(d.app)["notice"]
    finally:
        d.app.research = d.hub


def test_reused_array_parent_or_mixed_attempts_does_not_relabel(dashboard):
    d = dashboard
    for job in d.store.jobs + d.store.finished:
        job.submit = "2026-10-01T12:00:00"
    d.attach()
    group = {"id": "52", "cluster": "", "submit_times": ["2026-10-01T12:00:00"]}
    assert UI.label(d.app, group, 2).id == "sample-A"
    group["submit_times"] = ["2026-10-05T12:00:00"]
    assert UI.label(d.app, group, 2) is None
    group["submit_times"].append("2026-10-01T12:00:00")
    assert UI.label(d.app, group, 2) is None


def test_ambiguous_attempt_attachment_fails_instead_of_freezing_wrong_labels(dashboard):
    d = dashboard
    d.store.jobs[0].submit = "2026-10-01T12:00:00"
    d.store.jobs[1].submit = "2026-10-05T12:00:00"
    d.attach()
    assert UI.initialize(d.app)["manifest"] is None
    assert not d.app.command_ok and "multiple submission attempts" in d.app.message


def test_repeated_detail_frames_reuse_wrapped_rows(dashboard, monkeypatch):
    from raven import execution_ui
    d = dashboard
    d.attach()
    d.app.run_command("arraymap inspect")
    UI.handle_key(d.app, "enter")
    UI.overlay(d.views, d.store.snapshot(), d.app, 90, 30)
    monkeypatch.setattr(execution_ui, "_wrap", lambda *args, **kwargs: pytest.fail("detail rewrapped on idle frame"))
    for _ in range(20):
        UI.overlay(d.views, d.store.snapshot(), d.app, 90, 30)


