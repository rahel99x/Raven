"""Versioned, bounded Raven worker messages. No executable payloads or pickle."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import sys

from ..framing import Decoder as FramedDecoder, FramePolicy, frame

MAJOR = 1
MINOR = 0
MAX_FRAME = 1 << 20
MAX_QUEUE = 8 << 20
# A task's validated execution payload also traverses worker and allocation
# envelopes. One table keeps those relays at least as expressive as dispatch.
MAX_STRING = MAX_FRAME
MAX_DEPTH = 40
MAX_RECORDS = 110000
PAYLOAD_DEPTH = 32
PAYLOAD_RECORDS = 100000


class ProtocolError(ValueError):
    pass


def worker_argv():
    """Launch this installed/source package independently of the caller cwd."""
    return [
        sys.executable,
        "-c",
        "import runpy,sys;sys.path.insert(0,sys.argv.pop(1));runpy.run_module('raven.raven_worker',run_name='__main__')",
        str(Path(__file__).resolve().parent.parent.parent),
    ]


def validate(value):
    count = 0

    def visit(item, depth):
        nonlocal count
        count += 1
        if count > MAX_RECORDS or depth > MAX_DEPTH:
            raise ProtocolError("Worker document exceeds record or nesting budget")
        if item is None or isinstance(item, bool):
            return
        if isinstance(item, str):
            if len(item) > MAX_STRING or len(item.encode("utf-8")) > MAX_STRING:
                raise ProtocolError("Worker string exceeds its byte budget")
        elif isinstance(item, int):
            if not -(1 << 63) <= item < (1 << 63):
                raise ProtocolError("Worker integer exceeds signed 64-bit range")
        elif isinstance(item, float):
            if not math.isfinite(item):
                raise ProtocolError("Worker numbers must be finite")
        elif isinstance(item, list):
            for child in item:
                visit(child, depth + 1)
        elif isinstance(item, dict):
            for key, child in item.items():
                if not isinstance(key, str) or len(key) > 128:
                    raise ProtocolError("Worker object keys must be short strings")
                key.encode("utf-8")  # Reject surrogate keys before wire encoding.
                visit(child, depth + 1)
        else:
            raise ProtocolError("Worker messages contain only JSON data")

    try:
        visit(value, 0)
    except (UnicodeError, RecursionError) as exc:
        raise ProtocolError("Invalid worker text or nesting") from exc
    return value


def canonical(value):
    validate(value)
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


POLICY = FramePolicy(
    "Worker", MAX_FRAME, MAX_QUEUE, ProtocolError, eager_header=True, bytes_only=True
)


def encode(value):
    if not isinstance(value, dict):
        raise ProtocolError("Worker frame must contain an object")
    return frame(canonical(value), POLICY)


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ProtocolError("Duplicate worker object key")
        result[key] = value
    return result


def _parse(raw):
    try:
        value = json.loads(
            raw,
            object_pairs_hook=_pairs,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ProtocolError("Nonfinite worker number")
            ),
        )
        return validate(value)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ProtocolError("Invalid worker JSON frame") from exc


class Decoder(FramedDecoder):
    def __init__(self):
        super().__init__(POLICY, _parse)

    def take(self, count=32):
        return super().take(count)


def build_digest():
    """Digest only the fixed worker implementation, never user paths or secrets."""
    root = Path(__file__).resolve().parent.parent
    state = hashlib.sha256()
    for name in (
        "__init__.py",
        "framing.py",
        "transport/__init__.py",
        "transport/framing.py",
        "protocol_contracts.py",
        "raven_protocol.py",
        "raven_worker.py",
        "raven_fork_task.py",
        "raven_resources.py",
        "raven_execution.py",
        "execution/__init__.py",
        "execution/admission.py",
        "execution/log_export.py",
        "execution/planning.py",
        "execution/policy.py",
        "execution/registry.py",
        "execution/worker.py",
        "execution/capabilities.py",
        "execution/scope.py",
        "source_budget.py",
        "execution_policy.py",
        "execution_admission.py",
        "operation_json.py",
        "operation_support.py",
        "operations.py",
        "action_lifecycle.py",
        "operation_adapters/__init__.py",
        "operation_adapters/campaign_policy.py",
        "operation_adapters/campaigns.py",
        "operation_adapters/cluster.py",
        "operation_adapters/evidence_queries.py",
        "operation_adapters/health.py",
        "operation_adapters/investigation.py",
        "operation_adapters/platform.py",
        "operation_adapters/process_profile.py",
        "operation_adapters/queue_models.py",
        "operation_adapters/queue_order.py",
        "operation_adapters/runtime.py",
        "operation_adapters/scale.py",
        "operation_adapters/science.py",
        "operation_adapters/services.py",
        "operation_adapters/signal_analysis.py",
        "operation_adapters/trace.py",
        "operation_adapters/workflow.py",
        "operation_adapters/workspace.py",
        "tracing/__init__.py",
        "tracing/protocol.py",
        "tracing/capture.py",
        "tracing/analysis.py",
        "bounded_command.py",
        "raven_config.py",
        "configuration/__init__.py",
        "configuration/dashboard.py",
        "configuration/policy.py",
        "configuration/allocation.py",
        "configuration/profiles.py",
        "distribution_profile.py",
        "_distribution_profile.json",
        "heavy/__init__.py",
        "heavy/allocation.py",
        "heavy/allocation_adapters.py",
        "heavy/allocation_exec.py",
        "heavy/allocation_helper.py",
        "heavy/allocation_identity.py",
        "heavy/allocation_ownership.py",
        "heavy/allocation_recovery.py",
        "heavy/allocation_reuse.py",
        "heavy/worker.py",
        "heavy/worker_client.py",
        "heavy/protocol.py",
        "heavy/resources.py",
        "heavy/fork_task.py",
    ):
        path = root / name
        state.update(name.encode() + b"\0")
        if path.is_file():
            with path.open("rb") as source:
                data = source.read(2 << 20)
                if source.read(1):
                    raise ProtocolError(
                        "Worker module exceeds the build manifest budget"
                    )
            state.update(data)
        else:
            state.update(b"unavailable")
    return state.hexdigest()
