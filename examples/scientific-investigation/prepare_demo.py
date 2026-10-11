"""Create portable, hashed diagnostic recipes and analysis examples offline.

Usage: python3 prepare_demo.py NEW_DIRECTORY
This writes recipes and sources; it does not execute or submit work.
"""
import hashlib
import json
from pathlib import Path
import shutil
import sys
import time


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode()).hexdigest()


def prepare(destination):
    destination = Path(destination).absolute()
    destination.mkdir(mode=0o700)
    source = Path(__file__).parent
    for name in ("oracle.py", "input.txt", "numerical_program.py"):
        shutil.copyfile(source/name, destination/name)
    def pinned(name):
        return {"name": name, "path": name, "sha256": hashlib.sha256((destination/name).read_bytes()).hexdigest()}
    limits = {"max_trials": 40, "trial_seconds": 2, "total_seconds": 30, "cpus": 1,
              "memory_mb": 256, "output_bytes": 16384, "repeats": 2}
    common = {"schema": "tower.diagnostic-campaign/v1", "mode": "local-diagnostic", "limits": limits}
    reproducer = {**common, "id": "synthetic-minimizer", "kind": "minimize",
                  "command": [str(Path(sys.executable).resolve()), "{file:oracle.py}", "{input}"],
                  "files": [pinned("oracle.py"), pinned("input.txt")], "input": "input.txt",
                  "oracle": {"exit_code": 7, "contains": "TOWER_DEMO_TARGET"}}
    numerical = {**common, "id": "synthetic-roundoff", "kind": "campaign",
                 "command": [str(Path(sys.executable).resolve()), "{file:numerical_program.py}"],
                 "files": [pinned("numerical_program.py")],
                 "cases": [{"id": mode, "arguments": [mode, "{result}"]} for mode in ("naive", "compensated")],
                 "oracle": {"exit_code": 0, "contains": "TOWER_DEMO_MEASURED"}}
    planner = {"schema": "tower.investigation-plan/v1", "budget": {"seconds": 100, "resource_seconds": 200, "trials": 2},
               "known_evidence": ["pilot"], "risk_limit": 0, "actions": [
                   {"id": "accuracy-audit", "kind": "scientific-validation", "seconds": 20, "resource_seconds": 20,
                    "queue_seconds": 0, "decision_gain": .8, "success_probability": .9, "scientific_risk": 0,
                    "requires": ["pilot"], "model": "illustrative user-declared decision model; not measured probabilities"},
                   {"id": "repeat-timing", "kind": "repeat", "seconds": 30, "resource_seconds": 60,
                    "queue_seconds": 0, "decision_gain": .3, "success_probability": .9, "scientific_risk": 0,
                    "requires": ["pilot"], "model": "illustrative user-declared decision model; not measured probabilities"}]}
    identity = {"cluster": "synthetic", "job_id": "demo", "attempt": "example"}
    data = {"communication_fraction": .7}
    contract = {"id": "test-fewer-ranks", "identity": identity, "created": time.time(), "expires": time.time()+86400,
                "evidence": [{"id": "pilot", "sha256": digest(data)}],
                "preconditions": [{"source": "pilot", "path": ["communication_fraction"], "op": "gt", "value": .5}],
                "acceptance": [{"source": "measured", "path": ["scientific_error"], "op": "le", "value": .01}],
                "intervention": "Prepare a matched fewer-ranks scientific experiment; synthetic example only"}
    recommendation = {"schema": "tower.recommendation-review/v1", "contract": contract,
                      "snapshot": {"identity": identity, "evidence": [{"id": "pilot", "sha256": digest(data), "data": data, "complete": True}]}}
    lineage = {"schema": "tower.scientific-lineage/v1", "nodes": [
        {"id": "raw", "kind": "artifact", "dependencies": [], "complete": True},
        {"id": "parser", "kind": "policy", "dependencies": [], "complete": True},
        {"id": "claim", "kind": "claim", "dependencies": ["raw", "parser"], "complete": True}],
        "changed": ["parser"], "actions": [
            {"id": "reparse-validate", "covers": ["parser", "claim"], "requires": ["raw"], "cost": 2, "method": "correct parser; check declared scientific observable"}]}
    regression = {"schema": "tower.performance-investigation/v1", "unit": "synthetic independent unit", "design": "paired",
                  "baseline": "baseline", "minimum_change": .5, "versions": [
                      {"id": name, "scientific_valid": True, "samples": [{"id": str(i), "value": value} for i, value in enumerate(samples)]}
                      for name, samples in [("baseline", [10, 10.1, 9.9, 10.2]), ("slower", [15, 15.2, 15.1, 15.4]), ("recovered", [8, 8.2, 7.9, 8.1])]]}
    for name, value in (("minimize.json", reproducer), ("campaign.json", numerical), ("planner.json", planner),
                        ("recommendation.json", recommendation), ("lineage.json", lineage), ("regression.json", regression)):
        (destination/name).write_text(json.dumps(value, indent=2)+"\n", encoding="utf-8")
    return destination


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("Usage: python3 prepare_demo.py NEW_DIRECTORY")
    print(prepare(sys.argv[1]))
