"""Reviewable evidence interoperability, adapter, policy and qualification controls."""

from __future__ import annotations

import hashlib
import platform
import re

from .. import (
    operation_support as O,
    evidence as E,
    evidence_formats as F,
    site_policy as P,
)
from .. import adapter_protocol as A, science_files as SF


def _field(key, label, **kw):
    return {"key": key, "label": label, **kw}


SPECIFICATIONS = [
    {
        "key": "wasi-analysis",
        "title": "WASI analysis boundary",
        "group": "Platform",
        "proposal": "W40",
        "summary": "Review a pinned pure-analysis module with explicit memory, I/O, fuel and wall-time limits.",
        "fields": [
            _field("manifest", "WASI analysis manifest", required=True),
            _field("input", "Finite f64 series JSON", required=True),
            _field("destination", "New result directory (optional)"),
            _field("action", "Action", default="inspect", choices=["inspect", "run"]),
        ],
    },
    {
        "key": "site-telemetry",
        "title": "Site telemetry evidence",
        "group": "Platform",
        "proposal": "F19",
        "summary": "Inspect or explicitly query a bounded Prometheus source with exact job-attempt labels.",
        "fields": [
            _field("manifest", "Prometheus query manifest", required=True),
            _field("action", "Action", default="inspect", choices=["inspect", "query"]),
        ],
    },
    {
        "key": "evidence",
        "title": "Evidence receipts",
        "group": "Platform",
        "proposal": "F15",
        "summary": "Inspect exact execution identity, source coverage, units, clocks and integrity.",
        "fields": [_field("path", "Evidence JSON", required=True)],
    },
    {
        "key": "site-policy",
        "title": "Site collection admission",
        "group": "Platform",
        "proposal": "F29",
        "summary": "Check explicit site budgets against a source capability and collection request.",
        "fields": [
            _field("policy", "Site policy JSON", required=True),
            _field("capabilities", "Source capabilities JSON", required=True),
            _field("request", "Collection request JSON", required=True),
        ],
    },
    {
        "key": "adapter",
        "title": "External adapter workbench",
        "group": "Platform",
        "proposal": "F18",
        "summary": "Verify a pinned adapter manifest; review subprocess execution with hard output/time bounds.",
        "fields": [
            _field("manifest", "Adapter manifest", required=True),
            _field(
                "action",
                "Action",
                default="inspect",
                choices=["inspect", "describe", "validate", "run"],
            ),
            _field("request", "Request parameters JSON (optional)"),
        ],
    },
    {
        "key": "research-portable",
        "title": "Portable research evidence",
        "group": "Platform",
        "proposal": "F20",
        "summary": "Import actual Perfetto, Caliper and RO-Crate JSON; export a reviewed portable bundle.",
        "fields": [
            _field("path", "Source JSON", required=True),
            _field(
                "format",
                "Source format",
                default="raven-evidence",
                choices=list(F.FORMATS),
            ),
            _field("cluster", "Exact cluster", required=True),
            _field("job_id", "Exact job ID", required=True),
            _field("attempt", "Exact attempt", required=True),
            _field(
                "export_format",
                "Export format",
                default="workflow-run-ro-crate",
                choices=list(F.FORMATS),
            ),
            _field("destination", "New bundle directory (optional)"),
            _field(
                "signing_key",
                "Optional SSH signing key (private file or agent public key)",
            ),
            _field("signing_principal", "Signing principal (required when signing)"),
        ],
    },
    {
        "key": "qualification",
        "title": "Compatibility qualification",
        "group": "Platform",
        "proposal": "F34",
        "summary": "Run bounded, hashed format fixtures and report exact compatibility checks; no live cluster certification.",
        "fields": [_field("manifest", "Qualification manifest", required=True)],
    },
    {
        "key": "support-bundle",
        "title": "Reviewed support bundle",
        "group": "Platform",
        "proposal": "F32",
        "summary": "Collect explicitly listed reports with credential redaction and prepare a private support bundle.",
        "fields": [
            _field("manifest", "Report list manifest", required=True),
            _field("destination", "New bundle directory (optional)"),
        ],
    },
]


def _read(ctx, path, limit=E.MAX_BYTES):
    raw = O.read_bytes(ctx, path, limit)
    return E.loads(raw), {"path": str(path), "sha256": hashlib.sha256(raw).hexdigest()}


def _verify(ctx, sources):
    for source in sources:
        _, actual = _read(ctx, source["path"])
        if actual != source:
            raise ValueError("Reviewed source changed: " + source["path"])


def _rows(receipt):
    loss, coverage = receipt["loss"], receipt["coverage"]
    execution = receipt["identity"]
    return [
        f"Cluster {execution['cluster']} | Job {execution['job_id']} | Attempt {execution['attempt']}",
        f"Source: {receipt['source']['format']} | Records: {len(receipt['records'])}",
        f"Coverage: {coverage['mode']} | Complete: {coverage['complete']} | Omitted: {coverage['omitted']}",
        f"Lost records: {loss['records']} | {loss['reason']}",
        f"Clock: {receipt['clock']['domain']} ({receipt['clock']['unit']}); uncertainty: {receipt['clock']['uncertainty']}",
        "Integrity: digest detects changes; it does not attest that the source is truthful.",
    ]


def _binding(execution, ctx):
    if ctx.scope.get("cluster") and ctx.scope["cluster"] != execution["cluster"]:
        raise ValueError("Evidence cluster differs from the current connection")
    candidates = [
        job
        for job in ctx.jobs + ctx.finished
        if str(job.get("id")) == execution["job_id"]
    ]
    if candidates:
        attempts = set()
        for job in candidates:
            submit, start = str(job.get("submit") or ""), str(job.get("start") or "")
            if not submit:
                raise ValueError("Selected job has no verified submit identity")
            attempts.add(
                submit + "/" + start
                if start not in ("", "Unknown", "None", "N/A", "0")
                else submit
            )
        if len(attempts) != 1 or execution["attempt"] not in attempts:
            raise ValueError(
                "Evidence attempt differs from or is ambiguous in the current job snapshot"
            )
    return bool(candidates and ctx.scope.get("cluster"))


def _portable(params, ctx):
    source, fingerprint = _read(ctx, params.get("path", ""))
    execution = E.identity(
        {key: params.get(key, "") for key in ("cluster", "job_id", "attempt")}
    )
    verified = _binding(execution, ctx)
    receipt = F.import_document(
        source, params.get("format", "raven-evidence"), execution
    )
    exported = F.export_document(
        receipt, params.get("export_format", "workflow-run-ro-crate")
    )
    return receipt, exported, fingerprint, verified


def _qualification(params, ctx):
    manifest, fingerprint = _read(ctx, params.get("manifest", ""))
    E.keys(manifest, {"schema", "id", "checks"}, label="qualification manifest")
    if manifest["schema"] != "raven.qualification-manifest/v1":
        raise ValueError("Expected raven.qualification-manifest/v1")
    E.text(manifest["id"], "qualification id", 256)
    if (
        not isinstance(manifest["checks"], list)
        or not 1 <= len(manifest["checks"]) <= 64
    ):
        raise ValueError("Qualification requires 1..64 explicit checks")
    results, seen = [], set()
    total_bytes = 0
    for check in manifest["checks"]:
        O.checkpoint(ctx)
        E.keys(
            check,
            {"id", "path", "sha256", "format", "identity", "expect"},
            label="qualification check",
        )
        token = E.text(check["id"], "check id", 256)
        if token in seen:
            raise ValueError("Duplicate qualification check")
        seen.add(token)
        if not isinstance(check["sha256"], str) or not E.HASH.fullmatch(
            check["sha256"]
        ):
            raise ValueError("Qualification check requires pinned SHA256")
        expected = E.keys(
            check["expect"],
            {"min_records", "max_records"},
            {"mode"},
            "qualification expectation",
        )
        E.integer(expected["min_records"], "minimum records", E.MAX_RECORDS)
        E.integer(expected["max_records"], "maximum records", E.MAX_RECORDS)
        if expected["min_records"] > expected["max_records"]:
            raise ValueError("Qualification record range is reversed")
        try:
            remaining = (16 << 20) - total_bytes
            if remaining <= 0:
                raise ValueError("Qualification fixture total exceeds 16 MiB")
            raw = O.read_bytes(ctx, check["path"], min(4 << 20, remaining))
            total_bytes += len(raw)
            if hashlib.sha256(raw).hexdigest() != check["sha256"]:
                raise ValueError("Fixture hash differs from qualification manifest")
            receipt = F.import_document(
                E.loads(raw), check["format"], check["identity"]
            )
            count = len(receipt["records"])
            if not expected["min_records"] <= count <= expected["max_records"]:
                raise ValueError(
                    "Imported record count falls outside declared expectation"
                )
            if "mode" in expected and receipt["coverage"]["mode"] != expected["mode"]:
                raise ValueError("Coverage mode differs from declared expectation")
            results.append(
                {
                    "id": token,
                    "status": "pass",
                    "records": count,
                    "receipt_digest": receipt.get("digest"),
                }
            )
        except (ValueError, OSError) as exc:
            O.checkpoint(ctx)
            results.append({"id": token, "status": "fail", "reason": O.clean(exc)})
    data = {
        "schema": "raven.qualification-result/v1",
        "id": manifest["id"],
        "manifest_sha256": fingerprint["sha256"],
        "level": "offline-format-fixtures",
        "python": platform.python_version(),
        "platform": platform.system(),
        "passed": all(item["status"] == "pass" for item in results),
        "checks": results,
        "live_slurm_tested": False,
        "live_gpu_tested": False,
    }
    return O.report(
        "qualification",
        "Offline compatibility checks completed.",
        status="ok" if data["passed"] else "partial",
        rows=[
            f"{item['id']}: {item['status']}"
            + (" — " + item["reason"] if "reason" in item else "")
            for item in results
        ],
        data=data,
        warnings=[
            "This report certifies only the listed offline checks, not live Slurm, GPU, multi-node or site compatibility."
        ],
    )


_SECRET = re.compile(
    r"(?:token|password|secret|credential|authorization|cookie|private.?key)", re.I
)
_INLINE = re.compile(
    r"(?i)\b(token|password|secret|api[_-]?key|authorization)\s*[:=]\s*([^\s,;]+)"
)


def _redact(value):
    if isinstance(value, dict):
        return {
            key: "[REDACTED]"
            if _SECRET.search(key) or key.lower() in {"environment", "env"}
            else _redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, str):
        value = _INLINE.sub(lambda match: match[1] + "=[REDACTED]", value)
        return re.sub(r"(?i)(https?://)[^/@\s]+:[^/@\s]+@", r"\1[REDACTED]@", value)
    return value


def _support(params, ctx):
    manifest, source = _read(ctx, params.get("manifest", ""))
    E.keys(manifest, {"schema", "reports"}, label="support manifest")
    if (
        manifest["schema"] != "raven.support-bundle/v1"
        or not isinstance(manifest["reports"], list)
        or not 1 <= len(manifest["reports"]) <= 32
    ):
        raise ValueError("Support manifest requires 1..32 explicitly listed reports")
    reports, sources, seen, retained_bytes = [], [source], set(), 0
    for path in manifest["reports"]:
        E.text(path, "report path", 4096)
        if path in seen:
            raise ValueError("Duplicate support report path")
        seen.add(path)
        if retained_bytes >= E.MAX_BYTES:
            raise ValueError("Support report total exceeds the 8 MiB bundle bound")
        value, fingerprint = _read(ctx, path, E.MAX_BYTES - retained_bytes)
        retained_bytes += len(E.canonical(value))
        if not isinstance(value, dict) or value.get("schema") not in (
            O.REPORT_SCHEMA,
            E.SCHEMA,
            "raven.qualification-result/v1",
        ):
            raise ValueError(
                "Support bundles accept operation reports, evidence receipts and qualification results only"
            )
        sources.append(fingerprint)
        reports.append(_redact(value))
    bundle = {
        "schema": "raven.support-report/v1",
        "ui_state_status": _redact(getattr(ctx, "ui_state_status", {})),
        "python": platform.python_version(),
        "platform": platform.system(),
        "connection_digest": E.digest(ctx.scope),
        "reports": reports,
        "redaction": "Common credential keys and assignments removed; inspect before sharing. No environment, directories or logs were automatically collected.",
    }
    E.canonical(bundle)
    return bundle, sources


def run(feature, params, ctx):
    O.checkpoint(ctx)
    if feature == "wasi-analysis":
        O.local_only(ctx)
        from .. import wasi_analysis as W

        value, source = _read(ctx, params.get("manifest", ""), 65536)
        manifest = W.manifest(value)
        module = O.read_bytes(ctx, manifest["module"], W.MAX_MODULE)
        data, input_source = _read(ctx, params.get("input", ""), W.MAX_INPUT)
        preview = W.inspect(manifest, module, data)
        action = params.get("action", "inspect")
        if action not in {"inspect", "run"}:
            raise ValueError("WASI action must be inspect or run")
        destination = (
            SF.destination(params["destination"]) if params.get("destination") else ""
        )
        plan = (
            O.prepare_plan(
                ctx,
                feature,
                params,
                {
                    "sources": [source, input_source],
                    "reviewed": preview,
                    "destination": destination,
                },
            )
            if action == "run"
            else None
        )
        return O.report(
            feature,
            "WASI analysis reviewed; Apply executes the pinned module."
            if plan
            else "WASI analysis inspected without executing guest code.",
            rows=[
                f"Runtime: {preview['runtime']} {preview['version']}",
                "ABI: " + preview["abi"],
                f"Samples: {preview['samples']}; memory: {preview['limits']['memory_bytes']} bytes; fuel: {preview['limits']['fuel']}",
                f"Wall time: {preview['limits']['wall_seconds']} s; guest output: {preview['limits']['output_bytes']} bytes",
                "No preopened directories, environment, network, clocks or randomness; bounded in-memory stdin/stdout only.",
            ],
            data=preview,
            plan=plan,
            warnings=[
                "Experimental compatible pure-analysis boundary. Runtime vulnerabilities remain possible; results are adapter claims, not verified scientific truth."
            ],
        )
    if feature == "site-telemetry":
        from .. import prometheus_source as T

        value, _ = _read(ctx, params.get("manifest", ""))
        manifest = T.validate(value)
        verified = _binding(manifest["identity"], ctx)
        action = params.get("action", "inspect")
        if action == "inspect":
            return O.report(
                feature,
                "Telemetry manifest inspected; no HTTP request was made.",
                rows=[
                    "Endpoint: " + manifest["endpoint"],
                    "Exact labels: " + str(manifest["labels"]),
                    f"Range: {manifest['start']:g}..{manifest['end']:g} s; query step: {manifest['step']:g} s",
                    "Choose Query to fetch this explicit source.",
                ],
                data={
                    "manifest": _redact(manifest),
                    "selected_attempt_verified": verified,
                },
            )
        if action != "query":
            raise ValueError("Telemetry action must be inspect or query")
        O.local_only(ctx)
        receipt = T.collect(manifest, cancel=ctx.cancel)
        return O.report(
            feature,
            "Exact-attempt site telemetry collected.",
            rows=_rows(receipt),
            data=receipt,
            warnings=receipt.get("metadata", {}).get("warnings", []),
        )
    if feature == "evidence":
        value, _ = _read(ctx, params.get("path", ""))
        receipt = E.validate(value)
        verified = _binding(receipt["identity"], ctx)
        warnings = list(receipt.get("metadata", {}).get("warnings", []))
        if not verified:
            warnings.append(
                "Execution identity was supplied by the document; the current queue/history cannot independently verify it."
            )
        if "digest" not in receipt:
            warnings.append("This evidence document has no receipt digest.")
        return O.report(
            feature,
            "Evidence integrity and collection scope.",
            rows=_rows(receipt),
            data=receipt,
            warnings=warnings,
        )
    if feature == "site-policy":
        policy, _ = _read(ctx, params.get("policy", ""))
        observed, _ = _read(ctx, params.get("capabilities", ""))
        request, _ = _read(ctx, params.get("request", ""))
        decision = P.admit(policy, request, observed)
        return O.report(
            feature,
            "Collection admitted." if decision["allowed"] else "Collection refused.",
            status="ok" if decision["allowed"] else "blocked",
            rows=decision["reasons"]
            or [
                f"Minimum interval: {decision['effective_min_interval']:g} s",
                "Admission is bound to the exact policy, source capability and request digests.",
            ],
            data=decision,
            warnings=[
                "A policy document is configuration, not proof of administrator authorization. This inspection starts no collection."
            ],
        )
    if feature == "adapter":
        O.local_only(ctx)
        value, source = _read(ctx, params.get("manifest", ""))
        manifest = A.validate_manifest(value, verify_assets=True)
        action = params.get("action", "inspect")
        if action not in ("inspect", "describe", "validate", "run"):
            raise ValueError("Unknown adapter action")
        request, sources = {}, [source]
        if params.get("request"):
            request, fingerprint = _read(ctx, params["request"])
            if not isinstance(request, dict):
                raise ValueError("Adapter request JSON must be an object")
            sources.append(fingerprint)
        plan = None
        if action != "inspect":
            if action not in manifest["methods"]:
                raise ValueError("Adapter does not declare this method")
            plan = O.prepare_plan(
                ctx,
                feature,
                params,
                {
                    "sources": sources,
                    "manifest_digest": E.digest(manifest),
                    "request_digest": E.digest(request),
                    "method": action,
                },
            )
        return O.report(
            feature,
            "Adapter manifest verified; execution requires Apply."
            if plan
            else "Adapter inspected without execution.",
            rows=[
                f"{manifest['id']} {manifest['version']}",
                "Executable: " + manifest["argv"][0],
                f"Methods: {', '.join(manifest['methods'])}",
                f"Timeout: {manifest['limits']['timeout']:g} s; output budget: {manifest['limits']['total_bytes']} bytes",
                "Declared permissions: " + ", ".join(manifest.get("permissions", [])),
            ],
            data={
                "manifest": _redact(manifest),
                "asset_count": len(manifest["assets"]),
            },
            plan=plan,
            warnings=[
                "Installed adapters are executable code. Process/virtual-environment isolation is not a security sandbox; assets, dependencies and permissions require trust."
            ],
        )
    if feature == "research-portable":
        receipt, exported, source, verified = _portable(params, ctx)
        plan, signer = None, None
        if params.get("signing_key") or params.get("signing_principal"):
            O.local_only(ctx)
            if not params.get("signing_key") or not params.get("signing_principal"):
                raise ValueError(
                    "Signing requires both signing_key and signing_principal; omit both for an unsigned export."
                )
            from .. import evidence_signing as signing

            signer = signing.preview(params["signing_key"], params["signing_principal"])
        if params.get("destination"):
            O.local_only(ctx)
            destination = SF.destination(params["destination"])
            payload = {
                "sources": [source],
                "receipt_digest": E.digest(receipt),
                "export_digest": E.digest(exported),
                "destination": destination,
            }
            if signer:
                payload["signer"] = signer
            plan = O.prepare_plan(ctx, feature, params, payload)
        warnings = list(receipt.get("metadata", {}).get("warnings", []))
        if signer:
            warnings.append(
                "Signing on Apply: "
                + signer["principal"]
                + " | "
                + signer["fingerprint"]
                + "; namespace "
                + signer["namespace"]
                + ". Reviewer trust must come from an independent allowlist; authenticity does not establish scientific validity or trusted time."
            )
        if not verified:
            warnings.append(
                "Declared execution identity is not independently verified by this connection."
            )
        return O.report(
            feature,
            "Portable evidence prepared."
            if plan
            else "Portable evidence inspected; choose a new destination to export.",
            rows=_rows(receipt),
            data=receipt,
            warnings=warnings,
            plan=plan,
        )
    if feature == "qualification":
        return _qualification(params, ctx)
    if feature == "support-bundle":
        bundle, sources = _support(params, ctx)
        plan = None
        if params.get("destination"):
            O.local_only(ctx)
            plan = O.prepare_plan(
                ctx,
                feature,
                params,
                {
                    "sources": sources,
                    "bundle_digest": E.digest(bundle),
                    "destination": SF.destination(params["destination"]),
                },
            )
        return O.report(
            feature,
            "Review the support report before export or sharing.",
            rows=[f"Explicit reports: {len(bundle['reports'])}", bundle["redaction"]],
            data=bundle,
            plan=plan,
            warnings=[
                "Redaction cannot identify every secret embedded in arbitrary prose. Review the complete report; nothing is uploaded or sent automatically."
            ],
        )
    raise ValueError("Unknown platform operation")


def apply(feature, plan, ctx):
    payload = O.validate_plan(plan, ctx, feature)
    if feature not in {
        "adapter",
        "research-portable",
        "support-bundle",
        "wasi-analysis",
    }:
        raise ValueError("This platform inspection has no write action")
    O.local_only(ctx)
    _verify(ctx, payload["sources"])
    params = plan["params"]
    if feature == "wasi-analysis":
        from .. import wasi_analysis as W

        manifest = W.manifest(_read(ctx, params["manifest"], 65536)[0])
        module = O.read_bytes(ctx, manifest["module"], W.MAX_MODULE)
        data = _read(ctx, params["input"], W.MAX_INPUT)[0]
        if W.inspect(manifest, module, data) != payload["reviewed"]:
            raise ValueError(
                "WASI module, input, runtime or limits changed after review"
            )
        result = W.execute(manifest, module, data, cancel=ctx.cancel)
        _verify(ctx, payload["sources"])
        if payload["destination"]:
            destination = SF.publish(
                payload["destination"],
                files=[("analysis.json", E.canonical(result) + b"\n", False)],
                receipt={
                    "schema": "raven.wasi-analysis-receipt/v1",
                    "result_sha256": E.digest(result),
                    "module_sha256": result["module_sha256"],
                    "input_sha256": result["input_sha256"],
                },
                cancel=ctx.cancel,
                final_check=lambda: _verify(ctx, payload["sources"]),
            )
            result["destination"] = destination
        return O.report(
            feature,
            "WASI analysis finished within the declared boundary.",
            data=result,
            rows=[
                f"Count: {result['summary']['count']}; sum: {result['summary']['sum']:g}",
                f"Minimum: {result['summary']['minimum']:g}; maximum: {result['summary']['maximum']:g}",
            ],
        )
    if feature == "adapter":
        manifest, _ = _read(ctx, params["manifest"])
        request = _read(ctx, params["request"])[0] if params.get("request") else {}
        if (
            E.digest(manifest) != payload["manifest_digest"]
            or E.digest(request) != payload["request_digest"]
        ):
            raise ValueError("Adapter review data changed")
        result = A.AdapterClient(manifest).request(
            payload["method"], request, cancel=ctx.cancel
        )
        return O.report(
            feature, "Adapter completed within its process/output budget.", data=result
        )
    if feature == "research-portable":
        receipt, exported, _, _ = _portable(params, ctx)
        if (
            E.digest(receipt) != payload["receipt_digest"]
            or E.digest(exported) != payload["export_digest"]
        ):
            raise ValueError("Portable evidence changed after review")
        filename = (
            "ro-crate-metadata.json"
            if params.get("export_format", "workflow-run-ro-crate")
            == "workflow-run-ro-crate"
            else "export.json"
        )
        files = [("raven-evidence.json", E.canonical(receipt) + b"\n", False)]
        if filename != "raven-evidence.json":
            files.append((filename, E.canonical(exported) + b"\n", False))
        bundle_receipt = {
            "schema": "raven.portable-bundle/v1",
            "evidence_digest": E.digest(receipt),
        }
        if payload.get("signer"):
            from .. import evidence_signing as signing

            bundle_receipt["signature"] = {
                "manifest": signing.MANIFEST,
                "signature": signing.SIGNATURE,
                "namespace": signing.NAMESPACE,
            }
            _manifest, signatures = signing.sign(
                params["signing_key"],
                payload["signer"],
                files=files,
                receipt=bundle_receipt,
                identity=receipt["identity"],
                cancel=ctx.cancel,
            )
            files.extend(signatures)
        destination = SF.publish(
            payload["destination"],
            files=files,
            receipt=bundle_receipt,
            cancel=ctx.cancel,
            final_check=lambda: _verify(ctx, payload["sources"]),
        )
        return O.report(
            feature,
            "Signed portable bundle saved."
            if payload.get("signer")
            else "Portable bundle saved.",
            rows=[destination],
            data={
                "destination": destination,
                "evidence_digest": E.digest(receipt),
                "signed": bool(payload.get("signer")),
            },
        )
    if feature == "support-bundle":
        bundle, sources = _support(params, ctx)
        if (
            sources != payload["sources"]
            or E.digest(bundle) != payload["bundle_digest"]
        ):
            raise ValueError("Support report changed after review")
        destination = SF.publish(
            payload["destination"],
            files=[("support.json", E.canonical(bundle) + b"\n", False)],
            receipt={
                "schema": "raven.support-receipt/v1",
                "bundle_digest": E.digest(bundle),
            },
            cancel=ctx.cancel,
            final_check=lambda: _verify(ctx, payload["sources"]),
        )
        return O.report(
            feature,
            "Private support bundle saved; no data was transmitted.",
            rows=[destination],
            data={"destination": destination},
        )
    raise ValueError("This platform inspection has no write action")


# Managed Raven entry points also protect direct adapter callers.
from ..operation_support import (
    managed_run as _managed_run,
    managed_apply as _managed_apply,
)

run = _managed_run(run)
apply = _managed_apply(apply)
