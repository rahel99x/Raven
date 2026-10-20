#!/usr/bin/env python3
"""Rebuild the 2026-10-20 review ledger without losing source or owner evidence.

This documentation-only tool never imports Raven or changes runtime code. Owner
ledgers may continue to evolve: every run reads them again and replaces derived
coverage rather than appending to it. Integrated qualification stays pending
unless an explicit, validated root integration report is supplied.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OWNERS = (
    "root",
    "data",
    "heavy",
    "injob",
    "install",
    "mutations",
    "ui",
    "structure",
    "s3-ci",
)
CLAIM = re.compile(r"^\*\*([A-Z]+-\d+)\s*·\s*([^·]+)\s*·\s*(.*?)\*\*")
IDENTITY = re.compile(r"^([A-Z]+)[-\s]*(\d+)(?:\b|(?=[a-z]))", re.IGNORECASE)
CONTEXT_FIELDS = (
    "tests",
    "validation",
    "focused_validation",
    "qualification",
    "qualification_limits",
    "limits",
    "performance",
    "performance_artifact",
    "performance_limits",
    "benchmarks",
    "design_tradeoffs",
    "design_alternatives",
    "tradeoffs",
    "review",
    "structure",
    "maintenance_review",
    "live_slurm",
    "remaining",
    "reviewed_gate_adjustment",
)
SHARED = (("MUT-13", "DATA-21"), ("MUT-14", "UI-2"))


def encode(value, *, compact=False):
    options = {"separators": (",", ":")} if compact else {"indent": 2}
    return (
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, allow_nan=False, **options
        )
        + "\n"
    ).encode()


def read_json(path):
    """A concurrent owner rewrite must cause a retry, never a truncated merge."""
    for attempt in range(3):
        try:
            before = path.stat()
            if before.st_size > 8 << 20:
                raise ValueError(f"Owner ledger exceeds its 8 MiB bound: {path}")
            raw = path.read_bytes()
            after = path.stat()

            def pin(info):
                return (
                    info.st_dev,
                    info.st_ino,
                    info.st_size,
                    info.st_mtime_ns,
                    info.st_ctime_ns,
                )

            if pin(before) != pin(after):
                raise ValueError(f"Owner ledger changed while reading: {path}")
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise TypeError(f"Expected an owner ledger object: {path}")
            return value, hashlib.sha256(raw).hexdigest()
        except (json.JSONDecodeError, OSError, TypeError, ValueError):
            if attempt == 2:
                raise
            time.sleep(0.05)
    raise AssertionError("unreachable")


def normalize_id(value):
    if not isinstance(value, str) or (match := IDENTITY.match(value)) is None:
        raise ValueError(f"Unsupported finding identity: {value!r}")
    return match[1].upper() + "-" + str(int(match[2]))


def source_claims(text, *, expected=170):
    lines, findings, seen = text.splitlines(), [], set()
    for offset, line in enumerate(lines):
        match = CLAIM.match(line)
        if match is None:
            continue
        identity = normalize_id(match[1])
        if identity in seen:
            raise ValueError(f"Duplicate source claim: {identity}")
        seen.add(identity)
        end = offset + 1
        while (
            end < len(lines) and lines[end].strip() and CLAIM.match(lines[end]) is None
        ):
            end += 1
        findings.append(
            {
                "id": identity,
                "severity": match[2].strip(),
                "review_claim": match[3].strip(),
                "source_line": offset + 1,
                "source_end_line": end,
                "source_text": "\n".join(lines[offset:end]),
            }
        )
    if len(findings) != expected:
        raise ValueError(f"Expected {expected} source claims; found {len(findings)}")
    return findings


def implementation_state(item):
    """Verification of a baseline claim alone does not establish its resolution."""
    status = str(item.get("status", "")).lower()
    resolution = str(item.get("resolution", "")).lower()
    effective = (
        resolution if status in {"confirmed", "adjusted"} and resolution else status
    )
    if "delegat" in effective:
        return "delegated"
    if "blocked" in effective:
        return "blocked_with_evidence"
    if "reject" in effective or "not_applicable" in effective:
        return "rejected_with_evidence"
    if "already" in effective and (
        "satisfied" in effective or "implemented" in effective
    ):
        return "already_satisfied"
    if "duplicate" in effective or "consolidat" in effective:
        return "consolidated"
    implemented = (
        "implement" in effective
        or effective == "adjusted"
        and bool(item.get("implementation"))
    )
    if implemented:
        if (
            item.get("remaining")
            or "remaining_debt" in effective
            or "staged" in effective
        ):
            return "implemented_with_documented_limits"
        if "adjust" in effective or status == "adjusted":
            return "implemented_with_adjustments"
        return "implemented"
    if any(
        word in effective for word in ("harness", "integrated", "expanded", "gates")
    ):
        return "supplemental_qualification"
    return "unresolved"


def aggregate_state(entries):
    substantive_fields = ("evidence", "tests", "acceptance", "validation")
    if not any(
        any(entry["item"].get(field) for field in substantive_fields)
        for entry in entries
    ):
        raise ValueError("Owner entries provide a status without substantive evidence")
    states = {implementation_state(entry["item"]) for entry in entries}
    for state in (
        "implemented_with_documented_limits",
        "implemented_with_adjustments",
        "implemented",
        "already_satisfied",
        "consolidated",
        "rejected_with_evidence",
        "blocked_with_evidence",
    ):
        if state in states:
            return state
    raise ValueError(
        "No substantive implementation/resolution evidence: "
        + ", ".join(sorted(states))
    )


def rows(package):
    for key in ("items", "findings", "coverage"):
        if key in package:
            value = package[key]
            if not isinstance(value, list) or any(
                not isinstance(row, dict) for row in value
            ):
                raise ValueError(f"Owner {key} must contain finding objects")
            return value
    raise ValueError("Owner ledger has no items/findings/coverage array")


def validate_integration(report, *, final_revision=None):
    """Local regression evidence does not establish hosted CI or source acceptance."""
    if (
        not isinstance(report, dict)
        or report.get("schema") != "raven.review-integration/v1"
    ):
        raise ValueError("Expected integration schema raven.review-integration/v1")
    revision = report.get("code_revision")
    if not isinstance(revision, str) or re.fullmatch(r"[a-f0-9]{40}", revision) is None:
        raise ValueError(
            "Integration code_revision requires a complete lowercase 40-character commit ID"
        )
    if final_revision is not None and final_revision != revision:
        raise ValueError("Integration code_revision does not match --final-revision")
    status = report.get("status")
    if status not in ("qualified-local", "pending", "failed"):
        raise ValueError(
            "Integration status must be qualified-local, pending or failed"
        )
    checks = report.get("checks")
    if not isinstance(checks, list) or not checks:
        raise ValueError("Integration checks must be a nonempty array")
    identities, required = set(), []
    for check in checks:
        if not isinstance(check, dict):
            raise TypeError("Integration checks must be objects")
        identity = check.get("id")
        if (
            not isinstance(identity, str)
            or not identity.strip()
            or identity in identities
        ):
            raise ValueError("Integration check IDs must be nonempty and unique")
        identities.add(identity)
        if check.get("status") not in ("passed", "failed", "pending", "blocked"):
            raise ValueError(f"Integration check {identity} has an unsupported status")
        is_required = check.get("required", True)
        if not isinstance(is_required, bool):
            raise TypeError("Integration check required must be a boolean")
        command = check.get("command")
        commands = command if isinstance(command, list) else [command]
        if not commands or not all(
            isinstance(value, str) and value.strip() for value in commands
        ):
            raise ValueError(f"Integration check {identity} requires an actual command")
        evidence = check.get("evidence")
        if not isinstance(evidence, (str, list, dict)) or not evidence:
            raise ValueError(
                f"Integration check {identity} requires substantive evidence"
            )
        if is_required:
            required.append(check)
    if not required:
        raise ValueError("Integration report requires at least one required check")
    if status == "qualified-local" and any(
        check["status"] != "passed" for check in required
    ):
        raise ValueError(
            "Qualified-local integration requires every required check to pass"
        )
    policy = report.get("ci_policy")
    if (
        not isinstance(policy, dict)
        or policy.get("requires_hosted_checks") is not True
        or policy.get("delivered_revision") is not True
    ):
        raise ValueError(
            "Integration ci_policy must require hosted checks on the delivered revision"
        )
    return report


def derive(
    claims,
    packages,
    independent,
    *,
    owner_map=None,
    architecture_graph=None,
    architecture_plan=None,
):
    """Keep original typed rows; convenience indexes never replace raw evidence."""
    owner_map = {} if owner_map is None else owner_map
    by_id = {
        claim["id"]: {**claim, "evidence": [], "tests": [], "limits": []}
        for claim in claims
    }
    additional = []
    for owner, package in packages.items():
        ledger = "packages/" + owner + ".json"
        for item in rows(package):
            identity = normalize_id(item.get("id"))
            entry = {
                "package": owner,
                "ledger": ledger,
                "entry_id": item["id"],
                "item": item,
            }
            if identity not in by_id:
                additional.append({**entry, "normalized_id": identity})
                continue
            finding = by_id[identity]
            finding["evidence"].append(entry)
            for field in ("tests", "acceptance"):
                if field in item:
                    finding["tests"].append(
                        {"package": owner, "field": field, "value": item[field]}
                    )
            for field in (
                "limits",
                "qualification_limits",
                "qualification_limit",
                "remaining",
            ):
                if field in item:
                    finding["limits"].append(
                        {"package": owner, "field": field, "value": item[field]}
                    )
    for identity, finding in by_id.items():
        if not finding["evidence"]:
            raise ValueError(f"No owner evidence for accepted source claim {identity}")
        try:
            finding["status"] = aggregate_state(finding["evidence"])
        except ValueError as exc:
            raise ValueError(f"{identity}: {exc}") from exc
        owners = list(dict.fromkeys(entry["package"] for entry in finding["evidence"]))
        finding["owners"] = owners
        finding["owner"] = owner_map.get(identity, owners[0])
        finding["package_context"] = [
            {
                "package": owner,
                "ledger": "packages/" + owner + ".json",
                "fields": [
                    field for field in CONTEXT_FIELDS if field in packages[owner]
                ],
            }
            for owner in owners
        ]
        finding["integrated_validation"] = "pending"
    if "S-1" in by_id:
        if architecture_graph is None:
            raise ValueError(
                "S-1 requires the actual all-depth architecture graph evidence"
            )
        components = architecture_graph["cyclic_components"]
        edges = architecture_graph["cyclic_edges"]
        dynamic = architecture_graph["dynamic_module_expressions"]
        if not all(isinstance(value, list) for value in (components, edges, dynamic)):
            raise ValueError(
                "Architecture graph component/edge/dynamic fields must be arrays"
            )
        closure = packages.get("structure", {}).get("architecture_closure")
        complete_graph = not (
            components or edges or dynamic or architecture_graph.get("violations")
        )
        reconciled = False
        if closure is not None:
            if not isinstance(closure, dict):
                raise ValueError("Architecture closure evidence must be an object")
            if closure.get("status") == "qualified-local":
                if not complete_graph:
                    raise ValueError(
                        "Architecture closure cannot waive cycles, unresolved imports or violations"
                    )
                if not re.fullmatch(
                    r"[0-9a-f]{40}", str(closure.get("code_revision", ""))
                ):
                    raise ValueError(
                        "Architecture closure requires its qualified code revision"
                    )
                if (
                    closure.get("graph_sha256")
                    != hashlib.sha256(
                        encode(architecture_graph, compact=True)
                    ).hexdigest()
                ):
                    raise ValueError(
                        "Architecture closure graph digest does not match the actual evidence"
                    )
                command = closure.get("command")
                if (
                    not isinstance(command, str)
                    or "--require-acyclic" not in command.split()
                ):
                    raise ValueError(
                        "Architecture closure requires the strict all-depth gate command"
                    )
                if not isinstance(closure.get("tests"), list) or not closure["tests"]:
                    raise ValueError(
                        "Architecture closure requires behavioral qualification evidence"
                    )
                reconciled = True
        by_id["S-1"]["status"] = (
            "implemented" if reconciled else "adjusted_staged_architectural_milestone"
        )
        by_id["S-1"]["scope_adjustment"] = {
            "source_plan": architecture_plan,
            "original_global_no_cycle_target": "met"
            if reconciled
            else "unmet"
            if components or dynamic
            else "requires explicit root reconciliation",
            "delivered_scope": "Declared layer boundaries, explicit composition and globally acyclic imports including function bodies."
            if reconciled
            else "Enforced layer boundaries and staged extraction; PR 17 remains ongoing.",
            "graph_evidence": "packages/architecture-graph.json",
            "current_graph": {
                "cyclic_components": len(components),
                "largest_component_nodes": max(
                    (len(component) for component in components), default=0
                ),
                "cyclic_edges": len(edges),
                "dynamic_module_expressions": len(dynamic),
            },
            "remaining_work": None
            if reconciled
            else "Break the residual UI cooperation cycles using explicit application/domain ports or composition registration, and resolve the remaining dynamic module expressions.",
            "external_hardware_block": False,
        }
        if reconciled:
            by_id["S-1"]["architecture_acceptance"] = closure
    for left, right in SHARED:
        if left in by_id and right in by_id:
            for identity, other in ((left, right), (right, left)):
                by_id[identity]["related_source_claims"] = [other]
                by_id[identity]["shared_implementation_group"] = [left, right]
            if left == "MUT-13":
                by_id[left]["duplicate_of"] = right
    independent_rows = []
    for name, package in independent.items():
        for item in rows(package):
            supplied = item.get("related_source_claims", [])
            if not isinstance(supplied, list):
                raise TypeError("Independent related_source_claims must be an array")
            related = [normalize_id(value) for value in supplied]
            if not related:
                try:
                    related = [normalize_id(item.get("id"))]
                except ValueError:
                    # Independent faults need not reuse a numbered source ID.
                    # Preserve them without inventing a source relationship.
                    related = []
            related = [identity for identity in related if identity in by_id]
            record = {
                "package": name,
                "ledger": "packages/" + name + ".json",
                "item": item,
                "related_source_claims": related,
            }
            independent_rows.append(record)
            for identity in related:
                by_id[identity].setdefault("independent_review_findings", []).append(
                    {"ledger": record["ledger"], "id": item["id"]}
                )
    return list(by_id.values()), independent_rows, additional


def atomic_write(path, raw):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix="." + path.name + "-", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results-root", type=Path, default=Path("/workspace"))
    parser.add_argument(
        "--review-dir", type=Path, default=ROOT / "docs/reviews/2026-10-20"
    )
    parser.add_argument(
        "--final-revision",
        help="Full qualified code commit; later evidence-only commits and CI are recorded separately",
    )
    parser.add_argument(
        "--integration-report",
        type=Path,
        help="Root integration JSON; omitted reports preserve pending qualification",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Check deterministic outputs without writing",
    )
    args = parser.parse_args(argv)
    if (
        args.final_revision is not None
        and re.fullmatch(r"(?:[a-f0-9]{40}|[a-f0-9]{64})", args.final_revision) is None
    ):
        parser.error("--final-revision requires the complete lowercase commit ID")
    try:
        integration_report, integration_source_sha = None, None
        if args.integration_report is not None:
            integration_report, integration_source_sha = read_json(
                args.integration_report
            )
            validate_integration(integration_report, final_revision=args.final_revision)
        source = (args.review_dir / "source-review.md").read_bytes()
        claims = source_claims(source.decode("utf-8"))
        coverage_path = args.review_dir / "coverage.json"
        old = read_json(coverage_path)[0] if coverage_path.exists() else {}
        owner_map = {
            item["id"]: item["owner"]
            for item in old.get("findings", [])
            if "owner" in item
        }
        packages, independent, outputs, manifest = {}, {}, {}, {}
        inputs = {
            owner: ("review-" + owner + "-results.json", owner + ".json")
            for owner in OWNERS
        }
        inputs["independent-root"] = (
            "review-independent-root-findings.json",
            "independent-root.json",
        )
        for owner, candidates in inputs.items():
            path = next(
                (
                    args.results_root / name
                    for name in candidates
                    if (args.results_root / name).is_file()
                ),
                None,
            )
            if path is None:
                raise ValueError(
                    f"Missing {owner} ledger under {args.results_root}; expected one of {candidates}"
                )
            value, input_sha = read_json(path)
            raw = encode(value, compact=True)
            relative = Path("packages") / (owner + ".json")
            outputs[args.review_dir / relative] = raw
            manifest[owner] = {
                "path": str(relative),
                "source_name": path.name,
                "source_sha256": input_sha,
                "sha256": hashlib.sha256(raw).hexdigest(),
            }
            (independent if owner.startswith("independent-") else packages)[owner] = (
                value
            )
        graph_path = next(
            (
                args.results_root / name
                for name in (
                    "review-structural-imports.json",
                    "architecture-graph.json",
                )
                if (args.results_root / name).is_file()
            ),
            None,
        )
        if graph_path is None:
            raise ValueError(
                "Missing review-structural-imports.json / architecture-graph.json supporting evidence"
            )
        architecture_graph, graph_sha = read_json(graph_path)
        graph_raw = encode(architecture_graph, compact=True)
        outputs[args.review_dir / "packages/architecture-graph.json"] = graph_raw
        manifest["architecture-graph"] = {
            "path": "packages/architecture-graph.json",
            "source_name": graph_path.name,
            "source_sha256": graph_sha,
            "sha256": hashlib.sha256(graph_raw).hexdigest(),
        }
        architecture_plan = {}
        for offset, line in enumerate(source.decode("utf-8").splitlines(), 1):
            if line.startswith("**PR 17 "):
                architecture_plan.update(title=line, source_line=offset)
            if (
                "Done when the import graph has no cycle with function-level imports counted."
                in line
            ):
                architecture_plan["completion_target"] = {
                    "source_line": offset,
                    "text": line,
                }
        findings, independent_rows, additional = derive(
            claims,
            packages,
            independent,
            owner_map=owner_map,
            architecture_graph=architecture_graph,
            architecture_plan=architecture_plan,
        )
        closure = packages.get("structure", {}).get("architecture_closure")
        if (
            integration_report is not None
            and integration_report["status"] == "qualified-local"
            and isinstance(closure, dict)
            and closure.get("status") == "qualified-local"
            and closure.get("code_revision") != integration_report["code_revision"]
        ):
            raise ValueError(
                "Architecture closure and integrated qualification must refer to the same code revision"
            )
        final_revision = args.final_revision
        integration_status = "pending: final integrated regression, packaging, schema, compatibility and CI evidence"
        integrated_validation = "pending"
        integration = {
            "status": "pending",
            "revision": final_revision,
            "note": "Owner focused results and simulations are retained as recorded; they do not establish full integrated validation.",
        }
        if integration_report is not None:
            raw_report = encode(integration_report)
            outputs[args.review_dir / "integration.json"] = raw_report
            final_revision = final_revision or integration_report["code_revision"]
            integration_status = integration_report["status"]
            if integration_status == "qualified-local":
                integrated_validation = "regression-qualified"
            for finding in findings:
                finding["integrated_validation"] = integrated_validation
            integration = {
                "status": integration_status,
                "revision": final_revision,
                "report": "integration.json",
                "source_sha256": integration_source_sha,
                "sha256": hashlib.sha256(raw_report).hexdigest(),
                "note": "Local regression qualification preserves each finding's acceptance limits. Hosted CI remains required on the delivered revision and is not established by qualified-local status.",
            }
        coverage = {
            "schema": "raven.adversarial-review-coverage/v2",
            "reviewed_revision": old.get("reviewed_revision", "b98742f"),
            "implementation_base": old.get("implementation_base", "6faeea2"),
            "source_sha256": hashlib.sha256(source).hexdigest(),
            "source_review": "source-review.md",
            "source_claim_count": len(claims),
            "findings": findings,
            "package_ledgers": manifest,
            "independent_findings": independent_rows,
            "additional_package_findings": additional,
            "final_revision": final_revision,
            "integration_status": integration_status,
            "integration": integration,
        }
        outputs[coverage_path] = encode(coverage)
        changed = [
            str(path)
            for path, raw in outputs.items()
            if not path.exists() or path.read_bytes() != raw
        ]
        if args.check and changed:
            raise ValueError("Coverage outputs need rebuilding: " + ", ".join(changed))
        if not args.check:
            for path, raw in outputs.items():
                atomic_write(path, raw)
        print(
            json.dumps(
                {
                    "source_claims": len(findings),
                    "independent_findings": len(independent_rows),
                    "package_ledgers": len(manifest),
                    "changed_files": len(changed),
                    "integrated_validation": integrated_validation,
                },
                sort_keys=True,
            )
        )
        return 0
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print("Review coverage: " + str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
