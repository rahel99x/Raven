"""Actions on jobs (cancel, hold, release, requeue, top) with an audit trail in the events log, and the
notification hook that runs a configured command on transitions."""
from __future__ import annotations

import os
import subprocess
import time
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
        ok, out = fn(ids)
        verb, mark = VERBS[action]
        if ok:
            for j in jobs:
                self.marks[j.id] = (mark, clock.now())
        evs = [self.store.event("action", f"{verb} {j.id} {j.name}: {'ok' if ok else 'failed'} {out}".rstrip(), j, action=action, ok=ok) for j in jobs]
        if self.emit:
            self.emit(evs)
        if self.on_done:
            self.on_done()
        return ok, out

    def resubmit(self, clone) -> Tuple[bool, str, Optional[str]]:
        """sbatch the clone; an audit event either way."""
        ok, out, new_id = self.slurm.submit(clone.argv, clone.workdir)
        text = f"resubmitted {clone.id} {clone.name} as {new_id}" if ok and new_id else f"resubmit {clone.id} {clone.name}: {'ok' if ok else 'failed'} {out}".rstrip()
        ev = self.store.event("action", text + f"  [{clone.command()}]", job_id=clone.id, name=clone.name, action="resubmit", ok=ok, new_id=new_id or "")
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
        kind = ev.get("kind", "")
        if self.bell and kind in self.bell_kinds:
            try:
                self.bell()
            except Exception:
                pass
        if not self.command or kind not in self.kinds:
            return
        env = dict(os.environ, RAVEN_EVENT=kind, RAVEN_JOBID=str(ev.get("job", "")), RAVEN_JOBNAME=str(ev.get("name", "")), RAVEN_TEXT=str(ev.get("text", "")))

        def go():
            try:
                subprocess.run(self.command, shell=True, env=env, timeout=20, capture_output=True)
            except (OSError, subprocess.TimeoutExpired):
                pass

        try:
            return self.pool.submit(go)
        except RuntimeError as exc:
            self.error = str(exc)[:512]
