"""Guided first-run setup, with local checks and explicitly saved profiles.

Importing this module performs no I/O. Presence checks never contact a scheduler
or infer that a compute node can run the locally installed interpreter.
"""

from __future__ import annotations

import argparse
import json
import sys

from ..configuration import policy as C

KINDS = (
    ("local-slurm", "Workstation or cluster login with local Slurm commands"),
    ("ssh-slurm", "HPC cluster reached through SSH"),
    ("standalone", "Standalone workstation without Slurm"),
)
FORM_FIELDS = (
    "kind",
    "name",
    "host",
    "user",
    "cluster",
    "worker_python",
    "account",
    "partition",
)
DEMO_ARGS = ["--fake", "--no-state", "--no-plugins"]


from .onboarding_service import should_offer_first_run as should_offer_first_run


from .onboarding_service import installation_report as installation_report


from .onboarding_service import report_lines as report_lines


from .onboarding_service import default_form as default_form


from .onboarding_service import stage_form as stage_form


from .onboarding_service import preview_lines as preview_lines


from .onboarding_service import _draft_path as _draft_path


from .onboarding_service import save_progress as save_progress


from .onboarding_service import load_progress as load_progress


def _parser():
    parser = argparse.ArgumentParser(
        prog="raven setup",
        description="Guided installation checks and saved site/lab profiles; never submits or allocates.",
    )
    parser.add_argument("--config", help="Existing Raven configuration to extend")
    parser.add_argument("--site", help="Existing profile to review")
    parser.add_argument("--kind", choices=[key for key, _ in KINDS])
    parser.add_argument(
        "--name", help="Friendly profile name, such as LabX GPU experiments"
    )
    parser.add_argument(
        "--host", help="SSH host alias (uses existing SSH authentication)"
    )
    parser.add_argument("--user", help="SSH login, separate from the billing account")
    parser.add_argument("--cluster", help="Optional Slurm cluster identity")
    parser.add_argument(
        "--worker-python",
        help="Python with the same Raven release installed on the worker host",
    )
    parser.add_argument(
        "--account",
        help="Raven Heavy billing account; an empty value uses the Slurm default",
    )
    parser.add_argument("--partition", help="Raven Heavy partition")
    parser.add_argument(
        "--default",
        action="store_true",
        help="Use this profile for future launches without --site",
    )
    parser.add_argument(
        "--resume", action="store_true", help="Continue an explicitly saved draft"
    )
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument(
        "--check", action="store_true", help="Print local installation checks only"
    )
    actions.add_argument(
        "--review", action="store_true", help="Preview a profile without saving"
    )
    actions.add_argument(
        "--save",
        action="store_true",
        help="Explicitly save the reviewed profile settings",
    )
    actions.add_argument(
        "--save-draft",
        action="store_true",
        help="Save progress without changing configuration",
    )
    actions.add_argument(
        "--demo",
        action="store_true",
        help="Open simulated jobs without scheduler access or saved state",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Machine-readable checks or preview; requires an explicit action",
    )
    return parser


def run_setup(argv=None, *, config_path=None, site=None, input_fn=None, demo=None):
    args = _parser().parse_args(argv)
    if args.demo:
        if demo is None:
            raise ValueError("Demo requires the CLI-composed launch callback")
        return demo(DEMO_ARGS)
    try:
        resolved = C.resolve(args.config or config_path, site=args.site or site)
        if args.check:
            report = installation_report(resolved)
            print(
                json.dumps(report, indent=2)
                if args.json
                else "\n".join(report_lines(report))
            )
            return 0
        from ..configuration.profiles import ProfileSetup

        service = ProfileSetup(resolved)
        form, make_default = default_form(resolved), args.default or not resolved.source
        if args.resume:
            saved = load_progress(resolved)
            if saved is None:
                raise ValueError(
                    "No saved setup draft exists. Run raven setup to begin."
                )
            form, make_default = saved["form"], args.default or saved["make_default"]
        previous_kind = form["kind"]
        for key in FORM_FIELDS:
            value = getattr(args, key, None)
            if value is not None:
                form[key] = value
        if args.kind == "ssh-slurm" and args.worker_python is None and not args.resume:
            form["worker_python"] = ""
        elif args.kind and args.kind != previous_kind and args.worker_python is None:
            form["worker_python"] = sys.executable
        if args.kind in ("standalone", "local-slurm"):
            form.update(host="", user="")
        if args.kind == "standalone":
            form.update(account="", partition="")
        explicit = args.review or args.save or args.save_draft
        if explicit and not args.resume and (not args.kind or not args.name):
            raise ValueError(
                "Provide --kind and --name for scripted setup, or use --resume."
            )
        if args.save_draft:
            result = {
                "draft": save_progress(resolved, form, make_default),
                "configuration_changed": False,
            }
            print(
                json.dumps(result, indent=2)
                if args.json
                else "Saved setup progress: " + result["draft"]
            )
            return 0
        if not explicit:
            if args.json or (input_fn is None and not sys.stdin.isatty()):
                raise ValueError(
                    "Interactive setup needs a terminal. Use --check or --kind KIND --name NAME --review/--save."
                )
            completed = _interactive(resolved, form, make_default, input_fn or input)
            if completed is None:
                return 0
            form, make_default = completed
        draft = stage_form(service, form, make_default=make_default)
        if not args.json:
            print("\n".join(preview_lines(draft)))
        if args.review:
            if args.json:
                print(json.dumps(draft, indent=2))
            return 0
        if not args.save:
            answer = (input_fn or input)("Save this profile? [y/N] ").strip().lower()
            if answer not in ("y", "yes"):
                print(
                    "Configuration unchanged. Reopen raven setup, or use --save-draft to retain a draft."
                )
                return 0
        result = service.save(draft)
        if args.json:
            print(json.dumps(result, indent=2))
        else:
            print("\nSaved. Open Raven with:\n  " + result["command"])
            print(
                "Use Setup > Heavy allocation settings for association discovery on the connected site."
            )
        return 0
    except (OSError, ValueError, RuntimeError) as exc:
        print("Raven setup: " + str(exc), file=sys.stderr)
        return 2
    except (EOFError, KeyboardInterrupt):
        print("\nSetup cancelled. Configuration unchanged.", file=sys.stderr)
        return 130


from .onboarding_service import _interactive as _interactive
