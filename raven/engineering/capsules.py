"""Small reproducible execution packages built from existing run evidence.

Datasets and ambient environment variables are never discovered or copied.
An immutable payload, explicit environment/module/seed declarations, and input
versions travel together. Capture-time Git state is not execution-time proof.
"""

from __future__ import annotations

import copy
import hashlib
import math
import os
from pathlib import Path
import re
import shlex
import time
import uuid

from .. import bounded_command, projects, run_home, science_files
from ..slurm import Backend, CommandError
from . import _storage as S

SCHEMA = "raven.execution-capsule/v1"
MAX_SCRIPT = 1 << 20
MAX_DIFF = 1 << 20
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_SECRET = re.compile(r"(?:TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|PRIVATE|AUTH|COOKIE|API_KEY)", re.I)
_FIELDS = {"schema", "created", "run_identity", "run", "script", "resources", "effective_options", "option_sources",
           "scheduler_options", "execution", "arguments", "parameters", "execution_provenance", "git", "environment",
           "modules", "seeds", "inputs", "declared", "warnings", "files", "integrity"}


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _text(value, label, maximum=4096, *, empty=False):
    if (not isinstance(value, str) or not (0 if empty else 1) <= len(value) <= maximum
            or any(not char.isprintable() for char in value)):
        raise ValueError(label + " must be bounded printable text")
    return value


def _declarations(environment, modules, seeds, inputs):
    environment = {} if environment is None else copy.deepcopy(environment)
    modules = [] if modules is None else copy.deepcopy(modules)
    seeds = {} if seeds is None else copy.deepcopy(seeds)
    inputs = [] if inputs is None else copy.deepcopy(inputs)
    if not isinstance(environment, dict) or len(environment) > 128:
        raise ValueError("Declare at most 128 environment variables explicitly")
    for key, value in environment.items():
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", key) or _SECRET.search(key):
            raise ValueError("Environment names must be safe and must not identify credentials")
        _text(value, "Environment value", 8192, empty=True)
    if not isinstance(modules, list) or len(modules) > 128:
        raise ValueError("Declare at most 128 versioned modules")
    for item in modules:
        _text(item, "Module declaration", 256)
    if len(set(modules)) != len(modules):
        raise ValueError("Module declarations must be unique")
    if not isinstance(seeds, dict) or len(seeds) > 128:
        raise ValueError("Declare named random seeds explicitly")
    for key, value in seeds.items():
        _text(key, "Seed name", 128)
        if type(value) is not int or not 0 <= value < 2**64:
            raise ValueError("Random seeds must be unsigned 64-bit integers")
    if not isinstance(inputs, list) or len(inputs) > 256:
        raise ValueError("Declare at most 256 versioned input references")
    names = set()
    for item in inputs:
        if not isinstance(item, dict) or set(item) - {"name", "uri", "version", "sha256", "description"} or not {"name", "uri"} <= item.keys():
            raise ValueError("Inputs need a name, URI, and immutable version or SHA-256")
        _text(item["name"], "Input name", 128)
        if item["name"] in names:
            raise ValueError("Input names must be unique")
        names.add(item["name"])
        _text(item["uri"], "Input reference")
        if item.get("version"):
            _text(item["version"], "Input version", 256)
        if item.get("sha256") is not None and (not isinstance(item["sha256"], str) or not _HASH.fullmatch(item["sha256"])):
            raise ValueError("Input SHA-256 must be a lowercase full digest")
        if not item.get("version") and not item.get("sha256"):
            raise ValueError("An unversioned input cannot provide reproducible identity")
        if "description" in item:
            _text(item["description"], "Input description", empty=True)
    S.encoded(dict(environment=environment, modules=modules, seeds=seeds, inputs=inputs))
    return environment, modules, seeds, inputs


def _metadata(capsule):
    if not isinstance(capsule, dict) or set(capsule) != _FIELDS or capsule.get("schema") != SCHEMA:
        raise ValueError("Invalid or unsupported execution capsule metadata")
    if type(capsule["created"]) not in (int, float) or not math.isfinite(capsule["created"]) or capsule["created"] < 0:
        raise ValueError("Capsule capture timestamp must be finite")
    identity = capsule["run_identity"]
    if not isinstance(identity, list) or len(identity) != 6 or not isinstance(identity[0], str) or not identity[0].startswith("/"):
        raise ValueError("Capsule needs an exact retained run identity")
    run = capsule["run"]
    if not isinstance(run, dict) or set(run) != {"project_id", "experiment_id", "run_id", "attempt", "job_id", "state"}:
        raise ValueError("Capsule run identity is incomplete")
    if (run["run_id"] != identity[1] or run["attempt"] != identity[2] or run["job_id"] != identity[3]):
        raise ValueError("Capsule inventory and retained run identity disagree")
    inventory = dict(schema="raven.run/v1", **run, resources=capsule["resources"],
                     parameters=capsule["parameters"], provenance=capsule["execution_provenance"])
    if inventory["project_id"] is None:
        del inventory["project_id"]
    projects.validate_inventory(inventory)
    script = capsule["script"]
    if (not isinstance(script, dict) or set(script) != {"path", "sha256", "source"}
            or script["source"] not in {"retained-launch-payload", "declared-source"}
            or not isinstance(script["sha256"], str) or not _HASH.fullmatch(script["sha256"])):
        raise ValueError("Capsule source identity is incomplete")
    projects.relative_path(script["path"])
    if capsule["execution"] not in {"slurm", "local", "unknown"}:
        raise ValueError("Capsule execution source is unsupported")
    for key in ("effective_options", "option_sources"):
        if not isinstance(capsule[key], dict) or len(capsule[key]) > 128:
            raise ValueError("Capsule options must be bounded objects")
        for name, value in capsule[key].items():
            _text(name, "Scheduler option", 128)
            if type(value) is not bool:
                _text(value, "Scheduler option value", 8192, empty=True)
    for key in ("arguments", "scheduler_options", "warnings"):
        if not isinstance(capsule[key], list) or len(capsule[key]) > 4096:
            raise ValueError("Capsule arguments and warnings must be bounded lists")
        for value in capsule[key]:
            _text(value, "Capsule " + key, 8192, empty=True)
    declared = capsule["declared"]
    if (not isinstance(declared, dict) or set(declared) != {"environment", "modules", "seeds", "inputs"}
            or any(type(value) is not bool for value in declared.values())):
        raise ValueError("Capsule declarations require explicit known/unknown flags")
    if not isinstance(capsule["git"], dict):
        raise ValueError("Capsule Git provenance must be an object")
    _declarations(capsule["environment"], capsule["modules"], capsule["seeds"], capsule["inputs"])
    S.encoded(capsule)
    return capsule


def _git(project, include_diff, cancel):
    """Read an object identity without invoking Git worktree conversion.

    Even diff --no-ext-diff --no-textconv can execute configured clean/process
    filters. Revision plumbing is safe here; patch bytes must be supplied as an
    explicit file instead of executing repository configuration during capture.
    """
    prefix = ["git", "-c", "core.fsmonitor=false", "-c", "core.pager=cat", "-C", project]
    backend = Backend()
    def command(arguments, limit=MAX_DIFF):
        return bounded_command.execute(backend, prefix + arguments, 5, limit, cancel)
    try:
        commit = command(["rev-parse", "--verify", "HEAD"], 4096).strip()
        if not re.fullmatch(r"[0-9a-f]{40,64}", commit):
            raise ValueError("Git returned an invalid revision")
        return dict(status="observed", commit=commit, semantics="capture-time checkout; not execution-time evidence",
                    tracked_diff_status="unknown", tracked_diff_included=False,
                    tracked_diff_reason="Supply patch_path explicitly; Git working-tree diff can execute configured clean/process filters.",
                    untracked="Not read or copied; declare required sources explicitly"), None
    except CommandError as exc:
        if cancel is not None and cancel.is_set():
            raise
        return dict(status="unavailable", reason=str(exc),
                    semantics="Capture-time Git unavailable; execution provenance is retained separately"), None


def _script(result):
    binding, inventory = result["selected"]["binding"], result["inventory"]
    report = result.get("report")
    if report:
        relative = report["plan"].get("source_snapshot")
        source = "retained-launch-payload" if relative else "declared-source"
    else:
        relative, source = None, "declared-source"
    relative = relative or inventory.get("provenance", {}).get("script")
    expected = inventory.get("provenance", {}).get("script_sha256")
    expected = expected.lower() if isinstance(expected, str) else expected
    if not relative or not isinstance(expected, str) or not _HASH.fullmatch(expected):
        raise ValueError("This run lacks a recorded script path and SHA-256; attach verified launch evidence first")
    projects.relative_path(relative)
    _, fd = projects._root(binding["project_root"])
    try:
        raw, _ = projects._read(fd, relative, MAX_SCRIPT)
    finally:
        os.close(fd)
    if _sha(raw) != expected:
        raise ValueError("The recorded script changed; its original payload is required before creating a capsule")
    return raw, dict(path=relative, sha256=expected, source=source)


def capture(project, run_id, *, destination=None, environment=None, modules=None,
            seeds=None, inputs=None, include_diff=False, patch_path=None, cancel=None):
    """Publish one new package; never execute it or overwrite another package.

    Environment values, modules, random seeds, and input references are supplied
    by the caller. Their absence means unknown rather than an empty execution
    environment. Patch bytes require an explicit ``patch_path`` file and
    ``include_diff=True``. Repository Git filters are never executed here.
    """
    if type(include_diff) is not bool:
        raise ValueError("include_diff must be a boolean")
    if include_diff and patch_path is None:
        raise ValueError("include_diff requires an explicit reviewed patch_path; automatic Git diff can execute repository filters")
    declarations = _declarations(environment, modules, seeds, inputs)
    declared = {"environment": environment is not None, "modules": modules is not None,
                "seeds": seeds is not None, "inputs": inputs is not None}
    selected = projects.select_run(str(project), run_id)
    result = run_home.load(selected["binding"])
    raw, script = _script(result)
    binding, inventory = result["selected"]["binding"], result["inventory"]
    root = binding["project_root"]
    git, patch = _git(root, include_diff, cancel)
    patch_source = None
    if patch_path is not None:
        patch_source = Path(_text(str(patch_path), "Patch path")).expanduser().absolute()
        _, patch_fd = projects._root(str(patch_source.parent))
        try:
            patch_raw, _ = projects._read(patch_fd, patch_source.name, MAX_DIFF)
        finally:
            os.close(patch_fd)
        git.update(tracked_diff_status="supplied-patch", tracked_diff_sha256=_sha(patch_raw),
                   tracked_diff_bytes=len(patch_raw), tracked_diff_included=include_diff,
                   tracked_diff_reason="Explicit supplied patch; correspondence with checkout and execution is unverified.")
        patch = patch_raw if include_diff else None
    report = result.get("report")
    plan = report["plan"] if report else {}
    warnings = ["Input references are declarations; their bytes have not been read or independently verified.",
                "Environment and module declarations are supplied evidence, not recovered process state.",
                "Review scripts and optional patches before sharing; they can contain confidential source."]
    if git.get("tracked_diff_bytes") and not git.get("tracked_diff_included"):
        warnings.append("Tracked working-tree patch omitted. Its digest cannot restore unpublished changes.")
    if git.get("tracked_diff_status") != "supplied-patch":
        warnings.append("Working-tree changes are unknown. Supply a reviewed patch file to retain local modifications; capture never executes repository Git filters.")
    if not all(declared.values()):
        warnings.append("Some execution context was not declared; this package does not claim complete reproduction.")
    files = [("script.sh", raw, True)]
    if patch is not None:
        files.append(("working-tree.patch", patch, False))
    captured = dict(schema=SCHEMA, created=time.time(), run_identity=list(run_home.identity(binding)),
                    run=dict(project_id=binding.get("project_id"), experiment_id=binding.get("experiment_id"),
                             run_id=binding["run_id"], attempt=binding["attempt"], job_id=binding.get("job_id"),
                             state=inventory["state"]),
                    script=script, resources=copy.deepcopy(inventory.get("resources", {})),
                    effective_options=copy.deepcopy(plan.get("effective_options", {})),
                    option_sources=copy.deepcopy(plan.get("option_sources", {})),
                    scheduler_options=copy.deepcopy(plan.get("sbatch_options", [])),
                    execution=plan.get("execution", "unknown"), arguments=copy.deepcopy(plan.get("script_args", [])),
                    parameters=copy.deepcopy(inventory.get("parameters", {})),
                    execution_provenance=copy.deepcopy(inventory.get("provenance", {})), git=git,
                    environment=declarations[0], modules=declarations[1], seeds=declarations[2], inputs=declarations[3],
                    declared=declared, warnings=warnings,
                    files=[dict(path=name, sha256=_sha(content), size=len(content)) for name, content, _ in files],
                    integrity="Digests verify retained bytes; they do not prove declarations or successful reproduction.")
    manifest = S.encoded(_metadata(captured))
    files.append(("capsule.json", manifest, False))
    receipt = dict(schema="raven.capsule-publication/v1", manifest_sha256=_sha(manifest),
                   run_identity=captured["run_identity"])
    if destination is None:
        with S.locked(root, "capsules"):
            destination = str(Path(root) / ".raven" / "engineering" / "capsules" /
                              (run_id + "-" + uuid.uuid4().hex[:12]))
    def final_check():
        science_files.cancelled(cancel)
        current = run_home.load(binding)
        verify_raw, _ = _script(current)
        if verify_raw != raw:
            raise ValueError("Retained source changed during capsule publication")
        if patch_source is not None:
            _, current_fd = projects._root(str(patch_source.parent))
            try:
                current_patch, _ = projects._read(current_fd, patch_source.name, MAX_DIFF)
            finally:
                os.close(current_fd)
            if _sha(current_patch) != git["tracked_diff_sha256"]:
                raise ValueError("Supplied patch changed during capsule publication")
    path = science_files.publish(str(destination), files=files, receipt=receipt,
                                 cancel=cancel, final_check=final_check)
    return dict(path=path, capsule=captured, receipt=receipt)


def verify(path):
    """Verify a complete immutable publication with anchored bounded reads."""
    _, fd = projects._root(str(path))
    try:
        try:
            os.stat(".raven-incomplete", dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise ValueError("Capsule publication is incomplete")
        raw, _ = projects._read(fd, "capsule.json", S.MAX_JSON)
        from .. import reporting
        def json_value(data):
            # Existing bounded native reporting reader rejects duplicates and
            # nonfinite values; use it through the stable anchored raw parser.
            from ..artifacts import _json
            value = _json(data)
            reporting._encoded(value, S.MAX_JSON)
            return value
        capsule = json_value(raw)
        receipt_raw, _ = projects._read(fd, ".raven-receipt.json", 65536)
        receipt = json_value(receipt_raw)
        if (not isinstance(capsule, dict) or capsule.get("schema") != SCHEMA
                or not isinstance(receipt, dict) or receipt.get("schema") != "raven.capsule-publication/v1"
                or receipt.get("manifest_sha256") != _sha(raw)
                or receipt.get("run_identity") != capsule.get("run_identity")):
            raise ValueError("Capsule manifest or publication receipt failed integrity checks")
        _metadata(capsule)
        files = capsule.get("files")
        if not isinstance(files, list) or not 1 <= len(files) <= 2:
            raise ValueError("Capsule has an invalid retained-file inventory")
        seen = set()
        for item in files:
            if (not isinstance(item, dict) or set(item) != {"path", "sha256", "size"}
                    or item["path"] not in {"script.sh", "working-tree.patch"} or item["path"] in seen
                    or not isinstance(item["sha256"], str) or not _HASH.fullmatch(item["sha256"])
                    or type(item["size"]) is not int or not 0 <= item["size"] <= MAX_SCRIPT):
                raise ValueError("Invalid capsule file identity")
            seen.add(item["path"])
            data, _ = projects._read(fd, item["path"], MAX_SCRIPT)
            if len(data) != item["size"] or _sha(data) != item["sha256"]:
                raise ValueError("Capsule source bytes do not match their retained digest")
        if "script.sh" not in seen or capsule.get("script", {}).get("sha256") != next(item["sha256"] for item in files if item["path"] == "script.sh"):
            raise ValueError("Capsule script identity is incomplete")
        return capsule
    finally:
        os.close(fd)


def compare(left, right):
    """Show effective settings and provenance differences before any rerun.

    Paths are verified capsule publications. Documents must already be verified
    by their caller. A matching comparison does not certify scientific output.
    """
    left = verify(left) if isinstance(left, (str, Path)) else copy.deepcopy(left)
    right = verify(right) if isinstance(right, (str, Path)) else copy.deepcopy(right)
    _metadata(left)
    _metadata(right)
    fields = ("script", "resources", "effective_options", "option_sources", "scheduler_options", "execution",
              "arguments", "parameters", "execution_provenance", "git", "environment", "modules", "seeds", "inputs", "declared")
    changed = [dict(field=field, previous=left.get(field), current=right.get(field))
               for field in fields if left.get(field) != right.get(field)]
    unknown = sorted({name for value in (left, right) for name, declared in value.get("declared", {}).items() if not declared})
    return dict(changed=changed, matching=not changed, unknown=unknown,
                compatible_for_comparison=not changed and not unknown,
                limitation="Matching retained settings does not establish identical hardware, data bytes, process environment, or scientific results.")


diff = compare


def relaunch_draft(path, *, project=None):
    """Return the existing launch wizard's editable draft and an honest diff.

    No script, patch, environment assignment, or module command is executed.
    The capsule must be inside the selected project so ordinary launch path
    confinement remains effective. For an external package, explicitly copy it
    into the target project first, then verify that copy. Environment/module/
    seed declarations remain review context; application setup stays visible in
    the retained script instead of being injected as hidden executable state.
    """
    capsule = verify(path)
    package_root, package_fd = projects._root(str(path))
    os.close(package_fd)
    original_root = capsule["run_identity"][0]
    target = str(project) if project is not None else original_root
    project_root, root_fd = projects._root(target)
    os.close(root_fd)
    try:
        relative = str(Path(package_root).relative_to(project_root) / "script.sh")
    except ValueError as exc:
        raise ValueError("Copy this verified capsule into the selected project before preparing a rerun") from exc
    projects.relative_path(relative)
    if capsule["execution"] not in {"slurm", "local"}:
        raise ValueError("This capsule has no supported retained launch path; choose a native scheduler submission explicitly")
    effective = copy.deepcopy(capsule["effective_options"])
    if not isinstance(effective, dict) or len(effective) > 128:
        raise ValueError("Capsule effective options must be a bounded object")
    # The ordinary launch parser validates every scheduler option and disallows
    # script-boundary options. Its review is the sole path to submission.
    captured = dict(script_sha256=capsule["script"]["sha256"], effective_options=effective,
                    settings=dict(resources=copy.deepcopy(capsule["resources"]),
                                  parameters=copy.deepcopy(capsule["parameters"]),
                                  environment=copy.deepcopy(capsule["environment"])))
    draft = dict(project=project_root, script=str(Path(package_root) / "script.sh"),
                 arguments=shlex.join(capsule["arguments"]), execution=capsule["execution"],
                 recipe="", recipe_name="capsule-rerun", account="", partition="", qos="",
                 cpus="", gpus="", memory="", time="", gpu=None, cpu_memory=None,
                 trace=None, basic_research=False, captured_plan=captured, submit_dir=project_root)
    from ..launching import wizard
    reviewed = wizard.review(draft, environ={})
    changes = []
    if original_root != project_root:
        changes.append(dict(field="project_root", previous=original_root, current=project_root))
    if capsule["script"]["path"] != relative:
        changes.append(dict(field="script_location", previous=capsule["script"]["path"], current=relative,
                            content_sha256=capsule["script"]["sha256"]))
    if reviewed["plan"]["effective_options"] != effective:
        changes.append(dict(field="effective_options", previous=effective, current=reviewed["plan"]["effective_options"]))
    unknown = [name for name, supplied in capsule["declared"].items() if not supplied]
    return dict(draft=draft, command=reviewed["command"], changes=changes, unknown=sorted(unknown),
                declarations={key: copy.deepcopy(capsule[key]) for key in ("environment", "modules", "seeds", "inputs")},
                requires_review=True,
                limits=["Run this draft through the existing launch review; no work has been submitted.",
                        "Environment, module, seed, and input declarations are comparison context; they are not silently executed or injected.",
                        "Capture-time patches are retained evidence and are not applied automatically.",
                        "Hardware, current input bytes, external libraries, and output equivalence remain unverified."])
