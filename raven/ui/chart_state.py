"""Shared published chart identity, geometry queries and transient state.

No feature renderer, navigation handler or source transport is imported here.
"""

from __future__ import annotations
from dataclasses import dataclass
import math
from .geometry import plot_state as initialize

MAX_KEY_PARTS = 16
MAX_KEY_TEXT = 512


@dataclass(frozen=True)
class ZoomIdentity:
    """View-local display state without altering the shared source identity."""

    source: tuple
    pane: str


def _display_scope(app):
    explicit = getattr(app, "pane_identity", None)
    if isinstance(explicit, str) and explicit:
        return explicit
    mode, tab = getattr(app, "mode", "main"), getattr(app, "tab", "")
    if mode == "analysis":
        return "analysis:" + str(_state(app, "analysis_state").get("modal", ""))
    if tab in ("jobs", "history"):
        panel = _state(app, "job_panel_state")
        view = panel.get("mode", "inspector")
        if view in ("analytics", "research"):
            view += ":" + str(
                panel.get(
                    view + "_view", "job" if view == "analytics" else "experiment"
                )
            )
        return tab + ":details:" + view
    if tab == "research":
        return "research:" + str(getattr(app, "research_view", "experiment"))
    # Keep the existing primary Analytics key format for source consumers.
    return None


def _zoom_identity(identity, pane):
    return ZoomIdentity(identity, pane) if pane is not None else identity


def clear_source_zoom(app, identity, canonical):
    """A Live toggle clears boxes only in this presentation of the source."""
    state = initialize(app)
    pane = _display_scope(app)
    removed = [
        key
        for key in state["zoom"]
        if (key.pane if isinstance(key, ZoomIdentity) else None) == pane
        and canonical(key.source if isinstance(key, ZoomIdentity) else key) == identity
    ]
    for key in removed:
        state["zoom"].pop(key, None)
    if removed:
        state["revision"] += 1
    return bool(removed)


def _key(key):
    if isinstance(key, str):
        key = (key,)
    if not isinstance(key, tuple) or not 0 < len(key) <= MAX_KEY_PARTS:
        return None
    if any(
        not isinstance(value, (str, int, float, bool, type(None)))
        or isinstance(value, str)
        and (len(value) > MAX_KEY_TEXT or value and not value.isprintable())
        or isinstance(value, float)
        and not math.isfinite(value)
        for value in key
    ):
        return None
    return key


def key(app, metric, source, jid=None, *, scope="metric", attempt=None):
    """Identify the actual source and attempt, independent of fresh snapshots."""
    research = getattr(app, "research", None)
    project = getattr(app, "project_state", {})
    project = project if isinstance(project, dict) else {}
    binding = project.get("binding", {})
    binding = (
        binding
        if isinstance(binding, dict)
        and str(binding.get("job_id", "")) == str(jid or "")
        else {}
    )
    native = (
        isinstance(scope, str)
        and scope.startswith("resource-")
        or source == "Raven session resource samples"
        and isinstance(attempt, str)
        and attempt.startswith("scheduler:")
    )
    identity = (
        scope,
        str(jid or ""),
        str(metric),
        str(source),
        attempt if attempt is not None else None if native else binding.get("attempt"),
        None if native else binding.get("project_root") or project.get("root"),
        None if native else binding.get("run_id"),
        None if native else getattr(research, "generation", None),
    )
    # Do not truncate source paths into accidental identity collisions.
    return _key(identity)


def _state(app, name):
    value = getattr(app, name, {})
    return value if isinstance(value, dict) else {}


def _context(app):
    analysis = _state(app, "analysis_state")
    panel = _state(app, "job_panel_state")
    project = _state(app, "project_state")
    toolbar = _state(app, "toolbar_state")
    binding = project.get("binding", {})
    binding = binding if isinstance(binding, dict) else {}
    return (
        getattr(app, "mode", "main"),
        getattr(app, "tab", ""),
        getattr(app, "width", None),
        getattr(app, "height", None),
        getattr(app, "selected_id", None),
        getattr(app, "analytics_job", None),
        getattr(app, "research_job_id", None),
        getattr(app, "research_view", None),
        getattr(app, "analytics_view", None),
        analysis.get("modal"),
        analysis.get("chart_job"),
        analysis.get("metric"),
        panel.get("mode"),
        panel.get("research_view"),
        panel.get("analytics_view"),
        project.get("root"),
        binding.get("run_id"),
        binding.get("job_id"),
        binding.get("attempt"),
        getattr(getattr(app, "research", None), "generation", None),
        toolbar.get("menu"),
        toolbar.get("panel"),
    )


def _native(identity):
    return bool(
        identity
        and isinstance(identity[0], str)
        and (
            identity[0].startswith("resource-")
            or len(identity) > 4
            and identity[0] == "reported-metric"
            and identity[3] == "Raven session resource samples"
            and isinstance(identity[4], str)
            and identity[4].startswith("scheduler:")
        )
    )


def _capture_context(app, identity):
    """Track the displayed source rather than unrelated project readers."""
    app = getattr(app, "_chart_owner", app)
    if not _native(identity):
        return _context(app)
    analysis, panel, toolbar = (
        _state(app, name)
        for name in ("analysis_state", "job_panel_state", "toolbar_state")
    )
    mode, tab = getattr(app, "mode", "main"), getattr(app, "tab", "")
    selected = (
        getattr(app, "selected_id", None)
        if tab in ("jobs", "history")
        else getattr(app, "analytics_job", None)
        if tab == "analytics"
        else None
    )
    return (
        mode,
        tab,
        getattr(app, "width", None),
        getattr(app, "height", None),
        selected,
        getattr(app, "analytics_view", None) if tab == "analytics" else None,
        panel.get("mode") if tab in ("jobs", "history") else None,
        panel.get("analytics_view") if tab in ("jobs", "history") else None,
        panel.get("research_view") if tab in ("jobs", "history") else None,
        analysis.get("modal") if mode == "analysis" else None,
        analysis.get("chart_job") if mode == "analysis" else None,
        analysis.get("metric") if mode == "analysis" else None,
        toolbar.get("menu"),
        toolbar.get("panel"),
    )


def _blocked(app):
    startup = getattr(app, "startup_state", {})
    if isinstance(startup, dict) and startup.get("running"):
        from ..startup import active

        if active(app):
            return True
    toolbar = _state(app, "toolbar_state")
    if toolbar.get("menu") is not None or toolbar.get("panel"):
        return True
    mode = getattr(app, "mode", "main")
    analysis = _state(app, "analysis_state")
    return mode != "main" and not (
        mode == "analysis" and analysis.get("modal") in ("chart", "diff")
    )


def _at(app, y, x, *, allow_empty=False):
    state = initialize(app)
    if any(
        not isinstance(value, int) or isinstance(value, bool) for value in (y, x)
    ) or _blocked(app):
        return None
    return next(
        (
            plot
            for plot in reversed(state["plots"])
            if plot.visible.contains(y, x)
            and (allow_empty or plot.kind != "metric-empty")
            and (
                plot.context == _capture_context(app, plot.key)
                if plot.context is not None
                else state["frame_context"] == _context(app)
            )
        ),
        None,
    )


def cancel(app):
    """Discard a preview and consume its delayed release.

    Zoom changes only on a valid release within the captured plot's bounded
    tolerance. Its coordinates are clamped to the actual visible data cells.
    """
    state = initialize(app)
    was_active = state["capture"] is not None
    state["capture"] = None
    state["visual"] = None
    if was_active:
        state["cancelled_release"] = True
    return was_active
