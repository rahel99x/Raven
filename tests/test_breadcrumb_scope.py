"""Context labels use the painted publication and cannot resurrect old attempts."""
from types import SimpleNamespace

import pytest

from tower import navigation_ui
from tower.layout import row_text, vlen
from tower.model import Job, Store


def setup():
    job = Job("81", "solver", "cpu", "RUNNING", submit="2026-10-11T10:00:00", start="2026-10-11T10:01:00", cluster="lab")
    app = SimpleNamespace(tab="research", log_job=None, research_job_id="81", selected_id="81", cfg={},
                          research_view="experiment", analytics_view="job_series")
    app.project_state = {"auto_target": "81", "auto_attempt": ("lab", job.submit, job.start, "0"),
                         "binding": {"job_id": "81", "project_root": "/project/solver", "run_id": "job-81-a1", "attempt": 1,
                                     "launch": {"cluster": "lab"}, "metrics_file": "/project/solver/runs/job-81-a1/metrics.jsonl"}}
    snap = {"jobs": [job], "finished": [], "departed_jobs": {}, "details": {"81": {"Restarts": "0"}}}
    return app, snap, job


def test_bound_scope_includes_cluster_project_attempt_and_actual_source_without_io(monkeypatch):
    app, snap, _ = setup()
    monkeypatch.setattr("builtins.open", lambda *args, **kwargs: pytest.fail("breadcrumb read a file"))
    text = row_text(navigation_ui.breadcrumb(app, 240, True, snap=snap))
    for expected in ("cluster lab", "project solver", "run job-81-a1/a1", "job 81", "source metrics.jsonl"):
        assert expected in text


@pytest.mark.parametrize("change", ["submit", "start", "cluster", "restart"])
def test_same_id_requeue_or_new_cluster_never_shows_previous_project_attempt(change):
    app, snap, job = setup()
    assert "run job-81-a1" in row_text(navigation_ui.breadcrumb(app, 240, True, snap=snap))
    # A stale detail reply must not mask the new queue/accounting identity.
    snap["details"]["81"].update(SubmitTime=job.submit, StartTime=job.start)
    if change == "restart":
        snap["details"]["81"]["Restarts"] = "1"
    else:
        setattr(job, change, "different")
    text = row_text(navigation_ui.breadcrumb(app, 240, True, snap=snap))
    assert "run job-81-a1" not in text and "source metrics.jsonl" not in text
    assert "job 81" in text


def test_unknown_attempt_is_not_a_verified_association():
    app, snap, _ = setup()
    app.project_state["auto_attempt"] = None
    text = row_text(navigation_ui.breadcrumb(app, 240, True, snap=snap))
    assert "project solver" not in text and "attempt 2026-10-11" in text


@pytest.mark.parametrize("bad", [None, [], "malformed", 1])
def test_malformed_optional_plugin_mappings_do_not_break_render(bad):
    app, snap, _ = setup()
    app.project_state["binding"]["launch"] = bad
    assert "job 81" in row_text(navigation_ui.breadcrumb(app, 240, True, snap=snap))
    app.project_state["binding"]["launch"] = {"cluster": "lab"}
    app.project_state["binding"]["planning_files"] = bad
    app.project_state["binding"]["project_root"] = bad
    app.research_view = "forecast"
    assert "job 81" in row_text(navigation_ui.breadcrumb(app, 240, True, snap=snap))


def test_narrow_width_keeps_current_page_and_job_before_optional_context():
    app, snap, _ = setup()
    text = row_text(navigation_ui.breadcrumb(app, 62, True, snap=snap))
    assert "Research" in text and "job 81" in text
    assert vlen(text) <= 62


def test_cluster_wide_page_does_not_claim_to_be_scoped_to_previous_job():
    app, snap, _ = setup()
    app.tab = "cluster"
    app.cfg = {"cluster_name": "lab"}
    text = row_text(navigation_ui.breadcrumb(app, 240, True, snap=snap))
    assert "cluster lab" in text and "Cluster" in text
    assert "job 81" not in text and "run job-81-a1" not in text and "project solver" not in text


def test_context_is_sanitized_and_new_published_selection_reanchors():
    app, snap, job = setup()
    job.cluster = "lab\x1b[31m"
    app.project_state["binding"]["launch"]["cluster"] = job.cluster
    app.project_state["auto_attempt"] = (job.cluster, job.submit, job.start, "0")
    assert "\x1b" not in row_text(navigation_ui.breadcrumb(app, 240, False, snap=snap))
    changed = dict(snap, jobs=[Job("82", "next", "cpu", "RUNNING", cluster="other", submit="new")])
    app.research_job_id = app.selected_id = "82"
    text = row_text(navigation_ui.breadcrumb(app, 240, True, snap=changed))
    assert "job 82" in text and "cluster other" in text and "job-81-a1" not in text


def test_unchanged_publication_does_not_rescan_cached_job_rows():
    app, snap, _ = setup()
    class Rows(list):
        iterations = 0
        def __iter__(self):
            self.iterations += 1
            return super().__iter__()
    snap["jobs"] = Rows(snap["jobs"])
    navigation_ui.breadcrumb(app, 240, True, snap=snap)
    before = snap["jobs"].iterations
    for _ in range(100):
        navigation_ui.breadcrumb(app, 240, True, snap=snap)
    assert snap["jobs"].iterations == before


def test_real_shallow_snapshots_reuse_the_checked_slot_without_rescanning():
    app, _, selected = setup()
    class Counted:
        reads = 0
        @property
        def id(self):
            type(self).reads += 1
            return "unselected"
    store = Store(persist=False)
    store.jobs = [Counted() for _ in range(10000)] + [selected]
    navigation_ui.breadcrumb(app, 240, True, snap=store.render_snapshot())
    first = Counted.reads
    for _ in range(20):
        navigation_ui.breadcrumb(app, 240, True, snap=store.render_snapshot())
    assert first == 10000 and Counted.reads == first
    # Read the new frame's object, not the previous snapshot's cached object.
    store.jobs[-1] = Job("81", "new", "cpu", "RUNNING", submit="new", cluster="new-cluster")
    text = row_text(navigation_ui.breadcrumb(app, 240, True, snap=store.render_snapshot()))
    assert "cluster new-cluster" in text and "run job-81-a1" not in text
    assert Counted.reads == first
    # A changed queue publication can move the selected job to another slot.
    store.jobs = [selected]
    store.t_jobs += 1
    assert "cluster lab" in row_text(navigation_ui.breadcrumb(app, 240, True, snap=store.render_snapshot()))


def test_native_cached_absence_does_not_hide_a_later_same_size_publication():
    app, _, job = setup()
    store = Store(persist=False)
    store.jobs = [Job("82", "other", "cpu", "RUNNING")]
    assert "cluster lab" not in row_text(navigation_ui.breadcrumb(app, 240, True, snap=store.render_snapshot()))
    store.jobs = [job]
    assert "cluster lab" in row_text(navigation_ui.breadcrumb(app, 240, True, snap=store.render_snapshot()))
