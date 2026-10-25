"""Native scheduler commands: bounded observations and digest-reviewed actions."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys


def client(descriptor, *, state_dir=None, cancel=None):
    from .schedulers import create
    from .slurm import Backend
    from .remote import SshBackend

    if not isinstance(descriptor, dict) or descriptor.keys() - {
        "name",
        "scope",
        "host",
        "ssh_user",
    }:
        raise ValueError("Scheduler connection accepts name, scope, host and ssh_user")
    name = descriptor.get("name", "slurm")
    if not isinstance(descriptor.get("scope", {}), dict):
        raise ValueError("Scheduler scope must be a JSON object")
    scope = dict(descriptor.get("scope", {}))
    host = descriptor.get("host")
    if host:
        if not isinstance(host, str) or host.startswith("-") or len(host) > 253:
            raise ValueError("Use a hostname or SSH alias for the scheduler connection")
        scope["host"] = host
        backend = SshBackend(host, user=descriptor.get("ssh_user") or None)
    else:
        if descriptor.get("ssh_user"):
            raise ValueError("An SSH user requires an explicit host")
        backend = Backend()
    try:
        return create(name, backend, scope, journal_dir=state_dir, cancel=cancel)
    except BaseException:
        close = getattr(backend, "close", None)
        if close:
            close()
        raise


def close(scheduler):
    callback = getattr(scheduler.backend, "close", None)
    if callback:
        callback()


def state_directory():
    root = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state")))
    if not root.is_absolute():
        raise ValueError("XDG_STATE_HOME must be absolute")
    return str(root / "raven/scheduler-actions")


def save_new(path, document):
    """Publish a private review without overwriting a user's existing file."""
    from .config_trust import directory_fd
    from .operation_json import canonical

    output = Path(path).expanduser().absolute()
    if output.name in {"", ".", ".."}:
        raise ValueError("Choose a new output filename")
    with directory_fd(str(output.parent), private=False) as parent:
        fd = os.open(
            output.name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=parent,
        )
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(canonical(document) + b"\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.fsync(parent)
        except BaseException:
            os.unlink(output.name, dir_fd=parent)
            raise


def parser():
    p = argparse.ArgumentParser(
        prog="raven scheduler",
        description=__doc__,
        epilog="Reads do not submit work. Review produces a five-minute plan; apply requires its digest. Native format coverage is not live site qualification.",
    )
    p.add_argument(
        "action",
        choices=(
            "catalog",
            "capabilities",
            "jobs",
            "history",
            "detail",
            "review",
            "apply",
        ),
    )
    p.add_argument(
        "operation", nargs="?", choices=("submit", "cancel", "hold", "release")
    )
    p.add_argument(
        "--scheduler",
        default="slurm",
        help="slurm, kubernetes, lsf, openpbs, pbspro, htcondor, or flux",
    )
    p.add_argument(
        "--scope",
        help="Connection scope JSON (cluster, user, account, context, namespace, uri)",
    )
    for field in (
        "cluster",
        "user",
        "account",
        "context",
        "namespace",
        "uri",
        "schedd",
        "pool",
    ):
        p.add_argument(
            "--" + field,
            help="Explicit native connection "
            + field
            + "; may also be supplied in --scope",
        )
    p.add_argument(
        "--host", help="Existing SSH host alias; omitted means local scheduler commands"
    )
    p.add_argument("--ssh-user")
    p.add_argument("--identity", help="Exact identity JSON from a jobs/history result")
    p.add_argument(
        "--job",
        help="Observe this exact native ID and freeze its unique current attempt before detail/review",
    )
    p.add_argument(
        "--submission",
        help="Native submission resource JSON; Kubernetes uses a batch/v1 Job manifest",
    )
    p.add_argument("--plan", help="Saved scheduler review JSON")
    p.add_argument(
        "--review-digest", help="Digest of the plan explicitly reviewed before apply"
    )
    p.add_argument(
        "--state-dir", help="Private durable action journal; defaults to XDG state"
    )
    p.add_argument("--output", help="Save result as a new private JSON file")
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    from .engineering._json import request

    scheduler = None
    try:
        if args.action == "catalog":
            from .schedulers import NAMES

            result = dict(
                schema="raven.scheduler-catalog/v1",
                schedulers=list(NAMES),
                qualification="Native-format fixtures do not qualify a live site.",
                native_details="Use capabilities with an explicit scheduler and scope before connecting.",
            )
        else:
            scope = request(args.scope) if args.scope else {}
            for field in (
                "cluster",
                "user",
                "account",
                "context",
                "namespace",
                "uri",
                "schedd",
                "pool",
            ):
                explicit = getattr(args, field)
                if explicit is not None:
                    if field in scope and scope[field] != explicit:
                        raise ValueError("Command and scope disagree on " + field)
                    scope[field] = explicit
            scheduler = client(
                dict(
                    name=args.scheduler,
                    scope=scope,
                    host=args.host,
                    ssh_user=args.ssh_user,
                ),
                state_dir=args.state_dir or state_directory(),
            )
            if args.identity and args.job:
                raise ValueError("Choose --identity or --job, not both")
            observed = request(args.identity) if args.identity else None
            if args.job:
                if (
                    args.action not in {"detail", "review"}
                    or args.operation == "submit"
                ):
                    raise ValueError(
                        "--job is for detail or a reviewed existing-job action"
                    )
                candidates = [
                    record for record in scheduler.jobs() if record["id"] == args.job
                ]
                if not candidates:
                    candidates = [
                        record
                        for record in scheduler.jobs(history=True)
                        if record["id"] == args.job
                    ]
                if len(candidates) != 1:
                    raise ValueError(
                        "The native ID has no unique observed attempt; use an exact identity from queue/history"
                    )
                observed = candidates[0]["identity"]
            if args.action == "capabilities":
                result = scheduler.capabilities()
            elif args.action in {"jobs", "history"}:
                records = scheduler.jobs(history=args.action == "history")
                result = dict(
                    scheduler=args.scheduler,
                    records=records,
                    capabilities=scheduler.capabilities(),
                )
            elif args.action == "detail":
                if observed is None:
                    raise ValueError(
                        "detail requires --identity from an observed exact job attempt"
                    )
                result = scheduler.detail(observed)
            elif args.action == "review":
                if not args.operation:
                    raise ValueError("review requires submit, cancel, hold or release")
                result = scheduler.review(
                    args.operation,
                    identity=observed,
                    submission=request(args.submission) if args.submission else None,
                )
            else:
                if not args.plan or not args.review_digest:
                    raise ValueError(
                        "apply requires --plan and --review-digest; inspect the review before applying"
                    )
                result = scheduler.apply(request(args.plan), args.review_digest)
        if args.output:
            save_new(args.output, result)
        print(json.dumps(result, indent=2, ensure_ascii=True, allow_nan=False))
        return (
            3 if isinstance(result, dict) and result.get("status") == "unknown" else 0
        )
    except (OSError, ValueError, RuntimeError) as exc:
        print("Raven scheduler: " + str(exc), file=sys.stderr)
        return 2
    finally:
        if scheduler is not None:
            close(scheduler)
