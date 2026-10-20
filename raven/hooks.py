"""Bounded user-authorized hook execution, isolated from scheduler work."""

from __future__ import annotations

import json
import os
import signal
import subprocess


def run_hook(command, events, *, timeout=20, env=None):
    """One invocation per publication; stop its entire session on timeout."""
    process = subprocess.Popen(
        command,
        shell=True,
        env=env,
        start_new_session=True,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        process.communicate(
            json.dumps(
                {"schema": "raven.hook-events/v1", "events": events},
                ensure_ascii=True,
                default=str,
            ).encode(),
            timeout=timeout,
        )
    except BaseException:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            pass
        # The shell may exit before a descendant that ignores TERM. The
        # session group remains ours even after its leader has been reaped.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()
        raise
    return process.returncode
