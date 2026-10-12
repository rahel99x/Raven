"""History export gestures, async lifecycle, exact output and modal failsafes."""
from concurrent.futures import Future
from pathlib import Path
import threading
import time
from types import SimpleNamespace

import pytest

from raven import history_log_export as ui, layout as L, log_bundle
from raven.config import Config
from raven.controller import App
from raven.interaction import Rect
from raven.model import Finished, Store
from raven.remote import LocalFiles
from raven.views import Views


@pytest.fixture
def dashboard(tmp_path):
    projects = tmp_path / "projects"
    projects.mkdir()
    (projects / "project A").mkdir()
    (projects / "project B").mkdir()
    source = tmp_path / "logs"
    source.mkdir()
    store = Store(persist=False)
    store.finished = [Finished("101", "first", state="COMPLETED"), Finished("102", "second", state="FAILED")]
    payloads = {}
    for jid in ("101", "102"):
        stdout, stderr = source / (jid + ".log"), source / (jid + ".err")
        stdout.write_bytes(("AB line for " + jid + "\n" + "long line\n" * 120).encode())
        stderr.write_bytes(("CD error for " + jid + "\n").encode())
        payloads[str(stdout)] = stdout.read_bytes()
        payloads[str(stderr)] = stderr.read_bytes()
        store.details[jid] = {"JobId": jid, "WorkDir": str(source), "StdOut": str(stdout), "StdErr": str(stderr)}
    config = Config({"animations": False, "exports": {"projects_root": str(projects)},
                     "clipboard": {"osc52": False, "tools": False}})
    app = App(store, None, None, config, "tester", interactive=False)
    app.files = LocalFiles()
    app.state_dir = str(tmp_path / "state")
    app.tab, app.width, app.height, app.body_origin = "history", 100, 30, 0
    app.history_all_records = list(store.finished)
    app.selected_id = "101"
    app.history_jobs_rect = Rect(5, 0, 20, 70)
    views = Views(L.Glyphs(False), config)
    app.views_ref = views
    yield SimpleNamespace(app=app, store=store, views=views, root=projects, payloads=payloads)
    ui.cancel(app, close=True)
    if app.research:
        app.research.close()


def paint(dashboard, width=None, height=None):
    app = dashboard.app
    app.width = width if width is not None else app.width
    app.height = height if height is not None else app.height
    return ui.overlay(dashboard.views, {}, app, app.width, app.height)


def finish(app):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if app.research:
            app.research.poll_task()
        ui.tick(app)
        state = ui.initialize(app)
        if state["pending"] is None and state["callback"] is None:
            return
        time.sleep(.001)
    pytest.fail("Background export did not complete")


def click_action(dashboard, action):
    paint(dashboard)
    hit = next(value for value in ui.initialize(dashboard.app)["hits"] if value[3][0] == action)
    assert ui.handle_mouse(dashboard.app, hit[0], hit[1], button="left")


def open_picker(dashboard):
    assert ui.open_menu(dashboard.app)
    click_action(dashboard, "directory")
    finish(dashboard.app)
    assert dashboard.app.mode == "history_log_picker"


def test_right_click_is_restricted_to_history_list_and_freezes_jobs(dashboard, monkeypatch):
    app = dashboard.app
    app.marks = {"101", "102"}
    monkeypatch.setattr(app.store, "snapshot", lambda: pytest.fail("Pointer input must not snapshot the Store"))
    assert not ui.handle_mouse(app, 3, 3, button="right")
    assert not ui.handle_mouse(app, 7, 85, button="right")
    assert ui.handle_mouse(app, 7, 4, button="right")
    assert app.mode == "history_log_menu"
    assert ui.initialize(app)["jobs"] == ("101", "102")
    app.marks, app.selected_id = {"102"}, "102"
    assert ui.initialize(app)["jobs"] == ("101", "102")
    assert ui.handle_mouse(app, 3, 90, button="right")
    assert app.mode == "history_log_menu"


def test_no_selection_opens_no_menu_or_job_action(dashboard):
    app = dashboard.app
    app.selected_id = None
    assert ui.handle_mouse(app, 7, 4, button="right")
    assert app.mode == "main"
    assert "Select" in app.message
    assert not app.confirm


@pytest.mark.parametrize("tab,mode", [("jobs", "main"), ("history", "confirm"), ("log", "main")])
def test_only_main_history_opens_export(dashboard, tab, mode):
    dashboard.app.tab, dashboard.app.mode = tab, mode
    assert not ui.open_menu(dashboard.app)


def test_explicit_bad_or_oversized_selection_is_rejected_whole(dashboard):
    app = dashboard.app
    assert not ui.open_menu(app, ["not-here"])
    assert not ui.open_menu(app, ["101", "102\n"])
    assert not ui.open_menu(app, [str(i) for i in range(1025)])
    assert app.mode == "main"


def test_unrelated_marks_do_not_fall_back_to_other_selected_job(dashboard):
    dashboard.app.marks = {"999"}
    assert ui.selected_jobs(dashboard.app) == ()
    assert not ui.open_menu(dashboard.app)


def test_menu_keyboard_and_mouse_targets_do_not_click_through(dashboard):
    app = dashboard.app
    ui.open_menu(app)
    paint(dashboard)
    assert ui.controls(app)
    assert ui.handle_key(app, "end")
    assert ui.initialize(app)["cursor"] == 2
    assert ui.handle_key(app, "tab")
    assert ui.initialize(app)["cursor"] == 0
    assert ui.handle_key(app, "btab")
    assert ui.initialize(app)["cursor"] == 2
    assert ui.handle_mouse(app, 0, 0, button="left")
    assert app.mode == "history_log_menu"
    ui.handle_key(app, "enter")
    assert app.mode == "main" and not app.confirm


def test_pointer_hover_and_release_on_other_button_never_activate(dashboard):
    app = dashboard.app
    ui.open_menu(app)
    paint(dashboard)
    first, second = ui.initialize(app)["hits"][:2]
    ui.handle_mouse(app, first[0], first[1], button="motion")
    assert ui.initialize(app)["hover"] == first[3]
    ui.handle_mouse(app, first[0], first[1], button="press")
    ui.handle_mouse(app, second[0], second[1], button="release")
    assert app.mode == "history_log_menu"
    assert app.research is None


def test_resize_invalidates_old_mouse_and_graph_controls(dashboard):
    app = dashboard.app
    ui.open_menu(app)
    paint(dashboard)
    hit = ui.initialize(app)["hits"][0]
    app.width -= 1
    assert not ui.controls(app)
    ui.handle_mouse(app, hit[0], hit[1], button="left")
    assert app.research is None


@pytest.mark.parametrize("width,height", [(1, 1), (2, 2), (5, 5), (20, 8), (40, 12), (100, 30)])
@pytest.mark.parametrize("ascii_", [True, False])
def test_every_modal_fits_tiny_and_standard_terminals(dashboard, width, height, ascii_):
    app = dashboard.app
    dashboard.views.g = L.Glyphs(ascii_)
    ui.open_menu(app)
    for stage in ("menu", "picker", "mkdir", "busy", "missing", "receipt", "error"):
        ui._set_stage(app, stage)
        rows = paint(dashboard, width, height)
        assert rows
        assert all(0 <= y < height and 0 <= x < width and x + L.vlen(L.row_text(row)) <= width for y, x, row in rows)
        assert all(0 <= y < height and 0 <= left < right <= width for y, left, right, _ in ui.initialize(app)["hits"])


def test_source_discovery_and_clipboard_run_only_on_background_worker(dashboard, monkeypatch):
    app = dashboard.app
    app.marks = {"101", "102"}
    original_discover = log_bundle.discover_logs
    threads = []
    def discover(*args, **kwargs):
        threads.append(threading.current_thread().name)
        return original_discover(*args, **kwargs)
    monkeypatch.setattr(log_bundle, "discover_logs", discover)
    assert ui.run_command(app, ["historylogs", "clipboard"])
    finish(app)
    state = ui.initialize(app)
    assert app.mode == "history_log_receipt"
    assert threads and all(name.startswith("raven-research") for name in threads)
    result = state["result"]
    assert len(result["files"]) == 4
    assert result["bytes"] == sum(len(value) for value in dashboard.payloads.values())
    combined = Path(result["clipboard_path"]).read_bytes()
    for value in dashboard.payloads.values():
        assert value in combined
    assert b"AB line" in combined and b"CD error" in combined
    assert state["jobs"] == ("101", "102")


def test_directory_picker_mkdir_and_save_preserve_every_original_byte(dashboard):
    app = dashboard.app
    app.marks = {"101", "102"}
    open_picker(dashboard)
    assert ui.initialize(app)["cwd"] == str(dashboard.root)
    click_action(dashboard, "mkdir")
    assert ui.paste(app, "new research results")
    ui.handle_key(app, "enter")
    finish(app)
    assert app.mode == "history_log_picker"
    state = ui.initialize(app)
    created = str(dashboard.root / "new research results")
    assert any(entry["path"] == created for entry in state["entries"])
    ui._activate(app, ("directory-path", created))
    finish(app)
    assert state["cwd"] == created and state["parent"] == str(dashboard.root)
    click_action(dashboard, "save")
    finish(app)
    assert app.mode == "history_log_receipt"
    result = state["result"]
    assert Path(result["export_path"]).parent == Path(created)
    for item in result["files"]:
        assert (Path(result["export_path"]) / item["relative_path"]).read_bytes() == dashboard.payloads[item["source_path"]]
    assert (Path(result["export_path"]) / "manifest.json").is_file()


def test_name_editing_is_literal_printable_utf8_bounded(dashboard):
    app = dashboard.app
    ui.open_menu(app)
    ui._set_stage(app, "mkdir")
    ui.paste(app, "ab")
    ui.handle_key(app, "left")
    ui.handle_key(app, "q")
    assert ui.initialize(app)["name"] == "aqb" and app.mode == "history_log_mkdir"
    ui.handle_key(app, "backspace")
    assert ui.initialize(app)["name"] == "ab"
    ui.handle_key(app, "delete")
    assert ui.initialize(app)["name"] == "a"
    ui.handle_key(app, "space")
    assert ui.initialize(app)["name"] == "a "
    before = ui.initialize(app)["name"]
    for text in ("../escape", "a\\b", "a\nb", "\x1b[2J"):
        ui.paste(app, text)
        assert ui.initialize(app)["name"] == before
    ui.paste(app, "界" * 200)
    assert len(ui.initialize(app)["name"].encode("utf-8")) <= 255
    assert "255 UTF-8 bytes" in ui.initialize(app)["error"]


def test_directory_parent_never_leaves_projects_root(dashboard):
    open_picker(dashboard)
    app = dashboard.app
    state = ui.initialize(app)
    assert state["parent"] is None
    ui.handle_key(app, "left")
    assert app.mode == "history_log_picker" and state["cwd"] == str(dashboard.root)
    ui._activate(app, ("directory-path", str(dashboard.root.parent)))
    finish(app)
    assert app.mode == "history_log_error"
    assert "inside" in state["error"]


def test_named_folder_can_open_unlisted_child_without_creating_directories(dashboard, monkeypatch):
    existing = dashboard.root / "hidden by listing limit"
    existing.mkdir()
    original = log_bundle.list_directories
    def limited(root, current=None, **kwargs):
        result = original(root, current, **kwargs)
        if result["current"] == str(dashboard.root):
            result.update(entries=[], limited=True)
        return result
    monkeypatch.setattr(log_bundle, "list_directories", limited)
    monkeypatch.setattr(log_bundle, "mkdir_directory", lambda *args, **kwargs: pytest.fail("Opening an existing folder must not create it"))
    open_picker(dashboard)
    click_action(dashboard, "open-name")
    assert ui.initialize(dashboard.app)["name_purpose"] == "open"
    ui.paste(dashboard.app, "hidden by listing limit")
    ui.handle_key(dashboard.app, "enter")
    finish(dashboard.app)
    assert dashboard.app.mode == "history_log_picker"
    assert ui.initialize(dashboard.app)["cwd"] == str(existing)


@pytest.mark.parametrize("name", ["does not exist", "../outside", "linked child"])
def test_named_folder_never_creates_missing_or_follows_escape(dashboard, name):
    outside = dashboard.root.parent / "outside"
    outside.mkdir()
    (dashboard.root / "linked child").symlink_to(outside, target_is_directory=True)
    open_picker(dashboard)
    click_action(dashboard, "open-name")
    ui.paste(dashboard.app, name)
    ui.handle_key(dashboard.app, "enter")
    finish(dashboard.app)
    assert dashboard.app.mode in ("history_log_error", "history_log_mkdir")
    assert not (dashboard.root / "does not exist").exists()


def test_missing_preflight_lists_exact_jobs_and_never_silently_exports(dashboard):
    app = dashboard.app
    app.marks = {"101", "102"}
    missing = dashboard.store.details["102"]["StdErr"]
    Path(missing).unlink()
    ui.run_command(app, ["historylogs", "clipboard"])
    finish(app)
    state = ui.initialize(app)
    assert app.mode == "history_log_missing"
    assert not state["result"].get("export_path")
    text = "\n".join(ui._details(app))
    assert "Job 102" in text and missing in text
    assert "No bundle was copied" in text
    assert any(action == ("available",) for _, action in ui._items(app))
    click_action(dashboard, "available")
    finish(app)
    assert app.mode == "history_log_missing"
    assert len(state["result"]["files"]) == 3
    assert "Available logs were saved" in "\n".join(ui._details(app))
    assert state["result"]["export_path"] in "\n".join(ui._details(app))
    assert all(action != ("available",) for _, action in ui._items(app))
    click_action(dashboard, "receipt")
    assert app.mode == "history_log_receipt"


def test_warning_only_discovery_still_exports_complete_logs(dashboard, monkeypatch):
    original = log_bundle.discover_logs
    def warned(*args, **kwargs):
        report = original(*args, **kwargs)
        report.update(status="partial", warnings=["Metadata fallback used cached paths"])
        return report
    monkeypatch.setattr(log_bundle, "discover_logs", warned)
    ui.run_command(dashboard.app, ["historylogs", "clipboard"])
    finish(dashboard.app)
    assert dashboard.app.mode == "history_log_receipt"
    assert "Metadata fallback" in "\n".join(ui._details(dashboard.app))


def test_source_race_partial_receipt_does_not_claim_nothing_copied(dashboard, monkeypatch):
    original = log_bundle.export_logs
    def race(report, *args, **kwargs):
        Path(report["entries"][0]["path"]).unlink()
        return original(report, *args, **kwargs)
    monkeypatch.setattr(log_bundle, "export_logs", race)
    ui.run_command(dashboard.app, ["historylogs", "clipboard"])
    finish(dashboard.app)
    state = ui.initialize(dashboard.app)
    assert dashboard.app.mode == "history_log_missing"
    assert state["result"]["export_path"]
    text = "\n".join(ui._details(dashboard.app))
    assert "No bundle was copied" not in text
    assert state["result"]["export_path"] in text


def test_long_missing_report_formats_visible_rows_only(dashboard, monkeypatch):
    app = dashboard.app
    ui.open_menu(app)
    state = ui.initialize(app)
    state["report"] = {"missing": [{"job_id": "101", "path": str(i), "message": "unavailable"} for i in range(100000)]}
    ui._set_stage(app, "missing")
    state["scroll"] = 99980
    count = []
    original = ui.clean
    def counted(*args, **kwargs):
        count.append(1)
        return original(*args, **kwargs)
    monkeypatch.setattr(ui, "clean", counted)
    paint(dashboard)
    assert len(count) < 60
    assert ui._detail_count(app) == 100002


def test_missing_labels_and_basenames_precede_long_parent_paths(dashboard):
    app = dashboard.app
    ui.open_menu(app)
    state = ui.initialize(app)
    state["report"] = {"missing": [{"job_id": "101", "path": "/" + "long-shared-parent/" * 50 + "worker.err",
                                     "label": "stderr", "message": "missing"}]}
    ui._set_stage(app, "missing")
    line = ui._details(app)[2]
    assert line.startswith("Job 101: stderr | worker.err | missing | Path:")
    assert "worker.err" in L.cut(line, 60)


def test_modal_controls_keep_semantic_identity_when_list_viewport_changes(dashboard):
    app = dashboard.app
    open_picker(dashboard)
    for number in range(70):
        ui.initialize(app)["entries"].append({"name": str(number), "path": str(dashboard.root / str(number))})
    paint(dashboard, 80, 12)
    first = {control.label: control.id for control in ui.controls(app)}
    ui.handle_key(app, "end")
    paint(dashboard)
    last = {control.label: control.id for control in ui.controls(app)}
    for label in first.keys() & last.keys():
        assert first[label] == last[label]
    assert not (set(first.values()) & set(last.values())) - {first[label] for label in first.keys() & last.keys()}


def test_project_root_selection_is_local_and_respects_explicit_config(dashboard, monkeypatch):
    app = dashboard.app
    monkeypatch.setenv("HOME", "/tmp/export-home")
    app.project_state.update(registered_root="/registered/project")
    assert ui._projects_root(app) == str(dashboard.root)
    app.cfg["exports"]["projects_root"] = ""
    assert ui._projects_root(app) == "/registered/project"
    app.files = SimpleNamespace(remote=True)
    assert ui._projects_root(app) == "/tmp/export-home/projects"
    app.cfg["exports"]["projects_root"] = "~/custom projects"
    assert ui._projects_root(app) == "/tmp/export-home/custom projects"


class BusyHub:
    def __init__(self):
        self.lock = threading.RLock()
        self.closed = False
        self.pending = (Future(), object())
        self.future = self.pending[0]
        self.started = []
    def start_task(self, work, complete):
        self.started.append((work, complete))
        return True
    def close(self):
        pass


def test_busy_worker_keeps_one_queued_request_and_can_cancel_without_io(dashboard):
    app = dashboard.app
    hub = app.research = BusyHub()
    ui.run_command(app, ["historylogs", "clipboard"])
    state = ui.initialize(app)
    assert state["pending"] is not None and state["status"] == "queued"
    for _ in range(30):
        ui.tick(app)
        ui.handle_key(app, "enter") if state["status"] == "cancelling" else None
    assert not hub.started
    ui.handle_key(app, "esc")
    assert state["pending"] is None and app.mode == "history_log_menu"
    assert not hub.started


def test_early_cancel_waits_for_cleanup_and_reports_no_saved_path(dashboard, monkeypatch):
    app = dashboard.app
    entered, release = threading.Event(), threading.Event()
    def slow(*args, cancel=None, **kwargs):
        entered.set()
        assert release.wait(3)
        assert cancel.is_set()
        return {"status": "cancelled", "missing": []}
    monkeypatch.setattr(log_bundle, "discover_logs", slow)
    ui.run_command(app, ["historylogs", "clipboard"])
    assert entered.wait(3)
    ui.handle_key(app, "esc")
    assert app.mode == "history_log_busy" and ui.initialize(app)["status"] == "cancelling"
    release.set()
    finish(app)
    assert app.mode == "history_log_menu"
    assert app.message == "Log operation cancelled."


def test_late_cancel_preserves_publication_and_clipboard_receipt(dashboard, monkeypatch):
    app = dashboard.app
    def published(report, *args, cancel=None, **kwargs):
        cancel.set()
        return {"status": "cancelled", "export_path": "/saved/bundle", "clipboard_path": "/saved/bundle/all-logs.txt",
                "clipboard": {"methods": ["OSC 52 request sent"]}, "missing": [], "warnings": [], "message": "Saved logs"}
    monkeypatch.setattr(log_bundle, "export_logs", published)
    ui.run_command(app, ["historylogs", "clipboard"])
    finish(app)
    assert app.mode == "history_log_receipt"
    text = "\n".join(ui._details(app))
    assert "/saved/bundle" in text and "OSC 52 request sent" in text
    assert "Cancellation arrived after" in text


def test_shutdown_generation_discards_stale_completion(dashboard, monkeypatch):
    app = dashboard.app
    entered, release = threading.Event(), threading.Event()
    def slow(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return {"status": "ready", "entries": [], "missing": []}
    monkeypatch.setattr(log_bundle, "discover_logs", slow)
    ui.run_command(app, ["historylogs", "clipboard"])
    assert entered.wait(3)
    ui.cancel(app, close=True)
    app.tab = "jobs"
    release.set()
    future = app.research.future
    future.result(timeout=3)
    app.research.poll_task()
    assert app.mode == "main" and ui.initialize(app)["stage"] == ""


def test_reader_shutdown_is_itself_a_worker_cancellation(dashboard, monkeypatch):
    app = dashboard.app
    entered, release = threading.Event(), threading.Event()
    cancellations = []
    def slow(*args, cancel=None, **kwargs):
        entered.set()
        assert release.wait(3)
        cancellations.append(cancel.is_set())
        return {"status": "cancelled", "missing": []}
    monkeypatch.setattr(log_bundle, "discover_logs", slow)
    ui.run_command(app, ["historylogs", "clipboard"])
    assert entered.wait(3)
    app.research.close()
    release.set()
    app.research.future.result(timeout=3)
    assert cancellations == [True]


def test_visible_modal_navigation_and_paint_never_inspect_files(dashboard, monkeypatch):
    app = dashboard.app
    ui.open_menu(app)
    def forbidden(*args, **kwargs):
        pytest.fail("Modal navigation attempted filesystem or Store I/O")
    monkeypatch.setattr(app.store, "snapshot", forbidden)
    monkeypatch.setattr(log_bundle, "discover_logs", forbidden)
    monkeypatch.setattr(log_bundle, "list_directories", forbidden)
    for _ in range(20):
        rows = paint(dashboard)
        hit = ui.initialize(app)["hits"][0]
        ui.handle_mouse(app, hit[0], hit[1], button="motion")
        ui.handle_key(app, "down")
        ui.handle_key(app, "up")
        assert rows


def test_command_usage_cancel_and_unknown_dispatch(dashboard):
    assert not ui.run_command(dashboard.app, ["other"])
    assert ui.run_command(dashboard.app, ["historylogs", "unexpected"])
    assert "Use historylogs" in dashboard.app.message
    ui.open_menu(dashboard.app)
    assert ui.run_command(dashboard.app, ["historylogs", "cancel"])
    assert dashboard.app.mode == "main"
