"""Actions on jobs (cancel, hold, release, requeue, top) with an audit trail in the events log, and the
notification hook that runs a configured command on transitions."""
from __future__ import annotations

import os
import subprocess
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from . import clock
from .model import Job, Store
from .slurm import Slurm
from .worker_scheduler import WorkerScheduler

VERBS = {"cancel": ("scancel", "cancelling"), "hold": ("scontrol hold", "holding"), "release": ("scontrol release", "releasing"),
         "requeue": ("scontrol requeue", "requeueing"), "top": ("scontrol top", "moving up")}


class Actions:
    def __init__(self, slurm: Slurm, store: Store, on_done: Optional[Callable[[], None]] = None, emit: Optional[Callable[[List[dict]], None]] = None):
        self.slurm, self.store, self.on_done = slurm, store, on_done
        self.emit = emit                                   # the sampler's hook runner: plugins and notifications see actions too
        self.marks: Dict[str, Tuple[str, float]] = {}      # id -> (mark, t)

    def applicable(self, action: str, job: Job) -> Tuple[bool, str]:
        if action == "cancel":
            return True, ""
        if action == "hold":
            if not job.pending:
                return False, "hold applies to pending jobs"
            return (not job.held, "already held") if job.held else (True, "")
        if action == "release":
            if job.reason == "JobHeldAdmin":
                return False, "administrative holds cannot be released by the job owner"
            return (job.pending and job.held, "not held")
        if action == "requeue":
            return (True, "") if not job.pending else (False, "requeue applies to running jobs")
        if action == "top":
            return (job.pending, "top applies to pending jobs")
        return False, f"unknown action {action}"

    def run(self, action: str, jobs: Sequence[Job]) -> Tuple[bool, str]:
        jobs = [j for j in jobs if self.applicable(action, j)[0]]
        if not jobs:
            return False, "nothing applicable"
        ids = [j.id for j in jobs]
        fn = {"cancel": self.slurm.cancel, "hold": self.slurm.hold, "release": self.slurm.release, "requeue": self.slurm.requeue, "top": self.slurm.top}[action]
        # Slurm can apply a subset and then return a nonzero batch status.
        # Observe each independent mutation; native top is one ordered scope.
        outcomes = [fn(ids)] if action in {"cancel", "top"} else [fn([jid]) for jid in ids]
        results = {job.id: outcomes[0] if len(outcomes) == 1 else outcomes[index] for index, job in enumerate(jobs)}
        ok = all(result[0] for result in results.values())
        out = "\n".join((jid + ": " + result[1]).rstrip() for jid, result in results.items() if result[1])
        verb, mark = VERBS[action]
        for j in jobs:
            if results[j.id][0]:
                self.marks[j.id] = (mark, clock.now())
        evs = [self.store.event("action", f"{verb} {j.id} {j.name}: {'ok' if results[j.id][0] else 'failed'} {results[j.id][1]}".rstrip(), j, action=action, ok=results[j.id][0]) for j in jobs]
        if self.emit:
            self.emit(evs)
        if self.on_done:
            self.on_done()
        return ok, out

    def resubmit(self, clone) -> Tuple[bool, str, Optional[str]]:
        """sbatch the clone; an audit event either way."""
        from .resubmit_intent import submit
        ok, out, new_id = submit(self.slurm, self.store, clone)
        outcome = "accepted" if ok else "unknown" if out.startswith("Resubmit outcome unknown") else "rejected"
        label = "unknown" if outcome == "unknown" else "ok" if ok else "failed"
        text = f"resubmitted {clone.id} {clone.name} as {new_id}" if ok and new_id else f"resubmit {clone.id} {clone.name}: {label} {out}".rstrip()
        ev = self.store.event("action", text + f"  [{clone.command()}]", job_id=clone.id, name=clone.name, action="resubmit", ok=ok, outcome=outcome, new_id=new_id or "")
        if self.emit:
            self.emit([ev])
        if self.on_done:
            self.on_done()
        return ok, out, new_id

    def mark(self, job: Job) -> str:
        m = self.marks.get(job.id)
        if not m:
            return ""
        mark, t = m
        if mark == "holding" and job.held or mark == "releasing" and not job.held or clock.now() - t > 120:
            del self.marks[job.id]
            return ""
        return mark


class Notifier:
    """Runs ``command`` (through the shell) for the configured event kinds with the event in the environment."""

    def __init__(self, command: str, kinds: Sequence[str], bell: Callable[[], None] = None, bell_kinds: Sequence[str] = ("started",), *, worker_scheduler=None):
        self.command, self.kinds, self.bell, self.bell_kinds = command, set(kinds), bell, set(bell_kinds)
        self._owns_worker_scheduler = worker_scheduler is None
        self.worker_scheduler = worker_scheduler or WorkerScheduler(lanes=("notification",))
        self.pool = self.worker_scheduler.lane("notification")
        self.error = ""

    def close(self):
        self.pool.shutdown(wait=False, cancel_futures=True)
        if self._owns_worker_scheduler:
            self.worker_scheduler.shutdown(wait=False, cancel_futures=True)

    def __call__(self, ev: dict):
        return self.notify_many([ev])

    def notify_many(self, events):
        events = [dict(event) for event in events]
        for event in events:
            if self.bell and event.get("kind", "") in self.bell_kinds:
                try:
                    self.bell()
                except Exception:
                    pass
        events = [event for event in events if event.get("kind", "") in self.kinds]
        if not self.command or not events:
            return None
        first = events[0]
        kinds = {event.get("kind", "") for event in events}
        kind = first.get("kind", "") if len(kinds) == 1 else "batch"
        env = dict(os.environ, RAVEN_EVENT=kind, RAVEN_JOBID=str(first.get("job", "")),
                   RAVEN_JOBNAME=str(first.get("name", "")), RAVEN_TEXT=str(first.get("text", "")))
        from .hooks import run_hook
        def go():
            try:
                return run_hook(self.command, events, env=env)
            except (OSError, subprocess.TimeoutExpired):
                return None
        try:
            return self.pool.submit(go)
        except RuntimeError as exc:
            self.error = str(exc)[:512]
