"""Reconcile exact handoff clauses with package evidence without changing scope."""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path


def reconcile(directory):
    path = directory / "coverage.json"
    ledger = json.loads(path.read_text())
    for item in ledger["source_manifest"]:
        raw = (directory / item["file"]).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == item["sha256"], item["file"]
        assert len(raw) == item["bytes"] and item["complete"]
    total = Counter()
    for package in ledger["packages"]:
        status_path = directory / "evidence" / (package["id"] + "-status.json")
        status = json.loads(status_path.read_text())
        assert status["package"] == package["id"], status_path
        requirements = status["requirements"]
        if isinstance(requirements, dict):
            requirements = [dict(value, id=key) if isinstance(value, dict)
                            else {"id": key, "state": value}
                            for key, value in requirements.items()]
        by_id = {item["id"]: item for item in requirements}
        assert len(by_id) == len(requirements), (status_path, "duplicate clause")
        assert set(by_id) == {item["id"] for item in package["requirements"]}, status_path
        updated = []
        for original in package["requirements"]:
            evidence = dict(by_id[original["id"]])
            evidence["state"] = evidence.pop("status", evidence.get("state", "pending"))
            assert evidence["state"] in {"implemented", "already_satisfied", "adjusted",
                                         "rejected", "blocked", "pending", "in_progress"}, evidence
            evidence["text"] = original["text"]
            updated.append(evidence)
            total[evidence["state"]] += 1
        package["requirements"] = updated
        package["package_status_file"] = str(status_path.relative_to(directory))
        package["validation"] = status.get("validation", "See package evidence")
        package["qualification_limits"] = status.get("limits", status.get("qualification_limits", []))
        # Integrated revision gates are root-owned; a focused result must not
        # silently promote a whole package to final acceptance.
        if not package.get("tested_revision"):
            package["state"] = "in_progress"
            package["verification"] = "focused_evidence_recorded_final_integration_pending"
            package["next_action"] = "Complete final integrated gates and any explicit remaining qualification."
    assert len(ledger["packages"]) == 44 and sum(total.values()) == 371
    path.write_text(json.dumps(ledger, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps({"packages": 44, "clauses": 371, "dispositions": dict(total)}, sort_keys=True))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=Path(__file__).resolve().parent)
    reconcile(parser.parse_args().directory)
