"""Conditional, fixed two-rank same-host MPI probe; no caller command or host list."""
from __future__ import annotations
import fcntl
from collections import deque
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import selectors
import shlex
import stat
import subprocess
import tempfile
import socket
import sys
import time

SOURCE = r'''
#include <mpi.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <signal.h>
#include <sys/prctl.h>
int main(int argc, char **argv) {
  if(argc != 5) return 64;
  int n=atoi(argv[1]), warm=atoi(argv[2]), reps=atoi(argv[3]), seconds=atoi(argv[4]);
  if(n<4096 || n>1048576 || warm<0 || warm>2 || reps<2 || reps>8 || seconds<1 || seconds>30) return 64;
  pid_t parent=getppid();
  char *owner=getenv("RAVEN_MPI_PARENT_PID"), *end=NULL;
  long expected=owner ? strtol(owner,&end,10) : 0;
  if(expected<=1 || !end || *end || parent!=expected) return 65;
  if(prctl(PR_SET_PDEATHSIG,SIGKILL)!=0 || getppid()!=parent) return 65;
  alarm(seconds);
  if(MPI_Init(&argc,&argv)!=MPI_SUCCESS) return 66;
  MPI_Comm_set_errhandler(MPI_COMM_WORLD,MPI_ERRORS_RETURN);
  int rank=0,size=0; MPI_Comm_rank(MPI_COMM_WORLD,&rank); MPI_Comm_size(MPI_COMM_WORLD,&size);
  if(size!=2) MPI_Abort(MPI_COMM_WORLD,67);
  char host[MPI_MAX_PROCESSOR_NAME]={0}, hosts[2*MPI_MAX_PROCESSOR_NAME]={0}; int len=0;
  MPI_Get_processor_name(host,&len);
  MPI_Allgather(host,MPI_MAX_PROCESSOR_NAME,MPI_CHAR,hosts,MPI_MAX_PROCESSOR_NAME,MPI_CHAR,MPI_COMM_WORLD);
  if(strcmp(hosts,hosts+MPI_MAX_PROCESSOR_NAME)!=0) MPI_Abort(MPI_COMM_WORLD,68);
  char library[MPI_MAX_LIBRARY_VERSION_STRING]; MPI_Get_library_version(library,&len);
  if(rank==0){ printf("RAVEN_MPI_LIBRARY "); for(int i=0;i<len;i++) putchar(library[i]<' '?' ':library[i]); putchar('\n'); fflush(stdout); }
  unsigned char *send=malloc(n), *receive=malloc(n);
  if(!send || !receive) MPI_Abort(MPI_COMM_WORLD,69);
  memset(send,rank+1,n);
  for(int i=0;i<warm+reps;i++){
    MPI_Barrier(MPI_COMM_WORLD); double start=MPI_Wtime();
    int code=MPI_Sendrecv(send,n,MPI_BYTE,1-rank,1,receive,n,MPI_BYTE,1-rank,1,MPI_COMM_WORLD,MPI_STATUS_IGNORE);
    double elapsed=MPI_Wtime()-start, maximum=0; int valid=code==MPI_SUCCESS, all=0;
    for(int j=0;j<n;j++) if(receive[j]!=(unsigned char)(2-rank)) valid=0;
    MPI_Allreduce(&valid,&all,1,MPI_INT,MPI_MIN,MPI_COMM_WORLD);
    MPI_Allreduce(&elapsed,&maximum,1,MPI_DOUBLE,MPI_MAX,MPI_COMM_WORLD);
    if(!all) MPI_Abort(MPI_COMM_WORLD,70);
    if(rank==0){ printf("RAVEN_MPI_SAMPLE {\"repeat\":%d,\"seconds\":%.17g,\"bytes\":%d,\"verified\":true}\n",i,maximum,n); fflush(stdout); }
  }
  free(send); free(receive); MPI_Finalize(); return 0;
}
'''
MAX_FILE = 4 << 20
# Linux UAPI constants are absent from some supported Python fcntl builds.
ADD_SEALS = getattr(fcntl, "F_ADD_SEALS", 1033)
SEALS = 1 | 2 | 4 | 8  # SEAL_SEAL, SHRINK, GROW, WRITE


def authority_chain(path):
    """Inspect every original and symlink-target component, including alternatives."""
    path = Path(path).absolute()
    if len(str(path)) > 4096:
        raise ValueError("MPI toolchain path exceeds its bound")
    pending, current, hops, records = deque(path.parts[1:]), Path("/"), 0, []
    trusted = {0, os.getuid(), os.stat("/").st_uid}
    while pending:
        name = pending.popleft()
        if name in ("", "."):
            continue
        if name == "..":
            current = current.parent
            continue
        component = current / name
        info = component.lstat()
        link = stat.S_ISLNK(info.st_mode)
        owners = trusted if stat.S_ISDIR(info.st_mode) else {0, os.getuid()}
        if (info.st_uid not in owners or not link and info.st_mode & 0o022
                and not (stat.S_ISDIR(info.st_mode) and info.st_mode & stat.S_ISVTX)):
            raise ValueError("MPI toolchain and prefix must not be writable by other users")
        target = os.readlink(component) if link else ""
        records.append([str(component), str(info.st_dev), str(info.st_ino), info.st_uid,
                        info.st_mode, 0 if stat.S_ISDIR(info.st_mode) else info.st_mtime_ns,
                        0 if stat.S_ISDIR(info.st_mode) else info.st_size, target])
        if link:
            hops += 1
            if hops > 40:
                raise ValueError("MPI toolchain exceeds its symlink bound")
            value = Path(target)
            if value.is_absolute():
                current = Path("/")
                parts = value.parts[1:]
            else:
                parts = value.parts
            pending.extendleft(reversed(parts))
        else:
            current = component
    return current, records


def file_identity(path):
    path = Path(path).absolute()
    resolved, chain = authority_chain(path)
    descriptor = os.open(resolved, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_FILE:
            raise ValueError("MPI toolchain file is unavailable or exceeds 4 MiB")
        data = bytearray()
        while len(data) <= MAX_FILE:
            chunk = os.read(descriptor, min(65536, MAX_FILE + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
        after = os.fstat(descriptor)
        if (len(data) > MAX_FILE or authority_chain(path) != (resolved, chain)
                or (before.st_dev, before.st_ino, before.st_mtime_ns, before.st_size)
                != (after.st_dev, after.st_ino, after.st_mtime_ns, after.st_size)):
            raise ValueError("MPI toolchain changed during review")
    finally:
        os.close(descriptor)
    return {"path": str(path), "sha256": hashlib.sha256(data).hexdigest(), "device": str(before.st_dev),
            "inode": str(before.st_ino), "size": before.st_size, "mtime_ns": before.st_mtime_ns,
            "authority_sha256": hashlib.sha256(json.dumps(chain, separators=(",", ":")).encode()).hexdigest()}


def review(execution, prefix=""):
    if sys.platform != "linux" or not all(hasattr(os, key) for key in ("memfd_create", "pidfd_open", "sched_getaffinity")):
        raise ValueError("This MPI adapter requires Linux anonymous files, pidfds and CPU affinity")
    resources = execution.get("verified_resources", {})
    if (type(resources.get("cpus")) is not int or resources["cpus"] < 2
            or type(resources.get("memory_mib")) is not int or resources["memory_mib"] < 256
            or execution.get("host") != socket.gethostname()
            or len(os.sched_getaffinity(0)) < 2):
        raise ValueError("MPI preflight requires two authenticated worker CPUs, at least 256 MiB, and local verified placement")
    tools = {"prefix": str(Path(prefix).expanduser().absolute()) if prefix else ""}
    for name in ("mpicc", "mpiexec"):
        path = shutil.which(name)
        if not path:
            raise ValueError("MPI preflight requires a trusted installed Open MPI development/runtime toolchain")
        tools[name] = file_identity(path)
    from .diagnostic_trials import _execute
    version = _execute([sys.executable, "-I", "-S", str(Path(__file__).resolve()), "--clean-exec",
        tempfile.gettempdir(), tools["mpicc"]["path"], "--showme:version"], Path.cwd(),
        {"trial_seconds": 2, "memory_mb": 256, "cpus": 1, "output_bytes": 65536}, 2)
    if (version["state"] != "finished" or version["returncode"] != 0
            or "Open MPI 5." not in version["output"]):
        raise ValueError("Only the qualified Open MPI 5.x development/runtime toolchain is supported")
    tools["compiler_version"] = version["output"].strip()
    if file_identity(tools["mpicc"]["path"]) != tools["mpicc"]:
        raise ValueError("MPI compiler changed during review")
    if prefix:
        path = Path(prefix).expanduser().absolute()
        if ".." in path.parts:
            raise ValueError("MPI development prefix cannot contain parent traversal")
        include = path / "include"
        library = path / "lib"
        if not (include / "mpi.h").is_file():
            include = path / "usr/lib/x86_64-linux-gnu/openmpi/include"
            library = path / "usr/lib/x86_64-linux-gnu/openmpi/lib"
    else:
        from .diagnostic_trials import _execute
        def locate(option, filename):
            result = _execute([sys.executable, "-I", "-S", str(Path(__file__).resolve()), "--clean-exec",
                tempfile.gettempdir(), tools["mpicc"]["path"], option], Path.cwd(),
                {"trial_seconds": 2, "memory_mb": 256, "cpus": 1, "output_bytes": 65536}, 2)
            paths = shlex.split(result["output"])
            if result["returncode"] or len(paths) > 16:
                raise ValueError("Open MPI development paths could not be discovered within the review budget")
            for value in paths:
                path = Path(value)
                if path.is_absolute() and (path / filename).is_file(): return path
            raise ValueError("Install Open MPI development headers/libraries or choose their explicit trusted prefix")
        include = locate("--showme:incdirs", "mpi.h")
        library = locate("--showme:libdirs", "libmpi.so")
    tools["include"] = str(include)
    tools["library"] = str(library)
    tools["headers"] = [file_identity(include / name) for name in ("mpi.h", "mpi_portable_platform.h")]
    tools["library_file"] = file_identity(library / "libmpi.so")
    tools["source_sha256"] = hashlib.sha256(SOURCE.encode()).hexdigest()
    tools["limits"] = {"ranks": 2, "maximum_message_bytes": 1 << 20, "compile_output_bytes": 65536, "runtime_output_bytes": 1 << 20,
                       "runtime_file_bytes": 1 << 20, "runtime_total_bytes": 8 << 20, "runtime_files": 128,
                       "anonymous_compile_file_bytes": 65536, "same_host_only": True}
    return tools


def probe(settings, execution, tools, *, cancel=None, deadline=None):
    from .diagnostic_trials import _execute
    prefix = tools.get("prefix", "")
    if review(execution, prefix) != tools:
        raise ValueError("Reviewed MPI toolchain/source changed; prepare a new review")
    deadline = deadline or time.monotonic() + settings["seconds"]
    resources = execution["verified_resources"]
    limits = {"trial_seconds": settings["seconds"], "memory_mb": resources["memory_mib"],
              "cpus": 2, "output_bytes": 65536}
    commands = []
    workspace = SessionDirectory()
    def run(argv, *, runtime=False):
        selected = tools["mpiexec"] if runtime else tools["mpicc"]
        checked = [selected] + ([] if runtime else tools["headers"] + [tools["library_file"]])
        if any(file_identity(item["path"]) != item for item in checked):
            raise ValueError("Reviewed MPI toolchain changed immediately before execution")
        remaining = deadline - time.monotonic()
        wrapped = [sys.executable, "-I", "-S", str(Path(__file__).resolve()), "--clean-exec", workspace.path, *argv]
        result = _execute(wrapped, Path.cwd(), {**limits, "output_bytes": (1 << 20) if runtime else 65536}, remaining, WorkspaceCancellation(workspace.path, cancel))
        commands.append({"argv": argv, **result})
        if result["state"] == "cancelled": raise InterruptedError("MPI preflight cancelled")
        if result["state"] == "timeout": raise TimeoutError("MPI compilation/runtime exceeded the shared time budget: " + result["output"][-1500:])
        if result["state"] != "finished" or result["returncode"] != 0:
            raise OSError("MPI preflight " + result["state"] + ": " + result["output"][-1500:])
        return result["output"]
    descriptors = []
    try:
        version = tools["compiler_version"]
        for name in ("source", "object", "executable"):
            fd = os.memfd_create("raven-mpi-health-" + name, os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING)
            descriptors.append(fd)
        source, obj, binary = (f"/proc/{os.getpid()}/fd/{fd}" for fd in descriptors)
        os.write(descriptors[0], SOURCE.encode())
        seals = SEALS
        fcntl.fcntl(descriptors[0], ADD_SEALS, seals)
        flags = ["-I" + tools["include"]] if tools.get("include") else []
        run([tools["mpicc"]["path"], *flags, "-O2", "-pipe", "-x", "c", "-c", source, "-o", obj])
        if os.fstat(descriptors[1]).st_size > 65536: raise ValueError("Compiled MPI object exceeds its byte bound")
        fcntl.fcntl(descriptors[1], ADD_SEALS, seals)
        flags = ["-L" + tools["library"]] if tools.get("library") else []
        run([tools["mpicc"]["path"], *flags, obj, "-o", binary])
        if os.fstat(descriptors[2]).st_size > 65536: raise ValueError("Compiled MPI executable exceeds its byte bound")
        os.fchmod(descriptors[2], 0o700)
        fcntl.fcntl(descriptors[2], ADD_SEALS, seals)
        binary_hash = hashlib.sha256(os.pread(descriptors[2], 65536, 0)).hexdigest()
        seconds = max(1, min(30, math.ceil(deadline - time.monotonic())))
        output = run([tools["mpiexec"]["path"], "-n", "2", "--host", "localhost:2", "--bind-to", "none",
            "--prtemca", "plm", "ssh", "--prtemca", "plm_ssh_agent", "/usr/bin/false", "--prtemca", "ras", "^slurm", "--mca", "btl", "self,tcp", "--mca", "btl_tcp_if_include", "lo", "--mca", "pml", "ob1",
            "--prtemca", "prte_tmpdir_base", workspace.path, "--timeout", str(seconds), binary, str(settings["bytes"]), str(settings["warmup"]), str(settings["repeats"]), str(seconds)], runtime=True)
        samples, library = [], ""
        for line in output.splitlines():
            if line.startswith("RAVEN_MPI_SAMPLE "):
                row = json.loads(line.removeprefix("RAVEN_MPI_SAMPLE "))
                if (set(row) != {"repeat", "seconds", "bytes", "verified"} or row["repeat"] != len(samples)
                        or row["bytes"] != settings["bytes"] or row["verified"] is not True
                        or isinstance(row["seconds"], bool) or not isinstance(row["seconds"], (int, float))
                        or not math.isfinite(row["seconds"]) or row["seconds"] < 0):
                    raise ValueError("MPI output failed fixed-probe validation")
                samples.append(dict(row, test="mpi", warmup=row["repeat"] < settings["warmup"]))
            elif line.startswith("RAVEN_MPI_LIBRARY "):
                library = line.removeprefix("RAVEN_MPI_LIBRARY ")
        if len(samples) != settings["warmup"] + settings["repeats"] or not library:
            raise ValueError("MPI probe did not return its complete verified repetitions")
        return samples, {"toolchain": tools, "compiler_version": version.strip(), "library_version": library,
            "binary_sha256": binary_hash, "commands": commands, "runtime_temp": workspace.path,
            "runtime_temp_cleanup": "owned janitor verifies removal before return; pidfd/pipe EOF also closes crash lifetime", "placement": "two ranks on the authenticated worker host",
            "limits": "Same-host communication only; no inter-node/interconnect or scientific workload efficacy"}
    finally:
        for fd in descriptors: os.close(fd)
        workspace.close()


class WorkspaceCancellation:
    def __init__(self, path, cancel): self.path, self.cancel = path, cancel
    def is_set(self):
        if self.cancel is not None and self.cancel.is_set(): return True
        total = count = 0
        for root, dirs, files in os.walk(self.path, followlinks=False):
            count += len(dirs) + len(files)
            if count > 128: raise OSError("MPI runtime exceeds 128 temporary entries")
            for name in files:
                try: size = os.lstat(os.path.join(root, name)).st_size
                except FileNotFoundError: continue
                if size > 1 << 20: raise OSError("MPI runtime temporary file exceeds 1 MiB")
                total += size
                if total > 8 << 20: raise OSError("MPI runtime temporary storage exceeds 8 MiB")
        return False


class SessionDirectory:
    """A tiny owned janitor survives task SIGKILL to remove runtime session files."""
    def __init__(self):
        self.process = subprocess.Popen([sys.executable, "-I", "-S", str(Path(__file__).resolve()),
            "--session-directory", str(os.getpid())], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, start_new_session=True)
        self.path = ""
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(self.process.stdout, selectors.EVENT_READ)
                if not selector.select(2): raise OSError("MPI cleanup owner did not initialize")
            line = self.process.stdout.readline(8193)
            value = json.loads(line)
            self.path = value["path"]
            if not Path(self.path).name.startswith("raven-mpi-session-") or not Path(self.path).is_absolute():
                raise ValueError("Invalid MPI cleanup owner reply")
        except BaseException:
            self.close()
            raise

    def close(self):
        if self.process.stdin is not None and not self.process.stdin.closed:
            self.process.stdin.close()
        try:
            code = self.process.wait(timeout=3)
            if code or self.path and Path(self.path).exists():
                raise OSError("MPI runtime cleanup could not be verified")
        finally:
            self.process.stdout.close(); self.process.stderr.close()


def session_directory(parent):
    # Open the exact parent lifetime before creating any temporary state.
    import resource
    if os.getppid() != parent: return 125
    owner = os.pidfd_open(parent)
    if os.getppid() != parent:
        os.close(owner)
        return 125
    inherited_soft, inherited_hard = resource.getrlimit(resource.RLIMIT_CPU)
    hard = min(2, inherited_hard) if inherited_hard != resource.RLIM_INFINITY else 2
    soft = min(hard, inherited_soft) if inherited_soft != resource.RLIM_INFINITY else hard
    resource.setrlimit(resource.RLIMIT_CPU, (soft, hard))
    path = tempfile.mkdtemp(prefix="raven-mpi-session-")
    status = os.stat(path)
    try:
        print(json.dumps({"path": path}), flush=True)
        with selectors.DefaultSelector() as selector:
            selector.register(owner, selectors.EVENT_READ)
            selector.register(sys.stdin, selectors.EVENT_READ)
            selector.select(40)
        # The execution supervisor tears down its group on task death. This
        # short grace permits that signal before unlinking runtime metadata.
        time.sleep(.1)
    finally:
        current = os.lstat(path)
        if (current.st_uid, current.st_dev, current.st_ino) != (os.getuid(), status.st_dev, status.st_ino):
            raise ValueError("MPI cleanup directory identity changed")
        shutil.rmtree(path)
        os.close(owner)
    return 0


if __name__ == "__main__":
    if sys.argv[1:2] == ["--session-directory"] and len(sys.argv) == 3:
        raise SystemExit(session_directory(int(sys.argv[2])))
    if sys.argv[1:2] != ["--clean-exec"] or len(sys.argv) < 4:
        raise SystemExit(64)
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("OMPI_", "PRTE_", "PMIX_", "PMI_")) and key not in {"CFLAGS", "CPPFLAGS", "LDFLAGS", "LIBS"}}
    env["TMPDIR"] = sys.argv[2]
    # Fixed same-host ranks must reject adoption by init or a subreaper before
    # their first instruction establishes parent-death signalling.
    env["RAVEN_MPI_PARENT_PID"] = str(os.getpid())
    os.execvpe(sys.argv[3], sys.argv[3:], env)
