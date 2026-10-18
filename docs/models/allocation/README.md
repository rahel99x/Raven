# Bounded allocation ownership model

This model explores two request generations sharing one scheduler job number.
It includes a late grant after cancellation, lost transport, delayed/stale
publications, verified terminal cleanup, and job-number recycling. `unseen`
means unknown; a missing accounting row is **not** a terminal observation.

`Ownership.cfg` checks the ownership invariant. The negative control
`Ownership-unsafe.cfg` must fail: a cached job number/proof can cancel a recycled
allocation. Keep its counterexample as a regression requirement, not as an
allowed recovery shortcut.

```bash
java -cp /path/to/tla2tools.jar tlc2.TLC -workers 1 \
  -config Ownership.cfg Ownership.tla
java -cp /path/to/tla2tools.jar tlc2.TLC -workers 1 \
  -config Ownership-unsafe.cfg Ownership.tla
```

Run from this directory with TLA+ tools 1.7.4. The implementation evidence records
the tool digest, exact outputs and explored state counts. The JAR is a developer
tool and is not included in Raven's runtime or wheel.

The model is an independent reference abstraction, not extracted Python and not
a proof of all deployments. `CancelOwned` represents an ownership-bound action
at its execution boundary. It does **not** justify a separate scheduler read
followed by unrestricted `scancel JOBID`: another attempt could replace the job
between those operations. Socket/foreground ownership, exact request identity,
and any scheduler cancellation guarantee require their own implementation tests.

No distributed liveness theorem is claimed. Unknown ownership stays blocked;
the model permits progress when an authoritative terminal observation arrives.
Model fairness is deliberately not used to assume that inaccessible evidence
eventually appears.

| Model transition | Production regression boundary |
| --- | --- |
| Grant / RequestCancel / StalePublication | `AllocationManager._apply`, `cancel`, generation-bound `WorkerProxy`; `tests/test_ownership_stateful.py` |
| Lost channel / unknown outcome | real local subprocess SIGSTOP/SIGKILL and truncated frame checks in `tests/test_worker_fault_models.py` |
| Same job number, different owner/attempt | placement/reuse mismatch regressions in `tests/test_raven_worker_runtime.py` and `tests/test_raven_allocation.py` |
| Retry committed result after lost acknowledgment | real `WorkerClient`/worker state machine in `tests/test_worker_fault_models.py` |
| Verified cleanup before next generation | existing helper cleanup receipts and W17 reconciliation checks; no job-number-only cancellation |

Pointer capture is modeled separately against the real `PointerLease` and
scrollbar handlers; terminal coordinates and gesture geometry do not belong in
the allocation abstraction.
