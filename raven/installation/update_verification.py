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

    source = Path(info["source"])
    declared = git(
        source,
        "config",
        "--get-all",
        "remote." + info["tracking_remote"] + ".url",
    )
    # Git applies url.*.insteadOf before fetching. Authorizing only the stored
    # spelling could label a fetch from a completely different endpoint official.
    # Inspect all URLs with the same Git environment used by the fetch, retaining
    # ordinary credential, proxy and HTTPS-to-SSH configuration.
    effective = git(source, "remote", "get-url", "--all", info["tracking_remote"])

    def official_urls(output):
        urls = output.decode("utf-8", "replace").lower().splitlines()
        normalized = [url.rstrip("/").removesuffix(".git") for url in urls]
        return bool(normalized) and all(url in OFFICIAL for url in normalized)

    declared_official = official_urls(declared)
    effective_official = official_urls(effective)
    official = declared_official and effective_official
    if not official and not allow_fork:
        raise SourceUpdateError(
            "The tracking remote does not resolve exclusively to the official Raven "
            "repository (declared: "
            + ("official" if declared_official else "custom")
            + "; effective fetch URLs: "
            + ("official" if effective_official else "custom")
            + "). Inspect git remote get-url --all REMOTE and url.*.insteadOf settings. "
            "Review the resolved source and use --allow-fork explicitly to authorize it."
        )
    policy = {
        "source_channel": "official-repository" if official else "explicit-fork",
        "artifact_attestation": "not-requested",
        "source_commit": info["target_head"],
    }
    if not require_attestation:
        return policy
    if artifact is None:
        raise SourceUpdateError(
            "--require-attestation needs --attested-artifact FILE from the selected commit. No release or checkout was changed."
        )
    if not official:
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
