"""Diagnostic benchmarks exercise published views without source I/O."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture(scope="module")
def benchmark():
    path = Path(__file__).resolve().parents[1] / "scripts" / "benchmark_ui.py"
    spec = importlib.util.spec_from_file_location("raven_ui_benchmark", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("ascii_", [False, True])
@pytest.mark.parametrize("scenario,gesture,graphs,controls", [
    ("jobs", "chart-hover", 4, 2),
    ("analytics", "chart-hover", 6, 0),
    ("research", "chart-hover", 6, 0),
    ("jobs", "hover", 0, 6),
    ("advisor", "hover", 0, 6),
    ("jobs", "wheel", 0, 0),
    ("jobs", "render", 0, 0),
])
def test_benchmark_targets_real_graphs_and_controls_without_scheduler_or_file_access(
        benchmark, monkeypatch, ascii_, scenario, gesture, graphs, controls):
    from raven import screen
    # Test input invalidation independently of scheduled maintenance, animation,
    # and buffered-graph deadlines. CPU timings still use real perf_counter;
    # a slow worker must not turn a valid timer refresh into a hover failure.
    scheduled_now = screen.time.monotonic()
    monkeypatch.setattr(screen.time, "monotonic", lambda: scheduled_now)
    args = SimpleNamespace(scenario=scenario, gesture=gesture, jobs=1, history=8,
                           points=40, metrics=2, width=140, height=48, repeats=6)
    result = benchmark.run_case(args, ascii_)
    assert result["pointer_targets"] == {"graphs": graphs, "controls": controls}
    assert result["pipeline"] == "cached screen"
    assert result["glyphs"] == ("ascii" if ascii_ else "unicode")
    assert result["io_attempts"] == {}
    assert result["counts_per_frame"]["erase_calls"]["max"] == 0
    if gesture in ("hover", "chart-hover"):
        assert result["counts_per_frame"]["compose_calls"]["max"] == 0
        assert result["counts_per_frame"]["chart_rasters"]["max"] == 0
    else:
        assert result["counts_per_frame"]["compose_calls"]["median"] == 1
