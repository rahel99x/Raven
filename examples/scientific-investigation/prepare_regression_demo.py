"""Create an isolated synthetic Git history and real-measurement sweep recipe.

Usage: python3 prepare_regression_demo.py NEW_DIRECTORY
Only the new demonstration directory is modified. No workload is executed.
"""
import json
from pathlib import Path
import subprocess
import sys


def prepare(destination):
    root = Path(destination).absolute()
    root.mkdir(mode=0o700)
    repo = root/"synthetic-project"
    repo.mkdir()
    def git(*argv):
        return subprocess.check_output(["git", "-c", "user.name=Tower example", "-c", "user.email=tower@example.invalid", "-C", str(repo), *argv], text=True, timeout=10).strip()
    git("init", "-q")
    revisions = []
    for name, delay in (("baseline", .005), ("regression", .1), ("recovery", .005)):
        (repo/"measure.py").write_text(
            "import json,math,sys,time\nfrom pathlib import Path\n"
            f"start=time.perf_counter()\ntime.sleep({delay})\n"
            "answer=math.fsum([1e16,1.0,-1e16])\n"
            "Path(sys.argv[1]).write_text(json.dumps({'seconds':time.perf_counter()-start,'scientific_valid':answer==1.0}))\n"
            "print('SCIENTIFIC_CHECK_OK')\n", encoding="utf-8")
        git("add", "measure.py")
        git("commit", "-qm", "Synthetic " + name)
        revisions.append({"id": name, "commit": git("rev-parse", "HEAD")})
    recipe = {"schema": "tower.diagnostic-campaign/v1", "id": "synthetic-git-regression", "mode": "local-diagnostic", "kind": "regression",
              "command": [str(Path(sys.executable).resolve()), "{repo:measure.py}", "{result}"], "files": [],
              "limits": {"max_trials": 30, "trial_seconds": 3, "total_seconds": 45, "cpus": 1, "memory_mb": 256, "output_bytes": 16384, "repeats": 5},
              "oracle": {"exit_code": 0, "contains": "SCIENTIFIC_CHECK_OK"},
              "repository": {"path": str(repo), "commits": revisions, "baseline": "baseline", "minimum_change": .03, "confidence": .95, "metric": "result_seconds"}}
    target = root/"sweep.json"
    target.write_text(json.dumps(recipe, indent=2)+"\n", encoding="utf-8")
    return target


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("Usage: python3 prepare_regression_demo.py NEW_DIRECTORY")
    print(prepare(sys.argv[1]))
