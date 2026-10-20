# S1/S4 architecture closure

The accepted global acyclicity condition now passes. The integrated focused check counts 625 modules and 2,067 function-level imports, with zero cyclic components and zero unresolved runtime targets. Explicit lower query/state implementations and typed owner actions replace the original feedback edges. Actual module families were moved into coherent packages with exact compatibility aliases; persisted feature keys remain stable. The quality gate now requires `--require-acyclic`, rather than permitting the former debt baseline. Final delivered revision and full qualification are recorded by release integration.

## Historical closure assessment and implementation plan

The following assessment documents the intermediate 109-module component and the accepted sequence that subsequently removed it. It remains here for source traceability.

At that intermediate checkpoint, S1 was incomplete. The last reviewed static component contains 109 modules and
531 internal import edges (before the subsequently reviewed input-only
`workbench -> column_resize` registration). It includes UI, launch, tracing,
reporting and execution code. The exact current graph must be regenerated after
functional stabilization. No unavailable hardware, credential or external
service blocks this work.

The candidate table below records a 76-edge/45-source-file greedy
feedback-arc candidate. My independent ordering found 78 edges. Neither is a
minimum proof or a safe edit recipe: in particular, cutting `cli -> controller`
is the wrong architectural direction. Keep CLI/UI composition roots above their
implementations and instead remove the readers' imports back into those roots.

## Acceptance gate first

The existing gate counts imports inside functions and literal imports, but eight
computed core import expressions remain unresolved. Their finite known targets
must be included before declaring success. Prefer explicit imported callable
registries in UI, operation and worker composition roots. Resolve compatibility
aliases and generated bootstrap imports honestly; do not hide edges with getattr,
__import__, import strings, TYPE_CHECKING or same-module aggregation. Final checks
must run `scripts/check_import_layers.py --require-acyclic`, all six dependency
contracts, and tests checking finite registry coverage and unknown-key refusal.

## A. Shared state and query leaves

Extract actual implementation, retaining compatibility exports only in upper
facades and changing lower consumers to import the canonical leaves:

* `views.py` series preparation/publication/normalization and GPU row helpers ->
  `series_presentation.py`; column descriptors now owned by `Views` ->
  `ui/table_schema.py`. Removes `analysis_ui -> views`, `job_panels -> views`,
  `table_ui -> views` without a callback for ordinary reads.
* `interaction.py` immutable Control/Graph/span index, current graph/state,
  scope/hit tokens and geometric navigation -> `ui/control_graph.py` plus a small
  state-query module. Keep activation and key/mouse orchestration above it.
* `job_selection.py` selection publication/identity queries ->
  `ui/selection_state.py`. Put panel focus/geometry in `ui/panel_state.py` so a
  range lookup does not import `job_panels`. Keep clear/cancel/navigation actions
  in the feature handler; do not pretend effectful resume/clear is pure.
* `job_groups.py` Group/Child/index records, inference and projection data ->
  `job_group_domain.py`; grouping mode/sort/fold state -> `ui/list_preferences.py`.
  `job_batch_sort`, `manual_job_groups`, `job_list_controls`, `recent_history`
  and `recents_ui` then depend on those leaves. This removes real mutual
  grouping dependencies while preserving contiguous batches, identity, order
  and persisted modes; UI controls must not own domain records.

Proposed owner A owns these new leaves and their exact extracted slices first.
Consumer import edits can be mechanical once exports and function identity tests
are settled. Do not simultaneously modify the same source slices in owner B.

## B. Typed UI action composition

Use narrow capability records with explicit callable fields, not a single
`invoke(name, *args)` or imported-module lookup. Compose them once in workbench /
controller construction, with required callbacks verified at construction:

* Capture protocol receives registered owner handlers and snapshot/hover
  callbacks. `pointer_routing` no longer imports `history_browser` or
  `interaction` or resolves an owner module by name. Keep lease identity,
  tombstones, cancellation/rollback and source-attempt verification unchanged.
* Selection/interaction actions receive named clear-row/focus/open-export,
  activate-panel, cancel-tool and select-job callbacks. Ordinary state reads use
  A's leaves. This breaks `interaction <-> job_selection`,
  `interaction <-> toolbar/notification/job menus`, `chart_interaction <->
  metric_live` and `scrollbars -> metric_live/chart_interaction/pane_drag`.
  Shared live-chart state belongs in a leaf; zoom/stop/reset remain explicit
  effects. Reuse FocusStack for exactly one foreground owner.
* Panels receive renderer callbacks for analysis/research/trace/advisor/log views;
  those features receive panel activation/focus callbacks. Reuse the existing
  `App.background_worker_scheduler` service port instead of `_worker` imports
  from `job_panels` in quick_advisor/trace/browser code.
* Project/run-home/launch/setup links receive typed navigation/open-form effects.
  `project_ui -> array_manifest_ui/run_home_ui`, `run_home_ui ->
  launch_wizard_ui`, `notification_ui -> run_home_ui`, and setup/telemetry mutual
  navigation are composition concerns, not state-query dependencies.
* Terminal workflows/playback/fusion/diagnostics share runtime/session/query
  services below their UI. Opening an operation or terminal panel is an injected
  effect. No terminal application service should import `terminal_ui`.

Owner B owns these effectful source sections and composition bindings after A's
leaf contracts are agreed. Most affected source files are interaction,
job_selection, pointer_routing, chart_interaction, metric_live, scrollbars,
job_panels, log_workbench, log_tools, project_ui, run_home_ui, notification_ui,
command_ui, execution_ui, toolbar, terminal_ui/workflows/playback/fusion,
setup/telemetry UI and ui.research_binding. Each callback must keep cancellation,
current-context/attempt checks and existing return/consumption semantics.

## C. CLI, capture and execution adapters

This package is independent of A/B except browser UI effects and root binding:

* Move `cli.forecast_scope` to the existing lower connection-scope service.
  Extract option parsing shared by `cli`, `raven_launch` and `launch` into a
  parser module without UI imports. Move onboarding/demo CLI delegation above
  the onboarding service. This removes reader imports back into CLI roots.
* Split trace capture from trace CLI dispatch, and trace HTML/document
  generation from browser UI requests. `trace_native` imports capture core;
  capture core does not dispatch native/summary/export CLI commands. Export and
  stream readers share provenance in a leaf instead of `trace_stream ->
  trace_export -> trace_stream_export -> trace_stream`. Browser request uses B's
  existing scheduler/panel ports. Preserve legacy module identity where tests
  rely on monkeypatching; lower production code must use the canonical service,
  never an upper compatibility facade that re-exports CLI entrypoints.
* Extract launch locks/exclusive writes, runtime-directory lookup, diagnostics,
  stop-capture and plan inventory into actual lower modules. Runtime orchestration
  may call bootstrap/autotrace/trace execution; those modules must not import
  runtime orchestration merely for `_warn`, `_exclusive` or inventory helpers.
* Place worker operation bindings in an explicit composition registry above log,
  trace and planning implementations. Readers import admission/policy/context
  leaves, not `raven_execution`. Separate planning command records/result
  construction from research UI command dispatch.

Owner C owns CLI/launch/capture/execution files and new canonical service modules.
A/B must request imports through agreed contracts rather than editing those
files. Existing decoder/transport fragmentation tests, trusted ownership,
source budgets, worker execution admission and capture/import overhead limits
remain mandatory regression contracts.

## Sequence and feasibility

1. Freeze the functional regression fixes; record exact graph and resolve all
   finite dynamic targets. Agree leaf exports and typed effect signatures.
2. A builds state/query leaves while C removes independent entry/capture/runtime
   cycles. B starts capture registry and worker/panel service ports.
3. Once A publishes leaves, B removes feature-action backedges. A finishes grouping
   consumers; C binds explicit entry/worker registries. No shared-file write
   conflicts: source slices are transferred once, then frozen for their next owner.
4. Recompute SCCs after each package. Every residual component gets an explicit
   owner/cut, not an expanded debt allowance. Only legitimate functional owner
   registrations may amend the interim baseline; final acceptance is no SCCs.
5. Run targeted behavior suites on each package and the full suite, packaging,
   installed commands and actual Slurm checks against the final integrated tree.

Three agents can complete this. The work is materially larger than containment:
roughly 45-65 existing files, 10-16 real leaves/ports, three explicit composition
boundaries, and two integration/qualification passes. A normal engineering
estimate is 5-9 engineer-days including regression work, about 2-4 elapsed
working days with three coordinated owners. These are estimates, not a reason
to waive the requirement or a claim that tests validate a future extraction.
S4 should proceed with these actual coherent families, preserving exact legacy
aliases where needed; arbitrary directory moves alone do not close S1 or S4.

## Recorded feedback-edge candidate

This is a graph heuristic from the reviewed pre-registration snapshot, not an
implementation instruction or a mathematical minimum. The typed seams above
correct its architectural direction; regenerate the exact graph after each
completed extraction.

| Importing module | Imported module |
| --- | --- |
| `raven.analysis_ui` | `raven.views` |
| `raven.array_disclosure` | `raven.job_selection` |
| `raven.chart_interaction` | `raven.data_export_ui` |
| `raven.cli` | `raven.controller` |
| `raven.cli` | `raven.views` |
| `raven.command_ui` | `raven.table_ui` |
| `raven.execution_ui` | `raven.research_commands` |
| `raven.execution_ui` | `raven.shell_checks_ui` |
| `raven.gpu_diagnostics` | `raven.cli` |
| `raven.interaction` | `raven.history_log_export` |
| `raven.interaction` | `raven.job_group_menu` |
| `raven.interaction` | `raven.job_list_menu` |
| `raven.interaction` | `raven.job_panels` |
| `raven.interaction` | `raven.job_selection` |
| `raven.interaction` | `raven.notification_ui` |
| `raven.interaction` | `raven.ops_ui` |
| `raven.interaction` | `raven.scrollbars` |
| `raven.interaction` | `raven.table_ui` |
| `raven.interaction` | `raven.toolbar` |
| `raven.job_batch_sort` | `raven.job_groups` |
| `raven.job_list_controls` | `raven.grouping_toolbar` |
| `raven.job_list_controls` | `raven.job_groups` |
| `raven.job_list_menu` | `raven.grouping_toolbar` |
| `raven.job_panels` | `raven.analysis_ui` |
| `raven.job_panels` | `raven.research_views` |
| `raven.job_panels` | `raven.trace_ui` |
| `raven.job_panels` | `raven.views` |
| `raven.job_selection` | `raven.analysis_ui` |
| `raven.job_selection` | `raven.history_browser` |
| `raven.launch_autotrace` | `raven.launch_trace` |
| `raven.launch_autotrace` | `raven.trace_recorder` |
| `raven.launch_runtime` | `raven.launch_bootstrap` |
| `raven.launch_runtime` | `raven.launch_trace` |
| `raven.launch_runtime` | `raven.research_reporting` |
| `raven.launch_setup_ui` | `raven.data_export_ui` |
| `raven.log_tools` | `raven.job_selection` |
| `raven.log_workbench` | `raven.job_selection` |
| `raven.log_workbench` | `raven.log_tools` |
| `raven.manual_group_ui` | `raven.history_browser` |
| `raven.manual_job_groups` | `raven.job_groups` |
| `raven.metric_live` | `raven.chart_interaction` |
| `raven.notification_ui` | `raven.run_home_ui` |
| `raven.ops_ui` | `raven.terminal_ui` |
| `raven.planning_commands` | `raven.research_commands` |
| `raven.pointer_routing` | `raven.history_browser` |
| `raven.project_ui` | `raven.array_manifest_ui` |
| `raven.project_ui` | `raven.log_tools` |
| `raven.project_ui` | `raven.run_home_ui` |
| `raven.quick_advisor` | `raven.job_panels` |
| `raven.raven_execution` | `raven.log_bundle` |
| `raven.raven_execution` | `raven.log_scan` |
| `raven.raven_launch` | `raven.cli` |
| `raven.raven_launch` | `raven.launch` |
| `raven.raven_launch` | `raven.onboarding` |
| `raven.recent_history` | `raven.scrollbars` |
| `raven.recent_history` | `raven.table_tools` |
| `raven.recent_history` | `raven.table_ui` |
| `raven.recents_ui` | `raven.grouping_toolbar` |
| `raven.recents_ui` | `raven.job_list_controls` |
| `raven.run_home_ui` | `raven.launch_wizard_ui` |
| `raven.table_tools` | `raven.table_ui` |
| `raven.table_ui` | `raven.views` |
| `raven.telemetry_settings_ui` | `raven.setup_ui` |
| `raven.terminal_playback` | `raven.terminal_fusion` |
| `raven.terminal_ui` | `raven.terminal_fusion` |
| `raven.terminal_workflows` | `raven.ops_ui` |
| `raven.terminal_workflows` | `raven.terminal_ui` |
| `raven.toolbar` | `raven.launch_wizard_ui` |
| `raven.trace_browser` | `raven.job_panels` |
| `raven.trace_browser` | `raven.trace_ui` |
| `raven.trace_export` | `raven.trace_stream_export` |
| `raven.trace_native` | `raven.trace_recorder` |
| `raven.trace_snapshot` | `raven.trace_recorder` |
| `raven.ui.research_binding` | `raven.job_selection` |
| `raven.workspace_layout` | `raven.job_panels` |
| `raven.workspace_layout` | `raven.terminal_ui` |
