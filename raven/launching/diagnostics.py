"""Optional recording diagnostics never replace application execution."""

from __future__ import annotations
import sys
from .. import reporting as R
from .io import _lock


def _warn(run, text):
    try:
        print("Raven recording: " + text, file=sys.stderr)
    except OSError:
        pass
    try:
        with _lock(run):
            manifest = R._read_json(run / "run.json")
            warnings = (
                manifest.setdefault("metadata", {})
                .setdefault("automation", {})
                .setdefault("warnings", [])
            )
            # Keep the discoverable run inventory below its 64 KiB contract.
            # Complete diagnostics are also printed on the inherited stderr.
            if (
                sum(len(item.encode("utf-8")) for item in warnings)
                + len(text.encode("utf-8"))
                <= 8192
            ):
                warnings.append(text)
            elif (
                not warnings
                or warnings[-1]
                != "Further recording diagnostics are in the workload stderr (run inventory warning budget reached)."
            ):
                warnings.append(
                    "Further recording diagnostics are in the workload stderr (run inventory warning budget reached)."
                )
            R._atomic_json(run / "run.json", manifest, replace=True)
    except (OSError, ValueError):
        pass


def _diagnostic(run, message):
    """Recorder diagnostics must never prevent or replace application work."""
    try:
        if run is not None:
            _warn(run, message)
            return
    except Exception:
        pass
    try:
        print("Raven tracing: " + message, file=sys.stderr)
    except Exception:
        pass


def _stop_capture(capture, recorder, run):
    try:
        capture.stop()
    except Exception as exc:
        message = (
            "Optional trace shutdown failed; application outcome is preserved: "
            + str(exc)
        )
        try:
            if recorder is not None:
                recorder._fail("PARTIAL", message)
        except Exception:
            pass
        _diagnostic(run, message)
