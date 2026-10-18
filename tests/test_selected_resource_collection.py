"""Visible resource panels collect exact jobs and retain independent counters."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from raven.config import Config
from raven.controller import App
from raven import analysis_ui, layout
from raven.model import Finished, Job, Live, Store
from raven.platform_session import wrap_backend
from raven.sampler import Sampler
from raven.slurm import Slurm
from raven.views import _prepare_native_metrics


@pytest.fixture
def managed_resources(tmp_path):
    class Source:
        def __init__(self):
            self.commands = []

        def run(self, argv, timeout=8):
            self.commands.append(tuple(argv))
            assert argv[0] == "sstat"
            jid = argv[argv.index("-j") + 1]
            return f"{jid}.batch|00:00:10|1G|0|system|1G|1|00:00:10|0|system\n", .001

    raw = Source()
    collection = {"jitter_fraction": 0}
    cfg = Config()
    cfg.set("raven", {"collection": collection})
    cfg.set("collection_cache_dir", str(tmp_path / "cache"))
    governed = wrap_backend(raw, cfg, {"cluster": "local", "user": "researcher"}, None)
    store = Store(persist=False)
    store.jobs = [Job(jid, f"job-{jid}", "local", "RUNNING", cpus=2,
                      elapsed="00:00:20", mem_req="2G", nodelist="system") for jid in ("7", "8")]
    sampler = Sampler(Slurm(governed, "researcher"), store, {"live": 60}, [], collection=collection)
    app = App(store, sampler, None, cfg, "researcher", interactive=False)
    app.selected_id = "7"
    yield SimpleNamespace(app=app, sampler=sampler, store=store, raw=raw)
    sampler.shutdown()


@pytest.mark.parametrize("tab", ["jobs", "history"])
def test_visible_inspector_collects_selected_job_without_starting_steps(managed_resources, tab):
    current = managed_resources
    current.app.tab = tab
    current.app.tick()
    assert current.sampler._collection_needed("live")
    current.sampler.run_source("live")
    assert len(current.raw.commands) == 1
    assert current.raw.commands[0][:3] == ("sstat", "-j", "7")
    assert set(current.store.live) == {"7"}
    assert current.store.live["7"].rss == 1024 ** 3
    assert current.store.series["7"][0]["eff"] == .25
    assert "8" not in current.store.series


@pytest.mark.parametrize("mode", ["off", "logs", "research", "trace"])
def test_hidden_resource_panels_do_not_collect(managed_resources, mode):
    current = managed_resources
    current.app.job_panel_state["mode"] = mode
    current.app.tick()
    current.sampler.src_live()
    assert not current.sampler._collection_needed("live")
    assert not current.raw.commands


def test_explicit_inspector_uses_its_exact_job_on_analytics_tab(managed_resources):
    current = managed_resources
    current.app.tab = "analytics"
    current.app.analytics_job = "8"
    current.app.mode = "details"
    current.app.detail_id = "7"
    current.app.tick()
    current.sampler.run_source("live")
    assert current.raw.commands[0][:3] == ("sstat", "-j", "7")
    assert set(current.store.live) == {"7"}


def test_completed_job_keeps_recorded_series_without_fabricating_new_samples(managed_resources):
    current = managed_resources
    current.store.finished = [Finished("6", "completed", "COMPLETED")]
    current.store.record("6", {"t": 10, "k": "live", "rss": 1024., "cpu": .5})
    original = list(current.store.series["6"])
    current.app.selected_id = "6"
    current.app.tick()
    current.sampler.src_live()
    assert not current.raw.commands
    assert list(current.store.series["6"]) == original


def test_memory_only_observation_survives_persistence_and_prepares_memory_graph(tmp_path):
    store = Store(state_dir=str(tmp_path / "state"))
    store.apply_live("7", Live(cpu_time=None, rss=1024 ** 3, t=1000.))
    sample = json.loads(Path(store._series_path("7")).read_text())
    assert sample["rss"] == 1024 ** 3
    assert sample["cpu_time"] is None and sample["cpu"] is None and sample["eff"] is None
    prepared = _prepare_native_metrics([sample], 2 * 1024 ** 3, True, {"live": 60, "gpu": 60})
    assert prepared["live_count"] == 1
    cpu, memory = prepared["plots"]
    assert list(cpu[1]) == [None]
    assert list(memory[1]) == [50.]


@pytest.mark.parametrize("rss", [None, True, -1., float("nan"), float("inf"), 1 << 10000])
def test_unmeasured_or_invalid_memory_does_not_create_a_series(rss):
    store = Store(persist=False)
    store.apply_live("7", Live(cpu_time=None, rss=rss, t=1000.))
    assert not store.series.get("7")


def test_measured_zero_memory_retains_a_valid_zero_observation():
    store = Store(persist=False)
    store.apply_live("7", Live(cpu_time=None, rss=0., t=1000.))
    assert store.series["7"][0]["rss"] == 0.


@pytest.mark.parametrize("rate,avg,expected", [
    (None, .25, "25% (cumulative efficiency)"),
    (.5, .25, "50% (interval rate)"),
    (0., .25, "0% (interval rate)"),
    (None, None, "unavailable"),
])
def test_resource_inspector_labels_first_sample_efficiency_and_later_rate(managed_resources, rate, avg, expected):
    current = managed_resources
    current.store.apply_live("7", Live(cpu_time=10., rss=1024 ** 3, rate=rate, avg=avg, t=1000.))
    current.app.analysis_state.update(job="7", section=analysis_ui.SECTIONS.index("Resources"))
    rows = analysis_ui._inspector_rows(layout.Glyphs(True), current.store.snapshot(), current.app, 100)
    text = " ".join(" ".join(layout.row_text(row) for row in rows).split())
    assert "CPU per core " + expected in text
