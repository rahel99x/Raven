"""Caller-pinned GitHub/Sigstore signer and source identity policy."""

import re
from pathlib import Path


def verification_command(artifact, *, repository, workflow, source, bundle=None):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("Expected an explicit owner/repository trust identity")
    if not re.fullmatch(r"\.github/workflows/[A-Za-z0-9_.-]+\.ya?ml", workflow):
        raise ValueError("Expected one repository workflow path")
    if not re.fullmatch(r"[0-9a-f]{40}", source):
        raise ValueError("Expected the full independently selected source commit")
    command = [
        "gh",
        "attestation",
        "verify",
        str(Path(artifact).absolute()),
        "--repo",
        repository,
        "--signer-workflow",
        repository + "/" + workflow,
        "--signer-digest",
        source,
        "--source-digest",
        source,
        "--deny-self-hosted-runners",
        "--format",
        "json",
    ]
    if bundle:
        command += ["--bundle", str(Path(bundle).absolute())]
    return command
