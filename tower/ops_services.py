"""Reviewed service/data operations and native workflow observation."""
from __future__ import annotations

from pathlib import Path

from .operations import digest, local_only, prepare_plan, read_json, report, validate_plan
from . import services_staging, services_workflows, supervisor

SPECIFICATIONS = [
    {"key": "supervisor", "title": "Persistent monitor", "group": "Campaigns", "proposal": "S03",
     "summary": "Start, reconnect to, or stop a durable read-only Slurm monitor.",
     "fields": [{"key": "action", "label": "Action", "default": "status", "choices": ["status", "start", "stop"]},
                {"key": "directory", "label": "Private state directory", "default": ""},
                {"key": "interval", "label": "Poll seconds (1–300)", "default": "5"},
                {"key": "max_records", "label": "Records per source (1–10000)", "default": "1000"}]},
    {"key": "staging", "title": "Verified staging and return", "group": "Data", "proposal": "S04",
     "summary": "Review a declared transfer manifest, verify every file, and retain resumable receipts.",
     "fields": [{"key": "manifest", "label": "Staging manifest JSON", "default": "", "required": True},
                {"key": "direction", "label": "Transfer direction", "default": "all", "choices": ["all", "input", "output"]}]},
    {"key": "workflow-engine", "title": "Native workflow observer", "group": "Workflows", "proposal": "A11",
     "summary": "Read Nextflow trace or Snakemake DAG/runtime evidence without taking execution ownership.",
     "fields": [{"key": "engine", "label": "Workflow engine", "default": "nextflow", "choices": ["nextflow", "snakemake"]},
                {"key": "source", "label": "Trace or DAG file", "default": "", "required": True},
                {"key": "workflow_id", "label": "Workflow identity (optional)", "default": ""}]},
]


def _directory(ctx, params):
    explicit = params.get("directory", "")
    if explicit:
        return str(Path(explicit).expanduser().absolute())
    if not ctx.state_dir:
        raise ValueError("Set a private state directory for the persistent monitor")
    return str(Path(ctx.state_dir) / "supervisor" / digest(ctx.scope)[:24])


def _supervisor_rows(view):
    rows = [f"Service: {view['state']}", f"Running: {'yes' if view.get('running') else 'no'}",
            f"User: {view.get('config', {}).get('user', 'unknown')} | Cluster: {view.get('config', {}).get('cluster') or 'local default'}",
            f"Queue records: {len(view.get('active', []))} | Accounting records: {len(view.get('finished', []))}",
            "Ownership: monitor only; no scheduler changes, recovery, or submissions"]
    rows += [f"{item.get('id')}  {item.get('before') or 'first seen'} → {item.get('after')}"
             for item in view.get("events", [])[-50:]]
    return rows


def run(feature, params, ctx):
    if feature == "supervisor":
        local_only(ctx)
        directory = _directory(ctx, params)
        action = params.get("action", "status")
        if action not in ("status", "start", "stop"):
            raise ValueError("Unknown supervisor action")
        current = supervisor.status(directory)
        rows = [f"State directory: {directory}", *_supervisor_rows(current)]
        warnings = [str(item.get("reason", "")) for item in current.get("errors", [])]
        expected_user = getattr(ctx.slurm, "user", None) or ctx.scope.get("user")
        mismatch = bool(current.get("config")) and (current["config"].get("user") != expected_user or
                    current["config"].get("cluster", "") != (ctx.scope.get("cluster") or ""))
        if mismatch:
            warnings.append("This state directory belongs to another cluster or user. Its records are not from the current connection.")
            if action != "status":
                raise ValueError("The service directory belongs to another cluster or user; select the matching connection")
        if current.get("stale"):
            warnings.append("The saved observations are stale; the monitor is stopped.")
        plan = None
        if action != "status":
            from .slurm import FakeBackend
            backend = getattr(ctx.slurm, "b", None)
            while hasattr(backend, "inner"):
                backend = backend.inner
            if isinstance(backend, FakeBackend):
                raise ValueError("Simulated connections cannot control a live monitoring service")
            if action == "start" and current.get("running"):
                raise ValueError("Monitor already running; use Status to reconnect")
            if action == "stop" and not current.get("running"):
                return report(feature, "The monitor is already stopped.", rows=rows, data=current, warnings=warnings)
            config = supervisor.configuration(user=expected_user,
                                              cluster=ctx.scope.get("cluster") or "",
                                              interval=params.get("interval", "5"), max_records=params.get("max_records", "1000"))
            payload = {"action": action, "directory": directory, "config": config,
                       "owner_token": current.get("owner", {}).get("token")}
            plan = prepare_plan(ctx, feature, params, payload)
            rows += [f"Review action: {action}", f"User: {config['user']} | Cluster: {config['cluster'] or 'local default'}",
                     f"Read interval: {config['interval']:g} s; maximum records: {config['max_records']}"]
        return report(feature, "Persistent monitor inspection.", rows=rows, data=current, warnings=warnings, plan=plan)
    if feature == "staging":
        local_only(ctx)
        source = str(Path(params.get("manifest", "")).expanduser().absolute())
        raw = read_json(ctx, source)
        prepared = services_staging.inspect(raw, params.get("direction", "all"))
        if not ctx.state_dir:
            raise ValueError("Transfer receipts need a configured Tower state directory")
        payload = {"source": source, "source_digest": digest(raw), "transfer": prepared,
                   "receipts": str(Path(ctx.state_dir) / "staging")}
        plan = prepare_plan(ctx, feature, params, payload)
        rows = [f"Manifest: {prepared['manifest']['id']} | Revision: {prepared['revision'][:16]}",
                f"Entries: {len(prepared['entries'])} | Bytes: {prepared['bytes']}",
                "Source files remain in place. Existing destinations are verified or refused."]
        rows += [f"{entry['id']} [{entry['direction']}] {entry['source']} → {entry['destination']} ({entry['size']} bytes)"
                 for entry in prepared["entries"]]
        return report(feature, "Review the declared transfers before Apply.", rows=rows, plan=plan,
                      warnings=["Local and mounted filesystem paths only. Remote SSH profiles cannot transfer local substitutes."],
                      data={"revision": prepared["revision"], "bytes": prepared["bytes"], "entries": len(prepared["entries"])})
    if feature == "workflow-engine":
        engine = params.get("engine", "nextflow")
        if engine not in ("nextflow", "snakemake"):
            raise ValueError("Supported workflow engines: nextflow, snakemake")
        source = params.get("source", "")
        if not source or any(ch in source for ch in ("\x00", "\r", "\n")):
            raise ValueError("Choose an explicit workflow evidence file")
        value = getattr(services_workflows, engine)(ctx, source, params.get("workflow_id", ""))
        rows = [f"Engine: {engine} | Workflow: {value['workflow_id']}",
                f"Task attempts: {len(value['nodes'])} | Execution owner: {engine}"]
        rows += [f"{item['id']}  {item['name']}  {item['state']}  native={item['native_id'] or 'unknown'}"
                 f"  attempt={item['attempt'] or 'unknown'}  deps={','.join(item['dependencies']) or 'none reported'}"
                 for item in value["nodes"]]
        warnings = ["Tower observes engine evidence. Use the native engine for retry, resume, and cancellation."]
        if value.get("truncated"):
            warnings.append("Only the bounded tail of the trace is shown; earlier task attempts are omitted.")
        if value.get("incomplete_tail"):
            warnings.append("An unfinished final trace line was omitted; Refresh reads it after completion.")
        if not value.get("dag_available"):
            warnings.append("Nextflow trace does not report dependency edges; no DAG is inferred from task order.")
        if engine == "snakemake" and not value.get("runtime_available"):
            warnings.append("Native --d3dag output has no scheduler ids or runtime state. These fields remain unknown.")
        return report(feature, "Native workflow evidence loaded.", rows=rows, data=value, warnings=warnings)
    raise ValueError("Unknown services operation")


def apply(feature, plan, ctx):
    payload = validate_plan(plan, ctx, feature)
    local_only(ctx)
    if feature == "supervisor":
        if payload["action"] == "start":
            value = supervisor.start(payload["directory"], payload["config"])
        elif payload["action"] == "stop":
            value = supervisor.stop(payload["directory"], expected_token=payload["owner_token"])
        else:
            raise ValueError("Supervisor plan contains an unsupported action")
        return report(feature, f"Monitor {value['state']}.", rows=_supervisor_rows(value), data=value)
    if feature == "staging":
        if digest(read_json(ctx, payload["source"])) != payload["source_digest"]:
            raise ValueError("The transfer manifest changed after review; inspect it again")
        results = services_staging.execute(payload["transfer"], payload["receipts"], cancel=ctx.cancel)
        failed = [item for item in results if item["status"] in ("failed", "cancelled", "not-attempted")]
        return report(feature, f"Transfers: {len(results) - len(failed)} verified; {len(failed)} incomplete.",
                      status="partial" if failed else "ok", data={"entries": results, "receipts": payload["receipts"]},
                      rows=[f"{item['id']}: {item['status']} {item.get('reason', '')}" for item in results])
    raise ValueError("This workflow connector is read-only; the native engine retains execution ownership")


# Managed Raven entry points also protect direct adapter callers.
from .operations import managed_run as _managed_run, managed_apply as _managed_apply
run = _managed_run(run)
apply = _managed_apply(apply)
