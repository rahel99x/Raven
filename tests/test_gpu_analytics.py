"""GPU rate and efficiency proxies use exact observed devices, never allocations."""
import copy
import math
from types import SimpleNamespace

import pytest

from raven import chart_interaction as C, charts, job_panels as J, layout as L, palette
from raven.config import Config
from raven.controller import App
from raven.model import Finished, GpuSample, Health, Job, Node, Store
from raven.views import Views


@pytest.fixture
def dashboard():
    cfg = Config()
    store = Store(persist=False)
    store.jobs = [Job("900", "gpu-job", "gpu", "RUNNING", gpus=2,
                      submit="2026-10-08T00:00:00", start="2026-10-08T00:00:01")]
    app = App(store, None, None, cfg, "test", interactive=False)
    app.tab, app.selected_id, app.analytics_job = "analytics", "900", "900"
    views = Views(L.Glyphs(False), cfg)
    app.views_ref = views
    return SimpleNamespace(cfg=cfg, store=store, app=app, views=views)


def observations(dashboard, values, *, job="900", key="nodeA:0", timestamps=None):
    timestamps = range(len(values)) if timestamps is None else timestamps
    for timestamp, value in zip(timestamps, values):
        dashboard.store.record(job, {"k": "gpu", "t": timestamp,
                                     "gpu": {key: [value, 512, 1024]}})


def inspect_curves(monkeypatch, dashboard, *, width=100, ascii_=False):
    calls = []
    def curve(app, values, width, height, identity, **options):
        if not options.get("filled"):
            calls.append({"id": identity[2], "job": identity[1], "key": identity,
                          "values": list(values), "options": options})
        return []
    monkeypatch.setattr(dashboard.views, "metric_curve", curve)
    dashboard.views.g = L.Glyphs(ascii_)
    rows = dashboard.views.analytics_job(dashboard.store.snapshot(), dashboard.app, width, None)
    return {item["id"]: item for item in calls}, L.to_text(rows, width)


@pytest.mark.parametrize("ascii_", [False, True])
def test_gpu_rate_is_reported_utilization_and_busy_mean_counts_valid_retained_samples(monkeypatch, dashboard, ascii_):
    observations(dashboard, [0., 100., None, 25.])
    curves, text = inspect_curves(monkeypatch, dashboard, ascii_=ascii_, width=200)
    assert curves["gpu:nodeA:0:rate"]["values"] == [0., 100., None, 25.]
    assert curves["gpu:nodeA:0:busy-mean"]["values"] == [0., 50., None, pytest.approx(125 / 3)]
    assert "sampled device busy" in text and "retained samples" in text and "gaps are excluded" in text
    assert "observed devices" in text and "FLOP efficiency" in text
    for curve in curves.values():
        assert curve["job"] == "900"
        assert curve["options"]["hi"] == 100 and curve["options"]["unit"] == "%"
        assert curve["options"]["sample_times"] == [0, 1, 2, 3]
    assert "rate / utilization" in curves["gpu:nodeA:0:rate"]["options"]["title"]
    assert "efficiency proxy" in curves["gpu:nodeA:0:busy-mean"]["options"]["title"]


@pytest.mark.parametrize("bad", [None, math.nan, math.inf, -math.inf, -1., 101., True, False, "50", {}, []])
def test_invalid_gpu_readings_stay_missing_and_do_not_enter_busy_mean(monkeypatch, dashboard, bad):
    observations(dashboard, [20., bad, 80.])
    curves, _ = inspect_curves(monkeypatch, dashboard)
    assert curves["gpu:nodeA:0:rate"]["values"] == [20., None, 80.]
    assert curves["gpu:nodeA:0:busy-mean"]["values"] == [20., None, 50.]


@pytest.mark.parametrize("reading", [None, [], {}, 20, "20", ()])
def test_malformed_device_payloads_are_gaps_and_do_not_crash(monkeypatch, dashboard, reading):
    observations(dashboard, [20.])
    dashboard.store.record("900", {"k": "gpu", "t": 1, "gpu": {"nodeA:0": reading}})
    curves, _ = inspect_curves(monkeypatch, dashboard)
    assert curves["gpu:nodeA:0:rate"]["values"] == [20., None]
    assert curves["gpu:nodeA:0:busy-mean"]["values"] == [20., None]


def test_missing_devices_preserve_per_device_scope_and_never_divide_by_requested_gpu_count(monkeypatch, dashboard):
    dashboard.store.jobs[0].gpus = 8
    dashboard.store.record("900", {"k": "gpu", "t": 0, "gpu": {"nodeA:0": [80, 1, 2], "nodeB:0": [20, 1, 2]}})
    dashboard.store.record("900", {"k": "gpu", "t": 1, "gpu": {"nodeA:0": [40, 1, 2]}})
    dashboard.store.record("900", {"k": "gpu", "t": 2, "gpu": {"nodeB:0": [100, 1, 2]}})
    curves, _ = inspect_curves(monkeypatch, dashboard)
    assert curves["gpu:nodeA:0:rate"]["values"] == [80., 40., None]
    assert curves["gpu:nodeA:0:busy-mean"]["values"] == [80., 60., None]
    assert curves["gpu:nodeB:0:rate"]["values"] == [20., None, 100.]
    assert curves["gpu:nodeB:0:busy-mean"]["values"] == [20., None, 60.]
    assert len(curves) == 4


def test_chronological_gpu_mean_is_stable_when_published_records_arrive_out_of_order(monkeypatch, dashboard):
    observations(dashboard, [90., 30., 60.], timestamps=[30., 10., 20.])
    curves, _ = inspect_curves(monkeypatch, dashboard)
    assert curves["gpu:nodeA:0:rate"]["values"] == [30., 60., 90.]
    assert curves["gpu:nodeA:0:busy-mean"]["values"] == [30., 45., 60.]
    assert curves["gpu:nodeA:0:busy-mean"]["options"]["sample_times"] == [10., 20., 30.]


def test_trace_and_live_gpu_indexes_remain_distinct_sources(monkeypatch, dashboard):
    observations(dashboard, [80., 40.])
    dashboard.store.trace["900"] = [{"t": 0., "index": 0, "util": 10., "mem": 1},
                                     {"t": 60., "index": 0, "util": None, "mem": 1},
                                     {"t": 120., "index": 0, "util": 30., "mem": 1}]
    curves, _ = inspect_curves(monkeypatch, dashboard)
    assert curves["gpu-trace:0:rate"]["values"] == [10., None, 30.]
    assert curves["gpu-trace:0:busy-mean"]["values"] == [10., None, 20.]
    assert curves["gpu:nodeA:0:busy-mean"]["values"] == [80., 60.]
    assert curves["gpu-trace:0:rate"]["options"]["sample_interval"] == 60.
    assert len(curves) == 4


@pytest.mark.parametrize("bad", [None, math.nan, math.inf, -1., 101., True, "50", {}])
def test_trace_busy_mean_rejects_invalid_utilization(monkeypatch, dashboard, bad):
    dashboard.store.trace["900"] = [{"t": 0., "index": 0, "util": 10.},
                                     {"t": 60., "index": 0, "util": bad},
                                     {"t": 120., "index": 0, "util": 30.}]
    curves, _ = inspect_curves(monkeypatch, dashboard)
    assert curves["gpu-trace:0:rate"]["values"] == [10., None, 30.]
    assert curves["gpu-trace:0:busy-mean"]["values"] == [10., None, 20.]


@pytest.mark.parametrize("bad", [{}, {"t": True, "index": 0}, {"t": math.nan, "index": 0},
                                   {"t": 1, "index": True}, {"t": 1, "index": -1},
                                   {"t": 1, "index": "0"}, None, []])
def test_malformed_trace_records_do_not_displace_real_device_samples(monkeypatch, dashboard, bad):
    dashboard.store.trace["900"] = [{"t": 0., "index": 0, "util": 10.}, bad,
                                     {"t": 60., "index": 0, "util": 30.}]
    curves, _ = inspect_curves(monkeypatch, dashboard)
    assert curves["gpu-trace:0:rate"]["values"] == [10., 30.]
    assert curves["gpu-trace:0:busy-mean"]["values"] == [10., 20.]


def test_gpu_graph_identity_survives_new_trace_samples_and_ascii_or_theme_change(monkeypatch, dashboard):
    observations(dashboard, [20., 60.])
    dashboard.store.trace["900"] = [{"t": 0., "index": 0, "util": 10.}, {"t": 60., "index": 0, "util": 30.}]
    first, _ = inspect_curves(monkeypatch, dashboard)
    observations(dashboard, [100.], timestamps=[2])
    dashboard.store.trace["900"].append({"t": 120., "index": 0, "util": 80.})
    dashboard.app.theme = "gruvbox-dark"
    second, _ = inspect_curves(monkeypatch, dashboard, ascii_=True)
    assert {name: item["key"] for name, item in first.items()} == {name: item["key"] for name, item in second.items()}
    assert second["gpu:nodeA:0:busy-mean"]["values"] == [20., 40., 60.]
    assert second["gpu-trace:0:busy-mean"]["values"] == [10., 20., 40.]


def test_gpu_allocation_without_telemetry_shows_an_honest_available_data_message(monkeypatch, dashboard):
    dashboard.store.record("900", {"k": "live", "t": 0., "cpu": .5, "rss": 1})
    curves, text = inspect_curves(monkeypatch, dashboard)
    assert all(not name.startswith("gpu") for name in curves)
    assert "GPU telemetry is unavailable" in text and "nvidia-smi trace" in text


def test_cpu_only_job_does_not_show_unneeded_gpu_availability_message(monkeypatch, dashboard):
    dashboard.store.jobs[0].gpus = 0
    dashboard.store.record("900", {"k": "live", "t": 0., "cpu": .5, "rss": 1})
    _, text = inspect_curves(monkeypatch, dashboard)
    assert "GPU telemetry" not in text


def test_gpu_rendering_preserves_published_records_and_requests_no_io(monkeypatch, dashboard):
    observations(dashboard, [20., None, 80.])
    original = copy.deepcopy(list(dashboard.store.series["900"]))
    monkeypatch.setattr(dashboard.store, "_series_path", lambda jid: pytest.fail("GPU graph requested files"))
    dashboard.store._series_loaded.add("900")
    dashboard.app.research = SimpleNamespace(generation=1, request=lambda *a, **k: pytest.fail("GPU graph requested reports"))
    inspect_curves(monkeypatch, dashboard)
    assert list(dashboard.store.series["900"]) == original


def test_finished_job_inline_gpu_graphs_never_show_running_job_telemetry(monkeypatch, dashboard):
    finished = Finished("700", "old-gpu", "COMPLETED", gpus=1,
                        submit="2026-10-07T00:00:00", start="2026-10-07T00:00:01")
    dashboard.store.finished = [finished]
    observations(dashboard, [90., 90.])
    observations(dashboard, [10., 30.], job="700")
    calls = []
    def curve(app, values, width, height, identity, **options):
        if not options.get("filled"):
            calls.append((identity, list(values)))
        return []
    monkeypatch.setattr(dashboard.views, "metric_curve", curve)
    state = J.initialize(dashboard.app)
    state.update(mode="analytics", analytics_view="job")
    C.begin_frame(dashboard.app, 80, 100)
    rows, _ = J._analytics(dashboard.views, dashboard.store.snapshot(), dashboard.app, finished, 80, 100, state)
    assert calls and all(identity[1] == "700" for identity, _ in calls)
    assert next(values for identity, values in calls if identity[2].endswith(":rate")) == [10., 30.]
    assert next(values for identity, values in calls if identity[2].endswith(":busy-mean")) == [10., 20.]
    assert dashboard.app.selected_id == "900"


@pytest.mark.parametrize("ascii_", [False, True])
@pytest.mark.parametrize("width", [0, 3, 12, 40, 80, 160])
def test_gpu_rate_and_efficiency_graphs_fit_small_terminals_and_share_live_theme_tokens(dashboard, ascii_, width):
    observations(dashboard, [0., 25., None, 50., 100.])
    dashboard.views.g = L.Glyphs(ascii_)
    rows, _ = dashboard.views.compose(dashboard.store.snapshot(), dashboard.app, width, 40)
    assert all(L.vlen(L.row_text(row)) <= width for row in rows)
    raster = charts.braille_chart(L.Glyphs(ascii_), [0., 50., 100.], width, 6, hi=100)
    assert all(L.vlen(L.row_text(row)) <= width for row in raster)
    if ascii_:
        assert L.to_text(rows, width).isascii()
        content = dashboard.views.analytics_job(dashboard.store.snapshot(), dashboard.app, width, 100)
        assert L.to_text(content, width).isascii()
    curve_style = next(style for row in raster for _, style in row if style == "chart-1+bold") if width > 10 else None
    if curve_style:
        assert palette.resolve(curve_style, "dark").foreground != palette.resolve(curve_style, "light").foreground


@pytest.mark.parametrize("gap", ["missing", "outage"])
def test_rate_and_busy_mean_curves_keep_missing_or_unobserved_intervals_empty(monkeypatch, dashboard, gap):
    values = [20., None, 80.] if gap == "missing" else [20., 80.]
    timestamps = [0., 60., 120.] if gap == "missing" else [0., 1800.]
    observations(dashboard, values, timestamps=timestamps)
    curves, _ = inspect_curves(monkeypatch, dashboard)
    for item in curves.values():
        options = item["options"]
        metadata = {}
        rows = charts.braille_chart(L.Glyphs(False), item["values"], 50, 6, hi=100,
                                   sample_times=options["sample_times"], times=options["times"],
                                   sample_interval=options["sample_interval"], metadata=metadata)
        top, left, bottom, right = metadata["plot_rect"]
        measured = [L.row_text(row)[left:right] for row in rows[top:bottom]]
        assert any(row[0] in charts.BRAILLE[1:] for row in measured)
        assert any(row[-1] in charts.BRAILLE[1:] for row in measured)
        assert all(not any(char in charts.BRAILLE[1:] for char in row[1:-1]) for row in measured)


def test_maximum_gpu_device_and_trace_fanout_is_bounded(monkeypatch, dashboard):
    for timestamp in (0., 60.):
        dashboard.store.record("900", {"k": "gpu", "t": timestamp,
                                       "gpu": {f"nodeA:{i}": [i, 1, 2] for i in range(100)}})
    dashboard.store.trace["900"] = [{"t": timestamp, "index": i, "util": i}
                                     for timestamp in (0., 60.) for i in range(100)]
    curves, _ = inspect_curves(monkeypatch, dashboard)
    assert len(curves) == 16
    assert sum(name.startswith("gpu:") for name in curves) == 8
    assert sum(name.startswith("gpu-trace:") for name in curves) == 8


def test_sample_mean_uses_retained_observations_and_not_process_lifetime(monkeypatch, dashboard):
    store = Store(persist=False, series_keep=3)
    store.jobs = dashboard.store.jobs
    dashboard.store = dashboard.app.store = store
    observations(dashboard, [100., 20., 40., 60.])
    curves, _ = inspect_curves(monkeypatch, dashboard)
    assert curves["gpu:nodeA:0:rate"]["values"] == [20., 40., 60.]
    assert curves["gpu:nodeA:0:busy-mean"]["values"] == [20., 30., 40.]


@pytest.mark.parametrize("ascii_", [False, True])
@pytest.mark.parametrize("condition", ["waiting", "off", "disabled", "error"])
def test_empty_gpu_series_explains_collection_state_before_first_success(monkeypatch, dashboard, ascii_, condition):
    health = Health("gpu")
    dashboard.store.health["gpu"] = health
    expected = "GPU telemetry is unavailable"
    if condition == "waiting":
        dashboard.store.jobs[0].state = "PENDING"
        expected = "This job is waiting"
    elif condition == "off":
        dashboard.app.gpu = False
        expected = "GPU sampling is off"
    elif condition == "disabled":
        health.enabled = False
        expected = "The GPU source is disabled"
    else:
        health.error = "srun exit 1: Failed to initialize NVML: Driver/library version mismatch"
        expected = "Driver/library version mismatch"
    curves, text = inspect_curves(monkeypatch, dashboard, ascii_=ascii_)
    assert not curves
    assert expected in text
    assert "raven --gpu-check 900" in text
    assert "no samples recorded" in text


def test_gpu_health_notice_never_queries_sources_or_executes_terminal_controls(monkeypatch, dashboard):
    dashboard.store.health["gpu"] = Health("gpu", error="srun: \x1b[31mGPU initialization failed\x1b[0m\nretry")
    dashboard.app.sampler = SimpleNamespace(trace_status={}, slurm=SimpleNamespace(
        gpu=lambda *a: pytest.fail("GPU renderer attempted collection")))
    dashboard.store._series_loaded.add("900")
    monkeypatch.setattr(dashboard.store, "_series_path", lambda *a: pytest.fail("GPU renderer attempted file IO"))
    _, text = inspect_curves(monkeypatch, dashboard)
    assert "GPU initialization failed" in text
    assert "\x1b" not in text and "^Jretry" in text


@pytest.mark.parametrize("mode", ["off", "existing-telemetry"])
def test_managed_empty_gpu_graph_explains_existing_trace_requirement(monkeypatch, dashboard, mode):
    dashboard.cfg.set("raven", {"managed": True, "collection": {"gpu": mode}})
    dashboard.app.gpu = True
    dashboard.store.health["gpu"] = Health("gpu")
    curves, text = inspect_curves(monkeypatch, dashboard)
    assert not curves
    assert "existing logs/gpu-util-JOBID.csv trace" in text
    if mode == "off":
        assert "GPU collection is disabled by configuration" in text
    else:
        assert "automatic device probes are disabled" in text
    assert "Enable GPU sampling" not in text and "GPU sampling is off" not in text


@pytest.mark.parametrize("state", ["missing_workdir", "missing_file", "empty", "error"])
def test_empty_gpu_series_shows_published_trace_reason_without_loading_the_path(monkeypatch, dashboard, state):
    dashboard.app.sampler = SimpleNamespace(trace_status={"900": {
        "state": state, "path": "/unreachable/gpu-util-900.csv", "reason": "No valid timestamp/index rows", "rows": 0}})
    _, text = inspect_curves(monkeypatch, dashboard)
    assert "GPU trace: No valid timestamp/index rows" in text
    assert "raven --gpu-check 900" in text


@pytest.mark.parametrize("source", ["live", "trace"])
def test_detected_devices_with_only_unknown_counters_keep_gaps_and_explain_state(monkeypatch, dashboard, source):
    if source == "live":
        observations(dashboard, [None, None])
    else:
        dashboard.store.trace["900"] = [dict(t=0., index=0, util=None), dict(t=60., index=0, util=None)]
    curves, text = inspect_curves(monkeypatch, dashboard)
    assert len(curves) == 2
    assert all(item["values"] == [None, None] for item in curves.values())
    assert "devices were detected" in text and "counters are unavailable" in text
    assert "raven --gpu-check 900" in text


@pytest.mark.parametrize("ascii_", [False, True])
def test_optional_gpu_counters_are_safe_in_jobs_inspector_nodes_and_sort_values(dashboard, ascii_):
    from raven.views import job_sort_values
    dashboard.views.g = L.Glyphs(ascii_)
    job = dashboard.store.jobs[0]
    job.hosts = ["nodeA"]
    samples = [GpuSample("nodeA", 0, None, None, None),
               GpuSample("nodeA", 1, 0, 0, 0),
               GpuSample("nodeA", 2, 100, 512, 1024)]
    dashboard.store.gpu["900"] = samples
    dashboard.store.nodes["nodeA"] = Node("nodeA", cpus=8)
    snap = dashboard.store.snapshot()
    rows = dashboard.views.job_rows(snap, dashboard.app)
    assert rows[0]["gpu%"] == "50"
    assert job_sort_values(job, rows[0], snap, ["gpu%"]) == {**job_sort_values(job, rows[0], snap, []), "gpu%": 50}
    inspector = L.to_text(dashboard.views.selected_panel(snap, job, 100, 0, dashboard.app), 100)
    assert "unknown" in inspector and "0%" in inspector
    node_rows, _ = dashboard.views.my_nodes(snap, dashboard.app, 160, None)
    assert "50%" in L.to_text(node_rows, 160) and "unknown" in L.to_text(node_rows, 160)
    if ascii_:
        assert inspector.isascii()


@pytest.mark.parametrize("ascii_", [False, True])
def test_compare_graphs_and_inline_cards_ignore_unknown_device_counters(monkeypatch, dashboard, ascii_):
    dashboard.views.g = L.Glyphs(ascii_)
    dashboard.app.compare_ids = ["900"]
    for timestamp, data in enumerate(({}, {"nodeA:0": [None, None, None]},
                                      {"nodeA:0": [None], "nodeA:1": [0]},
                                      {"nodeA:0": [20], "nodeA:1": [60]})):
        dashboard.store.record("900", dict(k="gpu", t=float(timestamp), gpu=data))
    calls = []
    monkeypatch.setattr(dashboard.views, "metric_curve", lambda app, values, *a, **k: calls.append(list(values)) or [])
    body = dashboard.views.analytics_compare(dashboard.store.snapshot(), dashboard.app, 160, None)
    assert calls == [[None, None, 0., 40.]]
    assert "20%" in L.to_text(body, 160)
    narrow = J._analytics_cards(dashboard.views, dashboard.store.snapshot(), dashboard.app, body)
    assert "GPU mean 20%" in L.to_text(narrow, 160)


@pytest.mark.parametrize("values,expected", [([None, None], "idle share unknown"),
                                            ([None, 0, 100], "50% of valid samples idle"),
                                            ([0, 100], "50% of samples idle")])
def test_trace_summary_uses_only_measured_counters_and_never_invents_current_zero(dashboard, values, expected):
    trace = [dict(t=float(i * 120), index=0, util=value) for i, value in enumerate(values)]
    text = L.row_text(dashboard.views.trace_row(trace, dashboard.store.jobs[0], 12))
    assert expected in text
    if all(value is None for value in values):
        assert "gpu0 unknown (now unknown)" in text
        assert "0%" not in text


def test_trace_summary_old_device_has_unknown_recent_value(dashboard):
    trace = [dict(t=0., index=0, util=100.), dict(t=120., index=1, util=20.)]
    text = L.row_text(dashboard.views.trace_row(trace, dashboard.store.jobs[0], 12))
    assert "gpu0 100% (now unknown)" in text
    assert "gpu1 20% (now 20%)" in text


@pytest.mark.parametrize("counters,expected", [([None], None), ([None, 0], 0.),
                                               ([None, 20, 80], 50.),
                                               ([math.nan, math.inf, -1, 101, True, "50", 10**400], None)])
def test_gpu_expressions_and_full_field_peeks_use_only_valid_observations(dashboard, counters, expected):
    from raven import expr, navigation_tools as N, table_tools as T
    job = dashboard.store.jobs[0]
    job.hosts = ["nodeA"]
    dashboard.store.nodes["nodeA"] = Node("nodeA", cpus=8)
    dashboard.store.gpu[job.id] = [GpuSample("nodeA", index, counter, None, None)
                                    for index, counter in enumerate(counters)]
    T.select_resource(dashboard.app, "nodes", ["nodeA"])
    namespace = expr.job_ns(job, dashboard.store.snapshot())
    assert namespace.gpu == expected
    assert N.raw_field(dashboard.app, "jobs", "gpu%") == expected
    assert N.raw_field(dashboard.app, "nodes", "gutil") == expected
    assert expr.Expr("gpu is not None and gpu < 10")(namespace) is (expected is not None and expected < 10)


def test_node_field_peek_handles_failed_gpu_source_without_iterating_none(dashboard):
    from raven import navigation_tools as N, table_tools as T
    dashboard.store.jobs[0].hosts = ["nodeA"]
    dashboard.store.nodes["nodeA"] = Node("nodeA", cpus=8)
    dashboard.store.gpu["900"] = None
    T.select_resource(dashboard.app, "nodes", ["nodeA"])
    assert N.raw_field(dashboard.app, "nodes", "gutil") is None


def test_inspector_resource_graph_uses_valid_gpu_devices_and_keeps_unknown_time_gaps(monkeypatch, dashboard):
    from raven import analysis_ui as A
    for timestamp, values in enumerate(([None], [None, 0], [20, None, 80], [math.nan, -1, 101, True])):
        dashboard.store.record("900", dict(k="gpu", t=float(timestamp),
            gpu={f"nodeA:{i}": [value] for i, value in enumerate(values)}))
    monkeypatch.setattr(dashboard.store, "series_of", lambda *a: pytest.fail("Inspector frame read a series file"))
    series = A._resource_series(dashboard.app, "900")
    assert series["GPU utilization (%)"] == [dict(t=0., value=None), dict(t=1., value=0.),
                                               dict(t=2., value=50.), dict(t=3., value=None)]
