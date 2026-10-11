"""Convert completed demo trial measurements to Tower's numerical analysis schema."""
import json
from pathlib import Path
import sys


def collect(workspace):
    root = Path(workspace)
    recipe = json.loads((root/"recipe.json").read_text())
    receipt = json.loads((root/"result.json").read_text())
    if receipt["state"] != "completed" or recipe["kind"] != "campaign":
        raise ValueError("Use a completed numerical demo campaign")
    runs = []
    repeats = recipe["limits"]["repeats"]
    for index, trial in enumerate(receipt["trials"]):
        if not trial["oracle_matched"] or trial["state"] != "finished":
            raise ValueError("A demo trial did not produce its measurement marker")
        measured = json.loads((root/"trials"/f"{trial['sequence']:04d}"/"result.json").read_text())
        runs.append({"id": f"trial-{trial['sequence']}", "configuration": recipe["cases"][index//repeats]["id"], "metrics": {"answer": measured["answer"]}})
    return {"schema": "tower.numerical-stability/v1", "metrics": [{"name": "answer", "reference": 1, "absolute": 0, "relative": 0}], "runs": runs}


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("Usage: python3 collect_numerical.py WORKSPACE > numerical.json")
    print(json.dumps(collect(sys.argv[1]), indent=2, allow_nan=False))
