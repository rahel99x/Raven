"""The review ledger must retain source identity, evidence types and uncertainty."""

from __future__ import annotations

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
