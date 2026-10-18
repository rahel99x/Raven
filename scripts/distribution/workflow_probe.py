"""Run only in a clean prototype environment; emit actual imports and outcomes."""
import json
import importlib.util
import os
from pathlib import Path
import resource
import sys
import tempfile
import time

begin = time.perf_counter()
from raven.config import Config
from raven.controller import App
from raven.model import Store
from raven.slurm import Slurm, FakeBackend
from raven.views import Views
from raven.layout import Glyphs
from raven import launch, launch_runtime, run_home, operations
from raven.logs import LogBuffer
from raven.plugins import load as load_plugins
from raven.raven_worker_client import WorkerClient
from raven.raven_execution import ExecutionRouter

with tempfile.TemporaryDirectory(prefix="raven-distribution-probe-") as temporary:
    folder = Path(temporary)
    config_path = folder / "config.json"
    config_path.write_text('{"project":"preserve-user-value"}\n')
    original = config_path.read_bytes()
    store = Store(persist=False)
    scheduler = Slurm(FakeBackend("prototype", speed=0), "prototype")
    store.apply_jobs(scheduler.jobs())
    cfg = Config({"log_lines": 0, "startup_animation": False})
    app = App(store, None, None, cfg, "prototype", interactive=True)
    views = Views(Glyphs(True), cfg)
    app.views_ref = views
    rows, _ = views.compose(store.snapshot(), app, 80, 24)
    assert rows and store.jobs
    script = folder / "science.sh"
    script.write_text('#!/bin/sh\nprintf "prototype log\\n"\n')
    env = {"PATH": os.defpath}
    plan = launch.resolve_plan([str(script), "--project", str(folder), "--local", "--no-cpu-memory", "--no-gpu", "--no-trace", "--no-export-on-finish"], environ=env)
    receipt = launch_runtime.start_local_plan(plan, environ=env)
    inventory = Path(receipt["run_dir"]) / "run.json"
    until = time.monotonic() + 10
    while time.monotonic() < until:
        if inventory.exists() and json.loads(inventory.read_text()).get("state") == "COMPLETED":
            break
        time.sleep(.02)
    home = run_home.load(run_home.receipt_binding(receipt))
    assert home["inventory"]["state"] == "COMPLETED"
    logs = LogBuffer(receipt["logs"]["stdout"], max_bytes=4096)
    logs.refresh()
    assert "prototype log" in "\n".join(logs.lines)
    plugin_file = folder / "compatibility.py"
    plugin_file.write_text('def setup(api):\n    api.command("compatibility", lambda app, args: "ok")\n')
    plugin = load_plugins([str(plugin_file)])
    assert not plugin.errors and plugin.commands["compatibility"][0](None, []) == "ok"
    assert importlib.util.find_spec("pyte") is None, "Probe requires optional terminal dependency absence"
    client = WorkerClient({"site_kind": "desktop", "desktop_explicit": True, "context": {}, "resources": {"memory_mib": 512}})
    try:
        client.start()
        until = time.monotonic() + 8
        while not client.ready and time.monotonic() < until:
            client.poll()
            time.sleep(.01)
        assert client.ready, client.error
        router = ExecutionRouter({"managed": True}, lambda: client)
        result = router.dispatch("log-search", {"text": "hello\nprototype log\n", "needle": "prototype"})
        assert result["result"]["matches"][0]["line"] == 2
    finally:
        client.close()
    assert config_path.read_bytes() == original
    from raven.distribution_profile import profile
    optional = profile()["optional_modules"]
    missing = [name for name in optional if name not in sys.modules]
    mode = sys.argv[1] if len(sys.argv) > 1 else "complete"
    if mode == "core":
        assert len(missing) == len(optional), set(optional) & set(sys.modules)
        from raven import ops_ui
        ops_ui.restore(app, {"pinned": ["statistics"]})
        assert ops_ui.save(app)["pinned"] == ["statistics"], "Missing extensions must preserve saved favorites"
        try:
            operations.specification("statistics")
        except ValueError as exc:
            assert "matching experimental Raven lab wheel" in str(exc)
        else:
            raise AssertionError("Core unexpectedly includes the lab adapter")
    elif mode == "lab":
        from raven.science_stats import compare
        result = compare({"schema": "raven.statistics/v1", "design": "independent", "unit": "run",
            "control": [{"id": "a", "value": 1}, {"id": "b", "value": 2}, {"id": "c", "value": 3}],
            "treatment": [{"id": "d", "value": 4}, {"id": "e", "value": 5}, {"id": "f", "value": 6}]})
        assert result["difference"] == 3
        assert operations.specification("statistics")
    print(json.dumps({"schema": "raven.distribution-workflow-probe/v1", "mode": mode,
        "elapsed_seconds": time.perf_counter() - begin, "peak_process_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        "raven_imports": sorted(name for name in sys.modules if name == "raven" or name.startswith("raven.")),
        "checks": ["80-column fake monitor", "actual local launch", "retained logs", "exact run home", "installed Heavy child", "plugin API", "config preserved"],
        "human_study": False, "live_scheduler": False}))
