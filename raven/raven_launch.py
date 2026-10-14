"""Minimal Raven launchers with a responsive Light-to-Heavy dashboard.

Importing this module starts no process, collector, environment scan, or history
reader. Raven and Raven Light share the same bounded monitoring entry point.
"""
from __future__ import annotations

import argparse
import hashlib
import socket
import json
import os
import re
from pathlib import Path
import sys
import time

from .raven_config import SCHEMA, Resolved, canonical, doctor, resolve, to_raven_config, _roots


def _parser(heavy=False):
    name = "ravenh" if heavy else "ravenl"
    parser = argparse.ArgumentParser(prog=name, add_help=False, allow_abbrev=False,
        description="Raven: " + ("explicit Heavy admission; waiting stays lightweight." if heavy else "bounded Light monitoring; opening never requests compute."))
    parser.add_argument("--site", help="Named configured profile; desktop must be explicit")
    parser.add_argument("--profile", help="Legacy named Raven profile (read-only compatibility)")
    parser.add_argument("--config", help="Versioned Raven config, or a read-only legacy view")
    parser.add_argument("--host", help="Explicit SSH scheduler host alias for this startup")
    parser.add_argument("--ssh-user", help="SSH login for the explicit connection")
    parser.add_argument("--account", help="Account for monitoring and explicit Heavy allocation admission")
    parser.add_argument("--site-policy", help="Additional restrictive site policy; cannot replace mandatory policy")
    parser.add_argument("--allocation", help="Explicit exact allocation to validate and reuse (Heavy only)")
    parser.add_argument("--plan", action="store_true", help="Explain effective startup without submitting or writing state")
    parser.add_argument("--interface", choices=("local", "compute"), default="local")
    parser.add_argument("--json", action="store_true", help="Machine-readable bootstrap result; forwarded for dashboard output")
    parser.add_argument("--help", "-h", action="store_true")
    parser.add_argument("--version", action="store_true", help="Print the installed release without starting collection")
    parser.add_argument("--show-paths", action="store_true", help="Include configured paths in config explain")
    parser.add_argument("--write-config", nargs="?", const="", metavar="NEW_JSON", help="Create a new Raven JSON config without overwrite")
    return parser


def _output(value, *, json_mode=True):
    # Structured bootstrap output contains no command transcripts or credentials.
    print(json.dumps(value, ensure_ascii=True, indent=2, allow_nan=False))


def _create_config(path, resolved):
    from .raven_migration import private_directory, write_new
    destination = Path(path)
    private_directory(destination.parent)
    write_new(destination, canonical(resolved.data) + b"\n")
    return {"schema": "raven.config-write/v1", "path": str(destination), "created": True}


def _allocation(resolved):
    from .raven_allocation import AllocationManager
    return AllocationManager(resolved)


def configure_placement(resolved, manager):
    """Construct one governed collection owner; no sampler or history is loaded."""
    data = resolved.data if isinstance(resolved, Resolved) else resolved
    from .slurm import Backend
    from .remote import SshBackend
    from .platform_session import wrap_backend
    from .raven_collection import make_placement_verifier
    connection = data["connection"]
    if connection["transport"] == "ssh":
        backend = SshBackend(connection["host"], user=connection["user"], opts=connection["ssh_options"], control=False)
        host, kind, ssh_user = connection["host"], "ssh", connection["user"]
        options = hashlib.sha256(json.dumps(connection["ssh_options"], ensure_ascii=True, separators=(",", ":")).encode()).hexdigest()
    else:
        backend = Backend()
        host, kind, ssh_user, options = socket.gethostname(), "local", "", ""
    scope = {"backend": kind, "host": host, "profile": data["profile"], "uid": os.getuid(),
             "user": os.environ.get("USER", ""), "ssh_user": ssh_user, "ssh_options_sha256": options}
    governed = wrap_backend(backend, to_raven_config(resolved), scope, data["storage"]["state_dir"])
    manager.collection_backend = governed
    if data["site"]["kind"] != "desktop":
        manager.set_placement_verifier(make_placement_verifier(governed))
    return manager


class BackgroundHeavyStartup:
    """Drive existing nonblocking admission from the dashboard's event loop.

    The allocation manager owns the helper, scheduler I/O and verification
    threads. This object owns only the initial intent and its deadlines; it
    creates no second worker or polling loop. Construction never submits.
    """

    def __init__(self, manager, resolved, reuse=None):
        if reuse is not None and not re.fullmatch(r"[1-9][0-9]{0,19}", str(reuse)):
            raise ValueError("--allocation requires one exact numeric allocation ID.")
        self.manager, self.resolved, self.reuse = manager, resolved, reuse
        self.phase, self.error, self.deadline = "armed", "", None
        self.review_token = None
        self.review_plan = None

    @property
    def retryable(self):
        snap = self.manager.snapshot()
        return self.phase in {"failed", "cancelled"} and snap.get("state") == "light" and not snap.get("cleanup_uncertain")

    def publication(self, snapshot):
        value = dict(snapshot)
        if self.phase == "closed":
            return value
        if self.phase in {"armed", "reviewing"} and value.get("state") == "light":
            value.update(state="preparing", mode="light")
        if self.error and not value.get("cleanup_uncertain"):
            value["error"] = self.error
        value["startup"] = {"phase": self.phase, "reuse_job": self.reuse,
                            "retryable": self.retryable}
        return value

    def retry(self, plan=None):
        if not self.retryable:
            raise ValueError("Wait for confirmed cleanup before retrying Heavy startup.")
        # The ordinary request rechecks this frozen plan in manager.request;
        # explicit reuse obtains a new ownership receipt before launching.
        self.review_plan = plan
        self.phase, self.error, self.deadline, self.review_token = "armed", "", None, None
        return True

    def cancel(self, generation=None):
        snapshot = self.manager.snapshot()
        if generation is not None and generation != snapshot.get("generation"):
            return False
        self.phase, self.error = "cancelled", ""
        # A reuse read may finish after cancellation. Its token is discarded;
        # without a subsequent tick in 'reviewing' it can never start a step.
        self.review_token = None
        if snapshot.get("state") in {"preparing", "pending", "starting", "heavy", "draining", "reconciling"}:
            return self.manager.cancel(generation=generation)
        return True

    def close(self):
        self.phase, self.review_token = "closed", None

    def tick(self):
        if self.phase in {"closed", "cancelled", "failed"}:
            return
        try:
            if self.phase == "armed":
                if self.review_plan is not None and self.manager.plan().get("digest") != self.review_plan.get("digest"):
                    raise ValueError("Allocation settings changed; review Heavy startup again before retrying.")
                if self.reuse is not None:
                    self.review_token = self.manager.request_reuse_review(str(self.reuse))
                    self.phase, self.deadline = "reviewing", time.monotonic() + 60
                else:
                    self.manager.request(plan=self.review_plan)
                    self.phase = "waiting"
                    self.deadline = time.monotonic() + self.resolved.data["allocation"]["pending_timeout_seconds"] + 60
            elif self.phase == "reviewing":
                review = self.manager.snapshot().get("reuse_review", {})
                if review.get("token") == self.review_token:
                    if review.get("state") == "error":
                        raise ValueError(review.get("error") or "Allocation reuse review failed.")
                    if review.get("state") == "ready":
                        self.manager.request(reuse=review["token"], plan=review["plan"])
                        self.phase = "waiting"
                        self.deadline = time.monotonic() + self.resolved.data["allocation"]["pending_timeout_seconds"] + 60
            snapshot = self.manager.snapshot()
            if self.phase in {"waiting", "ready"}:
                if snapshot.get("state") == "heavy" and getattr(getattr(self.manager, "worker", None), "ready", False):
                    self.phase = "ready"
                elif snapshot.get("cleanup_uncertain") or snapshot.get("state") == "reconciling":
                    raise ValueError(snapshot.get("error") or "Allocation cleanup is uncertain; inspect ownership before retrying.")
                elif snapshot.get("state") == "light":
                    raise ValueError(snapshot.get("error") or "Heavy worker stopped. Review and retry when ready.")
            if self.phase in {"reviewing", "waiting"} and time.monotonic() >= self.deadline:
                self.cancel()
                raise ValueError("Heavy startup timed out; owned cleanup requested. Job inspection and logs remain available.")
        except (OSError, ValueError, RuntimeError) as exc:
            self.phase, self.error = "failed", str(exc)


def _background_dashboard(arguments, *, json_mode=False):
    """Only an interactive/watch dashboard can keep servicing admission."""
    if json_mode or (not sys.stdout.isatty() and "--watch" not in arguments):
        return False
    if arguments[:1] and arguments[0] in {"terminal", "trace", "runtime", "report", "run"}:
        return False
    from .cli import parse
    args = parse(arguments)
    return not (args.once or args.json or args.csv or args.report or args.run or args.eval
                or args.wait_for or args.doctor or args.gpu_check is not None or args.replay)


def shutdown_heavy(manager):
    startup = getattr(manager, "background_startup", None)
    if startup is not None:
        startup.close()
    manager.shutdown()


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
                print("Raven Heavy: verified explicit reuse of allocation " + str(reuse) + "; owns the worker step only.", file=sys.stderr)
                manager.request(reuse=review["token"], plan=review["plan"])
                break
            if review.get("state") == "error":
                raise ValueError(review.get("error") or "Allocation reuse review failed.")
            if time.monotonic() >= review_deadline:
                manager.cancel()
                raise ValueError("Allocation reuse review timed out; no worker was launched.")
            time.sleep(.025)
    else:
        manager.request()
    deadline = time.monotonic() + resolved.data["allocation"]["pending_timeout_seconds"] + 60
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
            raise ValueError(state.get("error") or "Allocation cleanup is uncertain; inspect ownership before retrying.")
        if time.monotonic() >= deadline:
            manager.cancel()
            raise ValueError("Heavy startup exceeded the configured pending/readiness deadline; no local fallback was started.")
        time.sleep(.025)


def _help(parser):
    parser.print_help()
    print("\nCommands: setup; config explain; doctor; migrate --plan|--apply; qualify --offline.")
    print("Dashboard options remain available: --once, --watch, --fake, --tab, --interval, --host, run COMMAND.")
    print("Use ravenl for Light. ravenh explicitly requests admission; missing Slurm never selects desktop Heavy.")
    print("Interactive ravenh opens immediately in Light while Heavy starts; the compute badge opens Cancel and Retry controls.")


def _main(argv=None, *, heavy=False):
    argv = list(sys.argv[1:] if argv is None else argv)
    from .launch import dispatch as dispatch_workload
    try:
        workload = dispatch_workload(argv)
        if workload is not None:
            return workload
    except (OSError, ValueError, RuntimeError) as exc:
        print("Raven: " + str(exc), file=sys.stderr)
        return 2
    parser = _parser(heavy)
    passthrough = {"report", "trace", "runtime", "terminal", "migrate", "run", "setup"}
    # Global options stop at the subcommand. A nested --help/--config belongs
    # to that command, even when a global --site precedes it. Skip option values
    # so a hostname or profile named 'terminal' is not mistaken for a command.
    takes_value = {"--site", "--profile", "--config", "--host", "--ssh-user", "--account", "--site-policy", "--allocation", "--interface"}
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
        from . import __version__
        print("Raven " + __version__)
        return 0
    if options.interface == "compute":
        print("Raven: allocation-first full-interface transfer is unavailable; use the persistent Light interface with an admitted worker.", file=sys.stderr)
        return 2
    if options.allocation and not heavy:
        print("Raven: --allocation is an explicit ravenh reuse action; ravenl never requests compute.", file=sys.stderr)
        return 2
    # Migration is intentionally independent of a possibly invalid old config.
    if remaining[:1] == ["migrate"]:
        from .raven_migration import cli as migrate
        return migrate(remaining[1:])
    if remaining[:1] == ["setup"]:
        # Setup is available before collection/admission, including from
        # ravenh. Its nested flags must never become bootstrap mutations.
        if (options.host or options.ssh_user or options.account is not None
                or options.site_policy or options.allocation or options.plan
                or options.write_config is not None or options.profile):
            print("Raven: use setup's connection and account fields after 'setup'; "
                  "only --config, --site, and --json may precede this command.", file=sys.stderr)
            return 2
        from .onboarding import run_setup
        setup_args = remaining[1:]
        if options.json and "--json" not in setup_args:
            setup_args = [*setup_args, "--json"]
        return run_setup(setup_args, config_path=options.config, site=options.site)
    try:
        if heavy and "--fake" in remaining:
            raise ValueError("ravenh --fake cannot request a real allocation. Use ravenl --fake for simulated monitoring or ravenh --plan for a read-only startup plan.")
        overrides = {}
        if options.host:
            overrides["connection"] = {"transport": "ssh", "host": options.host}
        if options.ssh_user:
            overrides.setdefault("connection", {})["user"] = options.ssh_user
        if options.account is not None:
            overrides["allocation"] = {"account": options.account}
        # Help is a read-only operation under either launcher. Never request an
        # allocation merely to display a subcommand's controls.
        help_only = bool(remaining and remaining[0] in passthrough and any(x in ("--help", "-h") for x in remaining[1:]))
        admission = heavy and not help_only and "--getting-started" not in remaining
        background = (admission and not options.plan and options.write_config is None
                      and remaining[:1] not in (["config"], ["doctor"], ["qualify"])
                      and _background_dashboard(remaining, json_mode=options.json))
        # A bare first launch may need account setup even when a mandatory
        # site policy requires billing before Heavy admission. Resolve Light
        # first for that candidate, then validate the real Heavy intent once
        # we know it is an already configured or noninteractive launch.
        setup_candidate = admission and not argv
        resolved = resolve(options.config, site=options.site, legacy_profile=options.profile,
                           launcher="heavy" if admission and not setup_candidate and not background else "light", site_policy_path=options.site_policy,
                           cli_overrides=overrides or None)
        from .onboarding import should_offer_first_run
        first_run = should_offer_first_run(resolved, argv=argv)
        if setup_candidate and not first_run and not background:
            resolved = resolve(options.config, site=options.site, legacy_profile=options.profile,
                               launcher="heavy", site_policy_path=options.site_policy,
                               cli_overrides=overrides or None)
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
                raise ValueError("Qualification requires --offline; live site pilots are separate reviewed work.")
            from .raven_qualification import main as qualification
            return qualification(remaining[1:])
        if options.write_config is not None:
            root, _state, _home = _roots(dict(os.environ))
            path = options.write_config or str(root / "raven/config.json")
            if not str(path).endswith(".json"):
                raise ValueError("The Raven bootstrap writes JSON; TOML remains an equivalent input on Python 3.11+.")
            _output(_create_config(path, resolved))
            return 0
        if options.plan:
            if options.allocation and not re.fullmatch(r"[1-9][0-9]{0,19}", options.allocation):
                raise ValueError("--allocation requires one exact numeric allocation ID.")
            allocation = ({"intent": "reuse", "job_id": options.allocation,
                           "validation": "unverified; startup requires fresh governed allocation/owner/placement checks"}
                          if options.allocation else _allocation(resolved).plan()) if heavy else None
            _output({"schema": "raven.launch-plan/v1", "intent": "heavy" if heavy else "light",
                     "configuration": resolved.explain(show_paths=options.show_paths), "allocation": allocation,
                     "mutations": 0, "dashboard_imported": False})
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
                    manager.background_startup = BackgroundHeavyStartup(manager, resolved, options.allocation)
                else:
                    _wait_heavy(manager, resolved, options.allocation)
            except BaseException:
                shutdown_heavy(manager)
                raise
        if options.json:
            remaining.append("--json")
        # Interactive sessions paint Light immediately. Heavy operations remain
        # gated by the same manager's verified WorkerProxy.ready publication.
        from . import cli
        try:
            if first_run:
                remaining.append("--getting-started")
            return cli.main(remaining, raven=resolved, raven_allocation=manager)
        finally:
            if manager is not None:
                shutdown_heavy(manager)
    except KeyboardInterrupt:
        print("Raven: startup cancelled; owned helper cleanup requested.", file=sys.stderr)
        return 130
    except (OSError, ValueError, RuntimeError, ImportError) as exc:
        print("Raven: " + str(exc), file=sys.stderr)
        return 2


def light_main(argv=None):
    return _main(argv, heavy=False)


def heavy_main(argv=None):
    return _main(argv, heavy=True)


if __name__ == "__main__":
    raise SystemExit(light_main())
