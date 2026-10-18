import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time

import pytest

from raven import allocation_health as H, mpi_health as M, operations as O, raven_execution as R
from raven.raven_worker_client import WorkerClient


def execution(**resources):
    return {"verified": True, "verification": "explicit local test fixture", "site_kind": "desktop",
            "uid": os.getuid(), "pid": os.getpid(), "host": socket.gethostname(),
            "verified_resources": {"cpus": 2, "memory_mib": 1024, **resources}}


def test_mpi_requires_authenticated_cpu_and_memory_grants():
    for value in (execution(cpus=1), execution(cpus=True), execution(memory_mib=128),
                  {**execution(), "verified_resources": {}}, {**execution(), "host": "different-host"}):
        with pytest.raises(ValueError, match="authenticated"):
            M.review(value)
    with pytest.raises(ValueError, match="1 MiB"):
        H.settings({"tests": "mpi", "bytes": (1 << 20) + 1})
    for operation in ("allocation-health", "allocation-health-probe"):
        assert R.OPERATIONS[operation]["budget"]["cpus"] == 2


def test_untrusted_writable_toolchain_refused(tmp_path):
    path = tmp_path / "compiler"
    path.write_bytes(b"compiler"); path.chmod(0o777)
    with pytest.raises(ValueError, match="writable"):
        M.file_identity(path)



def test_original_and_intermediate_symlink_authority_is_checked(tmp_path):
    trusted = tmp_path / "trusted"; trusted.mkdir()
    binary = trusted / "compiler"; binary.write_bytes(b"reviewed tool")
    original = tmp_path / "writable-original"; original.mkdir(); original.chmod(0o775)
    (original / "compiler").symlink_to(binary)
    with pytest.raises(ValueError, match="writable"):
        M.file_identity(original / "compiler")
    link = tmp_path / "chain"; link.symlink_to(original / "compiler")
    with pytest.raises(ValueError, match="writable"):
        M.file_identity(link)
    original.chmod(0o755)
    identity = M.file_identity(link)
    assert identity["sha256"] == M.file_identity(binary)["sha256"]
    assert isinstance(identity["device"], str) and isinstance(identity["inode"], str)
    (tmp_path / "unrelated").write_text("directory mtime must not invalidate immutable authority")
    assert M.file_identity(link) == identity
    (original / "compiler").unlink(); (original / "compiler").symlink_to(binary)
    assert M.file_identity(link) != identity, "Replacement link requires a fresh review"


def test_foreign_owned_original_component_is_refused(tmp_path, monkeypatch):
    original = tmp_path / "original"; original.mkdir()
    binary = tmp_path / "compiler"; binary.write_bytes(b"tool")
    link = original / "compiler"; link.symlink_to(binary)
    actual = Path.lstat
    def ownership(path):
        result = actual(path)
        if path == original:
            values = list(result); values[4] = 1234567
            return os.stat_result(values)
        return result
    monkeypatch.setattr(Path, "lstat", ownership)
    with pytest.raises(ValueError, match="writable"):
        M.file_identity(link)


def test_standard_system_alternatives_remain_trusted():
    path = shutil.which("mpicc") or shutil.which("python3")
    if not path:
        pytest.skip("No system tool to inspect")
    if Path(path).lstat().st_uid not in {0, os.getuid()}:
        with pytest.raises(ValueError, match="writable"):
            M.file_identity(path)
    else:
        assert len(M.file_identity(path)["sha256"]) == 64


def test_changed_toolchain_plan_is_refused_before_runtime(tmp_path, monkeypatch):
    tools = {"prefix": str(tmp_path), "source_sha256": "before"}
    monkeypatch.setattr(M, "review", lambda *args: {**tools, "source_sha256": "after"})
    with pytest.raises(ValueError, match="changed"):
        M.probe(H.settings({"tests": "mpi"}), execution(), tools)

def test_runtime_session_is_private_and_cleanup_verified():
    workspace = M.SessionDirectory()
    path = Path(workspace.path)
    assert path.stat().st_mode & 0o777 == 0o700
    (path / "session-state").write_text("owned")
    process = workspace.process
    workspace.close()
    assert not path.exists() and process.poll() == 0



def test_runtime_session_respects_a_smaller_inherited_cpu_limit():
    script = "import resource; resource.setrlimit(resource.RLIMIT_CPU,(1,1)); from raven.mpi_health import SessionDirectory; w=SessionDirectory(); w.close()"
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr

def test_runtime_session_cleanup_survives_actual_owner_sigkill(tmp_path):
    script = "from raven.mpi_health import SessionDirectory; import json,time; w=SessionDirectory(); print(json.dumps({'path':w.path,'pid':w.process.pid}),flush=True); time.sleep(30)"
    process = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, text=True)
    try:
        value = json.loads(process.stdout.readline())
        path = Path(value["path"]); assert path.exists()
        (path / "owned-file").write_text("retained until owner death")
        process.kill(); process.wait(timeout=3)
        deadline = time.monotonic() + 3
        while path.exists() and time.monotonic() < deadline: time.sleep(.02)
        assert not path.exists(), "pidfd-bound cleanup must survive task SIGKILL"
    finally:
        if process.poll() is None: process.kill(); process.wait(timeout=3)
        process.stdout.close()


def test_runtime_temporary_limits_are_checked(tmp_path):
    guard = M.WorkspaceCancellation(str(tmp_path), None)
    (tmp_path / "oversized").write_bytes(b"x" * ((1 << 20) + 1))
    with pytest.raises(OSError, match="1 MiB"): guard.is_set()
    cancel = threading.Event(); cancel.set()
    assert M.WorkspaceCancellation(str(tmp_path), cancel).is_set()


@pytest.mark.parametrize("message_bytes", [4096, 1048576])
def test_actual_mpi_review_apply_uses_owned_worker_and_reaps_temporary_state(tmp_path, message_bytes):
    prefix = os.environ.get("RAVEN_MPI_QUALIFICATION_PREFIX")
    if not prefix:
        pytest.skip("Optional explicit Open MPI 5 development/runtime qualification requires RAVEN_MPI_QUALIFICATION_PREFIX")
    client = WorkerClient({"site_kind": "desktop", "desktop_explicit": True, "context": {},
        "resources": {"cpus": 2, "memory_mib": 2048, "max_tasks": 1, "wall_seconds": 45}})
    try:
        client.start()
        deadline = time.monotonic() + 8
        while not client.ready and time.monotonic() < deadline:
            client.poll(); time.sleep(.01)
        assert client.ready, client.error
        router = R.ExecutionRouter({"managed": True}, lambda: client)
        ctx = O.Context(scope={"site": "mpi-local-qualification"}, execution=router, state_dir=str(tmp_path / "state"))
        params = {"tests": "mpi", "bytes": str(message_bytes), "seconds": "30", "repeats": "3", "warmup": "1",
                  "mpi_prefix": prefix, "destination": str(tmp_path / "receipt")}
        review = O.run("allocation-health", params, ctx)
        assert review["plan"]["payload"]["execution"]["verified_resources"]["cpus"] == 2
        assert not (tmp_path / "receipt").exists()
        result = O.apply("allocation-health", review["plan"], ctx)
        receipt = result["data"]
        assert receipt["status"] == "completed", receipt.get("error")
        assert len(receipt["samples"]) == 4 and all(row["verified"] for row in receipt["samples"])
        assert "Open MPI v5." in receipt["mpi"]["library_version"]
        assert not Path(receipt["mpi"]["runtime_temp"]).exists()
        assert receipt == json.loads((tmp_path / "receipt/health.json").read_text())
        destination = os.environ.get("RAVEN_MPI_QUALIFICATION_EVIDENCE")
        if destination:
            import hashlib
            root = Path(__file__).resolve().parents[1]
            Path(destination).write_text(json.dumps({"schema": "raven.mpi-health-qualification/v1", "receipt": receipt,
                "actual_governed_worker": True, "actual_same_host_mpi": True, "actual_slurm": False,
                "source_sha256": {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
                    for path in (root / "raven/mpi_health.py", root / "raven/allocation_health.py", root / "raven/raven_worker.py", root / "raven/raven_execution.py", root / "raven/ops_health.py", root / "raven/diagnostic_trials.py", root / "raven/science_files.py", Path(__file__))}}, indent=2) + "\n")
    finally:
        client.close()


def _owned_descendants(pid):
    """Observe exact Linux process lifetimes; /proc task/children is optional."""
    children, states = {}, {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            fields = (entry / "stat").read_text().rpartition(") ")[2].split()
            child, parent = int(entry.name), int(fields[1])
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            continue
        children.setdefault(parent, []).append(child)
        states[child] = fields
    found, pending = {}, [pid]
    while pending:
        for child in children.get(pending.pop(), []):
            if child in found:
                continue
            try:
                executable = os.readlink(f"/proc/{child}/exe")
            except (FileNotFoundError, ProcessLookupError, PermissionError):
                continue
            fields = states[child]
            found[child] = {"start_ticks": fields[19], "state": fields[0], "executable": executable}
            pending.append(child)
    return found


def _same_live_process(pid, recorded):
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rpartition(") ")[2].split()
    except FileNotFoundError:
        return False
    return fields[19] == recorded["start_ticks"] and fields[0] not in {"Z", "X"}


@pytest.mark.parametrize("termination", ["cancel", "owner-sigkill"])
def test_actual_running_mpi_termination_cleans_owned_lifetimes(tmp_path, termination):
    prefix = os.environ.get("RAVEN_MPI_QUALIFICATION_PREFIX")
    if not prefix:
        pytest.skip("Explicit Open MPI 5 qualification prefix is required")
    marker = tmp_path / "cancel"
    script = r'''
import json,os,socket,sys
from pathlib import Path
from raven import mpi_health as M, allocation_health as H
original=M.SessionDirectory
class Observed(original):
    def __init__(self):
        super().__init__()
        print(json.dumps({"path":self.path}),flush=True)
M.SessionDirectory=Observed
class Cancel:
    def is_set(self): return Path(sys.argv[2]).exists()
execution={"verified":True,"verification":"explicit local termination fixture","site_kind":"desktop",
           "uid":os.getuid(),"pid":os.getpid(),"host":socket.gethostname(),
           "verified_resources":{"cpus":2,"memory_mib":1024}}
settings=H.settings({"tests":"mpi","bytes":1048576,"repeats":8,"warmup":2,"seconds":20})
result=H.probe(settings,execution,mpi=M.review(execution,sys.argv[1]),cancel=Cancel())
print(json.dumps({"status":result["status"],"error":result.get("error")}),flush=True)
'''
    process = subprocess.Popen([sys.executable, "-c", script, prefix, str(marker)], stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True)
    observed, rank_observed = {}, False
    try:
        first = process.stdout.readline()
        assert first, process.stderr.read()
        workspace = Path(json.loads(first)["path"])
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and process.poll() is None:
            observed.update(_owned_descendants(process.pid))
            rank_observed = any("memfd:raven-mpi-health-executable" in row["executable"] for row in observed.values())
            if rank_observed:
                break
            time.sleep(.001)
        assert rank_observed, {"message": "Qualification must observe an actual fixed MPI rank before termination", "observed": observed, "output": process.stdout.read() if process.poll() is not None else "running", "error": process.stderr.read() if process.poll() is not None else "running"}
        if termination == "cancel":
            marker.touch()
        else:
            process.kill()
        output, error = process.communicate(timeout=8)
        if termination == "cancel":
            assert process.returncode == 0, error
            assert json.loads(output.strip().splitlines()[-1])["status"] == "cancelled"
        else:
            assert process.returncode == -signal.SIGKILL
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline and (workspace.exists() or any(_same_live_process(pid, row) for pid, row in observed.items())):
            time.sleep(.02)
        assert not workspace.exists()
        assert not {pid: row for pid, row in observed.items() if _same_live_process(pid, row)}
        evidence = os.environ.get("RAVEN_MPI_QUALIFICATION_EVIDENCE")
        if evidence:
            path = Path(evidence)
            result = json.loads(path.read_text()) if path.exists() else {}
            result.setdefault("actual_termination", {})[termination] = {"observed_processes": observed,
                "actual_rank_observed": rank_observed, "all_observed_lifetimes_terminated": True,
                "owned_runtime_directory_removed": True,
                "limit": "Termination checks identify exact observed lifetimes; zombie removal by host init is not claimed"}
            path.write_text(json.dumps(result, indent=2) + "\n")
    finally:
        if process.poll() is None:
            process.kill(); process.wait(timeout=5)
        process.stdout.close(); process.stderr.close()
