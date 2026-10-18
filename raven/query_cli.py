"""Bounded read-only SQL through the same admitted evidence operation as the UI."""
import argparse
import json
import sys

from . import evidence_index as I, operations as O


def main(argv=None):
    parser = argparse.ArgumentParser(prog="raven query", description="Query an existing evidence index on the admitted worker's filesystem; no scheduler mutations.")
    parser.add_argument("sql", nargs="?", help="One read-only SELECT; defaults to the named view")
    parser.add_argument("--index", required=True, help="Index directory created by Operations → Query retained measurements")
    parser.add_argument("--view", choices=tuple(I.VIEWS), default="sources")
    args = parser.parse_args(argv)
    try:
        result = O.run("evidence-query", {"index": args.index, "sql": args.sql or "", "view": args.view}, O.Context())
        print(json.dumps(result, ensure_ascii=True, allow_nan=False, indent=2))
        return 3 if result.get("status") == "deferred" or result.get("data", {}).get("status") == "partial" else 0
    except (OSError, ValueError, TypeError) as exc:
        print("Raven query: " + str(exc), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
