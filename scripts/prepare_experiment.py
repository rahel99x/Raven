#!/usr/bin/env python3
"""Compile or collect a declared factorial experiment; never execute a trial."""
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from raven import experiment_design as E, science_files as F


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("design")
    parser.add_argument("--workspace", action="append", default=[])
    parser.add_argument("--destination", required=True, help="New private output directory; never overwritten")
    args = parser.parse_args(argv)
    try:
        document, source = F.json_file(args.design)
        if args.workspace:
            value = E.collect(document, args.workspace)
            files = [("results.json", json.dumps(value["results"], indent=2).encode(), False),
                     ("collection.json", json.dumps(value, indent=2).encode(), False)]
            if value["recovery_recipe"] is not None:
                files.append(("remaining-recipe.json", json.dumps(value["recovery_recipe"], indent=2).encode(), False))
        else:
            value = E.prepare(document)
            files = [("plan.json", json.dumps(value, indent=2).encode(), False), ("recipe.json", json.dumps(value["recipe"], indent=2).encode(), False)]
        F.publish(args.destination, files=files, receipt={"schema": "raven.factorial-preparation/v1", "design_source": source, "design_sha256": E.digest(document)})
    except (ValueError, OSError, TypeError, KeyError) as exc:
        parser.error(str(exc))
    print(args.destination)


if __name__ == "__main__": main()
