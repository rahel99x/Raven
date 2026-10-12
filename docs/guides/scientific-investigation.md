# Scientific investigation

[Operations](operations.md) · [Project reporting standard](../PROJECT_STANDARD.md)

Use these operations to connect declared evidence, scientific checks, and
diagnostic experiments. Provide exact attempt identities, readable source
manifests, and project-defined acceptance criteria. Execution recipes also need
the declared executable, dependencies, and a permitted local or Slurm runtime.

Open **File → Research and cluster operations** and select the **Investigation**
group. Each `:ops NAME` command opens the corresponding form. Inspection runs
outside rendering. Diagnostic execution requires a separate action review.

## Controls and contracts

| Operation | Manifest schema | Result |
| --- | --- | --- |
| `:ops investigation-workflow` | `raven.investigation-workflow/v1` | One staged report from evidence and existing analyzers through reviewed diagnostics to measured findings |
| `:ops recommendation` | `raven.recommendation-review/v1` | Applicable hypothesis, supported/refuted experiment, stale, expired or inconclusive evidence |
| `:ops investigation-plan` | `raven.investigation-plan/v1` | Ordered information actions within declared budgets |
| `:ops scientific-impact` | `raven.scientific-lineage/v1` | Affected claims, missing lineage and proposed revalidation actions |
| `:ops numerical-stability` | `raven.numerical-stability/v1` | Measured ranges, variation and tolerance violations by configuration |
| `:ops regression-investigation` | `raven.performance-investigation/v1` | Ordered version comparisons with noise and scientific validity checks |
| `:ops regression-sweep` | `raven.diagnostic-campaign/v1`, kind `regression` | Real repeated measurements of reviewed immutable Git snapshots |
| `:ops diagnostic-campaign` | `raven.diagnostic-campaign/v1`, kind `campaign` | Reviewed configuration trials and persistent measurement files |
| `:ops minimize-reproducer` | `raven.diagnostic-campaign/v1`, kind `minimize` | A reduced input that retains the declared failure signature |

The five analysis operations are read-only. Enter **Investigation manifest** and select
**Inspect / prepare**. The three execution operations also require a new
**Diagnostic workspace (new for launch)** directory. Select **Action: launch** and inspect the exact
resource and source review. Select **Review action**, check the plan, then
select **Confirm apply**. Inspection does not submit or run a program.
For an existing workspace, select **Action: status**. A new recipe path is not
required for status. Select **Action: cancel** to review cancellation of a local
driver. Cancel a Slurm allocation through its reviewed Jobs action.

The [schemas](../schemas/) are named `investigation-*.schema.json`. Runtime
validation also checks source identity, unique IDs, finite values, graph cycles,
budget relationships, and placeholder bindings that JSON Schema cannot express.

## A staged investigation

Open **Investigate → Staged job investigation**, or enter
`:ops investigation-workflow`. Use one workflow manifest to connect existing
scientific tools. The scrollable report shows evidence, findings, missing
information, suggested diagnostics, execution state, and updated findings.
Inspection runs in an operation worker. It does not collect data during paint
or execute commands from capability files.

1. Set **Workflow manifest**, leave **Stage** at `inspect`, and select **Inspect / prepare**.
2. Read the findings and excluded actions. Missing files and unknown findings do
   not satisfy the planner's prerequisites. The workflow computes available
   evidence from this inspection; it replaces the planner's `known_evidence` list.
3. Choose an admitted **Diagnostic ID**, enter a new **Diagnostic workspace**,
   and set **Stage** to `review-diagnostic`. Select **Inspect / prepare**. Read the nested
   campaign, minimizer, or regression review and its actual resource limits.
4. Select **Review action**, check the prepared plan, then select **Confirm apply**.
   The existing receipt and source revalidation mechanisms still apply.
5. Set **Stage** to `status` and select **Inspect / prepare**. Use the same diagnostic ID
   and workspace. This reads the retained outcome; it does not repeat execution.

The manifest declares `identity` with `cluster`, exact numeric Slurm `job_id`
(including a task or step suffix when needed), and producer `attempt`.
When a job is selected, also supply `scheduler` with its exact `submit`, `start`
and `user` strings. Raven refuses a mismatched cluster, ambiguous selection,
changed scheduler anchor, or recommendation for another attempt. A producer's
attempt label and measurement coverage remain declarations, not independent
proof of source authenticity.

`sources` contains up to 16 entries with unique `id`, an analysis `operation`,
and `path`. Supported analyses are `recommendation`, `scientific-impact`,
`numerical-stability`, `regression-investigation`, `causal-delay`, and
`collective-audit`. `planner` is the path to
an existing investigation-plan manifest. `diagnostics` links a planner action
ID to `operation` and `manifest` for an existing `diagnostic-campaign`,
`minimize-reproducer`, or `regression-sweep`. Paths are relative to the workflow
manifest and read through the current connection. Combined workflow sources
are limited to 8 MiB. The
[workflow schema](../schemas/investigation-workflow.schema.json) describes the
fields. All original standalone analysis forms remain available.

An optional `intent` object contains `manifest` and `capabilities` paths. The
existing diagnostic compiler supplies an additional collection proposal. Its
capability-file commands remain inspection-only. Execution requires a separately
linked, reviewed diagnostic recipe. Planner costs are predictions; the nested
execution review displays its actual enforced limits.

A campaign link can declare `metrics` using the numerical-stability policies
(`name`, `reference`, `absolute`, `relative`). On **status**, Raven reads each
trial's real `result.json` and uses its top-level metric names. It maps repetitions
to the declared campaign cases and runs the existing numerical analyzer. Missing
files, unexecuted trials, and failed execution oracles remain missing measurements.
They cannot produce a passing check. No conversion command or external program
is required for this step. Other diagnostic kinds display their own retained
outcomes; process completion does not by itself prove a scientific claim.

Pure analyses share a bounded result cache. Cache keys include connection,
source identity, complete input revision, algorithm version, and settings.
Results are retained as immutable JSON bytes; views receive isolated values.
Equivalent in-flight analyses are joined. Different attempts and revisions do
not share results. Recommendation expiry is evaluated on every inspection.
Changed evidence invalidates publication and reviewed execution. Each explicit
inspection still reads and verifies its sources; this cache never authorizes an
action from an old observation.

For causal or collective sources, `path` names a registered trace project or a
single trace manifest. Both analyses share one bounded acquisition when they use
the same path. The existing journal reader verifies exact job, attempt, cluster,
process identity, sequence and checksum chains. Pure analysis results are cached
by the checked prefix. Later append-only records may extend an open recording;
they do not rewrite its already checked prefix. Prefix corrections, changed
coverage/loss declarations, and a changed registered stream set require a new
inspection. Open, truncated, invalid, missing-rank, or unsupported evidence remains
unknown and cannot satisfy planner prerequisites. Cross-node timestamp order does
not establish causality. A closed journal with no omitted records proves only its
declared instrumented scope. Trace acquisition currently requires Raven on the
target host, as in the existing standalone trace tools.

The offline generator also writes `workflow.json`. Its identity is explicitly
synthetic. Adapt that identity and scheduler anchor to actual evidence before
opening it from a selected live job. The generated `accuracy-audit` link runs
the same numerical demonstration described below and reads its trial outputs
directly on **status**.

## Recommendation validity

A contract names its exact `cluster`, `job_id` and `attempt`. Optional code,
input and environment SHA256 fields pin more context. Each evidence record
contains `id`, `data`, `sha256` and a boolean `complete` flag. Its digest is
SHA256 over sorted, compact, ASCII JSON with nonfinite numbers forbidden.

Precondition sources must be pinned in the contract. Queries contain `source`,
a list of object keys in `path`, an `op` (`eq`, `lt`, `le`, `gt`, or `ge`) and
`value`. Queries do not evaluate code. Missing or incomplete evidence cannot
pass a query. A changed digest or attempt invalidates the old review. A contract
outside its `created`/`expires` window is expired.

An optional outcome binds `contract_sha256`, `experiment_id`, the experiment's
exact identity and measured evidence. The experiment may have a different job
ID; declared cluster/build/input/environment context must agree. Every
acceptance predicate must pass before the state becomes `supported`. This is
support for the declared workload and checks, not proof of universal correctness
or a causal explanation. Producers remain responsible for honest coverage and
independent experimental units; a checksum is not an authenticity signature.

## Choose what to measure next

Actions can be configuration trials, repeats, traces, scientific validation,
placement checks or numerical audits. Each action declares costs, predicted
queue time, decision gain, success probability, scientific risk, prerequisites
and model provenance. The planner sorts by:

```text
decision_gain × success_probability × (1 − scientific_risk)
----------------------------------------------------------
              seconds + queue_seconds
```

Committed intents consume the budget even when their outcome is uncertain.
Missing prerequisites, excess risk and insufficient remaining time/resource
budgets exclude an action. This is a deterministic greedy proposal using the
supplied model. It is not a Bayesian model, calibrated probability estimate,
global optimum or queue-time guarantee. The planner does not execute actions.

## Scientific impact and revalidation

Declare a DAG of artifacts, attempts, claims, policies and validations. Each node
has explicit dependencies and a `complete` coverage flag. Mark changed roots.
Raven follows reverse edges, retains parent links explaining impact, and
propagates uncertainty from missing dependencies or incomplete coverage.

Revalidation actions declare their covered nodes, required inputs, cost and
method. The planner prefers low-cost coverage while ordering prerequisites.
It does not plan reuse through uncertain lineage. `unresolved_claims` identifies
what the available actions cannot justify. Proposed checks do not become passed
checks merely because the graph planner selected them. Approximate scientific
equivalence is not assumed transitive.

## Numerical measurements and performance regressions

Numerical policies declare a reference and absolute/relative tolerances. A run
has a unique ID, configuration and actual metrics. For each metric, Raven checks:

```text
abs(value − reference) <= max(absolute, relative × abs(reference))
```

The report gives observed minimum, maximum, mean, standard deviation, maximum
absolute error, violating runs and missing runs. A single observation has no
estimated standard deviation. `observed-pass` describes only the supplied
observations. Raven does not inject numerical perturbations into a running job.
Prepared campaign commands may invoke user-installed numerical tools or builds.

Regression manifests name independent experimental units, a paired/independent
design, ordered versions, a baseline and an absolute practical-change margin.
Raven uses Student t intervals with a Bonferroni adjustment across comparisons.
Missing scientific validity, insufficient samples, zero observed variance, or
intervals crossing the decision boundary remain inconclusive. A later recovery
is shown as nonmonotonic evidence; the system does not assume binary-search
monotonicity or identify a causal source commit from timing alone.

## Run bounded diagnostics

A recipe chooses `local-diagnostic` or `slurm` explicitly. Local mode executes
on the host running Raven. Use it for a permitted workstation or allocated
interactive session, not expensive login-node experiments. Slurm mode prepares
one node and one task with explicit CPU, memory and time limits, then uses
Raven's existing native submission and passport path. It does not attach to or
change an existing production job.

Local launch starts a detached Linux driver and returns promptly. It does not
occupy Single mode's only background worker for the lifetime of the experiment.
The driver continues if Raven closes; use its workspace with **Action: status**
after reconnecting. A launch acknowledgment does not mean the experiment passed.

The command is an argv array beginning with an absolute executable. Named
source files require SHA256 hashes. The following placeholders occupy a whole
argument; shell expansion is not performed:

| Placeholder | Meaning |
| --- | --- |
| `{file:NAME}` | The private staged copy of a declared file |
| `{input}` | The current minimization candidate |
| `{result}` | A private per-trial JSON output path chosen by the application |
| `{repo:RELATIVE_PATH}` | A file in the current immutable regression snapshot |

Campaign cases append their explicit argument lists to the base command. The
driver copies declared source files into a private workspace and verifies them
and the executable before trials and after execution. It uses a separate child
helper to set CPU-time, address-space, per-file-size and affinity limits without
`preexec_fn` in the multithreaded UI. Output capture and wall time are bounded;
timeouts and cancellation terminate the child's process group. On Linux, the
supervisor also uses a parent-death signal to clean its process group if the
driver crashes. A program that deliberately detaches into a different session
falls outside this process-group cleanup guarantee; use site containment for
untrusted or independently daemonizing programs.

The limits are 256 trials, 120 seconds per trial, one hour total, 1–8 CPUs,
32–65536 MiB address space, 1 MiB captured output per trial, and 1–10 repeated
oracle checks. Source data is limited to 32 MiB total. Shared libraries and
undeclared external inputs are not captured. These are trusted user programs;
a subprocess and resource limits are not a security sandbox or a total scratch
quota. Initial execution supports one-node CPU recipes. Arbitrary MPI topology
or GPU diagnostic admission is not implied.

The workspace and explicit Python/executable paths must be available on the
compute node. The staged driver is standard-library-only and runs with `-I -S`;
it does not need the Raven package installed on that node. The application may
still require its own packages, libraries, modules or site configuration.

## Minimize a reproducer

The reducer removes whole input lines. Its oracle requires both an
exact exit code and a nonempty output signature. This prevents an unrelated
nonzero exit from being counted as the intended failure. Repeated trials must
agree. A timeout, signal, budget exhaustion or inconsistent repetition stops
the reduction without a false minimality claim. Different ordinary exit codes
or absent signatures are nonmatches under the user's declared oracle.

The original failure is tested first. Each accepted candidate retains that
tested property. Empty input is tested too. The result may be 1-minimal under
line deletion, or the smallest confirmed candidate found before the budget
ended. It is not promised to be globally smallest or independent of scheduler,
input and runtime conditions. The user must supply an oracle that distinguishes
their target failure and input validity correctly.

## Measure an actual Git regression

Use `:ops regression-sweep` with kind `regression`. A `repository` object
declares `path`, 2–8 `commits` containing unique `id` and full immutable `commit`
hashes, the first revision's ID as `baseline`, `minimum_change` in seconds,
`confidence` from 0.8 to 0.95, and `metric` as `result_seconds` or `wall_seconds`.
Set at least three `repeats`. The command must use `{result}` and can address
tracked source through `{repo:measure.py}` or another relative source path.

Raven reads `git archive` snapshots and pins their exact bytes. It does not
switch branches, alter the index, create Git worktrees or replace dirty files.
Snapshots are limited to 4 MiB and 10000 entries per revision. Full commit hashes
are required; branches, tags, trees, symlinks, special files and path traversal
are refused. Submodule contents, Git LFS objects, ignored files and external
dependencies are not fetched automatically. The recipe must provide the
dependencies needed to run each snapshot.

Each measured trial starts from a fresh extracted snapshot. The revision order
alternates between repetitions to reduce fixed ordering bias. The application
must write this result to `{result}` and satisfy the recipe's exact output/exit
oracle:

```json
{"seconds": 0.125, "scientific_valid": true}
```

`result_seconds` uses that application measurement. `wall_seconds` uses the
observed process time, including interpreter/launcher startup. Neither skips
the required `scientific_valid` check. A false, absent or invalid check leaves
that version incomparable. The application defines the scientific criterion;
Raven does not infer it from an exit code.

The completed report uses 2000 deterministic bootstrap resamples of independent
median differences, with a Bonferroni family adjustment. It reports intervals,
the first observed regression and later recovery. Fewer than three valid samples,
zero variance, missing outputs, exhausted budgets and noisy intervals cannot
establish a regression. Bootstrap coverage itself remains approximate, especially
with small samples and correlated host noise. A measured difference is not proof
that a specific source edit caused it. The sweep does not assume monotonic
performance or silently perform a binary search through unreviewed commits.

The archived driver and commit snapshots also work inside the reviewed Slurm
allocation. Only the initial inspection needs Git; compute execution reads the
staged snapshots. Retained trial intents, application measurements and outcomes
allow the user to inspect partial or cancelled sweeps.

## Receipts, interruption and recovery

`launch.intent.json` is written before local execution or submission. Slurm
returns `submission.result.json`; acceptance means submission, not completion.
The driver exclusively creates `run.claim.json` before any trial. Each trial
writes an intent and an outcome receipt in `trials/NNNN/`, alongside bounded
`output.log` and any application `result.json`. Final reduction/campaign state
is written to workspace `result.json`. Interrupted drivers retain the claim and
may write `error.json`.

`driver.process.json` records PID, process start ticks, boot identity and recipe
digest. Status checks that identity on the current host. A reviewed local
cancellation uses a Linux pidfd, rechecks identity, records `cancel.intent.json`,
and requests graceful termination. It does not signal a reused PID. The driver
stops the current trial and publishes partial outcomes. **Action: status** must
confirm that it stopped; cancellation acknowledgment alone is not completion.
Missing pidfd support causes a clear refusal. A remote compute-node driver is
cancelled through Slurm, not by signaling a coincident local PID.

Raven refuses to reuse that workspace or repeat an uncertain submission. Read
the existing receipts and scheduler state before preparing an explicitly new
campaign. A requeued driver also refuses to replay the same claim. This avoids
duplicate experiments but does not provide automatic crash resumption.

## Offline demonstration

Create a new example directory; no work is executed by this step:

```sh
python3 examples/scientific-investigation/prepare_demo.py /tmp/raven-science-demo
```

Open `:ops minimize-reproducer`. Use `/tmp/raven-science-demo/minimize.json`, a
new destination such as `/tmp/raven-minimum`, and **Action: launch**. Review and
apply. `minimal-input.txt` should contain only `needle`.
Select **Action: status** until the driver reports `reduced` before reading it.

Open `:ops diagnostic-campaign`. Use `campaign.json` and a different new
destination such as `/tmp/raven-numerical`. Review and apply. The program computes
an actual order-sensitive sum and a compensated sum. Wait for **Action: status**
to report `completed`, then convert measured outputs:

```sh
python3 examples/scientific-investigation/collect_numerical.py \
  /tmp/raven-numerical > /tmp/raven-science-demo/numerical.json
```

Inspect that file with `:ops numerical-stability`. The naive sum fails the
declared answer check; the compensated sum passes for these observations.
The other generated files are clearly labeled synthetic analysis examples.

To create a separate real-execution regression example:

```sh
python3 examples/scientific-investigation/prepare_regression_demo.py \
  /tmp/raven-regression-demo
```

This creates a new synthetic Git repository only in that directory. Open
`:ops regression-sweep`, use its `sweep.json`, choose a new workspace, review and
apply. The program measures a deliberate delay with a scientific sum check.
The report should identify the slower middle revision and subsequent recovery;
heavy host noise can correctly leave a comparison inconclusive.

## Validation boundary

Tests execute real bounded child processes and the standalone staged driver.
They check exact failure signatures, empty candidates, repeated-oracle
disagreement, budgets, timeouts, output floods, signals, changed source files,
stale reviews, independent scientific evidence, missing lineage, deep graphs,
nonmonotonic regressions and uncertain scheduler receipts. Slurm submission
tests use a fixture transport. Actual scheduler policy, compute-node visibility,
resource enforcement and scientific-tool compatibility require site validation.
