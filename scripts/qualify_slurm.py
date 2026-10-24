#!/usr/bin/env python3
"""Build and qualify an owned, disposable real Slurm service; never touch a site."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
VERSIONS = ROOT / "scripts/slurm/versions.json"


def version_spec(version):
    document = json.loads(VERSIONS.read_text())
    matches = [item for item in document["versions"] if item["version"] == version]
    if len(matches) != 1 or not re.fullmatch(r"[0-9a-f]{64}", matches[0]["sha256"]):
        raise ValueError("Select one pinned version from scripts/slurm/versions.json")
    return matches[0]


def docker_environment():
    env = dict(os.environ)
    # Preserve registry credentials and inherited proxy/CA configuration. Only
    # remove selectors that could redirect this destructive disposable fixture.
    for name in ("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_TLS", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH"):
        env.pop(name, None)
    return env


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", default="26.05.5")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--skip-build", action="store_true")
    parser.add_argument("--scenario", choices=("lifecycle", "queue-observations", "allocation-recovery", "forecast-observations", "trace-workspace"), default="lifecycle")
    parser.add_argument("--plan", action="store_true", help="Print scope without contacting Docker")
    args = parser.parse_args(argv)
    spec = version_spec(args.version)
    if args.plan:
        print(json.dumps({"version": spec, "source_root": str(ROOT), "output": str(args.output),
                          "scope": "owned disposable privileged container; internal network; no host ports",
                          "production_qualified": False, "scenario": args.scenario}, indent=2))
        return 0
    # Reports are immutable; a failed run remains available for diagnosis.
    args.output = args.output.absolute()
    args.output.mkdir(parents=True, exist_ok=False)
    env = docker_environment()
    env["BUILDX_CONFIG"] = str(args.output / "buildx")
    docker = ["docker", "--host=unix:///var/run/docker.sock"]
    name = "raven-qualify-" + uuid.uuid4().hex[:12]
    network, image = name + "-net", "raven-slurm-qualification:" + args.version
    created_network = created_container = False

    def call(arguments, *, timeout=30, capture=True):
        return subprocess.run(docker + arguments, env=env, check=True, text=True,
                              capture_output=capture, timeout=timeout)

    try:
        call(["info", "--format", "{{.ServerVersion}}"])
        minimum_free = (8 if not args.skip_build else 4) << 30
        if shutil.disk_usage(ROOT).free < minimum_free:
            raise ValueError("Disposable Slurm qualification needs at least " + str(minimum_free >> 30) +
                             " GiB free on the checkout filesystem; preserve source and reclaim only owned obsolete build resources first")
        if not args.skip_build:
            ca = os.environ.get("CODEX_PROXY_CERT") or "/etc/ssl/certs/ca-certificates.crt"
            with (args.output / "build.log").open("w") as log:
                command = docker + ["build", "--secret", "id=proxy_ca,src=" + ca,
                                    "--build-arg", "SLURM_VERSION=" + spec["version"],
                                    "--build-arg", "SLURM_SHA256=" + spec["sha256"],
                                    "--build-arg", "BUILD_JOBS=2", "-t", image, str(ROOT / "scripts/slurm")]
                subprocess.run(command, env=env, check=True, stdout=log, stderr=subprocess.STDOUT, timeout=1200)
        if shutil.disk_usage(ROOT).free < 4 << 30:
            raise ValueError("At least 4 GiB must remain after building before starting the disposable Slurm service")
        info = call(["image", "inspect", image, "--format", "{{.Id}}"])
        (args.output / "image-id.txt").write_text(info.stdout)
        created_network = True
        call(["network", "create", "--internal", network])
        created_container = True
        call(["run", "--detach", "--name", name, "--hostname", "raven-ci", "--privileged",
              "--network", network, "--cpus", "2", "--memory", "2g",
              "--mount", "type=bind,src=" + str(ROOT) + ",dst=/raven,readonly",
              "--mount", "type=bind,src=" + str(args.output) + ",dst=/evidence",
              "--entrypoint", "/usr/bin/tini", image, "--", "/bin/bash", "/raven/scripts/slurm/entrypoint.sh"])
        until = time.monotonic() + 90
        while time.monotonic() < until:
            ready = subprocess.run(docker + ["exec", name, "test", "-f", "/tmp/raven-services-started"],
                                   env=env, capture_output=True, timeout=5)
            if ready.returncode == 0:
                break
            running = call(["inspect", name, "--format", "{{.State.Running}}"])
            if running.stdout.strip() != "true":
                raise ValueError("Slurm service container exited during initialization; see service logs")
            time.sleep(.5)
        else:
            raise ValueError("Slurm services did not initialize within 90 seconds")
        # The host checkout may be private to its owner. Prepare a readable
        # source copy only inside this disposable container for the raven UID;
        # never loosen host permissions or copy user configuration/credentials.
        call(["exec", name, "sh", "-c", "mkdir -p /opt/raven-qualification/scripts && cp -R /raven/raven /opt/raven-qualification/raven && cp -R /raven/scripts/slurm /opt/raven-qualification/scripts/slurm && chmod -R a+rX /opt/raven-qualification"])
        revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
        driver = args.scenario.replace("-", "_") + ".py"
        call(["exec", "--env", "RAVEN_QUALIFICATION_ROOT=/opt/raven-qualification", name, "python3", "/opt/raven-qualification/scripts/slurm/" + driver, "--output", "/evidence",
              "--revision", revision], timeout=600 if args.scenario == "forecast-observations" else 300, capture=False)
        return 0
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print("Slurm qualification failed: " + str(exc), file=sys.stderr)
        if isinstance(exc, subprocess.CalledProcessError) and exc.stderr:
            print(exc.stderr[-4096:], file=sys.stderr)
        return 1
    finally:
        if created_container:
            try:
                subprocess.run(docker + ["cp", name + ":/var/log/slurm", str(args.output / "daemon-logs")],
                               env=env, capture_output=True, timeout=15)
            except (OSError, subprocess.SubprocessError):
                pass
            for filename, command in (("service.log", ["logs", name]),
                                      ("slurm-services.log", ["exec", name, "sh", "-c", "tail -n 150 /var/log/slurm/*.log"]),
                                      ("system-packages.txt", ["exec", name, "dpkg-query", "-W"])):
                try:
                    with (args.output / filename).open("w") as log:
                        subprocess.run(docker + command, env=env, stdout=log, stderr=subprocess.STDOUT, timeout=15)
                except (OSError, subprocess.SubprocessError):
                    pass  # Diagnostic failure must not bypass owned cleanup.
            try:
                subprocess.run(docker + ["rm", "-f", name], env=env, capture_output=True, timeout=30)
            except (OSError, subprocess.SubprocessError) as exc:
                print("Owned container cleanup failed for " + name + ": " + str(exc), file=sys.stderr)
        if created_network:
            try:
                subprocess.run(docker + ["network", "rm", network], env=env, capture_output=True, timeout=15)
            except (OSError, subprocess.SubprocessError) as exc:
                print("Owned network cleanup failed for " + network + ": " + str(exc), file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
