"""Recorded-event details with sample and scheduling fidelity kept explicit."""

from __future__ import annotations

from .timeline_ui import duration
from .workspace_ui_state import _text


def render(rows, selected, width, append):
    append(
        rows, "Selected: " + _text(selected.get("name", "unnamed")), "heading", width
    )
    if selected.get("selection_context"):
        append(rows, selected["selection_context"], "dim", width)
    kind = selected.get("kind", "event")
    value = selected.get("duration_ns")
    data = selected.get("data") or {}
    if kind == "cpu_sample":
        append(rows, "Stack sample; per-call duration is unknown", "text", width)
        weight = data.get("weight")
        if type(weight) in (int, float):
            append(
                rows,
                "Sampling weight: "
                + str(weight)
                + " "
                + _text(data.get("weight_unit", "samples"), 64)
                + "; not measured call elapsed time",
                "dim",
                width,
            )
        frames = data.get("frames")
        if isinstance(frames, list):
            append(rows, "Observed stack (root to leaf)", "heading", width)
            for depth, frame in enumerate(frames[:1024]):
                if isinstance(frame, dict):
                    label = _text(
                        frame.get("name") or frame.get("ip") or "unresolved frame"
                    )
                    if frame.get("dso"):
                        label += " / " + _text(frame["dso"])
                else:
                    label = _text(frame)
                append(rows, str(depth) + ": " + label, "text", width)
            if len(frames) > 1024:
                append(
                    rows,
                    "Inspector bounds the stack to 1,024 frames; the source record remains unchanged.",
                    "warning",
                    width,
                )
    elif kind == "collective":
        append(
            rows,
            "MPI collective "
            + _text(data.get("phase", "phase unknown"))
            + " observation; per-call duration unavailable",
            "text",
            width,
        )
        append(
            rows,
            "Operation "
            + _text(data.get("operation", selected.get("name", "unknown")))
            + " / rank "
            + _text(data.get("rank", "unknown"))
            + " / communicator "
            + _text(data.get("communicator", "unknown"))
            + " / generation "
            + _text(data.get("generation", "unknown"))
            + " / sequence "
            + _text(data.get("sequence", "unknown")),
            "dim",
            width,
        )
        append(
            rows,
            "Recorded point: "
            + str(selected.get("start_ns", "unknown"))
            + " ns; collective dependencies do not establish cross-node elapsed time.",
            "dim",
            width,
        )
    else:
        label = (
            "query bracket"
            if kind == "cpu_observation"
            else "observed on-CPU slice"
            if kind == "cpu_stack_slice"
            else "device activity"
            if str(kind).startswith("gpu_")
            else "recorded elapsed"
        )
        append(
            rows,
            _text(kind, 128)
            + " / "
            + (
                duration(value) + " " + label
                if type(value) is int
                else "duration unavailable"
            )
            + " / thread "
            + _text(selected.get("thread", "?"), 128),
            "text",
            width,
        )
        if kind in {"python", "host"} and type(selected.get("self_ns")) is int:
            append(
                rows,
                duration(selected["self_ns"])
                + " self elapsed; valid synchronous child intervals excluded",
                "dim",
                width,
            )
    append(
        rows,
        "Source "
        + _text(selected.get("source_id", "unknown"), 256)
        + " / clock "
        + _text(selected.get("clock_id", "unknown"), 256),
        "dim",
        width,
    )
    if selected.get("file"):
        append(
            rows,
            _text(selected["file"])
            + (":" + str(selected["line"]) if selected.get("line") is not None else ""),
            "text",
            width,
        )
    if selected.get("fidelity"):
        append(rows, "Fidelity: " + _text(selected["fidelity"]), "dim", width)
    if type(selected.get("alignment_uncertainty_ns")) is int:
        append(
            rows,
            "Calibration uncertainty: ±"
            + duration(selected["alignment_uncertainty_ns"])
            + " / "
            + _text(selected.get("alignment_provenance", "producer-declared")),
            "warning",
            width,
        )
        append(
            rows,
            "Original clock "
            + _text(selected.get("raw_clock_id", "unknown"))
            + " / "
            + str(selected.get("raw_start_ns", "?"))
            + " - "
            + str(selected.get("raw_end_ns", "?"))
            + " ns; calibration does not prove precise cross-host order.",
            "dim",
            width,
        )
