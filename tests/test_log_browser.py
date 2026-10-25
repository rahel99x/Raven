"""Keyboard and mouse journeys through one job's grouped log locations."""
from __future__ import annotations

import json
from pathlib import Path
import time

import pytest

from raven.config import Config
from raven.controller import App
from raven.layout import Glyphs, row_text
from raven.model import Finished, Job, Store
from raven.remote import LocalFiles
from raven.views import Views


class BrowserSampler:
    def __init__(self):
        self.selected = []
        self.refreshes = 0

    def select(self, job_id):
        self.selected.append(job_id)

    def select_fin(self, job_id):
        pass

    def select_trace(self, job_id):
        pass

    def refresh_all(self):
        self.refreshes += 1


class BrowserDashboard:
    def __init__(self, root, *, ascii_=True, extra_entries=0, recorded_paths=True, manifest_job="77"):
        self.root = root
        self.run_root = root / "project with spaces" / "runs" / "failed 77"
        self.run_root.mkdir(parents=True)
        self.manifest = self.run_root / "metadata" / "logs.json"
        self.manifest.parent.mkdir()
        self.paths = {
            "stdout": self.run_root / "scheduler" / "stdout-77.log",
            "stderr": self.run_root / "scheduler errors" / "stderr-77.log",
            "worker": self.run_root / "scheduler" / "worker-77.log",
            "rank": self.run_root / "scheduler errors" / "rank-77.log",
            "component": self.run_root / "components" / "parser.log",
            "external": root / "separate storage" / "diagnostics.log",
            "notes": self.run_root / "metadata" / "notes.log",
        }
        self.markers = {}
        for key, path in self.paths.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            self.markers[str(path)] = "ONLY_THIS_FILE_" + key.upper()
            path.write_text(self.markers[str(path)] + "\n", encoding="utf-8")
        entries = [
            {"id": "component", "path": "../components/parser.log", "label": "Parser diagnostics", "group": "Application", "description": "Captured parse failures"},
            {"id": "external", "path": str(self.paths["external"]), "label": "External diagnostics", "group": "Infrastructure"},
            {"id": "notes", "path": "notes.log", "label": "Run notes", "group": "Application"},
        ]
        for i in range(extra_entries):
            path = self.run_root / "components" / f"component-{i:02d}.log"
            marker = f"ONLY_COMPONENT_{i:02d}"
            path.write_text(marker + "\n", encoding="utf-8")
            self.markers[str(path)] = marker
            entries.append({"id": f"extra-{i}", "path": str(path), "label": f"Component {i:02d}", "group": f"Group {i % 3}"})
        report = {"schema": "raven.logs/v1", "logs": entries}
        if manifest_job is not None:
            report["job_id"] = manifest_job
        self.manifest.write_text(json.dumps(report), encoding="utf-8")
        self.cfg = Config({"log_lines": 0,
                           "logs": {"manifest_file": "metadata/logs.json"},
                           "research": {"workdir": str(self.run_root)}})
        self.store = Store(persist=False)
        self.store.jobs = [Job("900", "currently running", "main", "RUNNING")]
        self.store.finished = [Finished("77", "failed experiment", "FAILED", workdir=str(self.run_root))]
        if recorded_paths:
            self.store.details["77"] = {"StdOut": str(self.paths["stdout"]), "StdErr": str(self.paths["stderr"]), "WorkDir": str(self.run_root)}
        live_path = root / "live-900.log"
        live_path.write_text("ACTIVE_JOB_900_SHOULD_NOT_APPEAR\n", encoding="utf-8")
        self.store.details["900"] = {"StdOut": str(live_path), "StdErr": str(live_path)}
        self.sampler = BrowserSampler()
        self.app = App(self.store, self.sampler, None, self.cfg, "reader", ascii_=ascii_)
        self.views = Views(Glyphs(ascii_), self.cfg, files=LocalFiles())
        self.app.logs.files = self.views.files
        self.app.views_ref = self.views
        self.render()

    def render(self, *, width=140, height=45):
        rows, hits = self.views.compose(self.store.snapshot(), self.app, width, height)
        return "\n".join(row_text(row) for row in rows), rows, hits

    def open_history(self):
        self.app.handle("3")
        self.render()
        self.app.handle("l")
        self.render()
        assert self.app.log_job == "77"
        return self

    def browse(self):
        self.open_history()
        self.app.handle("O")
        self.render()
        assert self.app.logs.browser
        return self

    def select_path(self, path):
        target = str(path)
        index = next(i for i, entry in enumerate(self.app.logs.entries) if entry["path"] == target)
        self.app.handle("home")
        for _ in range(index):
            self.app.handle("down")
        assert self.app.logs.browser_cursor == index
        return self.app.logs.entries[index]

    def assert_file(self, path):
        text, _, _ = self.render()
        target = str(path)
        assert not self.app.logs.browser
        assert self.app.log_job == "77"
        assert self.app.logs.path == target
        assert self.markers[target] in text
        assert "ACTIVE_JOB_900_SHOULD_NOT_APPEAR" not in text


@pytest.fixture
def browser(tmp_path):
    dashboard = BrowserDashboard(tmp_path)
    try:
        yield dashboard
    finally:
        if dashboard.app.research:
            dashboard.app.research.close()


@pytest.mark.parametrize("ascii_", [True, False])
def test_grouped_browser_includes_both_scheduler_directories_and_declared_locations(tmp_path, ascii_):
    dashboard = BrowserDashboard(tmp_path, ascii_=ascii_).browse()
    text, _, hits = dashboard.render()
    entries = dashboard.app.logs.entries
    assert set(map(str, dashboard.paths.values())) <= {entry["path"] for entry in entries}
    assert {"Application", "Infrastructure"} <= {entry["group"] for entry in entries}
    assert "Application" in text and "Infrastructure" in text
    assert "Parser diagnostics" in text and "External diagnostics" in text
    assert any(kind == "log_file" for _, kind, _ in hits)
    assert "ACTIVE_JOB_900_SHOULD_NOT_APPEAR" not in text


def test_browser_resolves_manifest_from_run_root_and_entries_from_manifest_directory(browser):
    browser.browse()
    entries = browser.app.logs.entries
    component = next(entry for entry in entries if entry["label"] == "Parser diagnostics")
    notes = next(entry for entry in entries if entry["label"] == "Run notes")
    external = next(entry for entry in entries if entry["label"] == "External diagnostics")
    assert component["path"] == str(browser.paths["component"])
    assert notes["path"] == str(browser.paths["notes"])
    assert external["path"] == str(browser.paths["external"])


def test_arrows_select_files_without_opening_or_changing_the_previous_stream(browser):
    browser.browse()
    previous = browser.app.logs.path
    browser.app.handle("home")
    browser.app.handle("down")
    browser.app.handle("down")
    browser.app.handle("up")
    assert browser.app.logs.browser_cursor == 1
    assert browser.app.logs.browser
    assert browser.app.logs.path == previous


def test_arrows_follow_visible_group_order_when_manifest_groups_are_interleaved(browser):
    browser.browse()
    _, _, hits = browser.render(height=80)
    visible_ids = list(dict.fromkeys(key for _, kind, key in hits if kind == "log_file"))
    browser.app.handle("home")
    for expected_id in visible_ids:
        _, rows, current_hits = browser.render(height=80)
        hit = next(hit for hit in current_hits if hit[1:] == ("log_file", expected_id))
        assert any("rev" in style for _, style in rows[hit[0]])
        browser.app.handle("down")


@pytest.mark.parametrize("key", ["m", "'", "N", "P", "f"])
def test_browser_file_only_shortcuts_cannot_change_previous_content_state(browser, key):
    browser.open_history()
    logs = browser.app.logs
    path = str(browser.paths["stdout"])
    logs.search, logs.match, logs.top = "ONLY_THIS_FILE_STDOUT", None, 0
    logs.bookmarks[path], logs.last_bookmark = [0], None
    browser.app.handle("O")
    browser.render()
    before = (logs.path, logs.top, logs.search, logs.match, logs.last_bookmark, list(logs.bookmarks[path]))
    browser.app.handle(key)
    browser.render()
    assert logs.browser
    assert (logs.path, logs.top, logs.search, logs.match, logs.last_bookmark, list(logs.bookmarks[path])) == before


def test_home_end_and_page_keys_select_and_reveal_files_in_long_catalog(tmp_path):
    dashboard = BrowserDashboard(tmp_path, extra_entries=32).browse()
    app = dashboard.app
    app.handle("home")
    assert app.logs.browser_cursor == 0
    app.handle("pgdn")
    assert 0 < app.logs.browser_cursor < len(app.logs.entries)
    app.handle("pgup")
    assert app.logs.browser_cursor == 0
    app.handle("end")
    assert app.logs.browser_cursor == len(app.logs.entries) - 1
    selected = app.logs.entries[app.logs.browser_cursor]
    _, rows, hits = dashboard.render(width=80, height=18)
    hit = next(hit for hit in hits if hit[1:] == ("log_file", selected["id"]))
    assert selected["label"] in row_text(rows[hit[0]])
    assert any("rev" in style for _, style in rows[hit[0]])
    app.handle("enter")
    dashboard.assert_file(Path(selected["path"]))


@pytest.mark.parametrize("location", ["stdout", "stderr", "worker", "rank", "component", "external", "notes"])
def test_enter_opens_the_selected_file_from_any_registered_location(browser, location):
    browser.browse()
    browser.select_path(browser.paths[location])
    browser.app.handle("enter")
    browser.assert_file(browser.paths[location])


def test_escape_returns_from_file_to_browser_and_then_to_last_open_file(browser):
    browser.browse()
    selected = browser.select_path(browser.paths["external"])
    browser.app.handle("enter")
    browser.assert_file(browser.paths["external"])
    browser.app.handle("esc")
    browser.render()
    assert browser.app.logs.browser
    assert browser.app.logs.entries[browser.app.logs.browser_cursor]["id"] == selected["id"]
    browser.app.handle("esc")
    browser.assert_file(browser.paths["external"])


def test_escape_from_initial_browser_restores_previous_stdout(browser):
    browser.browse()
    browser.select_path(browser.paths["external"])
    browser.app.handle("esc")
    browser.assert_file(browser.paths["stdout"])


def test_mouse_click_selects_exact_catalog_entry_and_enter_opens_it(browser):
    browser.browse()
    entry = next(entry for entry in browser.app.logs.entries if entry["path"] == str(browser.paths["external"]))
    _, rows, hits = browser.render()
    hit = next(hit for hit in hits if hit[1:] == ("log_file", entry["id"]))
    assert entry["label"] in row_text(rows[hit[0]])
    previous = browser.app.logs.path
    from raven.interaction import controls
    target = next(control for control in controls(browser.app)
                  if control.id == "log_file:" + entry["id"])
    browser.app.click(hit[0], target.rect.left, hits)
    assert browser.app.logs.entries[browser.app.logs.browser_cursor]["id"] == entry["id"]
    assert browser.app.logs.browser
    assert browser.app.logs.path == previous
    browser.app.handle("enter")
    browser.assert_file(browser.paths["external"])


def test_catalog_filter_then_enter_before_a_new_frame_opens_the_matching_file(browser):
    browser.browse()
    browser.app.logs.search = "keep the content search"
    browser.app.handle("/")
    for key in "External diagnostics":
        browser.app.handle("space" if key == " " else key)
    browser.app.handle("enter")
    browser.app.handle("enter")
    browser.assert_file(browser.paths["external"])
    assert browser.app.logs.search == "keep the content search"


def test_empty_catalog_filter_cannot_open_an_unmatched_file(browser):
    browser.browse()
    prior = browser.app.logs.path
    browser.app.handle("/")
    for key in "no matching file":
        browser.app.handle("space" if key == " " else key)
    browser.app.handle("enter")
    browser.app.handle("enter")
    text, _, hits = browser.render()
    assert browser.app.logs.browser
    assert browser.app.logs.path == prior
    assert not any(kind == "log_file" for _, kind, _ in hits)
    assert "ACTIVE_JOB_900_SHOULD_NOT_APPEAR" not in text


def test_browser_shortcut_can_open_grouped_logs_directly_from_jobs_recents(browser):
    browser.app.handle("down")
    browser.app.handle("O")
    browser.render()
    assert browser.app.log_job == "77"
    assert browser.app.logs.browser
    browser.select_path(browser.paths["component"])
    browser.app.handle("enter")
    browser.assert_file(browser.paths["component"])


def test_grouped_catalog_and_open_file_remain_bound_during_live_queue_changes(browser):
    browser.browse()
    entry = browser.select_path(browser.paths["component"])
    browser.store.jobs.insert(0, Job("901", "another running job", "main", "RUNNING"))
    browser.app.selected_id = "900"
    browser.app.cursor["jobs"] = 0
    browser.app.tick()
    browser.render()
    assert browser.app.log_job == "77"
    assert browser.app.logs.entries[browser.app.logs.browser_cursor]["id"] == entry["id"]
    assert browser.sampler.selected[-1] == "77"
    browser.app.handle("enter")
    # Discovery now shares the background file reader. A cold file stays
    # visibly pending until the next UI iterations publish its exact snapshot.
    text, _, _ = browser.render()
    assert "Reading the selected log in the background" in text
    assert "ACTIVE_JOB_900_SHOULD_NOT_APPEAR" not in text
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        browser.app.tick()
        text, _, _ = browser.render()
        assert browser.app.log_job == "77"
        assert browser.app.logs.path == str(browser.paths["component"])
        assert "ACTIVE_JOB_900_SHOULD_NOT_APPEAR" not in text
        if browser.markers[str(browser.paths["component"])] in text:
            break
        time.sleep(.005)
    else:
        pytest.fail("The pinned job's background log did not become visible")
    browser.assert_file(browser.paths["component"])


def test_refresh_updates_catalog_while_preserving_selected_file_identity(browser):
    browser.browse()
    selected = browser.select_path(browser.paths["external"])
    new_path = browser.run_root / "metadata" / "additional.log"
    new_path.write_text("MORE_LOG_EVIDENCE\n", encoding="utf-8")
    report = json.loads(browser.manifest.read_text(encoding="utf-8"))
    report["logs"].insert(0, {"id": "additional", "path": "additional.log", "group": "New group"})
    browser.manifest.write_text(json.dumps(report), encoding="utf-8")
    browser.app.handle("r")
    browser.render()
    assert browser.sampler.refreshes == 1
    assert str(new_path) in {entry["path"] for entry in browser.app.logs.entries}
    assert browser.app.log_entries()[browser.app.logs.browser_cursor]["id"] == selected["id"]
    browser.app.handle("enter")
    browser.assert_file(browser.paths["external"])


def test_browser_redraws_reuse_directory_inventory_and_do_not_query_scheduler(browser):
    class CountingFiles(LocalFiles):
        def __init__(self):
            self.listings = []

        def listdir(self, path):
            self.listings.append(path)
            return super().listdir(path)

    files = CountingFiles()
    browser.views.files = browser.app.logs.files = files
    browser.browse()
    first = list(files.listings)
    selections = list(browser.sampler.selected)
    assert str(browser.paths["stdout"].parent) in first
    assert str(browser.paths["stderr"].parent) in first
    for _ in range(20):
        browser.render()
    assert files.listings == first
    assert browser.sampler.selected == selections


def test_stderr_shortcut_still_opens_the_recorded_stderr_after_manifest_file(browser):
    browser.browse()
    browser.select_path(browser.paths["external"])
    browser.app.handle("enter")
    browser.app.handle("e")
    browser.assert_file(browser.paths["stderr"])
    browser.app.handle("e")
    browser.assert_file(browser.paths["stdout"])


def test_legacy_o_cycle_remains_distinct_from_the_grouped_browser(browser):
    browser.open_history()
    browser.app.handle("o")
    browser.render()
    assert not browser.app.logs.browser
    assert browser.app.logs.file_index > 0
    assert browser.app.logs.path != str(browser.paths["stdout"])
    assert browser.app.log_job == "77"


def test_manifest_can_supply_failed_job_logs_when_scheduler_paths_are_unavailable(tmp_path):
    dashboard = BrowserDashboard(tmp_path, recorded_paths=False).browse()
    dashboard.select_path(dashboard.paths["external"])
    dashboard.app.handle("enter")
    dashboard.assert_file(dashboard.paths["external"])


def test_manifest_for_another_job_cannot_supply_this_jobs_files(tmp_path):
    dashboard = BrowserDashboard(tmp_path, manifest_job="900").browse()
    entries = dashboard.app.logs.entries
    paths = {entry["path"] for entry in entries}
    assert str(dashboard.paths["stdout"]) in paths
    assert str(dashboard.paths["stderr"]) in paths
    assert str(dashboard.paths["external"]) not in paths
    assert str(dashboard.paths["component"]) not in paths


def test_deleted_selected_file_reports_that_file_without_switching_to_live_logs(browser):
    browser.browse()
    browser.select_path(browser.paths["external"])
    browser.paths["external"].unlink()
    browser.app.handle("enter")
    text, _, _ = browser.render()
    assert browser.app.log_job == "77"
    assert browser.app.logs.path == str(browser.paths["external"])
    assert "ONLY_THIS_FILE_EXTERNAL" not in text
    assert "ACTIVE_JOB_900_SHOULD_NOT_APPEAR" not in text
    assert browser.app.logs.buffers[str(browser.paths["external"])].error


def test_opening_another_history_job_clears_the_previous_manifest_entry(browser):
    browser.browse()
    browser.select_path(browser.paths["external"])
    browser.app.handle("enter")
    other_root = browser.root / "another run"
    other_root.mkdir()
    other_path = other_root / "stdout.log"
    other_path.write_text("SECOND_FAILED_JOB_78\n", encoding="utf-8")
    browser.store.finished.insert(0, Finished("78", "second failure", "TIMEOUT", end="2026-10-04T12:00:00", workdir=str(other_root)))
    browser.store.details["78"] = {"StdOut": str(other_path), "StdErr": str(other_path), "WorkDir": str(other_root)}
    browser.app.enter_tab("history")
    browser.app.filter = "78"
    browser.render()
    browser.app.handle("l")
    text, _, _ = browser.render()
    assert browser.app.log_job == "78"
    assert browser.app.logs.path == str(other_path)
    assert "SECOND_FAILED_JOB_78" in text
    assert "ONLY_THIS_FILE_EXTERNAL" not in text
