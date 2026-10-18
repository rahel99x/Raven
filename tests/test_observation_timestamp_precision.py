"""Captured telemetry retains its time through narrow charts and JSONL reloads."""
import json
from pathlib import Path

import pytest

from raven import charts, clock, layout as L
from raven.model import GpuSample, Live, Store


def identified_store(path):
    store = Store(state_dir=str(path))
    store.bind_series_attempt("7", {"run_root":"/verified/project", "run_id":"run", "attempt":"a1", "job_id":"7", "submission_token":"token", "cluster":"fixture"})
    return store


def capture(store, monkeypatch, kind, timestamp, value=0.5):
    if kind == "live":
        store.apply_live("7", Live(cpu_time=10., rss=1024., avg=value,
                                   rate=value, t=timestamp))
    else:
        monkeypatch.setattr(clock, "now", lambda: timestamp)
        store.apply_gpu("7", [GpuSample("node", 0, value * 100., 128., 1024.)])


@pytest.mark.parametrize("kind", ["live", "gpu"])
@pytest.mark.parametrize("timestamp", [1700000000.0005, 1700000000.0495,
                                       1700000000.0505, 1700000000.0995,
                                       1700000000.9995, 1700000001.0005,
                                       1699999999.9995, -0.0005])
def test_fractional_capture_is_exact_in_memory_json_and_new_session(tmp_path, monkeypatch,
                                                                 kind, timestamp):
    store = identified_store(tmp_path)
    capture(store, monkeypatch, kind, timestamp)
    memory = store.series_of("7")
    persisted = [json.loads(line) for line in Path(store._series_path("7")).read_text().splitlines()]
    restored = identified_store(tmp_path).series_of("7")
    assert memory == persisted == restored
    assert restored[0]["t"] == timestamp
    assert restored[0]["k"] == kind


@pytest.mark.parametrize("kind", ["live", "gpu"])
@pytest.mark.parametrize("ascii_", [False, True])
def test_capture_inside_one_millisecond_window_never_moves_into_future(monkeypatch, kind, ascii_):
    captured_at = 1700000000.0505
    now = captured_at + 0.0002
    window = (now - 0.001, now)
    # Rounding this actual observation to a tenth of a second dates it after
    # the display clock and removes it from the otherwise valid live window.
    assert round(captured_at, 1) > now
    store = Store(persist=False)
    capture(store, monkeypatch, kind, captured_at)
    sample = store.series_of("7")[0]
    assert window[0] < sample["t"] <= window[1]
    value = sample["cpu"] if kind == "live" else sample["gpu"]["node:0"][0] / 100.
    metadata = {}
    charts.braille_chart(L.Glyphs(ascii_), [value], 70, 6, hi=1.,
                         times=window, sample_times=[sample["t"]], metadata=metadata)
    assert metadata["x_bounds"] == window
    assert metadata["has_data"] is True


@pytest.mark.parametrize("kind", ["live", "gpu"])
def test_submillisecond_observations_remain_distinct_after_reload(tmp_path, monkeypatch, kind):
    timestamps = [1700000000.0502, 1700000000.0505, 1700000000.0508]
    store = identified_store(tmp_path)
    for timestamp, value in zip(timestamps, [0., 1., 0.]):
        capture(store, monkeypatch, kind, timestamp, value)
    restored = identified_store(tmp_path).series_of("7")
    assert [sample["t"] for sample in restored] == timestamps
    values = [sample["cpu"] if kind == "live" else sample["gpu"]["node:0"][0] / 100.
              for sample in restored]
    points, resolved = charts._time_points(values, timestamps, 100,
                                           (timestamps[0], timestamps[-1]), None, envelope=True)
    assert resolved == (timestamps[0], timestamps[-1])
    assert len({x for x, value, bridge in points}) == 3
    assert [value for x, value, bridge in points] == [0., 1., 0.]


@pytest.mark.parametrize("kind", ["live", "gpu"])
def test_new_precision_coexists_with_legacy_jsonl_samples(tmp_path, monkeypatch, kind):
    path = tmp_path / "series" / "7.jsonl"
    path.parent.mkdir()
    legacy = {"t": 1700000000.0, "k": kind}
    path.write_text(json.dumps(legacy) + "\n")
    captured_at = 1700000000.0005
    store = identified_store(tmp_path)
    capture(store, monkeypatch, kind, captured_at)
    merged = store.series_of("7")
    assert [sample["t"] for sample in merged] == [captured_at]
    assert store.legacy_series("7")[0] == [legacy]
    assert path.read_text() == json.dumps(legacy) + "\n"
    assert identified_store(tmp_path).series_of("7") == merged


def test_cpu_capture_time_is_independent_of_later_store_clock(monkeypatch):
    captured_at = 1700000000.0005
    monkeypatch.setattr(clock, "now", lambda: captured_at + 12.)
    store = Store(persist=False)
    store.apply_live("7", Live(cpu_time=10., t=captured_at))
    assert store.series_of("7")[0]["t"] == captured_at
