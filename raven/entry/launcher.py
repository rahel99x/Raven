"""Minimal Raven launchers with a responsive Light-to-Heavy dashboard.

Importing this module starts no process, collector, environment scan, or history
reader. Raven and Raven Light share the same bounded monitoring entry point.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
import sys
import time

from ..configuration.policy import canonical, doctor, resolve, _roots


from ..entry.grammar import bootstrap_parser as _parser


def _output(value, *, json_mode=True):
    # Structured bootstrap output contains no command transcripts or credentials.
    print(json.dumps(value, ensure_ascii=True, indent=2, allow_nan=False))


def _create_config(path, resolved):
    from ..installation.config_migration import private_directory, write_new

    destination = Path(path)
    private_directory(destination.parent)
    write_new(destination, canonical(resolved.data) + b"\n")
    return {
        "schema": "raven.config-write/v1",
        "path": str(destination),
        "created": True,
    }


def _allocation(resolved):
    from ..raven_allocation import AllocationManager

    return AllocationManager(resolved)


from ..entry.placement import configure_placement as configure_placement


from ..entry.heavy_startup import BackgroundHeavyStartup as BackgroundHeavyStartup


def _background_dashboard(arguments, *, json_mode=False):
    """Only an interactive/watch dashboard can keep servicing admission."""
    if json_mode or (not sys.stdout.isatty() and "--watch" not in arguments):
        return False
    if arguments[:1] and arguments[0] in {
        "terminal",
        "trace",
        "runtime",
        "report",
        "run",
        "query",
        "series",
        "mcp",
        "engineering",
    }:
        return False
    from ..entry.grammar import parse

    args = parse(arguments)
    return not (
        args.once
        or args.json
        or args.csv
        or args.report
        or args.run
        or args.eval
        or args.wait_for
        or args.doctor
        or args.gpu_check is not None
        or args.replay
    )


from ..entry.heavy_startup import shutdown_heavy as shutdown_heavy


def _wait_heavy(manager, resolved, reuse=None):
    if reuse is not None:
        if not re.fullmatch(r"[1-9][0-9]{0,19}", str(reuse)):
            raise ValueError("--allocation requires one exact numeric allocation ID.")
        manager.request_reuse_review(reuse)
        review_deadline = time.monotonic() + 60
        while True:
            manager.tick()
            review = manager.snapshot().get("reuse_review", {})
            if review.get("state") == "ready":
                print(
                    "Raven Heavy: verified explicit reuse of allocation "
                    + str(reuse)
                    + "; owns the worker step only.",
                    file=sys.stderr,
                )
                manager.request(reuse=review["token"], plan=review["plan"])
                break
            if review.get("state") == "error":
                raise ValueError(
                    review.get("error") or "Allocation reuse review failed."
                )
            if time.monotonic() >= review_deadline:
                manager.cancel()
                raise ValueError(
                    "Allocation reuse review timed out; no worker was launched."
                )
            manager.wait_for_activity(
                min(1.0, max(0.0, review_deadline - time.monotonic()))
            )
    else:
        manager.request()
    deadline = (
        time.monotonic() + resolved.data["allocation"]["pending_timeout_seconds"] + 60
    )
    last = None
    while True:
        manager.tick()
        state = manager.snapshot()
        status = state.get("state", "light")
        if status != last:
            print("Raven Heavy: " + str(status), file=sys.stderr)
            last = status
        if status == "heavy":
            worker = getattr(manager, "worker", None)
            if worker is not None and getattr(worker, "ready", False):
                return state
        if status == "light" and state.get("error") or status == "reconciling":
            raise ValueError(
                state.get("error")
                or "Allocation cleanup is uncertain; inspect ownership before retrying."
            )
        if time.monotonic() >= deadline:
            manager.cancel()
            raise ValueError(
                "Heavy startup exceeded the configured pending/readiness deadline; no local fallback was started."
            )
        manager.wait_for_activity(min(1.0, max(0.0, deadline - time.monotonic())))


def _help(parser):
    parser.print_help()
    print(
        "\nCommands: setup; update [--check|--plan|--overwrite]; revert BACKUP; trust PATH; config explain; doctor; migrate --plan|--apply; qualify --offline."
    )
    print(
        "Dashboard options remain available: --once, --watch, --fake, --tab, --interval, --host, run COMMAND."
    )
    print(
        "Use ravenl for Light. ravenh explicitly requests admission; missing Slurm never selects desktop Heavy."
    )
    print(
        "Interactive ravenh opens immediately in Light while Heavy starts; the compute badge opens Cancel and Retry controls."
    )


def _main_impl(argv=None, *, heavy=False):
    argv = list(sys.argv[1:] if argv is None else argv)
    from .workload import dispatch as dispatch_workload

    try:
        workload = dispatch_workload(argv)
        if workload is not None:
            return workload
    except (OSError, ValueError, RuntimeError) as exc:
        print("Raven: " + str(exc), file=sys.stderr)
        return 2
    parser = _parser(heavy)
    passthrough = {
        "report",
        "trace",
        "runtime",
        "terminal",
        "migrate",
        "run",
        "setup",
        "query",
        "series",
        "mcp",
        "update",
        "revert",
        "trust",
        "scheduler",
        "engineering",
    }
    # Global options stop at the subcommand. A nested --help/--config belongs
    # to that command, even when a global --site precedes it. Skip option values
    # so a hostname or profile named 'terminal' is not mistaken for a command.
    takes_value = {
        "--site",
        "--profile",
        "--config",
        "--host",
        "--ssh-user",
        "--account",
        "--site-policy",
        "--allocation",
        "--interface",
    }
    boundary, skip = len(argv), False
    for index, token in enumerate(argv):
        if skip:
            skip = False
            continue
        if token in takes_value:
            skip = True
        elif token == "--":
            boundary = index + 1
            break
        elif token in passthrough:
            boundary = index
            break
    options, remaining = parser.parse_known_args(argv[:boundary])
    remaining += argv[boundary:]
    if options.help:
        _help(parser)
        return 0
    if options.version:
        from .. import __version__

        print("Raven " + __version__)
        return 0
    # Package maintenance is independent of scheduler configuration, admission,
    # plugins and first-run setup under all three installed commands.
    if remaining[:1] in (["update"], ["revert"]):
        if (
            options.site
            or options.profile
            or options.host
            or options.ssh_user
            or options.account is not None
            or options.site_policy
            or options.allocation
            or options.plan
            or options.write_config is not None
            or options.json
            or options.show_paths
            or options.interface != "local"
        ):
            print(
                "Raven: put update/revert options after the command; scheduler options do not apply.",
                file=sys.stderr,
            )
            return 2
        from ..update import cli as maintenance

        arguments = list(remaining)
        if options.config:
            arguments += ["--config", options.config]
        return maintenance(arguments)
    if remaining[:1] == ["trust"]:
        from ..path_trust_cli import main as trust_main

        return trust_main(remaining[1:])
    if remaining[:1] in (["scheduler"], ["engineering"]):
        # These task commands bind their own explicit native connection and do
        # not request Slurm Heavy merely because ravenh is the entry point.
        from ..engineering.service import COMPUTE_ACTIONS

        compute = (
            remaining[:1] == ["engineering"]
            and tuple(remaining[1:3]) in COMPUTE_ACTIONS
        )
        if not compute and any(
            (
                options.site,
                options.profile,
                options.host,
                options.ssh_user,
                options.account is not None,
                options.site_policy,
                options.allocation,
                options.plan,
                options.write_config is not None,
                options.config,
                options.interface != "local",
            )
        ):
            print(
                "Raven: put the native connection in the scheduler/engineering request; dashboard overrides do not apply.",
                file=sys.stderr,
            )
            return 2
        if not compute:
            if remaining[0] == "scheduler":
                from ..scheduler_cli import main as task_main
            else:
                from ..engineering.cli import main as task_main
            return task_main(remaining[1:])
    if options.interface == "compute":
        print(
            "Raven: allocation-first full-interface transfer is unavailable; use the persistent Light interface with an admitted worker.",
            file=sys.stderr,
        )
        return 2
    if options.allocation and not heavy:
        print(
            "Raven: --allocation is an explicit ravenh reuse action; ravenl never requests compute.",
            file=sys.stderr,
        )
        return 2
    # Migration is intentionally independent of a possibly invalid old config.
    if remaining[:1] == ["migrate"]:
        from ..installation.config_migration import cli as migrate

        return migrate(remaining[1:])
    if remaining[:1] == ["setup"]:
        # Setup is available before collection/admission, including from
        # ravenh. Its nested flags must never become bootstrap mutations.
        if (
            options.host
            or options.ssh_user
            or options.account is not None
            or options.site_policy
            or options.allocation
            or options.plan
            or options.write_config is not None
            or options.profile
        ):
            print(
                "Raven: use setup's connection and account fields after 'setup'; "
                "only --config, --site, and --json may precede this command.",
                file=sys.stderr,
            )
            return 2
        from ..entry.onboarding_cli import run_setup

        setup_args = remaining[1:]
        if options.json and "--json" not in setup_args:
            setup_args = [*setup_args, "--json"]
        return run_setup(
            setup_args,
            config_path=options.config,
            site=options.site,
            **({"demo": light_main} if "--demo" in setup_args else {}),
        )
    try:
        if heavy and "--fake" in remaining:
            raise ValueError(
                "ravenh --fake cannot request a real allocation. Use ravenl --fake for simulated monitoring or ravenh --plan for a read-only startup plan."
            )
        overrides = {}
        if options.host:
            overrides["connection"] = {"transport": "ssh", "host": options.host}
        if options.ssh_user:
            overrides.setdefault("connection", {})["user"] = options.ssh_user
        if options.account is not None:
            overrides["allocation"] = {"account": options.account}
        # Help is a read-only operation under either launcher. Never request an
        # allocation merely to display a subcommand's controls.
        help_only = bool(
            remaining
            and remaining[0] in passthrough
            and any(x in ("--help", "-h") for x in remaining[1:])
        )
        admission = heavy and not help_only and "--getting-started" not in remaining
        background = (
            admission
            and not options.plan
            and options.write_config is None
            and remaining[:1] not in (["config"], ["doctor"], ["qualify"])
            and _background_dashboard(remaining, json_mode=options.json)
        )
        # A bare first launch may need account setup even when a mandatory
        # site policy requires billing before Heavy admission. Resolve Light
        # first for that candidate, then validate the real Heavy intent once
        # we know it is an already configured or noninteractive launch.
        setup_candidate = admission and not argv
        resolved = resolve(
            options.config,
            site=options.site,
            legacy_profile=options.profile,
            launcher="heavy"
            if admission and not setup_candidate and not background
            else "light",
            site_policy_path=options.site_policy,
            cli_overrides=overrides or None,
        )
        from ..entry.onboarding_cli import should_offer_first_run

        first_run = should_offer_first_run(resolved, argv=argv)
        if setup_candidate and not first_run and not background:
            resolved = resolve(
                options.config,
                site=options.site,
                legacy_profile=options.profile,
                launcher="heavy",
                site_policy_path=options.site_policy,
                cli_overrides=overrides or None,
            )
        for message in resolved.warnings:
            print("Raven: " + message, file=sys.stderr)
        if remaining[:2] == ["config", "explain"]:
            if len(remaining) != 2:
                raise ValueError("Usage: ravenl config explain [--show-paths] [--json]")
            _output(resolved.explain(show_paths=options.show_paths))
            return 0
        if remaining[:1] == ["doctor"]:
            if len(remaining) != 1:
                raise ValueError("Usage: ravenl doctor [--json]")
            _output(doctor(resolved))
            return 0
        if remaining[:1] == ["qualify"]:
            if remaining[1:] != ["--offline"]:
                raise ValueError(
                    "Qualification requires --offline; live site pilots are separate reviewed work."
                )
            from .qualification import main as qualification

            return qualification(remaining[1:])
        if options.write_config is not None:
            root, _state, _home = _roots(dict(os.environ))
            path = options.write_config or str(root / "raven/config.json")
            if not str(path).endswith(".json"):
                raise ValueError(
                    "The Raven bootstrap writes JSON; TOML remains an equivalent input on Python 3.11+."
                )
            _output(_create_config(path, resolved))
            return 0
        if options.plan:
            from ..heavy.allocation_adapters import prepare as prepare_allocation

            if options.allocation and not re.fullmatch(
                r"[1-9][0-9]{0,19}", options.allocation
            ):
                raise ValueError(
                    "--allocation requires one exact numeric allocation ID."
                )
            allocation = (
                (
                    {
                        "intent": "reuse",
                        "job_id": options.allocation,
                        "validation": "unverified; startup requires fresh governed allocation/owner/placement checks",
                    }
                    if options.allocation
                    else prepare_allocation(resolved)
                )
                if heavy
                else None
            )
            _output(
                {
                    "schema": "raven.launch-plan/v1",
                    "intent": "heavy" if heavy else "light",
                    "configuration": resolved.explain(show_paths=options.show_paths),
                    "allocation": allocation,
                    "mutations": 0,
                    "dashboard_imported": False,
                }
            )
            return 0
        if first_run and admission:
            # A fresh interactive installation must choose its site and
            # billing preferences before a Heavy allocation is requested.
            admission = False
        manager = None
        if admission:
            manager = _allocation(resolved)
            try:
                configure_placement(resolved, manager)
                if background:
                    manager.background_startup = BackgroundHeavyStartup(
                        manager, resolved, options.allocation
                    )
                else:
                    _wait_heavy(manager, resolved, options.allocation)
            except BaseException:
                shutdown_heavy(manager)
                raise
        if options.json:
            remaining.append("--json")
        # Interactive sessions paint Light immediately. Heavy operations remain
        # gated by the same manager's verified WorkerProxy.ready publication.
        from .. import cli

        try:
            if first_run:
                remaining.append("--getting-started")
            return cli.main(remaining, raven=resolved, raven_allocation=manager)
        finally:
            if manager is not None:
                shutdown_heavy(manager)
    except KeyboardInterrupt:
        print(
            "Raven: startup cancelled; owned helper cleanup requested.", file=sys.stderr
        )
        return 130
    except (OSError, ValueError, RuntimeError, ImportError) as exc:
        print("Raven: " + str(exc), file=sys.stderr)
        return 2


def _main(argv=None, *, heavy=False):
    arguments = list(sys.argv[1:] if argv is None else argv)
    if "--json" not in arguments:
        return _main_impl(arguments, heavy=heavy)
    # Early configuration/admission errors happen before cli.main's JSON
    # boundary. Keep installed launchers machine-readable there as well.
    from contextlib import redirect_stdout, redirect_stderr
    import io

    output, errors = io.StringIO(), io.StringIO()
    with redirect_stdout(output), redirect_stderr(errors):
        try:
            code = _main_impl(arguments, heavy=heavy)
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 2
    text, diagnostic = output.getvalue(), errors.getvalue()
    if diagnostic:
        sys.stderr.write(diagnostic)
    from ..cli_output import ApplicationExit

    if isinstance(code, ApplicationExit):
        sys.stdout.write(text)
        return code
    try:
        value = json.loads(text)
    except (ValueError, TypeError):
        if code:
            value = {
                "error": (diagnostic.strip() or text.strip() or "Raven launch failed")[
                    :8192
                ],
                "exit_code": code,
            }
        else:
            # Explicit help/version requests preserve their text contract.
            sys.stdout.write(text)
            return code
    print(json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":")))
    return code


def light_main(argv=None):
    return _main(argv, heavy=False)


def heavy_main(argv=None):
    return _main(argv, heavy=True)


if __name__ == "__main__":
    raise SystemExit(light_main())
