"""GPU collection regressions for per-job Slurm TRES and unavailable counters."""
from types import SimpleNamespace
import json

import pytest

from raven.model import Finished, GpuSample, Job, Store, gres_gpus, gpus_in_tres
from raven.sampler import Sampler
from raven.slurm import (CommandError, GPU_ALLOC_FMT, Slurm, parse_gpu_allocations,
                         parse_gpu_trace, parse_nvsmi)


@pytest.mark.parametrize("resource,kind,count", [
    ("gres/gpu:a100:2", "a100", 2),
    ("gpu:2", "", 2),
    ("gres:gpu:1", "", 1),
    ("gpu:a100:2(S:0-1)", "a100", 2),
    ("gpu:a100:2(IDX:0,1)", "a100", 2),
    ("gres/gpu=2", "", 2),
    ("gres/gpu:a100=2", "a100", 2),
    ("gres/gpu:a100_1g.5gb=2", "a100_1g.5gb", 2),
    ("cpu=8,gres/gpu=3,gres/gpu:a100=1,gres/gpu:a40=2", "mixed", 3),
    ("gres/gpu=2,gres/gpu:a100=2", "a100", 2),
    ("gres/gpu:a100=2,gres/gpu=2", "a100", 2),
    ("gres/gpu:a100=2,gres/gpu:a40=1", "mixed", 3),
    ("mps:100,gpu:a100:2(S:0),shard:50", "a100", 2),
    ("gpu:a100:1,gpu:a40:2", "mixed", 3),
    ("gpu:a100", "a100", 1),
    ("gres/gpu=0,gres/gpu:a100=1", "a100", 0),
    ("gres/gpu:a100=0", "", 0),
    ("gpu:0", "", 0),
    ("gres/gpumem=40960,gres/gpuutil=50", "", 0),
    ("notgpu:3", "", 0),
    ("gpu=-1", "", 0),
    ("gpu:-1", "", 0),
    ("gpu:a100:-1", "", 0),
    ("gpu:a100:bad", "", 0),
    ("N/A", "", 0),
    (None, "", 0),
])
def test_gpu_resource_encodings(resource, kind, count):
    assert gres_gpus(resource) == (kind, count)
    assert gpus_in_tres(resource) == count


def test_allocation_batch_is_exact_and_does_not_borrow_array_siblings():
    assert parse_gpu_allocations(
        "123_1|cpu=8,gres/gpu:a100=2\n"
        "123_2|cpu=8,gres/gpu=1,gres/gpu:a40=1\n"
        "123_[3-5]|gres/gpu=9\n123.batch|gres/gpu=9\n"
        "124|cpu=1\n125|gres/gpu=1\n125|gres/gpu=2\n"
        "126|N/A\n127|tres-alloc\n128|\n") == {
            "123_1": ("a100", 2), "123_2": ("a40", 1), "124": ("", 0)}


def test_allocation_reply_has_a_byte_limit():
    with pytest.raises(CommandError, match="1 MiB"):
        parse_gpu_allocations("é" * (1 << 19) + "x")


def make_sampler(slurm, jobs):
    store = Store(persist=False)
    store.apply_jobs(jobs)
    return Sampler(slurm, store, {}, [], workers=1), store


def test_per_job_gpu_allocation_is_discovered_without_scontrol_or_cpu_probes():
    class Backend:
        calls = []

        def run(self, cmd, timeout):
            self.calls.append(cmd)
            if cmd[0] == "squeue":
                assert cmd == ["squeue", "-u", "alex", "-h", "-O", GPU_ALLOC_FMT]
                return "7|cpu=8,gres/gpu=1,gres/gpu:rtx4090=1\n8|cpu=8\n7_1|gres/gpu=4\n", 0
            assert cmd[0] == "srun" and cmd[cmd.index("--jobid") + 1] == "7"
            return json.dumps(dict(raven_gpu=1, node="desktop", job_id="7", gpus="0", vendors={"nvidia": {"metrics": "0, 75, 2000, 24000, NVIDIA RTX 4090"}}), separators=(",", ":")), 0

    backend = Backend()
    sampler, store = make_sampler(Slurm(backend, "alex"), [
        Job("7", "training", "desktop", "RUNNING", gpus=0),
        Job("8", "cpu-only", "desktop", "RUNNING", gpus=0)])
    try:
        sampler.src_gpu()
        assert store.job("7").gpus == 1 and store.job("7").gpu_type == "rtx4090"
        assert store.job("8").gpus == 0 and "8" not in store.gpu
        assert store.gpu["7"][0].util == 75
        assert [cmd[0] for cmd in backend.calls] == ["squeue", "srun"]
    finally:
        sampler.shutdown()


def test_unsupported_allocation_field_is_probed_once_then_legacy_gpu_still_samples():
    class Backend:
        calls = []

        def run(self, cmd, timeout):
            self.calls.append(cmd)
            if cmd[0] == "squeue":
                raise CommandError("squeue: Invalid job format specification: tres-alloc")
            return json.dumps(dict(raven_gpu=1, node="desktop", job_id="2", gpus="0", vendors={"nvidia": {"metrics": "0, 50, 512, 1024, GPU"}}), separators=(",", ":")), 0

    backend = Backend()
    sampler, store = make_sampler(Slurm(backend, "alex"), [
        Job("1", "cpu-only", "main", "RUNNING"), Job("2", "gpu", "gpu", "RUNNING", gpus=1)])
    try:
        sampler.src_gpu()
        sampler.src_gpu()
        assert sum(cmd[0] == "squeue" for cmd in backend.calls) == 1
        assert store.gpu["2"][0].util == 50
    finally:
        sampler.shutdown()


def test_transient_allocation_failure_is_retained_and_retried():
    class Backend:
        calls = []

        def run(self, cmd, timeout):
            self.calls.append(cmd)
            raise CommandError("squeue: Unable to contact slurm controller")

    backend = Backend()
    sampler, _ = make_sampler(Slurm(backend, "alex"), [Job("1", "gpu", "gpu", "RUNNING")])
    try:
        for _ in range(2):
            with pytest.raises(CommandError, match="Unable to contact slurm controller"):
                sampler.src_gpu()
        assert len(backend.calls) == 2
    finally:
        sampler.shutdown()


def test_successful_exit_with_literal_unsupported_field_is_cached():
    backend = SimpleNamespace(run=lambda cmd, timeout: ("7|tres-alloc\n", 0))
    slurm = Slurm(backend, "alex")
    assert slurm.gpu_allocations() == {}
    backend.run = lambda *args: pytest.fail("Unsupported field must not be probed twice")
    assert slurm.gpu_allocations() == {}


def test_allocation_discovery_cannot_mutate_a_replaced_queue_publication():
    old = Job("1", "old", "gpu", "RUNNING")
    sampler, store = make_sampler(SimpleNamespace(gpu_timeout=.01), [old])
    new = Job("1", "new", "gpu", "RUNNING")

    def discover():
        store.apply_jobs([new])
        return {"1": ("a100", 1)}

    sampler.slurm.gpu_allocations = discover
    sampler.slurm.gpu = lambda job: pytest.fail("Replaced allocation must not be probed")
    try:
        sampler.src_gpu()
        assert old.gpus == new.gpus == 0
    finally:
        sampler.shutdown()


@pytest.mark.parametrize("same_attempt", [False, True])
def test_late_gpu_sample_checks_attempt_but_accepts_normal_queue_refresh(same_attempt):
    old = Job("1", "training", "gpu", "RUNNING", gpus=1, start="2026-10-01T00:00:00")
    sampler, store = make_sampler(SimpleNamespace(gpu_timeout=.01), [old])

    def probe(job):
        refreshed = Job("1", "training", "gpu", "RUNNING", gpus=1,
                        start=old.start if same_attempt else "2026-10-02T00:00:00")
        if not same_attempt:
            store.apply_jobs([])
        store.apply_jobs([refreshed])
        return [GpuSample("node", 0, 75, 512, 1024)]

    sampler.slurm.gpu = probe
    try:
        sampler.src_gpu()
        assert ("1" in store.gpu) is same_attempt
    finally:
        sampler.shutdown()


@pytest.mark.parametrize("counter", ["N/A", "[Not Supported]", "nan", "inf", "-1", "bad"])
def test_unavailable_nvidia_counters_remain_unknown(counter):
    sample, = parse_nvsmi(f"0, {counter}, {counter}, {counter}, GPU\n", labelled=False)
    assert sample.util is sample.used is sample.total is None
    store = Store(persist=False)
    store.apply_gpu("7", [sample])
    assert not store.hist_gpu and not store.gpu_mean
    assert store.series_of("7")[-1]["gpu"]["task0:0"] == [None, None, None]
    trace, = parse_gpu_trace(f"2026/10/01 06:00:01, 0, {counter}, {counter}\n".encode())
    assert trace["util"] is trace["mem"] is None


def test_real_zero_gpu_utilization_and_memory_are_preserved():
    sample, = parse_nvsmi("0, 0, 0, 1024, GPU\n", labelled=False)
    assert sample.util == sample.used == 0 and sample.total == 1024
    store = Store(persist=False)
    store.apply_gpu("7", [sample])
    assert list(store.hist_gpu["7:task0:0"]) == [0]
    assert store.gpu_mean_of("7:task0:0") == 0


def test_unmeasured_gpu_does_not_bias_measured_gpu_average():
    store = Store(persist=False)
    store.apply_gpu("7", [GpuSample("node", 0, None, 512, 1024), GpuSample("node", 1, 80, None, None)])
    assert store.gpu_mean_of("7:node:0") is None
    assert store.gpu_mean_of("7:node:1") == 80
    assert len(store.gpu["7"]) == 2


def test_original_gpu_probe_failure_reaches_source_health_without_unsafe_fallback():
    class Backend:
        def run(self, cmd, timeout):
            if cmd[0] == "srun":
                raise CommandError("srun: Unable to create step: Requested nodes are busy")
            raise CommandError("ssh: Permission denied (publickey)")

    job = Job("7", "gpu", "gpu", "RUNNING", gpus=1, hosts=["node01"])
    with pytest.raises(CommandError) as exc:
        Slurm(Backend(), "alex").gpu(job)
    assert "job 7" in str(exc.value)
    assert "Requested nodes are busy" in str(exc.value)
    assert "SSH fallback is disabled" in str(exc.value)


@pytest.mark.parametrize("source", ["live", "finished", "departed"])
def test_trace_path_uses_exact_record_workdir_without_inspector(source, tmp_path):
    sampler, store = make_sampler(SimpleNamespace(), [])
    job = Job("123_1", "gpu", "gpu", "RUNNING", gpus=1, workdir=str(tmp_path))
    if source == "live":
        store.apply_jobs([job])
    elif source == "finished":
        store.finished = [Finished("123_1", workdir=str(tmp_path))]
    else:
        store.departed_jobs["123_1"] = job
    try:
        assert sampler.trace_path("123_1") == str(tmp_path / "logs/gpu-util-123_1.csv")
        assert sampler.trace_path("123_2") == ""
        assert sampler.trace_path("../../7") == ""
    finally:
        sampler.shutdown()


@pytest.mark.parametrize("source", ["live", "finished", "departed"])
def test_real_trace_in_spaced_workdir_wins_over_truncated_inspection(source, tmp_path):
    workdir = tmp_path / "project with spaces"
    logs = workdir / "logs"
    logs.mkdir(parents=True)
    path = logs / "gpu-util-7.csv"
    path.write_text("2026/10/01 06:00:01, 0, 80, 100\n")
    sampler, store = make_sampler(Slurm(SimpleNamespace(), "alex"), [])
    job = Job("7", "gpu", "gpu", "RUNNING", gpus=1, workdir=str(workdir))
    if source == "live":
        store.apply_jobs([job])
    elif source == "finished":
        store.finished = [Finished("7", workdir=str(workdir))]
    else:
        store.departed_jobs["7"] = job
    store.details["7"] = {"WorkDir": str(tmp_path / "project")}
    sampler.want_trace = "7"
    try:
        assert sampler.trace_path("7") == str(path)
        sampler.src_trace()
        assert store.trace["7"][0]["util"] == 80
        assert sampler.trace_status["7"]["state"] == "ready"
    finally:
        sampler.shutdown()


def test_trace_path_keeps_inspection_fallback_without_record_workdir(tmp_path):
    sampler, store = make_sampler(SimpleNamespace(), [Job("7", "gpu", "gpu", "RUNNING", gpus=1)])
    store.details["7"] = {"WorkDir": str(tmp_path)}
    try:
        assert sampler.trace_path("7") == str(tmp_path / "logs/gpu-util-7.csv")
    finally:
        sampler.shutdown()


@pytest.mark.parametrize("fault,state", [("missing", "missing_file"), ("empty", "empty"), ("error", "error")])
def test_trace_missing_or_unreadable_does_not_keep_old_chart(fault, state, tmp_path):
    files = SimpleNamespace(exists=lambda path: fault != "missing")

    def read(path, selected_files):
        if fault == "error":
            raise PermissionError("GPU trace permission denied")
        return []

    sampler, store = make_sampler(SimpleNamespace(gpu_trace=read), [
        Job("7", "gpu", "gpu", "RUNNING", gpus=1, workdir=str(tmp_path))])
    sampler.files = files
    store.trace["7"] = [{"t": 1, "index": 0, "util": 80, "mem": 100}]
    try:
        if fault == "error":
            with pytest.raises(CommandError, match="GPU trace permission denied"):
                sampler.src_trace()
        else:
            sampler.src_trace()
        assert "7" not in store.trace
        assert sampler.trace_status["7"]["state"] == state
        assert sampler.trace_status["7"]["rows"] == 0
    finally:
        sampler.shutdown()


def test_selected_cpu_metadata_job_can_read_its_gpu_trace(tmp_path):
    rows = [{"t": 1, "index": 0, "util": 80, "mem": 100}]
    files = SimpleNamespace(exists=lambda path: True)
    sampler, store = make_sampler(SimpleNamespace(gpu_trace=lambda path, selected_files: rows), [
        Job("7", "gpu", "main", "RUNNING", gpus=0, workdir=str(tmp_path))])
    sampler.files, sampler.want_trace = files, "7"
    try:
        sampler.src_trace()
        assert store.trace["7"] == rows
        assert sampler.trace_status["7"]["state"] == "ready"
    finally:
        sampler.shutdown()


def cached_sampler(tmp_path, *, count=1):
    """A Slurm --gpus allocation whose per-node queue field remains empty."""
    factory = lambda: Job("7", "training", "desktop", "RUNNING", cpus=8,
                          start="2026-10-01T06:00:00", submit="2026-10-01T05:59:00",
                          nodelist="desktop", workdir=str(tmp_path))
    calls = {"queue": 0, "allocation": 0}

    def queue():
        calls["queue"] += 1
        return [factory()]

    def allocation():
        calls["allocation"] += 1
        return {"7": ("rtx4090", count)}

    slurm = SimpleNamespace(jobs=queue, gpu_allocations=allocation,
                            _gpu_allocations_supported=True, gpu_timeout=.01,
                            gpu=lambda job: [GpuSample("desktop", 0, 80, 512, 1024)],
                            gpu_trace=lambda path, files: [{"t": 1, "index": 0, "util": 80, "mem": 512}])
    sampler, store = make_sampler(slurm, [factory()])
    sampler.intervals["gpu"] = 5
    return sampler, store, calls, factory


def test_positive_allocation_survives_normal_queue_refresh_and_keeps_trace(tmp_path):
    sampler, store, calls, _ = cached_sampler(tmp_path)
    sampler.files = SimpleNamespace(exists=lambda path: True)
    try:
        sampler.src_gpu()
        sampler.src_trace()
        for _ in range(4):
            sampler.src_jobs()
            assert store.job("7").gpus == 1
            sampler.src_trace()
            assert store.trace["7"][0]["util"] == 80
        assert calls == {"queue": 4, "allocation": 1}
        assert len(sampler._gpu_allocation_cache) == 1
    finally:
        sampler.shutdown()


@pytest.mark.parametrize("correction", [2, 0, None])
def test_every_gpu_tick_rechecks_cached_allocation_and_applies_corrections(correction, tmp_path):
    sampler, store, calls, _ = cached_sampler(tmp_path)
    try:
        sampler.src_gpu()
        sampler.src_jobs()

        def fresh():
            calls["allocation"] += 1
            return {"7": ("rtx4090", correction)} if correction is not None else {}

        sampler.slurm.gpu_allocations = fresh
        sampler.src_gpu()
        assert store.job("7").gpus == (correction or 0)
        assert calls["allocation"] == 2
        assert bool(sampler._gpu_allocation_cache) is bool(correction)
        if not correction:
            assert store.gpu["7"] is None
            assert store.series_of("7")[-1]["k"] == "gpu", "Prior measured series must be retained"
    finally:
        sampler.shutdown()


def test_allocation_failures_cannot_extend_short_cache_expiry(monkeypatch, tmp_path):
    from raven import sampler as module
    now = [100.]
    monkeypatch.setattr(module.time, "monotonic", lambda: now[0])
    sampler, store, _, _ = cached_sampler(tmp_path)

    def unavailable():
        raise CommandError("allocation controller disconnected")

    try:
        sampler.src_gpu()
        assert sampler._gpu_allocation_cache["7"]["expires"] == 110
        sampler.slurm.gpu_allocations = unavailable
        for instant in (103, 107):
            now[0] = instant
            sampler.src_jobs()
            assert store.job("7").gpus == 1
            with pytest.raises(CommandError, match="controller disconnected"):
                sampler.src_gpu()
            assert sampler._gpu_allocation_cache["7"]["expires"] == 110
        now[0] = 111
        sampler.src_jobs()
        assert store.job("7").gpus == 0
        assert not sampler._gpu_allocation_cache
    finally:
        sampler.shutdown()


def test_cache_expiry_clears_current_inferred_publication_before_gpu_retry(monkeypatch, tmp_path):
    from raven import sampler as module
    now = [100.]
    monkeypatch.setattr(module.time, "monotonic", lambda: now[0])
    sampler, store, _, _ = cached_sampler(tmp_path)
    try:
        sampler.src_gpu()
        sampler.slurm.gpu_allocations = lambda: (_ for _ in ()).throw(CommandError("allocation controller disconnected"))
        now[0] = 111
        with pytest.raises(CommandError, match="controller disconnected"):
            sampler.src_gpu()
        assert store.job("7").gpus == 0
        assert not sampler._gpu_allocation_cache
    finally:
        sampler.shutdown()


@pytest.mark.parametrize("field,value", [
    ("start", "2026-10-02T06:00:00"), ("submit", "2026-10-02T05:59:00"),
    ("nodes", 2), ("nodelist", "desktop[1-2]"), ("cpus", 16),
    ("mem_req", "64G"), ("state", "PENDING"), ("limit", "2:00:00"),
])
def test_cache_rejects_changed_attempt_identity_or_resources(field, value, tmp_path):
    sampler, store, _, factory = cached_sampler(tmp_path)
    try:
        sampler.src_gpu()
        changed = factory()
        setattr(changed, field, value)
        sampler.slurm.jobs = lambda: [changed]
        sampler.src_jobs()
        assert store.job("7").gpus == 0
        assert not sampler._gpu_allocation_cache
    finally:
        sampler.shutdown()


def test_departure_and_reused_id_remove_cached_gpu_evidence(tmp_path):
    sampler, store, _, factory = cached_sampler(tmp_path)
    try:
        sampler.src_gpu()
        sampler.slurm.jobs = lambda: []
        sampler.src_jobs()
        assert not sampler._gpu_allocation_cache
        sampler.slurm.jobs = lambda: [factory()]
        sampler.src_jobs()
        assert store.job("7").gpus == 0
    finally:
        sampler.shutdown()


def test_authoritative_queue_gpu_change_wins_over_inferred_cache(tmp_path):
    sampler, store, _, factory = cached_sampler(tmp_path)
    try:
        sampler.src_gpu()
        changed = factory()
        changed.gpus, changed.gpu_type = 2, "a100"
        sampler.slurm.jobs = lambda: [changed]
        sampler.src_jobs()
        assert store.job("7").gpus == 2 and store.job("7").gpu_type == "a100"
        assert not sampler._gpu_allocation_cache
    finally:
        sampler.shutdown()


def test_positive_cache_is_bounded_even_with_more_active_jobs(monkeypatch):
    from raven import sampler as module
    assert module.GPU_ALLOCATION_CACHE_MAX == 10000
    monkeypatch.setattr(module, "GPU_ALLOCATION_CACHE_MAX", 2)
    factory = lambda jid: Job(jid, "gpu", "gpu", "RUNNING")
    slurm = SimpleNamespace(jobs=lambda: [factory(str(i)) for i in range(3)],
                            gpu_allocations=lambda: {str(i): ("a100", 1) for i in range(3)},
                            _gpu_allocations_supported=True, gpu_timeout=.01,
                            gpu=lambda job: [GpuSample("node", 0, 80, 512, 1024)])
    sampler, store = make_sampler(slurm, slurm.jobs())
    try:
        sampler.src_gpu()
        assert len(sampler._gpu_allocation_cache) == 2
        sampler.src_jobs()
        assert store.job("0").gpus == 0
        assert store.job("1").gpus == store.job("2").gpus == 1
    finally:
        sampler.shutdown()


def test_positive_cache_expiry_uses_bounded_effective_polling(monkeypatch, tmp_path):
    from raven import sampler as module
    monkeypatch.setattr(module.time, "monotonic", lambda: 100.)
    sampler, _, _, _ = cached_sampler(tmp_path)
    sampler.intervals["gpu"] = 3600
    try:
        sampler.src_gpu()
        # Explicit slow polling is preserved; cached allocation identity still
        # expires at the bounded metadata TTL rather than living for hours.
        assert sampler.effective_interval("gpu") == 3600
        assert sampler._gpu_allocation_cache["7"]["expires"] == 220
    finally:
        sampler.shutdown()
