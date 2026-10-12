"""Terminal presentation stays useful at SSH widths, with plain ASCII and a mouse."""
from types import SimpleNamespace

import pytest

from raven import charts, clock, layout as L
from raven.actions import Actions
from raven.config import Config
from raven.controller import App
from raven.model import Health, Job, Live, Store
from raven.sampler import Sampler
from raven.slurm import FakeBackend, Slurm
from raven.views import ANALYTICS_VIEWS, TABS, Views


@pytest.fixture
def dashboard():
    cfg = Config()
    store = Store(persist=False)
    slurm = Slurm(FakeBackend("alex"), "alex")
    sampler = Sampler(slurm, store, cfg["intervals"], cfg["gpu_types"], account="lab_01")
    actions = Actions(slurm, store)
    app = App(store, sampler, actions, cfg, "alex", ascii_=True)
    views = Views(L.Glyphs(True), cfg)
    app.views_ref = views
    sampler.round(wait=True)
    yield store, app, views, actions
    sampler.shutdown()


@pytest.mark.parametrize("width,height", [(40, 12), (80, 24), (160, 44)])
def test_all_pages_keep_active_navigation_metrics_and_ascii(dashboard, width, height):
    store, app, views, actions = dashboard
    for tab, title in TABS:
        if tab not in {"jobs", "cluster", "history", "analytics", "nodes", "group", "deps", "log", "sources"}:
            continue  # Other tests may register a plugin tab in the shared process.
        app.tab = tab
        rows, hits = views.compose(store.snapshot(), app, width, height, actions)
        assert len(rows) == height
        assert all(L.vlen(L.row_text(row)) <= width for row in rows)
        assert L.to_text(rows, width).isascii()
        assert L.row_text(rows[1]).strip(), tab
        assert "help" in L.row_text(rows[-1]) and "quit" in L.row_text(rows[-1])
        active = next(h for h in app.tab_hits if h[3] == tab)
        assert title in L.row_text(rows[active[0]])[active[1]:active[2]]
        assert all(0 <= y < height - 1 for y, _, _ in hits)
        # Visible mouse targets lead to the tab printed at that exact position.
        for y, left, right, key in app.tab_hits:
            assert 0 <= left < right <= width
            # Each target is tested from the page that painted these labels;
            # navigating away invalidates that page's old control registry.
            app.tab = tab
            _, hits = views.compose(store.snapshot(), app, width, height, actions)
            app.click(y, left, hits)
            assert app.tab == key


def test_analytics_node_views_overlays_and_reader_use_ascii(dashboard):
    store, app, views, actions = dashboard
    snap = store.snapshot()
    app.tab = "analytics"
    for view, _ in ANALYTICS_VIEWS:
        app.analytics_view = view
        rows, _ = views.compose(snap, app, 120, 40, actions)
        assert L.to_text(rows, 120).isascii(), view
    app.tab = "nodes"
    for view in ("mine", "map"):
        app.nodes_view = view
        assert L.to_text(views.compose(snap, app, 120, 40, actions)[0], 120).isascii()
    for mode in ("help", "details", "confirm"):
        app.mode = mode
        app.detail_id = snap["jobs"][0].id
        app.confirm = {"action": "cancel", "jobs": snap["jobs"][:1]}
        rows = [row for _, _, row in views.overlay(snap, app, 120, 40)]
        assert L.to_text(rows, 120).isascii(), mode
    app.mode = "main"
    app.set_theme("reader")
    app.message = ""
    rows, _ = views.compose(snap, app, 80, 24, actions)
    assert L.to_text(rows, 80).isascii()


def test_sources_scroll_to_selected_row_and_use_replay_clock(dashboard, monkeypatch):
    store, app, views, actions = dashboard
    store.health = {f"source{i:02}": Health(f"source{i:02}", last_ok=900) for i in range(50)}
    monkeypatch.setattr(clock, "now", lambda: 1000)
    app.tab, app.cursor["sources"] = "sources", 49
    rows, hits = views.compose(store.snapshot(), app, 100, 12, actions)
    selected = next(y for y, kind, name in hits if kind == "source" and name == "source49")
    assert "source49" in L.row_text(rows[selected])
    assert "1m 40s ago" in L.row_text(rows[selected])
    assert all("sel" in style.split("+") for _, style in rows[selected])
    assert len([hit for hit in hits if hit[1] == "source"]) <= 6
    assert all(0 <= key[2] < key[3] <= 100 for _, kind, key in hits if kind == "sort_header")


def test_dependency_cursor_remains_visible_when_chain_is_taller_than_screen(dashboard):
    store, app, views, actions = dashboard
    store.jobs = [Job(str(i), f"stage{i}", "main", "PENDING", dependency=f"afterok:{i - 1}" if i else "") for i in range(20)]
    app.tab, app.cursor["deps"] = "deps", 19
    rows, hits = views.compose(store.snapshot(), app, 120, 12, actions)
    assert app.selected_id == "19"
    selected = next(y for y, kind, jid in hits if kind == "dep" and jid == "19")
    assert "stage19" in L.row_text(rows[selected])
    rect = app.history_browser_content_rect
    assert rect.x == 0  # The default history dock is on the right.
    selected_content = L.clip_row(rows[selected], rect.width)
    assert all("sel" in style.split("+") for text, style in selected_content if text.strip())


def test_replay_tab_mouse_coordinates_follow_inserted_scrub_bar(dashboard):
    store, app, views, actions = dashboard
    app.replay = SimpleNamespace(clock=SimpleNamespace(now=lambda: 1000, t0=900, t1=1100, frac=0.5, speed=1, paused=True, at_end=False))
    rows, hits = views.compose(store.snapshot(), app, 160, 30, actions)
    assert "replay" in L.row_text(rows[3])
    assert all(y == 4 for y, _, _, _ in app.tab_hits)
    target = next(h for h in app.tab_hits if h[3] == "sources")
    app.click(target[0], target[1], hits)
    assert app.tab == "sources"


def test_log_severity_and_wide_character_wrap_make_progress(dashboard, tmp_path):
    store, app, views, _ = dashboard
    job = store.jobs[0]
    path = tmp_path / f"job-{job.id}.out"
    path.write_text("ERROR allocation failed\nWARNING retrying\nCompleted successfully\n", encoding="utf-8")
    store.details[job.id] = {"StdOut": str(path)}
    app.tab, app.log_job = "log", job.id
    rows, _ = views.log_tab(store.snapshot(), app, 100, 12)
    styles = {L.row_text(row).strip(): row[-1][1] for row in rows[2:]}
    assert styles["ERROR allocation failed"] == "red"
    assert styles["WARNING retrying"] == "yellow"
    assert styles["Completed successfully"] == "green"
    path.write_text("日本語\n", encoding="utf-8")
    app.logs.buffers.clear()
    app.logs.wrap = True
    rows, _ = views.log_tab(store.snapshot(), app, 3, 10)
    assert [L.row_text(row) for row in rows[2:]] == [" ?", " ?", " ?"]


def test_chart_stats_describe_actual_samples_before_resampling():
    samples = [0] * 999 + [100]
    rows = charts.vbar_chart(L.Glyphs(True), samples, 100, 5, unit="%", title="CPU")
    assert "last 100%" in L.row_text(rows[0])
    assert "max 100%" in L.row_text(rows[0])
    assert "min 0%" in L.row_text(rows[0])


@pytest.mark.parametrize("width", [0, 1, 3, 12, 24, 40])
def test_chart_and_overlay_primitives_fit_small_terminals(width):
    g = L.Glyphs(True)
    rows = charts.vbar_chart(g, [0, None, 50, 100], width, 4, title="CPU samples")
    rows += charts.hbar_rows(g, [("very long partition", 2, "cyan")], width)
    rows += charts.gantt(g, [dict(id="1", name="job", state="RUNNING", start=2)], 0, 10, width)
    rows += [L.rule(g, width, "日本語 samples")]
    assert all(L.vlen(L.row_text(row)) <= width for row in rows)
    for y, x, row in L.box(g, [[("日本語 details", "")]], width, 6, "日本語 overlay"):
        assert 0 <= y < 6 and x + L.vlen(L.row_text(row)) <= width


def test_demo_label_is_visible_before_metrics(dashboard):
    store, app, views, actions = dashboard
    app.demo = True
    rows, _ = views.compose(store.snapshot(), app, 80, 24, actions)
    assert "File" in L.row_text(rows[0]) and "Updates" in L.row_text(rows[0])
    assert "[DEMO]" in L.row_text(rows[1])


def test_selected_panel_distinguishes_unknown_metrics_from_real_zero(dashboard):
    store, app, views, _ = dashboard
    job = Job("1", "new-job", "main", "RUNNING", elapsed="", limit="")
    store.jobs = [job]
    store.live = {"1": Live(cpu_time=12, avg=None, rate=None)}
    rows = views.selected_panel(store.snapshot(), job, 120, 0, app)
    time_row = next(L.row_text(row) for row in rows if L.row_text(row).startswith("   time"))
    cpu_row = next(L.row_text(row) for row in rows if L.row_text(row).startswith("   cpu"))
    assert "n/a" in time_row and "0%" not in time_row
    assert "n/a" in cpu_row and "0%" not in cpu_row
    job.elapsed, job.limit = "0:00", "1:00:00"
    store.live["1"] = Live(cpu_time=0, avg=0, rate=0)
    rows = views.selected_panel(store.snapshot(), job, 120, 0, app)
    time_row = next(L.row_text(row) for row in rows if L.row_text(row).startswith("   time"))
    cpu_row = next(L.row_text(row) for row in rows if L.row_text(row).startswith("   cpu"))
    assert "0%" in time_row and "n/a" not in time_row.split(" of ")[0]
    assert "0%" in cpu_row and "n/a" not in cpu_row
