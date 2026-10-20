"""Existing Operations workbench integration for reviewed indexes and SQL reads."""

import json
from .. import operation_support as O, evidence_index as I, science_files as SF

SPECIFICATIONS = [
    {
        "key": "evidence-query",
        "title": "Query retained measurements",
        "group": "Research",
        "proposal": "W23",
        "summary": "Index an immutable export, query exact attempts, or export a consistent SQLite snapshot.",
        "fields": [
            {
                "key": "action",
                "label": "Action",
                "default": "query",
                "choices": ["query", "index", "export", "rate", "delta", "window"],
            },
            {"key": "index", "label": "Local index directory", "required": True},
            {"key": "bundle", "label": "Completed export directory (index only)"},
            {"key": "project", "label": "Exact project path from export (index only)"},
            {
                "key": "view",
                "label": "Measurement view",
                "default": "sources",
                "choices": list(I.VIEWS),
            },
            {"key": "sql", "label": "Advanced read-only SELECT (optional)"},
            {"key": "source_id", "label": "Exact source ID (series transform only)"},
            {"key": "attempt", "label": "Exact attempt key (series transform only)"},
            {"key": "metric", "label": "Metric/counter name (series transform only)"},
            {
                "key": "selector",
                "label": 'Stream dimensions as JSON, e.g. {"rank":"0"}',
                "default": "{}",
            },
            {
                "key": "max_gap",
                "label": "Largest valid rate interval in seconds",
                "default": "30",
            },
            {
                "key": "window_seconds",
                "label": "Sample-average window seconds (window only)",
                "default": "60",
            },
            {
                "key": "destination",
                "label": "New SQLite export directory (export only)",
            },
        ],
    }
]


def run(feature, params, ctx):
    O.local_only(ctx)
    action = params.get("action", "query")
    if action == "index":
        capture = I.inspect_bundle(params["bundle"], params["project"])
        payload = {
            "capture": capture["digest"],
            "index": params["index"],
            "bundle": capture["bundle"],
            "project": capture["project"],
        }
        plan = O.prepare_plan(ctx, feature, params, payload)
        return O.report(
            feature,
            "Review the project and immutable export before updating its rebuildable index.",
            rows=[
                "Project: " + capture["project"],
                "Capture: " + capture["digest"],
                f"Sources: {len(capture['files'])}; bytes: {capture['source_bytes']}",
                "Original exports are retained. Duplicate capture ingestion is a no-op.",
            ],
            plan=plan,
        )
    if action == "export":
        destination = SF.destination(params["destination"])
        result = I.query(
            params["index"],
            view="sources",
            max_rows=256,
            max_bytes=128 << 10,
            cancel=ctx.cancel,
        )
        plan = O.prepare_plan(
            ctx, feature, params, {"index": params["index"], "destination": destination}
        )
        return O.report(
            feature,
            "Review a consistent SQLite backup to a new directory.",
            data=result,
            plan=plan,
            warnings=[
                "The backup captures committed evidence at Apply time; new captures may have arrived since this preview."
            ],
        )
    if action in {"rate", "delta", "window"}:
        selected = I.series(
            params["index"],
            source_id=int(params.get("source_id") or "0"),
            attempt=params.get("attempt"),
            metric=params.get("metric"),
            selector=json.loads(params.get("selector") or "{}"),
            cancel=ctx.cancel,
        )
        result = I.transform(
            selected,
            operation=action,
            max_gap=float(params.get("max_gap", "30")),
            window_seconds=float(params.get("window_seconds", "60")),
        )
        rows = [
            f"Source {result['source_id']}; unit: {O.clean(result['unit'])}; operation: {action}"
        ]
        rows += [
            " | ".join(O.clean(row[key]) for key in ("timestamp", "value", "reason"))
            for row in result["samples"][:50]
        ]
        return O.report(feature, "Retained series transform", data=result, rows=rows)
    else:
        result = I.query(
            params["index"],
            params.get("sql") or None,
            view=params.get("view", "sources"),
            max_bytes=128 << 10,
            cancel=ctx.cancel,
        )
    rows = [" | ".join(O.clean(value, 256) for value in result["columns"])] + [
        " | ".join(
            "unavailable" if value is None else O.clean(value, 512) for value in row
        )
        for row in result["rows"][:50]
    ]
    return O.report(
        feature,
        "Retained evidence query: " + result["status"],
        data=result,
        rows=rows,
        warnings=[result["reason"]] if result["reason"] else [],
    )


def apply(feature, plan, ctx):
    O.local_only(ctx)
    payload = O.validate_plan(plan, ctx, feature)
    if plan["params"]["action"] == "index":
        result = I.ingest(
            payload["index"],
            payload["bundle"],
            payload["project"],
            cancel=ctx.cancel,
            expected_digest=payload["capture"],
        )
        return O.report(feature, "Evidence capture " + result["status"], data=result)
    if plan["params"]["action"] == "export":
        destination = I.export_snapshot(
            payload["index"], payload["destination"], cancel=ctx.cancel
        )
        return O.report(
            feature,
            "SQLite evidence snapshot saved.",
            rows=[destination],
            data={"destination": destination},
        )
    raise ValueError("Read-only queries have no Apply action")


run = O.managed_run(run)
apply = O.managed_apply(apply)
