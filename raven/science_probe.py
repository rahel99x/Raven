"""Small allocation-local placement observer. This module never alters workload affinity."""
from __future__ import annotations

# Sent as source so compute nodes need only Python 3 and their Slurm client.
SOURCE = r'''
import json, os, re, selectors, signal, socket, subprocess, sys, time
jid = sys.argv[1]
raw_jid = sys.argv[2] if len(sys.argv) > 2 else jid
result = {"job_id": jid, "node": socket.gethostname(), "processes": [], "warnings": []}
def listpids():
    process = subprocess.Popen(["scontrol", "listpids", jid], stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    data = {"out": bytearray(), "err": bytearray()}
    deadline = time.monotonic() + 3
    try:
        with selectors.DefaultSelector() as selector:
            for stream, name in ((process.stdout, "out"), (process.stderr, "err")):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ, name)
            while selector.get_map():
                if time.monotonic() >= deadline:
                    raise ValueError("scontrol listpids timed out")
                for key, _ in selector.select(.05):
                    chunk = os.read(key.fileobj.fileno(), 16384)
                    if chunk:
                        data[key.data].extend(chunk)
                        if sum(map(len, data.values())) > 1048576:
                            raise ValueError("scontrol listpids exceeded 1 MiB")
                    else:
                        selector.unregister(key.fileobj)
        process.wait(timeout=max(.01, deadline-time.monotonic()))
        if process.returncode:
            raise ValueError("scontrol listpids cannot identify allocation processes: " + data["err"].decode(errors="replace")[:200])
        lines = data["out"].decode(errors="replace").splitlines()
        header = lines[0].split() if lines else []
        if "PID" not in header or "JOBID" not in header:
            raise ValueError("scontrol listpids did not return PID/JOBID identity columns")
        pi, ji = header.index("PID"), header.index("JOBID")
        candidates = []
        for line in lines[1:]:
            fields = line.split()
            if len(fields) > max(pi, ji) and fields[pi].isdigit() and fields[ji] == raw_jid:
                candidates.append(int(fields[pi]))
        return sorted(set(candidates))
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=1)
        process.stdout.close()
        process.stderr.close()
try:
    candidates = listpids()
    if len(candidates) > 256:
        result["warnings"].append("Process inspection truncated to 256 allocation processes")
    budget = [32 * 1048576]
    deadline = time.monotonic() + 5
    for pid in candidates[:256]:
        if budget[0] <= 0 or time.monotonic() > deadline:
            result["warnings"].append("Per-node 32 MiB / five-second inspection budget reached")
            break
        if pid == os.getpid():
            continue
        try:
            def small(name, size):
                with open("/proc/%d/%s" % (pid, name), "rb") as stream:
                    value = stream.read(min(size+1, max(0, budget[0])+1))
                budget[0] -= len(value)
                if budget[0] < 0:
                    raise ValueError("Inspection byte budget reached")
                if len(value) > size:
                    raise ValueError(name + " exceeded read limit")
                return value
            stat_before = small("stat", 16384).decode().rsplit(")", 1)[1].split()[19]
            status = small("status", 65536).decode(errors="replace")
            fields = dict(line.split(":", 1) for line in status.splitlines() if ":" in line)
            if int(fields.get("Uid", "-1").split()[0]) != os.getuid():
                continue
            row = {"pid": pid, "start_ticks": stat_before, "cpu_allowed": fields.get("Cpus_allowed_list", "").strip(),
                   "numa_allowed": fields.get("Mems_allowed_list", "").strip(), "gpu_visibility": {}, "numa_pages": {}}
            try:
                entries = small("environ", 1048576).split(b"\0")
                for entry in entries:
                    key, _, value = entry.partition(b"=")
                    if key in (b"CUDA_VISIBLE_DEVICES", b"ROCR_VISIBLE_DEVICES", b"HIP_VISIBLE_DEVICES", b"ZE_AFFINITY_MASK", b"GPU_DEVICE_ORDINAL"):
                        row["gpu_visibility"][key.decode()] = value.decode(errors="replace")[:1024]
            except (OSError, ValueError):
                row["gpu_visibility_error"] = "Process GPU visibility is inaccessible"
            try:
                maps = small("numa_maps", 4194304).decode(errors="replace")
                for node, pages in re.findall(r"\bN(\d+)=(\d+)", maps):
                    row["numa_pages"][node] = row["numa_pages"].get(node, 0) + int(pages)
            except (OSError, ValueError):
                row["numa_error"] = "NUMA residency is inaccessible"
            stat_after = small("stat", 16384).decode().rsplit(")", 1)[1].split()[19]
            if stat_before != stat_after:
                raise ValueError("PID changed identity during observation")
            result["processes"].append(row)
        except (OSError, ValueError, IndexError):
            result["warnings"].append("PID %s exited or was inaccessible during observation" % pid)
    members = set(listpids())
    verified = []
    for row in result["processes"]:
        try:
            with open("/proc/%d/stat" % row["pid"], "r") as stream:
                start = stream.read(16384).rsplit(")", 1)[1].split()[19]
            if row["pid"] in members and start == row["start_ticks"]:
                verified.append(row)
            else:
                result["warnings"].append("PID %s allocation membership changed" % row["pid"])
        except (OSError, ValueError, IndexError):
            result["warnings"].append("PID %s disappeared before verification" % row["pid"])
    result["processes"] = verified
except (OSError, ValueError, subprocess.TimeoutExpired) as error:
    result["processes"] = []
    result["warnings"].append(str(error)[:300])
result["warnings"] = result["warnings"][:30]
print("RAVEN_PLACEMENT " + json.dumps(result, separators=(",", ":")))
'''


RUNTIME_SOURCE = r'''
import importlib.metadata, json, os, platform, socket, sys, time
requested = json.loads(sys.argv[2])
data = {"job_id": sys.argv[1], "node": socket.gethostname(), "python": platform.python_version(),
        "platform": platform.system(), "machine": platform.machine(), "libc": list(platform.libc_ver()),
        "packages": {}, "warnings": []}
deadline = time.monotonic() + 5
for name in requested:
    if time.monotonic() > deadline:
        data["warnings"].append("Package inventory reached the five-second node budget")
        break
    try:
        data["packages"][name] = importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        data["packages"][name] = None
print("RAVEN_RUNTIME " + json.dumps(data, separators=(",", ":")))
'''


def parse_runtime(output, job_id):
    import json
    rows, nodes = [], set()
    for line in output.splitlines():
        _, marker, text = line.partition("RAVEN_RUNTIME ")
        if not marker:
            continue
        data = json.loads(text)
        if not isinstance(data, dict) or data.get("job_id") != job_id or not isinstance(data.get("node"), str) or not data["node"] or data["node"] in nodes:
            raise ValueError("Runtime probe returned a mismatched or duplicate allocation identity")
        if any(not isinstance(data.get(key), str) for key in ("python", "platform", "machine")) or not isinstance(data.get("packages"), dict) or not isinstance(data.get("libc"), list) or len(data["libc"]) != 2 or not isinstance(data.get("warnings"), list):
            raise ValueError("Runtime probe returned malformed evidence")
        nodes.add(data["node"])
        rows.append(data)
        if len(rows) > 128:
            raise ValueError("Runtime probe exceeded 128 nodes")
    if not rows:
        raise ValueError("No allocation runtime evidence was returned")
    return rows


def parse(output, job_id):
    import json
    rows = []
    seen = set()
    for line in output.splitlines():
        _, marker, body = line.partition("RAVEN_PLACEMENT ")
        if not marker:
            continue
        row = json.loads(body)
        if not isinstance(row, dict) or row.get("job_id") != job_id or not isinstance(row.get("node"), str) or not row["node"]:
            raise ValueError("Placement probe returned a mismatched allocation identity")
        if row["node"] in seen:
            raise ValueError("Placement probe returned a duplicate node")
        if not isinstance(row.get("processes"), list) or len(row["processes"]) > 256 or not isinstance(row.get("warnings"), list):
            raise ValueError("Invalid placement probe result")
        for process in row["processes"]:
            if not isinstance(process, dict) or not isinstance(process.get("pid"), int) or process["pid"] <= 0 or not isinstance(process.get("cpu_allowed"), str) or not isinstance(process.get("gpu_visibility"), dict) or not isinstance(process.get("numa_pages"), dict):
                raise ValueError("Invalid placement process record")
        seen.add(row["node"])
        rows.append(row)
        if len(rows) > 128:
            raise ValueError("Placement probe exceeded 128 nodes")
    if not rows:
        raise ValueError("No allocation-scoped placement records were returned")
    return rows
