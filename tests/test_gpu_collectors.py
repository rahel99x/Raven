"""Vendor fixtures, unsafe attribution, bounded probes, and switching races."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import threading

import pytest

from raven.gpu_collectors import (MAX_BYTES, PROBE_SOURCE, parse_amd, parse_intel,
    parse_nvidia, parse_probe_output, probe_command, provider_name)
from raven.model import GpuSample, Job, Store
from raven.slurm import CommandError, Slurm

FIXTURES = Path(__file__).parent / "fixtures" / "gpu"


def fixture(name):
    return (FIXTURES / name).read_text()


def envelope(vendors=None, *, node="node1", job_id="7", gpus="0", **kwargs):
    return json.dumps(dict(raven_gpu=1, node=node, job_id=job_id, gpus=gpus,
        vendors=vendors if vendors is not None else {"nvidia": {"metrics": "0, 75, 512, 1024, NVIDIA, GPU-a, 0000:01:00.0\n"}},
        **kwargs), separators=(",", ":"))


def test_amd_schema_units_identity_and_unknown():
    first, second = parse_amd(fixture("amd-metric.json"), fixture("amd-list.json"), "n")
    assert (first.vendor, first.uuid, first.bdf, first.partition) == ("amd", "AMD-1111", "0000:01:00.0", "0")
    assert (first.util, first.used, first.total) == (72., 512., 65536.)
    assert second.util is None and second.used == 0 and second.total is None
    assert second.device_key != first.device_key


def test_intel_current_device_counter_not_tile_or_average():
    sample, = parse_intel(fixture("intel-stats.json"), fixture("intel-discovery.json"), "n")
    assert sample.util == 63.5 and sample.used == 1024 and sample.total is None
    assert sample.vendor == "intel" and sample.bdf == "0000:4d:00.0"
    row = json.loads(fixture("intel-stats.json"))
    row["device_level"] = [{"metrics_type": "XPUM_STATS_GPU_UTILIZATION", "avg": 80}]
    sample, = parse_intel(json.dumps(row), fixture("intel-discovery.json"))
    assert sample.util is None and sample.used is None


@pytest.mark.parametrize("bad", [None, True, False, "N/A", "-1", "nan", "inf", "101", {}, []])
def test_utilization_sentinels_never_turn_into_zero(bad):
    row = {"gpu": 0, "usage": {"gfx_activity": bad}, "mem_usage": {"used_vram": "N/A"}}
    sample, = parse_amd(json.dumps([row]))
    assert sample.util is None and sample.used is None


def test_zero_and_quoted_nvidia_names():
    sample, = parse_nvidia('2, 0, 0, 1024, "GPU, special", GPU-2, 0000:02:00.0')
    assert sample.name == "GPU, special" and sample.util == 0 and sample.used == 0
    assert sample.uuid == "GPU-2"


def test_samples_filter_exact_allocation_without_cpu_or_other_gpu_attribution():
    vendors = {"nvidia": {"metrics": "0, 80, 10, 20, GPU, GPU-a\n1, 90, 10, 20, GPU, GPU-b"}}
    samples, _, _ = parse_probe_output(envelope(vendors, gpus="1"), job_id="7")
    assert not samples
    samples, _, _ = parse_probe_output(envelope(vendors, gpus="1", visible={"nvidia": "GPU-b"}), job_id="7")
    assert [s.index for s in samples] == [1]
    samples, _, warnings = parse_probe_output(envelope(vendors, gpus=""), job_id="7")
    assert not samples and "refusing node-wide" in warnings[0]
    with pytest.raises(ValueError, match="allocation identity"):
        parse_probe_output(envelope(vendors), job_id="8")


@pytest.mark.parametrize("job,extra", [("7_0", dict(array_job_id="7", array_task_id="0")),
    ("7+0", dict(het_job_id="7", het_offset="0"))])
def test_exact_array_task_zero_and_heterogeneous_component_identity(job, extra):
    samples, _, _ = parse_probe_output(envelope(job_id="88", **extra), job_id=job)
    assert len(samples) == 1


def test_mixed_vendor_index_zero_never_double_attributes():
    vendors = {"nvidia": {"metrics": "0, 80, 10, 20, GPU, GPU-a"},
        "amd": {"inventory": fixture("amd-list.json"), "metrics": fixture("amd-metric.json")}}
    samples, _, _ = parse_probe_output(envelope(vendors), job_id="7")
    assert not samples
    samples, _, _ = parse_probe_output(envelope(vendors, gpus="AMD-1111"), job_id="7")
    assert len(samples) == 1 and samples[0].vendor == "amd"


def test_interleaved_labels_do_not_cross_nodes():
    raw = "1: " + envelope(node="n1", gpus="0") + "\n0: " + envelope(node="n0", gpus="1")
    samples, _, _ = parse_probe_output(raw, job_id="7")
    assert {sample.node for sample in samples} == {"n0", "n1"}


@pytest.mark.parametrize("raw", ["[" * 3000, "x" * (MAX_BYTES + 1), "null"])
def test_malformed_or_oversized_json_is_bounded(raw):
    with pytest.raises(ValueError):
        parse_amd(raw)


@pytest.mark.parametrize("scope", ["0-1000000000", "0;touch /tmp/oops", "3-1", "0\n1"])
def test_untrusted_allocation_identifiers_cannot_expand_or_execute(scope):
    with pytest.raises(ValueError):
        parse_probe_output(envelope(gpus=scope), job_id="7")


def test_duplicate_node_or_device_replies_are_rejected():
    raw = envelope()
    with pytest.raises(ValueError, match="duplicate"):
        parse_probe_output(raw + "\n" + raw, job_id="7")
    with pytest.raises(ValueError, match="Duplicate"):
        parse_nvidia("0, 1, 1, 2, GPU, same\n1, 1, 1, 2, GPU, same")


def test_stable_identity_used_in_history_without_vendor_collision():
    store = Store(persist=False)
    samples = [GpuSample("n", 0, 20, 1, 2, vendor="amd", uuid="A", partition="0"),
        GpuSample("n", 0, 80, 1, 2, vendor="intel", uuid="I")]
    store.apply_gpu("7", samples)
    assert len(store.gpu_mean) == 2
    assert set(store.series_of("7")[-1]["gpu"]) == {s.device_key for s in samples}
    store.apply_gpu("7", [GpuSample("n", 4, 40, 1, 2, vendor="amd", uuid="A", partition="0")])
    assert store.gpu_mean_of("7:n:amd:A:0") == 30
    legacy = GpuSample("n", 0, 10, 1, 2)
    store.apply_gpu("8", [legacy])
    assert store.gpu_mean_of("8:n:0") == 10


class Backend:
    def __init__(self, raw=None):
        self.raw = envelope() if raw is None else raw
        self.calls = []
    def run(self, cmd, timeout):
        self.calls.append((cmd, timeout))
        return self.raw, 0.


def job():
    return Job("7", "gpu", "gpu", "RUNNING", nodes=1, gpus=1)


def test_provider_switch_invalidates_inflight_reply():
    entered, resume = threading.Event(), threading.Event()
    class Blocking(Backend):
        def run(self, cmd, timeout):
            entered.set()
            assert resume.wait(3)
            return super().run(cmd, timeout)
    slurm = Slurm(Blocking(), "user")
    errors = []
    def run():
        try:
            slurm.gpu(job())
        except CommandError as error:
            errors.append(str(error))
    thread = threading.Thread(target=run)
    thread.start()
    assert entered.wait(3)
    assert slurm.set_gpu_provider("amd")
    assert slurm.gpu_provider_generation == 1
    resume.set()
    thread.join(3)
    assert errors and "stale reply discarded" in errors[0]
    assert not slurm.set_gpu_provider("amd")


def test_no_unscoped_ssh_fallback():
    backend = Backend("0: 0, 75, 10, 20, NVIDIA")
    with pytest.raises(CommandError, match="SSH fallback is disabled"):
        Slurm(backend, "u").gpu(job())
    assert [cmd[0] for cmd, _ in backend.calls] == ["srun"]


@pytest.mark.parametrize("value", ["", "bogus", "auto; id", 2, None])
def test_invalid_provider_rejected_before_execution(value):
    with pytest.raises(ValueError):
        provider_name(value)


def test_inventory_cache_expires_not_slid_by_each_poll(monkeypatch):
    now = [10.]
    monkeypatch.setattr("raven.slurm.time.monotonic", lambda: now[0])
    vendors = {"amd": {"inventory": fixture("amd-list.json"), "metrics": fixture("amd-metric.json")}}
    backend = Backend(envelope(vendors, visible={"amd": "AMD-1111"}))
    slurm = Slurm(backend, "u")
    slurm.gpu(job())
    now[0] = 20.
    slurm.gpu(job())
    assert json.loads(backend.calls[-1][0][-1])["node1"]["amd"]
    now[0] = 41.
    slurm.gpu(job())
    assert json.loads(backend.calls[-1][0][-1]) == {}


def test_probe_executes_fixed_commands_and_bounded_output(tmp_path):
    tool = tmp_path / "nvidia-smi"
    tool.write_text('#!' + sys.executable + '\nprint("0, 0, 0, 1024, GPU, GPU-0, 0000:01:00.0")\n')
    tool.chmod(0o755)
    env = dict(os.environ, PATH=str(tmp_path), SLURM_JOB_ID="7", SLURM_JOB_GPUS="0")
    command = [sys.executable] + probe_command("nvidia", budget=1)[1:]
    proc = subprocess.run(command, capture_output=True, text=True, env=env, timeout=3)
    assert proc.returncode == 0, proc.stderr
    samples, _, errors = parse_probe_output(proc.stdout, job_id="7")
    assert len(samples) == 1 and samples[0].util == 0 and not errors
    tool.write_text('#!' + sys.executable + '\nprint("x" * 400000)\n')
    proc = subprocess.run(command, capture_output=True, text=True, env=env, timeout=3)
    samples, _, errors = parse_probe_output(proc.stdout, job_id="7")
    assert not samples and "output limit exceeded" in errors[0]


def test_probe_missing_utility_never_returns_idle_gpu(tmp_path):
    command = [sys.executable] + probe_command("intel", budget=.2)[1:]
    proc = subprocess.run(command, capture_output=True, text=True,
        env=dict(os.environ, PATH=str(tmp_path), SLURM_JOB_ID="7", SLURM_JOB_GPUS="0"), timeout=3)
    samples, _, errors = parse_probe_output(proc.stdout, job_id="7")
    assert not samples and "xpu-smi: not found" in errors[0]


def test_current_amd_combined_gpu_data_envelope():
    metrics = json.dumps({"gpu_data": json.loads(fixture("amd-metric.json")), "cpu_data": []})
    assert parse_amd(metrics, fixture("amd-list.json"))[0].util == 72


def test_failed_other_vendor_cannot_make_numeric_ids_unambiguous():
    vendors = {"nvidia": {"metrics": "0, 50, 1, 2, GPU, GPU-a"},
        "amd": {"error": "driver unavailable"}}
    samples, _, warnings = parse_probe_output(envelope(vendors), job_id="7")
    assert not samples and any("mixed GPU" in warning for warning in warnings)
    samples, _, _ = parse_probe_output(envelope(vendors, gpus="GPU-a"), job_id="7")
    assert len(samples) == 1


def test_explicit_vendor_filter_does_not_hide_mixed_hardware_scope():
    samples, _, warnings = parse_probe_output(
        envelope(hardware_vendors=["nvidia", "amd"]), job_id="7")
    assert not samples and any("mixed GPU" in warning for warning in warnings)


def test_physical_mig_parent_never_becomes_job_partition_telemetry():
    vendors = {"nvidia": {"metrics": "0, 50, 100, 80000, A100, GPU-a, 0000:01:00.0, Enabled"}}
    samples, _, warnings = parse_probe_output(envelope(vendors), job_id="7")
    assert not samples and any("MIG parent" in warning for warning in warnings)
    inventory, _, _ = parse_probe_output(envelope(vendors), require_scope=False)
    assert inventory[0].partition == "mig-parent"


@pytest.mark.parametrize("vendor", ["amd", "intel"])
def test_real_helper_protocol_with_mock_vendor_executable(tmp_path, vendor):
    audit = tmp_path / "calls.jsonl"
    binary = "amd-smi" if vendor == "amd" else "xpu-smi"
    tool = tmp_path / binary
    inventory = fixture("amd-list.json" if vendor == "amd" else "intel-discovery.json")
    metrics = fixture("amd-metric.json" if vendor == "amd" else "intel-stats.json")
    tool.write_text('#!' + sys.executable + '\nimport json,sys\n'
        + 'with open(' + repr(str(audit)) + ', "a") as f: f.write(json.dumps(sys.argv[1:]) + "\\n")\n'
        + 'print(' + repr(inventory) + ' if sys.argv[1] in ("list", "discovery") else ' + repr(metrics) + ')\n')
    tool.chmod(0o755)
    env = dict(os.environ, PATH=str(tmp_path), SLURM_JOB_ID="7", SLURM_JOB_GPUS="0", ROCR_VISIBLE_DEVICES="AMD-1111")
    proc = subprocess.run([sys.executable] + probe_command(vendor, budget=2)[1:],
        capture_output=True, text=True, env=env, timeout=4)
    assert proc.returncode == 0, proc.stderr
    samples, cache, errors = parse_probe_output(proc.stdout, job_id="7")
    assert len(samples) == 1 and samples[0].vendor == vendor and not errors
    calls = [json.loads(line) for line in audit.read_text().splitlines()]
    assert calls[0] == (["list", "--json"] if vendor == "amd" else ["discovery", "-j"])
    assert calls[1] == (["metric", "--usage", "--mem-usage", "--json"] if vendor == "amd" else ["stats", "-d", "0", "-j"])
    proc = subprocess.run([sys.executable] + probe_command(vendor, budget=2, cache=cache)[1:],
        capture_output=True, text=True, env=env, timeout=4)
    assert proc.returncode == 0, proc.stderr
    calls = [json.loads(line) for line in audit.read_text().splitlines()]
    assert len(calls) == 3 and calls[-1] == calls[1]


def test_helper_deadline_kills_slow_vendor_without_blocking_ui(tmp_path):
    tool = tmp_path / "nvidia-smi"
    tool.write_text('#!' + sys.executable + '\nimport time\ntime.sleep(3)\n')
    tool.chmod(0o755)
    proc = subprocess.run([sys.executable] + probe_command("nvidia", budget=.2)[1:],
        capture_output=True, text=True,
        env=dict(os.environ, PATH=str(tmp_path), SLURM_JOB_ID="7", SLURM_JOB_GPUS="0"), timeout=2)
    assert proc.returncode == 0, proc.stderr
    samples, _, errors = parse_probe_output(proc.stdout, job_id="7")
    assert not samples and "timed out" in errors[0]


def test_allocation_change_cannot_reuse_previous_job_inventory():
    vendors = {"amd": {"inventory": fixture("amd-list.json"), "metrics": fixture("amd-metric.json")}}
    backend = Backend(envelope(vendors, visible={"amd": "AMD-1111"}))
    slurm = Slurm(backend, "u")
    original = job()
    original.start = "2026-10-10T12:00:00"
    slurm.gpu(original)
    original.start = "2026-10-10T13:00:00"
    slurm.gpu(original)
    assert json.loads(backend.calls[-1][0][-1]) == {}


def test_concurrent_allocations_merge_inventory_cache_without_thrashing():
    vendors = {"amd": {"inventory": fixture("amd-list.json"), "metrics": fixture("amd-metric.json")}}
    barrier = threading.Barrier(2)
    class Concurrent(Backend):
        synchronize = True
        def run(self, cmd, timeout):
            self.calls.append((cmd, timeout))
            jid = cmd[cmd.index("--jobid") + 1]
            if self.synchronize:
                barrier.wait(timeout=3)
            return envelope(vendors, job_id=jid, visible={"amd": "AMD-1111"}), 0.
    backend = Concurrent()
    slurm = Slurm(backend, "user")
    first, second = job(), job()
    second.id = "8"
    errors = []
    def collect(item):
        try:
            slurm.gpu(item)
        except Exception as exc:
            errors.append(exc)
    threads = [threading.Thread(target=collect, args=(item,)) for item in (first, second)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(3)
    assert not errors and all(not thread.is_alive() for thread in threads)
    backend.synchronize = False
    slurm.gpu(first)
    slurm.gpu(second)
    assert all(json.loads(cmd[-1])["node1"]["amd"] for cmd, _ in backend.calls[-2:])


def test_invalid_single_counter_does_not_become_valid_zero_in_store():
    sample, = parse_intel('{"device_id":0,"device_level":[{"metrics_type":"XPUM_STATS_GPU_UTILIZATION","value":true}]}')
    store = Store(persist=False)
    store.apply_gpu("7", [sample])
    assert not store.gpu_mean and store.series_of("7")[-1]["gpu"][sample.device_key][0] is None


@pytest.mark.parametrize("unit", [[], {}, False, None, "furlongs", "MiB"])
def test_malformed_memory_units_cannot_crash_worker(unit):
    raw = json.dumps([dict(gpu=0, mem_usage={"used_vram": {"value": 1, "unit": unit}})])
    sample, = parse_amd(raw)
    assert sample.used == (1 if unit == "MiB" else None)


def test_partial_node_reply_reports_coverage_gap():
    samples, _, warnings = parse_probe_output(envelope(), job_id="7", expected_nodes=2)
    assert len(samples) == 1 and any("1/2 node replies" in warning for warning in warnings)


def test_success_exit_vendor_error_payload_is_explained():
    with pytest.raises(ValueError, match="driver permission denied"):
        parse_amd('{"error":"driver permission denied"}')


def test_long_identity_cannot_collide_when_research_bounds_keys():
    a = GpuSample("n" * 250, 0, 20, 1, 2, vendor="amd", uuid="A", partition="0")
    b = GpuSample("n" * 250, 0, 30, 1, 2, vendor="amd", uuid="B", partition="0")
    assert a.device_key != b.device_key and len(a.device_key) <= 128
    assert a.device_key == a.device_key


def test_probe_prefers_slurm_node_name_over_operating_system_hostname(tmp_path):
    tool = tmp_path / "nvidia-smi"
    tool.write_text('#!' + sys.executable + '\nprint("0, 0, 0, 1024, GPU, GPU-0")\n')
    tool.chmod(0o755)
    proc = subprocess.run([sys.executable] + probe_command("nvidia", budget=1)[1:],
        capture_output=True, text=True,
        env=dict(os.environ, PATH=str(tmp_path), SLURM_JOB_ID="7", SLURM_JOB_GPUS="0", SLURMD_NODENAME="slurm-node-alias"), timeout=3)
    samples, _, _ = parse_probe_output(proc.stdout, job_id="7")
    assert samples[0].node == "slurm-node-alias"


@pytest.mark.parametrize("provider", ["auto", "nvidia", "amd", "intel"])
def test_demo_gpu_provider_uses_production_parsers(provider):
    from raven.slurm import FakeBackend
    backend = FakeBackend()
    slurm = Slurm(backend, "alex", gpu_provider=provider)
    gpu_job = next(j for j in slurm.jobs() if j.gpus and j.state == "RUNNING")
    samples = slurm.gpu(gpu_job)
    assert samples and {s.vendor for s in samples} == {"nvidia" if provider == "auto" else provider}
    output, _ = backend.run(["scontrol", "show", "config"])
    assert "JobAcctGatherFrequency = task=30" in output


def test_numeric_custom_gres_subset_never_assumes_vendor_enumeration_order():
    vendors = {"nvidia": {"metrics": "0, 80, 10, 20, GPU, GPU-0\n1, 20, 10, 20, GPU, GPU-1"}}
    samples, _, warnings = parse_probe_output(envelope(vendors, gpus="0"), job_id="7")
    assert not samples and any("UUID/BDF runtime visibility" in warning for warning in warnings)
    samples, _, _ = parse_probe_output(envelope(vendors, gpus="0", visible={"nvidia": "GPU-1"}), job_id="7")
    assert len(samples) == 1 and samples[0].uuid == "GPU-1"


def test_numeric_complete_visible_set_does_not_assume_same_index_numbers():
    vendors = {"nvidia": {"metrics": "3, 80, 10, 20, GPU, GPU-3\n5, 20, 10, 20, GPU, GPU-5"}}
    samples, _, _ = parse_probe_output(envelope(vendors, gpus="0,1"), job_id="7")
    assert {sample.index for sample in samples} == {3, 5}


def test_total_job_allocation_count_rejects_overbroad_step_scope():
    vendors = {"nvidia": {"metrics": "0, 80, 10, 20, GPU, GPU-0\n1, 20, 10, 20, GPU, GPU-1"}}
    slurm = Slurm(Backend(envelope(vendors, gpus="0,1")), "u")
    with pytest.raises(CommandError, match="exceeds.*allocation"):
        slurm.gpu(job())


def test_probe_isolated_from_project_modules_and_python_startup_hooks(tmp_path):
    marker = tmp_path / "unexpected-import"
    payload = 'open(' + repr(str(marker)) + ', "w").write("wrong import")\nraise RuntimeError("project shadow")\n'
    (tmp_path / "json.py").write_text(payload)
    (tmp_path / "sitecustomize.py").write_text(payload)
    command = [sys.executable] + probe_command("nvidia", budget=.2)[1:]
    proc = subprocess.run(command, capture_output=True, text=True, cwd=tmp_path,
        env=dict(os.environ, PATH=str(tmp_path), PYTHONPATH=str(tmp_path),
                 SLURM_JOB_ID="7", SLURM_JOB_GPUS="0"), timeout=2)
    assert proc.returncode == 0, proc.stderr
    assert not marker.exists()
    samples, _, warnings = parse_probe_output(proc.stdout, job_id="7")
    assert not samples and any("not found" in warning for warning in warnings)
