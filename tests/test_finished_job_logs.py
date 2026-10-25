"""Finished-job log selection through the same keyboard and mouse UI as live jobs."""
import pytest

from raven.config import Config
from raven.controller import App
from raven.layout import Glyphs, row_text
from raven.model import Finished, Job, Store
from raven.remote import LocalFiles
from raven.views import Views, stdout_path


class SelectionSampler:
    """Record selection requests without a scheduler, worker, or live allocation."""

    def __init__(self):
        self.active = []
        self.finished = []
        self.traces = []

    def select(self, job_id):
        self.active.append(job_id)

    def select_fin(self, job_id):
        self.finished.append(job_id)

    def select_trace(self, job_id):
        self.traces.append(job_id)


class FinishedDashboard:
    def __init__(self, root, *, ascii_=True):
        self.root = root
        self.cfg = Config()
        self.cfg.set("log_lines", 0)
        self.store = Store(persist=False)
        self.store.jobs = [Job("900", "active-only", "gpu", "RUNNING", cpus=4)]
        self.store.finished = [
            Finished("700", "zeta-failed", "FAILED", end="2026-10-04T12:00:00"),
            Finished("701", "alpha-success", "COMPLETED", end="2026-10-04T10:00:00"),
            Finished("702", "mu-cancelled", "CANCELLED", end="2026-10-04T11:00:00"),
        ]
        self.paths = {}
        for job in self.store.jobs + self.store.finished:
            self.add_logs(job)
        self.sampler = SelectionSampler()
        self.app = App(self.store, self.sampler, None, self.cfg, "reader", ascii_=ascii_)
        self.views = Views(Glyphs(ascii_), self.cfg, files=LocalFiles())
        self.app.logs.files = self.views.files
        self.app.views_ref = self.views
        self.render()

    def add_logs(self, job):
        workdir = self.root / f"job {job.id}"
        workdir.mkdir()
        stdout = workdir / "stdout.log"
        stderr = workdir / "stderr.log"
        stdout.write_text(f"STDOUT_FOR_{job.id}\n", encoding="utf-8")
        stderr.write_text(f"STDERR_FOR_{job.id}\n", encoding="utf-8")
        self.paths[job.id] = {"out": stdout, "err": stderr}
        self.store.details[job.id] = {
            "StdOut": str(stdout), "StdErr": str(stderr), "WorkDir": str(workdir),
        }

    def render(self, width=150, height=45):
        rows, hits = self.views.compose(self.store.snapshot(), self.app, width, height)
        return "\n".join(row_text(row) for row in rows), rows, hits

    def assert_log(self, job_id, which="out"):
        text, _, _ = self.render()
        marker = f"STD{'ERR' if which == 'err' else 'OUT'}_FOR_{job_id}"
        assert self.app.tab == "log"
        assert self.app.log_job == job_id
        assert self.app.logs.path == str(self.paths[job_id][which])
        assert marker in text
        assert f"STDOUT_FOR_{'900' if job_id != '900' else '700'}" not in text


@pytest.fixture
def dashboard(tmp_path):
    return FinishedDashboard(tmp_path)


@pytest.mark.parametrize("state", [
    "COMPLETED", "FAILED", "CANCELLED", "TIMEOUT", "OUT_OF_MEMORY",
    "NODE_FAIL", "PREEMPTED", "BOOT_FAIL", "DEADLINE", "REVOKED", "SPECIAL_EXIT",
])
def test_history_log_key_opens_selected_state_and_both_streams(dashboard, state):
    dashboard.store.finished[0].state = state
    dashboard.app.handle("3")
    dashboard.render()
    dashboard.app.handle("l")
    dashboard.assert_log("700")
    dashboard.app.handle("e")
    dashboard.assert_log("700", "err")
    dashboard.app.handle("e")
    dashboard.assert_log("700")


@pytest.mark.parametrize("key,reverse,expected", [
    ("name", False, "702"), ("name", True, "702"),
    ("end", False, "702"), ("end", True, "702"),
    ("state", False, "701"), ("state", True, "701"),
])
def test_history_fast_navigation_and_log_use_visible_sorted_rows(dashboard, key, reverse, expected):
    app = dashboard.app
    app.handle("3")
    app.sort["history"], app.reverse["history"] = key, reverse
    dashboard.render()
    # Keys may arrive together before another frame is composed.
    app.handle("home")
    app.handle("down")
    app.handle("l")
    dashboard.assert_log(expected)


@pytest.mark.parametrize("filter_text,expected", [
    ("success", "701"), ("CANCELLED", "702"), ("700", "700"), ("#keep", "701"),
])
def test_history_filter_only_opens_the_matching_visible_job(dashboard, filter_text, expected):
    dashboard.store.tags["701"] = {"tags": ["keep"]}
    dashboard.app.handle("3")
    dashboard.app.filter = filter_text
    dashboard.render()
    dashboard.app.handle("end")
    dashboard.app.handle("l")
    dashboard.assert_log(expected)


def test_history_mouse_click_uses_displayed_order_not_raw_store_order(dashboard):
    app = dashboard.app
    app.handle("3")
    app.sort["history"] = "name"
    _, rows, hits = dashboard.render()
    finished_hits = sorted((y, job_id) for y, kind, job_id in hits if kind == "fin")
    assert [job_id for _, job_id in finished_hits] == ["701", "702", "700"]
    assert all(job_id in row_text(rows[y]) for y, job_id in finished_hits)
    hit = next(hit for hit in hits if hit[1:] == ("fin", "701"))
    assert "701" in row_text(rows[hit[0]])
    app.click(hit[0], app.history_jobs_rect.left + 1, hits)
    assert app.selected_id == "701"
    app.run_command("inspect 701")
    inspector_rows = dashboard.views.overlay(dashboard.store.snapshot(), app, 150, 45)
    assert "alpha-success" in "\n".join(row_text(row) for _, _, row in inspector_rows)
    app.handle("esc")
    assert app.tab == "history" and app.selected_id == "701"
    app.handle("l")
    dashboard.assert_log("701")


def test_history_filter_then_log_without_a_new_frame_uses_new_filter(dashboard):
    app = dashboard.app
    app.handle("3")
    dashboard.render()
    app.handle("/")
    for key in "alpha-success":
        app.handle(key)
    app.handle("enter")
    app.handle("l")
    dashboard.assert_log("701")


def test_history_empty_filter_never_opens_previously_selected_running_logs(dashboard):
    app = dashboard.app
    assert app.selected_id == "900"
    app.handle("3")
    app.filter = "no-such-job"
    dashboard.render()
    app.handle("l")
    text, _, _ = dashboard.render()
    assert app.log_job != "900"
    assert "STDOUT_FOR_900" not in text


def test_history_arrow_end_can_select_offscreen_job_without_intervening_frame(dashboard):
    for number in range(710, 740):
        job = Finished(str(number), f"long-list-{number}", "FAILED", end=f"2026-10-04T09:{number-710:02d}:00")
        dashboard.store.finished.append(job)
        dashboard.add_logs(job)
    dashboard.app.handle("3")
    dashboard.app.sort["history"] = "name"
    dashboard.render(height=18)
    dashboard.app.handle("end")
    dashboard.app.handle("l")
    dashboard.assert_log("700")


def test_jobs_arrows_continue_from_active_queue_into_recent_jobs(dashboard):
    app = dashboard.app
    assert app.selected_id == "900"
    app.handle("down")
    app.handle("l")
    dashboard.assert_log("700")


def test_jobs_recent_keyboard_navigation_returns_to_active_queue(dashboard):
    app = dashboard.app
    app.handle("down")
    app.handle("down")
    app.handle("up")
    app.handle("up")
    app.handle("l")
    dashboard.assert_log("900")


def test_jobs_recent_mouse_selects_and_highlights_exact_job(dashboard):
    text, rows, hits = dashboard.render()
    recent_hit = next(hit for hit in hits if hit[1:] == ("recent", "701"))
    assert "701" in row_text(rows[recent_hit[0]])
    dashboard.app.click(recent_hit[0], dashboard.app.workspace_main_rect.x + 1, hits)
    _, rows, hits = dashboard.render()
    selected_hit = next(hit for hit in hits if hit[1:] == ("recent", "701"))
    selected = rows[selected_hit[0]]
    assert dashboard.app.selected_id == "701"
    assert any("701" in text and "sel" in style.split("+") for text, style in selected)
    dashboard.app.handle("l")
    dashboard.assert_log("701")


@pytest.mark.parametrize("ascii_", [True, False])
def test_jobs_queue_empty_still_selects_recent_logs(tmp_path, ascii_):
    dashboard = FinishedDashboard(tmp_path, ascii_=ascii_)
    dashboard.store.jobs.clear()
    dashboard.render()
    dashboard.app.handle("l")
    dashboard.assert_log("700")


def test_jobs_filter_can_select_recent_when_no_active_jobs_match(dashboard):
    dashboard.app.filter = "alpha-success"
    from raven.table_tools import set_filter_text
    set_filter_text(dashboard.app, "recent", "alpha-success")
    dashboard.render()
    assert dashboard.app.visible_ids == []
    dashboard.app.handle("l")
    dashboard.assert_log("701")


def test_jobs_recent_limit_is_applied_after_filtering(dashboard):
    for number in range(710, 717):
        job = Finished(str(number), f"another-run-{number}", "FAILED")
        dashboard.store.finished.append(job)
        dashboard.add_logs(job)
    dashboard.app.filter = "another-run-716"
    from raven.table_tools import set_filter_text
    set_filter_text(dashboard.app, "recent", "another-run-716")
    dashboard.render()
    dashboard.app.handle("l")
    dashboard.assert_log("716")


def test_jobs_filter_does_not_change_independent_recents(dashboard):
    before = list(dashboard.app.recent_ids)
    dashboard.app.filter = "alpha-success"
    dashboard.render()
    assert dashboard.app.visible_ids == []
    assert dashboard.app.recent_ids == before


def test_recent_focus_is_visible_on_a_small_terminal(dashboard):
    dashboard.app.handle("down")
    _, rows, hits = dashboard.render(width=80, height=18)
    recent_hit = next(hit for hit in hits if hit[1:] == ("recent", "700"))
    selected_row = rows[recent_hit[0]]
    assert "700" in row_text(selected_row)
    assert any("700" in text and "sel" in style.split("+") for text, style in selected_row)
    dashboard.app.handle("l")
    dashboard.assert_log("700")


def test_mark_all_keeps_historical_jobs_out_of_scheduler_action_targets(dashboard):
    dashboard.app.handle("a")
    assert dashboard.app.marks == {"900"}
    assert dashboard.app.visible_ids == ["900"]
    dashboard.app.handle("u")
    dashboard.app.handle("down")
    dashboard.app.handle("space")
    assert dashboard.app.marks == {"700"}
    assert dashboard.app.target_jobs() == []


def test_open_finished_log_remains_bound_while_active_queue_selection_changes(dashboard):
    app = dashboard.app
    app.handle("3")
    dashboard.render()
    app.handle("l")
    dashboard.assert_log("700")
    app.cursor["jobs"] = 0
    app.selected_id = "900"
    dashboard.store.jobs.insert(0, Job("901", "new-running", "gpu", "RUNNING"))
    app.tick()
    dashboard.assert_log("700")
    assert dashboard.sampler.active[-1] == "700"


def test_log_identity_survives_transition_from_live_queue_to_history(dashboard):
    dashboard.app.handle("l")
    dashboard.assert_log("900")
    running = dashboard.store.jobs.pop()
    dashboard.store.finished.append(Finished(running.id, running.name, "FAILED"))
    dashboard.app.tick()
    dashboard.assert_log("900")
    assert dashboard.sampler.active[-1] == "900"


def test_history_array_task_opens_its_exact_task_logs(dashboard):
    task = Finished("800_17", "array-task", "FAILED", end="2026-10-04T13:00:00")
    dashboard.store.finished.append(task)
    dashboard.add_logs(task)
    dashboard.app.handle("3")
    dashboard.render()
    dashboard.app.handle("l")
    dashboard.assert_log("800_17")
    dashboard.app.handle("e")
    dashboard.assert_log("800_17", "err")


def test_missing_finished_log_reports_error_for_that_job_without_live_fallback(dashboard):
    dashboard.paths["700"]["out"].unlink()
    dashboard.app.handle("3")
    dashboard.render()
    dashboard.app.handle("l")
    text, _, _ = dashboard.render()
    assert dashboard.app.log_job == "700"
    assert dashboard.app.logs.path == str(dashboard.paths["700"]["out"])
    assert "700" in text
    assert "STDOUT_FOR_900" not in text
    assert dashboard.app.logs.buffers[str(dashboard.paths["700"]["out"])].error
    dashboard.app.handle("e")
    dashboard.assert_log("700", "err")


def test_deleted_finished_log_stops_showing_cached_contents_without_switching_jobs(dashboard):
    dashboard.app.handle("3")
    dashboard.render()
    dashboard.app.handle("l")
    dashboard.assert_log("700")
    dashboard.paths["700"]["out"].unlink()
    text, _, _ = dashboard.render()
    assert dashboard.app.log_job == "700"
    assert dashboard.app.logs.path == str(dashboard.paths["700"]["out"])
    assert "STDOUT_FOR_700" not in text
    assert "STDOUT_FOR_900" not in text


def test_unknown_finished_paths_do_not_reuse_previously_opened_active_log(dashboard):
    dashboard.app.handle("l")
    dashboard.assert_log("900")
    dashboard.store.details.pop("700")
    dashboard.app.handle("3")
    dashboard.render()
    dashboard.app.handle("l")
    text, _, _ = dashboard.render()
    assert dashboard.app.log_job == "700"
    assert "STDOUT_FOR_900" not in text
    assert "STDOUT_FOR_700" not in text


@pytest.mark.parametrize("pattern,filename", [
    ("%j.out", "123.out"),
    ("%A_%a.out", "123_4294967294.out"),
    ("%x-%u-%j.out", "study-alice-123.out"),
    ("%04j-%06A.out", "0123-000123.out"),
    ("%010a.out", "4294967294.out"),
    ("literal%%-%j.out", "literal%-123.out"),
    ("%j-copy-%j.out", "123-copy-123.out"),
])
def test_accounting_log_patterns_expand_only_verified_job_tokens(tmp_path, pattern, filename):
    job = Finished("123", "study", "FAILED")
    metadata = {"LogPathSource": "sacct", "StdOut": pattern, "WorkDir": str(tmp_path), "User": "alice"}
    assert stdout_path(job, metadata, LocalFiles()) == str(tmp_path / filename)


@pytest.mark.parametrize("pattern,filename", [
    ("%j.out", "456.out"),
    ("%A_%a.out", "123_7.out"),
    ("%06j-%04A-%03a.out", "000456-0123-007.out"),
    ("%x-%u-%j-%A-%a.err", "study-alice-456-123-7.err"),
])
def test_array_accounting_patterns_use_raw_allocation_and_exact_task_identity(tmp_path, pattern, filename):
    job = Finished("123_7", "study", "FAILED")
    metadata = {"LogPathSource": "sacct", "StdOut": pattern, "WorkDir": str(tmp_path), "User": "alice",
                "JobIDRaw": "456", "ArrayJobId": "123", "ArrayTaskId": "7"}
    assert stdout_path(job, metadata, LocalFiles()) == str(tmp_path / filename)


@pytest.mark.parametrize("pattern", ["%j.out", "%04j.out"])
def test_array_pattern_with_missing_raw_allocation_id_abstains(tmp_path, pattern):
    job = Finished("123_7", "study", "FAILED")
    metadata = {"LogPathSource": "sacct", "StdOut": pattern, "WorkDir": str(tmp_path),
                "ArrayJobId": "123", "ArrayTaskId": "7"}
    assert stdout_path(job, metadata, LocalFiles()) == ""


def test_array_parent_and_task_tokens_do_not_require_an_unavailable_raw_allocation_id(tmp_path):
    job = Finished("123_7", "study", "FAILED")
    metadata = {"LogPathSource": "sacct", "StdOut": "%A_%a.out", "WorkDir": str(tmp_path)}
    assert stdout_path(job, metadata, LocalFiles()) == str(tmp_path / "123_7.out")


@pytest.mark.parametrize("workdir", ["", "unknown", "(null)", "n/a", "none", "relative/run"])
def test_relative_historical_pattern_abstains_without_a_real_absolute_workdir(workdir):
    job = Finished("123", "study", "FAILED")
    metadata = {"LogPathSource": "sacct", "StdOut": "logs/%j.out", "WorkDir": workdir}
    assert stdout_path(job, metadata, LocalFiles()) == ""


def test_relative_accounting_path_can_use_recorded_finished_workdir(tmp_path):
    job = Finished("123", "study", "FAILED", workdir=str(tmp_path))
    metadata = {"LogPathSource": "sacct", "StdOut": "logs/%j.out"}
    assert stdout_path(job, metadata, LocalFiles()) == str(tmp_path / "logs/123.out")


@pytest.mark.parametrize("which", ["StdOut", "StdErr"])
@pytest.mark.parametrize("source", ["scontrol", ""])
def test_concrete_scheduler_percent_filenames_are_not_treated_as_patterns(tmp_path, source, which):
    job = Finished("123", "study", "FAILED")
    concrete = str(tmp_path / "literal-%j-%A-%a-%x-%u-%N.out")
    metadata = {which: concrete}
    if source:
        metadata["LogPathSource"] = source
    assert stdout_path(job, metadata, LocalFiles(), which) == concrete


@pytest.mark.parametrize("token", ["%N", "%n", "%t", "%s", "%b", "%11j", "%04x", "%100j"])
def test_unsupported_or_ambiguous_historical_tokens_abstain(token):
    job = Finished("123", "study", "FAILED")
    metadata = {"LogPathSource": "sacct", "StdOut": f"/scratch/logs/job-{token}.out", "User": "alice"}
    assert stdout_path(job, metadata, LocalFiles()) == ""


def test_accounting_user_token_abstains_when_job_user_is_unknown():
    job = Finished("123", "study", "FAILED")
    metadata = {"LogPathSource": "sacct", "StdOut": "/scratch/%u/%j.out"}
    assert stdout_path(job, metadata, LocalFiles()) == ""


@pytest.mark.parametrize("which,suffix", [("StdOut", "out"), ("StdErr", "err")])
def test_historical_unknown_paths_never_guess_a_same_named_current_directory_file(tmp_path, monkeypatch, which, suffix):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "logs").mkdir()
    (tmp_path / f"logs/study-123.{suffix}").write_text("UNVERIFIED_CWD_LOG\n")
    job = Finished("123", "study", "FAILED")
    assert stdout_path(job, {}, LocalFiles(), which) == ""


@pytest.mark.parametrize("which,metadata_key,suffix", [("out", "StdOut", "out"), ("err", "StdErr", "err")])
def test_history_log_view_reads_the_exact_expanded_accounting_file(dashboard, which, metadata_key, suffix):
    directory = dashboard.paths["700"][which].parent
    actual = directory / f"expanded-0700-zeta-failed.{suffix}"
    actual.write_text(f"STD{'ERR' if which == 'err' else 'OUT'}_FOR_700\n", encoding="utf-8")
    dashboard.paths["700"][which] = actual
    dashboard.store.details["700"].update({"LogPathSource": "sacct", metadata_key: f"expanded-%04j-%x.{suffix}"})
    dashboard.app.handle("3")
    dashboard.render()
    dashboard.app.handle("l")
    if which == "err":
        dashboard.app.handle("e")
    dashboard.assert_log("700", which)
