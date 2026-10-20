"""The review ledger must retain source identity, evidence types and uncertainty."""

from __future__ import annotations

import copy
import hashlib
import json

import pytest

from scripts import merge_review_coverage as merge


def claim(identity):
    return {
        "id": identity,
        "source_line": 1,
        "source_text": "original reviewer statement",
    }


def test_baseline_confirmation_needs_a_resolution_and_substantive_evidence():
    assert (
        merge.implementation_state({"status": "confirmed", "resolution": "implemented"})
        == "implemented"
    )
    with pytest.raises(ValueError, match="substantive evidence"):
        merge.aggregate_state([{"item": {"id": "INS-1", "status": "implemented"}}])
    with pytest.raises(ValueError, match="No substantive implementation"):
        merge.aggregate_state(
            [
                {
                    "item": {
                        "id": "INS-1",
                        "status": "confirmed",
                        "evidence": "baseline reproduces",
                    }
                }
            ]
        )
    with pytest.raises(ValueError, match="No substantive implementation"):
        merge.aggregate_state(
            [
                {
                    "item": {
                        "id": "INS-9",
                        "status": "confirmed",
                        "resolution": "delegated to root",
                        "evidence": "root owns it",
                    }
                }
            ]
        )


def test_suffixes_merge_without_dropping_original_typed_records():
    nested = {"sample_count": 3, "qualified": False, "runs": [None, {"seconds": 1.5}]}
    raw = {
        "id": "HV-17-terminal",
        "status": "Implemented",
        "evidence": nested,
        "tests": "a concrete test",
        "limits": ["local only"],
    }
    package = {"findings": [raw], "validation": {"passed": False}}
    findings, independent, additional = merge.derive(
        [claim("HV-17")], {"ui": package}, {}
    )
    assert findings[0]["id"] == "HV-17"
    assert findings[0]["evidence"][0]["entry_id"] == "HV-17-terminal"
    assert findings[0]["evidence"][0]["item"] == raw
    assert findings[0]["tests"][0]["value"] == "a concrete test"
    assert findings[0]["evidence"][0]["item"]["evidence"]["qualified"] is False
    assert findings[0]["package_context"][0]["fields"] == ["validation"]
    assert independent == additional == []


def test_shared_implementations_and_nonnumeric_independent_faults_remain_distinct():
    ids = ("MUT-13", "DATA-21", "MUT-14", "UI-2")
    packages = {
        "owner": {
            "items": [
                {"id": identity, "status": "implemented", "evidence": "regression"}
                for identity in ids
            ]
        }
    }
    independent = {
        "independent-root": {
            "findings": [
                {
                    "id": "COL-DATA-review-accounting-restarts",
                    "status": "fixed_verified",
                    "evidence": {"actual": True},
                }
            ]
        }
    }
    findings, records, _ = merge.derive(
        [claim(identity) for identity in ids], packages, independent
    )
    by_id = {row["id"]: row for row in findings}
    assert by_id["MUT-13"]["duplicate_of"] == "DATA-21"
    assert by_id["DATA-21"]["related_source_claims"] == ["MUT-13"]
    assert by_id["MUT-14"]["related_source_claims"] == ["UI-2"]
    assert by_id["UI-2"]["related_source_claims"] == ["MUT-14"]
    assert len(findings) == 4 and len(records) == 1
    assert records[0]["item"] == independent["independent-root"]["findings"][0]
    assert records[0]["related_source_claims"] == []


def test_missing_source_evidence_fails_and_source_paragraph_is_preserved():
    text = "**INS-1 · High · VERIFIED · `code.py:2`.** Full claim and proposed fix.\nAdditional detail.\n\nUnrelated heading.\n"
    claims = merge.source_claims(text, expected=1)
    assert claims[0]["source_line"] == 1 and claims[0]["source_end_line"] == 2
    assert claims[0]["source_text"] == text.split("\n\n")[0]
    with pytest.raises(ValueError, match="No owner evidence"):
        merge.derive(claims, {}, {})


def test_architectural_milestone_does_not_claim_original_global_target_is_done():
    package = {
        "items": [
            {
                "id": "S-1",
                "status": "implemented_layer_boundaries_with_explicit_remaining_debt",
                "acceptance": "a boundary gate",
                "remaining": "global acyclicity remains unmet",
            }
        ]
    }
    graph = {
        "cyclic_components": [["a", "b"]],
        "cyclic_edges": [["a", "b"], ["b", "a"]],
        "dynamic_module_expressions": [{"module": "a", "line": 3}],
    }
    findings, _, _ = merge.derive(
        [claim("S-1")], {"structure": package}, {}, architecture_graph=graph
    )
    assert findings[0]["status"] == "adjusted_staged_architectural_milestone"
    scope = findings[0]["scope_adjustment"]
    assert scope["original_global_no_cycle_target"] == "unmet"
    assert scope["current_graph"] == {
        "cyclic_components": 1,
        "largest_component_nodes": 2,
        "cyclic_edges": 2,
        "dynamic_module_expressions": 1,
    }
    assert scope["external_hardware_block"] is False


def test_zero_cycle_graph_still_requires_explicit_bound_closure_evidence():
    graph = {
        "cyclic_components": [],
        "cyclic_edges": [],
        "dynamic_module_expressions": [],
        "violations": [],
    }
    package = {
        "items": [{"id": "S-1", "status": "implemented", "acceptance": "strict gate"}]
    }
    findings, _, _ = merge.derive(
        [claim("S-1")], {"structure": package}, {}, architecture_graph=graph
    )
    assert (
        findings[0]["scope_adjustment"]["original_global_no_cycle_target"]
        == "requires explicit root reconciliation"
    )
    closure = {
        "status": "qualified-local",
        "code_revision": "a" * 40,
        "graph_sha256": hashlib.sha256(merge.encode(graph, compact=True)).hexdigest(),
        "command": "python scripts/check_import_layers.py --require-acyclic",
        "tests": [
            {"command": "python -m pytest tests/test_import_layers.py", "passed": True}
        ],
    }
    package["architecture_closure"] = closure
    findings, _, _ = merge.derive(
        [claim("S-1")], {"structure": package}, {}, architecture_graph=graph
    )
    assert findings[0]["status"] == "implemented"
    assert findings[0]["architecture_acceptance"] == closure
    assert findings[0]["scope_adjustment"]["original_global_no_cycle_target"] == "met"
    assert findings[0]["scope_adjustment"]["remaining_work"] is None
    package["architecture_closure"]["graph_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="digest"):
        merge.derive(
            [claim("S-1")], {"structure": package}, {}, architecture_graph=graph
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("cyclic_components", [["a", "b"]]),
        ("cyclic_edges", [["a", "b"]]),
        ("dynamic_module_expressions", [{"module": "a"}]),
        ("violations", ["an upward dependency"]),
    ],
)
def test_claimed_closure_cannot_waive_any_architecture_gap(field, value):
    graph = {
        "cyclic_components": [],
        "cyclic_edges": [],
        "dynamic_module_expressions": [],
        "violations": [],
    }
    graph[field] = value
    package = {
        "items": [{"id": "S-1", "status": "implemented", "acceptance": "strict gate"}],
        "architecture_closure": {"status": "qualified-local"},
    }
    with pytest.raises(ValueError, match="cannot waive"):
        merge.derive(
            [claim("S-1")], {"structure": package}, {}, architecture_graph=graph
        )


def integration_report():
    return {
        "schema": "raven.review-integration/v1",
        "code_revision": "a" * 40,
        "status": "qualified-local",
        "checks": [
            {
                "id": "full-regression",
                "status": "passed",
                "command": "python -m pytest -q",
                "evidence": {
                    "passed": 170,
                    "warnings": [],
                    "hardware_qualified": False,
                },
            }
        ],
        "ci_policy": {
            "requires_hosted_checks": True,
            "delivered_revision": True,
            "status": "pending",
        },
        "limits": ["Original global architecture target remains unmet"],
    }


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("schema", "other/v1", "schema"),
        ("code_revision", "a" * 39, "40-character"),
        ("code_revision", "A" * 40, "40-character"),
        ("status", "everything-passed", "Integration status"),
        ("checks", [], "nonempty array"),
        ("checks", [{}], "IDs"),
        ("ci_policy", "CI required later", "ci_policy"),
        ("ci_policy", {"requires_hosted_checks": True}, "ci_policy"),
    ],
)
def test_malformed_integration_reports_are_rejected(field, value, message):
    report = integration_report()
    report[field] = value
    with pytest.raises((ValueError, TypeError), match=message):
        merge.validate_integration(report)


def test_integration_revision_must_match_explicit_final_revision():
    report = integration_report()
    assert merge.validate_integration(report, final_revision="a" * 40) is report
    with pytest.raises(ValueError, match="does not match"):
        merge.validate_integration(report, final_revision="b" * 40)


@pytest.mark.parametrize("status", ["failed", "pending", "blocked"])
def test_required_unsuccessful_checks_cannot_claim_local_qualification(status):
    report = integration_report()
    report["checks"][0]["status"] = status
    with pytest.raises(ValueError, match="every required check"):
        merge.validate_integration(report)
    report["status"] = "failed"
    assert merge.validate_integration(report) is report


def test_integration_checks_require_unique_ids_commands_and_evidence():
    base = integration_report()
    for field, value, message in (
        ("command", "", "actual command"),
        ("command", [], "actual command"),
        ("evidence", {}, "substantive evidence"),
        ("evidence", False, "substantive evidence"),
        ("required", "true", "boolean"),
        ("status", "unknown", "unsupported status"),
    ):
        report = copy.deepcopy(base)
        report["checks"][0][field] = value
        with pytest.raises((ValueError, TypeError), match=message):
            merge.validate_integration(report)
    report = copy.deepcopy(base)
    report["checks"].append(copy.deepcopy(report["checks"][0]))
    with pytest.raises(ValueError, match="unique"):
        merge.validate_integration(report)
    report = copy.deepcopy(base)
    report["checks"][0]["required"] = False
    with pytest.raises(ValueError, match="at least one required"):
        merge.validate_integration(report)


def test_optional_pending_ci_does_not_become_a_passed_required_check():
    report = integration_report()
    report["checks"].append(
        {
            "id": "hosted-ci",
            "status": "pending",
            "required": False,
            "command": "gh run list --commit " + report["code_revision"],
            "evidence": {"reason": "Must run on the delivered revision"},
        }
    )
    assert merge.validate_integration(report) is report
    assert report["ci_policy"]["status"] == "pending"


def test_integration_report_copy_hash_and_coverage_are_reproducible(tmp_path):
    results = tmp_path / "results"
    review = tmp_path / "review"
    results.mkdir()
    review.mkdir()
    identities = ["INS-" + str(index) for index in range(1, 170)] + ["S-1"]
    review.joinpath("source-review.md").write_text(
        "\n\n".join(
            "**" + identity + " · High · VERIFIED · `code.py`.** Exact original claim."
            for identity in identities
        )
        + "\n"
    )
    for owner in (*merge.OWNERS, "independent-root"):
        name = (
            "review-independent-root-findings.json"
            if owner == "independent-root"
            else "review-" + owner + "-results.json"
        )
        findings = (
            [
                {"id": identity, "status": "implemented", "evidence": "focused test"}
                for identity in identities
            ]
            if owner == "root"
            else []
        )
        results.joinpath(name).write_text(json.dumps({"findings": findings}))
    results.joinpath("review-structural-imports.json").write_text(
        json.dumps(
            {
                "cyclic_components": [["a", "b"]],
                "cyclic_edges": [["a", "b"], ["b", "a"]],
                "dynamic_module_expressions": [{"module": "a", "line": 3}],
            }
        )
    )
    args = ["--results-root", str(results), "--review-dir", str(review)]
    assert merge.main(args) == 0
    coverage = json.loads(review.joinpath("coverage.json").read_text())
    assert coverage["integration"]["status"] == "pending"
    assert all(
        row["integrated_validation"] == "pending" for row in coverage["findings"]
    )
    report = integration_report()
    report_path = tmp_path / "qualified.json"
    report_path.write_text(json.dumps(report))
    qualified = [*args, "--integration-report", str(report_path)]
    assert merge.main(qualified) == 0
    coverage = json.loads(review.joinpath("coverage.json").read_text())
    assert coverage["final_revision"] == report["code_revision"]
    assert coverage["integration_status"] == "qualified-local"
    assert all(
        row["integrated_validation"] == "regression-qualified"
        for row in coverage["findings"]
    )
    architecture = next(row for row in coverage["findings"] if row["id"] == "S-1")
    assert architecture["status"] == "adjusted_staged_architectural_milestone"
    assert (
        architecture["scope_adjustment"]["original_global_no_cycle_target"] == "unmet"
    )
    raw = review.joinpath("integration.json").read_bytes()
    assert json.loads(raw) == report
    assert coverage["integration"]["sha256"] == hashlib.sha256(raw).hexdigest()
    assert (
        coverage["integration"]["source_sha256"]
        == hashlib.sha256(report_path.read_bytes()).hexdigest()
    )
    assert "Hosted CI remains required" in coverage["integration"]["note"]
    assert merge.main([*qualified, "--check"]) == 0
    before = review.joinpath("coverage.json").read_bytes()
    assert merge.main([*qualified, "--final-revision", "b" * 40]) == 2
    report["checks"][0]["status"] = "failed"
    report_path.write_text(json.dumps(report))
    assert merge.main(qualified) == 2
    assert review.joinpath("coverage.json").read_bytes() == before
    assert review.joinpath("integration.json").read_bytes() == raw
