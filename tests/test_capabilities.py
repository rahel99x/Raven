"""Original requirements retain one owner despite overlapping mechanisms."""
from dataclasses import FrozenInstanceError, replace
import importlib.util
from pathlib import Path
import re
import subprocess
import sys

import pytest

from tower import capabilities as C
from tower.operations import catalog

ROOT = Path(__file__).resolve().parents[1]


def generator():
    spec = importlib.util.spec_from_file_location(
        "generate_capability_reference", ROOT / "scripts" / "generate_capability_reference.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_exact_original_entry_bijection_and_one_owner_per_requirement():
    expected = {"V5-TRACE"} | {f"V6-{n:02}" for n in range(1, 11)} | {
        f"F{n:02}" for n in range(1, 41)} | {f"R{n}" for n in range(1, 6)}
    assert len(C.PROPOSALS) == len(expected) == 56
    assert {item.id for item in C.PROPOSALS} == expected
    assert len(C.AREAS) == 8
    owned = [item.id for area in C.AREAS for item in C.for_area(area.key)]
    assert len(owned) == len(set(owned)) == 56
    assert set(owned) == expected
    assert C.validate() == ()


def test_source_dossier_and_ledger_ids_remain_fully_accounted_for():
    ledger = (ROOT / "docs" / "FLAGSHIP_IMPLEMENTATION.md").read_text(encoding="utf-8")
    dossier = (ROOT / "docs" / "research" / "flagship-strategy.md").read_text(encoding="utf-8")
    ledger_ids = {"F" + match for match in re.findall(r"^\| (\d\d) ", ledger, re.M)}
    dossier_ids = {"F" + match for match in re.findall(r"^\| (\d\d) \|", dossier, re.M)}
    assert ledger_ids == dossier_ids == {item.id for item in C.PROPOSALS if item.id.startswith("F")}
    assert set(re.findall(r"^\| (R[1-5]) ", ledger, re.M)) == {f"R{n}" for n in range(1, 6)}


def test_operation_associations_refer_to_installed_keys_without_double_counting():
    installed = {item["key"] for item in catalog()}
    associated = {key for proposal in C.PROPOSALS for key in proposal.operations}
    assert associated <= installed
    assert {item.id for item in C.for_operation("causal-delay")} == {"V6-01", "F22"}
    assert {item.id for item in C.for_operation("investigation-plan")} == {"V6-10", "F26", "R2"}
    assert C.for_operation("not-associated") == ()
    for proposal in C.PROPOSALS:
        for other in proposal.related:
            assert proposal.id in C.lookup(other).related


@pytest.mark.parametrize("number", range(1, 41))
def test_flagship_numeric_and_named_aliases_are_stable(number):
    item = C.lookup(f"F{number:02}")
    assert C.lookup(f"{number:02}") is item
    assert C.lookup(f"flagship-{number:02}") is item
    assert C.lookup(f"  f{number:02}  ") is item


def test_original_named_and_research_aliases_are_stable():
    original = ("causal-explorer", "collective-auditor", "execution-simulator",
                "numerical-robustness", "minimal-reproducer", "performance-regression",
                "i-o-coordination", "live-steering", "lineage-impact", "observability-planner")
    for number, name in enumerate(original, 1):
        assert C.lookup("v6-" + name) is C.lookup(f"V6-{number:02}")
    for number in range(1, 6):
        assert C.lookup(f"research-{number}") is C.lookup(f"R{number}")
    assert C.lookup("stack-tracing") is C.lookup("V5-TRACE")


@pytest.mark.parametrize("value", [None, 7, [], {}, "", "unknown", "F41"])
def test_invalid_references_never_fuzzily_match_or_select_a_command(value):
    with pytest.raises(ValueError):
        C.lookup(value)


def test_partial_delivery_and_external_programs_are_not_reported_complete():
    for identifier in ("F03", "F08", "F09", "F14", "F17", "F22", "F32", "V5-TRACE"):
        assert C.lookup(identifier).delivery == "partial"
    assert C.lookup("F39").delivery == C.lookup("F40").delivery == "procedure"
    assert C.lookup("F39").gates == ("external-users",)
    assert C.lookup("F40").gates == ("research",)
    assert all("research" in C.lookup(f"R{number}").gates for number in range(1, 6))
    assert "no GPU-device stacks" in C.lookup("V5-TRACE").boundary


def test_catalog_is_immutable_and_runtime_lookup_imports_no_operations():
    with pytest.raises(FrozenInstanceError):
        C.lookup("F01").area = "other"
    result = subprocess.run([sys.executable, "-c", "\n".join((
        "import sys",
        "from tower.capabilities import lookup, for_area, for_operation",
        "assert lookup('01').id == 'F01'",
        "assert for_area('experiments')",
        "assert for_operation('causal-delay')",
        "assert 'tower.operations' not in sys.modules",
        "assert not any(name.startswith('tower.ops_') for name in sys.modules)",
    ))], cwd=ROOT, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("mutation,expected", [
    (lambda rows: rows[:-1], "exactly the 56"),
    (lambda rows: rows + rows[:1], "Duplicate proposal ID"),
    (lambda rows: (replace(rows[0], area="missing"),) + rows[1:], "unknown owning area"),
    (lambda rows: (replace(rows[0], operations=("not-installed",)),) + rows[1:], "unknown operation"),
    (lambda rows: (replace(rows[0], aliases=("F01",)),) + rows[1:], "ambiguous alias"),
    (lambda rows: (replace(rows[0], related=("F99",)),) + rows[1:], "unknown overlap"),
])
def test_generator_validation_rejects_broken_ownership_or_aliases(mutation, expected):
    errors = C.validate(proposals=mutation(C.PROPOSALS))
    assert any(expected in error for error in errors)


def test_generated_reference_is_current_and_ownership_tables_list_each_id_once():
    module = generator()
    assert module.main(["--check"]) == 0
    text = module.render()
    tables = text.split("## Runtime lookup and compatibility")[0]
    ids = re.findall(r"^\| ((?:F\d\d|R[1-5]|V6-\d\d|V5-TRACE)) —", tables, re.M)
    assert len(ids) == len(set(ids)) == 56


def test_check_mode_reports_missing_stale_reference_without_rewriting(tmp_path):
    module = generator()
    target = tmp_path / "capabilities.md"
    assert module.main(["--check", "--output", str(target)]) == 1
    assert not target.exists()
    assert module.main(["--output", str(target)]) == 0
    target.write_text("stale\n", encoding="utf-8")
    assert module.main(["--check", "--output", str(target)]) == 1
    assert target.read_text(encoding="utf-8") == "stale\n"
