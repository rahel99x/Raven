"""Bounded job-local measurements, independent of the dashboard and scheduler.

GPU ownership uses existing allocation verification. Process observations cover
only the local workload tree. Missing counters stay null. Exports read these
retained streams, whereas dashboards read a bounded tail.
"""
from __future__ import annotations

import collections
import errno
import datetime as dt
import json
import math
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import re

INDEX_SCHEMA = "raven.telemetry-index/v2"
GPU_SCHEMA = "raven.gpu-sample/v1"
CPU_SCHEMA = "raven.cpu-memory-sample/v1"
MAX_INDEX_BYTES = 256 * 1024
MAX_READ_BYTES = 8 * 1024 * 1024
MAX_READ_ROWS = 20000
MAX_PROCESSES = 4096


def _number(value, default, minimum, maximum):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return default
    return max(minimum, min(maximum, value))


def _utc(timestamp):
    return dt.datetime.fromtimestamp(timestamp, dt.timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _storage_budget(plan,environ,settings,defaults,interval):
    explicit=settings.get('max_bytes_per_stream',defaults.get('storage_bytes'))
    if explicit is not None:
        return int(_number(explicit,32<<20,4096,256<<20))
    options=plan.get('effective_options',{})
    value=str(options.get('time') or environ.get('SLURM_TIMELIMIT') or '')
    duration=0
    try:
        days,clock=value.split('-',1) if '-' in value else ('0',value)
        parts=[int(part) for part in clock.split(':')]
        if len(parts)==1:duration=int(days)*86400+parts[0]*60
        elif len(parts)==2:duration=int(days)*86400+parts[0]*60+parts[1]
        elif len(parts)==3:duration=int(days)*86400+parts[0]*3600+parts[1]*60+parts[2]
    except (ValueError,TypeError):
        duration=0
    count=1
    gpu=str(options.get('gpus') or options.get('gpus-per-node') or environ.get('SLURM_GPUS_ON_NODE') or environ.get('SLURM_GPUS') or '')
    if re.fullmatch(r'(?:[A-Za-z0-9_-]+:)*[0-9]{1,3}',gpu):
        count=max(1,min(256,int(gpu.rsplit(':',1)[-1])))
    elif environ.get('CUDA_VISIBLE_DEVICES'):
        count=max(1,min(256,len(environ['CUDA_VISIBLE_DEVICES'].split(','))))
    estimated=max(0,min(duration,31536000))/interval*count*256*1.25
    return int(min(256<<20,max(32<<20,estimated)))


def _error(exc):
    return "".join(c if c.isprintable() else " " for c in str(exc))[:1000]


def _atomic_json(directory_fd, name, value):
    raw = (json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n").encode("utf-8")
    if len(raw) > MAX_INDEX_BYTES:
        raise ValueError("telemetry metadata exceeded its 256 KiB budget")
    temporary = ".index-" + uuid.uuid4().hex
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600, dir_fd=directory_fd)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        os.fsync(directory_fd)
    finally:
        try:
            os.unlink(temporary, dir_fd=directory_fd)
        except FileNotFoundError:
            pass


def gpu_observation(environ, provider, timeout, cache, stop):
    """One bounded node probe; no SSH, srun, or per-counter subprocesses here."""
    from .gpu_collectors import MAX_BYTES, parse_probe_output, probe_command
    argv = probe_command(provider, timeout, cache)
    argv[0] = sys.executable
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as error:
        process = subprocess.Popen(argv, stdout=output, stderr=error, env=environ, start_new_session=True)
        deadline = time.monotonic() + timeout + .5
        try:
            while process.poll() is None:
                if stop.wait(.025) or time.monotonic() >= deadline:
                    raise TimeoutError("GPU provider cancelled" if stop.is_set() else "GPU provider exceeded its time budget")
            output.seek(0)
            raw = output.read(MAX_BYTES + 1)
            error.seek(0)
            diagnostic = error.read(1000).decode("utf-8", "replace")
            if process.returncode:
                raise RuntimeError("GPU provider failed: " + (diagnostic or str(process.returncode)))
            if len(raw) > MAX_BYTES:
                raise ValueError("GPU provider output exceeded its byte budget")
            job_id = environ.get("SLURM_JOB_ID", "")
            return parse_probe_output(raw.decode("utf-8", "replace"), job_id=job_id, require_scope=bool(job_id))
        finally:
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=1)


def process_observation(pid, expected_start=None, *, proc_root="/proc"):
    """Linux descendants, without scanning every process on the node.

    CPU is the sum for processes present at this observation; departures can
    decrease it. RSS can count shared pages repeatedly. Neither is a job total.
    """
    ticks, page = os.sysconf("SC_CLK_TCK"), os.sysconf("SC_PAGE_SIZE")
    pending, seen, cpu, rss, count, issues = collections.deque([(int(pid), None)]), set(), 0.0, 0, 0, []
    deadline = time.monotonic() + .1
    root_start = None
    while pending and len(seen) < MAX_PROCESSES and time.monotonic() < deadline:
        current, parent_pid = pending.popleft()
        if current in seen:
            continue
        seen.add(current)
        base = Path(proc_root) / str(current)
        try:
            with (base / "stat").open("rb") as stream:
                raw = stream.read(8193)
            if len(raw) > 8192:
                raise ValueError("process stat exceeds its byte budget")
            fields = raw.decode("utf-8", "replace").rsplit(")", 1)[1].split()
            started = int(fields[19])
            if parent_pid is not None and int(fields[1]) != parent_pid:
                issues.append("a descendant changed parent during inspection")
                continue
            if current == pid:
                root_start = started
                if expected_start is not None and expected_start != started:
                    raise ValueError("workload PID was reused; refusing another process's measurements")
            cpu += (int(fields[11]) + int(fields[12])) / ticks
            rss += max(0, int(fields[21])) * page
            count += 1
            with os.scandir(base / "task") as entries:
                for index, entry in enumerate(entries):
                    if index >= MAX_PROCESSES or time.monotonic() >= deadline:
                        issues.append("process/thread inspection budget reached")
                        break
                    if not entry.name.isdecimal():
                        continue
                    try:
                        with (Path(entry.path) / "children").open("rb") as stream:
                            children = stream.read(32769)
                        if len(children) > 32768:
                            issues.append("process child list exceeded byte budget")
                            continue
                        for child in children.split():
                            if len(pending) >= MAX_PROCESSES:
                                issues.append("process count budget reached")
                                break
                            pending.append((int(child), current))
                    except (FileNotFoundError, ProcessLookupError):
                        pass
        except (FileNotFoundError, ProcessLookupError):
            if current == pid:
                return None, expected_start
        except PermissionError:
            if current == pid:
                raise
            issues.append("a descendant process was inaccessible")
    if pending:
        issues.append("process inspection budget reached; descendants are partial")
    return {"cpu_seconds": cpu, "rss_bytes": rss, "process_count": count,
            "status": "partial" if issues else "recorded", "reason": "; ".join(dict.fromkeys(issues)),
            "cpu_scope": "sum of currently observed processes; excludes departed processes",
            "memory_scope": "sum of observed RSS; shared pages may be counted more than once"}, root_start


class Recording:
    """Single coordinator, one writer per family, bounded immutable prefix."""
    def __init__(self, run, plan, environ=None):
        self.run = Path(run)
        self.environ = dict(os.environ if environ is None else environ)
        self.node = self.environ.get("SLURMD_NODENAME") or socket.gethostname()
        self.plan = plan
        settings = plan.get("settings", {})
        self.cpu_enabled = not isinstance(settings, dict) or settings.get("cpu_memory", True) is not False
        defaults = settings if isinstance(settings, dict) else {}
        settings = settings.get("telemetry", {}) if isinstance(settings, dict) else {}
        settings = settings if isinstance(settings, dict) else {}
        self.interval = float(_number(settings.get("interval_seconds", defaults.get("sampling_seconds")), 5., 1., 300.))
        self.timeout = float(_number(settings.get("timeout_seconds"), 2., .2, 10.))
        self.budget = _storage_budget(plan,self.environ,settings,defaults,self.interval)
        self.segment_bytes=min(4<<20,max(1024,self.budget//4))
        self._segments={}
        from .gpu_collectors import provider_name
        self.provider = provider_name(settings.get("gpu_provider", "auto"))
        self.pid = plan.get("runtime_pid")
        self.expected_start = None
        self.stop = threading.Event()
        self.started = time.time()
        self.monotonic_start = time.monotonic()
        self.expected_nodes = max(1, int(self.environ.get("SLURM_JOB_NUM_NODES", self.environ.get("SLURM_NNODES", "1"))))
        self.fd, self.thread = None, None
        self.streams, self.cache = {}, {}
        self.error, self.sources = "", []
        root_fd = os.open(self.run, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0))
        try:
            os.mkdir("telemetry", mode=0o700, dir_fd=root_fd)
            self.fd = os.open("telemetry", os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0), dir_fd=root_fd)
        finally:
            os.close(root_fd)
        try:
            self._source("cpu-memory", CPU_SCHEMA)
            if plan.get("gpu"):
                self._source("gpu", GPU_SCHEMA)
            self._publish()
            self.thread = threading.Thread(target=self._loop, name="raven-job-telemetry", daemon=True)
            self.thread.start()
        except BaseException:
            self._release()
            raise

    def _source(self, family, schema):
        scope = ("allocated-device" if self.environ.get("SLURM_JOB_ID") else "node-visible-device") if family == "gpu" else "local-workload-process-tree"
        reason = ""
        if family == "cpu-memory" and not self.cpu_enabled:
            reason = "CPU/memory recording was explicitly disabled for this launch."
        elif family == "cpu-memory" and (sys.platform != "linux" or type(self.pid) is not int or self.pid <= 0):
            reason = "Linux procfs and a verified workload child PID are required; supervisor measurements are not workload data."
        coverage = {"node": self.node, "expected_nodes": self.expected_nodes, "observed_nodes": [],
                    "scope": scope, "samples": 0, "missed_samples": 0, "errors": [], "started_at": self.started,
                    "ended_at": None, "storage_limit_bytes": self.budget, "bytes": 0,
                    "interval_seconds": self.interval, "first_timestamp": None, "last_timestamp": None}
        if self.expected_nodes > 1:
            coverage["limitation"] = "Only the supervisor node is observed; no node-side hook or additional Slurm step is launched."
        if family == "cpu-memory":
            coverage["limitation"] = (coverage.get("limitation", "") + " Local descendant process snapshots exclude departed processes and remote ranks; RSS can count shared pages repeatedly.").strip()
        source = {"family": family, "path": "telemetry/" + family + ".jsonl", "schema": schema,
                  "status": "unavailable" if reason else "recording", "reason": reason, "coverage": coverage,
                  "metrics": ["utilization_percent", "memory_used_mib", "memory_total_mib"] if family == "gpu" else ["cpu_seconds", "rss_bytes", "process_count"], "devices": []}
        source['segments']=[]
        source['storage_format']='raven.telemetry-compact/v1'
        source['row_defaults']={"schema":schema,"run_id":self.run.name,"node":self.node,"scope":scope,
            "status":"recorded","reason":"","quality":"sampled","interval_seconds":self.interval,
            "clock":"collector-system-realtime UTC at observation completion; source clock unavailable; cross-node skew unknown",
            "source_time":None,"source":"linux-procfs" if family=='cpu-memory' else 'vendor-utility'}
        coverage.update(retained_samples=0,evicted_samples=0,written_bytes=0,retention='bounded newest segments; evictions explicitly counted')
        self._segments[family]={'number':0,'bytes':0,'samples':0,'first_timestamp':None,'last_timestamp':None}
        self.sources.append(source)
        if not reason:
            self.streams[family] = os.open(family + ".jsonl", os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600, dir_fd=self.fd)

    def _publish(self):
        # A durable index never advertises sample bytes still only in cache.
        for fd in self.streams.values():
            if fd is None:
                continue
            os.fdatasync(fd) if hasattr(os, 'fdatasync') else os.fsync(fd)
        _atomic_json(self.fd, "index.json", {"schema": INDEX_SCHEMA, "run_id": self.run.name, "node": self.node,
                     "created_at": self.started, "updated_at": time.time(), "sources": self.sources})

    def _common(self, source, timestamp):
        return {"schema": source["schema"], "sequence": source["coverage"]["samples"], "run_id": self.run.name,
                "timestamp": timestamp, "timestamp_utc": _utc(timestamp), "source_time": None,
                "clock": "collector-system-realtime UTC at observation completion; source clock unavailable; cross-node skew unknown", "elapsed_seconds": time.monotonic() - self.monotonic_start,
                "node": self.node, "scope": source["coverage"]["scope"], "status": "recorded", "reason": "",
                "quality": "sampled", "interval_seconds": self.interval, "source": "linux-procfs" if source["family"] == "cpu-memory" else "vendor-utility"}

    def _append(self, source, row):
        coverage, family = source["coverage"], source["family"]
        if self.streams[family] is None:
            # A prior rotation retained its sealed segment but could not create
            # the next file. Keep the family scheduled and retry after recovery.
            self.streams[family]=os.open(family+'.jsonl',os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,'O_NOFOLLOW',0),0o600,dir_fd=self.fd)
        # Invariant metadata lives in the checksummed export's captured index,
        # rather than repeating hundreds of bytes on every observation.
        stored={key:value for key,value in row.items() if source['row_defaults'].get(key,object())!=value}
        stored.pop('timestamp_utc',None)  # lossless UTC derives from epoch timestamp
        if family=='gpu':
            for index,device in enumerate(source['devices']):
                if all(row.get(key)==value for key,value in device.items()):
                    stored['device']=index
                    for key in device:
                        stored.pop(key,None)
                    break
        raw = (json.dumps(stored, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n").encode()
        if len(raw)>self.budget:
            coverage['missed_samples']+=1
            source.update(status='partial',reason='One telemetry row exceeds its storage budget; later observations will retry.')
            return False
        segment=self._segments[family]
        if segment['bytes'] and segment['bytes']+len(raw)>self.segment_bytes:
            os.fsync(self.streams[family])
            number=segment['number']+1
            name=f"{family}.segment-{number:08d}.jsonl"
            os.rename(family+'.jsonl',name,src_dir_fd=self.fd,dst_dir_fd=self.fd)
            os.close(self.streams[family]);self.streams[family]=None
            source['segments'].append(dict(path='telemetry/'+name,bytes=segment['bytes'],samples=segment['samples'],
                first_timestamp=segment['first_timestamp'],last_timestamp=segment['last_timestamp']))
            segment.update(number=number,bytes=0,samples=0,first_timestamp=None,last_timestamp=None)
            self.streams[family]=os.open(family+'.jsonl',os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,'O_NOFOLLOW',0),0o600,dir_fd=self.fd)
        while source['segments'] and (coverage['bytes']+len(raw)>self.budget or len(source['segments'])>=128):
            expired=source['segments'].pop(0)
            os.unlink(expired['path'].split('/')[-1],dir_fd=self.fd)
            coverage['bytes']-=expired['bytes'];coverage['retained_samples']-=expired['samples']
            coverage['evicted_samples']+=expired['samples']
            source.update(status='partial',reason='Storage budget: oldest telemetry segments evicted; newest observations continue.')
        descriptor=self.streams[family]
        initial=os.lseek(descriptor,0,os.SEEK_END)
        try:
            view = memoryview(raw)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError("telemetry stream write made no progress")
                view = view[written:]
        except OSError:
            os.ftruncate(descriptor,initial);os.lseek(descriptor,initial,os.SEEK_SET)
            raise
        segment['bytes']+=len(raw);segment['samples']+=1
        segment['first_timestamp']=row['timestamp'] if segment['first_timestamp'] is None else segment['first_timestamp']
        segment['last_timestamp']=row['timestamp']
        coverage["bytes"] += len(raw)
        coverage['written_bytes']+=len(raw);coverage['retained_samples']+=1
        coverage["samples"] += 1
        if row["node"] not in coverage["observed_nodes"]:
            coverage["observed_nodes"].append(row["node"])
        if row.get("status") == "partial":
            coverage["partial_samples"] = coverage.get("partial_samples", 0) + 1
        coverage["first_timestamp"] = row["timestamp"] if coverage["first_timestamp"] is None else coverage["first_timestamp"]
        coverage["last_timestamp"] = row["timestamp"]
        return True

    def _sample(self, source):
        if source["family"] == "cpu-memory":
            values, self.expected_start = process_observation(self.pid, self.expected_start)
            if values is not None:
                for key in ('cpu_scope','memory_scope'):
                    if isinstance(values.get(key),str):
                        source['row_defaults'][key]=values[key]
                self._append(source, dict(self._common(source, time.time()), **values))
            return
        samples, self.cache, warnings = gpu_observation(self.environ, self.provider, self.timeout, self.cache, self.stop)
        timestamp = time.time()
        if not samples:
            raise RuntimeError("; ".join(warnings) or "No assigned GPU devices with supported counters were verified.")
        if warnings:
            source["coverage"]["errors"] = list(dict.fromkeys(source["coverage"]["errors"] + warnings))[-16:]
        for sample in samples:
            identity = {"node": sample.node, "provider": sample.vendor, "device_uuid": sample.uuid,
                        "device_index": sample.index, "device_bdf": sample.bdf, "partition": sample.partition}
            if identity not in source["devices"] and len(source["devices"]) < 256:
                source["devices"].append(identity)
            missing = [name for name, value in (("utilization", sample.util), ("memory used", sample.used), ("memory total", sample.total)) if value is None]
            row = dict(self._common(source, timestamp), **identity, utilization_percent=sample.util,
                       memory_used_mib=sample.used, memory_total_mib=sample.total)
            if missing:
                row.update(status="partial", reason="Unsupported/unavailable counters: " + ", ".join(missing))
                source["coverage"]["counter_gaps"] = sorted(set(source["coverage"].get("counter_gaps", []) + missing))
            if not self._append(source, row):
                break

    def _loop(self):
        failures, due = {}, {}
        try:
            while not self.stop.is_set() and self.streams:
                now = time.monotonic()
                changed = False
                for source in self.sources:
                    family = source["family"]
                    if family not in self.streams or now < due.get(family, 0) or self.stop.is_set():
                        continue
                    changed = True
                    try:
                        self._sample(source)
                        failures[family] = 0
                    except (OSError, ValueError, RuntimeError, IndexError) as exc:
                        if self.stop.is_set():
                            break
                        failures[family] = failures.get(family, 0) + 1
                        source["coverage"]["missed_samples"] += 1
                        source["coverage"]["errors"] = (source["coverage"]["errors"] + [_error(exc)])[-16:]
                        source["reason"] = _error(exc)
                        if isinstance(exc, OSError) and not isinstance(exc, (TimeoutError, PermissionError)) and exc.errno not in {errno.ENOSPC,errno.EDQUOT,errno.EIO,errno.EAGAIN,errno.EINTR}:
                            source["status"] = "partial" if source["coverage"]["samples"] else "failed"
                            descriptor=self.streams.pop(family)
                            if descriptor is not None: os.close(descriptor)
                    due[family] = time.monotonic() + min(60., self.interval * 2 ** min(failures.get(family, 0), 4))
                if changed:
                    try:
                        self._publish()
                    except OSError as exc:
                        if exc.errno not in {errno.ENOSPC, errno.EDQUOT, errno.EIO, errno.EAGAIN, errno.EINTR}:
                            raise
                        for source in self.sources:
                            source['coverage']['errors'] = (source['coverage']['errors'] + [_error(exc)])[-16:]
                            source['reason'] = 'Durable publication awaits storage recovery: ' + _error(exc)
                next_due = min((due.get(family, time.monotonic() + .25) for family in self.streams), default=time.monotonic() + .25)
                self.stop.wait(max(.01, next_due - time.monotonic()))
        except Exception as exc:
            self.error = _error(exc)
        finally:
            ended = time.time()
            for source in self.sources:
                coverage = source["coverage"]
                coverage["ended_at"] = ended
                if source["status"] == "recording":
                    partial = bool(coverage["missed_samples"] or coverage["errors"] or coverage.get("counter_gaps") or coverage.get("partial_samples") or self.expected_nodes > 1)
                    source["status"] = ("partial" if partial else "complete") if coverage["samples"] else "unavailable"
                    if not coverage["samples"] and not source["reason"]:
                        source["reason"] = "No samples were retained before the workload ended."
                    elif self.expected_nodes > 1:
                        source["reason"] = coverage["limitation"]
                    elif coverage.get("counter_gaps"):
                        source["reason"] = "Unavailable counters: " + ", ".join(coverage["counter_gaps"])
                    elif coverage.get("partial_samples"):
                        source["reason"] = "Some process observations reached their inspection budget; retained rows include reasons."
                if self.error:
                    source.update(status="partial" if coverage["samples"] else "failed", reason=self.error)
            try:
                self._publish()
            except (OSError, ValueError):
                pass
            self._release()

    def _release(self):
        for fd in self.streams.values():
            if fd is not None: os.close(fd)
        self.streams.clear()
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join(self.timeout + 2.)
            if self.thread.is_alive():
                self.error = "Recorder cleanup exceeded its deadline; retained recording may be incomplete."
        requested = [source for source in self.sources if source["family"] != "cpu-memory" or self.cpu_enabled]
        states = {source["status"] for source in requested}
        samples = any(source["coverage"]["samples"] for source in requested)
        status = ("complete" if states == {"complete"} else "partial" if samples else
                  "failed" if self.error or "failed" in states else "unavailable")
        return {"schema": INDEX_SCHEMA, "status": status, "sources": self.sources, "error": self.error}


class UnavailableRecording:
    def __init__(self, reason):
        self.error = reason
    def close(self):
        return {"schema": INDEX_SCHEMA, "sources": [], "status": "failed", "reason": self.error}


def start_recording(run, plan, environ=None):
    """Optional instrumentation can never replace the workload's exit status."""
    try:
        return Recording(run, plan, environ)
    except Exception as exc:
        return UnavailableRecording(_error(exc))


from .retained_telemetry_io import parse_rows as parse_rows, validate_index as validate_index


def display_gpu_rows(rows):
    """Adapt retained GPU rows without reducing device identity to an ordinal."""
    result = []
    for row in rows:
        index = row.get("device_index")
        if type(index) is not int or index < 0:
            continue
        identity = "/".join(_error(row.get(key) or "") for key in ("node", "provider"))
        identity += "/" + _error(row.get("device_uuid") or row.get("device_bdf") or str(index))
        if row.get("partition"):
            identity += "/" + _error(row["partition"])
        result.append({"t": row["timestamp"], "index": index, "device_id": identity,
                       "util": row.get("utilization_percent"), "mem": row.get("memory_used_mib"),
                       "source": row.get("source"), "scope": row.get("scope"),
                       "sample_interval": _number(row.get("interval_seconds"), 5., 1., 300.)})
    return result
