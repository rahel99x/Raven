"""Project engineering workflows using the shared accessible review workbench."""

from __future__ import annotations

import copy
import inspect

from .. import operation_support as O, science_files as SF, scheduler_cli
from ..engineering import service
from ..engineering.presentation import rows as _rows


def _field(key, label, **kwargs):
    return dict(key=key, label=label, **kwargs)


LABELS = {
    "experiments": (
        "Experiment lineage",
        "Link verified attempts and report total resource use, including failed attempts.",
    ),
    "qualification": (
        "Qualify a compute site",
        "Review a tiny native compute test and verify its attempt-bound results.",
    ),
    "capsules": (
        "Reproducible execution package",
        "Capture a bounded script, code provenance and explicit input references.",
    ),
    "labs": (
        "Sites and lab workspace",
        "Keep login, workload billing and Heavy resources distinct across sites.",
    ),
    "provenance": (
        "Research output provenance",
        "Retain source data, units, calculations, filters and coverage with each result.",
    ),
    "performance": (
        "Distributed performance evidence",
        "Explain retained elapsed spans, measured waits, gaps and clock uncertainty.",
    ),
    "incidents": (
        "Incident evidence recorder",
        "Retain bounded scheduler, log, sample and trace evidence around a declared trigger.",
    ),
    "optimization": (
        "Controlled resource trials",
        "Review finite repeated trials; compare measured runtime, queue time and resource use.",
    ),
    "recovery": (
        "Reviewed workflow recovery",
        "Verify failure evidence, checkpoint lineage and outputs before a reviewed retry.",
    ),
    "diagnosis": (
        "Next diagnostic measurement",
        "Separate observed facts from hypotheses and propose a bounded next test.",
    ),
    "reliability": (
        "Reliability release budgets",
        "Compare observed latency, freshness, scheduler requests and memory with explicit budgets.",
    ),
}


SPECIFICATIONS = [
    dict(
        key="native-scheduler",
        title="Native scheduler connection",
        group="Engineering",
        proposal="E0",
        summary="Inspect native jobs and capabilities or review a submission/action on a supported scheduler.",
        fields=[
            _field(
                "scheduler",
                "Scheduler",
                default="slurm",
                choices=[
                    "slurm",
                    "kubernetes",
                    "lsf",
                    "openpbs",
                    "pbspro",
                    "htcondor",
                    "flux",
                ],
            ),
            _field(
                "action",
                "Task",
                default="capabilities",
                choices=[
                    "capabilities",
                    "queue",
                    "history",
                    "detail",
                    "submit",
                    "cancel",
                    "hold",
                    "release",
                ],
            ),
            _field("scope", "Connection scope JSON (optional)"),
            _field("host", "SSH host alias (optional; otherwise local)"),
            _field("ssh_user", "SSH login (optional)"),
            _field("identity", "Exact observed job identity JSON (for a job action)"),
            _field("submission", "Native submission JSON (for submit)"),
        ],
    )
]
for _domain, (_title, _summary) in LABELS.items():
    _actions = list(service.SERVICES[_domain])
    _fields = [
        _field("action", "Task", default=_actions[0], choices=_actions),
        _field("request", "Arguments JSON (advanced; see engineering guide)"),
    ]
    if _domain in {
        "experiments",
        "capsules",
        "labs",
        "provenance",
        "optimization",
        "recovery",
    }:
        _fields += [_field("root", "Project root (current project by default)")]
    if _domain in {"experiments", "optimization"}:
        _fields += [_field("experiment_id", "Experiment identity")]
    if _domain == "experiments":
        _fields += [_field("run_id", "Existing Raven run (for link)")]
    if _domain == "capsules":
        _fields += [
            _field("run_id", "Existing Raven run identity"),
            _field("destination", "New package directory"),
        ]
    if _domain == "experiments":
        _fields += [_field("title", "Experiment title (for create)")]
    SPECIFICATIONS.append(
        dict(
            key="engineering-" + _domain,
            title=_title,
            group="Engineering",
            proposal="E" + str(len(SPECIFICATIONS)),
            summary=_summary,
            fields=_fields,
        )
    )


def _read(ctx, path, sources):
    record, raw = SF.snapshot(
        path, limit=service.MAX_REQUEST, keep=True, cancel=ctx.cancel
    )
    from ..evidence import loads

    result = loads(raw)
    if not isinstance(result, dict):
        raise ValueError("Engineering input must be a JSON object")
    sources.append(record)
    return result


def _request(ctx, params, sources, domain):
    document = _read(ctx, params["request"], sources) if params.get("request") else {}
    arguments = document.get("arguments", document)
    if not isinstance(arguments, dict):
        raise ValueError("Engineering arguments must be an object")
    callback = service.callback(domain, params["action"])
    accepted = inspect.signature(callback).parameters
    for key in ("root", "experiment_id", "run_id", "title", "destination"):
        if params.get(key):
            name = "project" if key == "root" else key
            if name not in accepted:
                continue
            if name in arguments and arguments[name] != params[key]:
                raise ValueError("Form and request disagree on " + name)
            arguments[name] = params[key]
    return document


def _native(params, sources, ctx):
    descriptor = dict(
        name=params["scheduler"],
        host=params.get("host") or None,
        ssh_user=params.get("ssh_user") or None,
        scope=_read(ctx, params["scope"], sources) if params.get("scope") else {},
    )
    scheduler = scheduler_cli.client(
        descriptor, state_dir=scheduler_cli.state_directory(), cancel=ctx.cancel
    )
    try:
        action = params["action"]
        if action == "capabilities":
            value = scheduler.capabilities()
        elif action in {"queue", "history"}:
            value = dict(
                records=scheduler.jobs(history=action == "history"),
                capabilities=scheduler.capabilities(),
            )
        elif action == "detail":
            if not params.get("identity"):
                raise ValueError(
                    "Select an exact identity JSON from native queue/history"
                )
            value = scheduler.detail(_read(ctx, params["identity"], sources))
        else:
            value = scheduler.review(
                action,
                identity=_read(ctx, params["identity"], sources)
                if params.get("identity")
                else None,
                submission=_read(ctx, params["submission"], sources)
                if params.get("submission")
                else None,
            )
        return descriptor, value
    finally:
        scheduler_cli.close(scheduler)


def run(feature, params, ctx):
    O.local_only(ctx)
    sources = []
    if feature == "native-scheduler":
        descriptor, result = _native(params, sources, ctx)
        plan = None
        if params["action"] in {"submit", "cancel", "hold", "release"}:
            plan = O.prepare_plan(
                ctx,
                feature,
                params,
                dict(descriptor=descriptor, native=result, sources=sources),
            )
        return O.report(
            feature,
            "Review native scheduler action"
            if plan
            else "Native scheduler observation",
            rows=_rows(result),
            data=result,
            plan=plan,
        )
    domain = feature.removeprefix("engineering-")
    action = params["action"]
    document = _request(ctx, params, sources, domain)
    if (domain, action) in service.WRITES:
        return O.report(
            feature,
            "Review project changes and resource costs before applying",
            rows=[
                "Task: " + domain + " / " + action,
                "Native actions require their own explicit review digest; uncertain actions are not replayed.",
            ],
            data=document,
            plan=O.prepare_plan(
                ctx,
                feature,
                params,
                dict(domain=domain, action=action, document=document, sources=sources),
            ),
        )
    from ..engineering.cli import execute

    result = execute(domain, action, document, cancel=ctx.cancel)
    return O.report(
        feature,
        "Engineering task deferred"
        if isinstance(result, dict) and result.get("status") == "deferred"
        else "Engineering task complete",
        data=result,
        rows=_rows(result),
        status=service.result_status(result),
    )


def apply(feature, plan, ctx):
    O.local_only(ctx)
    payload = O.validate_plan(plan, ctx, feature)
    for source in payload["sources"]:
        SF.verify(source, ctx.cancel)
    if feature == "native-scheduler":
        scheduler = scheduler_cli.client(
            payload["descriptor"],
            state_dir=scheduler_cli.state_directory(),
            cancel=ctx.cancel,
        )
        try:
            native = payload["native"]
            result = scheduler.apply(native, native["digest"])
        finally:
            scheduler_cli.close(scheduler)
    else:
        from ..engineering.cli import execute

        result = execute(
            payload["domain"],
            payload["action"],
            copy.deepcopy(payload["document"]),
            cancel=ctx.cancel,
        )
    return O.report(
        feature,
        "Engineering action complete; inspect receipt for qualification and unknown outcomes",
        status=service.result_status(result),
        data=result,
        rows=_rows(result),
    )
