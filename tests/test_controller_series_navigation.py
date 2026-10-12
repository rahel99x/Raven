"""Analytics keyboard navigation never inventories saved histories on the UI thread."""
from threading import Event, get_ident
from types import SimpleNamespace
import json
import os

import pytest

from raven.config import Config
from raven.controller import App
from raven.model import Job, Store
from raven.sampler import Sampler
from raven.actions import Actions
from raven.slurm import FakeBackend, Slurm


def test_interactive_navigation_uses_the_native_sampler_archive_worker(tmp_path, monkeypatch):
    store = Store(state_dir=str(tmp_path))
    store.jobs = [Job("7", "first", "cpu", "RUNNING"), Job("8", "second", "cpu", "RUNNING")]
    series = tmp_path / "series"
    series.mkdir(exist_ok=True)
    (series / "9.jsonl").write_text('{"k":"live","t":1,"cpu":0.5}\n')
    sampler = Sampler(SimpleNamespace(), store, {}, [], workers=1)
    app = App(store, sampler, None, Config(), "test", interactive=True)
    app.tab, app.analytics_view, app.analytics_job = "analytics", "job", "7"
    entered, release = Event(), Event()
    ui_thread, threads = get_ident(), []
    original = os.scandir

    def blocked_inventory(path):
        threads.append(get_ident())
        assert get_ident() != ui_thread, "saved metric inventory ran on the UI thread"
        entered.set()
        assert release.wait(5)
        return original(path)

    def forbidden_sync_inventory(*args, **kwargs):
        pytest.fail("interactive keyboard navigation used synchronous saved metric inventory")

    monkeypatch.setattr(os, "scandir", blocked_inventory)
    monkeypatch.setattr(store, "series_jobs", forbidden_sync_inventory)
    try:
        # A cold inventory may be arbitrarily slow; arrows use available jobs
        # immediately and the existing native source executor restores the rest.
        app.move("down")
        assert app.analytics_job == "8" and entered.wait(3)
        app.move("home")
        assert app.analytics_job == "7"
        release.set()
        sampler.pool.submit(lambda: None).result(timeout=5)
        app.move("end")
        assert app.analytics_job == "9"
        assert threads and all(thread != ui_thread for thread in threads)
    finally:
        release.set()
        sampler.shutdown()


def test_headless_navigation_keeps_synchronous_saved_history_discovery(tmp_path):
    store = Store(state_dir=str(tmp_path))
    series = tmp_path / "series"
    series.mkdir(exist_ok=True)
    (series / "9.jsonl").write_text('{"k":"live","t":1,"cpu":0.5}\n')
    app = App(store, None, None, Config(), "test", interactive=False)
    app.tab, app.analytics_view = "analytics", "job"
    app.move("end")
    assert app.analytics_job == "9"


@pytest.mark.parametrize("command", ["advise 12480001", "resubmit 12480001 --advised"])
@pytest.mark.parametrize("interactive", [False, True])
def test_explicit_advice_keeps_full_saved_evidence_and_ignores_corrupt_counters(tmp_path, monkeypatch, command, interactive):
    from raven import advisor
    backend = FakeBackend("alex")
    slurm = Slurm(backend, "alex")
    store = Store(state_dir=str(tmp_path))
    store.apply_jobs(slurm.jobs())
    series = tmp_path / "series"
    series.mkdir(exist_ok=True)
    path = series / "12480001.jsonl"
    peak = 1 << 40
    records = [{"k": "live", "t": 1, "rss": peak},
               {"k": "live", "t": 2, "rss": {"corrupt": True}},
               {"k": "live", "t": 3, "rss": "unavailable"},
               {"k": "live", "t": "invalid", "rss": 1 << 50}]
    original_bytes = "".join(json.dumps(record) + "\n" for record in records).encode()
    path.write_bytes(original_bytes)
    app = App(store, None, Actions(slurm, store), Config(), "alex", interactive=interactive)
    measured = []
    original = advisor.advise_running

    def observe(*args, **kwargs):
        result = original(*args, **kwargs)
        measured.append(result.mem_peak)
        return result

    monkeypatch.setattr(advisor, "advise_running", observe)
    monkeypatch.setattr(store, "series_view", lambda *_: pytest.fail("explicit advice discarded full saved evidence"))
    jobs_before = len(backend.spec)
    app.run_command(command)
    assert app.command_ok and measured == [peak]
    assert path.read_bytes() == original_bytes
    assert len(backend.spec) == jobs_before
    if command.startswith("resubmit"):
        assert app.mode == "confirm" and app.confirm["action"] == "resubmit"
    else:
        assert "so far" in app.message and app.mode == "main"
