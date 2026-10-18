# Observe an owned process

The experimental **Observe an owned local process** tool reads Linux process
and thread counters from an explicitly selected PID on Raven Heavy's current
node. It does not attach a debugger, signal a workload, change scheduling, or
submit another job. Capture is opt-in and requires a review before it starts.

Open the Operations workbench and choose **Observe an owned local process**, or
enter `:ops process-profile`. Enter the PID, capture duration, requested interval
and a new receipt directory. Review the node, UID and process start ticks before
applying. The default request is five seconds at 100 ms, subject to the adaptive
collector CPU target. Light defers the operation until a verified Heavy worker
is available. A remote interface cannot silently substitute its own local PID.

The process must be owned by the authenticated worker user and exist on that
same node. Raven pins `/proc` directory handles and rechecks PID/start time,
UID and every ancestor edge before retaining counters. Exits, reparenting,
permission failures, unavailable counters and retained-identity limits are
reported. A reused numeric PID never continues the reviewed capture.

Heavy's allocation identifies the **collector**, not the scientific job being
observed. Another owned process on the node may be selected, but scientific-job
and rank attribution remain explicitly unknown. Multiple nodes require a
separately verified collector and local target on each node; one receipt does
not claim coverage of an entire allocation. This initial collector does not
infer MPI rank from filenames, process order or environment guesses.

The private `process-profile.json` receipt includes:

- Per PID/TID/start-time observation counts and observed state proportions.
- Available CPU ticks, scheduler runtime/run-delay, context-switch, I/O and
  migration counter deltas, with unavailable values listed separately.
- Actual capture intervals and collector CPU time, exact node coverage,
  retained-task limits, issues and the reviewed target identity.

CPU ticks convert to seconds using `cpu_ticks_per_second`. Scheduler runtime and
run-delay are nanoseconds when kernel scheduler statistics are enabled; disabled
or unreadable counters are unknown. I/O byte counters and character counters
have distinct names. Deltas span each task's first and last successful reads;
new/exited tasks and counter resets are not filled in as zeros.

A point-sampled proportion is not exact time spent in a state. Linux `R` includes
both running and runnable tasks. `D` describes uninterruptible sleep and cannot
identify a filesystem or cause. Runtime deltas provide observed counter evidence;
they do not reconstruct exact scheduling history.

Capture retains at most 32 processes, 128 task identities and 600 samples. If the
kernel does not expose task `children` files, ancestry discovery inspects at
most 1024 same-user process metadata candidates per pass. It retains counters
only after verifying membership in the reviewed tree. Sampling adapts up to a
two-second interval; a saturated overhead budget stops the capture explicitly.
The CPU target is a feedback target, not a universal hard overhead guarantee.

Cancel through the existing Operations action. Workload processes remain
untouched. A completed or partial receipt is immutable; a killed worker may
leave no completed receipt. Inspect the destination before retrying, then
prepare a fresh review of the current PID/start identity. No automatic retry
reuses a stale capture plan. Capturing again requires a new destination.
