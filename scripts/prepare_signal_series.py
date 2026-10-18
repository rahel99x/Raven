#!/usr/bin/env python3
"""Print one exact evidence-index stream as bounded measurement JSON."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from raven.signal_inputs import from_index


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("index", help="Private Raven evidence index directory")
    parser.add_argument("--source-id", required=True, type=int)
    parser.add_argument("--attempt", required=True, help="Exact attempt_key shown by the evidence query")
    parser.add_argument("--metric", required=True)
    parser.add_argument("--selector", default="{}", help='Exact stream dimensions as JSON, e.g. {"rank":"0"}')
    parser.add_argument("--limit", type=float, help="Optional measured-scope resource limit in the resulting unit")
    args = parser.parse_args(argv)
    try:
        result = from_index(args.index, source_id=args.source_id, attempt=args.attempt,
                            metric=args.metric, selector=json.loads(args.selector), limit=args.limit)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
