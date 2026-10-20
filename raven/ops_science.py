"""Reviewed reproducibility, placement, scientific validation and dependency repair."""
from __future__ import annotations

import importlib.metadata
import json
import os
from pathlib import Path
import platform
import re
import shlex

from .deps import normalized_dependency as _normalized_dependency
from . import science_files as sf
from . import science_stats
from .operations import command, digest, local_only, prepare_plan, read_json, report, validate_plan, checkpoint


def _field(key, label, default="", required=False):
    return {"key": key, "label": label, "default": default, "required": required}


SPECIFICATIONS = [
    {"key": "environment", "title": "Recreate environment", "group": "Science", "proposal": "A01",
     "summary": "Compare a declared runtime and prepare a pinned, hashed Python environment bundle.",
     "fields": [_field("manifest", "Environment manifest", required=True), _field("destination", "New bundle directory (optional)"),
                _field("probe_job_id", "Probe compute host for running job (optional)")]},
    {"key": "placement", "title": "Allocation placement", "group": "Science", "proposal": "A02",
     "summary": "Compare requested allocation with existing process CPU, NUMA and GPU visibility.",
     "fields": [_field("job_id", "Running job ID")]},
    {"key": "acceptance", "title": "Scientific acceptance", "group": "Science", "proposal": "A04",
     "summary": "Evaluate every required scientific result against explicit absolute and relative tolerances.",
     "fields": [_field("manifest", "Acceptance policy", required=True), _field("results", "Result measurements", required=True)]},
    {"key": "statistics", "title": "Statistical comparison", "group": "Science", "proposal": "A07",
     "summary": "Compare independent units or exact pairs with Student t confidence intervals.",
     "fields": [_field("manifest", "Comparison manifest", required=True)]},
    {"key": "reuse", "title": "Verified result reuse", "group": "Science", "proposal": "A08",
     "summary": "Verify identity, dependencies, scientific acceptance and outputs before copying results.",
     "fields": [_field("manifest", "Reuse manifest", required=True), _field("identity", "Requested identity JSON", required=True),
                _field("destination", "New result directory", required=True)]},
    {"key": "dependency-repair", "title": "Repair dependencies", "group": "Science", "proposal": "A15",
     "summary": "Review exact pending-job dependency changes while preserving completed branches.",
     "fields": [_field("manifest", "Dependency repair manifest", required=True)]},
]

HASH = re.compile(r"[0-9a-f]{64}\Z")
JOB_ID = re.compile(r"[0-9]+(?:_[0-9]+)?(?:\+[0-9]+)?\Z")


def _required(params, key):
    value = params.get(key, "")
    if not isinstance(value, str) or not value.strip() or len(value) > 4096 or any(c in value for c in "\x00\n\r"):
        raise ValueError(f"Choose an explicit {key} value")
    return value.strip()


def _hash(value, name="SHA256"):
    if not isinstance(value, str) or not HASH.fullmatch(value):
        raise ValueError(f"{name} must contain exactly 64 lowercase hexadecimal digits")
    return value


def _schema(doc, name, keys):
    if not isinstance(doc, dict) or set(doc) != set(keys) | {"schema"} or doc.get("schema") != f"raven.{name}/v1":
        raise ValueError(f"Expected raven.{name}/v1 with exactly {', '.join(keys)}")


def _json_record(path, ctx):
    return sf.json_file(path, ctx.cancel)


def _environment(params, ctx):
    local_only(ctx)
    path = sf.path_at(os.getcwd(), _required(params, "manifest"))
    spec, record = _json_record(path, ctx)
    required = {"schema", "python", "platform", "machine", "packages", "lockfile"}
    if not isinstance(spec, dict) or not required <= set(spec) or set(spec) - required - {"container", "native"} or spec.get("schema") != "raven.environment/v1":
        raise ValueError("Expected raven.environment/v1 with python, platform, machine, packages, lockfile and optional container/native constraints")
    if not isinstance(spec["python"], str) or not re.fullmatch(r"3\.[0-9]{1,2}\.[0-9]{1,3}", spec["python"]):
        raise ValueError("Environment python must pin major.minor.patch")
    for key in ("platform", "machine"):
        if not isinstance(spec[key], str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", spec[key]):
            raise ValueError(f"Invalid expected {key}")
    packages = spec["packages"]
    if not isinstance(packages, dict) or not 1 <= len(packages) <= 2048:
        raise ValueError("Environment must declare 1–2048 exact package versions")
    normalized = {}
    for name, version in packages.items():
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", name) or not isinstance(version, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.+!-]{0,127}", version):
            raise ValueError("Use pinned package names and exact versions")
        key = re.sub(r"[-_.]+", "-", name).lower()
        if key in normalized:
            raise ValueError("Duplicate normalized package name")
        normalized[key] = version
    lock = spec["lockfile"]
    if not isinstance(lock, dict) or set(lock) != {"path", "sha256"}:
        raise ValueError("lockfile requires path and sha256")
    _hash(lock["sha256"])
    lock_record, raw = sf.snapshot(sf.path_at(Path(path).parent, lock["path"]), cancel=ctx.cancel, limit=sf.METADATA_LIMIT, keep=True)
    if lock_record["sha256"] != lock["sha256"]:
        raise ValueError("The environment lockfile SHA256 does not match")
    text = raw.decode("utf-8").replace("\\\r\n", " ").replace("\\\n", " ")
    pins = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = re.fullmatch(r"([A-Za-z0-9][A-Za-z0-9_.-]*)==([A-Za-z0-9][A-Za-z0-9_.+!-]*)(\s+--hash=sha256:[0-9a-f]{64})+", line)
        if not match:
            raise ValueError("Lockfile accepts only name==version with SHA256 hashes; no includes, URLs, editable installs or index options")
        name = re.sub(r"[-_.]+", "-", match[1]).lower()
        if name in pins:
            raise ValueError("Lockfile contains duplicate package pins")
        pins[name] = match[2]
    if pins != normalized:
        raise ValueError("Declared packages must exactly match the hash-locked requirements")
    actual = {"python": platform.python_version(), "platform": platform.system(), "machine": platform.machine(), "libc": list(platform.libc_ver()), "packages": {}}
    rows, mismatches = [], 0
    for key in ("python", "platform", "machine"):
        differs = actual[key] != spec[key]
        mismatches += differs
        rows.append(f"{key}: declared {spec[key]}; local {actual[key]}" + (" [DIFF]" if differs else " [MATCH]"))
    for name, expected in sorted(normalized.items()):
        checkpoint(ctx)
        try:
            installed = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            installed = None
        actual["packages"][name] = installed
        differs = installed != expected
        mismatches += differs
        rows.append(f"{name}: declared {expected}; local {installed or 'missing'}" + (" [DIFF]" if differs else " [MATCH]"))
    sources = [record, lock_record]
    warnings = ["Local measurements describe the Raven host. Allocation-host probes do not certify the existing workload or container process runtime."]
    native = spec.get("native")
    if native is not None:
        if not isinstance(native, dict) or set(native) != {"libc", "minimum_version"} or native["libc"] not in {"glibc", "musl"} or not isinstance(native["minimum_version"], str) or not re.fullmatch(r"[0-9]+(?:\.[0-9]+){1,3}", native["minimum_version"]):
            raise ValueError("Native constraints require libc (glibc/musl) and a numeric minimum_version")
        match = _libc_matches(native, actual["libc"])
        mismatches += not match
        rows.append(f"Native libc: require {native['libc']} >= {native['minimum_version']}; local {' '.join(actual['libc']) or 'unknown'}" + (" [MATCH]" if match else " [DIFF/UNKNOWN]"))
    container = spec.get("container")
    if container is not None:
        if not isinstance(container, dict):
            raise ValueError("container must be an immutable OCI reference or a hashed local SIF image")
        if container.get("kind") == "oci" and set(container) == {"kind", "reference"}:
            if not isinstance(container["reference"], str) or not re.fullmatch(r"docker://[A-Za-z0-9][A-Za-z0-9._:/-]{1,500}@sha256:[0-9a-f]{64}", container["reference"]):
                raise ValueError("OCI image references must pin an exact sha256 digest; mutable tags are not accepted")
            rows.append("Container identity pinned: " + container["reference"])
            warnings.append("OCI image bytes are not pulled during review. Apptainer must verify the digest when the generated script is run.")
        elif container.get("kind") == "sif" and set(container) == {"kind", "path", "sha256"}:
            image_record = _verified_entry({key: container[key] for key in ("path", "sha256")}, Path(path).parent, ctx)
            sources.append(image_record)
            rows.append("SIF image SHA256 verified: " + image_record["path"])
        else:
            raise ValueError("Container requires kind=oci/reference or kind=sif/path/sha256")
        warnings.append("Recreation uses Apptainer with a clean environment; it requires site-approved image support and destination bind access. Driver and external-library compatibility are not inferred.")
    compute = []
    if params.get("probe_job_id", "").strip():
        compute = _runtime_probe({"job_id": params["probe_job_id"]}, ctx, sorted(normalized))
        for node in compute:
            mismatch = [key for key in ("python", "platform", "machine") if node[key] != spec[key]]
            mismatch.extend(name for name, version in normalized.items() if node["packages"].get(name) != version)
            if native and not _libc_matches(native, node["libc"]):
                mismatch.append("native libc")
            rows.append(f"Compute host {node['node']}: {len(mismatch)} differences" + (" — " + ", ".join(mismatch[:12]) if mismatch else ""))
            node["differences"] = mismatch
            warnings.extend(f"{node['node']}: {message}" for message in node["warnings"])
    else:
        warnings.append("Compute-host compatibility was not measured. Enter a running job ID to collect allocation-scoped evidence.")
    payload = {"sources": sources, "manifest": spec, "runtime": actual}
    plan = None
    if params.get("destination", "").strip():
        payload["destination"] = sf.destination(_required(params, "destination"))
        if container and any(character in payload["destination"] for character in ":,\n\r"):
            raise ValueError("An Apptainer bundle path cannot contain bind-list delimiters (: or ,)")
        plan = prepare_plan(ctx, "environment", params, payload)
        rows.append("Apply writes recreate.sh, requirements.lock and environment.json. Raven does not execute them.")
    return report("environment", f"Environment comparison: {mismatches} differences", status="warning" if mismatches else "ok",
                  rows=rows, data={"expected": spec, "actual": actual, "differences": mismatches, "compute_hosts": compute}, plan=plan, warnings=warnings)


def _libc_matches(expected, actual):
    try:
        return actual[0] == expected["libc"] and tuple(map(int, actual[1].split("."))) >= tuple(map(int, expected["minimum_version"].split(".")))
    except (ValueError, TypeError, IndexError):
        return False


def _runtime_probe(params, ctx, packages):
    from .science_probe import RUNTIME_SOURCE, parse_runtime
    jid = _selected(params, ctx)
    before = _details(ctx, jid)
    _owner(ctx, before)
    if before.get("JobState") != "RUNNING":
        raise ValueError("Compute runtime inspection requires a running allocation")
    try:
        nodes = int(before["NumNodes"])
    except (KeyError, ValueError, TypeError) as exc:
        raise ValueError("Compute allocation node count is unknown") from exc
    if not 1 <= nodes <= 128:
        raise ValueError("Compute runtime inspection supports 1–128 nodes")
    argv = ["srun", "--jobid", jid, "--overlap", "--immediate=5", "--quiet", "-N", str(nodes), "--ntasks", str(nodes),
            "--ntasks-per-node=1", "--cpus-per-task=1", "--label", "python3", "-c", RUNTIME_SOURCE, jid, json.dumps(packages)]
    observations = parse_runtime(command(ctx, argv, timeout=20, limit=8 << 20), jid)
    after = _details(ctx, jid)
    if _attempt(before) != _attempt(after) or after.get("JobState") != "RUNNING" or after.get("NodeList") != before.get("NodeList"):
        raise ValueError("Allocation changed during runtime inspection")
    if len(observations) != nodes:
        raise ValueError("Compute runtime evidence is incomplete; not all allocation nodes returned")
    return observations


def _environment_apply(payload, ctx):
    local_only(ctx)
    for source in payload["sources"]:
        sf.verify(source, ctx.cancel)
    lock_fresh, lock = sf.snapshot(payload["sources"][1]["path"], cancel=ctx.cancel, limit=sf.METADATA_LIMIT, keep=True)
    if lock_fresh != payload["sources"][1]:
        raise ValueError("Reviewed lockfile changed before bundle publication")
    spec = payload["manifest"]
    python = "python" + ".".join(spec["python"].split(".")[:2])
    check = "import platform,sys; expected=" + repr({key: spec[key] for key in ("python", "platform", "machine")}) + "; actual=dict(python=platform.python_version(),platform=platform.system(),machine=platform.machine()); sys.exit(0 if actual==expected else 'Runtime mismatch: '+repr(actual))"
    if spec.get("native"):
        check = check.replace("sys.exit(0 if actual==expected else 'Runtime mismatch: '+repr(actual))", "")
        check += " native=" + repr(spec["native"]) + "; libc=platform.libc_ver(); ok=libc[0]==native['libc'] and tuple(map(int,libc[1].split('.')))>=tuple(map(int,native['minimum_version'].split('.'))); sys.exit(0 if actual==expected and ok else 'Runtime/native mismatch: '+repr((actual,libc)))"
    hash_check = "import hashlib,sys; actual=hashlib.sha256(open(sys.argv[1],'rb').read()).hexdigest(); sys.exit(0 if actual==" + repr(spec["lockfile"]["sha256"]) + " else 'Lockfile digest mismatch')"
    prefix, image_check = "", ""
    container = spec.get("container")
    if container:
        image = container["reference"] if container["kind"] == "oci" else payload["sources"][2]["path"]
        prefix = "apptainer exec --cleanenv --bind \"$base:$base:ro\" --bind \"$target_parent:$target_parent\" " + shlex.quote(image) + " "
        if container["kind"] == "sif":
            image_code = "import hashlib,sys; h=hashlib.sha256(); f=open(sys.argv[1],'rb'); [h.update(b) for b in iter(lambda:f.read(1048576),b'')]; sys.exit(0 if h.hexdigest()==" + repr(container["sha256"]) + " else 'SIF image digest mismatch')"
            image_check = "python3 -I -c " + shlex.quote(image_code) + " " + shlex.quote(image) + "\n"
    script = ("#!/bin/sh\nset -eu\numask 077\n"
              "base=$(CDPATH= cd -- \"$(dirname -- \"$0\")\" && pwd)\n"
              "target=${1:?Usage: recreate.sh NEW_VENV_PATH}\n"
              "case \"$target\" in /*) ;; *) target=\"$PWD/$target\" ;; esac\n"
              "case \"$target\" in *:*|*,*) echo 'Target cannot contain : or ,' >&2; exit 1 ;; esac\n"
              "target_parent=$(CDPATH= cd -- \"$(dirname -- \"$target\")\" && pwd)\n"
              "if [ -e \"$target\" ] || [ -L \"$target\" ]; then echo 'Target already exists' >&2; exit 1; fi\n" + image_check +
              prefix + shlex.quote(python) + " -I -c " + shlex.quote(check) + "\n" +
              prefix + shlex.quote(python) + " -I -c " + shlex.quote(hash_check) + " \"$base/requirements.lock\"\n"
              "mkdir -- \"$target\"\n" +
              prefix + shlex.quote(python) + " -I -m venv -- \"$target\"\n" +
              prefix + "\"$target/bin/python\" -I -m pip --isolated install --require-hashes --no-deps -r \"$base/requirements.lock\"\n" +
              prefix + "\"$target/bin/python\" -I -m pip --isolated check\n")
    result = sf.publish(payload["destination"], files=[("recreate.sh", script.encode(), True), ("requirements.lock", lock, False),
                        ("environment.json", (json.dumps(spec, indent=2)+"\n").encode(), False)],
                        receipt={"schema": "raven.environment-bundle/v1", "sources": payload["sources"]}, cancel=ctx.cancel)
    return report("environment", "Environment recreation bundle written", rows=[result, "Run recreate.sh with a new venv path after site review. No environment was executed by Raven."])


def _selected(params, ctx):
    jid = str(params.get("job_id") or ctx.selected)
    if not JOB_ID.fullmatch(jid):
        raise ValueError("Select one exact Slurm job ID; ranges and wildcards are not supported")
    return jid


def _details(ctx, jid):
    from .ops_cluster import parse_records
    checkpoint(ctx)
    records = parse_records(command(ctx, ["scontrol", "show", "job", "-o", jid]), "JobId")
    if len(records) != 1:
        raise ValueError(f"Scheduler returned ambiguous job records for {jid}")
    result = records[0]
    identities = {result.get("JobId", ""), f"{result.get('ArrayJobId', '')}_{result.get('ArrayTaskId', '')}",
                  f"{result.get('HetJobId', '')}+{result.get('HetJobOffset', '')}"}
    if jid not in identities:
        raise ValueError(f"Scheduler returned a different job identity for {jid}")
    return result


def _attempt(detail):
    return {key: detail.get(key, "") for key in ("JobId", "ArrayJobId", "ArrayTaskId", "HetJobId", "HetJobOffset", "SubmitTime", "StartTime", "Restarts", "RestartCnt", "UserId")}


def _owner(ctx, detail):
    user = str(getattr(ctx.slurm, "user", ""))
    if not user or detail.get("UserId", "").split("(", 1)[0] != user:
        raise ValueError("Operation requires a verified job owned by the active Slurm user")
    if not detail.get("SubmitTime") or detail["SubmitTime"] in {"Unknown", "N/A"}:
        raise ValueError("Scheduler did not provide a stable submission identity")


def _placement(params, ctx):
    from . import science_probe
    if ctx.replay:
        raise ValueError("Live placement cannot be collected from a recorded session")
    jid = _selected(params, ctx)
    detail = _details(ctx, jid)
    _owner(ctx, detail)
    if detail.get("JobState") != "RUNNING":
        raise ValueError("Placement requires a running job allocation")
    try:
        nodes = int(detail["NumNodes"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Scheduler did not provide an allocation node count") from exc
    if not 1 <= nodes <= 128:
        raise ValueError("On-demand placement supports 1–128 nodes")
    argv = ["srun", "--jobid", jid, "--overlap", "--immediate=5", "--quiet", "-N", str(nodes),
            "--ntasks", str(nodes), "--ntasks-per-node=1", "--cpus-per-task=1", "--label", "python3", "-c", science_probe.SOURCE, jid, detail["JobId"]]
    observations = science_probe.parse(command(ctx, argv, timeout=20, limit=8 << 20), jid)
    fresh = _details(ctx, jid)
    if _attempt(fresh) != _attempt(detail) or fresh.get("JobState") != "RUNNING" or fresh.get("NodeList") != detail.get("NodeList"):
        raise ValueError("Allocation changed during placement inspection; stale measurements were discarded")
    warnings = ["CPU masks and NUMA residency describe observed processes, not historical execution. GPU visibility is not proof of device utilization or binding.",
                "The explicit probe starts a short overlapping one-CPU step per node; existing process affinity is never changed."]
    rows = [f"Requested/allocated CPUs: {detail.get('NumCPUs', 'unknown')}; nodes: {nodes}; TRES: {detail.get('AllocTRES', detail.get('TRES', 'unknown'))}",
            f"Requested binding: {detail.get('CpuBind', 'not retained by scheduler')}"]
    if len(observations) != nodes:
        warnings.append(f"Only {len(observations)}/{nodes} allocation nodes returned evidence")
    count = 0
    for node in observations:
        warnings.extend(f"{node['node']}: {message}" for message in node["warnings"])
        for process in node["processes"]:
            count += 1
            rows.append(f"{node['node']} PID {process['pid']} CPU {process['cpu_allowed'] or 'unknown'}; NUMA pages {json.dumps(process['numa_pages'], sort_keys=True)}; GPU visibility {json.dumps(process['gpu_visibility'], sort_keys=True)}")
    if not count:
        warnings.append("No existing workload processes were accessible; placement remains unknown")
    return report("placement", f"Placement evidence for {jid}: {count} processes", status="ok" if count else "unknown",
                  rows=rows, data={"requested": detail, "observations": observations}, warnings=warnings)


def _acceptance(params, ctx):
    result = science_stats.acceptance(read_json(ctx, _required(params, "manifest")), read_json(ctx, _required(params, "results")))
    passed = result["passed"] == result["total"]
    rows = [f"{'PASS' if row['pass'] else 'FAIL'} {row['case']} / {row['metric']}: {row['observed']} vs {row['expected']}; tolerance {row['allowed_error']:g}; {row['reason']}" for row in result["matrix"]]
    return report("acceptance", f"Scientific acceptance: {result['passed']}/{result['total']} checks passed", status="ok" if passed else "failed", rows=rows, data=result,
                  warnings=["Tolerance is max(absolute, relative × abs(expected)); scheduler completion does not imply scientific acceptance."])


def _statistics(params, ctx):
    result = science_stats.compare(read_json(ctx, _required(params, "manifest")))
    rows = [f"Method: {result['method']}; independent unit: {result['unit']}",
            f"Control n={result['control_n']} mean={result['control_mean']:.8g}; treatment n={result['treatment_n']} mean={result['treatment_mean']:.8g}",
            f"Treatment − control: {result['difference']:.8g}"]
    if result["interval"]:
        rows.append(f"{result['confidence']*100:g}% confidence interval [{result['interval'][0]:.8g}, {result['interval'][1]:.8g}], two-sided p={result['p_value']:.6g}")
    else:
        rows.append("Confidence interval and p-value unavailable; uncertainty cannot be estimated.")
    return report("statistics", "Experimental comparison", status="ok" if result["interval"] else "unknown", rows=rows, data=result, warnings=result["warnings"])


def _identity(value):
    keys = {"project", "code_sha256", "environment_sha256", "parameters_sha256", "inputs_sha256"}
    if not isinstance(value, dict) or set(value) != keys or not isinstance(value["project"], str) or not value["project"].strip() or len(value["project"]) > 256:
        raise ValueError("Reuse identity requires project, code/environment/parameters SHA256 hashes, and inputs_sha256")
    if not isinstance(value["inputs_sha256"], dict) or len(value["inputs_sha256"]) > 10000:
        raise ValueError("inputs_sha256 must map every declared scientific input path to its hash")
    for path, value_hash in value["inputs_sha256"].items():
        if not isinstance(path, str) or not path or len(path) > 4096:
            raise ValueError("Input identity paths must be nonempty bounded strings")
        _hash(value_hash, "input SHA256")
    for key in keys - {"project", "inputs_sha256"}:
        _hash(value[key], key)
    return value


def _verified_entry(entry, base, ctx, extra=()):
    if not isinstance(entry, dict) or set(entry) != {"path", "sha256"} | set(extra):
        raise ValueError("A verified file needs path, sha256" + (" and " + ", ".join(extra) if extra else ""))
    _hash(entry["sha256"])
    record, _ = sf.snapshot(sf.path_at(base, entry["path"]), cancel=ctx.cancel)
    if record["sha256"] != entry["sha256"]:
        raise ValueError(f"SHA256 mismatch: {record['path']}")
    return record


def _reuse(params, ctx):
    local_only(ctx)
    path = sf.path_at(os.getcwd(), _required(params, "manifest"))
    base = Path(path).parent
    doc, manifest_record = _json_record(path, ctx)
    _schema(doc, "reuse", ("identity", "dependencies", "outputs", "acceptance", "results"))
    wanted, identity_record = _json_record(sf.path_at(os.getcwd(), _required(params, "identity")), ctx)
    if _identity(doc["identity"]) != _identity(wanted):
        raise ValueError("Cached result identity does not exactly match the requested project/code/environment/parameters")
    if not isinstance(doc["dependencies"], list) or not 3 <= len(doc["dependencies"]) <= 10000 or not isinstance(doc["outputs"], list) or not 1 <= len(doc["outputs"]) <= 10000:
        raise ValueError("Reuse requires bounded dependencies and at least one output")
    sources, roles, paths, inputs = [manifest_record, identity_record], {}, set(), {}
    for entry in doc["dependencies"]:
        record = _verified_entry(entry, base, ctx, ("role",))
        role = entry["role"]
        if role not in {"code", "environment", "parameters", "input"}:
            raise ValueError("Dependency role must be code, environment, parameters or input")
        canonical = os.path.realpath(record["path"])
        if canonical in paths:
            raise ValueError("A dependency path was declared more than once")
        paths.add(canonical)
        if role != "input":
            if role in roles or record["sha256"] != wanted[role+"_sha256"]:
                raise ValueError("Each identity role must have exactly one matching verified file")
            roles[role] = record["sha256"]
        else:
            inputs[entry["path"]] = record["sha256"]
        sources.append(record)
    if set(roles) != {"code", "environment", "parameters"}:
        raise ValueError("Code, environment and parameters must each be verified dependencies")
    if inputs != wanted["inputs_sha256"]:
        raise ValueError("Every declared scientific input must exactly match the requested input identity")
    policy_record = _verified_entry(doc["acceptance"], base, ctx)
    results_record = _verified_entry(doc["results"], base, ctx)
    policy, policy_fresh = _json_record(policy_record["path"], ctx)
    results, results_fresh = _json_record(results_record["path"], ctx)
    if policy_fresh != policy_record or results_fresh != results_record:
        raise ValueError("Scientific acceptance source changed during inspection")
    accepted = science_stats.acceptance(policy, results)
    if accepted["passed"] != accepted["total"]:
        raise ValueError(f"Scientific acceptance failed: {accepted['passed']}/{accepted['total']} checks passed")
    sources.extend([policy_record, results_record])
    outputs, targets = [], set()
    for entry in doc["outputs"]:
        record = _verified_entry(entry, base, ctx, ("target",))
        target = sf.relative_target(entry["target"])
        if target in targets or any(target.startswith(other+"/") or other.startswith(target+"/") for other in targets):
            raise ValueError("Output targets must be unique and cannot overlap files with directories")
        targets.add(target)
        outputs.append({"source": record, "target": target})
    dest = sf.destination(_required(params, "destination"))
    for source in sources + [output["source"] for output in outputs]:
        if os.path.commonpath((os.path.realpath(source["path"]), os.path.realpath(dest))) == os.path.realpath(dest):
            raise ValueError("Destination cannot contain a declared source")
    payload = {"destination": dest, "sources": sources, "outputs": outputs, "identity": wanted, "acceptance": accepted}
    plan = prepare_plan(ctx, "reuse", params, payload)
    return report("reuse", f"Verified reuse ready: {len(outputs)} outputs", rows=[f"{output['source']['path']} → {output['target']} ({output['source']['size']} bytes)" for output in outputs],
                  data={"identity": wanted, "acceptance": accepted, "destination": dest}, plan=plan,
                  warnings=["Only declared dependencies are verified. Declare every scientific input and random seed; hidden dependencies cannot be inferred."])


def _reuse_apply(payload, ctx):
    local_only(ctx)
    for source in payload["sources"]:
        sf.verify(source, ctx.cancel)
    for output in payload["outputs"]:
        sf.verify(output["source"], ctx.cancel)
    receipt = {"schema": "raven.reuse-receipt/v1", "identity": payload["identity"], "sources": payload["sources"],
               "outputs": payload["outputs"], "acceptance": payload["acceptance"]}
    def final_check():
        for source in payload["sources"]:
            sf.verify(source, ctx.cancel)
    path = sf.publish(payload["destination"], copies=[(entry["source"], entry["target"]) for entry in payload["outputs"]], receipt=receipt, cancel=ctx.cancel, final_check=final_check)
    return report("reuse", "Verified outputs copied", rows=[path], data={"destination": path, "receipt": receipt})


def _dependency(value):
    from .deps import KINDS, parse_dependency
    if not isinstance(value, str) or len(value) > 16384 or any(c in value for c in "\x00\n\r "):
        raise ValueError("Dependency must use explicit Slurm syntax without whitespace")
    if not value:
        return "", []
    if "," in value and "?" in value:
        raise ValueError("Slurm dependencies cannot mix AND and OR separators")
    for clause in re.split(r"[,?]", value):
        if clause == "singleton":
            continue
        parts = clause.split(":")
        if parts[0] not in KINDS or parts[0] == "singleton" or len(parts) < 2:
            raise ValueError("Unknown dependency kind or missing prerequisite")
        for target in parts[1:]:
            if parts[0] == "after":
                valid = re.fullmatch(r"[0-9]+(?:_[0-9]+)?(?:\+[0-9]+)?", target)
            else:
                valid = re.fullmatch(r"[0-9]+(?:_[0-9]+)?", target)
            if not valid:
                raise ValueError("Dependency targets must be exact job IDs")
    return value, parse_dependency(value)




def _cycles(graph):
    # Iterative three-color traversal avoids recursive-depth failures on long chains.
    color = {}
    for seed in graph:
        if color.get(seed):
            continue
        stack = [(seed, False)]
        while stack:
            node, leaving = stack.pop()
            if leaving:
                color[node] = 2
                continue
            if color.get(node) == 1:
                raise ValueError("Dependency repair would create a cycle")
            if color.get(node) == 2:
                continue
            color[node] = 1
            stack.append((node, True))
            stack.extend((target, False) for target in graph.get(node, ()) if color.get(target) != 2)


def _repair(params, ctx):
    if ctx.replay:
        raise ValueError("Recorded sessions cannot repair live dependencies")
    manifest = _required(params, "manifest")
    doc = read_json(ctx, manifest)
    _schema(doc, "dependency-repair", ("repairs",))
    changes = doc["repairs"]
    if not isinstance(changes, list) or not 1 <= len(changes) <= 128:
        raise ValueError("Repair requires 1–128 exact pending jobs")
    repairs, details = [], {}
    for row in changes:
        checkpoint(ctx)
        if not isinstance(row, dict) or set(row) != {"job_id", "submit_time", "dependency"} or not isinstance(row["job_id"], str) or not JOB_ID.fullmatch(row["job_id"]):
            raise ValueError("Repair entries require job_id, submit_time and dependency")
        jid = row["job_id"]
        if jid in details:
            raise ValueError("A job can be repaired only once per review")
        dependency, _ = _dependency(row["dependency"])
        detail = _details(ctx, jid)
        _owner(ctx, detail)
        if detail.get("JobState") != "PENDING" or detail.get("SubmitTime") != row["submit_time"]:
            raise ValueError(f"Job {jid} is not the declared pending submission; completed/running jobs cannot be repaired")
        details[jid] = detail
        old = _normalized_dependency(detail.get("Dependency", ""))
        if dependency == old:
            raise ValueError(f"Job {jid} already has that dependency")
        repairs.append({"job_id": jid, "attempt": _attempt(detail), "before": old, "after": dependency})
    graph, references = _repair_graph(ctx, repairs, details)
    repairs = _repair_order(repairs, references)
    payload = {"manifest": manifest, "manifest_digest": digest(doc), "repairs": repairs,
               "references": references}
    return report("dependency-repair", f"Dependency repair review: {len(repairs)} pending jobs", rows=[f"{row['job_id']}: {row['before'] or '(none)'} → {row['after'] or '(none)'}" for row in repairs],
                  data={"graph": graph}, plan=prepare_plan(ctx, "dependency-repair", params, payload),
                  warnings=["Updates are sequential, not transactional. A scheduler race can start a pending job before its update. Raven stops after a failure and reports every confirmed or uncertain outcome.",
                            "The plan changes dependency fields only. It does not requeue successful jobs or release administrative holds."])


def _repair_graph(ctx, repairs, known=None):
    from .deps import parse_dependency
    proposed = {entry["job_id"]: entry["after"] for entry in repairs}
    details = dict(known or {})
    queue = list(proposed)
    graph, references = {}, {}
    while queue:
        checkpoint(ctx)
        jid = queue.pop()
        if jid in graph:
            continue
        if len(graph) >= 256:
            raise ValueError("Dependency closure exceeds 256 jobs; split the repair into smaller verified reviews")
        if jid not in details:
            details[jid] = _details(ctx, jid)
        detail = details[jid]
        if not detail.get("SubmitTime") or detail.get("SubmitTime") in {"Unknown", "N/A"}:
            raise ValueError(f"Dependency {jid} lacks a stable attempt identity")
        # Retained successful prerequisites are terminal and cannot form a future cycle.
        terminal = detail.get("JobState") in {"COMPLETED", "CANCELLED", "FAILED", "TIMEOUT", "OUT_OF_MEMORY", "NODE_FAIL", "PREEMPTED", "BOOT_FAIL", "DEADLINE"}
        raw = proposed.get(jid, _normalized_dependency(detail.get("Dependency", "")))
        if not terminal and any(kind == "singleton" for kind, _ in parse_dependency(raw)):
            raise ValueError("Singleton dependencies have implicit name/federation edges; replace them with explicit prerequisites before cycle-verified repair")
        edges = [pre for _, pre in parse_dependency(raw) if pre]
        if any(not JOB_ID.fullmatch(pre) for pre in edges):
            raise ValueError("Dependency graph contains an unsupported compressed job identity")
        graph[jid] = [] if terminal and jid not in proposed else edges
        references[jid] = {"attempt": _attempt(detail), "dependency": _normalized_dependency(detail.get("Dependency", ""))}
        old_edges = [pre for _, pre in parse_dependency(_normalized_dependency(detail.get("Dependency", ""))) if pre]
        queue.extend(pre for pre in (*graph[jid], *old_edges) if pre not in graph)
    _cycles(graph)
    return graph, references


def _repair_order(repairs, references):
    """Find an order whose every intermediate scheduler graph is acyclic."""
    from .deps import parse_dependency
    graph = {jid: [pre for _, pre in parse_dependency(value["dependency"]) if pre]
             for jid, value in references.items()}
    _cycles(graph)
    remaining, ordered = list(repairs), []
    while remaining:
        for row in remaining:
            proposed = dict(graph, **{row["job_id"]: [pre for _, pre in parse_dependency(row["after"]) if pre]})
            try:
                _cycles(proposed)
            except ValueError:
                continue
            ordered.append(row)
            remaining.remove(row)
            graph = proposed
            break
        else:
            raise ValueError("No safe sequential dependency update order exists; split the repair without temporarily releasing constraints")
    return ordered


def _repair_apply(payload, ctx):
    if digest(read_json(ctx, payload["manifest"])) != payload["manifest_digest"]:
        raise ValueError("Dependency repair manifest changed since review")
    current = {}
    for jid, reference in payload["references"].items():
        current[jid] = _details(ctx, jid)
        if _attempt(current[jid]) != reference["attempt"] or _normalized_dependency(current[jid].get("Dependency", "")) != reference["dependency"]:
            raise ValueError(f"Dependency closure changed for {jid}; prepare a new review")
    _repair_graph(ctx, payload["repairs"], current)
    outcomes, attempted = [], set()
    for row in _repair_order(payload["repairs"], payload["references"]):
        jid = row["job_id"]
        try:
            checkpoint(ctx)
            fresh = _details(ctx, jid)
            _owner(ctx, fresh)
            if fresh.get("JobState") != "PENDING" or _attempt(fresh) != row["attempt"] or _normalized_dependency(fresh.get("Dependency", "")) != row["before"]:
                raise ValueError("Job state, attempt or dependency changed immediately before update")
            attempted.add(jid)
            command(ctx, ["scontrol", "update", f"JobId={jid}", f"Dependency={row['after']}"])
            after = _details(ctx, jid)
            if _attempt(after) != row["attempt"] or _normalized_dependency(after.get("Dependency", "")) != row["after"]:
                raise ValueError("Scheduler reply did not confirm the reviewed dependency; reconcile before retry")
            outcomes.append({"job_id": jid, "status": "confirmed", "dependency": row["after"]})
        except Exception as exc:
            from .slurm import CommandError
            rejected = isinstance(exc, CommandError) and bool(re.search(r"\bexit [1-9][0-9]*:", str(exc)))
            outcomes.append({"job_id": jid, "status": "rejected" if rejected else "uncertain" if jid in attempted else "not applied", "error": str(exc)[:500]})
            break
    completed_ids = {row["job_id"] for row in outcomes}
    outcomes.extend({"job_id": row["job_id"], "status": "not applied"} for row in payload["repairs"] if row["job_id"] not in completed_ids)
    success = all(row["status"] == "confirmed" for row in outcomes)
    return report("dependency-repair", "Dependency repair confirmed" if success else "Dependency repair stopped; inspect partial outcomes", status="ok" if success else "partial",
                  rows=[f"{row['job_id']}: {row['status']}" + (f" — {row['error']}" if row.get("error") else "") for row in outcomes], data={"outcomes": outcomes})


def run(feature, params, ctx):
    functions = {"environment": _environment, "placement": _placement, "acceptance": _acceptance,
                 "statistics": _statistics, "reuse": _reuse, "dependency-repair": _repair}
    if feature not in functions:
        raise ValueError("Unknown scientific operation")
    checkpoint(ctx)
    return functions[feature](params, ctx)


def apply(feature, plan, ctx):
    payload = validate_plan(plan, ctx, feature)
    functions = {"environment": _environment_apply, "reuse": _reuse_apply, "dependency-repair": _repair_apply}
    if feature not in functions:
        raise ValueError("This scientific operation is read-only")
    return functions[feature](payload, ctx)


# Managed Raven entry points also protect direct adapter callers.
from .operations import managed_run as _managed_run, managed_apply as _managed_apply
run = _managed_run(run)
apply = _managed_apply(apply)
