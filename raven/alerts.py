"""Alert rules: expressions over each job (or over the whole snapshot) that ring, notify, log an event or run a
command when they hold, with a rate limit per job and rule.

    [[alerts]]
    name = "idle gpu"
    when = "running and gpus and gpu is not None and gpu < 15 and elapsed > 900"
    every = 1800                      # seconds before the same job fires this rule again (0: once per job)
    actions = ["bell", "event"]       # bell | event | notify (the [notify] command with RAVEN_EVENT=alert) | command
    command = ""                      # with "command": a shell command, RAVEN_ALERT RAVEN_JOBID RAVEN_JOBNAME RAVEN_TEXT in the environment
    scope = "job"                     # job: per job; cluster: once over the snapshot (n_pending, free_gpus ...)
"""
from __future__ import annotations

import os
import subprocess
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Callable, Dict, List, Optional

from .expr import Expr, ExprError, cluster_ns, job_ns
from .worker_scheduler import WorkerScheduler


class Rule:
    def __init__(self, spec: dict):
        self.name = str(spec.get("name") or spec.get("when", "")[:30])
        self.when = str(spec.get("when", ""))
        self.every = float(spec.get("every", 1800))
        self.actions = list(spec.get("actions", ["bell", "event"]))
        self.command = str(spec.get("command", ""))
        self.scope = str(spec.get("scope", "job"))
        self.error = ""
        try:
            self.expr: Optional[Expr] = Expr(self.when)
        except ExprError as e:
            self.expr, self.error = None, str(e)
        self.fired: Dict[str, float] = {}             # job id (or "*" for cluster scope) -> last time it fired
        self.active: set = set()                      # keys currently true

    def due(self, key: str, now: float) -> bool:
        last = self.fired.get(key)
        if last is None:
            return True
        if self.every <= 0:
            return False
        return now - last >= self.every


class AlertEngine:
    """``check(snap)`` evaluates every rule; ``bell`` is a counter the screen watches."""

    def __init__(self, rules: List[dict], store, user: str = "", notify: Optional[Callable[[dict], None]] = None, bell: Optional[Callable[[], None]] = None,
                 *, worker_scheduler=None):
        self.rules = [Rule(r) for r in rules if isinstance(r, dict) and r.get("when")]
        self.store, self.user, self.notify, self.bell_fn = store, user, notify, bell
        self.bell = 0
        self.errors: Dict[str, str] = {r.name: r.error for r in self.rules if r.error}
        self.recent: List[dict] = []                   # the last alerts, newest last
        self.lock = threading.Lock()
        self._owns_worker_scheduler = worker_scheduler is None
        self.worker_scheduler = worker_scheduler or WorkerScheduler(lanes=("notification",))
        self.pool = self.worker_scheduler.lane("notification")
        self.snoozes = {}
        self.quiet = None
        self.quiet_zone = "America/Los_Angeles"
        for name, err in self.errors.items():
            store.event("alert_error", f"alert rule '{name}': {err}", name=name)

    def add(self, when: str, name: str = "", actions=("bell", "event"), every: float = 1800, scope: str = "job") -> Rule:
        r = Rule(dict(name=name or when, when=when, actions=list(actions), every=every, scope=scope))
        self.rules.append(r)
        if r.error:
            self.errors[r.name] = r.error
        return r

    def check(self, snap: dict, marks=(), tags=None) -> List[dict]:
        """Evaluate the rules over the snapshot; returns the alerts fired now."""
        if not self.rules:
            return []
        now = time.time()
        fired: List[dict] = []
        cns = None
        for rule in self.rules:
            if rule.expr is None:
                continue
            try:
                if rule.scope == "cluster":
                    cns = cns if cns is not None else cluster_ns(snap, self.user, marks, tags)
                    hits = [("*", "", bool(rule.expr(cns)))]
                else:
                    hits = []
                    for j in snap.get("jobs", []):
                        ns = job_ns(j, snap, marks, tags)
                        hits.append((j.id, j.name, bool(rule.expr(ns))))
            except ExprError as e:
                if rule.name not in self.errors:
                    self.errors[rule.name] = str(e)
                    self.store.event("alert_error", f"alert rule '{rule.name}': {e}", name=rule.name)
                continue
            rule.active.intersection_update(key for key, _name, _hit in hits)
            for key, name, hit in hits:
                if not hit:
                    rule.active.discard(key)
                    continue
                first = key not in rule.active
                rule.active.add(key)
                if not (first and rule.every <= 0 and key not in rule.fired) and not rule.due(key, now):
                    continue
                if rule.every <= 0 and key in rule.fired:
                    continue
                rule.fired[key] = now
                text = f"{rule.name}: {key + ' ' + name if key != '*' else rule.when}".strip()
                ev = self.store.event("alert", text, job_id=key if key != "*" else "", name=name, rule=rule.name)
                fired.append(ev)
                self.fire(rule, ev)
        if fired:
            with self.lock:
                self.recent = (self.recent + fired)[-50:]
        return fired

    def fire(self, rule: Rule, ev: dict):
        if self.notification_muted(rule.name, str(ev.get("job", "") or "*")):
            return
        if "bell" in rule.actions:
            self.bell += 1
            if self.bell_fn:
                try:
                    self.bell_fn()
                except Exception:
                    pass
        if "notify" in rule.actions and self.notify:
            try:
                self.notify(dict(ev, kind="alert"))
            except Exception:
                pass
        if "command" in rule.actions and rule.command:
            env = dict(os.environ, RAVEN_EVENT="alert", RAVEN_ALERT=rule.name, RAVEN_JOBID=str(ev.get("job", "")), RAVEN_JOBNAME=str(ev.get("name", "")), RAVEN_TEXT=ev["text"])

            def go():
                try:
                    subprocess.run(rule.command, shell=True, env=env, timeout=30, capture_output=True)
                except (OSError, subprocess.TimeoutExpired):
                    pass

            try:
                self.pool.submit(go)
            except RuntimeError as exc:
                self.errors[rule.name] = str(exc)[:512]

    def close(self):
        self.pool.shutdown(wait=False, cancel_futures=True)
        if self._owns_worker_scheduler:
            self.worker_scheduler.shutdown(wait=False, cancel_futures=True)

    def snooze(self, rule, seconds, job="*"):
        """Mute notification delivery; condition evaluation and events continue."""
        if rule not in {item.name for item in self.rules}:
            raise ValueError("Choose a configured alert rule.")
        seconds = float(seconds)
        if not 0 < seconds <= 604800:
            raise ValueError("Snooze duration must be between zero and seven days.")
        if not isinstance(job, str) or not job or len(job) > 128 or not job.isprintable():
            raise ValueError("Choose an exact job ID or * for the complete rule.")
        with self.lock:
            self._prune_snoozes(time.time())
            if len(self.snoozes) >= 256 and (rule, job) not in self.snoozes:
                raise ValueError("At most 256 alert snoozes are available.")
            self.snoozes[(rule, job)] = time.time() + seconds

    def _prune_snoozes(self, now):
        """Caller holds the controls lock; remove expired bounded preferences."""
        self.snoozes = {key: until for key, until in self.snoozes.items() if until > now}

    def unsnooze(self, rule=None, job="*"):
        with self.lock:
            if rule is None:
                self.snoozes.clear()
            else:
                self.snoozes.pop((rule, job), None)

    def set_quiet(self, start=None, end=None, zone="America/Los_Angeles"):
        if start is None:
            with self.lock:
                self.quiet = None
            return
        def minutes(value):
            parts = str(value).split(":")
            if len(parts) != 2 or not all(len(p) == 2 and p.isascii() and p.isdigit() for p in parts):
                raise ValueError("Quiet hours use HH:MM.")
            hour, minute = map(int, parts)
            if not 0 <= hour < 24 or not 0 <= minute < 60:
                raise ValueError("Quiet hours use valid 24-hour times.")
            return hour * 60 + minute
        window = minutes(start), minutes(end)
        if window[0] == window[1]:
            raise ValueError("Choose different start and end times, or use quiet off.")
        if not isinstance(zone, str) or not zone or len(zone) > 96 or not zone.isprintable():
            raise ValueError("Choose a valid IANA timezone name.")
        ZoneInfo(zone)  # Validate the zone before changing the current controls.
        with self.lock:
            self.quiet, self.quiet_zone = window, zone

    def notification_muted(self, rule, job="*", now=None):
        now = time.time() if now is None else now
        with self.lock:
            self._prune_snoozes(now)
            if any(self.snoozes.get(key, 0) > now for key in ((rule, "*"), (rule, job))):
                return True
            window, zone = self.quiet, self.quiet_zone
        if window:
            local = datetime.fromtimestamp(now, ZoneInfo(zone))
            minute = local.hour * 60 + local.minute
            start, end = window
            return start <= minute < end if start < end else minute >= start or minute < end
        return False

    def controls_snapshot(self):
        now = time.time()
        with self.lock:
            self._prune_snoozes(now)
            return {"snoozes": [{"rule": rule, "job": job, "until": until}
                                for (rule, job), until in self.snoozes.items() if until > now],
                    "quiet": list(self.quiet) if self.quiet else None, "zone": self.quiet_zone}

    def restore_controls(self, value):
        """Restore bounded preferences; invalid controls never alter rules."""
        import math
        if not isinstance(value, dict):
            return
        valid = {item.name for item in self.rules}
        now = time.time()
        result = {}
        entries = value.get("snoozes", [])
        for entry in entries[:256] if isinstance(entries, list) else []:
            if not isinstance(entry, dict):
                continue
            rule, job, until = entry.get("rule"), entry.get("job"), entry.get("until")
            if (isinstance(rule, str) and rule in valid and isinstance(job, str) and job.isprintable()
                    and 0 < len(job) <= 128 and isinstance(until, (int, float)) and math.isfinite(until)
                    and now < until <= now + 604800):
                result[(rule, job)] = until
        with self.lock:
            self.snoozes = result
        window, zone = value.get("quiet"), value.get("zone", "America/Los_Angeles")
        if "quiet" in value and window is None:
            self.set_quiet()
        if (isinstance(window, (list, tuple)) and len(window) == 2
                and all(isinstance(x, int) and not isinstance(x, bool) and 0 <= x < 1440 for x in window)
                and isinstance(zone, str) and len(zone) <= 96):
            try:
                self.set_quiet(*(f"{m // 60:02}:{m % 60:02}" for m in window), zone=zone)
            except (ValueError, KeyError):
                pass

    def active_count(self) -> int:
        return sum(len(r.active.copy()) for r in self.rules)

    def active_text(self) -> List[str]:
        out = []
        for r in self.rules:
            for key in sorted(r.active.copy()):
                out.append(f"{r.name}" + (f" {key}" if key != "*" else ""))
        return out
