# Raven project reporting schemas

These JSON Schemas describe the portable project convention in
[PROJECT_STANDARD.md](../PROJECT_STANDARD.md). They use JSON Schema Draft 2020-12 and
relative references so projects can copy this directory without changing URLs.
Raven's core runtime uses the standard library; it does not load these schema
files or require a JSON Schema package. Optional terminal dependencies are
separate from schema validation. Native readers remain authoritative for
runtime identity, semantics, and bounded I/O.

| File | Applies to | Current Raven reader |
| --- | --- | --- |
| [metrics.v1.schema.json](metrics.v1.schema.json) | Each object in `runs/<run_id>/metrics.jsonl` | Native incremental `MetricReader`; Experiment view |
| [run.v1.schema.json](run.v1.schema.json) | `runs/<run_id>/run.json` | Bounded exact-job runtime discovery or explicit project/run selection |
| [job-index.v1.schema.json](job-index.v1.schema.json) | `.raven/jobs/<job_id>.json` and cluster-prefixed equivalents | Exact job-to-attempt lookup without scanning sibling runs; ambiguous cluster aliases are refused |
| [logs.v1.schema.json](logs.v1.schema.json) | `runs/<run_id>/logs.json` | Native grouped Logs catalog, bound by `logs.manifest_file` |
| [summary.v1.schema.json](summary.v1.schema.json) | `runs/<run_id>/summary.json` | Flat fields consumed after aggregation into a planning bundle |
| [output-contract.v1.schema.json](output-contract.v1.schema.json) | Project `.raven/contracts/outputs.v1.json`, or the run-relative `paths.contract` | Native artifact contract loader and bounded validation; an explicit run contract takes precedence |
| [planning.v1.schema.json](planning.v1.schema.json) | Project or per-run `reports/planning.json`, plus per-view sources | Native planning-file views, exact run bindings, and offline analysis commands |
| [submission-plan.v1.schema.json](submission-plan.v1.schema.json) | `runs/<run_id>/reports/submit.json` | Intact native preflight shown as read-only Submit evidence |
| [array-manifest.schema.json](array-manifest.schema.json) | Run-relative `paths.array_manifest`, or an explicit map path | Automatic exact-run attachment or manual `arraymap`; cluster, parent, and task-index matching |

Release 4.15 adds explicit operation inputs. These documents do not change
automatic run discovery and are not new `run.json` path keys. Select them in
the relevant operation form. See [operation contracts](../PROJECT_STANDARD.md#research-operation-contracts).

| Contract | Operation | Reference |
| --- | --- | --- |
| [science-environment.schema.json](science-environment.schema.json) | `environment` | Pinned Python/runtime and lockfile/image identity |
| [science-acceptance.schema.json](science-acceptance.schema.json), [science-results.schema.json](science-results.schema.json) | `acceptance` | Required scientific tolerances and actual measurements |
| [science-statistics.schema.json](science-statistics.schema.json) | `statistics` | Independent units or exact paired observations |
| [science-identity.schema.json](science-identity.schema.json), [science-reuse.schema.json](science-reuse.schema.json) | `reuse` | Requested identity, declared dependencies, acceptance, and outputs |
| [science-dependency-repair.schema.json](science-dependency-repair.schema.json) | `dependency-repair` | Exact pending dependency changes |
| [staging.schema.json](staging.schema.json) | `staging` | Explicit source/destination, size, SHA-256, direction, and keep-source retention |
| [workflow-engine.schema.json](workflow-engine.schema.json) | `workflow-engine` | Snakemake runtime snapshot; native DAG and Nextflow TSV are separate supported formats |
| [profiler.schema.json](profiler.schema.json) | `bottlenecks` | Exact rank/phase measurements and attempt identity |
| [cluster-workspace.schema.json](cluster-workspace.schema.json) | `clusters` | Explicit endpoints or profile references |
| [incidents.schema.json](incidents.schema.json) | `incidents` | Node/time event evidence |
| [energy.schema.json](energy.schema.json) | `energy` | Attempt energy, attribution, measurement coverage, and accepted work |

Campaign recipe contracts and complete examples are in the
[campaign guide](../guides/operations-campaigns.md). Native readers enforce
cross-field semantics, uniqueness, graph cycles, stable file identity, and
content hashes in addition to schema shape. A valid schema alone is not an
action authorization or scientific validation result.

`planning.v1.schema.json` also includes native workflow and scaling recipe
definitions as `$defs.workflowRecipe` and `$defs.scalingRecipe`. Standalone
recipes retain their native `{"version": 1, "kind": "raven.workflow"}` or
`{"version": 1, "kind": "raven.scaling"}` header. The planning bundle uses
`{"version": 1, "kind": "raven.planning"}`. A run manifest and summary use the
distinct `schema` markers `raven.run/v1` and `raven.summary/v1`; metric records
carry no version or schema marker.
The separate log index uses `raven.logs/v1`; inventory `paths.log_index` describes
its location, while native `logs.manifest_file` binds it to the selected workdir.
The separate array map uses `raven.array-manifest/v1`. Declare it with
`paths.array_manifest` in `run.json` to attach it automatically. An undeclared
map still needs explicit `arraymap` attachment. Automatic maps follow validated
run declarations; manual maps retain their reviewed revision until reload.
Declared input/output paths are descriptive and are not opened.
See [scientific array identities](../PROJECT_STANDARD.md#scientific-array-identities)
for limits and reload rules.

`run.json` and scientific `results` remain project-owned metadata. Raven reads
bounded direct inventories beneath a known project root or standard selected
job WorkDir. Only an unambiguous exact `job_id` match binds automatically;
explicit selection remains available for local runs and repeated scheduler IDs.
Optional `paths.planning` and `paths.predict` / `forecast` / `blockers` /
`tradeoffs` / `scaling` / `workflow` bind confined run-relative native sources.
The view-specific declaration takes precedence over the shared source.
An absent per-run planning report can use the exact `paths.planning` declaration
in `.raven/project.json` (`raven.project/v1`). Both local and SSH readers pin the
run identity and confined file descriptors before reading this shared report.
SSH needs Python 3.10 or later on the selected host, with no Raven installation there.
Every discovery/report read runs in the background; rendering uses cached data.

A project reporter creates aggregates from explicitly selected summaries;
Raven does not choose a cohort or generate measurements. `paths.submit` binds
an intact captured native preflight for display. Its `plan_id` is checked, and
the attachment cannot authorize submission. Raven passports retain their native
schema and must be created through Raven's provenance commands. See the
[per-view reporting table](../PROJECT_STANDARD.md#files-for-every-research-view)
for all twelve views and exact producer locations.

## Meaning of the shared fields

Use stable workload `name`, scientific `parameters`, `script_sha256`, and known
input size to distinguish comparable work. `id` identifies one run attempt;
`job_id` identifies the scheduler job or scheduling episode. A retry gets a new
run directory and `id`. Duplicating one observation does not create another
independent repeat.

`cpus`, `nodes`, and `gpus` describe recorded allocation totals. `mem_bytes` is
the requested allocation-wide memory total and `time_seconds` is the requested
walltime limit. `runtime_seconds`, `cpu_seconds`, and `memory_bytes` are measured
quantities. An observed memory value requires `memory_scope`:

| Scope | Meaning |
| --- | --- |
| `job_peak` | Measured peak for the whole job |
| `per_node_peak` | Peak for the relevant node-level measurement |
| `max_task_rss` | Maximum individual task RSS, including Slurm MaxRSS |

These scopes are not interchangeable. Document the measurement source and
scope in `metadata`; do the same for CPU seconds. Python `process_time()`
excludes child processes and is not an allocation-wide measurement unless the
workload's scope actually matches it. Unknown data is omitted or `null`, never
an invented zero. Explicit `gpus: 0` means known zero GPUs. An empty `gpu_type`
means known generic GPUs; an omitted/null type means unknown.

Native prediction needs concrete `name`, `partition`, `cpus`, `nodes`, and
`gpus`. Known optional identities further constrain the cohort. Missing fields
or incompatible memory scopes cause abstention. Scaling additionally needs
explicit `workers`, `problem_size`, `parameters`, `script_sha256`, terminal
state, and a unique `job_id` or `repeat`; it does not infer CPU counts from
workers. Failed, timed-out, or OOM runs remain in reports as incomplete/censored
observations rather than being relabeled as successful measurements.

`UNKNOWN` and `INTERRUPTED` are descriptive application states when a verified
scheduler terminal cause is unavailable. A signal alone does not establish a
timeout or OOM. Preserve that uncertainty and reconcile with scheduler evidence
later; the analysis readers exclude unsupported states from completed fits.

## Structural and native validation

### Runtime research contracts

The [runtime research guide](../guides/runtime-research.md) and
[runtime examples](../../examples/runtime) use these additional contracts:

| Schema | Document |
| --- | --- |
| [runtime-control.schema.json](runtime-control.schema.json) | Exact attempts, safe-point commands, parameter types and I/O limits |
| [runtime-request.schema.json](runtime-request.schema.json) | A reviewed command, parameter epoch and expiration |
| [io-request.schema.json](io-request.schema.json) | Either mailbox inspection or a complete bounded permit request |
| [execution-model.schema.json](execution-model.schema.json) | Fixed-workload resource DAG, calibrated domain and observations |
| [diagnostic-intent.schema.json](diagnostic-intent.schema.json) | Required observations, explicit approximations and resource budget |
| [site-capabilities.schema.json](site-capabilities.schema.json) | Declared runtime fingerprints, adapters, conflicts and costs |
| [query-source.schema.json](query-source.schema.json) | Source rows, raw references and coverage |
| [summary-query.schema.json](summary-query.schema.json) | Selected fields and count/sum/min/max operations |
| [scientific-field.schema.json](scientific-field.schema.json) | Bounded row-major 1D/2D/3D fields with units and gaps |

[Shared runtime definitions](runtime-common.schema.json) keep identifier,
parameter, cost and scenario contracts consistent. Native readers also check
resource references and cycles, unique record identities, matching runtime
fingerprints, ordered numeric limits, selected numeric query columns, field
shape products and mailbox/application state. These relational requirements
cannot all be expressed by a structural JSON Schema. Unknown unselected query
columns are retained without reinterpretation.
Native integer fields also require integer JSON tokens; a schema validator can
accept an integral floating-point value such as `1.0` that the native reader
rejects for such a field.

Direct runtime APIs have bounded collection sizes. Operation input files also
have a 4 MiB byte limit and the shared JSON depth/value budgets; a theoretically
maximum-size array can exceed the file reader's combined budget. Schema
acceptance does not authorize an action or establish model/scientific validity.

The [invalid fixture corpus](../../tests/fixtures/runtime-contracts/invalid)
contains malformed runtime documents. Conformance tests require both schema
and native rejection. Additional tests demonstrate structurally valid documents
that fail native relational checks. Audit published valid examples with
`python3 scripts/validate_operation_schemas.py` in a development environment.

### Apply both validation layers

A schema validator checks types, keys, and local ranges. The native readers
also check semantics and bounded I/O. Both apply to portable projects:

- JSON is UTF-8, with no duplicate object keys or nonfinite numbers. Numeric
  unknowns use `null` or omission; a string such as `"NaN"` is not a numeric
  measurement. Native printable-text checks are stricter than the schemas'
  control-character patterns for some Unicode characters.
- A metric line ends with a newline and stays within 65,536 bytes. Each record
  contains at most 64 named metrics; the stream also retains at most 64 distinct
  series. Native progress validation enforces `completed <= total` and
  `total > 0`. The reader keeps a bounded recent sample for responsive plotting.
- A planning document stays within the default 1 MiB command/UI read budget,
  32 nesting levels, and 100,000 JSON values. Individual analysis input lists
  stay within 10,000 records; candidate lists stay within 64. Split larger
  histories into explicitly chosen cohort/report files.
- Shared prediction parameters stay within four nesting levels and 128 total
  JSON values, with at most 64 members per object/list. Schemas enforce depth
  and local limits; native validation additionally checks the total-value
  budget and byte limits.
- Output contracts stay within 256 KiB, use exact relative paths, and contain
  no symlinks or glob expansion. Native validation checks unique paths,
  ordered min/max sizes or row counts, and conflicting exact row counts.
  Passing the contract schema does not mean the outputs exist or passed checks.
- A log index stays within 256 KiB and 256 entries. Native validation checks
  unique entry IDs and actual selected `job_id` when recorded. Relative paths
  resolve from the index directory, including explicitly named sibling files;
  exact absolute paths may point to other locations. Neither expands globs.
  Catalog construction deduplicates normalized paths and shows groups/labels;
  schema validation alone does not establish file availability. These read-only
  log paths have a different policy from confined artifact-contract outputs.
- Workflow validation checks unique IDs, known dependencies, no cycles, at
  most 4,096 edges, and `lower <= estimate <= upper`. Resource/topology and
  walltime checks still apply when scripts are prepared.
- Scaling validation checks unique configurations, experiment size limits,
  concrete independent repeat identity, matching scientific work, and
  controlled strong/weak scaling. A valid recipe prepares reviewable plans;
  it does not execute the workload or establish that it consumed the controls.
- Forecast observations must contain actual pre-start scheduler predictions
  and later observed starts. Calibration checks chronology, resource cohorts,
  distinct submitted jobs, and forecast lead. A planning `now` value freezes
  analysis for historical reports; omit it for a current queue forecast.

Local native file readers reject symlinks/devices and detect files that change
while being read. Producers should append complete metric records and replace
manifests, log indexes, summaries, and aggregates atomically. Keep logs, credentials,
datasets, personal shell configuration, and generated run outputs out of the
public project template.

For optional development validation, use a Draft 2020-12 validator and resolve
the sibling schema references from this directory. Native Raven readers remain
the final authority for runtime compatibility; the runnable project template
demonstrates those checks without adding a dependency to Raven.
