"""Copy a checkout into a private, fingerprinted review-qualification snapshot."""
from pathlib import Path
import argparse
import hashlib
import json
import shutil
import subprocess

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("source", type=Path)
parser.add_argument("snapshot", type=Path)
args = parser.parse_args()
source = args.source.resolve(strict=True)
dest = args.snapshot.absolute()
dest.mkdir(parents=True, exist_ok=False)
shutil.copytree(source / "raven", dest / "raven", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
shutil.copytree(source / "scripts/slurm", dest / "scripts/slurm")
original = (source / "scripts/qualify_slurm.py").read_text()
scenario = '\"forecast-observations\")'
revision = 'revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()'
if original.count(scenario) != 1 or original.count(revision) != 1:
    raise SystemExit("The source harness changed; reconcile the two documented snapshot adaptations before running.")
script = original.replace(scenario, '\"forecast-observations\", \"review-findings\")')
script = script.replace(revision, 'revision = os.environ["RAVEN_QUALIFICATION_REVISION"]')
(dest / "scripts/qualify_slurm.py").write_text(script)
shutil.copy(Path(__file__).with_name("review_findings_driver.py"), dest / "scripts/slurm/review_findings.py")
files = {str(p.relative_to(dest)): hashlib.sha256(p.read_bytes()).hexdigest()
         for p in sorted((dest / "raven").rglob("*")) if p.is_file()}
base = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True).strip()
manifest = {"schema": "raven.qualification-source-snapshot/v1", "base_revision": base,
            "files": files, "sha256": hashlib.sha256(json.dumps(files, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
            "driver_sha256": hashlib.sha256((dest / "scripts/slurm/review_findings.py").read_bytes()).hexdigest(),
            "harness": "Existing harness plus review-findings choice and explicit revision; no product code changes."}
(dest / "source-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
print(json.dumps({"snapshot": str(dest), "revision_environment": {"RAVEN_QUALIFICATION_REVISION": base},
                  "command": ["python3", str(dest / "scripts/qualify_slurm.py"), "--version", "26.05.5", "--skip-build", "--scenario", "review-findings", "--output", str(dest / "evidence/26.05.5")]}, indent=2))
