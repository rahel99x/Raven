"""Pure workspace names and supported sort keys, independent of renderers."""

from ..planning_catalog import PLANNING_VIEWS

TABS = [
    ("jobs", "Jobs"),
    ("cluster", "Cluster"),
    ("history", "History"),
    ("analytics", "Analytics"),
    ("nodes", "Nodes"),
    ("group", "Group"),
    ("deps", "Deps"),
    ("log", "Log"),
    ("sources", "Sources"),
    ("research", "Research"),
    ("traces", "Traces"),
]

ANALYTICS_VIEWS = [
    ("job", "job series"),
    ("history", "history"),
    ("timeline", "timeline"),
    ("advisor", "advisor"),
    ("compare", "compare"),
]

NODES_VIEWS = [("mine", "my nodes"), ("map", "cluster map")]

SORTS = {
    "jobs": ["state", "name", "id", "time", "priority"],
    "history": ["end", "name", "state", "elapsed", "cpu_eff", "mem_eff"],
    "nodes": ["name", "load"],
    "cluster": ["name"],
    "log": ["name"],
    "sources": ["name"],
    "group": ["user", "state", "name", "id", "time", "priority"],
    "deps": ["name"],
    "traces": ["name"],
}

RESEARCH_VIEWS = (
    [
        ("experiment", "Experiment"),
        ("arrays", "Arrays"),
        ("evidence", "Evidence"),
        ("artifacts", "Artifacts"),
        ("passport", "Passport"),
        ("submit", "Submit"),
    ]
    + PLANNING_VIEWS
    + [("operations", "Operations")]
)
