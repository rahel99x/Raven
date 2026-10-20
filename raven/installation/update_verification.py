"""Explicit source-channel and optional artifact-provenance update policy."""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from ..config_trust import read
from ..release_verification import verification_command

OFFICIAL = {
    "https://github.com/rahel99x/raven",
    "git@github.com:rahel99x/raven",
    "ssh://git@github.com/rahel99x/raven",
}


def verify(
    info,
    *,
    allow_fork=False,
    require_attestation=False,
    artifact=None,
    bundle=None,
    plan=False,
):
    from ..update_source import SourceUpdateError, git

    declared = (
        git(
            Path(info["source"]),
            "config",
            "--get-all",
            "remote." + info["tracking_remote"] + ".url",
        )
        .decode()
        .strip()
        .lower()
        .rstrip("/")
    )
    normalized = declared[:-4] if declared.endswith(".git") else declared
    if normalized not in OFFICIAL and not allow_fork:
        raise SourceUpdateError(
            "The tracking remote is not the official Raven repository. Review this fork and use --allow-fork explicitly to authorize it."
        )
    policy = {
        "source_channel": "official-repository"
        if normalized in OFFICIAL
        else "explicit-fork",
        "artifact_attestation": "not-requested",
        "source_commit": info["target_head"],
    }
    if not require_attestation:
        return policy
    if artifact is None:
        raise SourceUpdateError(
            "--require-attestation needs --attested-artifact FILE from the selected commit. No release or checkout was changed."
        )
    if normalized not in OFFICIAL:
        raise SourceUpdateError(
            "The attestation policy pins rahel99x/Raven; fork artifacts need their own independently reviewed signer policy."
        )
    read(artifact, limit=64 << 20)
    if bundle is not None:
        read(bundle, limit=16 << 20)
    if plan:
        policy["artifact_attestation"] = "required-at-apply"
        return policy
    command = verification_command(
        artifact,
        repository="rahel99x/Raven",
        workflow=".github/workflows/release-candidate.yml",
        source=info["target_head"],
        bundle=bundle,
    )
    try:
        with tempfile.TemporaryFile() as output:
            result = subprocess.run(
                command,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=output,
                timeout=90,
            )
            if result.returncode or output.tell() > 4 << 20:
                raise SourceUpdateError(
                    "Artifact attestation verification failed; no release or checkout was changed. Verify the artifact, expected commit, signer workflow and GitHub CLI access."
                )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SourceUpdateError(
            "Artifact attestation verification could not complete. Install/configure gh attestation verification and provide an artifact for this exact commit; no source update was applied."
        ) from exc
    policy["artifact_attestation"] = "verified"
    return policy
