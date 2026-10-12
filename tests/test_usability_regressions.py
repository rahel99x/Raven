"""Selection and palette consistency across empty views and log navigation."""
from __future__ import annotations

import pytest

from raven.config import Config
from raven.controller import App
from raven.layout import Glyphs, row_text
from raven.model import Finished, Job, Store
from raven.remote import LocalFiles
from raven.views import Views


class Dashboard:
    def __init__(self, root):
        self.cfg = Config({"log_lines": 0})
        self.store = Store(persist=False)
        self.store.jobs = [Job("900", "active experiment", "main", "RUNNING")]
        self.store.finished = [Finished("700", "failed experiment", "FAILED")]
        self.paths = {}
        for record in self.store.jobs + self.store.finished:
            path = root / f"stdout-{record.id}.log"
            path.write_text(f"ERROR_FOR_{record.id}\n", encoding="utf-8")
            self.paths[record.id] = path
            self.store.details[record.id] = {
                "StdOut": str(path), "StdErr": str(path), "WorkDir": str(root),
            }
        self.app = App(self.store, None, None, self.cfg, "reader", ascii_=True)
        self.views = Views(Glyphs(True), self.cfg, files=LocalFiles())
        self.app.logs.files = self.views.files
        self.app.views_ref = self.views
        self.render()

    def render(self):
        rows, hits = self.views.compose(self.store.snapshot(), self.app, 120, 35)
        return "\n".join(row_text(row) for row in rows), hits


@pytest.fixture
def dashboard(tmp_path):
    return Dashboard(tmp_path)


@pytest.mark.parametrize("tab", ["group", "deps"])
@pytest.mark.parametrize("key", ["l", "enter"])
def test_empty_job_view_does_not_open_previous_tabs_selection(dashboard, tab, key):
    app = dashboard.app
    assert app.selected_id == "900"
    app.enter_tab(tab)
    dashboard.render()
    app.handle(key)
    assert app.selected_id is None
    assert app.log_job is None
    assert app.detail_id is None
    assert app.tab == tab


def test_group_queue_becoming_empty_clears_its_previous_ids(dashboard):
    app = dashboard.app
    dashboard.store.group = [dashboard.store.jobs[0]]
    app.enter_tab("group")
    dashboard.render()
    assert app.group_ids == ["900"]
    dashboard.store.group.clear()
    dashboard.render()
    app.handle("l")
    assert app.group_ids == []
    assert app.selected_id is None
    assert app.log_job is None
    assert app.tab == "group"


def test_filtered_empty_group_does_not_open_an_unmatched_job(dashboard):
    app = dashboard.app
    dashboard.store.group = [dashboard.store.jobs[0]]
    app.enter_tab("group")
    app.filter = "no matching jobs"
    dashboard.render()
    app.handle("l")
    assert app.group_ids == []
    assert app.selected_id is None
    assert app.log_job is None
    assert app.tab == "group"


def test_palette_find_from_history_binds_selected_job_not_previous_log(dashboard):
    app = dashboard.app
    app.handle("l")
    dashboard.render()
    assert app.log_job == "900"
    app.enter_tab("history")
    dashboard.render()
    app.run_command("find ERROR_FOR_700")
    text, _ = dashboard.render()
    assert app.tab == "log"
    assert app.log_job == "700"
    assert app.logs.path == str(dashboard.paths["700"])
    assert app.logs.search == "ERROR_FOR_700"
    assert "ERROR_FOR_700" in text
    assert "ERROR_FOR_900" not in text


def test_palette_find_without_selected_history_job_keeps_old_logs_closed(dashboard):
    app = dashboard.app
    app.handle("l")
    dashboard.render()
    app.enter_tab("history")
    app.filter = "no matching history"
    dashboard.render()
    app.run_command("find ERROR")
    assert not app.command_ok
    assert app.tab == "history"


def test_palette_find_in_log_view_preserves_exact_current_job(dashboard):
    app = dashboard.app
    app.enter_tab("history")
    dashboard.render()
    app.handle("l")
    dashboard.render()
    app.run_command("find ERROR")
    text, _ = dashboard.render()
    assert app.command_ok
    assert app.log_job == "700"
    assert app.logs.search == "ERROR"
    assert "ERROR_FOR_700" in text


@pytest.mark.parametrize("payload", ["stderr", "two words", ""])
def test_palette_filter_in_log_browser_only_changes_file_filter(dashboard, payload):
    app = dashboard.app
    app.handle("l")
    dashboard.render()
    app.logs.browser = True
    app.logs.browser_cursor, app.logs.browser_top = 4, 3
    app.logs.file_filter = "previous"
    app.filter = "retained jobs filter"
    app.run_command("filter " + payload)
    assert app.command_ok
    assert app.logs.file_filter == payload
    assert app.logs.browser_cursor == app.logs.browser_top == 0
    assert app.filter == "retained jobs filter"


def test_palette_filter_in_jobs_keeps_existing_job_filter_behavior(dashboard):
    dashboard.app.run_command("filter active experiment")
    assert dashboard.app.command_ok
    assert dashboard.app.filter == "active experiment"
    assert dashboard.app.logs.file_filter == ""


@pytest.mark.parametrize("job_id", ["900", "700"])
def test_pager_uses_selected_jobs_stderr_after_stream_toggle(dashboard, job_id):
    app = dashboard.app
    stderr = dashboard.paths[job_id].parent / f"error stream {job_id}.log"
    stderr.write_text(f"STDERR_ONLY_{job_id}\n", encoding="utf-8")
    dashboard.store.details[job_id]["StdErr"] = str(stderr)
    app.open_log(job_id)
    dashboard.render()
    app.handle("e")
    text, _ = dashboard.render()
    assert f"STDERR_ONLY_{job_id}" in text
    app.handle("L")
    assert app.want_less
    assert dashboard.views.pager_path(dashboard.store.snapshot(), app) == str(stderr)


@pytest.mark.parametrize("job_id", ["900", "700"])
def test_pager_uses_the_extra_log_selected_with_cycle_key(dashboard, job_id):
    app = dashboard.app
    extra = dashboard.paths[job_id].parent / f"worker-{job_id}.log"
    extra.write_text(f"WORKER_ONLY_{job_id}\n", encoding="utf-8")
    app.open_log(job_id)
    dashboard.render()
    app.handle("o")
    text, _ = dashboard.render()
    assert f"WORKER_ONLY_{job_id}" in text
    app.handle("L")
    assert app.want_less
    assert dashboard.views.pager_path(dashboard.store.snapshot(), app) == str(extra)


@pytest.mark.parametrize("job_id", ["900", "700"])
def test_pager_from_file_browser_opens_the_selected_external_log(dashboard, job_id):
    app = dashboard.app
    external = dashboard.paths[job_id].parent / "separate location" / f"external-{job_id}.log"
    external.parent.mkdir()
    external.write_text(f"EXTERNAL_ONLY_{job_id}\n", encoding="utf-8")
    app.open_log(job_id)
    dashboard.render()
    app.logs.entries = [
        {"id": "stdout", "path": str(dashboard.paths[job_id]), "label": "Scheduler output", "group": "Scheduler"},
        {"id": "external", "path": str(external), "label": "External diagnostics", "group": "Application"},
    ]
    app.logs.browser, app.logs.browser_cursor = True, 1
    app.handle("L")
    assert app.want_less
    assert app.logs.entry["id"] == "external"
    assert not app.logs.browser
    assert dashboard.views.pager_path(dashboard.store.snapshot(), app) == str(external)


def test_pager_from_history_uses_its_row_despite_previous_live_log(dashboard):
    app = dashboard.app
    app.open_log("900")
    dashboard.render()
    app.enter_tab("history")
    dashboard.render()
    app.handle("L")
    assert app.want_less
    assert dashboard.views.pager_path(dashboard.store.snapshot(), app) == str(dashboard.paths["700"])


def test_pager_without_a_matching_history_row_has_no_old_job_fallback(dashboard):
    app = dashboard.app
    app.open_log("900")
    dashboard.render()
    app.enter_tab("history")
    app.filter = "no matching history"
    dashboard.render()
    app.handle("L")
    assert dashboard.views.pager_path(dashboard.store.snapshot(), app) == ""


def test_pager_in_empty_filtered_file_browser_does_not_open_previous_file(dashboard):
    app = dashboard.app
    app.open_log("700")
    dashboard.render()
    app.logs.entries = [{"id": "stdout", "path": str(dashboard.paths["700"]), "label": "Scheduler output", "group": "Scheduler"}]
    app.logs.browser = True
    app.logs.file_filter = "no matching file"
    app.handle("L")
    assert not getattr(app, "want_less", False)
    assert app.logs.browser


@pytest.mark.parametrize("height", [None, 40])
def test_recent_rows_show_current_tags_without_visiting_history(dashboard, height):
    app = dashboard.app
    assert app.tab == "jobs"
    dashboard.store.tags = {"700": {"tags": ["first"]}}
    app.filter = "#first"
    rows, hits = dashboard.views.compose(dashboard.store.snapshot(), app, 240, height)
    # A wide Details pane may share a physical row with a table header. Use
    # the exact recent-row hit rather than text from an unrelated right pane.
    selected_row = (row_text(rows[next(y for y, kind, jid in hits if kind == "recent" and jid == "700")])
                    if height is not None else next(row_text(row) for row in rows
                    if "700" in row_text(row) and "failed experiment" in row_text(row)))
    assert "first" in selected_row
    # Replace the mapping, so a tag cache retained from a prior frame cannot pass.
    dashboard.store.tags = {"700": {"tags": ["revised"]}}
    app.filter = "#revised"
    rows, hits = dashboard.views.compose(dashboard.store.snapshot(), app, 240, height)
    selected_row = (row_text(rows[next(y for y, kind, jid in hits if kind == "recent" and jid == "700")])
                    if height is not None else next(row_text(row) for row in rows
                    if "700" in row_text(row) and "failed experiment" in row_text(row)))
    assert "revised" in selected_row
    assert "first" not in selected_row
    assert app.tab == "jobs"
    assert app.recent_ids == ["700"]


def test_full_report_from_log_browser_exports_selected_content_without_mutating_ui(dashboard):
    from raven.report import build

    app = dashboard.app
    custom = dashboard.paths["700"].parent / "custom-700.log"
    custom.write_text("".join(f"CUSTOM_CONTENT_{i:02d}\n" for i in range(80)), encoding="utf-8")
    app.open_log("700")
    dashboard.render()
    entry = {"id": "custom", "path": str(custom), "label": "Selected custom diagnostics", "group": "Application"}
    app.logs.entries = [entry]
    app.logs.entry = dict(entry)
    app.logs.path = str(custom)
    app.logs.top = 5
    app.logs.browser, app.logs.browse_return = True, True
    app.logs.browser_cursor, app.logs.browser_top = 0, 3
    app.logs.file_filter = "Selected custom"
    before = {
        "tab": app.tab, "selected_id": app.selected_id, "log_job": app.log_job,
        "browser": app.logs.browser, "browse_return": app.logs.browse_return,
        "browser_cursor": app.logs.browser_cursor, "browser_top": app.logs.browser_top,
        "file_filter": app.logs.file_filter, "entry": dict(app.logs.entry),
        "entries": [dict(item) for item in app.logs.entries],
        "path": app.logs.path, "top": app.logs.top,
    }
    page = build(dashboard.store.snapshot(), app, dashboard.views)
    log_section = page.split("08 / LOG\n", 1)[1].split("09 / SOURCES\n", 1)[0]
    assert "CUSTOM_CONTENT_05" in log_section
    assert "CUSTOM_CONTENT_44" in log_section
    assert "CUSTOM_CONTENT_04" not in log_section
    assert "CUSTOM_CONTENT_45" not in log_section
    assert "ERROR_FOR_900" not in log_section
    assert "log files" not in log_section
    after = {
        "tab": app.tab, "selected_id": app.selected_id, "log_job": app.log_job,
        "browser": app.logs.browser, "browse_return": app.logs.browse_return,
        "browser_cursor": app.logs.browser_cursor, "browser_top": app.logs.browser_top,
        "file_filter": app.logs.file_filter, "entry": dict(app.logs.entry),
        "entries": [dict(item) for item in app.logs.entries],
        "path": app.logs.path, "top": app.logs.top,
    }
    assert after == before
