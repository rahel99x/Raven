"""Canonical UI state leaves remain importable below feature composition."""

import json
import subprocess
import sys
from pathlib import Path


def test_query_leaves_do_not_import_feature_roots():
    code = """
import importlib, json, sys
for name in (
    'series_presentation', 'table_query', 'ui.table_schema',
    'ui.control_graph', 'ui.interaction_state', 'ui.selection_state',
    'ui.panel_state', 'ui.history_state', 'ui.workspace_state',
    'ui.notification_state', 'job_group_domain', 'job_group_membership',
):
    importlib.import_module('raven.' + name)
forbidden = {
    'raven.views', 'raven.interaction', 'raven.job_selection',
    'raven.job_panels', 'raven.history_browser', 'raven.workspace_layout',
    'raven.notification_ui', 'raven.table_ui', 'raven.table_tools',
    'raven.job_groups', 'raven.manual_job_groups',
}
print(json.dumps(sorted(forbidden.intersection(sys.modules))))
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).resolve().parents[1],
        text=True,
        capture_output=True,
        check=True,
        timeout=10,
    )
    assert json.loads(result.stdout) == []


def test_compatibility_exports_retain_canonical_state_identity():
    from raven import interaction, job_selection, views, table_ui, table_tools
    from raven import history_browser, job_panels, workspace_layout, notification_ui
    from raven import series_presentation, table_query
    from raven.ui import control_graph, interaction_state, selection_state, table_schema
    from raven.ui import panel_state, history_state, workspace_state, notification_state

    assert interaction.Control is control_graph.Control
    assert interaction.Graph is control_graph.Graph
    assert interaction.nearest is control_graph.nearest
    assert interaction._current is interaction_state._current
    assert job_selection.initialize is selection_state.initialize
    assert job_selection.context is selection_state.context
    assert job_selection.publish is selection_state.publish
    assert views.prepared_series is series_presentation.prepared_series
    assert views.normalized_series is series_presentation.normalized_series
    assert views.JOB_COLS is table_schema.JOB_COLS
    assert views.Views.FIN_COLS is table_schema.FIN_COLS
    assert views.Views.GROUP_COLS is table_schema.GROUP_COLS
    assert table_ui.fingerprint is table_query.fingerprint
    assert table_tools.numeric_matches is table_query.numeric_matches
    assert job_panels.contains is panel_state.contains
    assert history_browser._current is history_state._current
    assert history_browser.Rect is history_state.Rect
    assert workspace_layout.LayoutState is workspace_state.LayoutState
    assert notification_ui.context is notification_state.context
