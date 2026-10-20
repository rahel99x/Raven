"""Existing shared research/background service ownership for application clients."""

from __future__ import annotations

from .research import ResearchHub


def research_hub(app, files=None):
    """Reuse the application's existing bounded worker and lifecycle owner."""
    if getattr(app, "research", None) is None:
        app.research = ResearchHub(app.cfg, files)
    return app.research
