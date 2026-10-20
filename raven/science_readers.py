"""Pure scientific analysis catalog; does not import operation adapters."""
from . import investigation_planner, science_contracts, science_diagnostics, scientific_lineage

READERS = {
    "recommendation": ("Recommendation validity", "Inspect pinned evidence, applicability, expiry and measured scientific outcome.", science_contracts.evaluate),
    "investigation-plan": ("Next investigation action", "Rank declared information actions within explicit time, resource and scientific-risk budgets.", investigation_planner.plan),
    "scientific-impact": ("Scientific impact and revalidation", "Trace affected claims and propose dependency-aware checks; missing lineage remains unknown.", scientific_lineage.analyze),
    "factorial-design": ("Plan factorial experiment", "Compile declared compatible factors into a randomized bounded diagnostic campaign.", science_diagnostics.factorial_plan),
    "factorial-analysis": ("Analyze factorial experiment", "Compare observed controls, equivalence, main effects and interactions without causal percentages.", science_diagnostics.factorial),
    "numerical-stability": ("Numerical stability laboratory", "Analyze measured configuration repeats, tolerance violations and missing measurements.", science_diagnostics.numerical),
    "regression-investigation": ("Performance regression investigation", "Compare ordered versions with scientific checks, noise and multiple comparisons accounted for.", science_diagnostics.regression),
}
