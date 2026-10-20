"""Confined project reads on the selected SSH host, using only its Python stdlib.

The helper caches a content-addressed reader bundle in a private remote
temporary directory. It imports no project code and exposes only explicit
read operations, keeping local and SSH path/identity validation identical.
"""
from __future__ import annotations

import ast
import base64
from functools import lru_cache
import json
from pathlib import Path
import hashlib
import secrets
import lzma

from .remote_files import RemoteFiles

_MODULES = ("projects", "artifacts", "remote", "slurm", "model", "clock", "history_snapshots", "ui_state",
            "series_archive", "series_publication", "series_cache_limits", "predict", "planning_io", "log_catalog", "provenance", "array_manifest",
            "artifact_pages", "log_presentation", "retained_telemetry_io",
            "series_identity", "state_ownership", "config_trust", "transport_errors",
            "log_text", "log_paths", "user_identity", "report_lock", "source_budget",
            "local_files", "remote_files", "project_files")
_OPERATIONS = {"discover_project", "discover_job", "select_run", "read_bound_json",
               "read_bound_passport", "read_bound_artifacts", "read_bound_tail",
               "read_bound_metric_chunk", "read_bound_telemetry", "read_passport", "artifact_tree", "preview_artifact", "read_artifact_page"}
MAX_REPLY = 96 * 1024 * 1024

_BOOTSTRAP = r'''
import base64, importlib.abc, importlib.util, json, sys, lzma
decoder = lzma.LZMADecompressor(memlimit=32 * 1024 * 1024)
source_bytes = decoder.decompress(base64.b64decode("".join(sys.argv[2:]), validate=True), max_length=2 * 1024 * 1024 + 1)
if len(source_bytes) > 2 * 1024 * 1024 or not decoder.eof or decoder.unused_data:
    raise ValueError("Invalid or oversized project reader bundle")
sources = json.loads(source_bytes)
class ReaderLoader(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in sources:
            return importlib.util.spec_from_loader(fullname, self, is_package=fullname == "raven")
    def create_module(self, spec):
        return None
    def exec_module(self, module):
        exec(compile(sources[module.__name__], "<raven-project-reader>", "exec"), module.__dict__)
sys.meta_path.insert(0, ReaderLoader())
from raven import projects
try:
    request = json.loads(sys.argv[1])
    allowed = {"discover_project", "discover_job", "select_run", "read_bound_json",
               "read_bound_passport", "read_bound_artifacts", "read_bound_tail",
               "read_bound_metric_chunk", "read_bound_telemetry", "read_passport", "artifact_tree", "preview_artifact", "read_artifact_page"}
    if request["operation"] not in allowed:
        raise ValueError("unsupported project reader operation")
    value = getattr(projects, request["operation"])(*request["args"], **request["kwargs"])
    selected = value.get("selected") if request["operation"] == "discover_job" else value if request["operation"] == "select_run" else None
    if selected:
        selected.pop("array_manifest_record", None)
    if request["operation"] == "read_bound_tail":
        value = [base64.b64encode(value[0]).decode("ascii"), value[1], value[2]]
    response = {"value": value}
except (OSError, ValueError, TypeError, KeyError, RecursionError) as exc:
    response = {"error": str(exc)[:2048], "missing": isinstance(exc, FileNotFoundError)}
print(json.dumps(response, ensure_ascii=True, allow_nan=False, separators=(",", ":")))
'''


def supported(files):
    delegate = getattr(files, "project_delegate", None)
    return type(files) is RemoteFiles or type(delegate) is RemoteFiles


def identity(files):
    """An in-process connection identity; reconnecting requires a fresh binding."""
    delegate = getattr(files, "project_delegate", None)
    if type(delegate) is RemoteFiles:
        return identity(delegate)
    if not supported(files):
        raise ValueError("This file adapter cannot prove confined SSH project reads")
    nonce = getattr(files, "_project_reader_nonce", None)
    if nonce is None:
        nonce = secrets.token_hex(16)
        files._project_reader_nonce = nonce
    ssh = files.ssh
    scope = (getattr(ssh, "target", None), getattr(ssh, "host", None), getattr(ssh, "user", None), tuple(getattr(ssh, "opts", ())))
    cached = getattr(files, "_project_reader_scope", None)
    if not cached or cached[0] != scope:
        fingerprint = hashlib.sha256(repr(scope).encode("utf-8")).hexdigest()[:16]
        files._project_reader_scope = (scope, fingerprint)
    return nonce + ":" + files._project_reader_scope[1]


@lru_cache(maxsize=1)
def _payload():
    root = Path(__file__).parent
    sources = {"raven": ""}
    pending = list(_MODULES)
    while pending:
        name = pending.pop()
        if "raven." + name in sources:
            continue
        source = (root / (name + ".py")).read_text(encoding="utf-8")
        sources["raven." + name] = source
        # Keep module-level reader dependencies synchronized with extracted
        # pure ports without shipping the UI's lazy import graph.
        nodes = list(ast.parse(source).body)
        while nodes:
            node = nodes.pop()
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            if isinstance(node, ast.ImportFrom) and node.level == 1:
                children = [node.module.split(".")[0]] if node.module else [item.name for item in node.names]
                pending.extend(child for child in children if (root / (child + ".py")).is_file())
            nodes.extend(ast.iter_child_nodes(node))
    data = json.dumps(sources, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    if len(data) > 2 * 1024 * 1024:
        raise ValueError("project reader source exceeds its transfer budget")
    # Source is compressed once per local process. XZ keeps the transitive
    # reader modules below SSH's single remote-command argument limit; splitting
    # argv locally cannot evade that limit. The remote decoder is bounded.
    packed = base64.b64encode(lzma.compress(data, preset=6)).decode("ascii")
    if len(packed) > 512 * 1024:
        raise ValueError("project reader bundle exceeds its transfer budget")
    return tuple(packed[i:i + 24576] for i in range(0, len(packed), 24576))


def request(files, operation, *args, **kwargs):
    if not supported(files) or operation not in _OPERATIONS:
        raise ValueError("unsupported SSH project reader")
    request_identity = identity(files)
    ssh = files.ssh
    arguments = list(args)
    if arguments and isinstance(arguments[0], dict) and "run_root" in arguments[0]:
        binding = dict(arguments[0])
        if binding.pop("remote_identity", None) != request_identity:
            raise ValueError("project binding belongs to another connection; refresh its job first")
        # UI discovery summaries are not authority for a confined source read.
        # Keep them off the SSH command; the helper reopens current declarations.
        binding.pop("scientific_query", None)
        binding.pop("telemetry_sources", None)
        arguments[0] = binding
    encoded = json.dumps({"operation": operation, "args": arguments, "kwargs": kwargs},
                         ensure_ascii=True, allow_nan=False, separators=(",", ":"))
    if len(encoded) > 65536:
        raise ValueError("project reader request exceeds its metadata budget")
    payload = _payload()
    digest = hashlib.sha256("".join(payload).encode("ascii")).hexdigest()
    from .project_wire import command
    cached = getattr(files, "_project_reader_bundle", None)
    root = cached[2] if cached and cached[:2] == (request_identity, digest) else ""
    try:
        runner = getattr(files, "project_command", ssh.run)
        output, _ = runner(command("read", digest, root, encoded, len(payload)), files.timeout)
        # A cache miss carries no project data and executes no uploaded code.
        # Publish bounded, idempotent chunks, then retry the original request.
        missing = json.loads(output).get("raven_cache_missing") if isinstance(output, str) and len(output) < 8192 else None
        if missing:
            if not isinstance(missing, str) or len(missing) > 4096:
                raise ValueError("invalid remote reader cache root")
            for index, chunk in enumerate(payload):
                runner(command("upload", digest, missing, str(index) + ":" + chunk, len(payload)), files.timeout)
            output, _ = runner(command("read", digest, missing, encoded, len(payload)), files.timeout)
            files._project_reader_bundle = (request_identity, digest, missing)
    except Exception as exc:
        from .raven_collection import Deferred
        if isinstance(exc, Deferred):
            # Admission deferral is a retry instruction, not a failed SSH
            # read. Preserve its message and deadline for the sampler.
            raise
        raise OSError("SSH project reader: " + str(exc)[:512]) from exc
    if files.ssh is not ssh or identity(files) != request_identity:
        raise ValueError("SSH project connection changed during its read; refresh this job")
    if not isinstance(output, str) or len(output) > MAX_REPLY:
        raise ValueError("SSH project reader returned an oversized result")
    from .artifacts import _json
    reply = _json(output.encode("utf-8"))
    if not isinstance(reply, dict):
        raise ValueError("SSH project reader returned an invalid result")
    if "error" in reply:
        raise (FileNotFoundError if reply.get("missing") else ValueError)(str(reply["error"])[:2048])
    value = reply["value"]
    selected = value.get("selected") if operation == "discover_job" else value if operation == "select_run" else None
    if selected:
        selected["binding"]["remote_identity"] = request_identity
        if selected.get("array_manifest_document"):
            from .array_manifest import parse
            selected["array_manifest_record"] = parse(json.dumps(selected["array_manifest_document"], separators=(",", ":")))
    if operation == "read_bound_tail":
        value[0] = base64.b64decode(value[0], validate=True)
    return value
