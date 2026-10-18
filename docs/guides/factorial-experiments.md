# Controlled experiment campaigns

Raven can compare explicitly selected implementation choices in a small,
reviewed experiment. This experimental workflow estimates main effects and
pairwise interactions. It does not infer causal percentages or presume that a
requested GPU, BLAS, or MPI setting took effect.

The workflow reuses Raven's diagnostic runner, including its source hashes,
resource review, process cancellation, durable trial receipts, and allocation
ownership checks. Planning and analysis execute no application code.

## Try the controlled reduction example

From a Raven source checkout, create a design and a new preparation directory:

```bash
python examples/factorial-experiment/make_design.py > reduction-design.json
python scripts/prepare_experiment.py reduction-design.json \
  --destination reduction-plan
```

The example varies input order and summation method. Four independently
identified repetitions of each of four combinations give 16 trials. Its local
budget is one CPU, 256 MiB, two seconds per trial, and 30 seconds overall. The
application reports the controls it actually used and the measured sum.

In Raven, open Operations with `:ops factorial-design` and select the design to
inspect its schedule. To execute, open `:ops diagnostic-campaign`, select
`reduction-plan/recipe.json`, choose **Launch**, and choose a new destination
workspace. Review the executable, pinned sources, explicit local mode, and
limits before confirming. The same operation offers status and cancellation
for the workspace. Launching is a separate reviewed action; compiling a plan
never starts a trial.

After the driver has ended, collect its receipts into another new directory:

```bash
python scripts/prepare_experiment.py reduction-design.json \
  --workspace /absolute/path/to/reduction-workspace \
  --destination reduction-results
```

Open `:ops factorial-analysis` and select `reduction-results/results.json`.
The controlled example demonstrates that floating-point order and algorithm
can change the sum. Its deterministic residual is effectively zero, so Raven
reports contrasts without pretending to have inferential confidence intervals.

## Connect an application

A design uses `raven.factorial-design/v1`; its complete machine-readable format
is [factorial-design.v1.schema.json](../schemas/factorial-design.v1.schema.json).
Start with the generated design and replace its pinned source files, command,
factors, output units, semantic label, and equivalence policy.

Each of one to four factors has exactly two compatible levels. A level supplies
literal command arguments; Raven never constructs a shell command. The template
recipe must have one empty case, `kind: "campaign"`, `limits.repeats: 1`, and an
explicit `{result}` command argument. Design repetitions, from three to ten,
already identify every trial. Choose `randomized` for seeded global order or
`blocked` for independently shuffled complete repetitions. The all-low control
appears once per repetition. Incompatible cells are refused; select a compatible
subset instead of silently omitting cells and confounding the analysis.

The application writes this JSON object to its `{result}` path and prints the
recipe's declared success marker when its scientific check succeeds:

```json
{
  "value": 1.0,
  "unit": "sum",
  "output_sha256": "d0ff5974b6aa52cf562bea5921840c032a860a91a3512f7fe8f768f6bbe005f6",
  "semantic": "sum(1e16,1,-1e16)",
  "controls": {"order": "paired", "method": "accurate"}
}
```

Hash the actual output whose bitwise identity matters, using an explicitly
chosen canonical representation. Report observed effective controls, not a
copy of requested settings. Omit unsupported controls or report their effective
level: Raven marks mismatches and excludes those observations from effect
estimation. The result is limited to 64 KiB and the recipe output budget.

Raven assigns the trial ID from its durable receipt. An application cannot
choose a different trial ID. Imported analysis files use
[factorial-results.v1.schema.json](../schemas/factorial-results.v1.schema.json).
Imported observations are producer assertions, not proof that a live scheduler
or hardware enforced a setting.

## Workstations and lab allocations

Use `local-diagnostic` only for an explicitly budgeted workstation experiment.
On a Slurm system, change the recipe to `mode: "slurm"` and set its `account`
and `partition` to your authorized lab or personal allocation. These are the
scientific experiment's resources, separately from Raven Heavy's worker.
Review the generated campaign through the existing diagnostic launch workflow;
it submits one allocation containing the bounded serial trials. The driver
refuses Slurm mode outside an allocation. No experiment runs automatically when
you open a report, reconnect, or change a display setting.

Choose limits for the actual process tree. Local diagnostics constrain CPU
affinity, elapsed time, output files, and inherited per-process virtual address
space (`RLIMIT_AS`); the memory field is not an aggregate MPI process-tree RSS
guarantee. Use a site-enforced Slurm allocation when aggregate memory isolation
is required. A three-rank MPI experiment needs at
least three allocated CPUs. The included `reduction_mpi.c` and `mpi_adapter.py`
illustrate an MPI gather followed by an explicitly controlled reduction. They
do not claim to control an MPI implementation's internal reduction tree. The
qualification helper `scripts/evaluate_experiment_design.py` builds and runs
this small local fixture when a matching MPI compiler and runtime are available;
its default ceiling is three CPUs, 1 GiB, eight seconds per trial, and 120
seconds overall. Running that helper explicitly authorizes those local test
processes; it submits no Slurm job.

## Cancellation and recovery

A workspace is single use. Never rerun its driver, delete receipts to retry a
case, or overwrite its preparation files. Collection accepts up to four
workspaces for the same exact design. It checks local recipe order, trial
sequence, expanded arguments, final committed declarations, and duplicate
intents across workspaces, with a cumulative 8 MiB read limit.

If all drivers ended and every started trial has consistent committed evidence,
collection writes `remaining-recipe.json` for cases that never started. Review
that recipe as a new campaign in a new workspace. Collect both old and new
workspaces afterward using repeated `--workspace` arguments. Committed work is
excluded, including unsuccessful or unsupported trials. A missing receipt,
missing committed directory, live driver, or inconsistent identity pauses
recovery. Restore and reconcile the original evidence before reviewing a new
remaining recipe; absence is not permission to repeat work.

## Read the conclusions

Bitwise change compares output hashes against the repeated controls. Tolerance
failure compares the scalar output against the declared absolute/relative
reference. Semantic change compares the explicit scientific output label.
These are independent checks. Tolerance failures remain measurable outcomes;
semantic changes and unsupported controls are excluded from effect estimation.

Main effects are average high-minus-low contrasts. A pairwise interaction is
half the corresponding difference of differences. Blocked designs include
block intercepts. Raven checks rank, residual degrees of freedom, and numerical
conditioning before reporting estimates. Nonzero residuals permit 95%
Bonferroni family Student-t intervals under independent, homoscedastic residual
assumptions. Insufficient, confounded, unstable, or essentially noiseless data
receive an explicit limitation instead of invented uncertainty.

Residual variation can include omitted higher-order interactions, uncontrolled
factors, and measurement noise. The supplied held-out synthetic evaluation and
local Python/MPI fixtures validate known effects; they do not establish efficacy
on an unseen scientific workload or qualify a site's GPU/BLAS stack.
