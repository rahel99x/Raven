"""Independent panes build only their visible width and reuse exact wrapping."""
from types import SimpleNamespace

import pytest

from raven import job_panels as J, layout as L, scrollbars as S, workspace_layout as W
from raven.config import Config
from raven.controller import App
from raven.model import Finished, Job, Live, Store
from raven.views import Views


@pytest.fixture
def dashboard():
    cfg = Config()
    cfg.set("log_lines", 0)
    store = Store(persist=False)
    store.jobs = [Job("900", "selected-exact", "cpu", "RUNNING", cpus=8, mem_req="8G")]
    store.finished = [Finished("700", "previous-exact", "COMPLETED", cpus=8,
                               elapsed="01:00:00", cpu_time=7200,
                               req_mem=8 * 1024 ** 3, rss=2 * 1024 ** 3,
                               limit="02:00:00")]
    store.live["900"] = Live(rss=1024 ** 3, rate=.5)
    app = App(store, None, None, cfg, "test", interactive=False)
    views = Views(L.Glyphs(False), cfg)
    app.views_ref = views
    yield SimpleNamespace(app=app, store=store, views=views)
    if app.research:
        app.research.close()


@pytest.mark.parametrize("width,height,density", [(40, 60, "compact"), (80, 50, "compact"),
                                                 (160, 50, "compact"), (180, 60, "comfortable")])
def test_details_source_builds_once_at_its_actual_width(dashboard, monkeypatch, width, height, density):
    app, views, store = dashboard.app, dashboard.views, dashboard.store
    W.initialize(app).density = density
    J.run_command(app, ["jobpanel", "analytics", "advisor"])
    sources, queue_widths = [], []
    actual_cards, actual_jobs = J._analytics_cards, views.jobs_tab
    def cards(*args, **kwargs):
        sources.append((args[2].selected_id, kwargs["width"]))
        return actual_cards(*args, **kwargs)
    def queue(snap, target, actions, panel_width, panel_height, **kwargs):
        queue_widths.append(panel_width)
        return actual_jobs(snap, target, actions, panel_width, panel_height, **kwargs)
    monkeypatch.setattr(J, "_analytics_cards", cards)
    monkeypatch.setattr(views, "jobs_tab", queue)
    rows, hits = views.compose(store.snapshot(), app, width, height)
    rect = app.job_panel_rect
    padding = 2 if density == "comfortable" and rect.width >= 8 and rect.height >= 5 else 0
    assert sources == [("900", rect.width - padding - 1)]
    pane = next(item for item in S.initialize(app)["panes"] if item.key == "workspace:jobs:details")
    assert sources[0][1] == pane.rect.right - pane.rect.left - 1
    assert len(queue_widths) == 1
    assert all(L.vlen(L.row_text(row)) <= width for row in rows)
    assert all(0 <= y < height for y, _, _ in hits)
    assert app.selected_id == "900" and app.tab == "jobs"
    assert not getattr(app, "job_panel_defer_content", False)


def test_hidden_details_never_builds_source_or_requests_worker(dashboard, monkeypatch):
    state = W.initialize(dashboard.app)
    state.focus, state.maximized = "main", True
    J.run_command(dashboard.app, ["jobpanel", "research", "experiment"])
    state.focus = "main"
    def forbidden(*args, **kwargs):
        raise AssertionError("A hidden Details pane built a source document")
    monkeypatch.setattr(J, "_research", forbidden)
    rows, hits = dashboard.views.compose(dashboard.store.snapshot(), dashboard.app, 160, 50)
    assert dashboard.app.selected_id == "900"
    assert dashboard.app.research is None
    assert dashboard.app.job_panel_rect is None
    assert any(kind == "job" and target == "900" for _, kind, target in hits)
    assert all("Details" not in L.row_text(row) for row in rows)


def test_deferred_queue_identity_restores_flags_if_renderer_fails(dashboard):
    app = dashboard.app
    app.job_panel_defer_content = False
    app.job_panel_source_canvas = False
    def failure(*args):
        assert app.job_panel_defer_content and app.job_panel_source_canvas
        raise ValueError("source failed")
    with pytest.raises(ValueError, match="source failed"):
        W.render_body(dashboard.views, dashboard.store.snapshot(), app, 160, 50, None, failure)
    assert not app.job_panel_defer_content and not app.job_panel_source_canvas


def test_narrow_advisor_aggregates_once_and_retains_empty_running_cards(dashboard, monkeypatch):
    from raven import advisor
    app, views, store = dashboard.app, dashboard.views, dashboard.store
    store.finished.clear()
    store.live["900"] = Live(rss=4 * 1024 ** 3, rate=.5)
    J.run_command(app, ["jobpanel", "analytics", "advisor"])
    calls, original = [], advisor.advise_names
    monkeypatch.setattr(advisor, "advise_names", lambda records: (calls.append(len(records)), original(records))[1])
    rows, _ = views.compose(store.snapshot(), app, 180, 80)
    text = L.to_text(rows, 180)
    assert calls == [0]
    assert "nothing finished in the window" in text
    assert "Running jobs so far" in text
    assert "900 / selected-exact" in text
    assert "--mem 5G" in text


def test_reflow_cache_reuses_wrapping_during_scroll_and_detects_in_place_edits(monkeypatch):
    app = SimpleNamespace(cfg={}, tab="jobs", mode="main", selected_id="900")
    state = W.initialize(app)
    rows = [[(" A long report sentence with every character retained during wrapping.", "cyan")]]
    target = {"job": "900", "path": "/exact/old.log"}
    hits = [(0, "job_panel_action", (("inline_log", target), 0, 18))]
    calls, original = [], W._reflow
    def reflow(*args, **kwargs):
        calls.append(args[2])
        return original(*args, **kwargs)
    monkeypatch.setattr(W, "_reflow", reflow)
    mapping = {}
    first = W._cached_reflow(state, "details", rows, hits, 18, mapping)
    second_mapping = {}
    second = W._cached_reflow(state, "details", rows, hits, 18, second_mapping)
    assert first == second and mapping == second_mapping
    assert calls == [18]
    target["path"] = "/exact/new.log"
    updated = W._cached_reflow(state, "details", rows, hits, 18, {})
    assert calls == [18, 18]
    assert all(value[0][1]["path"] == "/exact/new.log" for _, _, value in updated[1])
    rows[0][0] = (" NEW PUBLISHED REPORT", "yellow")
    updated = W._cached_reflow(state, "details", rows, hits, 18, {})
    assert calls == [18, 18, 18]
    assert "NEW PUBLISHED" in L.row_text(updated[0][0])
    assert updated[0][0][0][1] == "yellow"
    W._cached_reflow(state, "details", rows, hits, 12, {})
    assert calls[-1] == 12 and len(calls) == 4


def test_reflow_cache_keeps_only_the_current_two_panel_sources():
    state = W.LayoutState()
    for width in range(8, 80):
        for panel in W.PANELS:
            W._cached_reflow(state, panel, [[(f"published {width}", "")]], [], width, {})
    assert len(state.reflow_cache) == 2
    assert all(entry["width"] == 79 for entry in state.reflow_cache.values())
    assert "reflow_cache" not in W.save(SimpleNamespace(layout_state=state))
