#!/usr/bin/env python3
"""Controlled, unpublished W42 extraction comparison; data harness, not Raven code.

Run only in the coordinator's exclusive slot after the final prototype passed.
Both wheels execute the unchanged workflow's common `complete` path. Minimal
AST instrumentation adds phase markers and observations, not behavior changes.
"""
from __future__ import annotations

import argparse
import ast
import datetime
import hashlib
import json
import os
from pathlib import Path
import platform
import statistics
import subprocess
import sys
import time
import zipfile


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def utc():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def instrument(source):
    tree = ast.parse(source)
    top_with = [node for node in tree.body if isinstance(node, ast.With)]
    assert len(top_with) == 1
    scope = top_with[0]
    import_index = tree.body.index(scope)
    tree.body[import_index:import_index] = ast.parse('''
_bench_import_seconds = time.perf_counter() - begin
_bench_import_cpu_seconds = time.process_time() - _bench_cpu_start
_bench_import_count = len([name for name in sys.modules if name == "raven" or name.startswith("raven.")])
''').body
    begin_index = next(i for i, node in enumerate(tree.body)
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "begin" for t in node.targets))
    tree.body[begin_index:begin_index] = ast.parse('''
_bench_allocations = os.environ.get("RAVEN_W42_ALLOCATIONS") == "1"
if _bench_allocations:
    import tracemalloc
    tracemalloc.start()
_bench_cpu_start = time.process_time()
''').body
    frame_index = next(i for i, node in enumerate(scope.body)
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Attribute) and node.value.func.attr == "compose")
    scope.body[frame_index + 1:frame_index + 1] = ast.parse('''
_bench_first_frame_seconds = time.perf_counter() - begin
_bench_first_frame_cpu_seconds = time.process_time() - _bench_cpu_start
_bench_rows = [[list(segment) for segment in row] for row in rows]
_bench_job_fields = ("id", "name", "partition", "state", "elapsed", "limit", "nodes", "cpus", "gpu_type", "gpus", "nodelist", "mem_req", "reason", "priority", "dependency", "account", "qos", "user")
_bench_jobs = [{field: getattr(job, field) for field in _bench_job_fields} for job in sorted(store.jobs, key=lambda job: job.id)]
''').body
    print_node = scope.body[-1]
    assert isinstance(print_node, ast.Expr) and isinstance(print_node.value, ast.Call)
    assert isinstance(print_node.value.func, ast.Name) and print_node.value.func.id == "print"
    payload = print_node.value.args[0].args[0]
    assert isinstance(payload, ast.Dict)
    scope.body[-1:-1] = ast.parse('''
_bench_self = resource.getrusage(resource.RUSAGE_SELF)
_bench_children = resource.getrusage(resource.RUSAGE_CHILDREN)
_bench_python_peak = tracemalloc.get_traced_memory()[1] if _bench_allocations else None
_bench_io = {}
try:
    for _bench_line in Path("/proc/self/io").read_text().splitlines():
        _bench_key, _bench_value = _bench_line.split(":", 1)
        _bench_io[_bench_key] = int(_bench_value.strip())
except OSError:
    pass
''').body
    extra = ast.parse('''{
"instrumentation": "phase markers; separate tracemalloc runs; common complete path",
"phase": {
    "raven_import_seconds": _bench_import_seconds,
    "raven_import_cpu_seconds": _bench_import_cpu_seconds,
    "first_frame_seconds": _bench_first_frame_seconds,
    "first_frame_cpu_seconds": _bench_first_frame_cpu_seconds,
    "workflow_cpu_seconds": time.process_time() - _bench_cpu_start,
    "raven_import_count_after_imports": _bench_import_count,
    "process_cpu_seconds": _bench_self.ru_utime + _bench_self.ru_stime,
    "reaped_children_cpu_seconds": _bench_children.ru_utime + _bench_children.ru_stime,
    "largest_reaped_child_peak_rss_kib": _bench_children.ru_maxrss,
    "python_peak_allocated_bytes": _bench_python_peak,
    "process_io": _bench_io
},
"installed_raven_path": sys.modules["raven"].__file__,
"semantic_fixture": {
    "jobs": _bench_jobs, "frame_dimensions": [80, 24],
    "frame_row_count": len(rows), "run_state": home["inventory"]["state"],
    "log_lines": list(logs.lines), "worker_result": result["result"],
    "config_bytes": original.decode("utf-8"),
    "fake_scheduler_calls": scheduler.b.calls
},
"frame_rows": _bench_rows,
"allocation_run": _bench_allocations
}''', mode="eval").body
    payload.keys.extend(extra.keys)
    payload.values.extend(extra.values)
    return ast.unparse(ast.fix_missing_locations(tree)) + "\n"


def summary(values):
    return {"count": len(values), "median": statistics.median(values), "minimum": min(values), "maximum": max(values)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--prototype", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--pairs", type=int, default=8)
    args = parser.parse_args()
    assert 4 <= args.pairs <= 20
    output = args.output.absolute()
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    original = args.source / "scripts/distribution/workflow_probe.py"
    manifest = json.loads((args.prototype / "manifest.json").read_text())
    assert manifest["status"] == "passed-experimental-extraction"
    assert all(digest(args.source / name) == expected for name, expected in manifest["source_sha256"].items()), "Prototype must use final current runtime bytes"
    runtime_revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=args.source, text=True).strip()
    child = output / "instrumented_workflow.py"
    child.write_text(instrument(original.read_text()))
    env = dict(os.environ, PYTHONHASHSEED="0", PYTHONNOUSERSITE="1", PYTHONDONTWRITEBYTECODE="1", LANG="C.UTF-8", LC_ALL="C.UTF-8", TZ="UTC", TERM="xterm-256color", COLUMNS="80", LINES="24")
    for key in ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "RAVEN_W42_ALLOCATIONS"):
        env.pop(key, None)
    evidence = {
        "schema": "raven.distribution-controlled/v1", "started_utc": utc(),
        "runtime_revision": runtime_revision,
        "source_sha256": manifest["source_sha256"],
        "harness_sha256": digest(__file__), "workflow_sha256": digest(original),
        "instrumented_workflow_sha256": digest(child),
        "prototype_manifest_sha256": digest(args.prototype / "manifest.json"),
        "host": {"platform": platform.platform(), "python": sys.version, "cpu_affinity": sorted(os.sched_getaffinity(0))},
        "environment_overrides": {key: env[key] for key in ("PYTHONHASHSEED", "PYTHONNOUSERSITE", "PYTHONDONTWRITEBYTECODE", "LANG", "LC_ALL", "TZ", "TERM", "COLUMNS", "LINES")},
        "method": {
            "warmup_pairs": 2, "measured_pairs": args.pairs, "allocation_pairs": 2,
            "order": "alternate full/core, core/full each pair; each observation a fresh process",
            "cache": "Warm filesystem/page cache after two warmup pairs; no OS cache flush; installed bytecode generated by pip",
            "timing": "Untraced runs only. Parent process wall includes interpreter, marker harness and complete actual-local workflow. Import and first-frame phases begin before Raven imports.",
            "memory": "Process and largest reaped child peak RSS in KiB; child peaks are not simultaneous aggregate memory. Parent-only Python allocation peaks use separate fresh tracemalloc runs and are never RSS.",
            "equivalence": "Same existing workflow_probe.py common complete path and stable logical jobs/logs/run/worker/config predicates; raw rendered rows retained, dynamic timestamps not erased from raw evidence.",
            "limits": ["Experimental unpublished source extraction; not a small-core claim", "Synthetic scheduler, actual local launch and managed worker; no live Slurm, GPU or human evidence", "Small within-host series, not population confidence intervals or general cold-start claims", "Process I/O counters exclude child processes; block bytes may be zero under warm cache", "Coordinator-exclusive measurement window; host background activity remains uncontrolled"]
        },
        "installations": {}, "observations": [], "commands": []
    }
    (output / "original_workflow.py").write_bytes(original.read_bytes())
    (output / "harness.py").write_bytes(Path(__file__).read_bytes())

    def run(argv, *, command_env=env, timeout=60):
        start = utc()
        then = time.perf_counter()
        result = subprocess.run([str(x) for x in argv], env=command_env, cwd=output, text=True, capture_output=True, timeout=timeout)
        duration = time.perf_counter() - then
        entry = {"argv": [str(x) for x in argv], "started_utc": start, "finished_utc": utc(), "process_wall_seconds": duration, "returncode": result.returncode}
        evidence["commands"].append(entry)
        if result.returncode:
            (output / "failure.stdout").write_text(result.stdout)
            (output / "failure.stderr").write_text(result.stderr)
            (output / "failed.json").write_text(json.dumps(evidence, indent=2) + "\n")
            raise RuntimeError(f"Command failed: {entry}; see retained stdout/stderr")
        return result, entry

    interpreters = {}
    for kind in ("full", "core"):
        wheel, = (args.prototype / kind / "dist").glob("*.whl")
        venv = output / (kind + "-venv")
        run([sys.executable, "-m", "venv", venv])
        python = venv / "bin/python"
        run([python, "-m", "pip", "install", "--no-index", "--no-deps", wheel])
        run([python, "-m", "pip", "check"])
        interpreters[kind] = python
        raven, = venv.glob("lib/python*/site-packages/raven")
        with zipfile.ZipFile(wheel) as archive:
            members = [entry for entry in archive.infolist() if entry.filename.startswith("raven/") and not entry.is_dir()]
            assert all(digest(raven.parent / entry.filename) == hashlib.sha256(archive.read(entry.filename)).hexdigest() for entry in members)
        files = [path for path in raven.rglob("*") if path.is_file()]
        evidence["installations"][kind] = {
            "python": str(python), "wheel": str(wheel), "wheel_sha256": digest(wheel), "wheel_bytes": wheel.stat().st_size,
            "shipped_raven_bytes": sum(entry.file_size for entry in members),
            "python_modules": sum(entry.filename.endswith(".py") for entry in members),
            "installed_raven_logical_bytes": sum(path.stat().st_size for path in files),
            "installed_raven_allocated_bytes": sum(path.stat().st_blocks * 512 for path in files),
            "footprint_scope": "Raven package only, including installed bytecode; excludes interpreter, pip and stdlib",
            "installed_source_matches_wheel": True
        }

    semantic = None
    sample_directory = output / "observations"
    sample_directory.mkdir()
    for phase, pairs in (("warmup", 2), ("measured", args.pairs), ("allocations", 2)):
        for pair in range(pairs):
            for position, kind in enumerate(("full", "core") if pair % 2 == 0 else ("core", "full")):
                child_env = dict(env, RAVEN_W42_ALLOCATIONS="1" if phase == "allocations" else "0")
                result, command = run([interpreters[kind], "-s", child, "complete"], command_env=child_env, timeout=30)
                name = f"{phase}-{pair:02d}-{position}-{kind}"
                (sample_directory / (name + ".stdout.txt")).write_text(result.stdout)
                (sample_directory / (name + ".stderr.txt")).write_text(result.stderr)
                (output / "in-progress.json").write_text(json.dumps(evidence, indent=2) + "\n")
                record = json.loads(result.stdout)
                assert record["mode"] == "complete"
                assert str(output / (kind + "-venv")) in record["installed_raven_path"], "Source checkout shadowed installed wheel"
                assert record["checks"] == ["80-column fake monitor", "actual local launch", "retained logs", "exact run home", "installed Heavy child", "plugin API", "config preserved"]
                fixture = json.dumps(record["semantic_fixture"], sort_keys=True, separators=(",", ":"))
                if semantic is None:
                    semantic = fixture
                assert fixture == semantic, "Full/core observations did not execute equivalent common fixtures"
                record.update({"profile": kind, "phase_kind": phase, "pair": pair, "position": position,
                    "process_wall_seconds": command["process_wall_seconds"], "started_utc": command["started_utc"], "finished_utc": command["finished_utc"],
                    "semantic_sha256": hashlib.sha256(fixture.encode()).hexdigest(),
                    "raven_import_count_after_workflow": len(record["raven_imports"])})
                (sample_directory / (name + ".json")).write_text(json.dumps(record, indent=2) + "\n")
                evidence["observations"].append({key: value for key, value in record.items() if key not in ("frame_rows", "semantic_fixture", "raven_imports")})

    metrics = {
        "process_wall_seconds": lambda r: r["process_wall_seconds"],
        "raven_import_seconds": lambda r: r["phase"]["raven_import_seconds"],
        "first_frame_seconds": lambda r: r["phase"]["first_frame_seconds"],
        "workflow_seconds": lambda r: r["elapsed_seconds"],
        "process_cpu_seconds": lambda r: r["phase"]["process_cpu_seconds"],
        "reaped_children_cpu_seconds": lambda r: r["phase"]["reaped_children_cpu_seconds"],
        "peak_process_rss_kib": lambda r: r["peak_process_rss_kib"],
        "largest_reaped_child_peak_rss_kib": lambda r: r["phase"]["largest_reaped_child_peak_rss_kib"],
        "raven_import_count_after_imports": lambda r: r["phase"]["raven_import_count_after_imports"],
        "raven_import_count_after_workflow": lambda r: r["raven_import_count_after_workflow"],
        "read_bytes": lambda r: r["phase"]["process_io"].get("read_bytes", 0),
        "write_bytes": lambda r: r["phase"]["process_io"].get("write_bytes", 0),
        "read_characters": lambda r: r["phase"]["process_io"].get("rchar", 0),
        "write_characters": lambda r: r["phase"]["process_io"].get("wchar", 0)
    }
    measured = [r for r in evidence["observations"] if r["phase_kind"] == "measured"]
    evidence["comparison"] = {}
    for metric, getter in metrics.items():
        full = [getter(r) for r in measured if r["profile"] == "full"]
        core = [getter(r) for r in measured if r["profile"] == "core"]
        evidence["comparison"][metric] = {
            "full": summary(full), "core": summary(core),
            "paired_core_minus_full": summary([b - a for a, b in zip(full, core)]),
            "median_change_percent": 100 * (statistics.median(core) / statistics.median(full) - 1) if statistics.median(full) else None
        }
    evidence["separate_python_allocations"] = {kind: summary([r["phase"]["python_peak_allocated_bytes"] for r in evidence["observations"] if r["phase_kind"] == "allocations" and r["profile"] == kind]) for kind in ("full", "core")}
    assert all(digest(args.source / name) == expected for name, expected in manifest["source_sha256"].items()), "Runtime changed during controlled measurement"
    evidence["runtime_source_unchanged"] = True
    evidence["semantic_equivalence"] = {"all_equal": True, "sha256": hashlib.sha256(semantic.encode()).hexdigest()}
    evidence["finished_utc"] = utc()
    (output / "report.json").write_text(json.dumps(evidence, indent=2) + "\n")
    print(json.dumps({"status": "passed", "output": str(output), "runtime_revision": runtime_revision, "observations": len(evidence["observations"]), "comparison": evidence["comparison"], "separate_python_allocations": evidence["separate_python_allocations"]}))


if __name__ == "__main__":
    main()
