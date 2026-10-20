"""Finite callable worker composition registry; no module-name lookup."""

from __future__ import annotations
from dataclasses import dataclass
from functools import lru_cache
from types import MappingProxyType
from typing import Callable
from .policy import _PURE


@dataclass(frozen=True)
class PureBinding:
    function: Callable
    fields: tuple[str, ...]


def _load_statistics():
    from ..science_stats import compare

    return PureBinding(compare, ("document",))


def _load_acceptance():
    from ..science_stats import acceptance

    return PureBinding(acceptance, ("document", "results"))


def _load_numerical_stability():
    from ..science_diagnostics import numerical

    return PureBinding(numerical, ("document",))


def _load_factorial_design():
    from ..science_diagnostics import factorial_plan

    return PureBinding(factorial_plan, ("document",))


def _load_factorial_analysis():
    from ..science_diagnostics import factorial

    return PureBinding(factorial, ("document",))


def _load_regression_investigation():
    from ..science_diagnostics import regression

    return PureBinding(regression, ("document",))


def _load_recommendation():
    from ..science_contracts import evaluate

    return PureBinding(evaluate, ("document",))


def _load_investigation_plan():
    from ..investigation_planner import plan

    return PureBinding(plan, ("document",))


def _load_scientific_impact():
    from ..scientific_lineage import analyze

    return PureBinding(analyze, ("document",))


def _load_causal_delay():
    from ..tracing.analysis import analyze_causality

    return PureBinding(analyze_causality, ("events",))


def _load_collective_audit():
    from ..tracing.analysis import audit_collectives

    return PureBinding(audit_collectives, ("events",))


def _load_execution_simulation():
    from ..execution_simulator import simulate

    return PureBinding(simulate, ("document", "scenario"))


def _load_diagnostic_recipe():
    from ..diagnostic_recipes import compile_recipe

    return PureBinding(compile_recipe, ("document", "capabilities"))


def _load_information_query():
    from ..diagnostic_recipes import summarize_query

    return PureBinding(summarize_query, ("document", "query"))


def _load_scientific_field():
    from ..science_fields import inspect_field

    return PureBinding(inspect_field, ("document",))


def _load_mcp_evidence_read():
    from ..mcp_evidence import read

    return PureBinding(read, ("document",))


LOADERS = MappingProxyType(
    {
        "statistics": _load_statistics,
        "acceptance": _load_acceptance,
        "numerical-stability": _load_numerical_stability,
        "factorial-design": _load_factorial_design,
        "factorial-analysis": _load_factorial_analysis,
        "regression-investigation": _load_regression_investigation,
        "recommendation": _load_recommendation,
        "investigation-plan": _load_investigation_plan,
        "scientific-impact": _load_scientific_impact,
        "causal-delay": _load_causal_delay,
        "collective-audit": _load_collective_audit,
        "execution-simulation": _load_execution_simulation,
        "diagnostic-recipe": _load_diagnostic_recipe,
        "information-query": _load_information_query,
        "scientific-field": _load_scientific_field,
        "mcp-evidence-read": _load_mcp_evidence_read,
    }
)


@lru_cache(maxsize=32)
def pure_binding(operation):
    if operation not in _PURE or operation not in LOADERS:
        raise ValueError("Unknown worker operation binding: " + str(operation))
    return LOADERS[operation]()
