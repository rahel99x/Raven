# Consolidation in 6.1

This guide describes the shared mechanisms introduced in Raven 6.1 and retained
in Raven. Views reuse prepared observations and analyses while keeping their
pane state local. Source collection, scientific analysis, and reviewed actions
retain separate identities and lifecycles.

The [capability map](../reference/capabilities.md) assigns each of the 56 original
proposal entries to one primary area. Related entries identify overlap;
they do not count the same operation as another implementation. The
[implementation ledger](../FLAGSHIP_IMPLEMENTATION.md) retains the scope limits
and qualification requirements of the original proposals.

<a id="eight-owning-areas"></a>

## Architecture ownership

| Area | Shared responsibility |
| --- | --- |
| Evidence and results | Source identity, provenance, lineage, portable records, and derived results |
| Collection and integrations | Collector admission, tracing adapters, subscriptions, source limits, and capability discovery |
| Investigation | Timelines, specialized analyzers, missing evidence, planning, and recommendations |
| Experiments | Trial admission, preparation, measurement, repeated checks, minimization, and regression |
| Controlled actions | Scope review, source revalidation, cooperative policy, dispatch, and receipts |
| Terminal workspace | Commands, navigation, pane documents, gestures, charts, and accessibility |
| Execution and state | Worker budgets, publication revisions, retained state, and reconciliation |
| Delivery and qualification | Documentation, distribution, starter kits, regression evidence, and external qualification |

These areas describe ownership. Specialized scientific algorithms still have
their own validation rules. A shared experiment lifecycle does not make a
numerical check equivalent to a regression test or failure oracle.

## Existing controls and compatibility

Continue to use Jobs, History, Analytics, Research, and the current Details tabs.
The Run, Investigate, Compare, Results, and Connect workspaces remain available.
Existing commands, operation keys, and saved UI settings retain their meanings.
Standalone scientific operation forms remain available alongside the staged
investigation workflow.

Command descriptions, argument suggestions, task controls, toolbar action
metadata, and generated command help now use the shared command registry.
Specialized command handlers continue to validate their own inputs. Installed
operation specifications supply their form fields and completion choices;
trusted plugin commands remain a separate extension contract.

Job selection, group controls, resizable columns and panes, log selection,
scrollbars, theme controls, and graph Live/sampling controls remain available.
Details retains its existing compact cards and companion area plots. The shared
pane contract preserves the different layouts used by a full page and a narrow
Details column.

For the complete control reference, see [controls](../CONTROLS.md) and
[operation fields](../reference/operations.md).

## Publication and presentation reuse

Managed metric observations are captured into Store-owned records. A source
revision identifies the immutable publication used by renderers. Appends and
supported nested corrections invalidate that publication; unchanged observations
reuse it. A corrected older sample therefore does not remain hidden behind an
unchanged job ID or list length.

Prepared metric transformations include the Store scope, exact job attempt, and
their transformation settings. Native Job Series, comparison and advisor
consumers, and resource-column analyses use this shared preparation path where
applicable. The same observations can support several independently configured
views without copying the entire application object.

Direct extension writes that do not use the owned-record protocol keep the
existing content-checking path. Unsupported values and inputs beyond cache
limits use the uncached path. This preserves extension behavior without treating
an untracked mutable object as an immutable publication.

Retention remains bounded:

- Source publications and prepared metrics have entry, observation, and byte
  limits. Eviction releases retained publication references.
- Chart rasters and prepared time points have entry, point, and cell limits.
  Changing geometry, rendering settings, or source data selects the appropriate
  raster rather than reusing a mismatched image.
- The Jobs row cache retains one checked native preparation. It compares current
  fields, including mutable job data, and bypasses unsupported custom callbacks.
  Native table consumers copy the visible rows for presentation.
- Details documents reserve offscreen chart rows and rasterize visible bands.
  Their source rows and retained width maps remain bounded. A first publication,
  newly available data, or a scrolled resize can request a bounded full
  composition to establish a valid map.

These are targeted reuse mechanisms. They do not establish that every Store
field or every document in Raven is incrementally composed.

## Pane and pointer ownership

An embedded pane uses an explicit context containing its selection, analysis
preferences, and presentation overrides. Controller methods operate on that
context. Published services and source caches remain owned by the application.
Rendering a Details view does not copy the entire App or change the standalone
workspace's selection.

`PaneDocument` carries rows, control targets, chart geometry, and scrollbars
through one transformation. Removing a navigation prefix or clipping a viewport
therefore transforms the input targets with the rendered content. The shared
contract is used by Research and Analytics Details and native Job Series.

Details clears retained document state when the job's execution changes,
including a new start or queue generation for the same job ID. Painted buttons
from an older queue generation cannot activate a new execution before its next
frame. Pane boundaries continue to block clicks on the underlying job table.

The pointer router gives each active gesture one owner across native terminal
input and direct UI dispatch. That owner handles motion and release through its
existing control state machine. The shared lifecycle cancels stale captures and
clears obsolete release markers; control-specific preview and rollback rules
remain intact.

Chart display state is separate from collection identity. A box zoom in Jobs
Details, History Details, or a full-screen chart does not replace the standalone
Analytics zoom for the same source. Zoom reset and Live activation clear the
relevant display scope. Source sampling still uses the exact shared metric and
attempt identity, so separate zooms do not create separate acquisition keys.

See [pointer navigation](pointer-navigation.md) and
[pane navigation](pane-navigation.md) for the user controls.

## One acquisition path for governed reads

Governed scheduler reads and explicitly cached site telemetry use
`ReadAcquisition` over the collection broker and shared query cache. Equivalent
concurrent read requests can share an in-flight observation. Identity includes
connection scope, query, policy, capabilities, and the request's freshness contract.

The first waiting caller executes an admitted loader on its existing worker.
The acquisition layer creates no additional executor or polling thread. The
host-local shared cache remains responsible for cached values, source floors,
and cross-process invalidation. This avoids competing cache authorities when
the configured worker budget changes.

Each consumer has its own lease and cancellation signal. One cancelled consumer
does not cancel a read that another consumer still needs. The loader must honor
its cooperative cancellation signal and deadline; arbitrary Python callbacks
cannot be forcibly stopped by this interface.

Action validation can request a fresh read. Such a request does not join an
observation started before the request. It still respects source floors and waits
for older owners to drain. Changed policy or connection scope cannot reuse a
result authorized under a different identity. Only explicitly read-only requests
enter this path; scheduler mutations bypass it and invalidate observations.

The mechanism covers connected reads that use the governed acquisition path.
It does not provide an institution-wide monitoring service or enforce Raven's
worker budget on every optional external tool.
See [background workers](background-workers.md) for worker-mode controls.

## A connected investigation

Open **Investigate → Staged job investigation**, or enter
`:ops investigation-workflow`. The report combines these stages:

1. Read declared evidence for an exact attempt.
2. Run the supported specialized analyses and show their findings.
3. Identify missing evidence and evaluate the declared planning budget.
4. Suggest admitted diagnostics linked to explicit recipes.
5. Prepare and confirm a diagnostic review.
6. Read its retained outcome and applicable measured findings on **status**.

Inspection does not execute a diagnostic. Compiled collection proposals remain
inspection-only unless a separately declared diagnostic recipe is reviewed.
Unknown, incomplete, or missing evidence cannot satisfy planner prerequisites.
Numerical trial outputs can feed the existing numerical analyzer when the
workflow declares the corresponding metric policies. A completed process alone
does not establish a scientific conclusion.

Pure analysis results share a bounded cache keyed by connection, source identity,
complete input revision, algorithm version, and settings. Cached results are
immutable JSON bytes; callers receive isolated decoded values. Equivalent
in-flight analyses can join. Recommendation expiry is evaluated on each
inspection. Explicit inspection still reads and verifies sources before using
derived results or preparing an action.

The [scientific investigation guide](scientific-investigation.md#a-staged-investigation)
contains the workflow schema, supported sources, exact-attempt anchors, review
steps, and offline example.

## Shared trial and action lifecycles

Campaigns, reproducer minimization, and Git regression sweeps use one trial
lifecycle for budget admission, source verification, sequence assignment,
preparation, bounded child execution, measurement, and durable publication.
Algorithms supply their preparation and measurement callbacks. They retain their
own failure-oracle and scientific interpretation rules.

A trial checks its remaining budget after staging and writing its intent, before
starting a child. Cancellation, time exhaustion, or failed publication cannot
silently start an unbudgeted retry. Interrupted work requires receipt inspection
and an explicitly reviewed new workspace. The standalone driver remains usable
inside a reviewed Slurm workspace without installing Raven there.

Reviewed operations share `action_lifecycle.execute`. It captures the reviewed
plan, validates its exact scope, records an intent when state storage is enabled,
and revalidates at dispatch. Adapter-specific source and scheduler checks still
run. Reusing an already attempted durable plan is rejected. Results and failures
retain receipts or an unresolved intent for reconciliation.

A receipt marked `finished` means the adapter returned a valid report. The report
still determines whether work is applied, pending, partial, or uncertain. These
receipts do not create a distributed transaction or guarantee rollback of
scheduler, filesystem, or application effects. If state storage is disabled, the
report states that its outcome was not saved.

## Scope and qualification

Ownership consolidation does not remove the original scientific, runtime, site,
or adoption gates. Prepared tracing remains limited to its supported
instrumentation. External user studies and research evaluations remain external
work. Actual GPU, multi-node, SSH, and terminal qualification must be recorded
for the target installation.

The [capability reference](../reference/capabilities.md) exposes this distinction
through immutable descriptors and lookup functions. It does not add another menu
hierarchy. Maintainers can run:

```bash
python3 scripts/generate_capability_reference.py --check
python3 scripts/generate_operation_reference.py --check
```

The first check requires exactly one primary owner for every original proposal
entry and validates aliases, overlap references, and installed operation keys.
Use [qualification](../QUALIFICATION.md) for target-system evidence and the
[release validation record](consolidation-validation.md) for measured results.
