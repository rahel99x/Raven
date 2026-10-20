"""Planning argument/result services independent of terminal command dispatch."""

from __future__ import annotations
from itertools import islice
from ..planning import analyze, record, select_job
from ..planning_io import load_json
from ..entry.command_options import parser, PLANNING_COMMANDS as COMMANDS


def options(cmd, args):
    if cmd not in COMMANDS:
        raise ValueError("unknown planning command")
    p = parser(cmd)
    if cmd == "checkpoint-advice":
        p.add_argument("file")
        return p.parse_args(args)
    if cmd in ("predict", "forecast", "blockers"):
        p.add_argument("job_id", nargs="?")
        p.add_argument("--file")
        p.add_argument("--coverage", type=float)
        if cmd == "predict":
            for name in ("name", "partition", "account", "qos"):
                p.add_argument("--" + name)
            for name in ("cpus", "nodes", "gpus"):
                p.add_argument("--" + name, type=int)
        return p.parse_args(args)
    if cmd == "tradeoffs":
        p.add_argument("file")
        p.add_argument("--choose", type=int)
        p.add_argument("--script")
        p.add_argument("--workdir")
        return p.parse_args(args)
    if cmd == "choose":
        p.add_argument("index", type=int)
        p.add_argument("script")
        p.add_argument("--file")
        p.add_argument("--workdir")
        return p.parse_args(args)
    p.add_argument("action", choices=("analyze", "plan"))
    p.add_argument("file")
    p.add_argument("--workdir")
    if cmd == "scaling":
        p.add_argument("--mode", choices=("strong", "weak"))
        p.add_argument("--baseline", type=int)
    return p.parse_args(args)


def is_offline(cmd, args):
    return (
        cmd in ("tradeoffs", "scaling", "workflow", "choose", "checkpoint-advice")
        or cmd in ("predict", "forecast", "blockers")
        and any(a == "--file" or a.startswith("--file=") for a in args)
    )


def _finished(output, opts, source):
    path = getattr(opts, "file", None)
    simulated = isinstance(source, dict) and source.get("simulated") is True
    opts.source_label = (
        (("SIMULATED frozen observations: " if simulated else "") + str(path))
        if path
        else "scheduler snapshot"
    )
    output["source"] = opts.source_label
    if isinstance(source, dict) and "jobs" in source:
        output["data_jobs"] = [record(job) for job in islice(source["jobs"], 256)]
    return output, opts


def result(
    cmd,
    args,
    *,
    snap=None,
    job=None,
    observations=(),
    choices=None,
    execution=None,
    loader=None,
):
    from .policy import router_for
    from ..planning_input import planning_inputs

    router = execution or router_for()
    if router.managed:
        opts = options(cmd, args)
        blocked = router.require(cmd, worker_operation="planning-command")
        if blocked is not None:
            return blocked, opts
        captured = planning_inputs(
            cmd, None, snap=snap, job=job, observations=observations
        )
        receipt = router.dispatch(
            "planning-command",
            dict(
                command=cmd,
                args=list(args),
                snap=captured["snap"],
                job=captured["job"],
                observations=captured["observations"],
                choices=choices,
            ),
        )
        if receipt.get("status") == "deferred":
            return receipt, opts
        from argparse import Namespace

        return receipt["result"]["output"], Namespace(**receipt["result"]["options"])
    opts = options(cmd, args)
    read_source = load_json if loader is None else loader
    if cmd == "scaling":
        from ..scaling import MAX_BYTES

        source = read_source(opts.file, max_bytes=MAX_BYTES)
    else:
        source = read_source(opts.file) if getattr(opts, "file", None) else None
    snap = snap or {}
    overrides = {}
    opts.analysis_overrides = overrides
    opts.selected_job_id = None
    if cmd == "checkpoint-advice":
        return _finished(analyze(cmd, source), opts, source)
    if cmd in ("predict", "forecast", "blockers"):
        if isinstance(source, list) and cmd in ("forecast", "blockers"):
            source = {"jobs": source}
        mapping = source if isinstance(source, dict) else {}
        jobs = mapping.get("jobs", snap.get("jobs", []) + snap.get("finished", []))
        if opts.job_id:
            job = select_job(jobs, opts.job_id)
            overrides["job_id"] = opts.job_id
        elif (
            "jobs" in mapping
            or mapping.get("job_id") is not None
            or job is None
            or cmd in ("forecast", "blockers")
            and record(job).get("state") not in ("PENDING", "PD")
        ):
            job = select_job(
                jobs, mapping.get("job_id"), pending=cmd in ("forecast", "blockers")
            )
        selected = record(job).get("id") or record(job).get("job_id")
        opts.selected_job_id = str(selected) if selected is not None else None
        if cmd == "predict":
            query = {
                name: getattr(opts, name)
                for name in (
                    "name",
                    "partition",
                    "account",
                    "qos",
                    "cpus",
                    "nodes",
                    "gpus",
                )
                if getattr(opts, name) is not None
            }
            if query:
                overrides["query"] = query
        if opts.coverage is not None:
            overrides["coverage"] = opts.coverage
        output = analyze(
            cmd,
            source,
            snap=snap,
            job=job,
            observations=observations,
            overrides=overrides,
        )
        return _finished(output, opts, source)
    if cmd in ("tradeoffs", "choose"):
        from ..tradeoffs import prepare_choice

        comparison = analyze("tradeoffs", source) if source is not None else choices
        if comparison is None:
            raise ValueError(
                "attach tradeoffs first, or use choose INDEX SCRIPT --file FILE"
            )
        selected = opts.choose if cmd == "tradeoffs" else opts.index
        if selected is not None:
            if not opts.script:
                raise ValueError(
                    "--script is required when preparing a tradeoff choice"
                )
            plan = prepare_choice(
                comparison, selected, opts.script, workdir=opts.workdir
            )
            comparison = dict(comparison, prepared_plan=plan)
        elif opts.script or opts.workdir:
            raise ValueError("--script and --workdir require --choose INDEX")
        return _finished(comparison, opts, source)
    if cmd == "scaling":
        if opts.action == "analyze" and opts.workdir:
            raise ValueError("--workdir applies to plan, not analysis")
        if opts.action == "plan" and (
            opts.mode is not None or opts.baseline is not None
        ):
            raise ValueError(
                "--mode and --baseline apply to analysis; edit the scaling recipe for planning"
            )
        overrides["action"] = opts.action
        if opts.workdir is not None:
            overrides["workdir"] = opts.workdir
        if opts.mode is not None:
            overrides["mode"] = opts.mode
        if opts.baseline is not None:
            overrides["baseline"] = opts.baseline
        return _finished(analyze("scaling", source, overrides=overrides), opts, source)
    if cmd == "workflow":
        if opts.action == "analyze" and opts.workdir:
            raise ValueError("--workdir applies to plan, not analysis")
        overrides["action"] = opts.action
        if opts.workdir is not None:
            overrides["workdir"] = opts.workdir
        return _finished(analyze("workflow", source, overrides=overrides), opts, source)
    raise ValueError("unknown planning command")


def success(output):
    if output.get("prepared_plan"):
        return bool(output["prepared_plan"].get("valid"))
    return (
        output.get("status") not in ("error", "invalid", "blocked", "deferred")
        and output.get("valid") is not False
    )
