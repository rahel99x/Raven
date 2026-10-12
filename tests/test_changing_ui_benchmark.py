"""Fresh-data stress fixtures measure maintenance, Live, resize and capture paths."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture(scope="module")
def benchmark():
    path = Path(__file__).resolve().parents[1] / "scripts" / "benchmark_changing_ui.py"
    spec = importlib.util.spec_from_file_location("raven_changing_ui_benchmark", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("case", ["static", "live30", "live1", "switch", "resize", "scroll-end"])
def test_changing_fixture_uses_actual_effects_and_new_samples_without_frame_io(benchmark, case):
    args = SimpleNamespace(jobs=3, history=8, points=40, width=320, height=52,
                           repeats=56, profile=False)
    result = benchmark.run_case(args, case, False)
    assert result["io_attempts"] == {}
    assert result["native_publications"] == 3
    assert result["visible_plots"] > 0
    assert result["counts_per_frame"]["compose_calls"]["max"] == 1
    assert result["phases_ms"]["frame"]["p95"] <= result["phases_ms"]["frame"]["p99"]
    assert result["phases_ms"]["frame"]["p99"] <= result["phases_ms"]["frame"]["max"]
    frames = result["frames"]
    assert sum(frame["publication"] for frame in frames) == 3
    assert any("maintenance" in frame["reasons"] or "live" in frame["reasons"] for frame in frames)
    # Default running plots now move a buffered full-history endpoint too.
    # Depending on span/terminal width, its subcolumn deadline or normal
    # maintenance can win. A one-second Live window still requires faster
    # movement; neither longer view must manufacture redundant redraws.
    if case == "live1":
        assert any("live" in frame["reasons"] for frame in frames)
    if case == "switch":
        assert frames[40]["gesture"] == "job_click"
        assert frames[40]["selected_id"] != frames[0]["selected_id"]
    if case == "resize":
        assert frames[50]["resized"]
    if case == "scroll-end":
        assert not result["geometry_failures"]
        assert frames[0]["gesture"] == "scroll_bottom"
        assert frames[40]["gesture"] == "scroll_top"
        assert all(frame["visible_metrics"] for frame in frames)
