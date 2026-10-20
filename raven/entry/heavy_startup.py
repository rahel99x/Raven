"""Nonblocking Heavy startup lifecycle, independent of CLI and UI owners."""

from __future__ import annotations
import re
import time


class BackgroundHeavyStartup:
    """Drive existing nonblocking admission from the dashboard's event loop.

    The allocation manager owns the helper, scheduler I/O and verification
    threads. This object owns only the initial intent and its deadlines; it
    creates no second worker or polling loop. Construction never submits.
    """

    def __init__(self, manager, resolved, reuse=None):
        if reuse is not None and not re.fullmatch(r"[1-9][0-9]{0,19}", str(reuse)):
            raise ValueError("--allocation requires one exact numeric allocation ID.")
        self.manager, self.resolved, self.reuse = manager, resolved, reuse
        self.phase, self.error, self.deadline = "armed", "", None
        self.review_token = None
        self.review_plan = None

    @property
    def retryable(self):
        snap = self.manager.snapshot()
        return (
            self.phase in {"failed", "cancelled"}
            and snap.get("state") == "light"
            and not snap.get("cleanup_uncertain")
        )

    def publication(self, snapshot):
        value = dict(snapshot)
        if self.phase == "closed":
            return value
        if self.phase in {"armed", "reviewing"} and value.get("state") == "light":
            value.update(state="preparing", mode="light")
        if self.error and not value.get("cleanup_uncertain"):
            value["error"] = self.error
        value["startup"] = {
            "phase": self.phase,
            "reuse_job": self.reuse,
            "retryable": self.retryable,
        }
        return value

    def retry(self, plan=None):
        if not self.retryable:
            raise ValueError(
                "Wait for confirmed cleanup before retrying Heavy startup."
            )
        # The ordinary request rechecks this frozen plan in manager.request;
        # explicit reuse obtains a new ownership receipt before launching.
        self.review_plan = plan
        self.phase, self.error, self.deadline, self.review_token = (
            "armed",
            "",
            None,
            None,
        )
        return True

    def cancel(self, generation=None):
        snapshot = self.manager.snapshot()
        if generation is not None and generation != snapshot.get("generation"):
            return False
        self.phase, self.error = "cancelled", ""
        # A reuse read may finish after cancellation. Its token is discarded;
        # without a subsequent tick in 'reviewing' it can never start a step.
        self.review_token = None
        if snapshot.get("state") in {
            "preparing",
            "pending",
            "starting",
            "heavy",
            "draining",
            "reconciling",
        }:
            return self.manager.cancel(generation=generation)
        return True

    def close(self):
        self.phase, self.review_token = "closed", None

    def tick(self):
        if self.phase in {"closed", "cancelled", "failed"}:
            return
        try:
            if self.phase == "armed":
                if self.review_plan is not None and self.manager.plan().get(
                    "digest"
                ) != self.review_plan.get("digest"):
                    raise ValueError(
                        "Allocation settings changed; review Heavy startup again before retrying."
                    )
                if self.reuse is not None:
                    self.review_token = self.manager.request_reuse_review(
                        str(self.reuse)
                    )
                    self.phase, self.deadline = "reviewing", time.monotonic() + 60
                else:
                    self.manager.request(plan=self.review_plan)
                    self.phase = "waiting"
                    self.deadline = (
                        time.monotonic()
                        + self.resolved.data["allocation"]["pending_timeout_seconds"]
                        + 60
                    )
            elif self.phase == "reviewing":
                review = self.manager.snapshot().get("reuse_review", {})
                if review.get("token") == self.review_token:
                    if review.get("state") == "error":
                        raise ValueError(
                            review.get("error") or "Allocation reuse review failed."
                        )
                    if review.get("state") == "ready":
                        self.manager.request(reuse=review["token"], plan=review["plan"])
                        self.phase = "waiting"
                        self.deadline = (
                            time.monotonic()
                            + self.resolved.data["allocation"][
                                "pending_timeout_seconds"
                            ]
                            + 60
                        )
            snapshot = self.manager.snapshot()
            if self.phase in {"waiting", "ready"}:
                if snapshot.get("state") == "heavy" and getattr(
                    getattr(self.manager, "worker", None), "ready", False
                ):
                    self.phase = "ready"
                elif (
                    snapshot.get("cleanup_uncertain")
                    or snapshot.get("state") == "reconciling"
                ):
                    raise ValueError(
                        snapshot.get("error")
                        or "Allocation cleanup is uncertain; inspect ownership before retrying."
                    )
                elif snapshot.get("state") == "light":
                    raise ValueError(
                        snapshot.get("error")
                        or "Heavy worker stopped. Review and retry when ready."
                    )
            if (
                self.phase in {"reviewing", "waiting"}
                and time.monotonic() >= self.deadline
            ):
                self.cancel()
                raise ValueError(
                    "Heavy startup timed out; owned cleanup requested. Job inspection and logs remain available."
                )
        except (OSError, ValueError, RuntimeError) as exc:
            self.phase, self.error = "failed", str(exc)


def shutdown_heavy(manager):
    startup = getattr(manager, "background_startup", None)
    if startup is not None:
        startup.close()
    manager.shutdown()
