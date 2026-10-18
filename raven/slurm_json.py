"""Bounded Slurm JSON normalizers qualified against recorded data-parser shapes.

Unknown descriptive fields are ignored. Schema identity, null/sentinel numbers,
record identity and operational fields are validated rather than guessed.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import math
import re
import time

from .model import Job, Finished, gres_gpus, gpus_in_tres, hms

SUPPORTED_PARSERS = frozenset(("data_parser/v0.0.45",))
MAX_BYTES = 16 << 20
MAX_JOBS = 20000
_CONTROLS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


class UnsupportedShape(ValueError):
    pass


def document(text):
    if not isinstance(text, str) or len(text) > MAX_BYTES or len(text.encode("utf-8")) > MAX_BYTES:
        raise ValueError("Slurm JSON exceeds the 16 MiB reply bound")
    def pairs(items):
        result = {}
        for key, value in items:
            if not key or len(key) > 256 or not key.isprintable():
                raise ValueError("Invalid bounded Slurm JSON key")
            if key in result:
                raise ValueError("Duplicate Slurm JSON field")
            result[key] = value
        return result
    def constant(_):
        raise ValueError("Nonfinite Slurm JSON number")
    try:
        value = json.loads(text, object_pairs_hook=pairs, parse_constant=constant)
        # Decode validates keys once in the pairs hook. The remaining linear
        # walk preserves depth/node/string/numeric bounds without revalidating
        # every key or scanning each ASCII character in Python.
        pending, count = [(value, 0)], 0
        while pending:
            item, depth = pending.pop()
            count += 1
            if count > 1000000 or depth > 24:
                raise ValueError("Slurm JSON nesting or node count exceeds its bound")
            kind = type(item)
            if kind is dict:
                pending.extend((child, depth + 1) for child in item.values())
            elif kind is list:
                pending.extend((child, depth + 1) for child in item)
            elif kind is str:
                if len(item) > 65536 or _CONTROLS.search(item):
                    raise ValueError("Invalid bounded Slurm JSON string")
            elif kind in (int, float) and (abs(item) > 1e100 or not math.isfinite(item)):
                raise ValueError("Invalid bounded Slurm JSON number")
    except (RecursionError, OverflowError, UnicodeError) as exc:
        raise ValueError("Invalid bounded Slurm JSON") from exc
    if not isinstance(value, dict) or not isinstance(value.get("jobs"), list):
        raise ValueError("Slurm JSON has no jobs array")
    if value.get("errors"):
        raise ValueError("Slurm JSON reports scheduler errors; inspect the source diagnostics")
    meta = value.get("meta")
    if not isinstance(meta, dict):
        raise ValueError("Slurm JSON has no data-parser identity")
    # Recorded 23.02 uses the plural metadata key. Recognize its explicit
    # capability identity, but do not thereby qualify that payload normalizer.
    keys = [key for key in ("plugin", "plugins") if key in meta]
    if len(keys) != 1 or not isinstance(meta[keys[0]], dict):
        raise ValueError("Slurm JSON has missing or ambiguous data-parser identity")
    parser = meta[keys[0]].get("data_parser")
    if not isinstance(parser, str):
        raise ValueError("Slurm JSON has no data-parser identity")
    if parser not in SUPPORTED_PARSERS:
        raise UnsupportedShape("unqualified Slurm JSON data-parser " + parser[:80])
    if len(value["jobs"]) > MAX_JOBS:
        raise ValueError("Slurm JSON exceeds the 20000-job bound")
    return value


def number(value):
    if isinstance(value, dict):
        if type(value.get("set")) is not bool or type(value.get("infinite")) is not bool:
            raise ValueError("Malformed Slurm numeric sentinel")
        if not value["set"] or value["infinite"]:
            return None
        value = value.get("number")
    if value is None:
        return None
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0 or value > 2**63 - 1:
        raise ValueError("Invalid Slurm numeric value")
    return value


def integer(value, *, required=False):
    result = number(value)
    if result is None:
        if required:
            raise ValueError("Required Slurm count is unavailable")
        return None
    if int(result) != result:
        raise ValueError("Slurm count is not integral")
    return int(result)


def text(value, *, default=""):
    if value is None:
        return default
    if not isinstance(value, str) or len(value) > 65536 or "\0" in value:
        raise ValueError("Invalid Slurm text field")
    return value


def timestamp(value):
    seconds = number(value)
    if not seconds:
        return ""
    try:
        return datetime.fromtimestamp(seconds, timezone.utc).isoformat()
    except (ValueError, OverflowError, OSError) as exc:
        raise ValueError("Invalid scheduler timestamp") from exc


def state(value):
    if isinstance(value, list):
        if not value or not all(isinstance(item, str) for item in value):
            raise ValueError("Invalid Slurm state list")
        value = value[0]
    if not isinstance(value, str) or not re.fullmatch(r"[A-Z][A-Z_+]*", value):
        raise ValueError("Invalid Slurm job state")
    return value


def identifier(job):
    jid = integer(job.get("job_id"), required=True)
    array, task = integer(job.get("array_job_id")), integer(job.get("array_task_id"))
    if array and task is not None:
        return f"{array}_{task}"
    array_tasks = text(job.get("array_task_string"))
    if array and array_tasks:
        if not re.fullmatch(r"[0-9,:%-]+", array_tasks):
            raise ValueError("Invalid Slurm array range")
        return f"{array}_[{array_tasks}]"
    het, offset = integer(job.get("het_job_id")), integer(job.get("het_job_offset"))
    return f"{het}+{offset}" if het and offset is not None else str(jid)


def runtime(job, observed):
    start = number(job.get("start_time"))
    if not start:
        return 0 if state(job.get("job_state")) == "PENDING" else None
    suspended = number(job.get("suspend_time")) or 0
    prior = number(job.get("pre_sus_time")) or 0
    if state(job.get("job_state")) == "SUSPENDED":
        return prior
    end = number(job.get("end_time")) or observed
    if state(job.get("job_state")) in ("RUNNING", "COMPLETING", "CONFIGURING"):
        end = observed
    if end is None:
        return None
    return max(0, end - suspended + prior if suspended else end - start)


def limit(value):
    if isinstance(value, dict) and value.get("infinite") is True:
        return "UNLIMITED"
    result = number(value)
    return hms(result * 60) if result is not None else ""


def queue_record(job, observed):
    if not isinstance(job, dict):
        raise ValueError("Slurm job is not an object")
    cpus, nodes = integer(job.get("cpus"), required=True), integer(job.get("node_count"), required=True)
    tres = text(job.get("tres_alloc_str")) or text(job.get("tres_req_str"))
    gpu_type, _ = gres_gpus(tres)
    memory = number(job.get("memory_per_node"))
    if memory is None:
        memory = number(job.get("memory_per_cpu"))
        mem_req = f"{memory:g}Mc" if memory is not None else ""
    else:
        mem_req = f"{memory:g}M"
    elapsed = runtime(job, observed)
    return Job(id=identifier(job), name=text(job.get("name")), partition=text(job.get("partition")),
               state=state(job.get("job_state")), elapsed=hms(elapsed) if elapsed is not None else "",
               limit=limit(job.get("time_limit")), nodes=nodes, cpus=cpus, gpu_type=gpu_type,
               gpus=gpus_in_tres(tres), nodelist=text(job.get("nodes")), mem_req=mem_req,
               start=timestamp(job.get("start_time")), submit=timestamp(job.get("submit_time")),
               reason=text(job.get("state_description")) or text(job.get("state_reason")),
               priority=integer(job.get("priority")) or 0, dependency=text(job.get("dependency")),
               account=text(job.get("account")), qos=text(job.get("qos")), end=timestamp(job.get("end_time")),
               command=text(job.get("command")), user=text(job.get("user_name")),
               workdir=text(job.get("current_working_directory")), comment=text(job.get("comment")),
               cluster=text(job.get("cluster")))


def queue(value, report, observed=None):
    observed = time.time() if observed is None else number(observed)
    output, seen, duplicate = [], set(), set()
    for record in value["jobs"]:
        report.rows += 1
        try:
            job = queue_record(record, observed)
        except (ValueError, TypeError, KeyError):
            report.issue("invalid structured job record")
            continue
        if job.id in seen:
            duplicate.add(job.id)
            report.issue("duplicate structured job identity")
            continue
        seen.add(job.id)
        output.append(job)
        report.accepted += 1
    return [job for job in output if job.id not in duplicate]


def detail(value, jid, report, observed=None):
    records = []
    for record in value["jobs"]:
        try:
            if identifier(record) == jid:
                records.append(record)
        except (ValueError, TypeError):
            report.issue("invalid structured job identity")
    report.rows = len(value["jobs"])
    if len(records) != 1:
        report.issue("missing or duplicate selected structured job")
        return {}
    job = records[0]
    normalized = queue_record(job, time.time() if observed is None else observed)
    fields = {"JobId": str(integer(job.get("job_id"), required=True)), "JobName": normalized.name,
              "JobState": normalized.state, "UserId": f"{normalized.user}({integer(job.get('user_id'), required=True)})",
              "Cluster": normalized.cluster, "SubmitTime": normalized.submit, "StartTime": normalized.start,
              "Restarts": str(integer(job.get("restart_cnt"), required=True)), "RunTime": normalized.elapsed,
              "TimeLimit": normalized.limit, "WorkDir": normalized.workdir, "Command": normalized.command,
              "Account": normalized.account, "Partition": normalized.partition, "QOS": normalized.qos,
              "NumCPUs": str(normalized.cpus), "NumNodes": str(normalized.nodes), "NodeList": normalized.nodelist,
              "StdOut": text(job.get("stdout_expanded")) or text(job.get("standard_output")),
              "StdErr": text(job.get("stderr_expanded")) or text(job.get("standard_error")),
              "AllocTRES": text(job.get("tres_alloc_str")), "ReqTRES": text(job.get("tres_req_str"))}
    array, task = integer(job.get("array_job_id")), integer(job.get("array_task_id"))
    if array and task is not None:
        fields.update(ArrayJobId=str(array), ArrayTaskId=str(task))
    report.accepted = 1
    return fields


def _tres(rows, kind, name=""):
    if not isinstance(rows, list):
        raise ValueError("Invalid Slurm TRES list")
    matches = [row for row in rows if isinstance(row, dict) and row.get("type") == kind and row.get("name", "") == name]
    if len(matches) > 1:
        raise ValueError("Ambiguous Slurm TRES entry")
    return number(matches[0].get("count")) if matches else None


def accounting_record(job):
    if not isinstance(job, dict):
        raise ValueError("Slurm accounting job is not an object")
    array, het = job.get("array", {}), job.get("het", {})
    jid = identifier({"job_id": job.get("job_id"), "array_job_id": array.get("job_id"),
                      "array_task_id": array.get("task_id"), "array_task_string": array.get("task"),
                      "het_job_id": het.get("job_id"), "het_job_offset": het.get("job_offset")})
    times, tres, required = job["time"], job["tres"], job["required"]
    cpus = _tres(tres["allocated"], "cpu")
    if cpus is None:
        cpus = integer(required.get("CPUs"), required=True)
    nodes = integer(job.get("allocation_nodes"), required=True)
    memory = number(required.get("memory_per_cpu"))
    if memory is not None:
        request = memory * cpus * 1024**2
    else:
        memory = number(required.get("memory_per_node"))
        request = memory * nodes * 1024**2 if memory is not None else 0
    rss_values = []
    for step in job.get("steps", []):
        rss = _tres(step.get("tres", {}).get("requested", {}).get("max", []), "mem")
        if rss is not None:
            rss_values.append(rss)
    total = times.get("total", {})
    seconds, micros = number(total.get("seconds")), number(total.get("microseconds"))
    cpu_time = seconds + micros / 1e6 if seconds is not None and micros is not None else None
    exit_code = job.get("exit_code", {})
    returned = integer(exit_code.get("return_code"))
    signal = integer(exit_code.get("signal", {}).get("id"))
    # An unset signal means no terminating signal when a return code is known.
    exit_text = f"{returned}:{signal or 0}" if returned is not None else ""
    gpu = _tres(tres["allocated"], "gres", "gpu")
    if gpu is None:
        gpu = _tres(tres.get("requested", []), "gres", "gpu")
    return Finished(id=jid, name=text(job.get("name")), state=state(job["state"]["current"]),
                    elapsed=hms(number(times.get("elapsed"))) if number(times.get("elapsed")) is not None else "",
                    cpus=int(cpus), cpu_time=cpu_time, req_mem=request, rss=max(rss_values, default=0),
                    start=timestamp(times.get("start")), end=timestamp(times.get("end")),
                    partition=text(job.get("partition")), nodes=nodes, exit=exit_text, gpus=int(gpu or 0),
                    nodelist=text(job.get("nodes")), submit=timestamp(times.get("submission")),
                    workdir=text(job.get("working_directory")), limit=limit(times.get("limit")),
                    user=text(job.get("user")), account=text(job.get("account")),
                    comment=text(job.get("comment", {}).get("job")), cluster=text(job.get("cluster")))


def accounting(value, report):
    records, seen, duplicates = [], set(), set()
    for job in value["jobs"]:
        report.rows += 1
        try:
            record = accounting_record(job)
        except (ValueError, KeyError, TypeError, AttributeError):
            report.issue("invalid structured accounting record")
            continue
        report.accepted += 1
        if record.id in seen:
            duplicates.add(record.id)
            report.issue("duplicate structured accounting attempt")
        seen.add(record.id)
        records.append(record)
    records = [record for record in records if record.id not in duplicates and record.state not in
               ("RUNNING", "PENDING", "COMPLETING", "CONFIGURING", "SUSPENDED")]
    return sorted(records, key=lambda record: record.end, reverse=True)


def _cpu_time(times):
    total = times.get("total", {})
    seconds, micros = number(total.get("seconds")), number(total.get("microseconds"))
    return f"{seconds + micros / 1e6:.6f}" if seconds is not None and micros is not None else ""


def _exit(value):
    result = integer(value.get("return_code"))
    signal = integer(value.get("signal", {}).get("id"))
    return f"{result}:{signal or 0}" if result is not None else ""


def _tres_text(rows):
    output = []
    for row in rows:
        kind, name = text(row.get("type")), text(row.get("name"))
        if kind not in ("cpu", "mem", "node", "gres"):
            continue
        count = number(row.get("count"))
        if count is None:
            continue
        key = kind + ("/" + name if name else "")
        output.append(f"{key}={count:g}" + ("M" if kind == "mem" else ""))
    return ",".join(output)


def accounting_rows(value, jid, kind, report):
    """Typed internal rows retain literal delimiters; no lossy JSON-to-text hop."""
    rows = []
    for job in value["jobs"]:
        report.rows += 1
        try:
            normalized = accounting_record(job)
            if normalized.id != jid:
                report.accepted += 1
                continue
            identity = [normalized.submit, normalized.start, normalized.user, normalized.cluster]
            if kind == "logs":
                # The selected historical record also supplies native archive
                # attribution after scontrol has purged the completed job.
                # Missing counters remain unknown; never infer zero.
                restart = integer(job.get("restart_cnt"))
                if restart is not None and restart > 9999999999:
                    raise ValueError("Accounting restart count exceeds its identity bound")
                rows.append([jid, str(job["job_id"]), normalized.name, normalized.workdir,
                             text(job.get("stdout")), text(job.get("stderr")), normalized.user,
                             normalized.submit, normalized.start, normalized.cluster,
                             str(restart) if restart is not None else ""])
            elif kind == "submit":
                req = job["required"]
                per_cpu = number(req.get("memory_per_cpu"))
                per_node = number(req.get("memory_per_node"))
                memory = f"{per_cpu:g}Mc" if per_cpu is not None else f"{per_node:g}Mn" if per_node is not None else ""
                rows.append([jid, text(job.get("submit_line")), normalized.workdir, normalized.name,
                             normalized.partition, normalized.account, text(job.get("qos")),
                             str(integer(req.get("CPUs"), required=True)), memory, normalized.limit,
                             str(normalized.nodes), _tres_text(job["tres"]["requested"]), *identity])
            elif kind == "steps":
                rows.append([jid, normalized.name, normalized.state, normalized.elapsed, _cpu_time(job["time"]),
                             "", "", "", str(normalized.nodes), normalized.exit, normalized.nodelist,
                             normalized.end, *identity])
                for step in job.get("steps", []):
                    times = step["time"]
                    maximum = [row for row in step.get("tres", {}).get("requested", {}).get("max", []) if row.get("type") == "mem"]
                    if len(maximum) > 1:
                        raise ValueError("Ambiguous step memory")
                    rss = number(maximum[0].get("count")) if maximum else None
                    rows.append([text(step["step"]["id"]), text(step["step"].get("name")), state(step.get("state")),
                                 hms(number(times.get("elapsed"))), _cpu_time(times), str(rss) if rss is not None else "",
                                 text(maximum[0].get("node")) if maximum else "",
                                 str(integer(maximum[0].get("task"))) if maximum else "",
                                 str(integer(step.get("tasks", {}).get("count"), required=True)), _exit(step.get("exit_code", {})),
                                 text(step.get("nodes", {}).get("range")), timestamp(times.get("end")),
                                 normalized.submit, timestamp(times.get("start")), normalized.user, normalized.cluster])
            else:
                raise ValueError("Unknown structured accounting operation")
        except (ValueError, KeyError, TypeError, AttributeError):
            report.issue("invalid selected structured accounting record")
            continue
        report.accepted += 1
    return rows
