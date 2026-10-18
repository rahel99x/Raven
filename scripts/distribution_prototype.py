#!/usr/bin/env python3
"""Build and qualify unpublished core/lab extraction wheels in private venvs."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import tarfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
LAB = frozenset({"ops_science", "ops_investigation", "ops_runtime", "ops_trace", "science_stats", "science_diagnostics",
                 "scientific_lineage", "science_fields", "trace_analysis", "execution_simulator", "investigation_planner", "investigation_workflow", "ops_signal_analysis", "signal_inputs", "phase_alignment", "experiment_design"})


def stage(output):
    from raven.operations import _registry
    registry = _registry()
    mapping = {key: module.__name__ for key, (module, _spec) in registry.items() if module.__name__.removeprefix("raven.") in LAB}
    from raven import __version__
    version = __version__
    profile = {"profile": "core-experiment", "api": 1, "optional_modules": ["raven." + name for name in sorted(LAB)], "operations": mapping}
    snapshot = {}
    for source in (ROOT / "raven").rglob("*"):
        if source.is_file() and "__pycache__" not in source.parts and source.suffix in (".py", ".typed"):
            before = source.stat()
            data = source.read_bytes()
            after = source.stat()
            if (before.st_ino, before.st_mtime_ns, before.st_size) != (after.st_ino, after.st_mtime_ns, after.st_size):
                raise ValueError("Source changed during prototype capture; retry after integration is stable")
            snapshot[source.relative_to(ROOT)] = data
    if sum(map(len, snapshot.values())) > 32 << 20:
        raise ValueError("Prototype source capture exceeds 32 MiB")
    for kind in ("full", "core", "lab"):
        target = output / kind
        target.mkdir()
        for name in ("LICENSE", "README.md"):
            shutil.copy2(ROOT / name, target / name)
        for relative, data in snapshot.items():
            lab_file = relative.parent == Path("raven") and relative.stem in LAB
            if kind != "full" and (kind == "lab") != lab_file:
                continue
            destination = target / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
        dependencies = f'["raven-core-experiment=={version}"]' if kind == "lab" else "[]"
        metadata = f'''[build-system]\nrequires = ["setuptools>=77"]\nbuild-backend = "setuptools.build_meta"\n[project]\nname = "raven-{kind}-experiment"\nversion = "{version}"\nrequires-python = ">=3.10"\ndescription = "Unpublished Raven core/lab qualification prototype"\nlicense = "MIT"\ndependencies = {dependencies}\n[tool.setuptools.packages.find]\ninclude = ["raven", "raven.*"]\nnamespaces = true\n[tool.setuptools.package-data]\nraven = ["_distribution_profile.json"]\n"raven.transport" = ["py.typed"]\n'''
        if kind == "core":
            metadata += '\n[project.scripts]\nraven = "raven.raven_launch:light_main"\nravenl = "raven.raven_launch:light_main"\nravenh = "raven.raven_launch:heavy_main"\n'
            (target / "raven/_distribution_profile.json").write_text(json.dumps(profile, indent=2) + "\n")
        if kind == "full":
            metadata = (ROOT / "pyproject.toml").read_text()
        (target / "pyproject.toml").write_text(metadata)
    return profile, {str(path): hashlib.sha256(data).hexdigest() for path, data in snapshot.items()}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--stage-only", action="store_true")
    args = parser.parse_args(argv)
    output = args.output.absolute()
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    profile, source_hashes = stage(output)
    evidence = {"schema": "raven.distribution-prototype/v1", "source_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "experimental": True, "published": False, "production_rename": False, "profile": profile,
        "source_sha256": source_hashes,
        "limits": ["W44 human workflow evidence is unavailable", "This first extraction is not the proposed few-dozen-module core", "No coinstallation with the production raven wheel; private venv only"]}
    if args.stage_only:
        (output / "manifest.json").write_text(json.dumps(evidence, indent=2) + "\n")
        return 0
    env = dict(os.environ, PYTHONNOUSERSITE="1")
    env.pop("PYTHONPATH", None)
    def run(argv, cwd=output):
        return subprocess.run([str(a) for a in argv], cwd=cwd, env=env, text=True, capture_output=True, check=True, timeout=180)
    try:
        wheels = {}
        members = {}
        for kind in ("full", "core", "lab"):
            result = run([sys.executable, "-m", "build", "--no-isolation"], output / kind)
            (output / (kind + "-build.log")).write_text(result.stdout + result.stderr)
            wheel, = (output / kind / "dist").glob("*.whl")
            wheels[kind] = wheel
            with zipfile.ZipFile(wheel) as archive:
                members[kind] = {name for name in archive.namelist() if name.startswith("raven/")}
                wheel_sources = {name: hashlib.sha256(archive.read(name)).hexdigest() for name in members[kind]}
                evidence[kind] = {"wheel_bytes": wheel.stat().st_size, "wheel_sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
                    "shipped_bytes": sum(item.file_size for item in archive.infolist() if item.filename.startswith("raven/")),
                    "python_modules": len([name for name in members[kind] if name.endswith(".py")])}
            sdist, = (output / kind / "dist").glob("*.tar.gz")
            with tarfile.open(sdist) as archive:
                source_members = {"raven/" + member.name.split("/raven/", 1)[1]: hashlib.sha256(archive.extractfile(member).read()).hexdigest()
                                  for member in archive if member.isfile() and "/raven/" in member.name}
            assert source_members == wheel_sources, "Wheel and sdist package contents differ"
            evidence[kind]["sdist_bytes"] = sdist.stat().st_size
            evidence[kind]["sdist_sha256"] = hashlib.sha256(sdist.read_bytes()).hexdigest()
        assert not members["core"] & members["lab"], "Core/lab wheels must not own overlapping files"
        assert "raven/transport/framing.py" in members["core"] and "raven/transport/py.typed" in members["core"]
        assert "raven/_distribution_profile.json" in members["core"]
        assert all("raven/" + name + ".py" in members["lab"] for name in LAB)
        # A second unpublished package revision exercises real pip upgrade and
        # downgrade of both distributions. Source content stays identical so
        # this isolates packaging compatibility from an application migration.
        from raven import __version__
        for kind in ("core", "lab"):
            target = output / ("next-" + kind)
            shutil.copytree(output / kind, target, ignore=shutil.ignore_patterns("dist", "build", "*.egg-info"))
            metadata = target / "pyproject.toml"
            metadata.write_text(metadata.read_text().replace(__version__, __version__ + "+qualification1"))
            result = run([sys.executable, "-m", "build", "--no-isolation", "--wheel"], target)
            (output / ("next-" + kind + "-build.log")).write_text(result.stdout + result.stderr)
            wheel, = (target / "dist").glob("*.whl")
            wheels["next-" + kind] = wheel
        baseline_venv = output / "baseline-venv"
        run([sys.executable, "-m", "venv", str(baseline_venv)])
        baseline_python = baseline_venv / "bin/python"
        run([baseline_python, "-m", "pip", "install", "--no-index", "--no-deps", wheels["full"]])
        baseline = run([baseline_python, ROOT / "scripts/distribution/workflow_probe.py", "complete"])
        evidence["baseline_workflow"] = json.loads(baseline.stdout.strip().splitlines()[-1])
        venv = output / "venv"
        run([sys.executable, "-m", "venv", str(venv)])
        python = venv / "bin/python"
        run([python, "-m", "pip", "install", "--no-index", "--no-deps", wheels["core"]])
        persistent = output / "user-data"
        persistent.mkdir()
        (persistent / "config.json").write_text('{"account":"labx","user_setting":"retained"}\n')
        (persistent / "recording.csv").write_text('time,cpu\n1,0.5\n')
        retained = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in persistent.iterdir()}
        evidence["journeys"] = []
        for mode in ("core", "lab", "core-after-removal", "lab-after-reinstall", "lab-after-upgrade", "lab-after-downgrade"):
            if mode in ("lab", "lab-after-reinstall"):
                run([python, "-m", "pip", "install", "--no-index", "--no-deps", wheels["lab"]])
            elif mode == "core-after-removal":
                run([python, "-m", "pip", "uninstall", "-y", "raven-lab-experiment"])
            elif mode in ("lab-after-upgrade", "lab-after-downgrade"):
                prefix = "next-" if mode == "lab-after-upgrade" else ""
                if mode == "lab-after-upgrade":
                    mismatch = subprocess.run([str(python), "-m", "pip", "install", "--no-index", str(wheels["next-lab"])],
                        cwd=output, env=env, text=True, capture_output=True, timeout=60)
                    assert mismatch.returncode != 0, "Mismatched extension must require its exact core version"
                    evidence["mismatched_extension_refused"] = True
                run([python, "-m", "pip", "install", "--no-index", wheels[prefix + "core"], wheels[prefix + "lab"]])
            run([python, "-m", "pip", "check"])
            for command in ("raven", "ravenl", "ravenh"):
                from raven import __version__
                assert __version__ in run([venv / "bin" / command, "--version"]).stdout
            result = run([python, ROOT / "scripts/distribution/workflow_probe.py", "lab" if mode.startswith("lab") else "core"])
            observation = json.loads(result.stdout.strip().splitlines()[-1])
            observation["phase"] = mode
            observation["installed_versions"] = json.loads(run([python, "-c", "import importlib.metadata as m,json;print(json.dumps({name:m.version(name) for name in ('raven-core-experiment','raven-lab-experiment') if any(x.metadata['Name']==name for x in m.distributions())}))"]).stdout)
            installed_paths = [p for folder in venv.glob("lib/python*/site-packages/raven") for p in folder.rglob("*") if p.is_file()]
            observation["installed_raven_logical_bytes"] = sum(p.stat().st_size for p in installed_paths)
            observation["installed_raven_disk_bytes"] = sum(p.stat().st_blocks * 512 for p in installed_paths)
            evidence["journeys"].append(observation)
            assert retained == {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in persistent.iterdir()}
        # Logical installed bytes and disk blocks are different quantities.
        for kind, installation in (("full", baseline_venv), ("core-plus-lab", venv)):
            paths = [p for folder in installation.glob("lib/python*/site-packages/raven") for p in folder.rglob("*") if p.is_file()]
            evidence.setdefault("installed", {})[kind] = {
                "logical_bytes": sum(p.stat().st_size for p in paths),
                "disk_allocated_bytes": sum(p.stat().st_blocks * 512 for p in paths),
                "scope": "Raven files only; excludes interpreter/pip/stdlib"}
        evidence["status"] = "passed-experimental-extraction"
    except (OSError, AssertionError, subprocess.SubprocessError) as exc:
        evidence["status"] = "failed"
        evidence["error"] = str(exc)
        if isinstance(exc, subprocess.CalledProcessError):
            evidence["stdout"], evidence["stderr"] = exc.stdout, exc.stderr
    (output / "manifest.json").write_text(json.dumps(evidence, indent=2) + "\n")
    print(json.dumps({key: evidence.get(key) for key in ("status", "error", "core", "lab")}))
    return int(evidence.get("status") != "passed-experimental-extraction")


if __name__ == "__main__":
    raise SystemExit(main())
