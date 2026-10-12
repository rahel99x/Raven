"""Queue-to-Recents selection changes never commandeer the Queue viewport."""
from dataclasses import replace

import pytest

from raven import recent_history, scrollbars, scrolling
from raven.config import Config
from raven.controller import App
from raven.layout import Glyphs
from raven.model import Finished, Job, Store
from raven.views import Views


@pytest.fixture
def app():
    cfg = Config({"animations": False, "startup_animation": False,
                  "gpu_sampling": False, "smooth_scrolling": False, "log_lines": 0})
    result = App(Store(persist=False), None, None, cfg, "reader", interactive=False)
    result.views_ref = Views(Glyphs(False), cfg)
    jobs = [Job(str(index), f"job-{index:03}", "cpu", "RUNNING") for index in range(100)]
    result.store.apply_jobs(jobs)
    result.store.apply_finished([Finished("old", "earlier-job", "COMPLETED")])
    yield result
    if result.research:
        result.research.close()


def draw(app):
    return app.views_ref.compose(app.store.snapshot(), app, 160, 50)


def pane(app, key):
    return next(item for item in scrollbars.initialize(app)["panes"] if item.key == key)


def select_queue(app, identifier="20", top=15):
    draw(app)
    app.selected_id = identifier
    app.cursor["jobs"] = app.visible_ids.index(identifier)
    app.top["jobs"] = top
    draw(app)
    assert pane(app, "jobs").painted == top


def complete(app, identifiers, state="COMPLETED", *, accounting=True):
    jobs = {job.id: job for job in app.store.jobs}
    app.store.apply_jobs([job for job in jobs.values() if job.id not in identifiers])
    if accounting:
        records = [Finished(identifier, jobs[identifier].name, state,
                            end="2026-10-12T10:00:00") for identifier in identifiers]
        app.store.apply_finished([*records, *app.store.finished])


@pytest.mark.parametrize("mode", ["grouped", "sequential"])
@pytest.mark.parametrize("state", ["COMPLETED", "CANCELLED", "FAILED"])
def test_selected_completion_preserves_queue_viewport_and_exact_actions(app, mode, state):
    app.table_state["recents_mode"] = mode
    select_queue(app)
    app.marks = {"20", "21"}
    complete(app, ["20"], state, accounting=False)

    for accounting in (False, True):
        if accounting:
            app.store.apply_finished([Finished("20", "job-020", state), *app.store.finished])
        for _ in range(2):
            _, hits = draw(app)
            assert app.selected_id == "20"
            assert app.recent_ids[app.cursor["jobs"] - len(app.visible_ids)] == "20"
            assert app.top["jobs"] == pane(app, "jobs").painted == 15
            assert "20" in {identifier for _, kind, identifier in hits if kind == "recent"}
            assert app.marks == {"20", "21"}
        app.marks.clear()
        assert app.target_ids() == ["20"]
        app.marks = {"20", "21"}


@pytest.mark.parametrize("accounting", [False, True])
def test_completion_burst_admits_selected_job_beyond_preview_without_losing_queue_offset(app, accounting):
    select_queue(app, "20", top=15)
    # Newer completions occupy the preview before the selected queue job.
    completed = ["20", *[str(index) for index in range(50, 90)]]
    complete(app, completed, accounting=accounting)
    if accounting:
        app.store.apply_finished([replace(record, end="2026-10-12T09:59:59") if record.id == "20" else record
                                  for record in app.store.finished])
        assert next(index for index, record in enumerate(app.store.finished) if record.id == "20") == 40
    draw(app)
    assert app.selected_id == "20" and app.target_ids() == ["20"]
    assert "20" in app.recent_ids
    assert recent_history.initialize(app).target > recent_history.initialize(app).page
    assert app.top["jobs"] == pane(app, "jobs").painted == 15
    # A further ordinary refresh must not restore the old queue cursor.
    draw(app)
    assert app.selected_id == "20" and app.top["jobs"] == 15


def test_selected_completion_keeps_manual_queue_scroll_even_after_cursor_reanchor(app):
    select_queue(app)
    scrollbars.set_manual(app, "jobs", 35)
    app.top["jobs"] = 35
    draw(app)
    assert pane(app, "jobs").painted == 35 and app.selected_id == "20"
    complete(app, ["20"])
    draw(app)
    assert app.selected_id == "20"
    assert pane(app, "jobs").target == pane(app, "jobs").painted == 35
    assert scrollbars.initialize(app)["manual"]["jobs"]["target"] == 35


def test_completion_clamps_only_when_queue_really_becomes_shorter_than_viewport(app):
    select_queue(app, "80", top=70)
    complete(app, [str(index) for index in range(60, 100)], accounting=False)
    draw(app)
    assert app.selected_id == "80" and app.target_ids() == ["80"]
    queue = pane(app, "jobs")
    assert queue.target == queue.painted == queue.limit
    assert queue.limit < 70


def test_selected_completion_preserves_in_flight_queue_target(app, monkeypatch):
    now = [100.0]
    monkeypatch.setattr(scrolling.time, "monotonic", lambda: now[0])
    app.interactive = True
    app.cfg.set("animations", True)
    app.cfg.set("smooth_scrolling", True)
    app.scrolling_state = None
    select_queue(app)
    scrolling.note_input(app, "wheel")
    app.top["jobs"] = 18
    draw(app)
    before = pane(app, "jobs").painted
    assert before < pane(app, "jobs").target == 18
    complete(app, ["20"])
    now[0] += .01
    draw(app)
    queue = pane(app, "jobs")
    assert app.selected_id == "20" and queue.target == 18
    assert before <= queue.painted <= 18
    now[0] += .3
    draw(app)
    assert pane(app, "jobs").painted == 18


def test_queue_cursor_navigation_resumes_after_completed_selection(app):
    select_queue(app)
    complete(app, ["20"])
    draw(app)
    app.move("home")
    app.sync_selection()
    draw(app)
    assert app.selected_id == app.visible_ids[0]
    assert pane(app, "jobs").painted == 0


def test_selected_completion_joins_collapsed_recent_batch_as_exact_representative(app):
    from raven import job_groups
    jobs = list(app.store.jobs)
    jobs[20] = replace(jobs[20], id="700_1", name="batch-task-1")
    app.store.apply_jobs(jobs)
    app.store.apply_finished([Finished("700_0", "batch-task-0", "COMPLETED"),
                              Finished("700_2", "batch-task-2", "COMPLETED")])
    draw(app)
    group = job_groups.registry(app).index.for_job("700_1")
    assert job_groups.fold(app, group.id, True)
    select_queue(app, "700_1", top=15)
    complete(app, ["700_1"])
    draw(app)
    assert app.selected_id == "700_1" and app.target_ids() == ["700_1"]
    meta = job_groups.metadata_for_record(app, "recent", "700_1")
    assert meta.header and meta.collapsed and meta.representative_id == "700_1"
    assert app.top["jobs"] == 15


def test_active_selection_does_not_scan_accounting_for_a_completion_anchor(app):
    class CountedHistory(list):
        visits = 0

        def __iter__(self):
            for record in super().__iter__():
                self.visits += 1
                yield record

    selected = app.store.jobs[20]
    history = CountedHistory(Finished(str(1000 + index), "past-job", "COMPLETED")
                             for index in range(10_000))
    snap = {"jobs": app.store.jobs, "finished": history}
    app.selected_id = selected.id
    app.last_jobs_ids = [job.id for job in app.store.jobs]
    recent_history.allocate(app, 14, len(app.store.jobs))
    recent_history.records(app, snap)
    history.visits = 0
    records = recent_history.records(app, snap)
    assert len(records) == history.visits == recent_history.candidate_limit(app)
