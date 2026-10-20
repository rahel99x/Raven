"""GPU checks must explain absent graphs without changing jobs or user state."""
from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import subprocess
import sys

import pytest

from raven import gpu_diagnostics as G
from raven import cli
from raven.config import Config
from raven.remote import LocalFiles, SshBackend
from raven.slurm import Backend, CommandError, GPU_ALLOC_FMT, JOB_FMT, NVSMI, SACCT_FIELDS


ROOT = Path(__file__).resolve().parents[1]
VALID_GPU = "0, 75, 2048, 24576, NVIDIA RTX 4090\n"
TRACE_ROW = "2026/10/08 12:00:00, 0, 25, 100\n"


def queue_row(job="7", *, gres="gres/gpu:rtx4090:1", state="RUNNING",
              workdir="", hosts="desktop", nodes=1):
    return "|".join((job, "training", "desktop", state, "00:01:00", "01:00:00",
                     str(nodes), "8", gres, hosts, "16G", "2026-10-08T12:00:00",
                     "2026-10-08T11:59:00", "None", "100", "", "lab", "normal",
                     "2026-10-08T13:00:00", "/usr/bin/python train.py", workdir)) + "\n"


def finished_row(job="7", *, workdir="", tres="cpu=8,gres/gpu=1"):
    return "|".join((job, "training", "COMPLETED", "00:10:00", "8", "00:20:00",
                     "16G", "4G", "2026-10-08T12:00:00", "2026-10-08T12:10:00",
                     "desktop", "1", "0:0", tres, "desktop", "2026-10-08T11:59:00",
                     workdir, "01:00:00")) + "\n"


class ProbeBackend(Backend):
    """Replay real scheduler text while recording every read-only command."""
    probe_json = False  # This fixture records the legacy command contract.
    def __init__(self, queue=None, *, details=None, host=VALID_GPU, live=None,
                 ssh=None, accounting="", hosts="desktop\n", pci="", errors=None,
                 allocations=""):
        self.queue = queue_row() if queue is None else queue
        self.details = details or "JobId=7 JobState=RUNNING AllocTRES=cpu=8,gres/gpu=1 NodeList=desktop NumNodes=1"
        self.host, self.live = host, "0: " + VALID_GPU if live is None else live
        self.ssh, self.accounting, self.hosts, self.pci = ssh, accounting, hosts, pci
        self.errors = errors or {}
        self.allocations = allocations
        self.calls = []

    def run(self, cmd, timeout=8):
        cmd = list(cmd)
        self.calls.append((cmd, timeout))
        if cmd[0] in self.errors:
            raise CommandError(self.errors[cmd[0]])
        if cmd[:2] == ["sh", "-c"]:
            assert "command -v" in cmd[2]
            return "\n".join(f"{name}|/usr/bin/{name}" for name in G.TOOLS), 0
        if cmd[0] == "python3":
            if "nvidia-smi" in self.errors:
                raise CommandError(self.errors["nvidia-smi"])
            return json.dumps(dict(raven_gpu=1, node="scheduler-host", vendors={"nvidia": {"metrics": self.host}}), separators=(",", ":")), 0
        if cmd[0] == "nvidia-smi":
            return self.host, 0
        if cmd[0] == "lspci":
            return self.pci, 0
        if cmd[0] == "squeue":
            assert "-u" in cmd and cmd[cmd.index("-u") + 1] == "alex"
            if "-O" in cmd:
                assert cmd[cmd.index("-O") + 1] == GPU_ALLOC_FMT
                return self.allocations, 0
            return self.queue, 0
        if cmd[:3] == ["scontrol", "show", "hostnames"]:
            return self.hosts, 0
        if cmd[:3] == ["scontrol", "show", "job"]:
            return self.details, 0
        if cmd[:3] == ["scontrol", "show", "node"]:
            return f"NodeName={cmd[3]} State=MIXED Gres=gpu:rtx4090:1 CfgTRES=cpu=8,gres/gpu=1", 0
        if cmd[0] == "srun":
            if isinstance(self.live, Exception):
                raise self.live
            # The production helper wraps vendor output with verified allocation metadata.
            metrics = "\n".join(line.split(": ", 1)[-1] for line in self.live.splitlines())
            jid = cmd[cmd.index("--jobid") + 1]
            return "0: " + json.dumps(dict(raven_gpu=1, node="desktop", job_id=jid,
                gpus="0", vendors={"nvidia": {"metrics": metrics}}), separators=(",", ":")), 0
        if cmd[0] == "ssh":
            if isinstance(self.ssh, Exception):
                raise self.ssh
            return VALID_GPU if self.ssh is None else self.ssh, 0
        if cmd[0] == "sacct":
            return self.accounting, 0
        pytest.fail(f"Unexpected diagnostic command: {cmd!r}")

    def call(self, cmd, timeout=15):
        pytest.fail(f"GPU checks must never call a job action: {cmd!r}")


def check(backend=None, *, cfg=None, files=None, **kwargs):
    return G.diagnose(cfg or Config(), backend or ProbeBackend(), files or LocalFiles(),
                      user="alex", **kwargs)


def findings(result, code, *, job=None):
    return [item for item in result["checks"] if item["code"] == code
            and (job is None or item.get("job_id") == job)]


def trace_file(tmp_path, data):
    folder = tmp_path / "project" / "logs"
    folder.mkdir(parents=True)
    path = folder / "gpu-util-7.csv"
    path.write_bytes(data)
    return path


def assert_read_only(backend):
    for cmd, timeout in backend.calls:
        assert cmd[0] not in {"sbatch", "scancel", "salloc", "sstat"}
        if cmd[0] == "scontrol":
            assert cmd[1] == "show"
        assert 0 < timeout <= G.DEADLINE


@pytest.mark.parametrize("job,identity", [
    ("7_4", "JobId=19 ArrayJobId=7 ArrayTaskId=4"),
    ("7+1", "JobId=19 HetJobId=7 HetJobOffset=1"),
    ("7_4", "JobId=7_4"),
])
def test_raw_scheduler_ids_retain_exact_array_and_component_identity(job, identity):
    backend = ProbeBackend(queue=queue_row(job), details=identity + " AllocTRES=gres/gpu=1")
    result = check(backend, job_id=job)
    assert not findings(result, "job_details_identity")
    assert findings(result, "live_samples", job=job)
    assert result["jobs"][0]["allocation_fields"]["JobId"] in ("19", "7_4")


@pytest.mark.parametrize("job,identity", [
    ("7_4", "JobId=19 ArrayJobId=7 ArrayTaskId=5"),
    ("7_4", "JobId=19 ArrayJobId=8 ArrayTaskId=4"),
    ("7_4", "JobId=19 ArrayJobId=7 ArrayTaskId=4-5"),
    ("7+1", "JobId=19 HetJobId=7 HetJobOffset=0"),
    ("7+1", "JobId=19 HetJobId=8 HetJobOffset=1"),
    ("7", "JobId=19 ArrayJobId=7 ArrayTaskId=4"),
])
def test_scheduler_metadata_for_other_task_or_component_is_discarded(job, identity):
    backend = ProbeBackend(queue=queue_row(job), details=identity + " AllocTRES=gres/gpu=99")
    result = check(backend, job_id=job)
    assert findings(result, "job_details_identity", job=job)
    assert result["jobs"][0]["allocation_fields"] == {}
    assert result["jobs"][0]["allocated_gpus"] == 1


def test_success_probes_exact_allocation_and_preserves_host_scope():
    backend = ProbeBackend()
    result = check(backend)
    assert result["schema"] == "raven.gpu-check/v1"
    assert result["mode"] == "local" and result["requested_job"] == "all"
    assert result["summary"]["jobs_with_graph_data"] == 1
    assert result["jobs"][0]["sampling_eligible"]
    assert result["jobs"][0]["devices"][0]["util"] == 75
    assert findings(result, "live_samples", job="7")
    assert findings(result, "host_gpu_inventory")[0]["evidence"][0]["node"] == "scheduler-host"
    srun = next(cmd for cmd, _ in backend.calls if cmd[0] == "srun")
    assert srun[srun.index("--jobid") + 1] == "7"
    assert "python3" in srun and "--label" in srun
    assert "SLURM_STEP_GPUS" in srun[srun.index("-c") + 1]
    assert_read_only(backend)


@pytest.mark.parametrize("saved", [True, False])
def test_managed_check_uses_existing_trace_without_host_or_allocation_probes(tmp_path, saved):
    path = trace_file(tmp_path, TRACE_ROW.encode())
    state = tmp_path / "state"
    state.mkdir()
    (state / "ui.json").write_text(json.dumps({"gpu": saved}))
    cfg = Config({"raven": {"managed": True, "collection": {"gpu": "existing-telemetry"}},
                  "gpu_sampling": True})
    backend = ProbeBackend(queue=queue_row(workdir=str(path.parent.parent)))
    result = check(backend, cfg=cfg, state_path=str(state))
    assert result["collection_mode"] == "existing-telemetry"
    assert not result["sampling_enabled"]
    assert not result["jobs"][0]["sampling_eligible"]
    assert result["summary"]["jobs_with_graph_data"] == 1
    assert result["jobs"][0]["trace"]["valid_utilization_rows"] == 1
    assert findings(result, "existing_telemetry", job="7")
    assert not findings(result, "sampling_disabled")
    assert not any(cmd[0] in {"sh", "python3", "nvidia-smi", "lspci", "srun", "ssh"}
                   for cmd, _ in backend.calls)
    assert "allow GPU sampling to run" not in G.render(result)
    assert "existing" in G.render(result).lower()


def test_managed_check_preserves_governed_host_expansion_and_controller_limits(tmp_path):
    from raven.platform_session import wrap_backend
    cfg = Config({"raven": {"managed": True, "collection": {"gpu": "existing-telemetry"}},
                  "collection_cache_dir": str(tmp_path / "cache")})
    raw = ProbeBackend()
    backend = wrap_backend(raw, cfg, {"cluster": "test", "user": "alex"}, None)
    try:
        result = check(backend, cfg=cfg)
        assert result["ok"]
        assert result["collection_mode"] == "existing-telemetry"
        assert findings(result, "trace_workdir_missing", job="7")
        assert not any(cmd[:3] == ["scontrol", "show", "hostnames"] for cmd, _ in raw.calls)
        assert not any(cmd[0] in {"srun", "sh", "python3"} for cmd, _ in raw.calls)
        assert backend.ledger.report()["rates_last_minute"]["controller"] == 2
    finally:
        backend.close()


def test_managed_remote_check_retains_typed_file_admission(tmp_path, monkeypatch):
    from raven.platform_session import wrap_backend
    from raven.raven_collection import wrap_files
    from raven import remote
    class RemoteFixture(LocalFiles):
        remote = True
    path = trace_file(tmp_path, TRACE_ROW.encode())
    cfg = Config({"raven": {"managed": True, "collection": {"gpu": "existing-telemetry"}},
                  "collection_cache_dir": str(tmp_path / "cache")})
    raw = ProbeBackend(queue=queue_row(workdir=str(path.parent.parent)))
    backend = wrap_backend(raw, cfg, {"cluster": "test", "user": "alex"}, None)
    files = wrap_files(RemoteFixture(), backend)
    monkeypatch.setattr(remote, "RemoteFiles", lambda *_a, **_kw: pytest.fail("Managed file admission was replaced"))
    try:
        result = check(backend, cfg=cfg, files=files)
        assert result["mode"] == "remote"
        assert result["jobs"][0]["trace"]["valid_utilization_rows"] == 1
        rates = backend.ledger.report()["rates_last_minute"]
        assert rates["filesystem"] == 2
        assert rates["file-bytes"] == len(TRACE_ROW.encode())
    finally:
        backend.close()


@pytest.mark.parametrize("tres", ["cpu=8,gres/gpu=1", "cpu=8,gres/gpu:rtx4090=1",
                                 "cpu=8,gres/gpu=2,gres/gpu:a100=2"])
def test_per_job_allocation_explains_legacy_queue_count_and_probes(tres):
    backend = ProbeBackend(queue_row(gres="N/A"), details=f"JobId=7 AllocTRES={tres}")
    result = check(backend)
    assert findings(result, "queue_gpu_underreported", job="7")
    assert result["jobs"][0]["allocated_gpus"] >= 1
    assert result["jobs"][0]["graph_samples"] == 1
    assert not findings(result, "allocation_missing")


def test_batch_gpu_allocation_matches_the_production_discovery_path():
    backend = ProbeBackend(queue_row(gres="N/A"), allocations="7|cpu=8,gres/gpu:rtx4090=2\n")
    result = check(backend)
    evidence = findings(result, "allocation_batch")[0]["evidence"]
    assert evidence == [{"job_id": "7", "queue_gpus": 0, "allocated_gpus": 2}]
    assert result["jobs"][0]["allocated_gpus"] == 2
    assert result["summary"]["jobs_with_graph_data"] == 1
    assert not findings(result, "queue_gpu_underreported")


def test_saved_on_overrides_config_off_but_cli_off_has_priority(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    (state / "ui.json").write_text('{"gpu":true}')
    cfg = Config({"gpu_sampling": False})
    assert check(cfg=cfg, state_path=str(state))["sampling_enabled"]
    assert not check(cfg=cfg, state_path=str(state), no_gpu=True)["sampling_enabled"]


def test_host_gpu_cannot_supply_a_cpu_job_graph():
    backend = ProbeBackend(queue_row(gres="N/A"), details="JobId=7 AllocTRES=cpu=8")
    result = check(backend)
    assert findings(result, "host_gpu_inventory")[0]["status"] == "ok"
    assert findings(result, "allocation_missing", job="7")
    assert not result["jobs"][0]["sampling_eligible"]
    assert result["summary"]["jobs_with_graph_data"] == 0
    assert not any(cmd[0] in {"srun", "ssh"} for cmd, _ in backend.calls)


@pytest.mark.parametrize("mode", ["config", "cli", "saved"])
def test_sampling_preferences_explain_off_without_sampling_or_writes(tmp_path, mode):
    state = tmp_path / "state"
    state.mkdir()
    ui = state / "ui.json"
    ui.write_text(json.dumps({"gpu": None if mode == "config" else mode != "saved", "theme": "keep-me"}))
    before = ui.read_bytes()
    backend = ProbeBackend()
    result = check(backend, cfg=Config({"gpu_sampling": mode != "config"}),
                   no_gpu=mode == "cli", state_path=str(state))
    assert not result["sampling_enabled"]
    assert findings(result, "sampling_disabled", job="7")
    assert findings(result, "sampling_settings")[0]["status"] == "warning"
    assert not any(cmd[0] in {"srun", "ssh"} for cmd, _ in backend.calls)
    assert ui.read_bytes() == before
    assert list(state.iterdir()) == [ui]


def test_no_state_ignores_saved_off_and_never_creates_state(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    (state / "ui.json").write_text('{"gpu":false}')
    result = check(state_path=str(state), no_state=True)
    assert result["sampling_enabled"]
    assert not findings(result, "saved_sampling")
    missing = tmp_path / "missing"
    check(state_path=str(missing))
    assert not missing.exists()


def test_saved_ui_read_is_bounded_and_does_not_modify_oversized_content(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    path = state / "ui.json"
    data = b" " * ((1 << 20) + 64)
    path.write_bytes(data)
    result = check(state_path=str(state))
    assert findings(result, "saved_sampling")[0]["status"] == "warning"
    assert "read limit" in findings(result, "saved_sampling")[0]["detail"]
    assert path.read_bytes() == data and result["sampling_enabled"]


@pytest.mark.parametrize("content", ["not-json", "[]", "{\"gpu\":\"false\"}", "{}"])
def test_unusable_saved_preferences_do_not_crash_or_disable_valid_config(tmp_path, content):
    state = tmp_path / "state"
    state.mkdir()
    path = state / "ui.json"
    path.write_text(content)
    result = check(state_path=str(state))
    assert result["sampling_enabled"]
    assert path.read_text() == content


def test_no_jobs_is_explicit_and_never_uses_inventory_as_graph_data():
    backend = ProbeBackend(queue="")
    result = check(backend)
    assert findings(result, "no_current_jobs")
    assert not result["jobs"]
    assert result["summary"]["jobs_checked"] == result["summary"]["jobs_with_graph_data"] == 0
    assert not any(cmd[0] in {"sacct", "srun", "ssh"} for cmd, _ in backend.calls)


def test_exact_completed_job_uses_matching_accounting_and_trace(tmp_path):
    path = trace_file(tmp_path, TRACE_ROW.encode())
    backend = ProbeBackend(queue=queue_row("8"), accounting=
                           finished_row("7", workdir=str(path.parent.parent)) + finished_row("70"))
    result = check(backend, job_id="7")
    assert [item["job_id"] for item in result["jobs"]] == ["7"]
    assert result["jobs"][0]["state"] == "COMPLETED"
    assert result["jobs"][0]["trace"]["valid_utilization_rows"] == 1
    assert findings(result, "not_running", job="7")
    assert not any(cmd[0] in {"srun", "ssh"} for cmd, _ in backend.calls)
    accounting = next(cmd for cmd, _ in backend.calls if cmd[0] == "sacct")
    assert accounting == ["sacct", "-u", "alex", "-j", "7", "-n", "-P", "-o", SACCT_FIELDS]


def test_exact_id_is_not_replaced_by_array_sibling_or_prefix():
    result = check(ProbeBackend(queue_row("7_2"), accounting=finished_row("7_20")), job_id="7_1")
    assert findings(result, "job_not_found", job="7_1")
    assert not result["ok"] and not result["jobs"]


@pytest.mark.parametrize("job", ["7_[1-9]", "7.batch", "../../escape", "7\x1b[2J"])
def test_nonindividual_or_unsafe_queue_ids_cannot_be_probed_or_read_as_files(job):
    backend = ProbeBackend(queue_row(job))
    result = check(backend)
    assert not result["jobs"]
    if job == "7_[1-9]":
        assert findings(result, "nonindividual_queue_ids")
    else:
        assert findings(result, "queue") and not result["ok"]
        assert "parse coverage" in G.render(result)
    assert not any(cmd[0] in {"srun", "ssh", "sacct"} or cmd[:3] == ["scontrol", "show", "job"]
                   for cmd, _ in backend.calls)
    assert "\x1b" not in G.render(result)


@pytest.mark.parametrize("state", ["PENDING", "COMPLETING", "SUSPENDED", "CONFIGURING"])
def test_inactive_allocations_are_not_live_probed(state):
    backend = ProbeBackend(queue_row(state=state))
    result = check(backend)
    assert findings(result, "not_running", job="7")
    assert not any(cmd[0] in {"srun", "ssh"} for cmd, _ in backend.calls)


@pytest.mark.parametrize("counter", ["N/A", "[Not Supported]", "NaN", "inf", "-1", "101", "bad"])
def test_unsupported_utilization_is_detected_but_not_zero_graph_data(counter):
    result = check(ProbeBackend(live=f"0: 0, {counter}, 2048, 24576, GPU\n"))
    assert result["jobs"][0]["devices"][0]["util"] is None
    assert findings(result, "utilization_unsupported", job="7")
    assert result["summary"]["jobs_with_graph_data"] == 0


def test_genuine_zero_utilization_is_graph_data():
    result = check(ProbeBackend(live="0: 0, 0, 0, 24576, GPU\n"))
    assert result["jobs"][0]["devices"][0]["util"] == 0
    assert result["summary"]["jobs_with_graph_data"] == 1


def test_srun_failure_preserves_reason_without_unsafe_ssh_fallback():
    backend = ProbeBackend(live=CommandError("srun: step creation temporarily disabled"),
                           ssh=CommandError("ssh: Permission denied (publickey)."))
    result = check(backend)
    problem = [item for item in result["checks"] if item["status"] == "error" and item.get("job_id") == "7"]
    assert problem and "step creation" in problem[0]["detail"] and "SSH fallback is disabled" in problem[0]["detail"]
    failures = {record["argv"][0]: record for record in result["commands"] if record["status"] == "error"}
    assert "step creation" in failures["srun"]["error"]
    assert "ssh" not in failures
    assert result["summary"]["jobs_with_graph_data"] == 0


def test_srun_failure_cannot_use_node_inventory_as_job_telemetry():
    backend = ProbeBackend(live=CommandError("srun: step creation temporarily disabled"))
    result = check(backend)
    assert "devices" not in result["jobs"][0]
    assert not findings(result, "live_samples", job="7")
    assert any(item["argv"][0] == "srun" and item["status"] == "error" for item in result["commands"])
    assert not result["ok"]


@pytest.mark.parametrize("detail,code", [("NVIDIA-SMI has failed because it couldn't communicate with the NVIDIA driver", "driver_unavailable"),
                                        ("nvidia-smi: command not found", "command_missing"),
                                        ("invalid job id; no allocation", "allocation_ended"),
                                        ("srun: timed out after 12s", "probe_timeout")])
def test_probe_failure_classification_retains_original_reason(detail, code):
    result = check(ProbeBackend(live=CommandError(detail), ssh=CommandError(detail)))
    assert findings(result, code, job="7")
    assert detail in findings(result, code, job="7")[0]["detail"]


def test_mixed_desktop_gpu_mapping_failure_explains_identity_and_preserves_probe():
    """A successful NVIDIA command can still lack safe allocation identity."""
    payload = dict(raven_gpu=1, node="desktop", job_id="7", gpus="0",
                   hardware_vendors=["amd", "nvidia"],
                   visible={"nvidia": "0", "amd": ""},
                   vendors={"nvidia": {"metrics":
                       "0, 31, 2335, 24564, NVIDIA RTX 4090, "
                       "GPU-779bcac9-8a24-3cb2-e70e-e164e94d360e, 00000000:01:00.0, [N/A]\n"}})
    reply = "0: " + json.dumps(payload, separators=(",", ":"))

    class MixedDesktopBackend(ProbeBackend):
        def run(self, cmd, timeout=8):
            if cmd[0] == "srun":
                self.calls.append((list(cmd), timeout))
                return reply, 0
            return super().run(cmd, timeout)

    backend = MixedDesktopBackend()
    result = check(backend, job_id="7")
    finding, = findings(result, "allocation_mapping_unavailable", job="7")
    assert finding["status"] == "error"
    assert "mixed GPU vendor indices" in finding["detail"]
    assert "CUDA_VISIBLE_DEVICES" in finding["next_step"]
    assert "libcuda.so.1" in finding["next_step"]
    assert "does not require an AMD utility" in finding["next_step"]
    assert result["summary"]["jobs_with_graph_data"] == 0
    assert not findings(result, "driver_unavailable")
    recorded, = [item for item in result["commands"] if item["argv"][0] == "srun"]
    assert recorded["status"] == "ok" and recorded["stdout"] == reply
    assert_read_only(backend)


@pytest.mark.parametrize("reason", [
    "CUDA visibility resolution unavailable: libcuda.so.1: cannot open shared object file: No such file or directory",
    "CUDA visibility resolution unavailable: CUDA driver could not resolve ordinal 0",
    "numeric Slurm GPU IDs do not prove a complete visible device set",
    "Slurm GPU allocation identifiers unavailable; refusing node-wide attribution",
    "GPU probe allocation identity does not match requested job",
    "GPU device count exceeds the requested job allocation; refusing attribution",
])
def test_identity_diagnostic_retains_resolution_reason_instead_of_generic_error(reason):
    result = check(ProbeBackend(live=CommandError(reason)), job_id="7")
    finding, = findings(result, "allocation_mapping_unavailable", job="7")
    assert reason in finding["detail"]
    assert "resolved device UUIDs" in finding["next_step"]
    assert "Do not change device visibility to include unallocated GPUs" in finding["next_step"]
    assert not findings(result, "sampling_failed")


def test_non_nvidia_hardware_explains_sampler_scope():
    result = check(ProbeBackend(queue="", pci="01:00.0 VGA compatible controller: AMD Navi [1002:744c]\n",
                                errors={"nvidia-smi": "nvidia-smi: not found"}))
    assert findings(result, "pci_gpu_inventory")
    assert findings(result, "gpu_provider_setup")
    assert "amd, intel" in findings(result, "gpu_provider_setup")[0]["next_step"]


def test_large_queue_and_node_lists_stay_within_detailed_probe_limits():
    queue = "".join(queue_row(str(i + 1), nodes=12, hosts="desktop[01-12]") for i in range(20))
    nodes = "\n".join(f"desktop{i:02}" for i in range(12))
    backend = ProbeBackend(queue, hosts=nodes, live=CommandError("srun failed"), ssh=CommandError("ssh failed"))
    result = check(backend)
    assert result["summary"]["jobs_checked"] == G.MAX_JOBS
    assert result["summary"]["jobs_omitted"] == 20 - G.MAX_JOBS
    assert findings(result, "job_limit")
    assert sum(cmd[0] == "ssh" for cmd, _ in backend.calls) <= G.MAX_JOBS * G.MAX_NODES
    assert sum(cmd[:3] == ["scontrol", "show", "node"] for cmd, _ in backend.calls) <= G.MAX_NODES
    assert len(result["commands"]) <= G.MAX_COMMANDS


def test_missing_trace_identifies_exact_expected_path(tmp_path):
    workdir = tmp_path / "no logs yet"
    result = check(ProbeBackend(queue_row(workdir=str(workdir)), details=f"JobId=7 WorkDir={workdir}"))
    expected = str(workdir / "logs" / "gpu-util-7.csv")
    assert result["jobs"][0]["trace"]["path"] == expected
    assert findings(result, "trace_unavailable", job="7")
    assert expected in findings(result, "trace_unavailable", job="7")[0]["detail"]


@pytest.mark.parametrize("data,rows,valid", [(b"", 0, 0), (b"not a GPU CSV\n", 0, 0),
                                          (b"2026/10/08 12:00:00, 0, N/A, N/A\n", 1, 0),
                                          (TRACE_ROW.encode(), 1, 1),
                                          ((TRACE_ROW + "2026/10/08 12:00:01, 0, 0, 0\n").encode(), 2, 2)])
def test_trace_content_has_truthful_counts_and_cannot_invent_unknown_values(tmp_path, data, rows, valid):
    path = trace_file(tmp_path, data)
    backend = ProbeBackend(queue_row(workdir=str(path.parent.parent)), live="0: 0, N/A, 1, 2, GPU\n")
    result = check(backend)
    trace = result["jobs"][0]["trace"]
    assert trace["bytes"] == len(data) and trace["rows"] == rows
    assert trace["valid_utilization_rows"] == valid
    assert bool(result["summary"]["jobs_with_graph_data"]) is bool(valid)
    assert findings(result, "trace_read", job="7")[0]["status"] == ("ok" if valid else "warning")


def test_trace_tail_is_bounded_and_discards_partial_boundary_line(tmp_path, monkeypatch):
    monkeypatch.setattr(G, "MAX_TRACE", 128)
    data = b"X" * 1024 + b"\n" + TRACE_ROW.encode()
    path = trace_file(tmp_path, data)

    class TrackingFiles(LocalFiles):
        reads = []

        def read(self, path, offset, length):
            self.reads.append((path, offset, length))
            return super().read(path, offset, length)

    files = TrackingFiles()
    result = check(ProbeBackend(queue_row(workdir=str(path.parent.parent))), files=files)
    assert result["jobs"][0]["trace"]["rows"] == 1
    assert files.reads == [(str(path), len(data) - 128, 128)]
    assert result["jobs"][0]["trace"]["bytes"] == len(data)
    assert path.read_bytes() == data


def test_trace_path_must_be_regular_and_cannot_read_a_directory(tmp_path):
    folder = tmp_path / "project" / "logs" / "gpu-util-7.csv"
    folder.mkdir(parents=True)
    result = check(ProbeBackend(queue_row(workdir=str(folder.parent.parent))))
    assert findings(result, "trace_unavailable", job="7")
    assert "regular file" in findings(result, "trace_unavailable", job="7")[0]["detail"]


@pytest.mark.parametrize("reading,expected", [([50, 1, 2], 1), ([0, 0, 2], 1),
                                             ([None, 1, 2], 0), (["50", 1, 2], 0),
                                             ([True, 1, 2], 0), ([101, 1, 2], 0),
                                             ([], 0), ({}, 0), ("bad", 0)])
def test_retained_gpu_payloads_only_count_real_graph_data_and_never_write(tmp_path, reading, expected):
    state = tmp_path / "state"
    series = state / "series"
    series.mkdir(parents=True)
    path = series / "7.jsonl"
    path.write_text("not json\n[]\n" + json.dumps({"t": 1., "k": "gpu", "gpu": {"desktop:0": reading}}) + "\n{truncated")
    original = path.read_bytes()
    result = check(ProbeBackend(live="0: 0, N/A, 1, 2, GPU\n"), state_path=str(state))
    assert result["summary"]["jobs_with_graph_data"] == 0
    assert result["jobs"][0]["legacy_measured_gpu_rows"] == expected
    assert result["jobs"][0]["retained_attribution"] == "legacy-ambiguous"
    assert findings(result, "retained_samples", job="7")
    assert path.read_bytes() == original
    assert sorted(str(p.relative_to(state)) for p in state.rglob("*")) == ["series", "series/7.jsonl"]


def test_retained_malformed_timestamps_and_json_values_do_not_crash(tmp_path):
    state = tmp_path / "state"
    (state / "series").mkdir(parents=True)
    data = [{"t": [], "k": "gpu", "gpu": {"desktop:0": [50, 1, 2]}},
            {"t": "bad", "k": "gpu", "gpu": {"desktop:0": [50, 1, 2]}},
            {"t": True, "k": "gpu", "gpu": {"desktop:0": [50, 1, 2]}},
            {"t": 2, "k": "live"}, {"t": 3, "k": "gpu", "gpu": []}]
    path = state / "series" / "7.jsonl"
    path.write_text("\n".join(json.dumps(row) for row in data) + "\n")
    result = check(ProbeBackend(live="0: 0, N/A, 1, 2, GPU\n"), state_path=str(state))
    assert result["summary"]["jobs_with_graph_data"] == 0
    assert findings(result, "retained_samples", job="7")


def test_retained_nonfinite_and_huge_integer_values_are_unmeasured(tmp_path):
    state = tmp_path / "state"
    (state / "series").mkdir(parents=True)
    rows = [{"t": float("nan"), "k": "gpu", "gpu": {"desktop:0": [50, 1, 2]}},
            {"t": float("inf"), "k": "gpu", "gpu": {"desktop:0": [50, 1, 2]}},
            {"t": 10 ** 1000, "k": "gpu", "gpu": {"desktop:0": [50, 1, 2]}},
            {"t": 1., "k": "gpu", "gpu": {"desktop:0": [float("nan"), 1, 2]}},
            {"t": 2., "k": "gpu", "gpu": {"desktop:0": [10 ** 1000, 1, 2]}}]
    (state / "series" / "7.jsonl").write_text("\n".join(json.dumps(row) for row in rows))
    result = check(ProbeBackend(live="0: 0, N/A, 1, 2, GPU\n"), state_path=str(state))
    assert result["summary"]["jobs_with_graph_data"] == 0
    assert result["jobs"][0]["retained_valid_gpu_rows"] == 0
    json.dumps(result, allow_nan=False)


def test_large_retained_cache_reads_only_a_bounded_regular_file_tail(tmp_path, monkeypatch):
    monkeypatch.setattr(G, "MAX_SERIES", 128)
    state = tmp_path / "state"
    (state / "series").mkdir(parents=True)
    path = state / "series" / "7.jsonl"
    last = json.dumps({"t": 10., "k": "gpu", "gpu": {"desktop:0": [80, 1, 2]}}).encode() + b"\n"
    original = b"X" * 1024 + b"\n" + last
    path.write_bytes(original)
    reads = []

    class TrackingFiles(LocalFiles):
        def read(self, path, offset, length):
            reads.append((path, offset, length))
            return super().read(path, offset, length)

    monkeypatch.setattr(G, "LocalFiles", TrackingFiles)
    result = check(ProbeBackend(live="0: 0, N/A, 1, 2, GPU\n"), state_path=str(state))
    assert reads == [(str(path), len(original) - 128, 128)]
    retained = findings(result, "retained_samples", job="7")[0]
    assert retained["evidence"]["tail_only"] and retained["evidence"]["inspected_bytes"] <= 128
    assert result["jobs"][0]["retained_valid_gpu_rows"] == 0
    assert result["jobs"][0]["legacy_measured_gpu_rows"] == 1
    assert path.read_bytes() == original


def test_retained_directory_is_reported_without_attempting_to_read(tmp_path):
    state = tmp_path / "state"
    (state / "series" / "7.jsonl").mkdir(parents=True)
    result = check(state_path=str(state))
    assert findings(result, "retained_samples", job="7")[0]["status"] == "warning"
    assert "regular file" in findings(result, "retained_samples", job="7")[0]["detail"]


def test_allocation_details_for_a_different_job_are_not_borrowed():
    backend = ProbeBackend(queue_row(gres="N/A"), details="JobId=70 AllocTRES=gres/gpu=9")
    result = check(backend)
    assert findings(result, "job_details_identity", job="7")
    assert result["jobs"][0]["allocated_gpus"] == 0
    assert not any(cmd[0] in {"srun", "ssh"} for cmd, _ in backend.calls)


def test_queue_error_is_reported_and_does_not_start_sampling():
    backend = ProbeBackend(errors={"squeue": "squeue: Unable to contact slurm controller"})
    result = check(backend)
    assert not result["ok"] and findings(result, "queue")[0]["status"] == "error"
    assert "Unable to contact slurm controller" in G.render(result)
    assert not any(cmd[0] in {"srun", "ssh"} for cmd, _ in backend.calls)


def test_native_evidence_preserves_exact_stderr_argv_and_returncode(monkeypatch):
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 17, "stdout evidence\n", "driver permission denied\nsecond exact line\n")

    monkeypatch.setattr(G.subprocess, "run", run)
    backend = G.EvidenceBackend(Backend())
    argv = ["nvidia-smi", "literal $(touch must-not-run)"]
    with pytest.raises(CommandError, match="driver permission denied"):
        backend.run(argv, 3)
    assert calls[0][0] == argv and not calls[0][1].get("shell", False)
    record, = backend.records
    assert record["argv"] == argv and record["returncode"] == 17
    assert record["stdout"] == "stdout evidence\n"
    assert record["stderr"] == "driver permission denied\nsecond exact line\n"


def test_native_timeout_preserves_both_partial_byte_streams(monkeypatch):
    def timeout(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"], output=b"partial output\n", stderr=b"partial failure\n")

    monkeypatch.setattr(G.subprocess, "run", timeout)
    backend = G.EvidenceBackend(Backend())
    with pytest.raises(CommandError, match="timed out"):
        backend.run(["nvidia-smi"], 2)
    assert backend.records[0]["status"] == "timeout"
    assert backend.records[0]["stdout"] == "partial output\n"
    assert backend.records[0]["stderr"] == "partial failure\n"


def test_ssh_evidence_preserves_native_error_stream_and_connection_argv():
    attempts = []

    def runner(argv, timeout):
        attempts.append((argv, timeout))
        return 255, "", "ssh: connect to host desktop port 22: Connection refused\n"

    backend = G.EvidenceBackend(SshBackend("desktop", user="alex", runner=runner, control=False))
    with pytest.raises(CommandError, match="Connection refused"):
        backend.run(["nvidia-smi"], 4)
    assert attempts[0][0][-3:] == ["--", "alex@desktop", "nvidia-smi"]
    record, = backend.records
    assert record["argv"] == ["nvidia-smi"] and record["returncode"] == 255
    assert record["stderr"] == "ssh: connect to host desktop port 22: Connection refused\n"
    assert attempts[0][1] <= G.DEADLINE


def test_evidence_deadline_stops_commands_before_execution(monkeypatch):
    now = [100.]
    monkeypatch.setattr(G.time, "monotonic", lambda: now[0])
    inner = ProbeBackend(queue="")
    backend = G.EvidenceBackend(inner, duration=2)
    now[0] = 101.5
    backend.run(NVSMI, 99)
    assert inner.calls[-1][1] == .5
    now[0] = 102.
    with pytest.raises(CommandError, match="budget exhausted"):
        backend.run(NVSMI)
    assert len(inner.calls) == 1 and backend.records[-1]["status"] == "skipped"


def test_evidence_output_and_command_counts_are_bounded(monkeypatch):
    monkeypatch.setattr(G, "MAX_STREAM", 32)
    monkeypatch.setattr(G, "MAX_EVIDENCE", 64)
    monkeypatch.setattr(G, "MAX_COMMANDS", 3)
    backend = G.EvidenceBackend(ProbeBackend(host="é" * 100))
    for _ in range(3):
        backend.run(NVSMI)
    with pytest.raises(CommandError, match="budget exhausted"):
        backend.run(NVSMI)
    assert len(backend.records) == 3 and backend.omitted == 1
    assert all(row["stdout_bytes"] == 200 and row["stdout_truncated"] for row in backend.records)
    assert sum(len(row[stream].encode()) for row in backend.records for stream in ("stdout", "stderr")) <= 64


def test_native_executable_error_is_recorded_without_slurm_installation(tmp_path):
    script = tmp_path / "driver-check"
    script.write_text(f"#!{sys.executable}\nimport sys\nprint('ordinary output')\nprint('NVML library mismatch: exact reason', file=sys.stderr)\nraise SystemExit(9)\n")
    script.chmod(0o700)
    backend = G.EvidenceBackend(Backend())
    with pytest.raises(CommandError, match="NVML library mismatch"):
        backend.run([str(script)])
    assert backend.records[0]["returncode"] == 9
    assert backend.records[0]["stderr"] == "NVML library mismatch: exact reason\n"


def test_render_blocks_terminal_controls_in_all_untrusted_fields():
    result = check(ProbeBackend(errors={"nvidia-smi": "bad\x1b[31mred\x07bell\rreplace"}))
    result["user"] = "alex\x1b[2J"
    result["profile"] = "fedora\x07"
    text = G.render(result)
    assert all(char.isprintable() or char == "\n" for char in text)
    assert "\x1b" not in text and "\x07" not in text and "\r" not in text


def test_report_directory_and_files_are_private_and_retain_all_artifacts(tmp_path):
    result = check()
    target = tmp_path / "diagnostic"
    assert G.save(result, target) == str(target)
    assert stat.S_IMODE(target.stat().st_mode) == 0o700
    assert {path.name for path in target.iterdir()} == {"report.json", "report.txt", "commands.jsonl"}
    for path in target.iterdir():
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads((target / "report.json").read_text()) == result
    assert (target / "report.txt").read_text() == G.render(result) + "\n"
    assert [json.loads(line) for line in (target / "commands.jsonl").read_text().splitlines()] == result["commands"]


@pytest.mark.parametrize("kind", ["directory", "file", "symlink"])
def test_existing_report_paths_are_preserved(tmp_path, kind):
    target = tmp_path / "existing"
    if kind == "directory":
        target.mkdir()
        (target / "keep.txt").write_text("preserve directory")
    elif kind == "file":
        target.write_text("preserve file")
    else:
        source = tmp_path / "source"
        source.mkdir()
        (source / "keep.txt").write_text("preserve target")
        target.symlink_to(source, target_is_directory=True)
    before = list(tmp_path.rglob("*"))
    with pytest.raises(FileExistsError):
        G.save(check(), target)
    assert list(tmp_path.rglob("*")) == before
    if kind == "file":
        assert target.read_text() == "preserve file"
    else:
        assert (target / "keep.txt").read_text().startswith("preserve")


@pytest.mark.parametrize("arguments,job", [(["--gpu-check"], "all"), (["--gpu-check=all"], "all"),
                                         (["--gpu-check", "123"], "123"), (["--gpu-check=123_4"], "123_4"),
                                         (["--gpu-check", "123+2"], "123+2"),
                                         (["--json", "--gpu-check", "--profile", "fedora"], "all")])
def test_cli_accepts_exact_job_and_connection_forms(arguments, job):
    args = cli.parse(arguments)
    assert args.gpu_check == job


@pytest.mark.parametrize("job", ["7.batch", "7_[1-8]", "7;touch /tmp/unsafe", "-1", "7/../8", "7_", "", "*", "七", "7\x1b"])
def test_invalid_job_ids_fail_before_backend_or_session(job, monkeypatch):
    monkeypatch.setattr(cli, "build", lambda *_: pytest.fail("must reject before session"))
    with pytest.raises(SystemExit) as error:
        cli.parse([f"--gpu-check={job}"])
    assert error.value.code == 2


@pytest.mark.parametrize("other", [["--doctor"], ["--run", "cancel 7"], ["--eval", "n_running"],
                                    ["--wait-for", "True"], ["--watch"], ["--report"], ["--csv"],
                                    ["--write-config"], ["--record", "record.jsonl"]])
def test_conflicting_modes_are_rejected_before_diagnostic_or_job_actions(other):
    with pytest.raises(SystemExit) as error:
        cli.parse(["--gpu-check", *other])
    assert error.value.code == 2


def test_output_requires_diagnostic_mode():
    with pytest.raises(SystemExit) as error:
        cli.parse(["--gpu-check-output", "report"])
    assert error.value.code == 2


def test_cli_never_builds_session_loads_plugins_or_overwrites_config(tmp_path, monkeypatch, capsys):
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"gpu_sampling": True, "record": str(tmp_path / "forbidden-recording.jsonl")}))
    original = config.read_bytes()
    monkeypatch.setattr(cli, "build", lambda *_: pytest.fail("GPU check must not build session"))
    monkeypatch.setattr(cli.plugins, "load", lambda *_: pytest.fail("GPU check must not load plugins"))
    monkeypatch.setattr(cli.Sampler, "__init__", lambda *_args, **_kwargs: pytest.fail("GPU check must not construct sampler"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    status = cli.main(["--gpu-check", "--fake", "--json", "--no-state", "--config", str(config)])
    result = json.loads(capsys.readouterr().out)
    assert status == (0 if result["ok"] else 1)
    assert result["requested_job"] == "all"
    assert config.read_bytes() == original
    assert not (tmp_path / "forbidden-recording.jsonl").exists()
    assert not (tmp_path / "state").exists()


def test_cli_saves_reports_even_when_diagnostic_finds_errors(tmp_path, monkeypatch, capsys):
    backend = ProbeBackend(queue="", accounting="")
    monkeypatch.setattr(cli, "Backend", lambda: backend)
    monkeypatch.setattr(cli, "build", lambda *_: pytest.fail("GPU check must not start session"))
    target = tmp_path / "gpu report"
    status = cli.main(["--gpu-check", "7", "--user", "alex", "--no-state", "--gpu-check-output", str(target)])
    streams = capsys.readouterr()
    assert status == 1 and "Traceback" not in streams.err
    result = json.loads((target / "report.json").read_text())
    assert findings(result, "job_not_found", job="7")
    assert str(target) in streams.out or str(target) in streams.err


def test_replay_check_is_explicit_recorded_evidence_and_never_probes_hardware(tmp_path, monkeypatch, capsys):
    recording = tmp_path / "replay.jsonl"
    records = [{"kind": "header", "user": "alex", "t": 1},
               {"t": 1, "cmd": ["squeue", "-u", "alex", "-h", "-o", JOB_FMT], "out": "", "dt": 0}]
    recording.write_text("\n".join(json.dumps(row) for row in records) + "\n")
    original = recording.read_bytes()
    monkeypatch.setattr(cli, "build", lambda *_: pytest.fail("GPU check must not start session"))
    monkeypatch.setattr(G.subprocess, "run", lambda *_args, **_kwargs: pytest.fail("Replay must not probe hardware"))
    status = cli.main(["--gpu-check", "--replay", str(recording), "--json", "--no-state"])
    result = json.loads(capsys.readouterr().out)
    assert status == 0 and result["mode"] == "replay"
    assert "not a live hardware check" in result["scope"]
    assert findings(result, "no_current_jobs") and recording.read_bytes() == original


def test_cli_existing_report_failure_is_actionable_and_preserves_files(tmp_path, monkeypatch, capsys):
    target = tmp_path / "existing"
    target.mkdir()
    (target / "keep.txt").write_text("keep me")
    monkeypatch.setattr(cli, "build", lambda *_: pytest.fail("GPU check must not start session"))
    status = cli.main(["--gpu-check", "--fake", "--no-state", "--gpu-check-output", str(target)])
    streams = capsys.readouterr()
    assert status == 1 and "GPU check:" in streams.err and "Traceback" not in streams.err
    assert (target / "keep.txt").read_text() == "keep me"
    assert [path.name for path in target.iterdir()] == ["keep.txt"]


def test_native_check_file_pipeline_saves_exact_errors_and_finds_typed_fedora_allocation(tmp_path):
    """Run the check file with real subprocesses and harmless native fixtures."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    project = tmp_path / "project with spaces"
    project.mkdir()
    audit = tmp_path / "commands.jsonl"
    script = bin_dir / "fixture-command"
    script.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "name = Path(sys.argv[0]).name\n"
        f"with open({str(audit)!r}, 'a') as handle: handle.write(json.dumps(sys.argv) + '\\n')\n"
        "if name == 'squeue':\n"
        f"    print('7|cpu=8,gres/gpu:rtx4090=1' if '-O' in sys.argv else {queue_row(gres='N/A', workdir=str(project))!r}, end='' if '-O' not in sys.argv else '\\n')\n"
        "elif name == 'scontrol':\n"
        "    if sys.argv[2] == 'hostnames': print('desktop')\n"
        f"    elif sys.argv[2] == 'job': print('JobId=7 JobState=RUNNING AllocTRES=cpu=8,gres/gpu:rtx4090=1 WorkDir={project}')\n"
        "    else: print('NodeName=desktop State=MIXED Gres=gpu:rtx4090:1 CfgTRES=cpu=8,gres/gpu=1')\n"
        "elif name == 'nvidia-smi': print('0, 50, 2048, 24576, NVIDIA RTX 4090')\n"
        "elif name == 'srun':\n"
        "    print('step stdout evidence')\n"
        "    print('srun: step creation temporarily disabled: exact Fedora reason', file=sys.stderr)\n"
        "    sys.exit(11)\n"
        "elif name == 'ssh':\n"
        "    print('ssh: Permission denied (publickey): exact Fedora fallback reason', file=sys.stderr)\n"
        "    sys.exit(255)\n"
        "elif name == 'sacct': pass\n"
        "elif name == 'lspci': print('01:00.0 VGA compatible controller: NVIDIA [10de:2684]')\n"
        "else: raise SystemExit(23)\n"
    )
    script.chmod(0o700)
    for tool in G.TOOLS:
        (bin_dir / tool).symlink_to(script)
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env.pop("RAVEN_CONFIG", None)
    env["PATH"] = str(bin_dir) + os.pathsep + env.get("PATH", "")
    env["XDG_CONFIG_HOME"] = str(tmp_path / "config")
    env["XDG_STATE_HOME"] = str(tmp_path / "state")
    target = tmp_path / "report with spaces"
    done = subprocess.run([sys.executable, str(ROOT / "scripts" / "check_gpu.py"), "7",
                           "--user", "alex", "--json", "--no-state", "--gpu-check-output", str(target)],
                          cwd=tmp_path, env=env, capture_output=True, text=True, timeout=10)
    assert done.returncode == 1 and "Traceback" not in done.stderr, done.stderr
    result = json.loads(done.stdout)
    assert result["mode"] == "local" and result["requested_job"] == "7"
    assert findings(result, "allocation_batch")[0]["evidence"][0]["allocated_gpus"] == 1
    assert result["jobs"][0]["trace"]["path"] == str(project / "logs" / "gpu-util-7.csv")
    records = [json.loads(line) for line in (target / "commands.jsonl").read_text().splitlines()]
    srun = next(record for record in records if record["argv"][0] == "srun")
    assert not any(record["argv"][0] == "ssh" for record in records)
    assert srun["returncode"] == 11 and srun["stdout"] == "step stdout evidence\n"
    assert srun["stderr"] == "srun: step creation temporarily disabled: exact Fedora reason\n"
    assert findings(result, "allocation_probe_failed", job="7")
    for argv in [json.loads(line) for line in audit.read_text().splitlines()]:
        assert Path(argv[0]).name not in {"sbatch", "scancel", "salloc"}
    assert not (tmp_path / "state").exists()
    assert stat.S_IMODE(target.stat().st_mode) == 0o700


@pytest.mark.parametrize("job", [None, "12477369"])
def test_check_file_wrapper_runs_from_another_directory_without_install(tmp_path, job):
    args = [sys.executable, str(ROOT / "scripts" / "check_gpu.py")]
    if job:
        args.append(job)
    args += ["--fake", "--json", "--no-state", "--user", "alex"]
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env["XDG_CONFIG_HOME"] = str(tmp_path / "config")
    env["XDG_STATE_HOME"] = str(tmp_path / "state")
    done = subprocess.run(args, cwd=tmp_path, env=env, capture_output=True, text=True, timeout=10)
    assert done.returncode in (0, 1), done.stderr
    result = json.loads(done.stdout)
    assert result["requested_job"] == (job or "all")
    assert result["mode"] == "demo" and "not a live hardware check" in result["scope"]
    assert not (tmp_path / "state").exists() and "Traceback" not in done.stderr
