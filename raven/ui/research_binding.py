"""UI composition for explicit research selection and run binding changes."""

from __future__ import annotations

from ..research_context import InteractionCallbacks


def select_job(app, jid, *, view=None, record_back=True):
    """Select an explicit job without retaining a different project's sources."""
    from ..project_ui import selected_binding, clear_binding

    binding = selected_binding(app)
    changed = getattr(app, "research_job_id", None) != jid
    mismatch = binding is not None and binding.get("job_id") != jid
    if record_back and (changed or mismatch):
        from ..navigation_ui import record

        record(app, "research", force=True)
    if mismatch:
        # Restore the pre-project settings before assigning the new identity.
        clear_binding(app)
    app.research_job_id = jid
    from ..job_selection import resume

    resume(app, "research")
    if getattr(app, "tab", "") == "research":
        app.selected_id = jid
    if view is not None:
        app.research_view = view
    app.research_scroll = 0


def detach_manual_source(app):
    """Explicit file attachment leaves run binding before configuring its path."""
    from ..project_ui import selected_binding, clear_binding

    if selected_binding(app) is not None:
        from ..navigation_ui import record

        record(app, "research", force=True)
    clear_binding(app, suppress_auto=True)


INTERACTION = InteractionCallbacks(select_job, detach_manual_source)
