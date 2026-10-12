"""The rich terminal pages keep accurate instruments, responsive layouts and cursor targets."""
import pytest

from raven import charts, layout as L
from raven.actions import Actions
from raven.config import Config
from raven.controller import App
from raven.model import Job, Live, Store
from raven.sampler import Sampler
from raven.slurm import FakeBackend, Slurm
from raven.views import TABS, Views


@pytest.fixture
def visual_dashboard():
    cfg = Config()
    store = Store(persist=False)
    slurm = Slurm(FakeBackend("alex"), "alex")
    sampler = Sampler(slurm, store, cfg["intervals"], cfg["gpu_types"], account="lab_01")
    actions = Actions(slurm, store)
    app = App(store, sampler, actions, cfg, "alex")
    views = Views(L.Glyphs(False), cfg)
    app.views_ref = views
    sampler.round(wait=True)
    yield store, app, views, actions
    sampler.shutdown()


@pytest.mark.parametrize("width,height", [(40, 12), (104, 32), (160, 52)])
def test_rich_pages_fit_and_preserve_mouse_selection(visual_dashboard, width, height):
    store, app, views, actions = visual_dashboard
    for tab, _ in TABS[:9]:
        app.tab = tab
        rows, hits = views.compose(store.snapshot(), app, width, height, actions)
        assert len(rows) == height
        assert all(L.vlen(L.row_text(row)) <= width for row in rows)
        for y, kind, key in hits:
            assert 0 <= y < height - 1
            if kind == "sort_header":
                _, _, left, right = key
                assert 0 <= left < right <= width
                assert L.row_text(rows[y])[left:right].strip()
            elif kind in ("job_panel_tab", "job_panel_file", "job_panel_view", "job_panel_action"):
                identifier, left, right = key
                assert 0 <= left < right <= width
                assert L.row_text(rows[y])[left:right].strip()
                if kind == "job_panel_tab":
                    from raven.job_panels import TABS as panel_tabs
                    assert dict(panel_tabs)[identifier].casefold() in L.row_text(rows[y])[left:right].casefold()
            elif kind == "control":
                assert key["id"] and key["action"]
                assert 0 <= key["left"] < key["right"] <= width
                assert L.row_text(rows[y])[key["left"]:key["right"]].strip()
            else:
                assert key in L.row_text(rows[y])
        assert any(hit[3] == tab for hit in app.tab_hits)
        data_hits = [hit for hit in hits if hit[1] != "sort_header"]
        if data_hits:
            y, kind, key = data_hits[-1]
            app.click(y, 2, hits)
            if kind in {"job", "group", "dep"}:
                views.compose(store.snapshot(), app, width, height, actions)
                assert app.selected_id == key
            elif kind == "node_row":
                assert app.mode == "table_tools"
                assert app.table_tools_state["node"] == key
                app.handle("esc")
                assert app.mode == "main"
            elif kind == "partition_row":
                assert app.tab == "jobs"
                assert app.table_state["facets"]["jobs"]["partition"] == key


def test_resource_cards_keep_unknown_and_real_zero_distinct(visual_dashboard):
    store, app, views, _ = visual_dashboard
    job = Job("1", "test", "main", "RUNNING", cpus=8, mem_req="8G")
    text = L.to_text(views.resource_cards(store.snapshot(), job, 120), 120)
    assert "waiting for a sample" in text and "memory sample unknown" in text
    assert "wall-time limit unknown" in text
    job.elapsed, job.limit = "0:00", "1:00:00"
    store.live["1"] = Live(rate=0, avg=0, rss=0)
    text = L.to_text(views.resource_cards(store.snapshot(), job, 120), 120)
    assert "0% now" in text and "0.0% of requested memory" in text
    assert "1:00:00 remaining" in text
    assert "#" not in text


def test_analytics_uses_each_resources_actual_timestamps(visual_dashboard, monkeypatch):
    store, app, views, _ = visual_dashboard
    job = Job("1", "test", "main", "RUNNING", cpus=8, mem_req="8G")
    store.jobs = [job]
    store.record("1", {"k": "live", "t": 100, "cpu": 0.0, "rss": 0})
    store.record("1", {"k": "gpu", "t": 110, "gpu": {"node:0": [20, 10, 100]}})
    store.record("1", {"k": "live", "t": 130, "cpu": 0.5, "rss": 1024})
    store.record("1", {"k": "gpu", "t": 170, "gpu": {"node:0": [80, 10, 100]}})
    store.trace["1"] = [{"index": 0, "t": 10, "util": 5}, {"index": 0, "t": 70, "util": 15}]
    calls = []
    monkeypatch.setattr(charts, "braille_chart", lambda g, values, width, height, **kw: calls.append((values, kw)) or [])
    monkeypatch.setattr(charts, "vbar_chart", lambda *a, **kw: [])
    views.analytics_job(store.snapshot(), app, 160, 38)
    assert calls[0][1]["times"] == (100, 130)
    assert calls[0][1]["sample_times"] == [100, 130]
    assert calls[1][1]["sample_times"] == [100, 130]
    assert calls[2][1]["times"] == (110, 170)
    assert calls[2][1]["sample_times"] == [110, 170]
    # The sampled busy mean shares this device's actual clock. Both job trace
    # curves retain their independent span and one-minute source cadence.
    assert calls[3][0] == [20, 50]
    assert calls[3][1]["times"] == (110, 170)
    assert calls[3][1]["sample_times"] == [110, 170]
    assert calls[4][1]["times"] == (10, 70)
    assert calls[4][1]["sample_interval"] == 60
    assert calls[5][0] == [5, 10]
    assert calls[5][1]["times"] == (10, 70)
    assert calls[5][1]["sample_interval"] == 60


def test_large_analytics_has_observed_heatmap_and_precision_curves(visual_dashboard):
    store, app, views, _ = visual_dashboard
    job = Job("1", "test", "main", "RUNNING", cpus=8, mem_req="8G")
    store.jobs = [job]
    for i in range(20):
        store.record("1", {"k": "live", "t": 100 + i * 30, "cpu": i / 20, "rss": i * 1024 ** 2})
    rows = views.analytics_job(store.snapshot(), app, 160, 50)
    text = L.to_text(rows, 160)
    assert "telemetry heatmap" in text and "newest at right" in text
    assert "██" in text
    assert any(char in charts.BRAILLE[1:] for char in text)
    assert "#" not in text
    assert len(rows) <= 50


def test_comparison_uses_elapsed_time_and_preserves_sample_positions(visual_dashboard, monkeypatch):
    store, app, views, _ = visual_dashboard
    store.jobs = [Job("1", "test", "main", "RUNNING", cpus=8)]
    store.record("1", {"k": "live", "t": 100, "cpu": 0.0, "rss": 0})
    store.record("1", {"k": "live", "t": 130, "cpu": 0.5, "rss": 1024})
    app.compare_ids = ["1"]
    calls = []
    monkeypatch.setattr(charts, "braille_chart", lambda g, values, width, height, **kw: calls.append((values, kw)) or [])
    views.analytics_compare(store.snapshot(), app, 120, 40)
    assert calls[0][0] == [0.0, 50.0]
    assert calls[0][1]["times"] == (0, 30)
    assert calls[0][1]["sample_times"] == [0, 30]
    assert calls[0][1]["elapsed"] is True


def test_dependency_mix_retains_selected_job_hit(visual_dashboard):
    store, app, views, actions = visual_dashboard
    store.jobs = [Job(str(i), f"stage{i}", "main", "PENDING", dependency=f"afterok:{i - 1}" if i else "") for i in range(25)]
    app.tab, app.cursor["deps"] = "deps", 24
    rows, hits = views.compose(store.snapshot(), app, 160, 28, actions)
    selected_row = next(y for y, _, key in hits if key == "24")
    assert "stage24" in L.row_text(rows[selected_row])
    rect = app.history_browser_content_rect
    assert rect.x == 0  # The default history dock is on the right.
    selected_content = L.clip_row(rows[selected_row], rect.width)
    assert all("sel" in style.split("+") for text, style in selected_content if text.strip())


def test_many_group_users_keep_selected_table_row_visible(visual_dashboard):
    store, app, views, actions = visual_dashboard
    store.group = [Job(str(i), f"job{i}", "main", "RUNNING", user=f"user{i:02}", cpus=i + 1) for i in range(30)]
    app.tab, app.cursor["group"] = "group", 29
    rows, hits = views.compose(store.snapshot(), app, 120, 24, actions)
    selected_row = next(y for y, _, key in hits if key == "29")
    assert "job29" in L.row_text(rows[selected_row])
    assert "30 users" in L.to_text(rows, 120)


def test_reader_mode_stays_plain_after_rich_rendering(visual_dashboard):
    store, app, views, actions = visual_dashboard
    views.compose(store.snapshot(), app, 160, 40, actions)
    app.set_theme("reader")
    app.message = ""
    for tab, _ in TABS[:9]:
        app.tab = tab
        assert L.to_text(views.compose(store.snapshot(), app, 120, 30, actions)[0], 120).isascii()
