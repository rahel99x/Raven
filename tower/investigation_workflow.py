"""Connect evidence, existing analyses, planning and reviewed diagnostic trials.

The manifest binds an explicit attempt; it is not an automatic assertion that a
producer's measurements describe the selected scheduler attempt. Selected jobs
also require the scheduler anchor. No manifest command runs during inspection.
"""
from __future__ import annotations

import os
from pathlib import Path

from . import evidence, investigation_planner, operations as O, science_contracts as C
from .derived_results import RESULTS, ResultKey

SCHEMA = "tower.investigation-workflow/v1"
VERSION = "1"
READERS = {"recommendation", "scientific-impact", "numerical-stability", "regression-investigation"}
TRACE_READERS = {"causal-delay", "collective-audit"}
DIAGNOSTICS = {"diagnostic-campaign", "minimize-reproducer", "regression-sweep"}
UNKNOWN = {"unknown", "inconclusive", "expired", "stale", "inapplicable", "scientifically-incomparable"}
_ABSENT = object()


def analyze(operation, document, ctx, *, identity=None, settings=None):
    """The standalone form and staged workflow use the identical pure analysis."""
    from .ops_investigation import _READERS
    function = _READERS[operation][2]
    O.checkpoint(ctx)
    # Recommendation expiry is a wall-clock predicate. Never retain it under a
    # source-only revision, even when its underlying evidence has not changed.
    if operation == "recommendation":
        return function(document)
    revision = evidence.digest(document)
    selection = [O._identity(job) for job in ctx.jobs + ctx.finished if ctx.selected and str(job.get("id")) == ctx.selected]
    source_identity = {"source": identity or {"document_sha256": revision}, "selected_scheduler_attempts": selection}
    key = ResultKey.create(scope=ctx.scope, identity=source_identity,
                           revision=revision, algorithm=operation, version=VERSION, settings=settings)
    return RESULTS.get_or_compute(key, lambda: function(document), cancel=ctx.cancel)


def validate(document, ctx):
    evidence.canonical(document)
    C.obj(document, {"schema", "identity", "sources", "planner", "diagnostics"},
          {"scheduler", "intent"}, "investigation workflow")
    if document["schema"] != SCHEMA:
        raise ValueError("Expected " + SCHEMA)
    identity = evidence.identity(document["identity"])
    if set(identity) != {"cluster", "job_id", "attempt"}:
        raise ValueError("Workflow identity must identify one job attempt, not an individual thread")
    if ctx.scope.get("cluster") and ctx.scope["cluster"] != identity["cluster"]:
        raise ValueError("Investigation cluster differs from the current connection")
    anchor = document.get("scheduler")
    if anchor is not None:
        C.obj(anchor, {"submit", "start", "user"}, label="scheduler anchor")
        for name, value in anchor.items():
            C.text(value, "scheduler " + name, 512)
        if any(anchor[name].lower() in {"unknown", "none", "n/a", "(null)"} for name in ("submit", "user")):
            raise ValueError("Scheduler anchor requires a known submit time and user")
    if ctx.selected:
        candidates = [job for job in ctx.jobs + ctx.finished if str(job.get("id")) == ctx.selected]
        if ctx.selected != identity["job_id"] or len(candidates) != 1 or anchor is None:
            raise ValueError("Selected investigations require one matching job and an exact scheduler anchor")
        if any(str(candidates[0].get(name) or "") != value for name, value in anchor.items()):
            raise ValueError("The selected job attempt changed; refresh the investigation anchor")
        if candidates[0].get("cluster") and candidates[0]["cluster"] != identity["cluster"]:
            raise ValueError("Selected job cluster differs from the investigation")
    ids = set()
    for source in C.rows(document["sources"], "sources", 16):
        C.obj(source, {"id", "operation", "path"}, label="analysis source")
        if C.text(source["id"]) in ids or not isinstance(source["operation"], str) or source["operation"] not in READERS | TRACE_READERS:
            raise ValueError("Sources require unique IDs and a supported scientific analysis")
        ids.add(source["id"])
        C.text(source["path"], "source path", 4096)
    C.text(document["planner"], "planner path", 4096)
    ids = set()
    for diagnostic in C.rows(document["diagnostics"], "diagnostics", 16):
        C.obj(diagnostic, {"id", "operation", "manifest"}, {"metrics"}, "diagnostic link")
        if C.text(diagnostic["id"]) in ids or not isinstance(diagnostic["operation"], str) or diagnostic["operation"] not in DIAGNOSTICS:
            raise ValueError("Diagnostics require unique IDs and a supported reviewed operation")
        ids.add(diagnostic["id"])
        C.text(diagnostic["manifest"], "recipe path", 4096)
        if "metrics" in diagnostic:
            if diagnostic["operation"] != "diagnostic-campaign":
                raise ValueError("Numerical trial measurements require a configuration campaign")
            # Reuse the actual analyzer's policy validation without inventing a
            # second numerical policy parser.
            from .science_diagnostics import numerical
            numerical({"schema": "tower.numerical-stability/v1", "metrics": diagnostic["metrics"],
                       "runs": [{"id": "validation", "configuration": "validation", "metrics": {}}]})
    if "intent" in document:
        C.obj(document["intent"], {"manifest", "capabilities"}, label="diagnostic intent")
        for value in document["intent"].values():
            C.text(value, "intent path", 4096)
    return document


def _path(root, value):
    # Remote paths remain remote; no local resolution or environment expansion
    # is used to substitute a different host's files.
    return os.path.normpath(value if os.path.isabs(value) else os.path.join(root, value))


def _state(operation, result):
    if "state" in result:
        return result["state"]
    if operation == "scientific-impact":
        return "unknown" if result["unresolved_claims"] or result["uncertain"] else "proposed-checks"
    states = {item["state"] for item in result.get("comparisons", [])}
    return "unknown" if not states or states & UNKNOWN else "regression" if "regression" in states else "observed-comparison"


def inspect(params, ctx):
    path = C.text(params.get("manifest"), "workflow manifest", 4096)
    root = os.path.dirname(path)
    loaded, captures, records, total = {}, {}, [], 0

    def read(source, optional=False):
        nonlocal total
        source = _path(root, source)
        if source in loaded:
            return loaded[source]
        O.checkpoint(ctx)
        try:
            value = O.read_json(ctx, source)
        except FileNotFoundError:
            if not optional:
                raise
            loaded[source] = _ABSENT
            records.append({"path": source, "sha256": None})
            return _ABSENT
        raw = evidence.canonical(value)
        total += len(raw)
        if total > 8 << 20:
            raise ValueError("Combined workflow evidence exceeds 8 MiB")
        loaded[source] = value
        records.append({"path": source, "sha256": evidence.digest(value)})
        return value

    # Read the manifest at its supplied path, not relative to itself.
    document = validate(O.read_json(ctx, path), ctx)
    records.append({"path": path, "sha256": evidence.digest(document)})
    total += len(evidence.canonical(document))
    identity = document["identity"]
    analysis_identity = {"declared": identity, "scheduler": document.get("scheduler")}
    findings, missing, known = [], [], []
    for source in document["sources"]:
        if source["operation"] in TRACE_READERS:
            from . import ops_trace, trace_protocol
            trace_path = _path(root, source["path"])
            if trace_path not in captures:
                try:
                    capture = ops_trace.collect_trace({"path": trace_path, "job_id": identity["job_id"], "attempt": identity["attempt"]}, ctx)
                except ops_trace.MissingTraceAttempt:
                    captures[trace_path] = _ABSENT
                    records.append({"format": "trace-index", "path": str(Path(trace_path).absolute()), "paths": [],
                                    "job_id": identity["job_id"], "attempt": identity["attempt"]})
                except FileNotFoundError as exc:
                    captures[trace_path] = _ABSENT
                    records.append({"format": "trace-missing", "path": str(exc.filename or trace_path)})
                else:
                    if any(snapshot["identity"]["cluster"] != identity["cluster"] for snapshot in capture["snapshots"]):
                        raise ValueError("Trace cluster differs from the workflow identity")
                    total += sum(len(trace_protocol.encoded(event))+1 for event in capture["events"])
                    if total > 8 << 20:
                        raise ValueError("Combined workflow evidence exceeds 8 MiB")
                    captures[trace_path] = capture
                    records.extend(capture["guards"])
                    if capture["project"]:
                        records.append({"format": "trace-index", "path": capture["project"], "paths": capture["paths"],
                                        "job_id": identity["job_id"], "attempt": identity["attempt"]})
            capture = captures[trace_path]
            if capture is _ABSENT:
                missing.append({"id": source["id"], "reason": "Trace evidence is absent"})
                continue
            report = ops_trace.analysis_report(source["operation"], capture, ctx)
            observed = report["data"]["analysis_status"]
            state = ("observed-match" if observed == "MATCH" else "observed-causal-path" if observed == "COMPLETE"
                     else "failed" if observed == "MISMATCH" else "unknown") if report["data"]["source_complete"] else "unknown"
            findings.append({"id": source["id"], "operation": source["operation"], "state": state,
                             "revision": evidence.digest(capture["guards"]), "result": report["data"],
                             "rows": report["rows"], "warnings": report["warnings"]})
            if state == "unknown":
                missing.append({"id": source["id"], "reason": "Trace prefix, declared rank coverage or causal evidence is incomplete/unsupported"})
            else:
                known.append(source["id"])
            continue
        value = read(source["path"], optional=True)
        if value is _ABSENT:
            missing.append({"id": source["id"], "reason": "Evidence file is absent"})
            continue
        if source["operation"] == "recommendation":
            # A recommendation embeds its own exact attempt. Never endorse a
            # recommendation about another attempt under this workflow header.
            snapshot = value.get("snapshot") if isinstance(value, dict) else None
            embedded = snapshot.get("identity", {}) if isinstance(snapshot, dict) else {}
            if not isinstance(embedded, dict):
                raise ValueError("Recommendation snapshot identity must be an object")
            if any(embedded.get(key) != item for key, item in identity.items()):
                raise ValueError("Recommendation evidence belongs to another workflow attempt")
        result = analyze(source["operation"], value, ctx, identity=analysis_identity)
        state = _state(source["operation"], result)
        findings.append({"id": source["id"], "operation": source["operation"], "state": state,
                         "revision": evidence.digest(value), "result": result})
        if state in UNKNOWN:
            missing.append({"id": source["id"], "reason": "Analysis retains unknown, expired or incomplete evidence"})
        else:
            known.append(source["id"])
    planner = read(document["planner"])
    if not isinstance(planner, dict):
        raise ValueError("Planner input must be an object")
    # Only this inspection's present, non-unknown findings satisfy prerequisites.
    # A stale producer-supplied known_evidence list cannot suppress a missing file.
    planner = dict(planner, known_evidence=known)
    planning = analyze("investigation-plan", planner, ctx, identity=analysis_identity)
    action_ids = {item["id"] for item in planner.get("actions", [])}
    if any(item["id"] not in action_ids for item in document["diagnostics"]):
        raise ValueError("Every diagnostic link must identify a declared planner action")
    links = {item["id"]: item for item in document["diagnostics"]}
    suggested = [dict(item, diagnostic=links.get(item["id"])) for item in planning["selected"]]
    compiled = None
    if document.get("intent"):
        from .diagnostic_recipes import compile_recipe
        intent = read(document["intent"]["manifest"])
        site = read(document["intent"]["capabilities"])
        key = ResultKey.create(scope=ctx.scope, identity=analysis_identity, revision=evidence.digest([intent, site]),
                               algorithm="diagnostic-recipe", version=VERSION)
        compiled = RESULTS.get_or_compute(key, lambda: compile_recipe(intent, site, cancel=ctx.cancel), cancel=ctx.cancel)
    data = {"identity": identity, "findings": findings, "missing": missing, "planning": planning,
            "suggested": suggested, "compiled": compiled, "sources": records,
            "revision": evidence.digest(records), "execution": None, "updated_findings": [],
            "stages": ["evidence", "findings", "missing-information", "suggested-diagnostic", "reviewed-execution", "updated-findings"],
            "limitations": ["Checks describe supplied evidence; producer identity and scientific coverage remain declared.",
                            "Planner costs are predictions. The diagnostic review separately states enforced execution limits.",
                            "Inspection never executes a diagnostic. Refresh reads new source revisions and re-evaluates expiring recommendations."]}
    return document, data, root


def verify_sources(records, ctx):
    """Prevent publication or execution from a mixed/stale source generation."""
    prefixes = []
    for record in records:
        O.checkpoint(ctx)
        if record.get("format") == "trace-prefix":
            prefixes.append(record)
            continue
        if record.get("format") == "trace-index":
            from .trace_protocol import discover_trace
            if discover_trace(record["path"], record["job_id"], record["attempt"]) != record["paths"]:
                raise ValueError("Registered trace streams changed during investigation; inspect again")
            continue
        if record.get("format") == "trace-missing":
            if Path(record["path"]).exists():
                raise ValueError("Trace evidence appeared during investigation; inspect again")
            continue
        try:
            current = evidence.digest(O.read_json(ctx, record["path"]))
        except FileNotFoundError:
            current = None
        if current != record["sha256"]:
            raise ValueError("Investigation evidence changed during review; inspect again")
    if prefixes:
        from .ops_trace import verify_prefixes
        verify_prefixes(prefixes, ctx)


def _measurements(link, status, ctx, identity):
    """Consume actual, bounded campaign outputs; missing trials stay unknown."""
    if not link.get("metrics") or not status.get("result"):
        return []
    root = Path(status["workspace"])
    from . import science_files
    recipe, recipe_source = science_files.json_file(str(root/"recipe.json"), ctx.cancel)
    if O.digest(recipe) != status["recipe_sha256"] or recipe["kind"] != "campaign":
        raise ValueError("Diagnostic recipe changed while collecting measurements")
    outcome = status["result"]
    if outcome.get("recipe_sha256") != status["recipe_sha256"]:
        return []  # Submission/error receipt is not a measured campaign result.
    retained, outcome_source = science_files.json_file(str(root/"result.json"), ctx.cancel)
    if O.digest(retained) != O.digest(outcome):
        raise ValueError("Diagnostic outcome changed while collecting measurements")
    trials = outcome.get("trials", [])
    repeats = recipe["limits"]["repeats"]
    expected = len(recipe["cases"]) * repeats
    if expected > 2560 or not isinstance(trials, list) or len(trials) > min(expected, recipe["limits"]["max_trials"]):
        raise ValueError("Diagnostic trial count exceeds its declared campaign")
    seen, measured, measurement_sources = set(), {}, []
    for ordinal, trial in enumerate(trials):
        sequence = C.integer(trial.get("sequence"), "trial sequence", 0, expected-1)
        if sequence in seen:
            raise ValueError("Duplicate diagnostic trial sequence")
        if sequence != ordinal:
            raise ValueError("Diagnostic trials must retain their declared campaign order")
        seen.add(sequence)
        if trial.get("state") != "finished" or trial.get("oracle_matched") is not True:
            continue
        try:
            record, raw = science_files.snapshot(str(root/"trials"/f"{sequence:04d}"/"result.json"), cancel=ctx.cancel,
                                             limit=recipe["limits"]["output_bytes"], keep=True)
            value = evidence.loads(raw)
        except FileNotFoundError:
            continue
        if not isinstance(value, dict):
            raise ValueError("Trial scientific measurements must be an object")
        measurement_sources.append(record)
        measured[sequence] = {policy["name"]: value[policy["name"]] for policy in link["metrics"] if policy["name"] in value}
    document = {"schema": "tower.numerical-stability/v1", "metrics": link["metrics"],
                "runs": [{"id": f"trial-{i}", "configuration": recipe["cases"][i//repeats]["id"],
                          "metrics": measured.get(i, {})} for i in range(expected)]}
    result = analyze("numerical-stability", document, ctx, identity=identity,
                     settings={"recipe_sha256": status["recipe_sha256"], "workspace": str(root)})
    for record in [recipe_source, outcome_source, *measurement_sources]:
        science_files.verify(record, ctx.cancel)
    return [{"id": link["id"], "operation": "numerical-stability", "revision": evidence.digest(document),
             "state": result["state"], "result": result,
             "provenance": {"workspace": str(root), "recipe_sha256": status["recipe_sha256"],
                            "measurements": [{"path": record["path"], "sha256": record["sha256"]} for record in measurement_sources],
                            "source": "Declared numeric keys in per-trial result.json; missing/oracle-failed trials remain unknown"}}]


def run(params, ctx):
    from . import ops_investigation as adapter
    document, data, root = inspect(params, ctx)
    action = params.get("action", "inspect")
    if action not in {"inspect", "review-diagnostic", "status"}:
        raise ValueError("Choose inspect, review-diagnostic or status")
    plan, nested_rows, warnings = None, [], list(data["limitations"])
    warnings += [warning for finding in data["findings"] for warning in finding.get("warnings", [])]
    execution_warning = False
    if action != "inspect":
        link = next((item for item in document["diagnostics"] if item["id"] == params.get("diagnostic_id")), None)
        if link is None:
            raise ValueError("Choose a diagnostic ID declared in this workflow")
        if action == "review-diagnostic" and link["id"] not in {item["id"] for item in data["suggested"]}:
            raise ValueError("This diagnostic is not admitted by the current evidence, risk and planning budget")
        destination = C.text(params.get("destination"), "diagnostic workspace", 4096)
        nested_params = {"manifest": _path(root, link["manifest"]), "destination": destination,
                         "action": "launch" if action == "review-diagnostic" else "status"}
        nested = adapter.run(link["operation"], nested_params, ctx)
        nested_rows, warnings = nested["rows"], warnings + nested.get("warnings", [])
        execution_warning = nested["status"] != "ok"
        data["execution"] = {"operation": link["operation"], "diagnostic_id": link["id"],
                             "state": "review-required" if action == "review-diagnostic" else nested["data"]["state"],
                             "data": nested["data"]}
        if action == "review-diagnostic":
            nested["plan"]["workflow_binding"] = {"schema": "tower.investigation-binding/v1", "identity": document["identity"],
                                                    "diagnostic_id": link["id"], "workflow_revision": data["revision"]}
            nested["plan"]["digest"] = O.digest({key: value for key, value in nested["plan"].items() if key != "digest"})
            plan = O.prepare_plan(ctx, "investigation-workflow", params,
                                  {"sources": data["sources"], "nested_plan": nested["plan"],
                                   "identity": document["identity"]})
        else:
            binding = nested["data"].get("workflow_binding")
            if not isinstance(binding, dict) or binding.get("schema") != "tower.investigation-binding/v1" or binding.get("identity") != data["identity"] or binding.get("diagnostic_id") != link["id"]:
                raise ValueError("This diagnostic workspace is not bound to this investigation attempt and diagnostic ID")
            if O.digest(O.read_json(ctx, nested_params["manifest"])) != nested["data"]["recipe_sha256"]:
                raise ValueError("This diagnostic workspace belongs to a different recipe")
            data["updated_findings"] = _measurements(link, nested["data"], ctx, data["identity"])
            if not data["updated_findings"]:
                data["updated_findings"] = [{"id": link["id"], "operation": link["operation"],
                                              "state": nested["data"]["state"], "result": nested["data"].get("result"),
                                              "scope": "Diagnostic outcome; a completed process alone does not prove a scientific claim"}]
    verify_sources(data["sources"], ctx)
    rows = [f"Evidence: {data['identity']['cluster']} / {data['identity']['job_id']} / {data['identity']['attempt']}",
            f"Revision: {data['revision'][:16]}"]
    rows += [f"Finding {item['id']}: {item['state']} ({item['operation']})" for item in data["findings"]]
    rows += [f"Missing {item['id']}: {item['reason']}" for item in data["missing"]]
    rows += [f"Suggested {item['id']}: {'review available' if item['diagnostic'] else 'no executable recipe linked'}" for item in data["suggested"]]
    rows += [f"Excluded {item['id']}: {item['reason']}" for item in data["planning"]["excluded"]]
    if data["compiled"] is not None:
        rows += ["Collection proposal: " + data["compiled"]["question"]]
        rows += [f"Observation {name}: {item['observed']} ({'exact' if item['exact'] else 'declared approximation'})"
                 for name, item in data["compiled"]["coverage"].items()]
        rows += ["Proposed collection adapter: " + item["id"] + " (inspection only)" for item in data["compiled"]["adapters"]]
        warnings += data["compiled"]["limitations"]
    rows += nested_rows
    rows += [f"Updated {item['id']}: {item['state']}" for item in data["updated_findings"]]
    # Include the specialized analysis lines in the same scrollable document.
    for finding in data["findings"] + data["updated_findings"]:
        if finding["operation"] in TRACE_READERS:
            rows += finding["rows"]
        if finding["operation"] in READERS and finding.get("result"):
            rows += adapter._rows(finding["operation"], finding["result"])
    warning = execution_warning or bool(data["missing"]) or any(item["state"] in UNKNOWN | {"failed", "refuted", "regression"}
                                           for item in data["findings"] + data["updated_findings"])
    return O.report("investigation-workflow", "Staged job investigation", status="warning" if warning else "ok",
                    rows=rows, warnings=warnings, data=data, plan=plan)


def apply(plan, ctx):
    payload = O.validate_plan(plan, ctx, "investigation-workflow")
    verify_sources(payload["sources"], ctx)
    document = validate(O.read_json(ctx, plan["params"]["manifest"]), ctx)
    if document["identity"] != payload["identity"]:
        raise ValueError("Investigation attempt changed before execution")
    # Re-evaluate time-sensitive recommendations and admission immediately before
    # launching. The nested adapter independently revalidates its exact sources.
    _, current, _ = inspect(plan["params"], ctx)
    if plan["params"]["diagnostic_id"] not in {item["id"] for item in current["suggested"]}:
        raise ValueError("Diagnostic prerequisites expired or changed; inspect again")
    verify_sources(current["sources"], ctx)
    verify_sources(payload["sources"], ctx)
    nested = payload["nested_plan"]
    result = O.apply(nested["feature"], nested, ctx)
    return O.report("investigation-workflow", "Reviewed diagnostic started; inspect status for updated findings",
                    status=result["status"], rows=result["rows"], warnings=result["warnings"],
                    data={"identity": payload["identity"], "diagnostic": result,
                          "next": "Set action=status with this manifest, diagnostic ID and workspace"})
