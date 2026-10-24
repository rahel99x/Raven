"""Optional framework activity recording; no mandatory GPU dependency.

The import boundary and committed Raven journals have byte limits. PyTorch's
internal profiler buffers and temporary Chrome export are tool-dependent; this
adapter does not claim a hard bound on those resources or universal GPU capture.
"""

from __future__ import annotations

import importlib.util
from contextvars import ContextVar
import os
from pathlib import Path
import runpy
import socket
import sys
import tempfile
import threading
import uuid


_ACTIVE = ContextVar("raven_activity_recorder", default=None)


def step():
    """Advance an active wrapper's cooperative window, or harmlessly return False."""
    current = _ACTIVE.get()
    if current is None or current._owner != threading.get_ident():
        return False
    active = current._profiler is not None
    current.step()
    return active


def capabilities():
    """Read package prerequisites without importing Torch or probing a GPU."""
    try:
        present = importlib.util.find_spec("torch") is not None
    except (ImportError, ValueError):
        present = False
    return {
        "id": "torch-profiler-activity",
        "implemented": True,
        "status": "prerequisite-present" if present else "unavailable",
        "qualified": False,
        "scope": "PyTorch CPU operators and CUDA/ROCm activities when its profiler provides them; not arbitrary native/device call stacks",
        "live": "Cooperative step() exports scheduled windows; generic script wrapping exports at completion",
        "buffer_bound": "tool-dependent; output limits do not bound the framework's internal profiler buffer",
        "temporary_export_bound": "tool-dependent; byte budget is checked before retention/import",
    }


def _load_torch():
    import torch

    return torch


class TorchActivityRecorder:
    """Record optional activities and publish verified windows while a job runs.

    Call ``step()`` after each application step for scheduled live windows.
    ``active_steps=None`` deliberately records a single completion-time report.
    Recording errors are retained in ``receipt`` and leave application exception
    semantics unchanged unless ``required=True`` was explicitly selected.
    """

    def __init__(
        self,
        project,
        *,
        job_id,
        attempt,
        cluster="local",
        node=None,
        gpu=False,
        required=False,
        with_stack=False,
        active_steps=5,
        max_windows=16,
        max_bytes=32 << 20,
        max_total_bytes=256 << 20,
    ):
        if any(type(value) is not bool for value in (gpu, required, with_stack)):
            raise ValueError(
                "GPU, required and framework-stack recording flags must be boolean"
            )
        if active_steps is not None and (
            type(active_steps) is not int or not 1 <= active_steps <= 10_000
        ):
            raise ValueError(
                "Activity window must contain 1 through 10,000 explicit steps"
            )
        if type(max_windows) is not int or not 1 <= max_windows <= 128:
            raise ValueError("Activity recording supports 1 through 128 windows")
        if type(max_bytes) is not int or not 4096 <= max_bytes <= 128 << 20:
            raise ValueError("Activity input budget must be 4 KiB through 128 MiB")
        if type(max_total_bytes) is not int or not 8192 <= max_total_bytes <= 1 << 30:
            raise ValueError("Activity artifact budget must be 8 KiB through 1 GiB")
        from .protocol import text

        self.project = Path(project).absolute()
        if isinstance(job_id, bool) or not isinstance(job_id, (str, int)):
            raise ValueError("An explicit activity job ID is required")
        text(str(job_id), "job ID", 256)
        text(attempt, "attempt", 256)
        text(cluster, "cluster", 256)
        self.identity = dict(
            project=str(self.project),
            job_id=str(job_id),
            attempt=attempt,
            cluster=cluster,
            node=node or socket.gethostname(),
        )
        text(self.identity["node"], "source node", 256)
        self.gpu, self.required, self.with_stack = gpu, required, with_stack
        self.active_steps, self.max_windows = active_steps, max_windows
        self.max_bytes, self.max_total_bytes = max_bytes, max_total_bytes
        self._profiler = None
        self._temporary = None
        self._torch = None
        self._stop_requested = False
        self._closed = False
        self._finishing = False
        self._started = False
        self._stored_bytes = 0
        self._devices = []
        self._clock = None
        self._token = None
        self._owner = None
        self.receipt_path = None
        self.receipt = {
            "schema": "raven.activity-capture/v1",
            "adapter": "torch-profiler-activity",
            "status": "not-started",
            "reports": [],
            "errors": [],
            "steps": 0,
            "gpu_requested": gpu,
            "framework_python_stacks": with_stack,
            "stack_scope": "Optional framework-reported call sites; not native or device call-stack unwinding"
            if with_stack
            else "Operator/activity names only; use Raven Python recording for Python call boundaries",
            "required": required,
            "capture_qualified": False,
            "live": active_steps is not None,
            "window_steps": active_steps,
            "max_windows": max_windows,
            "buffer_bound": "tool-dependent; Raven does not bound PyTorch's internal activity buffer",
            "temporary_export_bound": "tool-dependent; byte budget is checked before retention/import",
            "artifact_budget_bytes": max_total_bytes,
            "scope": "Observed framework activities; application source and compute operations are not rewritten",
            "interpreter": sys.executable,
            "python_version": sys.version.split()[0],
            "identity": {
                key: value for key, value in self.identity.items() if key != "project"
            },
            "application_status": "not-started",
        }

    def _publish(self):
        """Keep capture feedback accessible even when the application exits."""
        try:
            from .capture import _trace_root, _safe_directory
            from ..reporting import _atomic_json

            if self.receipt_path is None:
                parent = _trace_root(self.project).parent / "activity-captures"
                parent.mkdir(mode=0o700, exist_ok=True)
                _safe_directory(parent)
                folder = parent / uuid.uuid4().hex
                folder.mkdir(mode=0o700)
                self.receipt_path = folder / "receipt.json"
                self.receipt["receipt"] = str(self.receipt_path)
            _atomic_json(self.receipt_path, self.receipt, replace=True)
        except (OSError, ValueError) as error:
            self._stop_requested = True
            self.receipt["status"] = "error"
            if len(self.receipt["errors"]) < 16:
                self.receipt["errors"].append(
                    "Capture receipt could not be saved: " + type(error).__name__
                )

    def _error(self, exception, *, status="error"):
        from ..job_logs.text import display_text

        message = display_text(type(exception).__name__ + ": " + str(exception))[:2000]
        self.receipt["status"] = status
        if len(self.receipt["errors"]) < 16:
            self.receipt["errors"].append(message)
        self._stop_requested = True

    def __enter__(self):
        if self._started or self._closed:
            raise RuntimeError("Activity recorder can be entered only once")
        self._started = True
        self._owner = threading.get_ident()
        self._publish()
        if self._stop_requested:
            if self.required:
                raise RuntimeError(
                    "Required activity capture could not create its receipt"
                )
            return self
        try:
            if _ACTIVE.get() is not None:
                raise RuntimeError(
                    "An existing Raven activity recorder is active; nested capture is unavailable"
                )
            self._torch = _load_torch()
            from .capture import _boot_id, _process_start

            self._clock = {
                "schema": "raven.trace-clock/v1",
                "kind": "producer-session",
                "id": "torch-profiler-relative-ns",
                "session_id": uuid.uuid4().hex,
                "producer": {
                    "node": socket.gethostname(),
                    "boot_id": _boot_id(),
                    "pid": os.getpid(),
                    "process_start": _process_start(),
                },
            }
            self.receipt["torch_version"] = str(
                getattr(self._torch, "__version__", "unknown")
            )
            active = getattr(
                getattr(self._torch, "autograd", None), "_profiler_enabled", None
            )
            if callable(active) and active():
                raise RuntimeError(
                    "An existing framework profiler is active; Raven will not replace it"
                )
            profiler = self._torch.profiler
            activities = [profiler.ProfilerActivity.CPU]
            if self.gpu:
                if not self._torch.cuda.is_available():
                    raise RuntimeError(
                        "Requested CUDA/ROCm activity capture is unavailable in this PyTorch execution environment"
                    )
                activities.append(profiler.ProfilerActivity.CUDA)
                vendor = (
                    "amd"
                    if getattr(self._torch.version, "hip", None)
                    else "nvidia"
                    if getattr(self._torch.version, "cuda", None)
                    else "unknown"
                )
                for ordinal in range(min(4096, self._torch.cuda.device_count())):
                    properties = self._torch.cuda.get_device_properties(ordinal)
                    record = {
                        "id": ordinal,
                        "name": str(properties.name),
                        "vendor": vendor,
                    }
                    device_uuid = getattr(properties, "uuid", None)
                    if device_uuid is not None:
                        record["uuid"] = str(device_uuid)
                    self._devices.append(record)
            options = dict(
                activities=activities,
                on_trace_ready=self._capture,
                record_shapes=False,
                profile_memory=False,
                # Python-stack replay has substantial provider/version-dependent
                # overhead and can fail when scheduled windows restart. Keep
                # that separate opt-in from useful CPU/device activity timing.
                with_stack=self.with_stack,
            )
            if self.active_steps is not None:
                options["schedule"] = profiler.schedule(
                    wait=0, warmup=0, active=self.active_steps, repeat=self.max_windows
                )
            self._temporary = tempfile.TemporaryDirectory(
                prefix="raven-framework-activities-"
            )
            candidate = profiler.profile(**options)
            candidate.__enter__()
            self._profiler = candidate
            self.receipt["status"] = "recording"
        except Exception as exc:
            self._error(exc, status="unavailable")
            if self._temporary is not None:
                self._temporary.cleanup()
                self._temporary = None
            if self.required:
                self._publish()
                raise RuntimeError(
                    "Required activity capture could not start: "
                    + self.receipt["errors"][-1]
                ) from exc
        self._publish()
        if self._stop_requested:
            self._finish_profiler(None, None, None)
            if self.required:
                raise RuntimeError(
                    "Required activity capture could not persist its startup receipt"
                )
        if self._profiler is not None:
            self._token = _ACTIVE.set(self)
        self.receipt["application_status"] = "running"
        return self

    def _capture(self, profiler):
        if self._stop_requested or len(self.receipt["reports"]) >= self.max_windows:
            return
        source = None
        raw = None
        try:
            if self._temporary is None:
                raise RuntimeError("Activity export has no owned temporary directory")
            source = Path(self._temporary.name) / (
                "window-" + str(len(self.receipt["reports"])) + ".json"
            )
            profiler.export_chrome_trace(str(source))
            # The producer's file is untrusted data, even when locally emitted.
            from . import protocol

            descriptor = protocol.regular_open(source)
            with os.fdopen(descriptor, "rb") as incoming:
                raw = incoming.read(self.max_bytes + 1)
            if len(raw) > self.max_bytes:
                raise ValueError(
                    "Framework activity export exceeds its input byte budget"
                )
            available = (
                self.max_total_bytes - self._stored_bytes - len(raw) - (64 << 10)
            )
            if available < 4096:
                raise ValueError(
                    "Framework activity recording reached its artifact budget"
                )
            from .activity_import import import_activity

            receipt = import_activity(
                source,
                **self.identity,
                max_bytes=self.max_bytes,
                max_output_bytes=min(1 << 30, available),
                device_metadata=self._devices or None,
                clock=self._clock,
            )
            self._clock = receipt["clock"]
            manifest_bytes = Path(receipt["manifest"]).stat().st_size
            if manifest_bytes > 64 << 10:
                raise ValueError(
                    "Framework activity manifest exceeds its reserved metadata budget"
                )
            # Keep the exact bounded input report beside its derived journal.
            destination = Path(receipt["manifest"]).parent / "producer-report.json"
            with destination.open("xb") as outgoing:
                outgoing.write(raw)
            destination.chmod(0o600)
            receipt["producer_report"] = str(destination)
            self._stored_bytes += len(raw) + receipt["journal_bytes"] + manifest_bytes
            self.receipt["reports"].append(receipt)
            self.receipt["stored_bytes"] = self._stored_bytes
            if not receipt["registered"]:
                self.receipt["status"] = "partial"
            if len(self.receipt["reports"]) == self.max_windows and not self._finishing:
                if self.receipt["status"] != "partial":
                    self.receipt["status"] = "limit"
                self.receipt["limit_reason"] = (
                    "The configured number of profiler windows was recorded; later application work is unobserved"
                )
                self._stop_requested = True
        except Exception as exc:
            self._error(exc)
            if (
                raw is not None
                and len(raw) <= self.max_bytes
                and self.receipt_path is not None
                and self._stored_bytes + len(raw) + (64 << 10) <= self.max_total_bytes
            ):
                try:
                    import hashlib

                    failed = self.receipt_path.parent / "failed-producer-report.json"
                    with failed.open("xb") as outgoing:
                        outgoing.write(raw)
                    failed.chmod(0o600)
                    self._stored_bytes += len(raw)
                    self.receipt.update(
                        failed_report=str(failed),
                        failed_report_sha256=hashlib.sha256(raw).hexdigest(),
                        stored_bytes=self._stored_bytes,
                    )
                except OSError as error:
                    self._error(error)
        finally:
            if source is not None:
                try:
                    source.unlink()
                except FileNotFoundError:
                    pass
                except OSError as error:
                    self._error(error)
            self._publish()

    def step(self):
        """Advance the explicitly cooperative live capture window."""
        if self._closed or not self._started:
            raise RuntimeError("Activity step requires an entered live recorder")
        if self._owner != threading.get_ident():
            raise RuntimeError("Activity step must run on its recorder's owner thread")
        self.receipt["steps"] += 1
        if self._profiler is not None:
            try:
                self._profiler.step()
            except Exception as exc:
                self._error(exc)
            if self._stop_requested:
                self._finish_profiler(None, None, None)

    def _finish_profiler(self, exc_type, exc, traceback):
        current, self._profiler = self._profiler, None
        if current is not None:
            try:
                current.__exit__(exc_type, exc, traceback)
            except Exception as error:
                self._error(error)

    def __exit__(self, exc_type, exc, traceback):
        if self._closed:
            return False
        self._finishing = True
        self._finish_profiler(exc_type, exc, traceback)
        if self._temporary is not None:
            try:
                self._temporary.cleanup()
            except OSError as error:
                self._error(error)
            self._temporary = None
        self._closed = True
        if self._token is not None:
            _ACTIVE.reset(self._token)
            self._token = None
        if self.receipt["status"] == "recording":
            self.receipt["status"] = "complete" if self.receipt["reports"] else "empty"
        failed = exc_type is not None and not (
            isinstance(exc, SystemExit) and exc.code in (None, 0)
        )
        self.receipt["application_status"] = "failed" if failed else "completed"
        self.receipt["application_exit_code"] = (
            exc.code
            if isinstance(exc, SystemExit) and type(exc.code) is int
            else 0
            if not failed
            else None
        )
        self.receipt["live_observed"] = (
            self.active_steps is not None
            and self.receipt["steps"] > 0
            and bool(self.receipt["reports"])
        )
        if self.receipt["status"] == "unavailable":
            self.receipt["live_reason"] = (
                "The requested capture adapter was unavailable"
            )
        elif self.active_steps is not None and not self.receipt["steps"]:
            self.receipt["live_reason"] = (
                "No cooperative step() calls were observed; only completion-time export was possible"
            )
        self._publish()
        if (
            self.required
            and self.receipt["status"] in {"error", "unavailable", "empty", "partial"}
            and not failed
        ):
            raise RuntimeError(
                "Required activity capture did not complete; inspect its receipt"
            )
        return False


def profile_script(
    script_argv,
    *,
    project,
    job_id,
    attempt,
    cluster="local",
    node=None,
    gpu=False,
    required=False,
    with_stack=False,
    max_bytes=32 << 20,
    active_steps=None,
    max_windows=16,
):
    """Run an existing Python script with optional completion-time activities.

    The script is not rewritten. argv/path are restored and original script
    failures and nonzero exits propagate. A successful SystemExit permits the
    wrapper to return its receipt. With active_steps, call the module's step()
    inside the application; this generic wrapper cannot invent step points.
    """
    script_argv = list(script_argv)
    if not script_argv:
        raise ValueError("Activity profile requires a Python script")
    target = Path(script_argv[0]).absolute()
    if not target.is_file():
        raise ValueError("Activity profile requires an existing Python script")
    recorder = TorchActivityRecorder(
        project,
        job_id=job_id,
        attempt=attempt,
        cluster=cluster,
        node=node,
        gpu=gpu,
        required=required,
        with_stack=with_stack,
        active_steps=active_steps,
        max_windows=max_windows if active_steps is not None else 1,
        max_bytes=max_bytes,
    )
    old_argv, old_path = sys.argv, list(sys.path)
    try:
        with recorder:
            if recorder.receipt_path is not None:
                print(
                    "Raven activity receipt: " + str(recorder.receipt_path),
                    file=sys.stderr,
                )
            sys.argv = [str(target), *script_argv[1:]]
            sys.path.insert(0, str(target.parent))
            try:
                runpy.run_path(str(target), run_name="__main__")
            except SystemExit as error:
                if error.code not in (None, 0):
                    raise
    finally:
        sys.argv, sys.path[:] = old_argv, old_path
    return recorder.receipt
