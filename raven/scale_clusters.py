"""Independent bounded readers for concurrent cluster workspaces."""
from __future__ import annotations

from dataclasses import asdict, replace
from datetime import datetime, timedelta
import hashlib
import json
import re

from .scale_common import document, items, number, text
from .worker_scheduler import collection_lane

MAX_CLUSTERS = 8


def configuration(value, cfg=None):
    cfg = cfg or {}
    if value is None:
        profiles = cfg.get("profiles") or {}
        value = {"schema": "raven.cluster-workspace/v1", "clusters": [dict(name=name, profile=name) for name in sorted(profiles)]}
    document(value, "raven.cluster-workspace/v1")
    seen, result = set(), []
    for record in items(value.get("clusters"), "clusters", limit=MAX_CLUSTERS, empty=False):
        name = text(record.get("name"), "cluster workspace name", limit=80)
        if name in seen:
            raise ValueError("Cluster workspace names must be unique")
        seen.add(name)
        profile = record.get("profile", "")
        inherited = {}
        if profile:
            text(profile, "profile", limit=80)
            inherited = (cfg.get("profiles") or {}).get(profile)
            if not isinstance(inherited, dict):
                raise ValueError(f"Unknown cluster profile: {profile}")
        host = record.get("host", inherited.get("host", cfg.get("host", "") if profile else ""))
        user = record.get("ssh_user", inherited.get("ssh_user", cfg.get("ssh_user", "")))
        for label, field in (("host", host), ("ssh_user", user)):
            text(field, label, empty=True)
            if field and (field.startswith("-") or not re.fullmatch(r"[A-Za-z0-9_.:@\[\]-]+", field)):
                raise ValueError(f"Invalid {label}")
        cluster = record.get("cluster", inherited.get("cluster_name", inherited.get("cluster", cfg.get("cluster_name", "") if profile else "")))
        text(cluster, "Slurm cluster name", empty=True)
        if cluster and not re.fullmatch(r"[A-Za-z0-9_.-]+", cluster):
            raise ValueError("Invalid Slurm cluster name")
        opts = inherited.get("ssh_opts", cfg.get("ssh_opts", []))
        if not isinstance(opts, list) or len(opts) > 32 or any(not isinstance(opt, str) or not opt.isprintable() or len(opt) > 1024 for opt in opts):
            raise ValueError("Invalid profile SSH options")
        slurm_user = record.get("user", inherited.get("user", ""))
        text(slurm_user, "Slurm user", empty=True, limit=128)
        if slurm_user and (not re.fullmatch(r"[A-Za-z0-9_.@-]+", slurm_user) or slurm_user.startswith("-")):
            raise ValueError("Invalid profile Slurm user")
        descriptor = dict(name=name, profile=profile, host=host, ssh_user=user, user=slurm_user, cluster=cluster, ssh_opts=list(opts))
        descriptor["cluster_key"] = hashlib.sha256(json.dumps(descriptor, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:24]
        result.append(descriptor)
    return result


def collect_one(ctx, source, user, hours):
    from .operations import command, checkpoint
    from .remote import SshBackend, LocalFiles, RemoteFiles
    from .slurm import Backend, Slurm, JOB_FMT, SACCT_FIELDS, parse_jobs, parse_sacct
    # Never reuse or mutate the selected connection. Each source owns its adapter.
    backend = SshBackend(source["host"], source["ssh_user"], opts=source["ssh_opts"], control=False) if source["host"] else Backend()
    subctx = replace(ctx, slurm=Slurm(backend, user), files=RemoteFiles(backend) if source["host"] else LocalFiles(),
                     scope=source, jobs=(), finished=(), selected="")
    options = ["-M", source["cluster"]] if source["cluster"] else []
    since = (datetime.now() - timedelta(hours=hours)).isoformat(timespec="seconds")
    queries = [("queue", ["squeue", *options, "-u", user, "-h", "-o", JOB_FMT], parse_jobs),
               ("history", ["sacct", *options, "-u", user, "-S", since, "-X", "--duplicates", "-n", "-P", "--format=" + SACCT_FIELDS], parse_sacct)]
    jobs, failures = [], []
    for kind, argv, parse in queries:
        checkpoint(ctx)
        try:
            output = command(subctx, argv, timeout=4, limit=1 << 20)
            lines = [line for line in output.splitlines() if line.strip() and not line.startswith("CLUSTER:")]
            if any(len(line.split("|")) < (13 if kind == "history" else 15) for line in lines):
                raise ValueError("Malformed cluster job records")
            # The shared history parser combines steps by numeric ID. Parse each
            # allocation row separately here to preserve IDs reused over time.
            records = [record for line in lines for record in parse(line)]
            if len(records) > 5000:
                raise ValueError("Cluster job limit of 5000 exceeded")
            for record in records:
                item = asdict(record)
                attempt = str(item.get("submit") or "") + "/" + str(item.get("start") or "")
                item.update(cluster_key=source["cluster_key"], cluster_name=source["name"], kind=kind,
                            attempt=attempt, attempt_known=bool(item.get("submit")), job_id=item["id"])
                item["identity"] = [source["cluster_key"], item["id"], attempt]
                json.dumps(item, allow_nan=False)
                jobs.append(item)
        except Exception as exc:
            if ctx.cancel is not None and ctx.cancel.is_set():
                raise
            failures.append(f"{kind}: {str(exc)[:400]}")
    # Queue wins only for exactly the same attempt; reused IDs remain separate.
    unique = {}
    for job in jobs:
        key = tuple(job["identity"])
        if key not in unique or job["kind"] == "queue":
            unique[key] = job
    return dict(source=source, jobs=list(unique.values()), errors=failures,
                status="unavailable" if len(failures) == len(queries) else "partial" if failures else "ok")


def collect(ctx, sources, *, user="", history_hours=24):
    from .operations import checkpoint
    checkpoint(ctx)
    if ctx.replay:
        raise ValueError("Concurrent live clusters are unavailable in a recorded session")
    explicit_user = user
    user = user or getattr(ctx.slurm, "user", "")
    text(user, "Slurm user", limit=128)
    if not re.fullmatch(r"[A-Za-z0-9_.@-]+", user) or user.startswith("-"):
        raise ValueError("Invalid Slurm user")
    hours = number(history_hours, "history_hours")
    if not 0 < hours <= 24 * 31:
        raise ValueError("history_hours must be in (0, 744]")
    if not 1 <= len(sources) <= MAX_CLUSTERS:
        raise ValueError("Cluster count must be between 1 and 8")
    results = []
    mode = "single" if ctx.cfg.get("worker_mode") == "single" else "multi"
    with collection_lane(workers=min(4, len(sources)), mode=mode) as (scheduler, pool):
        budget = scheduler.status()
        window = min(4, len(sources), budget["lanes"]["source"]["limit"])
        workers = 1 if budget["mode"] == "single" else window
        remaining, futures = iter(sources), {}
        exhausted = False
        while futures or not exhausted:
            checkpoint(ctx)
            # Four pending descendants fit the scheduler's bounded child
            # reservation even if unrelated accepted roots fill its queue.
            while not exhausted and len(futures) < window:
                source = next(remaining, None)
                if source is None:
                    exhausted = True
                    break
                future = pool.submit(collect_one, ctx, source, explicit_user or source.get("user") or user, hours)
                futures[future] = source
            if not futures:
                break
            done, _ = scheduler.gather_children(futures, timeout=.1)
            for future in done:
                checkpoint(ctx)
                source = futures.pop(future)
                try:
                    results.append(future.result())
                except Exception as exc:
                    checkpoint(ctx)
                    results.append(dict(source=source, jobs=[], errors=[str(exc)[:400]], status="unavailable"))
    results.sort(key=lambda result: result["source"]["name"])
    jobs, rows, warnings, summaries = [], [], [], []
    result_bytes, omitted = 0, 0
    for result in results:
        name = result["source"]["name"]
        rows.append(f"{name}: {result['status']}; {len(result['jobs'])} job attempts")
        public_source = {key: value for key, value in result["source"].items() if key != "ssh_opts"}
        summaries.append({key: value for key, value in result.items() if key not in ("jobs", "source")} |
                         {"source": public_source, "job_count": len(result["jobs"])})
        for job in sorted(result["jobs"], key=lambda r: (r["submit"], r["id"]), reverse=True):
            size = len(json.dumps(job, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode())
            if len(jobs) >= 5000 or result_bytes + size > 4 << 20:
                omitted += 1
                continue
            result_bytes += size
            jobs.append(job)
            rows.append(f"  {job['id']} | {job['state']} | {job['name']} | submit {job.get('submit') or 'unknown'} | {job.get('partition', '')}")
        warnings.extend(f"{name}: {error}" for error in result["errors"])
    if omitted:
        warnings.append(f"{omitted} job rows omitted by the workspace's 5000-row / 4 MiB result budget; narrow the source set or history window")
    return {"sources": summaries, "jobs": jobs, "rows": rows, "warnings": warnings,
            "workers": workers, "read_only": True, "omitted_jobs": omitted}
