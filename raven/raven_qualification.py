"""Bounded, offline qualification evidence; never a production scale certificate.

The cohort exercise uses the actual admission ledger and synthetic identities.
It sends no scheduler commands, reads no job payloads, and starts no allocations.
"""
from __future__ import annotations

from collections import Counter
import argparse
import hashlib
import json
from pathlib import Path
import platform
import tempfile
import time


def cohort_report(directory, users, *, sessions=2):
    """Record emitted attempts for distinct users plus duplicate session traffic.

    Identifiers model separate authorities and independent private databases,
    not a multi-user deployment or an authentication mechanism.
    """
    from .raven_collection import Ledger, Deferred, classify, DEFAULTS
    from .shared_collection import SharedQueryCache
    if type(users) is not int or not 1 <= users <= 256 or type(sessions) is not int or not 1 <= sessions <= 4:
        raise ValueError("Offline cohorts require 1..256 users and 1..4 sessions per user")
    timer = [1000.]
    owners = [Ledger(SharedQueryCache(Path(directory) / ("cohort-" + str(users)) / str(user)), {"backend": "fixture", "host": "fake-controller", "uid": user,
                            "user": "synthetic-" + str(user)}, {}, now=lambda: timer[0]) for user in range(users)]
    emitted, denied, by_user = Counter(), Counter(), Counter()
    digest = hashlib.sha256()
    # The first window deliberately hammers distinct source arguments; this
    # bypasses cache benefits to measure the conservative admission ceiling.
    # The second adds failed producers and then a recovery epoch.
    for epoch, offsets in (("startup-refresh", (0., .1, .2, 2., 4., 6., 8., 10.)),
                           ("outage", (61., 61.1, 63., 65.)),
                           ("recovery", (190., 190.1, 192., 194.))):
        for offset in offsets:
            timer[0] = 1000. + offset
            for user, owner in enumerate(owners):
                for session in range(sessions):
                    for endpoint, argv in (("controller", ["squeue", "-u", str(user), "-j", str(session) + str(offset)]),
                                           ("accounting", ["sacct", "-u", str(user)])):
                        try:
                            with owner.admit(classify(argv), argv):
                                emitted[epoch, endpoint] += 1
                                by_user[epoch, endpoint, user] += 1
                                digest.update(json.dumps([epoch, offset, user, session, endpoint], separators=(",", ":")).encode())
                                if epoch == "outage":
                                    raise OSError("synthetic endpoint outage")
                        except Deferred:
                            denied[epoch, endpoint] += 1
                        except OSError:
                            pass
    limits = {"controller": DEFAULTS["controller_requests_per_minute"],
              "accounting": DEFAULTS["accounting_requests_per_minute"]}
    for (_epoch, endpoint, _user), count in by_user.items():
        if count > limits[endpoint]:
            raise AssertionError("Actual emitted attempts exceeded a per-user rolling ceiling")
    return {"users": users, "sessions_per_user": sessions,
            "scope": "synthetic distinct authorities; actual ledger; no network or scheduler",
            "cross_user_coalescing": False, "authentication_qualified": False,
            "nominal_queue_queries_per_second_at_60s": users / 60.,
            "maximum_emitted_per_user_per_test_window": {endpoint: max((n for (_, e, _), n in by_user.items() if e == endpoint), default=0) for endpoint in limits},
            "windows": [{"phase": phase, "endpoint": endpoint, "emitted": emitted[phase, endpoint],
                         "deferred": denied[phase, endpoint]} for phase in ("startup-refresh", "outage", "recovery") for endpoint in limits],
            "emitted_attempts_sha256": digest.hexdigest(), "ceilings_passed": True}


def qualify(*, cohorts=(1, 10, 50, 200, 256)):
    from . import __version__
    from .raven_config import resolve, to_raven_config
    from .raven_execution import capability_ledger
    from .raven_protocol import Decoder, encode
    from .raven_allocation_adapters import prepare
    started = time.monotonic()
    checks = []
    with tempfile.TemporaryDirectory(prefix="raven-offline-") as directory:
        root = Path(directory)
        env = {"HOME": str(root), "XDG_CONFIG_HOME": str(root / "config"), "XDG_STATE_HOME": str(root / "state")}
        desktop = resolve(site="desktop", env=env, system_policy_paths=[])
        plan = prepare(desktop)
        assert plan["site_kind"] == "desktop"
        assert to_raven_config(desktop)["raven"]["managed"] is True
        checks.append({"id": "explicit-desktop-plan", "status": "passed", "allocations_started": 0})
        payload = {"op": "ping", "generation": 1, "text": "Raven · 🐦"}
        frame = encode(payload)
        for boundary in range(len(frame) + 1):
            decoder = Decoder()
            decoder.feed(frame[:boundary])
            decoded = decoder.take()
            decoder.feed(frame[boundary:])
            decoded += decoder.take()
            assert decoded == [payload]
        checks.append({"id": "protocol-fragmentation", "status": "passed", "boundaries": len(frame) + 1})
        cohorts = [cohort_report(root, size) for size in cohorts]
        checks.append({"id": "distinct-authority-load", "status": "passed", "cohorts": cohorts})
    return {"schema": "raven.qualification/v1", "version": __version__, "python": platform.python_version(),
            "scope": "deterministic offline configuration, framing, and synthetic admission",
            "production_qualified": False, "seconds": round(time.monotonic() - started, 3),
            "checks": checks, "capabilities": capability_ledger(),
            "untested": ["real Slurm allocation and placement", "production controller RPC impact", "site service authentication",
                         "multi-hour soak", "physical GPUs", "multiple nodes", "production SSH", "administrator-approved 200-user pilot"]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline", action="store_true", required=True)
    parser.parse_args(argv)
    try:
        report = qualify()
    except (OSError, ValueError, RuntimeError, AssertionError) as exc:
        print(json.dumps({"schema": "raven.qualification/v1", "status": "failed", "error": str(exc)[:1000], "production_qualified": False}))
        return 1
    print(json.dumps(report, indent=2, allow_nan=False))
    return 0
