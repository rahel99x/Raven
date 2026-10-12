"""A complete native GPU dashboard must fit its bounded raster working set."""
import math

import pytest

from raven import chart_interaction as C, charts, clock, layout as L, metric_live as M
from raven.config import Config
from raven.controller import App
from raven.metric_raster import MAX_CELLS, MAX_ENTRIES, MAX_POINTS
from raven.model import Job, Store
from raven.views import Views


@pytest.mark.parametrize("tab", ["jobs", "analytics"])
@pytest.mark.parametrize("ascii_", [False, True])
def test_complete_native_gpu_and_trace_cards_reuse_unchanged_rasters(monkeypatch, tab, ascii_):
    cfg = Config({"animations": False, "log_lines": 0, "startup_animation": False,
                  "workspace": {"density": "compact"}})
    store = Store(persist=False)
    store.apply_jobs([Job("7", "four-device-training", "gpu", "RUNNING", cpus=16,
                          mem_req="32G", gpus=4)])
    for index in range(1000):
        store.record("7", {"k": "live", "t": 1000. + index,
                           "cpu": .5 + .4 * math.sin(index / 20),
                           "rss": (2 + math.sin(index / 30)) * (1 << 30)})
        store.record("7", {"k": "gpu", "t": 1000. + index,
                           "gpu": {str(device): [50 + 40 * math.sin(index / 20 + device)]
                                   for device in range(4)}})
    store.trace["7"] = [{"index": device, "t": 1000. + index * 60,
                         "util": 50 + 40 * math.sin(index / 20 + device)}
                        for device in range(4) for index in range(1000)]
    monkeypatch.setattr(clock, "now", lambda: 61000.)
    app = App(store, None, None, cfg, "test", interactive=False)
    app.tab, app.selected_id, app.analytics_job = tab, "7", "7"
    app.analytics_view = "job"
    app.job_panel_state.update(mode="analytics", analytics_view="job")
    views = Views(L.Glyphs(ascii_), cfg)
    app.views_ref = views
    calls = []
    for name in ("braille_chart", "vbar_chart"):
        original = getattr(charts, name)

        def counted(*args, _original=original, _name=name, **kwargs):
            calls.append((_name, kwargs.get("title", "")))
            return _original(*args, **kwargs)

        monkeypatch.setattr(charts, name, counted)
    try:
        height = 60 if tab == "jobs" else 400
        first, _ = views.compose(store.snapshot(), app, 180, height)
        # CPU, memory, four GPU rate/mean pairs, and four trace rate/mean
        # pairs are distinct measured sources: eighteen native metric cards.
        assert len([value for value in calls if value[0] == "braille_chart"]) == 18
        expected = {"cpu-rate", "memory-request"}
        expected |= {f"gpu:{device}:{kind}" for device in range(4) for kind in ("rate", "busy-mean")}
        expected |= {f"gpu-trace:{device}:{kind}" for device in range(4) for kind in ("rate", "busy-mean")}
        entries = M.initialize(app)["entries"]
        assert {identity[2] for identity in entries if identity[1] == "7"} == expected
        assert any("GPU trace 3" in title for _, title in calls)
        assert any("GPU 3" in title for _, title in calls)
        assert C.initialize(app)["plots"] and M.initialize(app)["records"]
        cache = views._metric_rasters
        working_set = 36 if tab == "jobs" and not ascii_ else 18
        assert len(cache.entries) == working_set
        assert len(cache.entries) <= MAX_ENTRIES
        assert cache.points <= MAX_POINTS and cache.cells <= MAX_CELLS

        calls.clear()
        second, _ = views.compose(store.snapshot(), app, 180, height)
        assert not calls, "An unchanged complete GPU dashboard evicted its own chart working set"
        assert first == second
        assert C.initialize(app)["plots"] and M.initialize(app)["records"]
        assert len(cache.entries) == working_set

        # Correct a historical reading in place. Rate and observed mean must
        # invalidate while unrelated devices and CPU/memory retain their data.
        store.series["7"][101]["gpu"]["0"][0] = 99.
        views.compose(store.snapshot(), app, 180, height)
        changed_titles = {title for _, title in calls if title}
        assert changed_titles
        assert all("GPU 0" in title for title in changed_titles)
    finally:
        if app.research:
            app.research.close()
