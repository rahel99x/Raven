"""Phase-one controls share the existing terminal, identity and worker contracts."""
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import threading

import pytest

from raven import gpu_provider_ui, interaction, layout as L, screen, toolbar, workbench
from raven.actions import Actions
from raven.config import Config
from raven.controller import App
from raven.model import GpuSample, Job, Store
from raven.sampler import Sampler
from raven.slurm import CommandError, FakeBackend, Slurm
from raven.views import Views
from raven.worker_scheduler import WorkerScheduler

MOUSE = SimpleNamespace(BUTTON1_CLICKED=1, BUTTON1_PRESSED=2, BUTTON1_RELEASED=4,
    BUTTON1_DOUBLE_CLICKED=8, BUTTON3_CLICKED=16, BUTTON3_PRESSED=32,
    BUTTON4_PRESSED=64, BUTTON5_PRESSED=128, REPORT_MOUSE_POSITION=256, BUTTON_SHIFT=512)


@pytest.fixture
def dashboard():
    cfg = Config()
    cfg.set("startup_animation", False)
    cfg.set("animations", False)
    cfg.set("log_lines", 0)
    store = Store(persist=False)
    store.jobs = [Job("70", "selected", "gpu", "RUNNING", cpus=2, gpus=1),
                  Job("71", "other", "gpu", "RUNNING", cpus=2, gpus=1)]
    slurm = Slurm(FakeBackend(), "test")
    sampler = Sampler(slurm, store, cfg["intervals"], cfg["gpu_types"], weather=False, budget=False)
    app = App(store, sampler, Actions(slurm, store), cfg, "test", interactive=False)
    views = Views(L.Glyphs(False), cfg)
    app.views_ref = views
    app.selected_id = "70"
    result = SimpleNamespace(app=app, store=store, views=views, sampler=sampler, slurm=slurm)

    def draw(width=150, height=45):
        rows, hits = views.compose(store.snapshot(), app, width, height)
        positioned = views.overlay(store.snapshot(), app, width, height)
        return rows, hits, positioned

    result.draw = draw
    draw()
    yield result
    if app.research:
        app.research.close()
    sampler.shutdown()
    sampler.worker_scheduler.shutdown(wait=True, cancel_futures=True)


@pytest.mark.parametrize("mode", ["telemetry", "shell_checks", "arraymap", "gpu_provider"])
@pytest.mark.parametrize("button", ["motion", "drag", "release", "right", "left", "press"])
def test_modal_owns_pointer_over_underlying_tabs_and_jobs(dashboard, mode, button):
    app = dashboard.app
    tab_point = next((y, left) for y, left, right, key in app.tab_hits if key == "research")
    job_row = next(y for y, kind, value in app.last_hits if kind == "job" and value == "71")
    if mode == "arraymap":
        app.run_command("arraymap")
    else:
        app.mode = mode
    dashboard.draw()
    initial = (app.tab, app.selected_id, tuple(app.marks))
    for y, x in (tab_point, (job_row, 2), (app.height - 2, 2)):
        app.click(y, x, app.last_hits, button=button)
        assert (app.tab, app.selected_id, tuple(app.marks)) == initial
        assert app.mode == mode


@pytest.mark.parametrize("ascii_mode", [False, True])
@pytest.mark.parametrize("size", [(28, 12), (60, 24), (150, 45)])
@pytest.mark.parametrize("mode", ["telemetry", "shell_checks", "arraymap", "gpu_provider"])
def test_new_modal_controls_are_bounded_and_do_not_run_collectors(dashboard, monkeypatch, ascii_mode, size, mode):
    app = dashboard.app
    dashboard.views.set_ascii(ascii_mode)
    if mode == "arraymap":
        app.run_command("arraymap")
    else:
        app.mode = mode
    def forbidden(*args, **kwargs):
        raise AssertionError("Rendering performed a scheduler command")
    monkeypatch.setattr(dashboard.slurm.b, "run", forbidden)
    _, _, positioned = dashboard.draw(*size)
    assert positioned
    for y, x, row in positioned:
        assert 0 <= y < size[1]
        assert 0 <= x < size[0]
        assert L.vlen(L.row_text(row)) + x <= size[0]
    for control in app.interaction_state["graph"].controls:
        assert control.rect.clip(*size) == control.rect


def test_source_controls_and_provider_changes_use_real_registry(dashboard):
    app = dashboard.app
    app.enter_tab("sources")
    dashboard.draw()
    control = next(c for c in app.interaction_state["graph"].controls if c.id == "source-tools:gpu-provider")
    app.click(control.rect.top, control.rect.left, app.last_hits, button="left")
    assert app.mode == "gpu_provider" and app.tab == "sources"
    dashboard.draw()
    control = next(c for c in app.interaction_state["graph"].controls if c.id == "gpu-provider:amd")
    app.click(control.rect.top, control.rect.left, app.last_hits, button="left")
    assert app.cfg["gpu_provider"] == "amd"
    assert dashboard.slurm.gpu_provider == "amd"
    assert app.mode == "gpu_provider"
    app.handle("esc")
    # Escape may first leave the shared button-navigation graph.
    if app.mode == "gpu_provider":
        app.handle("esc")
    assert app.mode == "main" and app.tab == "sources"


def test_provider_setting_invalidates_current_data_and_keeps_history(dashboard):
    dashboard.store.apply_gpu("70", [GpuSample("node", 0, 50, 10, 20, "NVIDIA")])
    history = list(dashboard.store.series_of("70"))
    generation = dashboard.slurm.gpu_provider_generation
    dashboard.app.run_command("gpuprovider intel")
    assert dashboard.store.gpu["70"] is None
    assert list(dashboard.store.series_of("70")) == history
    assert dashboard.slurm.gpu_provider_generation == generation + 1
    dashboard.app.run_command("gpuprovider intel")
    assert dashboard.slurm.gpu_provider_generation == generation + 1
    dashboard.app.run_command("gpuprovider unsafe")
    assert dashboard.app.cfg["gpu_provider"] == "intel"


def test_provider_preferences_survive_restore_without_running_commands(dashboard, monkeypatch):
    dashboard.app.run_command("gpuprovider amd")
    saved = workbench.save(dashboard.app)
    dashboard.app.cfg.set("gpu_provider", "auto")
    def forbidden(*args, **kwargs):
        raise AssertionError("Restoring a preference performed network I/O")
    monkeypatch.setattr(dashboard.slurm.b, "run", forbidden)
    workbench.restore(dashboard.app, saved)
    gpu_provider_ui.bind(dashboard.app)
    assert dashboard.slurm.gpu_provider == "amd"


def test_managed_provider_modal_explains_existing_telemetry_without_collecting(dashboard):
    app = dashboard.app
    app.cfg.set("raven", {"managed": True, "collection": {"gpu": "existing-telemetry"}})
    before = list(dashboard.slurm.b.calls)
    app.run_command("gpuprovider intel")
    app.run_command("gpuprovider")
    rows = gpu_provider_ui.overlay(dashboard.views, dashboard.store.snapshot(), app, 140, 30)
    text = L.to_text([row for _, _, row in rows], 140)
    assert "existing job telemetry" in text
    assert "do not start automatic GPU probes" in text
    assert "GPU sampling must be enabled" not in text
    assert dashboard.slurm.b.calls == before


def test_first_use_array_menu_opens_loader(dashboard):
    item = next(item for item in toolbar.menu_items(dashboard.app, "Edit") if item.key == "array-map")
    assert item.command == "arraymap"
    dashboard.app.run_command(item.command)
    assert dashboard.app.mode == "arraymap"
    assert dashboard.app.array_manifest_state["view"] == "path"


@pytest.mark.parametrize("mode", ["telemetry", "shell_checks", "arraymap", "gpu_provider"])
@pytest.mark.parametrize("bits", [2, 4, 64, 128, 256, 258])
def test_raw_terminal_events_never_dispatch_to_hidden_panes(dashboard, monkeypatch, mode, bits):
    app = dashboard.app
    # This browser used to intercept wheel events before the controller saw
    # the modal; a press-only test cannot detect that regression.
    app.enter_tab("deps")
    from raven import history_browser
    history_browser._view(app).update(explicit=True, selected="70")
    if mode == "arraymap":
        app.run_command("arraymap")
    else:
        app.mode = mode
    dashboard.draw()
    initial = (app.tab, app.selected_id)
    def forbidden(*args, **kwargs):
        raise AssertionError("A modal pointer event reached the hidden pane")
    monkeypatch.setattr(app, "move", forbidden)
    from raven import chart_interaction, history_browser
    monkeypatch.setattr(chart_interaction, "hover", forbidden)
    monkeypatch.setattr(history_browser, "handle_mouse", forbidden)
    screen._apply_input(app, ("mouse", (0, 2, app.height - 3, 0, bits)), app.last_hits, MOUSE)
    assert (app.tab, app.selected_id) == initial
    assert app.mode == mode


def test_raw_gpu_provider_press_release_changes_only_requested_source(dashboard):
    app = dashboard.app
    app.run_command("gpuprovider")
    dashboard.draw()
    target = next(control for control in app.interaction_state["graph"].controls if control.id == "gpu-provider:intel")
    for bits in (MOUSE.BUTTON1_PRESSED, MOUSE.BUTTON1_RELEASED):
        screen._apply_input(app, ("mouse", (0, target.rect.left, target.rect.top, 0, bits)), app.last_hits, MOUSE)
    assert app.cfg["gpu_provider"] == "intel" and app.mode == "gpu_provider"
    assert app.tab == "jobs" and app.selected_id == "70"


@pytest.mark.parametrize("outcome", [None, CommandError, RuntimeError])
@pytest.mark.parametrize("with_rate", [False, True])
def test_provider_switch_drops_old_retry_delay_and_completion(dashboard, outcome, with_rate):
    sampler = dashboard.sampler
    if with_rate:
        attempt = sampler._metric_attempt(dashboard.store.jobs[0])
        sampler.set_metric_sampling({("resource-series", "70", "gpu:node:0:util", "%", attempt, None, None, None): 100})
    entered, release = threading.Event(), threading.Event()
    health = sampler.health("gpu")
    health.error, health.backoff, health.inflight = "old provider unavailable", 300.0, True
    def source():
        sampler._sampling_targets("gpu", dashboard.store.jobs)
        entered.set()
        assert release.wait(3)
        sampler._sampling_complete("gpu", "70")
        if outcome:
            raise outcome("old provider finished late")
    sampler.sources["gpu"] = source
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(sampler.run_source, "gpu")
        try:
            assert entered.wait(3)
            assert sampler.set_gpu_provider("intel")
            assert health.backoff == 0 and health.error == ""
        finally:
            release.set()
        future.result(timeout=3)
    assert health.backoff == 0 and health.error == "" and not health.inflight
    assert "gpu" not in sampler._baseline_completed
    assert not sampler._metric_completed
    assert sampler.last_run["gpu"] == 0


@pytest.mark.parametrize("mode", ["single", "multi"])
def test_provider_switch_discards_inflight_samples_in_both_worker_modes(mode):
    entered, release = threading.Event(), threading.Event()
    class DeviceSource:
        gpu_provider_generation = 0
        gpu_provider = "auto"
        def set_gpu_provider(self, value):
            if self.gpu_provider == value:
                return False
            self.gpu_provider = value
            self.gpu_provider_generation += 1
            return True
        def gpu(self, job):
            entered.set()
            assert release.wait(3)
            return [GpuSample("node", 0, 90, 1, 2, "old")]
    store, source = Store(persist=False), DeviceSource()
    store.jobs = [Job("70", "running", "gpu", "RUNNING", gpus=1)]
    scheduler = WorkerScheduler(mode=mode)
    sampler = Sampler(source, store, Config()["intervals"], [], worker_scheduler=scheduler)
    pool = ThreadPoolExecutor(max_workers=1)
    future = pool.submit(sampler.src_gpu)
    try:
        assert entered.wait(3)
        assert sampler.set_gpu_provider("amd")
        release.set()
        future.result(timeout=3)
        assert not store.gpu.get("70")
        assert not store.series_of("70")
        assert scheduler.status()["running"] == 0
    finally:
        release.set()
        pool.shutdown(wait=True)
        sampler.shutdown()
        scheduler.shutdown(wait=True, cancel_futures=True)
