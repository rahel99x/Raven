"""Adaptive Recents use real Jobs composition and exact lifecycle records."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from raven import job_groups, layout as L, pane_drag, recent_history, workspace_layout
from raven.config import Config
from raven.controller import App
from raven.model import Finished, Job, Store
from raven.remote import LocalFiles
from raven.views import Views


@pytest.fixture
def dashboard(tmp_path):
    cfg = Config()
    cfg.set("log_lines", 0)
    store = Store(persist=False)
    store.jobs = [Job("900", "active-exact", "cpu", "RUNNING", cpus=4)]
    store.finished = [Finished(str(1000 + index), f"history-{index:04d}", "COMPLETED",
                               end="2026-10-08T11:00:00") for index in range(1000)]
    app = App(store, None, None, cfg, "reader", ascii_=True)
    views = Views(L.Glyphs(True), cfg, files=LocalFiles())
    app.views_ref, app.logs.files = views, views.files
    workspace_layout.initialize(app).density = "compact"
    def render(width=160, height=48):
        app.width, app.height = width, height
        return views.compose(store.snapshot(), app, width, height)
    fixture = SimpleNamespace(app=app, store=store, views=views, render=render, root=tmp_path)
    yield fixture
    if app.research:
        app.research.close()


def recent_hits(hits):
    return [(y, jid) for y, kind, jid in hits if kind == "recent"]


def select(app, hits, jid):
    y = next(y for y, kind, key in hits if kind in ("job", "recent", "fin") and key == jid)
    rect = recent_history.initialize(app).rect
    x = rect.x + 3 if rect is not None and jid in app.recent_ids else app.workspace_main_rect.x + 4
    app.click(y, x, hits)


@pytest.mark.parametrize("width,density", [(80, "compact"), (160, "compact"), (160, "comfortable")])
def test_native_jobs_recents_use_actual_main_height_and_grow_with_terminal(dashboard, width, density):
    app = dashboard.app
    app.layout_state.density = density
    short_rows, short_hits = dashboard.render(width, 28)
    short_count = len(recent_hits(short_hits))
    assert short_count >= 1
    tall_rows, tall_hits = dashboard.render(width, 60)
    state = recent_history.initialize(app)
    assert len(recent_hits(tall_hits)) > short_count
    assert len(recent_hits(tall_hits)) > 5
    assert len(recent_hits(tall_hits)) == state.page
    assert state.page < app.workspace_main_usable_height <= app.workspace_main_rect.height
    assert all(L.vlen(L.row_text(row)) <= width for row in short_rows + tall_rows)
    assert all(0 <= y < 60 for y, _, _ in tall_hits)


def test_manual_split_preserves_blank_queue_space_in_real_main_canvas(dashboard):
    _, initial_hits = dashboard.render()
    initial = pane_drag.initialize(dashboard.app)["dividers"]["recent:jobs"].y
    recent_history.resize(dashboard.app, 20)
    rows, hits = dashboard.render()
    divider = pane_drag.initialize(dashboard.app)["dividers"]["recent:jobs"]
    assert divider.y > initial + 10
    assert len([hit for hit in hits if hit[1] == "job"]) == 1
    assert len(recent_hits(hits)) == recent_history.initialize(dashboard.app).page
    main = dashboard.app.workspace_main_rect
    queue_y = next(y for y, kind, _ in hits if kind == "job")
    assert all(not L.row_text(rows[y])[main.x:main.x + main.width].strip()
               for y in range(queue_y + 1, divider.y))


@pytest.mark.parametrize("buffer", [-1, 0, 1])
def test_horizontal_divider_pointer_buffer_resizes_sparse_queue_without_marking_jobs(dashboard, buffer):
    app = dashboard.app
    _, hits = dashboard.render()
    divider = pane_drag.initialize(app)["dividers"]["recent:jobs"]
    before = recent_history.initialize(app).page
    # Job rows and headings keep their click areas. Buffer dragging uses the
    # unoccupied leading cell on either side of the actual divider line.
    x, original_y = divider.x if buffer else divider.x + divider.width // 2, divider.y
    save = app.save = Mock()
    app.click(original_y + buffer, x, hits, button="press")
    assert pane_drag.active(app)
    app.click(original_y + 10 + buffer, x, hits, button="drag")
    _, moved_hits = dashboard.render()
    moved = pane_drag.initialize(app)["dividers"]["recent:jobs"]
    assert moved.y > original_y
    assert recent_history.initialize(app).page < before
    app.click(moved.y + buffer, x, moved_hits, button="release")
    assert not pane_drag.active(app) and save.call_count == 1
    assert app.marks == set()


def test_real_keyboard_burst_loads_older_ids_before_log_action(dashboard):
    app = dashboard.app
    _, hits = dashboard.render()
    select(app, hits, "1000")
    for _ in range(80):
        app.handle("down")
    assert app.selected_id == "1080"
    assert app.recent_ids[app.cursor["jobs"] - len(app.visible_ids)] == "1080"
    path = dashboard.root / "older-exact.log"
    path.write_text("OLDER_JOB_1080_ONLY\n", encoding="utf-8")
    dashboard.store.details["1080"] = {"StdOut": str(path), "WorkDir": str(dashboard.root)}
    app.handle("l")
    rows, _ = dashboard.render()
    assert app.tab == "log" and app.log_job == "1080"
    assert "OLDER_JOB_1080_ONLY" in L.to_text(rows, app.width)


def test_mouse_wheel_targets_recents_then_keyboard_returns_to_active_queue(dashboard):
    app = dashboard.app
    _, hits = dashboard.render()
    state = recent_history.initialize(app)
    assert state.rect is not None
    for _ in range(50):
        app.click(state.rect.y + 2, state.rect.x + 4, hits, button="wheel-down")
    assert app.selected_id == "1050"
    _, scrolled_hits = dashboard.render()
    assert app.selected_id in [jid for _, jid in recent_hits(scrolled_hits)]
    app.handle("home")
    assert app.selected_id == "1000"
    app.handle("up")
    assert app.selected_id == "900" and app.cursor["jobs"] == 0


def test_end_then_resize_keeps_selected_oldest_id_visible(dashboard):
    app = dashboard.app
    _, hits = dashboard.render()
    select(app, hits, "1000")
    app.handle("end")
    assert app.selected_id == "1999"
    for width, height in ((160, 48), (80, 28), (120, 55)):
        _, resized_hits = dashboard.render(width, height)
        assert app.selected_id == "1999"
        assert "1999" in [jid for _, jid in recent_hits(resized_hits)]


def test_live_departure_and_accounting_keep_selected_history_identity(dashboard):
    app, store = dashboard.app, dashboard.store
    store.apply_jobs(store.jobs)
    store.apply_finished(store.finished)
    _, hits = dashboard.render()
    select(app, hits, "1005")
    store.apply_jobs([])
    _, waiting_hits = dashboard.render()
    assert "900" in app.recent_ids and "900" not in app.visible_ids
    assert app.selected_id == "1005"
    assert "1005" in [jid for _, jid in recent_hits(waiting_hits)]
    store.apply_finished([Finished("900", "active-exact", "FAILED", end="2026-10-08T12:00:00")] + store.finished)
    _, confirmed_hits = dashboard.render()
    assert app.selected_id == "1005"
    confirmed = next(record for record in app.recent_jobs(store.snapshot()) if record.id == "900")
    assert isinstance(confirmed, Finished) and confirmed.state == "FAILED"
    assert "900" in [jid for _, jid in recent_hits(confirmed_hits)]


def test_live_completion_keeps_selected_preview_boundary_when_queue_size_is_constant(dashboard):
    app, store = dashboard.app, dashboard.store
    store.jobs.append(Job("901", "other-active", "cpu", "RUNNING", cpus=4))
    store.apply_jobs(store.jobs)
    store.apply_finished(store.finished)
    _, hits = dashboard.render()
    selected = recent_hits(hits)[-1][1]
    select(app, hits, selected)
    store.apply_jobs([store.jobs[1], Job("902", "new-active", "cpu", "RUNNING", cpus=4)])
    _, updated = dashboard.render()
    assert app.selected_id == selected
    assert selected in [jid for _, jid in recent_hits(updated)]


def test_shared_array_fold_reanchors_real_members_in_queue_recents_and_history(dashboard):
    app, store = dashboard.app, dashboard.store
    store.jobs = [Job(f"800_{i}", "launch", "cpu", "RUNNING", cpus=1) for i in range(3)]
    store.finished = [Finished(f"800_{i}", "launch", "COMPLETED", end="2026-10-08T12:00:00")
                      for i in range(3, 6)] + store.finished
    _, hits = dashboard.render()
    select(app, hits, "800_1")
    group = job_groups.registry(app).index.for_job("800_1")
    assert group is not None
    app.handle("left")
    _, closed_hits = dashboard.render()
    assert app.selected_id == "800_0"
    assert [jid for _, kind, jid in closed_hits if kind == "job"] == ["800_0"]
    assert [jid for _, kind, jid in closed_hits if kind == "recent" and jid.startswith("800_")] == ["800_3"]
    app.enter_tab("history")
    _, history_hits = dashboard.render()
    assert [jid for _, kind, jid in history_hits if kind == "fin" and jid.startswith("800_")] == ["800_3"]
    app.run_command("jobgroup open " + group.id)
    _, reopened_history = dashboard.render()
    assert {jid for _, kind, jid in reopened_history if kind == "fin" and jid.startswith("800_")} == {"800_3", "800_4", "800_5"}
    app.enter_tab("jobs")
    _, reopened_jobs = dashboard.render()
    assert {jid for _, kind, jid in reopened_jobs if kind == "job"} == {"800_0", "800_1", "800_2"}


def test_tiny_native_main_keeps_jobid_header_and_selected_queue_visible(dashboard):
    app = dashboard.app
    app.layout_state.density = "comfortable"
    rows, hits = dashboard.render(80, 24)
    assert app.workspace_main_usable_height >= 3
    assert any(kind == "sort_header" and value[:2] == ("jobs", "id") for _, kind, value in hits)
    assert any(kind == "job" and jid == "900" for _, kind, jid in hits)
    assert "JOBID" in L.to_text(rows, 80)
