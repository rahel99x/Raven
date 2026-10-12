"""Native series scrolling exposes every card without rasterizing hidden cards."""
import copy
from dataclasses import replace
from types import SimpleNamespace

import pytest

from raven import analytics_document as D, chart_interaction as C, layout as L, metric_live as M
from raven.config import Config
from raven.controller import App
from raven.model import Job, Store
from raven.views import Views


@pytest.fixture
def native():
    cfg = Config()
    store = Store(persist=False)
    store.jobs = [Job("900", "many-gpus", "gpu", "RUNNING", cpus=8, gpus=4,
                      submit="2026-10-08T00:00:00", start="2026-10-08T00:00:01"),
                  Job("901", "other-job", "gpu", "RUNNING", gpus=1)]
    for stamp in range(6):
        store.record("900", {"k": "live", "t": stamp, "cpu": .1 + stamp / 10,
                             "rss": 1024 ** 3})
        store.record("900", {"k": "gpu", "t": stamp,
                             "gpu": {f"node:{index}": [stamp * 10, 1, 2] for index in range(4)}})
    store.trace["900"] = [{"index": index, "t": stamp, "util": stamp * 10}
                          for index in range(4) for stamp in range(6)]
    app = App(store, None, None, cfg, "test", interactive=False)
    app.tab, app.analytics_job, app.selected_id = "analytics", "900", "900"
    app.layout_state.density = "compact"
    app.run_command("smoothscroll off")
    views = Views(L.Glyphs(False), cfg)
    app.views_ref = views
    app.run_command("history-dock off")
    yield app, views, store
    if app.research:
        app.research.close()


def draw(native, width=120, height=36):
    app, views, store = native
    rows, _ = views.compose(store.snapshot(), app, width, height)
    return rows, C.initialize(app)["plots"]


def expected():
    return {"cpu-rate", "resident-memory"} | {
        f"{prefix}:{device}:{kind}"
        for prefix, devices in (("gpu", [f"node:{index}" for index in range(4)]),
                                ("gpu-trace", range(4)))
        for device in devices for kind in ("rate", "busy-mean")
    }


@pytest.mark.parametrize("width", [40, 80, 120, 180])
@pytest.mark.parametrize("ascii_", [False, True])
def test_page_scrolling_reaches_every_real_gpu_and_trace_metric_without_changing_job(native, width, ascii_):
    app, views, _ = native
    views.set_ascii(ascii_)
    seen = set()
    rows, plots = draw(native, width)
    frame = D.initialize(app)["frame"]
    assert frame["count"] > frame["page"] and frame["page"] > 0
    # Sticky navigation and exact source job remain in the same rows.
    source_origin = app.body_origin
    source = "\n".join(L.row_text(row) for row in rows[source_origin:frame["rect"][0]])
    assert "900" in source and "job series" in source
    iterations = 0
    while True:
        frame = D.initialize(app)["frame"]
        top, left, bottom, right = frame["rect"]
        for plot in plots:
            assert plot.key[1] == "900"
            assert top <= plot.visible.top < plot.visible.bottom <= bottom
            assert left <= plot.visible.left < plot.visible.right <= right
            seen.add(plot.key[2])
        assert app.analytics_job == "900"
        assert app.body_origin == source_origin
        assert "\n".join(L.row_text(row) for row in rows[source_origin:top]) == source
        before = D.initialize(app)["top"]
        app.handle("pgdn")
        if D.initialize(app)["top"] == before:
            break
        rows, plots = draw(native, width)
        iterations += 1
        assert iterations < 200
    assert seen == expected()
    assert D.initialize(app)["top"] == frame["count"] - frame["page"]
    app.handle("pgup")
    draw(native, width)
    assert D.initialize(app)["top"] < frame["count"] - frame["page"]
    app.run_command("series-scroll home")
    draw(native, width)
    assert D.initialize(app)["top"] == 0


def test_visible_band_raster_budget_and_no_phantom_live_controls(native, monkeypatch):
    app, views, _ = native
    calls = []
    curve = views.metric_curve
    def observe(*args, **kwargs):
        calls.append(args[4][2])
        return curve(*args, **kwargs)
    monkeypatch.setattr(views, "metric_curve", observe)
    draw(native)
    assert 1 <= len(calls) <= 3
    assert all(name in ("cpu-rate", "resident-memory") for name in calls)
    assert len(C.initialize(app)["plots"]) <= 3
    assert all(record.key[2] in ("cpu-rate", "resident-memory") for record in M.initialize(app)["records"])
    calls.clear()
    app.run_command("series-scroll end")
    _, plots = draw(native)
    assert 1 <= len(calls) <= 3
    assert all(name.startswith("gpu-trace:3:") for name in calls)
    assert all(plot.key[2].startswith("gpu-trace:3:") for plot in plots)
    assert all(record.key[2].startswith("gpu-trace:3:") for record in M.initialize(app)["records"])


@pytest.mark.parametrize("width", [40, 120, 180])
def test_selected_time_notes_are_counted_for_hidden_bands_and_all_metrics_remain_reachable(native, width):
    app, _, _ = native
    seen = {}
    _, plots = draw(native, width)
    original_count = D.initialize(app)["frame"]["count"]
    while True:
        seen.update({item.key[2]: item for item in plots})
        before = D.initialize(app)["top"]
        app.handle("pgdn")
        if D.initialize(app)["top"] == before:
            break
        _, plots = draw(native, width)
    assert set(seen) == expected()
    for item in seen.values():
        C._apply(app, replace(item), {"x": (1., 4.), "y": item.y_bounds, "fit_y": True})
    app.run_command("series-scroll home")
    _, plots = draw(native, width)
    new_count = D.initialize(app)["frame"]["count"]
    assert new_count == original_count + (9 if width >= 140 else 18)
    selected_seen = set()
    while True:
        selected_seen.update(item.key[2] for item in plots)
        assert all(item.x_bounds == (1., 4.) for item in plots)
        before = D.initialize(app)["top"]
        app.handle("pgdn")
        if D.initialize(app)["top"] == before:
            break
        _, plots = draw(native, width)
    assert selected_seen == expected()
    frame = D.initialize(app)["frame"]
    assert D.initialize(app)["top"] == frame["count"] - frame["page"]


def test_native_ordinary_arrow_home_end_keys_keep_job_navigation(native):
    app, _, _ = native
    draw(native)
    app.handle("down")
    assert app.analytics_job == "901"
    draw(native)
    app.handle("up")
    assert app.analytics_job == "900"
    app.handle("end")
    assert app.analytics_job == "901"
    app.handle("home")
    assert app.analytics_job == "900"


@pytest.mark.parametrize("dock", ["left", "right", "top", "bottom"])
def test_history_dock_transforms_document_wheel_and_plot_bounds_once(native, dock):
    app, _, _ = native
    app.run_command("history-dock " + dock)
    rows, plots = draw(native, 180, 48)
    frame = D.initialize(app)["frame"]
    top, left, bottom, right = frame["rect"]
    assert bottom > top and right > left
    assert plots
    for plot in plots:
        assert top <= plot.visible.top < plot.visible.bottom <= bottom
        assert left <= plot.visible.left < plot.visible.right <= right
    assert not D.handle_mouse(app, top - 1, left, "wheel-down")
    assert D.handle_mouse(app, top, left, "wheel-down")
    assert D.initialize(app)["top"] == 3
    assert app.analytics_job == "900"


@pytest.mark.parametrize("mutate", [lambda app: setattr(app, "analytics_job", "901"),
                                     lambda app: setattr(app, "tab", "research"),
                                     lambda app: setattr(app, "mode", "confirm"),
                                     lambda app: setattr(app, "width", 80),
                                     lambda app: setattr(app, "height", 24)])
def test_stale_document_frame_rejects_wheel_and_page_keys(native, mutate):
    app, _, _ = native
    draw(native)
    y, x, _, _ = D.initialize(app)["frame"]["rect"]
    mutate(app)
    assert not D.handle_mouse(app, y, x, "wheel-down")
    assert not D.handle_key(app, "pgdn")
    assert D.initialize(app)["top"] == 0


def test_new_job_or_attempt_resets_offset_and_missing_samples_clear_geometry(native):
    app, _, store = native
    draw(native)
    app.run_command("series-scroll end")
    draw(native)
    assert D.initialize(app)["top"] > 0
    store.jobs[0].start = "2026-10-08T01:00:01"
    draw(native)
    assert D.initialize(app)["top"] == 0
    app.analytics_job = "901"
    draw(native)
    assert D.initialize(app)["frame"] is None
    assert not D.handle_key(app, "pgdn")


def test_inline_and_custom_workspace_defer_to_outer_document_without_mutating_native_state(native):
    app, views, store = native
    draw(native)
    app.run_command("series-scroll end")
    draw(native)
    before = copy.deepcopy(D.initialize(app))
    proxy = copy.copy(app)
    proxy.analytics_document_mode = True
    rows, _ = views.analytics_tab(store.snapshot(), proxy, 80, 512)
    assert D.initialize(app) == before
    text = "\n".join(L.row_text(row) for row in rows)
    assert "GPU trace 3" in text
    app.layout_state.density = "comfortable"
    draw(native)
    assert D.initialize(app)["frame"] is None
    assert not D.handle_key(app, "pgdn")


@pytest.mark.parametrize("args", [["series-scroll"], ["series-scroll", "wrong"],
                                  ["series-scroll", "end", "extra"]])
def test_series_scroll_rejects_malformed_commands(native, args):
    app, _, _ = native
    draw(native)
    assert D.run_command(app, args)
    assert "series-scroll <" in app.message
    assert D.initialize(app)["top"] == 0


@pytest.mark.parametrize("changed_job", [False, True])
def test_document_command_through_painted_palette_uses_last_matching_main_viewport(native, changed_job):
    app, _, _ = native
    draw(native)
    app.handle(":")
    for char in "series-scroll end":
        app.handle("space" if char == " " else char)
    if changed_job:
        app.analytics_job = "901"
    draw(native)
    assert app.mode == "palette"
    assert D.initialize(app)["frame"] is not None
    assert not D.handle_key(app, "pgdn")
    app.handle("enter")
    assert app.mode == "main"
    if changed_job:
        assert D.initialize(app)["top"] == 0
        assert "Draw native Analytics Job Series" in app.message
        assert app.analytics_job == "901"
    else:
        draw(native)
        frame = D.initialize(app)["frame"]
        assert D.initialize(app)["top"] == frame["painted"] == frame["count"] - frame["page"]


def test_doc_scroll_and_passive_queries_never_read_metric_files_or_schedule_work(native, monkeypatch):
    app, _, _ = native
    draw(native)
    def forbidden(*args, **kwargs):
        raise AssertionError("document scroll read or scheduled source IO")
    monkeypatch.setattr(app.store, "series_of", forbidden)
    app.research = SimpleNamespace(request=forbidden)
    app.sampler = SimpleNamespace(select=forbidden)
    frame = D.initialize(app)["frame"]
    y, x, _, _ = frame["rect"]
    assert D.handle_mouse(app, y, x, "wheel-down")
    assert D.handle_key(app, "pgdn")
    assert D.run_command(app, ["series-scroll", "home"])
    assert app.analytics_job == "900"
    # Avoid the fixture closing this deliberately minimal sentinel.
    app.research = None


def test_smooth_wheel_rasterizes_painted_cards_and_keyboard_commands_snap(native, monkeypatch):
    from raven import scrolling as S
    app, _, _ = native
    now = [100.0]
    monkeypatch.setattr(S.time, "monotonic", lambda: now[0])
    app.interactive = True
    app.run_command("smoothscroll on")
    draw(native)
    y, x, _, _ = D.initialize(app)["frame"]["rect"]
    for _ in range(20):
        assert D.handle_mouse(app, y, x, "wheel-down")
    _, plots = draw(native)
    frame = D.initialize(app)["frame"]
    assert frame["painted"] < D.initialize(app)["top"]
    assert S.active(app)
    assert {plot.key[2] for plot in plots} <= {"cpu-rate", "resident-memory"}
    for _ in range(20):
        now[0] += 1 / 60
        _, plots = draw(native)
        frame = D.initialize(app)["frame"]
        top, left, bottom, right = frame["rect"]
        assert all(top <= plot.visible.top < plot.visible.bottom <= bottom for plot in plots)
        if not S.active(app):
            break
    assert frame["painted"] == D.initialize(app)["top"]
    assert D.handle_mouse(app, y, x, "wheel-down")
    D.run_command(app, ["series-scroll", "end"])
    draw(native)
    frame = D.initialize(app)["frame"]
    assert frame["painted"] == D.initialize(app)["top"] == frame["count"] - frame["page"]
    assert not S.active(app)
