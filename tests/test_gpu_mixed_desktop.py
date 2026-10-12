"""Hybrid desktop regression: display hardware is not an allocation namespace.

This fixture follows the reported Fedora probe, with synthetic identities.
The NVIDIA utility succeeds; numeric Slurm/CUDA IDs need a stable device match.
"""
import copy
import json

import pytest

from raven import chart_interaction as C, job_panels as J, layout as L
from raven.config import Config
from raven.controller import App
from raven.gpu_collectors import parse_probe_output
from raven.model import Job, Store
from raven.sampler import Sampler
from raven.slurm import Slurm
from raven.views import Views


UUID = "GPU-12345678-1234-5678-1234-567812345678"
OTHER_UUID = "GPU-87654321-4321-8765-4321-876543218765"
BDF = "00000000:01:00.0"


def desktop_reply(*, resolved=False):
    payload = dict(raven_gpu=1, node="desktop", job_id="506", gpus="0",
                   hardware_vendors=["amd", "nvidia"],
                   visible={"nvidia": "0", "amd": ""}, cuda_device_order="",
                   vendors={"nvidia": {"metrics":
                       f"0, 31, 2335, 24564, NVIDIA GeForce RTX 4090, {UUID}, {BDF}, [N/A]\n"}})
    if resolved:
        payload["cuda_visibility"] = dict(
            key=["0", "0", "", [[0, UUID.lower(), BDF, ""]]], uuids=[UUID])
    return payload


def parse(payload, **options):
    return parse_probe_output("0: " + json.dumps(payload, separators=(",", ":")),
                              job_id="506", **options)


def test_reported_fedora_failure_is_reproduced_without_identity_evidence():
    samples, cache, warnings = parse(desktop_reply())
    assert not samples and not cache
    assert any("mixed GPU" in item for item in warnings)


def test_resolved_fedora_device_has_real_counters_without_spurious_backoff_warning():
    samples, cache, warnings = parse(desktop_reply(resolved=True))
    assert len(samples) == 1
    assert (samples[0].uuid, samples[0].util, samples[0].used) == (UUID, 31, 2335)
    assert not warnings
    assert cache["desktop"]["_cuda_visibility"]["uuids"] == [UUID]


def test_stable_environment_identity_needs_no_cuda_probe_or_mixed_warning():
    reply = desktop_reply()
    reply["visible"]["nvidia"] = UUID
    samples, cache, warnings = parse(reply)
    assert [sample.uuid for sample in samples] == [UUID]
    assert not warnings
    assert not cache


@pytest.mark.parametrize("change", ["gpus", "visible", "order", "inventory", "partition"])
def test_old_identity_cache_cannot_be_used_for_new_visibility_or_topology(change):
    reply = desktop_reply(resolved=True)
    if change == "gpus":
        reply["gpus"] = "1"
    elif change == "visible":
        reply["visible"]["nvidia"] = "1"
    elif change == "order":
        reply["cuda_device_order"] = "PCI_BUS_ID"
    elif change == "inventory":
        reply["vendors"]["nvidia"]["metrics"] = reply["vendors"]["nvidia"]["metrics"].replace(UUID, OTHER_UUID)
    else:
        reply["vendors"]["nvidia"]["metrics"] = reply["vendors"]["nvidia"]["metrics"].replace("[N/A]", "Enabled")
    try:
        samples, cache, warnings = parse(reply)
    except ValueError:
        return
    assert not samples and warnings
    assert not cache


@pytest.mark.parametrize("uuids", [[], [OTHER_UUID], [UUID, UUID], [UUID, OTHER_UUID],
                                  ["0"], [None], "GPU-0", None, ["x"] * 257])
def test_invalid_runtime_identity_never_makes_up_device_samples(uuids):
    reply = desktop_reply(resolved=True)
    reply["cuda_visibility"]["uuids"] = uuids
    try:
        samples, cache, warnings = parse(reply)
    except ValueError:
        return
    assert not samples and warnings
    assert not cache


def test_unknown_counter_stays_unknown_after_identity_resolution():
    reply = desktop_reply(resolved=True)
    reply["vendors"]["nvidia"]["metrics"] = reply["vendors"]["nvidia"]["metrics"].replace("0, 31,", "0, N/A,")
    samples, _, warnings = parse(reply)
    assert len(samples) == 1 and samples[0].util is None
    assert not warnings


def test_resolver_error_is_reported_without_attributing_host_gpu():
    reply = desktop_reply()
    reply["cuda_visibility_error"] = "libcuda.so.1: driver unavailable"
    samples, _, warnings = parse(reply)
    assert not samples
    assert any("CUDA visibility" in warning and "driver unavailable" in warning for warning in warnings)


def test_scope_and_stable_identity_disagreement_cannot_select_wrong_device():
    reply = desktop_reply()
    reply["gpus"] = OTHER_UUID
    reply["visible"]["nvidia"] = UUID
    samples, _, _ = parse(reply)
    assert not samples


def test_host_inventory_is_still_not_job_telemetry():
    reply = desktop_reply()
    reply.update(job_id="", gpus="", visible={"nvidia": "", "amd": ""})
    assert len(parse(reply, require_scope=False)[0]) == 1
    with pytest.raises(ValueError, match="allocation identity"):
        parse(reply)


@pytest.mark.parametrize("tab", ["analytics", "jobs"])
@pytest.mark.parametrize("ascii_", [False, True])
def test_sampler_recovers_and_same_dashboard_renders_gpu_curves(monkeypatch, tab, ascii_):
    class Backend:
        payload = desktop_reply()
        calls = []

        def run(self, cmd, timeout):
            self.calls.append(cmd)
            if cmd[0] == "squeue":
                return "506|cpu=4,gres/gpu=1\n", 0.
            assert cmd[0] == "srun"
            return json.dumps(self.payload, separators=(",", ":")), 0.

    backend = Backend()
    store = Store(persist=False)
    store.apply_jobs([Job("506", "training", "local", "RUNNING", gpus=1)])
    store.record("506", {"k": "live", "t": 1., "cpu": .5, "rss": 1024})
    old = copy.deepcopy(list(store.series_of("506")))
    sampler = Sampler(Slurm(backend, "test"), store, {"gpu": 5}, [], workers=1)
    cfg = Config()
    app = App(store, None, None, cfg, "test", interactive=False)
    app.tab, app.selected_id, app.analytics_job = tab, "506", "506"
    views = Views(L.Glyphs(ascii_), cfg)
    app.views_ref = views
    curves = {}

    def curve(app, values, width, height, identity, **options):
        if not options.get("filled"):
            curves[identity[2]] = list(values)
        return []

    monkeypatch.setattr(views, "metric_curve", curve)

    def render():
        curves.clear()
        if tab == "analytics":
            views.analytics_job(store.snapshot(), app, 120, None)
        else:
            state = J.initialize(app)
            state.update(mode="analytics", analytics_view="job")
            C.begin_frame(app, 100, 120)
            J._analytics(views, store.snapshot(), app, store.job("506"), 100, 120, state)

    try:
        sampler.run_source("gpu")
        assert "mixed GPU" in store.health["gpu"].error
        assert store.health["gpu"].backoff > 0
        assert list(store.series_of("506")) == old
        render()
        assert not any(key.startswith("gpu:") for key in curves)

        backend.payload = desktop_reply(resolved=True)
        # Source timing is independently tested; force the next due round here.
        sampler._baseline_completed.clear()
        sampler.run_source("gpu")
        assert store.health["gpu"].error == ""
        assert store.health["gpu"].backoff == 0
        assert list(store.series_of("506"))[:len(old)] == old
        assert store.gpu["506"][0].util == 31
        before_render = len(backend.calls)
        render()
        assert len(backend.calls) == before_render
        assert next(values for key, values in curves.items() if key.endswith(":rate")) == [31.]
        assert next(values for key, values in curves.items() if key.endswith(":busy-mean")) == [31.]
    finally:
        sampler.shutdown()
        if app.research:
            app.research.close()
