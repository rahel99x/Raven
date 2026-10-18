"""Metric kinds remain descriptive, confined, bounded and user-overridable."""
from __future__ import annotations

import json
import math
from types import SimpleNamespace

import pytest

from raven import analysis_ui as A, chart_tools, layout as L, metric_envelope as E
from raven.metrics import MAX_META_BYTES, MetricReader, metric_insights, read_meta, validate_meta, write_metric


def sidecar(**updates):
    return {"schema": "raven.metrics-meta/v1", "client": {"api_version": 1},
            "kinds": {"residual": "residual"}, "coalesced": {"residual": 99},
            "rank_policy": {"mode": "coordinator", "rank": 0}, **updates}


def points(values):
    return [{"t": float(i + 1), "value": value} for i, value in enumerate(values)]


def visible(rows):
    return "\n".join(L.row_text(row) for row in rows)


def test_sidecar_changes_without_a_new_metric_record_and_old_snapshots_are_owned(tmp_path):
    stream = tmp_path / "metrics.jsonl"
    write_metric(stream, {"residual": .2})
    meta = tmp_path / "metrics.meta.json"
    meta.write_text(json.dumps(sidecar()))
    reader = MetricReader()
    first = reader.read(stream)
    assert first["kinds"] == {"residual": "residual"}
    assert first["coalesced"] == {"residual": 99}
    assert read_meta(tmp_path) == read_meta(stream) == read_meta(meta)
    meta.write_text(json.dumps(sidecar(coalesced={"residual": 200})))
    second = reader.read(stream)
    assert first["coalesced"]["residual"] == 99
    assert second["coalesced"]["residual"] == 200
    assert second["records"] == first["records"] == 1
    meta.unlink()
    assert reader.read(stream)["kinds"] == {}


@pytest.mark.parametrize("mutation", [
    {"schema": "unknown"}, {"client": {"api_version": True}},
    {"kinds": {"loss": "unknown"}}, {"kinds": {"bad\n": "objective"}},
    {"coalesced": {"loss": -1}}, {"coalesced": {"loss": True}},
    {"baselines": {"loss": float("nan")}}, {"baselines": {"loss": float("inf")}},
    {"kinds": {str(i): "gauge" for i in range(65)}},
    {"rank_policy": {"mode": "run-shell", "rank": 0}},
    {"rank_policy": {"mode": "coordinator", "rank": -1}},
    {"rank_policy": {"mode": "coordinator", "rank": 0, "coverage": ["x"] * 33}},
])
def test_invalid_metadata_never_changes_valid_metric_observations(tmp_path, mutation):
    stream = tmp_path / "metrics.jsonl"
    write_metric(stream, {"residual": .2}, t=10)
    (tmp_path / "metrics.meta.json").write_text(json.dumps(sidecar(**mutation)))
    result = MetricReader().read(stream)
    assert result["status"] == "ok"
    assert result["latest"] == {"residual": .2}
    assert result["meta_error"] and result["kinds"] == {}


@pytest.mark.parametrize("unsafe", ["symlink", "oversized", "duplicate", "fifo"])
def test_metadata_unsafe_sources_refused_without_blocking_stream(tmp_path, unsafe):
    import os
    stream = tmp_path / "metrics.jsonl"
    write_metric(stream, {"residual": 1})
    meta = tmp_path / "metrics.meta.json"
    if unsafe == "symlink":
        target = tmp_path / "elsewhere"
        target.write_text(json.dumps(sidecar()))
        meta.symlink_to(target)
    elif unsafe == "oversized":
        meta.write_bytes(b" " * (MAX_META_BYTES + 1))
    elif unsafe == "fifo":
        os.mkfifo(meta)
    else:
        meta.write_text('{"schema":"raven.metrics-meta/v1","schema":"other"}')
    result = MetricReader().read(stream)
    assert result["records"] == 1 and result["meta_error"]
    assert not result["kinds"]


def test_per_rank_and_custom_stream_sidecars_are_not_confused(tmp_path):
    for name, kind in (("metrics.rank-3", "throughput"), ("custom", "error")):
        stream = tmp_path / (name + ".jsonl")
        write_metric(stream, {"x": 1})
        (tmp_path / (name + ".meta.json")).write_text(json.dumps(sidecar(kinds={"x": kind})))
        assert MetricReader().read(stream)["kinds"] == {"x": kind}


def test_screen_median_is_bounded_and_never_bridges_unknown_columns():
    profile = [E.Column(i, v, v, v, v, 1, i != 0) for i, v in enumerate([1, 100, 2, 3])]
    assert [point[1] for point in E.rolling_median(profile, 4)] == [1, 50.5, 2, 2.5]
    profile[2] = E.Column(2, 2, 2, 2, 2, 1, True, broken=True)
    result = E.rolling_median(profile, 4)
    assert result[-1] == (3, 3, False)
    assert len(E.rolling_median(profile, 2)) == 2


def test_drift_band_uses_reference_without_inventing_a_tolerance():
    profile = [E.Column(0, 2, 3, 2, 3, 2, False), E.Column(1, -2, -1, -1, -2, 2, True)]
    assert [(item.low, item.high) for item in E.drift_columns(profile, 0)] == [(0, 3), (-2, 0)]
    assert E.drift_columns(profile, float("nan")) == ()


@pytest.mark.parametrize("ascii_", [False, True])
@pytest.mark.parametrize("kind", ["residual", "error"])
def test_log_kind_defaults_are_gaps_for_nonpositive_values_and_preserve_raw_inspection(ascii_, kind):
    app = SimpleNamespace()
    original = points([10, 0, -1, 1, .1])
    metadata = {}
    rows = A.chart_rows(L.Glyphs(ascii_), app, original, 140, 5, "r", "test",
                        interactive=False, metadata=metadata, metric_meta={"kind": kind, "coalesced": 99})
    assert metadata["scale"] == "log"
    text = visible(rows)
    assert f"[{kind}]" in text
    assert "2 nonpositive samples undefined" in text
    assert "coalesced " + ("x" if ascii_ else "×") + "99" in text
    assert original == points([10, 0, -1, 1, .1])


@pytest.mark.parametrize("kind,expected", [("throughput", "rolling median"), ("conserved", "observed drift")])
def test_semantic_chart_layers_have_explicit_labels(kind, expected):
    app = SimpleNamespace()
    meta = {}
    rows = A.chart_rows(L.Glyphs(False), app, points([1, 2, 4, 3, 6, 5, 8]), 140, 7, "value", "test",
                        interactive=False, metadata=meta, metric_meta={"kind": kind, "baseline": 1.0})
    assert expected in visible(rows)
    if kind == "conserved":
        assert "no tolerance" in visible(rows)


def test_chart_modes_and_axes_override_defaults_and_persist_without_changing_source():
    app = SimpleNamespace(mode="main", say=lambda message: None, save=lambda: None)
    state = A.initialize(app)
    state["metric"] = "residual"
    series = {"residual": points([8, 6, 5, 4, 3, 2, 1])}
    assert A._chart_control(app, ["mode", "raw"], series)
    assert A._chart_control(app, ["axis", "auto"], series)
    clone = SimpleNamespace()
    A.restore(clone, A.save(app))
    assert clone.analysis_state["metric_modes"] == {"residual": "raw"}
    meta = {}
    A.chart_rows(L.Glyphs(False), clone, series["residual"], 110, 5, "residual", "test",
                       interactive=False, metadata=meta, metric_meta={"kind": "residual"})
    assert meta["scale"] == "linear" and not meta["trend"] and not meta["band"]
    assert A._chart_control(app, ["mode", "default"], series)
    assert not app.analysis_state["metric_modes"]


def test_chart_cache_invalidates_when_only_kind_changes():
    app = SimpleNamespace()
    source = points([1, 2, 3, 4, 5])
    def draw(kind):
        meta = {}
        A.chart_rows(L.Glyphs(False), app, source, 90, 5, "value", "test", interactive=False,
                     metadata=meta, metric_meta={"kind": kind})
        return meta
    assert draw("objective")["scale"] == "linear"
    assert draw("residual")["scale"] == "log"


def test_metric_estimates_abstain_on_gaps_nonpositive_or_nonadvancing_time():
    result = {"kinds": {"r": "residual"}, "series": {"r": points([1, 2, 3, 4, 5])}}
    valid = metric_insights(result)["r"]
    assert valid["status"] == "caution" and valid["log_rate_per_second"] == pytest.approx(math.log(5) / 4)
    for source in (points([1, 0, 2, 3, 4]), points([1, 2]),
                   [{"t": t, "value": 1} for t in [1, 2, 3, 100, 101]],
                   [{"t": 1, "value": i + 1} for i in range(5)]):
        item = metric_insights({**result, "series": {"r": source}})["r"]
        assert item["status"] == "unknown" and "log_rate_per_second" not in item


def test_conserved_zero_baseline_and_coalescing_are_honest():
    item = metric_insights({"kinds": {"e": "conserved"}, "coalesced": {"e": 100}, "baselines": {"e": 0.0},
                            "series": {"e": points([1, 2, 3, 4, 5])}})["e"]
    assert item["delta"] == 5 and item["baseline"] == 0
    assert any("No tolerance" in text for text in item["limitations"])
    assert any("coalesced" in text for text in item["limitations"])
    assert "percent" not in str(item).lower()


def test_chart_preferences_filter_untrusted_and_excessive_mode_declarations():
    saved = {"metric_modes": {**{str(i): "raw" for i in range(70)}, "bad\x1b": "range", "evil": "execute"}}
    restored = chart_tools.restore(saved)
    assert len(restored["metric_modes"]) == 64
    assert "bad\x1b" not in restored["metric_modes"]


def test_schema_example_and_runtime_reader_agree():
    from pathlib import Path
    import jsonschema
    root = Path(__file__).resolve().parents[1]
    schema = json.loads((root / "docs/schemas/metrics-meta.v1.schema.json").read_text())
    example = json.loads((root / "docs/examples/metrics.meta.json").read_text())
    jsonschema.Draft202012Validator(schema).validate(example)
    accepted = validate_meta(example)
    assert accepted["baselines"]["energy"] == 0


def test_80_column_inspector_exposes_modes_and_mouse_uses_same_persisted_command(monkeypatch):
    from raven.config import Config
    from raven.controller import App
    from raven.model import Job, Store
    from raven.research import ResearchHub
    app = App(Store(persist=False), None, None, Config(), "test")
    app.store.jobs = [Job("1", "test", "cpu", "RUNNING")]
    app.selected_id = "1"
    app.research = ResearchHub(Config())
    app.analysis_result = {"path": "metrics.jsonl", "kinds": {"r": "residual"}, "series": {"r": points([1, 2, 3, 4, 5])}}
    app.analysis_result_job = "1"
    app.analysis_result_generation = app.research.generation
    saves = []
    monkeypatch.setattr(app, "save", lambda: saves.append(A.save(app)))
    try:
        A.run_command(app, ["chart", "r"])
        monkeypatch.setattr(MetricReader, "_read_meta", lambda *a, **k: pytest.fail("render read metadata"))
        A.overlay(SimpleNamespace(g=L.Glyphs(False)), app.store.snapshot(), app, 80, 24)
        y, _, control = next(hit for hit in app.analysis_state["control_hits"] if hit[2]["label"] == "Raw")
        assert A.handle_mouse(app, y, control["left"])
        assert app.analysis_state["metric_modes"]["r"] == "raw"
        assert saves[-1]["analysis"]["metric_modes"] == {"r": "raw"}
    finally:
        app.research.close()
        app.store.close()


def test_advisor_separates_observed_change_from_tolerance_and_excludes_old_attempt():
    from raven.quick_advisor import analyze
    from raven.model import Job
    job = Job("1", "test", "cpu", "RUNNING", start="1970-01-01T00:01:40+00:00")
    reported = {"kinds": {"r": "residual"}, "series": {"r": points([1, 2, 3, 4, 5])}}
    old = analyze({"job": job, "application_metrics": reported})
    section = next(item for item in old["sections"] if item["name"] == "Metric: r")
    assert section["status"] == "unknown"
    job.start = ""
    current = analyze({"job": job, "application_metrics": reported})
    section = next(item for item in current["sections"] if item["name"] == "Metric: r")
    assert section["status"] == "caution"
    assert "does not prove convergence" in " ".join(section["facts"])


def test_actual_client_all_ranks_reaches_reader_and_index_ui_without_canonical_stream(tmp_path, monkeypatch):
    import os
    import subprocess
    import sys
    from pathlib import Path
    from raven.config import Config
    from raven.controller import App
    from raven.model import Job, Store
    from raven.research import ResearchHub
    from raven.research_views import render
    from raven.views import Views
    script = "import raven; raven.configure(path='metrics.jsonl',all_ranks=True); raven.metric('loss',VALUE,kind='residual')"
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
    for rank, value in [(0, 1), (3, 4)]:
        subprocess.run([sys.executable, "-c", script.replace("VALUE", str(value))], cwd=tmp_path,
                       env={**env, "RANK": str(rank)}, check=True, timeout=10)
    assert not (tmp_path / "metrics.jsonl").exists()
    stream = str(tmp_path / "metrics.jsonl")
    first = MetricReader().read(stream)
    assert first["latest"] == {"loss": 1} and len(first["rank_streams"]) == 2
    cfg = Config()
    app = App(Store(persist=False), None, None, cfg, "test")
    app.store.jobs = [Job("1", "test", "cpu", "RUNNING")]
    app.selected_id = app.research_job_id = "1"
    app.tab, app.research_view = "research", "experiment"
    hub = app.research = ResearchHub(cfg)
    hub.configure(metrics_file=stream)
    try:
        context = hub.context(app.store.snapshot(), app)
        result = hub.request(context, wait=True)
        assert result["latest"] == {"loss": 1}
        monkeypatch.setattr(hub, "request", lambda context: result)
        views = Views(L.Glyphs(False), cfg)
        rows, hits = render(views, app.store.snapshot(), app, 80, 24)
        assert "[Rank 0]" in visible(rows) and "Rank 3" in visible(rows)
        assert "one independently recorded rank" in visible(rows)
        action = next(value["action"][1] for _, kind, value in hits
                      if kind == "control" and value.get("label") == "Rank 3")
        app.run_command(action)
        assert app.command_ok
        assert hub.settings["metric_stream"] == {"source": stream, "name": "metrics.rank-3.jsonl"}
        selected_context = hub.context(app.store.snapshot(), app)
        selected = hub._read(selected_context)
        assert selected["latest"] == {"loss": 4} and selected["records"] == 1
        assert selected["kinds"] == {"loss": "residual"}
        # A source switch cannot carry the rank selector onto a different file.
        hub.configure(metrics_file=str(tmp_path / "other.jsonl"))
        write_metric(tmp_path / "other.jsonl", {"other": 8})
        assert hub._read(hub.context(app.store.snapshot(), app))["latest"] == {"other": 8}
        # Reset remains available even if a vanished index removed all choices.
        A.publish_metric_metadata(app, {})
        assert A._chart_control(app, ["rank", "default"], {})
        assert hub.settings["metric_stream"] == {}
    finally:
        hub.close()
        app.store.close()


def test_semantic_estimates_remain_finite_at_float_extremes():
    profile = [E.Column(i, 1e308, 1e308, 1e308, 1e308, 1, bool(i)) for i in range(7)]
    assert all(value == 1e308 for _, value, _ in E.rolling_median(profile, 7))
    profile[0] = E.Column(0, -1e308, -1e308, -1e308, -1e308, 1, False)
    assert E.rolling_median(profile, 7)[1][1] == 0
    item = metric_insights({"kinds": {"r": "residual"}, "series": {"r": [
        {"t": (i + 1) * 1e-320, "value": 10 ** i} for i in range(5)]}})["r"]
    assert item["status"] == "unknown" and "log_rate_per_second" not in item
    json.dumps(item, allow_nan=False)


def test_actual_no_trace_launch_all_rank_client_reads_begun_empty_canonical(tmp_path):
    import os
    import shlex
    import sys
    from pathlib import Path
    from raven.launch import resolve_plan
    from raven.launch_runtime import execute_plan
    from raven.projects import select_run
    script = tmp_path / "job.sh"
    code = "import raven; raven.configure(all_ranks=True); raven.metric('loss',VALUE,kind='objective')"
    script.write_text("#!/bin/sh\nset -eu\n" + "\n".join(
        f"RANK={rank} " + shlex.join([sys.executable, "-c", code.replace("VALUE", str(value))])
        for rank, value in [(0, 2), (1, 8)]) + "\n")
    env = {key: value for key, value in os.environ.items() if not key.startswith("RAVEN_") and key != "PYTHONPATH"}
    plan = resolve_plan([str(script), "--project", str(tmp_path), "--local", "--no-trace"], cwd=tmp_path, environ=env)
    outcome = execute_plan(plan, environ=env)
    assert outcome["exit_code"] == 0
    run = Path(outcome["run_dir"])
    assert (run / "metrics.jsonl").read_bytes() == b""
    reader = MetricReader()
    assert reader.read(run / "metrics.jsonl")["latest"] == {"loss": 2}
    binding = select_run(str(tmp_path), run.name)["binding"]
    first = reader.read_confined(binding["metrics_file"], binding)
    other = reader.read_confined(binding["metrics_file"], binding, rank_stream="metrics.rank-1.jsonl")
    assert first["latest"] == {"loss": 2} and first["selected_stream"] == "metrics.rank-0.jsonl"
    assert other["latest"] == {"loss": 8} and other["records"] == 1
