#!/usr/bin/env python3
"""Verify release bytes and GitHub/Sigstore provenance against caller trust."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess
import sys

from release_inventory import verify_release_policy


def verification_command(artifact, *, repository, workflow, source, bundle=None):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("Expected an explicit owner/repository trust identity")
    if not re.fullmatch(r"\.github/workflows/[A-Za-z0-9_.-]+\.ya?ml", workflow):
        raise ValueError("Expected one repository workflow path")
    if not re.fullmatch(r"[0-9a-f]{40}", source):
        raise ValueError("Expected the full independently selected source commit")
    command = ["gh", "attestation", "verify", str(Path(artifact).absolute()), "--repo", repository,
               "--signer-workflow", repository + "/" + workflow, "--signer-digest", source,
               "--source-digest", source, "--deny-self-hosted-runners", "--format", "json"]
    if bundle:
        command += ["--bundle", str(Path(bundle).absolute())]
    return command


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inventory")
    parser.add_argument("artifacts", nargs="+")
    parser.add_argument("--repository", required=True)
    parser.add_argument("--workflow", default=".github/workflows/release-candidate.yml")
    parser.add_argument("--source", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--tag")
    parser.add_argument("--bundle")
    parser.add_argument("--plan", action="store_true", help="Validate bytes and print verification commands; does not verify signatures")
    args = parser.parse_args(argv)
    try:
        verify_release_policy(args.inventory, args.artifacts, expected_version=args.version,
                              expected_source=args.source, tag=args.tag)
        commands = [verification_command(artifact, repository=args.repository, workflow=args.workflow,
                                         source=args.source, bundle=args.bundle) for artifact in args.artifacts]
        if args.plan:
            print(json.dumps({"signature_verified": False, "commands": commands}, indent=2))
            return 0
        for command in commands:
            # gh performs certificate, transparency-log, identity and artifact
            # digest verification. Caller policy is never read from the bundle.
            subprocess.run(command, check=True, timeout=90, stdin=subprocess.DEVNULL)
        print("Artifact digests and expected repository/workflow/source provenance verified.")
        return 0
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print("Release verification failed: " + str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
