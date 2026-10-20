"""Canonical group inference works without importing terminal action owners."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from raven import job_groups, job_group_domain, job_group_records, manual_job_groups
from raven import job_group_membership
from raven import job_list_controls, job_batch_sort, recents_ui
from raven.ui import list_preferences


def test_compatibility_exports_keep_canonical_record_and_query_identity():
    for name in ("Group", "Index", "Header", "Child", "GroupMeta", "StatusSummary"):
        assert getattr(job_groups, name) is getattr(job_group_records, name)
    assert job_groups.Registry is job_group_domain.Registry
    assert job_groups.project_records is job_group_domain.project_records
    assert manual_job_groups.ManualState is job_group_membership.ManualState
    assert (
        manual_job_groups.SelectionTokenCache
        is job_group_membership.SelectionTokenCache
    )
    assert job_list_controls.mode is list_preferences.mode
    assert job_list_controls.fingerprint is list_preferences.fingerprint
    assert job_batch_sort.mode is list_preferences.jobs_batch_mode
    assert recents_ui.mode is list_preferences.recent_mode


def test_inference_and_manual_membership_do_not_import_action_facades():
    code = """
import sys
from raven.job_group_domain import Registry
from raven.model import Job
registry=Registry()
records=[Job('17_1','experiment','local','RUNNING'),Job('17_2','experiment','local','PENDING')]
index=registry.ensure({'jobs':records})
assert index.for_job('17_1').members==('17_1','17_2')
assert registry.fold(index.for_job('17_1').id,True)
assert len(registry.project(records))==1
for owner in ('job_groups','manual_job_groups','job_list_controls','recents_ui','grouping_toolbar','views','controller'):
    assert 'raven.'+owner not in sys.modules,owner
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr


def test_jobs_family_legacy_modules_are_exact_canonical_identity():
    import importlib

    for legacy, canonical in (
        ("job_groups", "group_domain"),
        ("job_group_domain", "group_domain"),
        ("job_group_records", "group_records"),
        ("job_group_membership", "group_membership"),
        ("job_batch_sort", "batch_sort"),
        ("manual_job_groups", "manual_groups"),
        ("job_copy", "copy"),
        ("job_group_actions", "group_actions"),
        ("job_group_drag", "group_drag"),
        ("job_group_menu", "group_menu"),
        ("job_group_ui", "group_ui"),
        ("job_identity", "identity"),
        ("job_list_controls", "list_controls"),
        ("job_list_menu", "list_menu"),
        ("job_panels", "panels"),
        ("job_progress", "progress"),
        ("job_row_cache", "row_cache"),
        ("job_selection", "selection"),
    ):
        assert importlib.import_module("raven." + legacy) is importlib.import_module(
            "raven.jobs." + canonical
        )


def test_production_jobs_consumers_use_canonical_package_imports():
    import ast

    package = Path(__file__).resolve().parents[1] / "raven"
    legacy = {
        "job_groups",
        "job_group_domain",
        "job_group_records",
        "job_group_membership",
        "job_batch_sort",
        "manual_job_groups",
        "job_copy",
        "job_group_actions",
        "job_group_drag",
        "job_group_menu",
        "job_group_ui",
        "job_identity",
        "job_list_controls",
        "job_list_menu",
        "job_panels",
        "job_progress",
        "job_row_cache",
        "job_selection",
    }
    failures = []
    for path in package.rglob("*.py"):
        if path.parent == package and path.stem in legacy:
            continue
        module = ".".join(("raven", *path.relative_to(package).with_suffix("").parts))
        parent = module.split(".")[:-1]
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ImportFrom):
                base = node.module or ""
                if node.level:
                    base = ".".join(parent[: len(parent) - node.level + 1]) + (
                        "." + base if base else ""
                    )
                if base in {"raven." + name for name in legacy} or (
                    base == "raven"
                    and any(alias.name in legacy for alias in node.names)
                ):
                    failures.append((str(path.relative_to(package)), node.lineno))
            elif isinstance(node, ast.Import):
                if any(
                    alias.name in {"raven." + name for name in legacy}
                    for alias in node.names
                ):
                    failures.append((str(path.relative_to(package)), node.lineno))
    assert failures == []
