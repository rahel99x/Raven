"""Bounded local archive inspection, copy/verify migration and raw export."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import secrets
import sys
import zlib

from . import series_blocks as blocks
from . import series_storage as storage


def main(argv=None):
    parser = argparse.ArgumentParser(prog="raven series", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("inspect", "migrate", "export", "recover"):
        command = sub.add_parser(name)
        command.add_argument("source", help="Local retained .jsonl or .rvb file")
        command.add_argument("--max-bytes", type=int, default=storage.MAX_SOURCE)
        if name == "migrate":
            command.add_argument("--apply", action="store_true", help="Publish the verified copy; without this flag, preview only")
            command.add_argument("--allow-unattributed", action="store_true", help="Copy legacy evidence without claiming attempt attribution")
        if name in ("export", "recover"):
            command.add_argument("--output", required=True, help="New JSONL destination; existing files are preserved")
    retention = sub.add_parser("retention", help="Preview or publish an explicit-fidelity working copy; preserve original raw evidence")
    retention.add_argument("source")
    retention.add_argument("--apply", action="store_true")
    retention.add_argument("--raw-seconds", type=float, default=86400)
    retention.add_argument("--ten-second-seconds", type=float, default=604800)
    retention.add_argument("--minute-seconds", type=float, default=2592000)
    retention.add_argument("--gap-seconds", type=float, default=2.)
    retention.add_argument("--as-of", type=float)
    retention.add_argument("--unprotect-working-copy", action="store_true", help="Permit tiering only this working copy; original source remains protected")
    retention.add_argument("--max-bytes", type=int, default=storage.MAX_SOURCE)
    query = sub.add_parser("query", help="Read raw or explicit-fidelity retention records")
    query.add_argument("source")
    query.add_argument("--start", type=float)
    query.add_argument("--end", type=float)
    query.add_argument("--limit", type=int, default=10000)
    query.add_argument("--max-bytes", type=int, default=storage.MAX_SOURCE)
    pin = sub.add_parser("pin", help="Protect or explicitly unprotect a retention working copy")
    pin.add_argument("source")
    pin.add_argument("--unpin", action="store_true")
    pin.add_argument("--max-bytes", type=int, default=storage.MAX_SOURCE)
    args = parser.parse_args(argv)
    try:
        if not 1 <= args.max_bytes <= storage.MAX_SOURCE:
            raise ValueError("Read budget must be between 1 byte and 64 MiB")
        source = Path(args.source).expanduser().absolute()
        if args.command in ("retention", "query", "pin"):
            from . import series_retention as retention_module
            if args.command == "query":
                result = retention_module.query(source, start=args.start, end=args.end, limit=args.limit, max_bytes=args.max_bytes)
            else:
                mutates = args.command == "pin" or args.apply
                owner = None
                if mutates:
                    from .series_identity import inventory_job
                    # Require an attempt-scoped native cache, not arbitrary
                    # project/scientific files or ambiguous old job numbers.
                    candidate = source.name[:-4] + ".jsonl" if source.suffix == ".rvt" else source.name
                    if source.parent.name != "series" or inventory_job(candidate) is None:
                        raise ValueError("Retention publication requires an exact attempt-scoped state/series file")
                    from .state_ownership import StateOwner
                    owner = StateOwner(source.parent.parent, strict=True)
                try:
                    if args.command == "pin":
                        result = retention_module.pin(source, not args.unpin, max_bytes=args.max_bytes)
                    else:
                        values = dict(raw_seconds=args.raw_seconds, ten_second_seconds=args.ten_second_seconds,
                                      minute_seconds=args.minute_seconds, gap_seconds=args.gap_seconds,
                                      protect_raw=not args.unprotect_working_copy)
                        result = retention_module.build(source, settings=values, as_of=args.as_of, apply=args.apply, max_bytes=args.max_bytes)
                finally:
                    if owner: owner.close()
        elif args.command == "inspect":
            if source.suffix == ".rvt":
                from .series_retention import inspect
                result = inspect(source, max_bytes=args.max_bytes)
            elif source.suffix == ".rvb":
                _, result = blocks.read(source, keep=1, max_bytes=args.max_bytes)
            else:
                rows = sum(1 for _ in storage.json_rows(source, max_bytes=args.max_bytes))
                result = {"status": "ready", "format": "jsonl", "verified_samples": rows, "fidelity": "raw"}
        elif args.command == "migrate":
            from .series_identity import inventory_job
            verified = inventory_job(source.name) is not None
            if args.apply:
                if source.parent.name != "series":
                    raise ValueError("Apply migration to an owned state/series file; arbitrary project evidence is read-only")
                if not verified and not args.allow_unattributed:
                    raise ValueError("Legacy identity is ambiguous; --allow-unattributed copies it without assigning it to a current attempt")
                from .state_ownership import StateOwner
                owner = StateOwner(source.parent.parent, strict=True)
                try:
                    result = storage.migrate(source, apply=True, max_bytes=args.max_bytes)
                finally:
                    owner.close()
            else:
                result = storage.migrate(source, max_bytes=args.max_bytes)
            result["attribution"] = "preserved-attempt-scope" if verified else "legacy-ambiguous"
        else:
            target = Path(args.output).expanduser().absolute()
            if target == source or target.exists():
                raise ValueError("Export requires a new destination; source and existing output remain unchanged")
            temporary = target.with_name(target.name + ".stage-" + secrets.token_hex(8))
            count = 0
            try:
                rows = storage.block_rows(source, max_bytes=args.max_bytes) if source.suffix == ".rvb" else storage.json_rows(source, max_bytes=args.max_bytes)
                coverage_error = ""
                with temporary.open("xb") as output:
                    iterator = iter(rows)
                    while True:
                        try:
                            row = next(iterator)
                        except StopIteration:
                            break
                        except (OSError, ValueError, zlib.error, RecursionError) as exc:
                            if args.command != "recover" or not count:
                                raise
                            coverage_error = str(exc)
                            break
                        output.write(storage._canonical(row)); count += 1
                    output.flush(); os.fsync(output.fileno())
                os.link(temporary, target, follow_symlinks=False)
                result = {"status": "recovered-prefix" if coverage_error else "published", "source": str(source),
                          "output": str(target), "raw_samples": count, "fidelity": "raw-lossless-prefix" if coverage_error else "raw-lossless",
                          "complete": not bool(coverage_error), "read_error": coverage_error, "source_preserved": True}
            finally:
                try: temporary.unlink()
                except FileNotFoundError: pass
        print(json.dumps(result, indent=2, allow_nan=False))
        return 0 if result.get("status") != "error" else 1
    except (OSError, ValueError, zlib.error, RecursionError) as exc:
        print("Raven series: " + str(exc), file=sys.stderr)
        return 1
