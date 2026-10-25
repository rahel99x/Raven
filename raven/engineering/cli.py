"""Task-oriented CLI for bounded project engineering services."""

from __future__ import annotations

import argparse
import json
import sys

from . import service


def parser():
    p = argparse.ArgumentParser(
        prog="raven engineering",
        description=__doc__,
        epilog="Use catalog to list actions. A request contains function arguments; optional scheduler and arguments objects bind a native connection. See docs/guides/engineering-platform.md for recipes.",
    )
    p.add_argument("domain", choices=("catalog", *service.SERVICES))
    p.add_argument("action", nargs="?")
    p.add_argument(
        "--request",
        help="Bounded JSON arguments or {arguments: {...}, scheduler: {...}}",
    )
    p.add_argument(
        "--project", help="Project root; supplied as the project's explicit argument"
    )
    p.add_argument("--id", dest="experiment_id", help="Stable experiment identity")
    p.add_argument("--run", dest="run_id", help="Existing verified Raven run identity")
    p.add_argument("--title")
    p.add_argument("--destination", help="New immutable output directory")
    p.add_argument(
        "--review-digest", help="Explicitly reviewed plan digest for a native mutation"
    )
    p.add_argument("--state-dir", help="Private durable native action journal")
    p.add_argument("--output", help="Save result as a new private JSON file")
    return p


def execute(domain, action, document, *, state_dir=None, cancel=None):
    from .. import scheduler_cli

    scheduler = None
    try:
        if not isinstance(document, dict):
            raise ValueError("Engineering request must be an object")
        if "arguments" in document:
            if document.keys() - {"arguments", "scheduler"}:
                raise ValueError("Use only arguments and scheduler in a bound request")
            arguments = document["arguments"]
            if "scheduler" in document:
                scheduler = scheduler_cli.client(
                    document["scheduler"],
                    state_dir=state_dir or scheduler_cli.state_directory(),
                    cancel=cancel,
                )
        else:
            arguments = document
        if (domain, action) in service.COMPUTE_ACTIONS:
            if scheduler is not None:
                raise ValueError(
                    "Retained evidence analysis does not accept a live scheduler connection"
                )
            from ..execution.policy import process_router

            router = process_router()
            if router is not None and router.managed:
                blocked = router.require(
                    "engineering-analysis", worker_operation="engineering-analysis"
                )
                if blocked is not None:
                    return blocked
                receipt = router.dispatch(
                    "engineering-analysis",
                    dict(domain=domain, action=action, arguments=arguments),
                    {},
                    cancel=cancel,
                )
                if receipt.get("status") == "deferred":
                    return receipt
                return dict(
                    result=receipt["result"],
                    execution_receipt={
                        key: value for key, value in receipt.items() if key != "result"
                    },
                )
        return service.execute(
            domain,
            action,
            arguments,
            dependencies={"scheduler": scheduler} if scheduler else {},
            cancel=cancel,
        )
    finally:
        if scheduler is not None:
            scheduler_cli.close(scheduler)


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.domain == "catalog":
            result = service.describe()
        else:
            if args.action not in service.SERVICES[args.domain]:
                raise ValueError(
                    "Choose an action: " + ", ".join(service.SERVICES[args.domain])
                )
            document = service.request(args.request) if args.request else {}
            arguments = document.get("arguments", document)
            if not isinstance(arguments, dict):
                raise ValueError("Engineering arguments must be a JSON object")
            for key in (
                "project",
                "experiment_id",
                "run_id",
                "title",
                "destination",
                "review_digest",
            ):
                value = getattr(args, key)
                if value is not None:
                    if key in arguments:
                        raise ValueError(
                            "Argument appears in both request and command: " + key
                        )
                    arguments[key] = value
            result = execute(
                args.domain, args.action, document, state_dir=args.state_dir
            )
        if args.output:
            from ..scheduler_cli import save_new

            save_new(args.output, result)
        print(json.dumps(result, indent=2, ensure_ascii=True, allow_nan=False))
        return 0 if service.result_status(result) == "ok" else 3
    except (OSError, ValueError, RuntimeError) as exc:
        print("Raven engineering: " + str(exc), file=sys.stderr)
        return 2
