#!/usr/bin/env python3
"""Rebuild the 2026-10-20 review ledger without losing source or owner evidence.

This documentation-only tool never imports Raven or changes runtime code. Owner
ledgers may continue to evolve: every run reads them again and replaces derived
coverage rather than appending to it. Final integrated qualification stays pending.
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
                raise ValueError(f"Expected an owner ledger object: {path}")
            return value, hashlib.sha256(raw).hexdigest()
        except (json.JSONDecodeError, OSError, ValueError):
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
        by_id["S-1"]["status"] = "adjusted_staged_architectural_milestone"
        by_id["S-1"]["scope_adjustment"] = {
            "source_plan": architecture_plan,
            "original_global_no_cycle_target": "unmet"
            if components or dynamic
            else "requires explicit root reconciliation",
            "delivered_scope": "Enforced layer boundaries and staged extraction; PR 17 remains ongoing.",
            "graph_evidence": "packages/architecture-graph.json",
            "current_graph": {
                "cyclic_components": len(components),
                "largest_component_nodes": max(
                    (len(component) for component in components), default=0
                ),
                "cyclic_edges": len(edges),
                "dynamic_module_expressions": len(dynamic),
            },
            "remaining_work": "Break the residual UI cooperation cycles using explicit application/domain ports or composition registration, and resolve the remaining dynamic module expressions.",
            "external_hardware_block": False,
        }
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
                raise ValueError("Independent related_source_claims must be an array")
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
        help="Full root-supplied delivered commit; does not assert CI success",
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
            "final_revision": args.final_revision,
            "integration_status": "pending: final integrated regression, packaging, schema, compatibility and CI evidence",
            "integration": {
                "status": "pending",
                "revision": args.final_revision,
                "note": "Owner focused results and simulations are retained as recorded; they do not establish full integrated validation.",
            },
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
                    "integrated_validation": "pending",
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
