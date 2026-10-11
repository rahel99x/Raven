# Changelog

## 4.16.1

- Add Collapse All beside Expand All in Jobs Main, with the matching `:grouping collapse` command. Preserve membership, manual exclusions, and explicit fold ownership when Undo Auto is used.
- Keep the grouping controls fitted to narrow panes and retain the existing bounded fold storage.

## 4.16.0

- Add captured mouse column resizing with cancel/reset controls, shared Jobs and Recents geometry, and clipped headings that retain correct hit targets. Keep completion trails on the left of Main.
- Add inclusive Shift-click job ranges and allow plain drag selection from selected rows. Reserve Alt-drag for group membership and preserve native and raw-terminal mouse isolation.
- Add automatic-group, undo, clear, and expand controls. Preserve manual memberships and fold ownership. A dedicated group handle prepares reviewed, same-owner pending-job ordering through supported Slurm `scontrol top`.
- Add Bash Research reporting for actual submissions, dependencies, arrays, retries, lifecycle, metrics, artifacts, and declared native sources. Discover attempt-bound sources locally and through supported SSH readers during runtime.
- Guard stale gestures, scheduler identity changes, ambiguous submissions, concurrent report publication, and fast job startup. Add focused and sequential interaction regressions.

## 4.15.1

- Restore NVIDIA GPU measurements on mixed-graphics desktops by resolving
  numeric CUDA visibility to stable device UUIDs inside the job allocation.
- Resolve ambiguous device subsets with a bounded CUDA Driver API helper.
  It creates no CUDA context and requires no new Python package. Cache identity
  briefly per allocation and node; invalidate changed visibility or inventory.
- Suppress obsolete ambiguity warnings after a valid stable match, so successful
  collection does not trigger retry backoff. Preserve mixed-vendor, allocation,
  unknown-counter, and physical MIG-parent safeguards.
- Explain allocation identity failures in GPU diagnostics and document recovery.
  Add hybrid-desktop, driver-helper, cache, and sampler-to-chart regressions.

## 4.15.0

Implement the remaining 26 research proposals through a shared terminal
operations workbench. Open Research → Operations or enter `:ops`. Related
controls also appear in job Details, Research views, Analytics, and Sources.

- Inspect storage and quota evidence, edit pending jobs, adjust array limits,
  inspect reservations and licenses, diagnose Slurm, recover retained scripts,
  and open a reviewed allocation shell or step attachment.
- Validate software environments, measured placement, scientific acceptance,
  statistical comparisons, verified output reuse, and dependency-chain repair.
- Prepare checkpoint recovery, adaptive searches, packed tasks, optional Dask
  pools, and heterogeneous allocations. Use durable monitoring, verified data
  staging, and read-only Nextflow or Snakemake connectors.
- Inspect distributed bottlenecks, multiple clusters, historical incidents,
  and energy per useful result with explicit attribution and missing evidence.
- Keep collection off the renderer. Bind action reviews to their connection,
  job attempt, and input evidence; reject stale and repeated actions.
- Validate cross-feature sequences, worker-mode transitions, bounded reads,
  subprocess cleanup, and mouse isolation in ASCII and Unicode layouts.

See the [operations guide](docs/guides/operations.md) for controls, contracts,
optional dependencies, and site limits. The separate live stack tracer remains
a future 5.0 proposal.

## 4.14.0

Phase one adds measurement and submission checks while retaining the terminal
interface and optional-adapter architecture.

- Inspect Tower polling separately from Slurm collection capabilities.
- Select NVIDIA, AMD, or Intel GPU collection where the required vendor tool
  and allocation access are available.
- Run explicit local shell syntax checks and optional ShellCheck analysis
  without executing the batch script.
- Attach a validated scientific manifest to a Slurm array using actual task
  indices rather than submission order.
- Keep inspection-dialog mouse events isolated from hidden panes.
- Preserve fitted graph geometry during selection when retained samples expire.
- Keep the selected source visible in short panes and clear stale retry delays
  when the GPU provider changes.

See the [phase one guide](docs/guides/phase-one.md) for controls, data contracts,
and limits. The [roadmap](docs/ROADMAP.md) tracks the remaining proposals.

For earlier releases, see the [README release notes](README.md#changes-in-tower-4130).
