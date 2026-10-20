# Architecture acceptance

The review's global acyclicity target is implemented. The strict check includes
function bodies, annotation guards, relative compatibility imports, and literal
runtime imports. The final graph contains 625 modules, zero cyclic components,
and zero unresolved runtime targets. CI requires `--require-acyclic`; rewriting
a debt baseline cannot waive that condition.

The [coverage ledger](coverage.json) binds the accepted requirements to their
source paragraphs, implementations, behavioral checks, and final qualification.
Its [graph evidence](packages/architecture-graph.json) records the actual result.

## Boundaries

| Responsibility | Implementation |
| --- | --- |
| Application composition | CLI, controller, and workbench bind concrete capabilities once per session |
| Input and navigation | One foreground owner, stable feature keys, and typed capture, selection, panel, navigation, and export actions |
| State and queries | Canonical geometry, selection, grouping, panel, workspace, and publication records |
| Application services | Research, execution, launch, recording, terminal-session, and trace-document services |
| Connection adapters | Slurm, collection governance, local and remote files, and concrete operation adapters |
| Transport | Shared framing and bounded worker/allocation protocols |

Seven dependency contracts and the all-depth graph gate enforce these boundaries.
Canonical worker, recording, capture, and terminal-session services cannot import
interactive owners. An injected callback names its capability explicitly;
modules are not selected by an arbitrary runtime name.

## Canonical packages and compatibility

Actual implementations live in `collection`, `configuration`, `entry`,
`execution`, `heavy`, `installation`, `job_logs`, `jobs`, `launching`,
`operation_adapters`, `recordings`, `terminal`, `tracing`, and `ui` packages.
Released module paths remain compatibility aliases or explicit public
aggregates. Lower production consumers import the canonical implementation.

Feature persistence uses stable public keys such as `terminal_ui` and
`job_selection`, independently of Python module paths. Package moves therefore
do not rename saved preferences. Legacy workflow forwarding retains normal
admission, expiry checks, and both outer and nested durable action receipts.

Package-sensitive boundaries have specific regression checks: worker build
digests include canonical implementations; private job runtimes use relative
imports; remote readers load nested packages; CLI shims retain module and direct
script behavior; installed artifacts are checked outside the checkout.

## Import and performance decisions

Eighty-one unambiguous query and geometry imports were hoisted from frequent
input and rendering paths. Deferred imports remain where they isolate optional
features, admitted worker operations, or private job runtimes. Eagerly importing
all scientific and UI implementations into a traced job would undermine startup
and isolation requirements. Their dependencies still participate in the strict
graph; deferred loading does not hide cycles.

[UI dispatch measurements](measurements/ui-dispatch.json) compare both control
flows against identical current feature callbacks. The
[entry/capture/execution record](entry-capture-execution-results.json) records
startup, tracing, and worker measurements, including small regressions and
source hashes. These are local observations, not production-site guarantees.

Full regression, packaging, installed-command, and hosted CI evidence belongs to
the final integrated revision. Physical GPU, multi-node transport, production
SSH/shared filesystems, and scientific efficacy require separate qualification.
