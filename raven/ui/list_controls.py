"""Three bounded job-list menu controls; no command-handler dependencies."""

from __future__ import annotations
from .list_preferences import mode, batch_mode


def controls(app, g, width, context):
    from ..control_rows import buttons
    from ..layout import vlen

    view = mode(app, context).title()
    order = {"min": "Fewest running first", "max": "Most running first"}.get(
        batch_mode(app, context), "Column order"
    )
    # Three bounded menus replace nine competing actions. At narrow split-pane
    # widths the full current value stays available in the semantic label/menu.
    labels = [
        ("view", "View: " + view),
        ("order", "Order: " + order),
        ("groups", "Groups"),
    ]
    if sum(vlen(label) + 2 for _, label in labels) + 2 > width:
        labels = [("view", "View: " + view), ("order", "Order"), ("groups", "Groups")]
    if sum(vlen(label) + 2 for _, label in labels) + 2 > width:
        labels = [("view", "View"), ("order", "Order"), ("groups", "Groups")]
    rows, hits = buttons(
        g,
        width,
        [
            (key, label, ("command", f"joblistmenu {context} {key}"))
            for key, label in labels
        ],
        group="job-list-controls",
        prefix=f"joblist:{context}:",
        gap=0,
    )
    full = {
        "view": "View: " + view + "; change presentation without removing assignments",
        "order": "Order: " + order + "; batch order applies in Grouped view",
        "groups": "Groups: expand, collapse, or manage shared assignments",
    }
    for _, _, value in hits:
        value["label"] = full[value["id"].rsplit(":", 1)[-1]]
    return rows, hits
