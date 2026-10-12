"""Noninteractive retained-data export and read-only preview."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys

from . import data_export


def _time(value):
    try:
        return float(value)
    except ValueError:
        try:
            parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                raise ValueError("timezone required")
            return parsed.timestamp()
        except ValueError as exc:
            raise argparse.ArgumentTypeError("Use epoch seconds or an ISO timestamp with an explicit timezone") from exc


def parser():
    result = argparse.ArgumentParser(prog="raven export-data", description="Export retained original measurements without scheduler queries or Heavy allocation.", allow_abbrev=False)
    result.add_argument("--project", required=True, help="Exact job project folder on this filesystem")
    result.add_argument("--run", action="append", default=[], help="Exact run ID; repeat for multiple attempts")
    result.add_argument("--include", action="append", help="Comma-separated families; all excludes logs and artifacts")
    result.add_argument("--metric", action="append", default=[], help="Exact application metric or family.metric selection")
    result.add_argument("--device", action="append", default=[], help="Exact retained device UUID/index/BDF/partition")
    result.add_argument("--source", action="append", default=[], help="Exact declared native report, trace, log or artifact source ID")
    result.add_argument("--start", type=_time, help="Inclusive UTC epoch seconds or timezone-qualified ISO timestamp")
    result.add_argument("--end", type=_time, help="Inclusive UTC epoch seconds or timezone-qualified ISO timestamp")
    result.add_argument("--layout", choices=("category", "metric"), default="category")
    destination = result.add_mutually_exclusive_group()
    destination.add_argument("--relative-dir", help="Parent folder relative to this job's project")
    destination.add_argument("--absolute-dir", help="Explicit absolute parent folder")
    result.add_argument("--host", default=data_export.local_host(), help="Filesystem host; unsupported remote adapters fail explicitly")
    result.add_argument("--check", action="store_true", help="Read-only source and destination preview; creates nothing")
    result.add_argument("--json", action="store_true", help="Machine-readable plan/receipt; errors go to stderr")
    result.add_argument("--reconcile", action="store_true", help="Recover pending captured completion exports for this project")
    result.add_argument("--retry", action="store_true", help="Explicitly retry exhausted completion exports (requires --reconcile)")
    return result


def main(argv=None):
    command = parser()
    args = command.parse_args(argv)
    try:
        if args.retry and not args.reconcile:
            raise data_export.ExportError("--retry requires --reconcile")
        if args.reconcile:
            if args.run or args.include or args.metric or args.device or args.source or args.relative_dir or args.absolute_dir or args.start is not None or args.end is not None or args.layout != "category":
                raise data_export.ExportError("--reconcile uses captured requests; do not override run, selections, or destination")
            if not data_export._is_local(args.host):
                raise data_export.ExportError("Completion reconciliation must run on the project's filesystem host")
            from . import auto_export
            requests = auto_export.pending_status(args.project) if args.check else auto_export.process_pending(args.project, deadline_seconds=30, limit=16, retry=args.retry)
            launches = auto_export.launch_candidates(args.project) if hasattr(auto_export, "launch_candidates") else []
            result = {"schema": "raven.export-reconciliation/v1", "requests": requests, "launches": launches}
            statuses = [row.get("status") for row in requests + launches if isinstance(row, dict)]
            code = 4 if "failed" in statuses else 3 if any(status not in {"complete", "resolved"} for status in statuses) else 0
        else:
            if not args.run:
                raise data_export.ExportError("Select an exact --run RUN_ID, or use --reconcile for captured completion exports")
            mode = "relative" if args.relative_dir is not None else "absolute" if args.absolute_dir is not None else "project"
            request = dict(project=args.project, runs=args.run, metrics=args.metric, devices=args.device, sources=args.source,
                           start=args.start, end=args.end, layout=args.layout,
                           destination=dict(mode=mode, path=args.relative_dir or args.absolute_dir or "", host=args.host))
            if args.include is not None:
                request["include"] = [family.strip() for value in args.include for family in value.split(",") if family.strip()]
            result = data_export.plan_export(request) if args.check else data_export.export_data(request)
            code = (0 if result["valid"] else 2) if args.check else result["exit_code"]
        if args.json:
            print(json.dumps(result, ensure_ascii=False, allow_nan=False, sort_keys=True))
        elif args.reconcile:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        elif args.check:
            print("Retained-data export preview (no files created)")
            print(f"Attempts: {len(result['attempts'])}; layout: {result['request']['layout']}")
            for destination in result["destinations"]:
                print(f"Destination: {destination['host']}:{destination['path']}")
            for family in result["request"]["include"]:
                availability = result["availability"][family]
                print(f"{family}: {'Available' if availability['available'] else 'Unavailable'} ({availability['sources']} sources)")
            for issue in result["issues"]:
                print(f"  {issue['message']}")
        else:
            print(f"Export {result['status']}: {result['files']} files, {result['attempts']} attempts")
            for path in result["outputs"]:
                print(f"{data_export.local_host()}:{path}")
            for issue in result["issues"]:
                print(f"  {issue['message']}", file=sys.stderr)
        return code
    except (data_export.ExportError, OSError, ValueError) as exc:
        print(f"raven export-data: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("raven export-data: cancelled", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
