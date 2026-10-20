"""Shared export/advanced-launch dialog state and feedback without peer forms."""

from __future__ import annotations

FAMILIES = (
    ("application", "Application metrics"),
    ("gpu", "GPU measurements"),
    ("cpu-memory", "CPU and memory"),
    ("summary", "Run summary"),
    ("research", "Research reports"),
    ("trace", "Trace evidence"),
    ("logs", "Logs"),
    ("artifacts", "Declared artifacts"),
)
DEFAULT_FAMILIES = tuple(key for key, _ in FAMILIES[:6])


def initialize(app):
    state = getattr(app, "data_export_state", None)
    if not isinstance(state, dict):
        state = dict(
            generation=0,
            revision=0,
            stage="",
            return_mode="main",
            cursor=0,
            top=0,
            visible=1,
            follow=True,
            control_hits=[],
            painted=None,
            pending=None,
            running=False,
            cancel=None,
            progress={},
            frozen=(),
            attempts=(),
            discovery=None,
            plan=None,
            receipt=None,
            error="",
            message="",
            editing=None,
            include=list(DEFAULT_FAMILIES),
            metrics=[],
            devices=[],
            sources=[],
            layout="category",
            range_policy="full",
            chart_range=None,
            start="",
            end="",
            destination=dict(mode="project", path="", host=""),
            destination_blocked=False,
            settings={},
            presets={},
            preset="",
            preset_name="Paper figures",
            auto=False,
            future_auto=False,
            remote=False,
            cache={},
        )
        app.data_export_state = state
    return state


def _invalidate(state, *, content=True):
    state["revision"] += 1
    if content:
        state["content_revision"] = state.get("content_revision", 0) + 1
    state.update(control_hits=[], painted=None)


def _feedback(app, message, error=False):
    callback = getattr(app, "fail" if error else "say", None)
    if callable(callback):
        callback(str(message))


def _failure(app, error):
    state = initialize(app)
    state.update(error=str(error), stage=state.get("return_stage", "form"), message="")
    _invalidate(state)
    # App.fail feeds the retained notification center; no renderer truncation.
    _feedback(
        app,
        (
            "Launch settings: "
            if getattr(app, "mode", "") == "launch_setup"
            else "Export data: "
        )
        + str(error),
        True,
    )


def _toggle(values, value):
    return (
        [item for item in values if item != value]
        if value in values
        else values + [value]
    )
