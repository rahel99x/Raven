# Connect and validate a project

Use the project guide to inspect a native reporting project, validate one sample
run, and prepare reporting hooks. The project root and declared sources must be
readable through the active connection.

Open `:projectguide /absolute/project/path`, or choose **Connect → Connect and
validate a research project**. The form reads a bounded inventory and identifies
which declared sources can populate each Research view. Inspection is read-only.

## Validate an actual sample

1. Open the project guide with the project's absolute path.
2. Set **Sample run ID** to one directory name under `runs/`.
3. Select **Inspect / prepare** and read each check's status.

The equivalent explicit inspection is:

```text
:ops run project-guide root="/project/solver" run_id=job-1234-a1
```

When a scheduler job is selected, supply the exact sample run ID. A run's job ID
and cluster do not prove that it belongs to the current scheduler attempt:
Slurm can reuse an ID, and native inventory times are reporter wall times.
The guide therefore does not choose a run from job ID alone. If no job is
selected, it can use a project's sole run. An ambiguous selection or an unknown
explicit run is never replaced with a different attempt.

**Expected result:** The report distinguishes declared paths from checked content:

| Check | What Raven verifies |
| --- | --- |
| Inventory | Run directory, ID, attempt, job identity and native path rules |
| Metrics | Complete records from one confined 256 KiB read, up to 128 retained points per series |
| Summary | Native terminal summary, matching run ID and any declared job/attempt identity |
| Artifacts | Actual output checks from the native contract, with shared limits of 256 entries and 8 MiB |
| Logs | Availability of at most two declared streams through 16 KiB tails |

Native run binding also checks declared array/log indexes, contract structure
and supported passport records. Other Research sources remain declaration
coverage; open their native views for scientific analysis. Local and supported
SSH reads use the same confined readers. Replaced attempts, symlinks, missing
files and malformed records produce explicit notices.

`ready` applies to that check's observed scope. `partial` retains inspection
limits or incomplete samples. `missing`, `invalid` and `not-declared` identify
the action needed. A running job can correctly lack a terminal summary. Old
metrics can be outside the bounded sample, and an incomplete final JSONL line
waits for a newline. No status certifies all project data or scientific results.

## Copy the reporting hooks

Set **Batch script relative to project** and **Experiment label** if needed.
The default batch path is `tower-batch.sh`; the experiment comes from the sample
or defaults to `experiment`. Inspect again to regenerate the quoted hooks.

Copy the **Batch hook** into that batch script. Keep your site's required
`#SBATCH` directives before executable lines. Pass the actual program and its
arguments through your normal submission procedure, for example:

```bash
sbatch tower-batch.sh python3 solve.py --iterations 100
```

The hook uses `tower report exec`. It records the actual start, stdout, stderr,
wall time, exit status and finish, and passes `TOWER_RUN_DIR` to the application.
It refuses an empty payload. Use an installed `tower` command available on the
compute node; an interactive alias alone is insufficient. Do not overwrite
application source with a generated batch hook.

The **Application metric/output hooks** use actual application variables and
declared files. Rename `loss`, `LOSS` and `STEP` for your scientific measurement.
Undefined values fail explicitly; Raven does not insert example measurements.

The **Manual lifecycle alternative** is for an existing launcher that already
owns its traps and exit handling. It provides `report start` and `report finish`
with explicit run identity. Preserve the actual payload status. Choose either
the automatic `report exec` lifecycle or the manual lifecycle; do not nest them.

## Initialize the optional descriptor

1. Set **Prepare project descriptor** to `yes`.
2. Select **Inspect / prepare**, then **Review action**.
3. Check the destination and select **Confirm apply**.

**Expected result:** The reporter creates the standard descriptor and preserves
existing data. Descriptor creation does not repair failed sample checks or
execute the generated hooks.

Read [Bash automation](research-automation.md), the
[project standard](../PROJECT_STANDARD.md), and the
[generated form reference](../reference/operations.md) for complete reporting
contracts and controls.
