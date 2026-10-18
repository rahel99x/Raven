"""Installed, offline setup recipes for the Research and Analytics views.

These are instructions, not collectors: looking up a topic never reads project
files, starts a process, or queries a scheduler. Actions use the regular command
handlers so source selection, validation, and action reviews stay in one place.
"""
from __future__ import annotations


METRIC_EXAMPLE = (
    '{"t":1791801005.0,"step":1,"metrics":{"loss":0.42},'
    '"progress":{"completed":1,"total":100,"unit":"iterations"}}'
)
GPU_EXAMPLE = "2026/10/12 14:30:00.000, 0, 83, 6120"

# Keep the shipped recipes independent of a checkout or installed documentation.
# Lists returned by topic() are copies: one dialog cannot change another's text.
_GENERAL = {
    "overview": (
        "Setup Raven",
        (
            "Choose the task you want to configure. Each Research and Analytics view also has its own setup action.",
            "1. Connections and accounts: choose a named site or lab profile and review Raven Heavy's allocation preferences.",
            "2. Launch defaults: save reusable workload resources and recording choices for your project.",
            "3. Recordings and metrics: connect a real data file, validate its format, preview measurements, or copy an application snippet.",
            "4. Refresh and performance: inspect queue and recording read intervals, observed freshness, and advanced worker preferences.",
            "5. Installation and diagnostics: check the worker environment, terminal, and source health.",
            "Raven Heavy is the dashboard's analysis worker. Its allocation settings do not change an application's existing sbatch script or the billing account of an existing job.",
            "On local Slurm, a blank account can use the scheduler default when permitted. On shared clusters, use an account assigned to you; an account label is not an access grant.",
            "Standalone desktop mode is for machines without Slurm. A workstation with personal Slurm still uses the Slurm site.",
        ),
        (("Connections and accounts", "profiles", False),
         ("Launch defaults", "launchsetup", False),
         ("Recordings and metrics", "connectdata", False),
         ("Refresh and performance", "refreshsettings", False),
         ("Installation and diagnostics", "setup installation", False)),
    ),
    "storage": (
        "Recording storage and retention",
        (
            "JSONL is the default. Compact blocks are opt-in: they preserve raw values, timestamps, missing fields and attempt identity while compressing groups of records.",
            "Preview a selected job before converting. Conversion copies and verifies every admitted row, then publishes a new file; the original JSONL is preserved. Incomplete or oversized inputs are refused with their actual error.",
            "Saving a format affects new streams; existing JSONL changes only through explicit conversion. An existing compact file remains canonical if JSONL is selected later, preventing a stale original from replacing newer observations.",
            "Each compact block has bounds, checksums and an append commit marker. Corruption exposes only its verified prefix and a coverage error; the damaged file is never silently repaired or overwritten.",
            "Native retained archives are local to Raven's state directory. Project scientific metrics remain authoritative in the project and are not converted by these actions.",
            "For reproducible commands: raven series inspect FILE; raven series migrate FILE (preview); raven series migrate FILE --apply; raven series export FILE --output NEW.jsonl.",
            "Retention working archives use configured raw, 10-second and 1-minute tiers. Raw protection is enabled by default; originals and receipt references are always preserved. Set storage.retention in the managed configuration to choose ages and protect_raw for the working copy.",
            "Tier results label raw versus rollup, counts, extrema, exact decimal sums, unknown units, source rows and observed gaps. Rollups cannot answer original ordering, quantiles, rates or smaller boundary intervals exactly. They never replace raw chart or export inputs.",
            "Stop other Raven writers before applying a CLI migration. Keep state and original JSONL when downgrading. Compact storage is experimental until its storage, CPU and memory tradeoffs are measured for your recording workload.",
        ),
        (("Preview selected recording conversion", "setup storage-preview", False),
         ("Convert selected recording, preserve original", "setup storage-convert", False),
         ("Save compact block format", "setup storage-format blocks-v1", False),
         ("Save default JSONL format", "setup storage-format jsonl", False),
         ("Preview retention policy", "setup storage-retain-preview", False),
         ("Publish protected retention working copy", "setup storage-retain", False),
         ("Inspect explicit-fidelity tiers", "setup storage-tier-query", False)),
    ),
    "exports": (
        "Automated reporting and exports",
        (
            "Run an existing Bash script with raven launch test.sh --research experiment --gpu. Preview prerequisites without writing or submitting using --check; local execution requires explicit --local.",
            "Select the exact job or run in Results, Jobs, Recents, History, Research, or Analytics, then choose Export data in the File menu or Run home. A collapsed group exports only its already admitted members.",
            "Select available families and individual application metrics or GPU devices. Full recorded run is the default; Current chart range is available only with a known recorded epoch clock. Original retained records are exported, not chart pixels.",
            "Choose Job's project folder / exports, a path relative to that project, or an absolute path. Review the actual filesystem host, unique output paths, source cutoffs, and unavailable data before Export. Missing parents are created only when Export runs.",
            "Save project preferences or a named preset such as Paper figures. Absolute destinations are scoped to their filesystem host and login user. Presets contain no transient job IDs.",
            "Export when this attempt finishes captures an exact request during Export. The project-default checkbox applies to future launches only after Save preferences. Failed and cancelled runs may export partial evidence. Killed jobs remain pending until a reconciliation worker is available.",
            "Remote source export currently requires running raven export-data on the project host. An identical local pathname never implies a shared filesystem.",
        ),
        (("Project launch and recording defaults", "launchsetup", False),
         ("Export selected run data", "exportdata", False),
         ("Choose an exact project run", "runs", False),
         ("Connect a project", "projectguide ", True)),
    ),
    "project": (
        "Project reporting setup",
        (
            "Goal: attach application progress, results, and logs to one exact execution attempt.",
            "1. Open Connect a project, enter its absolute root on the connected filesystem, and inspect the discovered runs. Choose the exact run; similar names do not identify attempts.",
            "2. Keep each attempt in PROJECT/runs/RUN_ID/. A run.json manifest uses schema raven.run/v1, a unique run_id, experiment_id, positive attempt, and state. Include job_id only when there is a real Slurm job.",
            'Minimal standalone manifest: {"schema":"raven.run/v1","run_id":"fit-a1","experiment_id":"fit","attempt":1,"state":"RUNNING","paths":{"metrics":"metrics.jsonl"}}',
            "Paths in run.json are run-relative POSIX paths. Use exact files with no '..', absolute paths, globs, or symlink traversal. Array job_id uses the actual parent_task identity.",
            "3. Append one complete JSON object and a newline to metrics.jsonl for each observation. The following is a format example; replace its timestamp and values with actual measurements:",
            METRIC_EXAMPLE,
            "Use epoch seconds for t, finite JSON numbers for metrics, and one writer per run. Keep metric names and units consistent. Missing measurements are omitted; zero means measured zero.",
            "4. Finalize summary.json with schema raven.summary/v1, id equal to run_id, name, and terminal state. Add measured runtime_seconds and any CPU or memory values with their actual scope.",
            'Minimal completed summary: {"schema":"raven.summary/v1","id":"fit-a1","name":"fit","state":"COMPLETED"}',
            "5. Declare logs through paths.log_index and optional outputs through paths.contract. Preserve run files for later inspection.",
            "For an existing project, the portable V2 kit supplies run.sh, batch.sh, portable_run.py, and reporting.py. It requires Python 3.10+; Raven need not be installed in the application environment.",
            "After copying the kit into .raven/reporting-v2, set RAVEN_EXPERIMENT and RAVEN_SCRIPT to the real scientific source, then wrap the existing launch once:",
            "  bash .raven/reporting-v2/run.sh srun python3 train.py",
            "On a permitted local workstation without Slurm, omit srun. Inside MPI, use one coordinator around srun or mpirun, not a separate wrapper for each rank.",
            "The kit prints RAVEN_RUN_DIR and supplies RAVEN_REPORT_HELPER / RAVEN_REPORT_PYTHON to the application. With actual loss, step, and total values already set, a Bash hook is:",
            '  "$RAVEN_REPORT_PYTHON" "$RAVEN_REPORT_HELPER" metric --value "loss=$loss" --step "$step" --completed "$step" --total "$total" --unit steps',
            "V2 names the integration guide revision. Keep the /v1 schema markers. Full copy/install recipes: https://github.com/rahel99x/Raven/blob/main/docs/PROJECT_REPORTING_V2.md",
        ),
        (("Connect data and validate a sample", "connectdata", False),
         ("Connect a project", "projectguide ", True),
         ("Attach a metric file", "metrics ", True),
         ("Open Experiment", "workspace experiment", False)),
    ),
    "telemetry": (
        "CPU, memory, and GPU setup",
        (
            "1. Select an actual job in Jobs, then open Metric sampling to see source availability, freshness, requested cadence, and effective cadence.",
            "2. CPU/RSS: Slurm sstat supplies live task observations; sacct supplies accounting outcomes. The site must enable job accounting. Raven cannot reconstruct a missing historical time series from a final total.",
            "CPU per core is measured CPU time divided by observed wall time and allocated CPUs. Later samples support an interval rate; the first usable value may be cumulative. Memory uses observed RSS and the known request.",
            "MaxRSS is the largest observed task RSS, not necessarily the total memory of a multi-process job. Missing CPU coverage can coexist with valid memory observations.",
            "3. GPU: managed Raven reads an existing job trace. Enabling GPU display does not start nvidia-smi or create a new trace producer inside jobs.",
            "Write <actual Slurm WorkDir>/logs/gpu-util-<job_id>.csv; arrays use parent_task. Keep the trace after completion. Each CSV row has NVIDIA timestamp, integer device index, utilization percent, and optional used memory in MiB:",
            GPU_EXAMPLE,
            "Use nvidia-smi inside the allocated job with --query-gpu=timestamp,index,utilization.gpu,memory.used --format=csv,noheader,nounits. Select only devices assigned to that job and give the producer a cleanup trap.",
            "Do not assume CUDA_VISIBLE_DEVICES indices are host device indices. This CSV has no node identity; do not combine identical device indices from different nodes. The reader interprets timestamps in its local timezone.",
            "Slurm gres/gpuutil and gres/gpumem require working GPU accounting, such as NVML. Their aggregated high-water counters are not per-device instantaneous curves. A working NVIDIA driver may produce the CSV even if Slurm's NVML plugin is absent.",
            "4. Application loss/progress belongs in metrics.jsonl. Naming an application metric cpu_pct or gpu_util does not populate native Analytics charts.",
            "Collection obeys site policy, polling floors, and demand limits. Changing chart zoom or opening Setup does not collect missing past samples.",
        ),
        (("Connect data and inspect recording readiness", "connectdata", False),
         ("Inspect metric sampling", "telemetry", False),
         ("Choose GPU source", "gpuprovider", False),
         ("Check source health", "tab sources", False)),
    ),
    "installation": (
        "Installation and preferences",
        (
            "1. Install Raven from this repository or its built wheel. The bare package-index name raven may refer to a different project. All three commands, raven, ravenl, and ravenh, come from this installation.",
            "In a Raven checkout, pipx install '.[terminal]' installs the optional embedded terminal. Its executable directory must be on PATH; shell aliases are unnecessary. The core package also supports a standard Python virtual environment.",
            "2. From a shell, run ravenl doctor and ravenh --plan. Doctor inspects capabilities; --plan previews Heavy without allocating.",
            "3. Default local Slurm: use ravenl / ravenh with your Slurm settings. Without Slurm, explicitly use ravenl --site desktop. Desktop mode does not bypass permissions or make a cluster login node suitable for computation.",
            "4. Inspect effective settings with ravenl config explain. The default managed file is $XDG_CONFIG_HOME/raven/config.json, falling back to ~/.config/raven/config.json; --config or RAVEN_CONFIG can select another file.",
            "ravenl --write-config creates a new private config and preserves an existing file. Configure the cluster connection and a compute-node Python that can import this same Raven build.",
            "5. Use Allocation settings for personal/lab billing and Heavy resource defaults. A saved preference applies to a later allocation; it cannot change a running worker's grant.",
            "6. Use Preferences for terminal, sampler, and clipboard settings. Test keys and glyphs if the screen or mouse behaves incorrectly.",
            "SSH authentication and jump hosts belong in SSH configuration. Restart after changing the connection/profile; existing workers and terminals remain tied to their original target.",
            "On CARC or UCI HPC3, use the site's approved account, partition, compute environment, and storage. Site policies remain authoritative. Student login, lab billing account, and project directory are separate choices.",
        ),
        (("Recording storage", "setup storage", False),
         ("Heavy allocation settings", "setup allocation", False),
         ("Open preferences", "settings", False),
         ("Test terminal", "terminaltest", False),
         ("Inspect Heavy", "raven inspect", False),
         ("Check source health", "tab sources", False)),
    ),
}


_RESEARCH = {
    "checkpoint-advice": (
        "Checkpoint advice",
        (
            "Source: a raven.checkpoint-advice/v1 JSON document; examples/checkpoint-advice.json provides a complete starting point.",
            "1. Declare the application and allocation scope, useful work in seconds, and whether checkpoint/restart support has been verified.",
            "2. Supply measured checkpoint and restart duration samples, or explicitly labelled estimated ranges. File size and modification times cannot establish checkpoint duration.",
            "3. Supply attempt exposure and outcomes. Node failures inform the interruption model; timeouts and preemption remain separate constraints. Sparse or incompatible evidence produces an explanation instead of an interval recommendation.",
            "4. Attach the document and review the advised, fixed-interval, and no-checkpoint comparisons, uncertainty, and assumptions. These are advisory model estimates, not measured site efficacy.",
            "Launch review can attach the same document through advanced launch settings. Raven does not insert checkpoint hooks or alter a submitted job automatically.",
        ),
        (("Attach checkpoint advice", "checkpoint-advice ", True),),
    ),
    "experiment": (
        "Experiment",
        (
            "Source: RUN/metrics.jsonl, selected through run.json paths.metrics or Attach metric file.",
            "1. Select the project and exact run, or attach an existing UTF-8 JSONL file.",
            "2. Append one newline-terminated JSON object per observation. This example shows the complete format:",
            METRIC_EXAMPLE,
            "t is epoch seconds. metrics maps stable names to finite numbers; use no null, string, Boolean, NaN, or Infinity values. Omit unavailable observations.",
            "step is an optional nonnegative integer; phase is an optional label. progress requires completed and positive total, with 0 <= completed <= total; unit names the work, such as iterations.",
            "Use one writer, at most 64 metrics per row, and at most 65,536 encoded bytes per row. Flush complete records. Incomplete trailing lines wait for their newline.",
            "3. Open Experiment. Several advancing progress observations permit rate and ETA; a single row shows a value but cannot establish a reliable rate.",
            "Pin, hide, reorder, or rename displayed metrics with Dashboard controls. Declared display units do not convert the producer's measurements.",
            "These are application graphs. Native CPU/RSS/GPU Series still need their scheduler or trace sources.",
        ),
        (("Attach metric file", "metrics ", True),
         ("Connect project", "projectguide ", True)),
    ),
    "arrays": (
        "Arrays",
        (
            "Source: scheduler array records; an optional array manifest adds scientific labels and parameters.",
            "1. Submit the array normally. Keep a separate run for every task and restart. Record the actual array parent_task job_id and cluster identity; similar job names do not establish an array.",
            "2. Open Arrays to inspect observed task states and exact selected task evidence. Unsampled tasks remain unknown, even when the scheduler advertises a larger range.",
            "3. To label inputs, attach an array manifest or bind its run-relative path through run.json paths.array_manifest. Its task indices must match the actual array.",
            "Raven shows counts, observed failures, duration samples, and sampled slow tasks. Optional labels do not create scheduler observations.",
            "Retries prepare only observed failed tasks and require the original script, working directory, and action review. Opening the view never retries a job.",
        ),
        (("Attach array map", "arraymap", False),),
    ),
    "evidence": (
        "Evidence",
        (
            "Sources: exact job identity and scheduler details, linked application metrics, and declared logs.",
            "1. Select the job and its exact run. Register stdout/stderr and any rank logs through RUN/logs.json; bind it with run.json paths.log_index.",
            'Minimal log index: {"schema":"raven.logs/v1","run_id":"fit-a1","logs":[{"id":"stdout","path":"logs/stdout.log"}]}',
            "Paths in the log index resolve from its containing directory. Each entry has a unique id and exact path; up to 256 entries are supported. Publish the index atomically and keep the actual log files.",
            "2. Preserve logs with the run, then open Evidence. Raven cites the source file or scheduler fact for each observation and shows gaps in inspected coverage.",
            "3. Open a cited log to inspect the original context. A scheduler failure, an application error, and a missing final output are distinct observations.",
            "No diagnosis.json is required. Hypotheses depend on observed facts; missing evidence does not become a confirmed cause or a scientific success claim.",
        ),
        (("Connect project and logs", "projectguide ", True),
         ("Investigate a job", "investigate ", True)),
    ),
    "artifacts": (
        "Artifacts",
        (
            "Sources: an output contract and the actual declared files.",
            "1. Put the standard contract at PROJECT/.raven/contracts/outputs.v1.json, or declare a run-specific contract with run.json paths.contract.",
            "2. Declare exact output paths, required/optional status, and relevant checks: size bounds, format, JSON keys, CSV rows/columns, or known hashes. The producer must write the actual files.",
            'Minimal output contract: {"version":1,"outputs":[{"path":"outputs/results.csv","required":true,"format":"csv","columns":["step","loss"],"min_rows":1}]}',
            "Contract paths resolve from the selected run directory. This example expects a real CSV with header step,loss and at least one data row. Replace it with the files and fields your application actually produces.",
            "3. Attach the contract and its root, then open Artifacts to inspect existence, structural checks, and previews. A missing required final output can be expected while the job is still running.",
            "Validation records the evidence it inspected. A file changing during inspection is incomplete; a structural pass does not establish scientific correctness.",
        ),
        (("Attach contract and root", "artifacts ", True),
         ("Browse project outputs", "outputs", False)),
    ),
    "passport": (
        "Passport",
        (
            "Source: a genuine immutable Raven passport, retained in the run's declared paths.passports location.",
            "1. Capture the actual scientific script and working directory through Passport capture, or retain the native passport from an existing submission workflow.",
            "2. Attach the recorded passport to the selected run or open it with Passport show. Inspect code, input, environment, and allocation provenance that was actually recorded.",
            "3. Compare two recorded passports to identify changes before treating their runs as comparable.",
            "run.json describes the reporting attempt; copying or renaming it does not create a passport. Missing provenance remains unknown.",
        ),
        (("Capture a passport", "passport capture ", True),
         ("Open a passport", "passport show ", True)),
    ),
    "submit": (
        "Submit",
        (
            "Source: the intact native submission plan created from the actual batch script and working directory.",
            "1. Prepare the script with its effective sbatch options. Specify the real personal/lab --account, partition, and resource requests when required by the site.",
            "2. Inspect preflight findings, resource requests, identity, and review digest. Preserve the plan through the run's paths.submit if it should be revisited later.",
            "3. Use the existing submission review to authorize execution. Opening or discovering a plan never submits it.",
            "Raven Heavy's default billing account does not rewrite this script. Slurm directives do not expand shell variables; supply variable options as command-line arguments or generate reviewed literal directives.",
        ),
        (("Prepare batch script", "prepare ", True),
         ("Open submission tools", "workbench run", False)),
    ),
    "predict": (
        "Resources",
        (
            "Source: comparable observed history plus an explicit workload/resource query, through paths.predict or paths.planning.",
            "1. Finalize real summaries with runtime_seconds, allocated cpus/nodes/gpus, partition, and available cpu_seconds or memory_bytes. Give memory its measured scope: job_peak, per_node_peak, or max_task_rss.",
            "2. Export the selected attempts into a raven.planning bundle with version 1 and history. Set query to the same scientific identity and proposed resource configuration; retain known account, QoS, script hash, and parameters.",
            "3. Attach that planning file. Inspect sample counts, coverage, intervals, and reasons to abstain before using a recommendation.",
            "Durations are seconds, CPU time is CPU-seconds, and memory is bytes. Do not pool changed workloads or convert one rank's memory into a whole-job peak. Failures, timeouts, and OOMs are evidence too.",
        ),
        (("Attach resource evidence", "predict --file ", True),),
    ),
    "forecast": (
        "Forecast",
        (
            "Sources: an actual pending job and scheduler start estimate; optional paths.forecast/planning supplies retained forecast evidence.",
            "1. Select the pending job. Keep its submit timestamp and current scheduler snapshot.",
            "2. For historical validation, record each forecast before the job starts: job_id, submit, issued_at, and predicted_start in epoch seconds. Add actual_start only after it occurs.",
            "3. Open Forecast or attach the retained planning source. Read source age, revision, sample count, and calibration coverage alongside any start interval.",
            "A scheduler estimate can change. Application runtime predictions and values filled in after execution cannot establish a historical queue forecast.",
            "Optional request alternatives use raven.forecast-scenarios/v1 (examples/forecast-scenarios.json). Supply pre-start model points, complete request/context and distinct completed outcomes. Missing runtime-risk or policy evidence causes abstention; alternatives never change a job automatically.",
        ),
        (("Attach forecast evidence", "forecast --file ", True),),
    ),
    "blockers": (
        "Blockers",
        (
            "Sources: the selected pending job and matching scheduler snapshot, or a paths.blockers/planning report.",
            "1. Select the actual pending job and inspect its reason and dependencies.",
            "2. For a captured report, include job_id, jobs, and matching details. Add partitions, nodes, share, and health only when observed in the same snapshot.",
            "3. Open Blockers to distinguish dependency, account/access, capacity, and source-health evidence. Missing site limits or association data stay unknown.",
            "This is a check against observed facts, not a guaranteed scheduler decision or exact start time. Choosing an account in Setup cannot grant membership or remove a site limit.",
        ),
        (("Attach blocker evidence", "blockers --file ", True),
         ("Inspect scheduler sources", "tab sources", False)),
    ),
    "tradeoffs": (
        "Tradeoffs",
        (
            "Source: paths.tradeoffs/planning with explicit candidates and comparable measured history.",
            "1. Define each candidate's name, partition, total cpus/nodes/gpus, mem_bytes, and time_seconds. Keep the scientific workload constant; retain known account and QoS.",
            "2. Supply matching runtime observations and actual queue outcomes if queue comparisons are needed. Unsupported candidate dimensions must remain unknown.",
            "3. Attach the file. Compare runtime, wait/completion estimates, reserved core-hours, GPU-hours, and memory GiB-hours together with uncertainty and coverage.",
            "Frontier membership depends on the supplied candidates and bounds. Reserved resource-hours are quantities, not a currency charge or an approval to use another allocation.",
        ),
        (("Attach tradeoff candidates", "tradeoffs ", True),),
    ),
    "scaling": (
        "Scaling",
        (
            "Source: paths.scaling/planning containing measured scaling records, or a native raven.scaling recipe for proposed experiments.",
            "1. Record actual workers, problem_size, repeat identity, parameters/script hash, terminal state, and runtime_seconds for every attempt. Retain failures.",
            "2. For strong scaling, keep total problem size fixed. For weak scaling, document the constant work per worker. Choose an explicit baseline and actual independent repeats.",
            "3. Analyze measured records to inspect runtime spread, speedup, efficiency, and supported resource-hours. A recipe alone describes a plan and supplies no measured speedup.",
            "Missing worker counts, changed inputs, or incompatible observations can prevent a comparison. A rank's CPU time does not represent all ranks.",
        ),
        (("Analyze scaling records", "scaling analyze ", True),
         ("Prepare scaling recipe", "scaling plan ", True)),
    ),
    "workflow": (
        "Workflow",
        (
            "Source: paths.workflow with a native raven.workflow recipe (version 1), or a planning bundle's workflow field.",
            "1. Give each node a unique ID and explicit dependencies. Include the real script/resources when preparing execution.",
            "2. Add measured or explicitly estimated runtime_seconds, or declared lower/estimate/upper duration scenarios. Document their source; omitted durations remain unknown.",
            "3. Analyze the recipe to inspect its DAG, supported critical path, makespan, slack, concurrency, and resource-hours. Review any proposed execution separately.",
            "Scenario bounds are not calibrated confidence intervals. Analysis assumes successful predecessors and immediate capacity; scheduler queue delays are excluded.",
            "For native Nextflow/Snakemake observations, select the Workflow engine operation and its supported source contract. A job name or row order is not a dependency edge.",
        ),
        (("Analyze workflow recipe", "workflow analyze ", True),
         ("Open workflow engine", "ops open workflow-engine", False)),
    ),
    "operations": (
        "Operations",
        (
            "Source: the selected operation's explicit form and native data contract.",
            "1. Open the catalog, search by task, and choose the tool. Its form lists required input files, options, and supported actions.",
            "2. Supply the actual evidence and select Inspect / prepare. Scientific acceptance needs results plus an acceptance policy; summaries alone do not invoke acceptance.",
            "Profiler inputs distinguish rank, phase, elapsed/CPU/MPI/I/O seconds and bytes. Energy inputs state joules, attribution boundary, coverage, and accepted work. Do not substitute a similarly named generic application metric.",
            "3. Inspect the result, coverage, and any capability requirement. Pin frequently used tools for later access.",
            "Execution may require Heavy, an exact job-local target, site permission, or a supported adapter, followed by its normal review. Heavy does not provide an unavailable instrument or grant access to someone else's job.",
            "There is no generic operations.json auto-loader or report research operations publisher. Each operation has its own input contract.",
        ),
        (("Browse operation forms", "ops catalog", False),),
    ),
}


_ANALYTICS = {
    "job": (
        "Job series",
        (
            "Sources: retained live scheduler samples and the selected job's existing GPU trace.",
            "1. Select the exact running job in Jobs. Open Metric sampling to check live CPU/RSS capability and coverage.",
            "2. Keep the job visible for demand-driven sampling. CPU per core needs measured CPU time and allocation size; memory needs observed RSS and a known request for a percentage.",
            "3. For GPU curves, publish Slurm WorkDir/logs/gpu-util-<job_id>.csv; array tasks use parent_task. A complete format example is:",
            GPU_EXAMPLE,
            "Columns are NVIDIA timestamp, device index, utilization percent, and optional used memory in MiB. Sample only allocated devices inside the actual job; keep the file after completion.",
            "4. Open Job series and inspect source age, observed span, gaps, and sample counts. A completed job needs retained observations; accounting totals cannot recreate missing curves.",
            "GPU CSV supplies this view. Application metrics.jsonl supplies Research Experiment. Moving the display window cannot collect older observations.",
        ),
        (("Inspect metric sampling", "telemetry", False),
         ("Choose GPU source", "gpuprovider", False)),
    ),
    "history": (
        "History",
        (
            "Source: Slurm accounting records within the selected history window. No application analytics.json is required.",
            "1. Check Accounting source health. The site must retain and permit access to your job history.",
            "2. Set the history window with Days, then open Analytics History. The initial accounting request and later refreshes obey the configured cadence and limits.",
            "3. Inspect daily outcomes, allocated core-hours/GPU-hours, partition usage, queue waits, and CPU efficiency. Missing timestamps or CPU totals exclude those rows from the corresponding statistic.",
            "Core-hours and GPU-hours describe allocated count times elapsed hours. GPU-hours are not measured utilization, energy, or a lab invoice. Expanding the window cannot recover data the accounting service no longer retains.",
        ),
        (("Choose history days", "days ", True),
         ("Inspect source health", "tab sources", False)),
    ),
    "timeline": (
        "Timeline",
        (
            "Sources: active and accounting jobs with actual submit, start, and end timestamps.",
            "1. Enable healthy queue/accounting sources and choose the history window.",
            "2. Open Timeline to distinguish waiting intervals from execution intervals. Pending jobs have no fabricated running interval.",
            "3. Select an exact job to inspect its evidence. Missing timestamps and gaps in accounting coverage limit the displayed span.",
            "Application phase events can be inspected through their own event controls; recording a phase metric does not change Slurm's allocation start or end.",
        ),
        (("Choose history days", "days ", True),
         ("Inspect source health", "tab sources", False)),
    ),
    "advisor": (
        "Advisor",
        (
            "Sources: historical jobs and current observations, grouped by job name.",
            "1. Collect comparable completed attempts with known requests and CPU/RSS observations. Choose an accounting window that includes those attempts.",
            "2. Open Advisor to compare observed memory/request, CPU efficiency, elapsed/requested walltime, suggested requests, idle core-hours, and flags.",
            "3. Check each suggestion against the actual workload and site constraints before changing a batch script. Requests include heuristic headroom.",
            "Equal job names do not prove scientific comparability. Use Research Resources with explicit code, parameters, and allocation identity when controlled cohorts matter. Missing memory scope or CPU coverage limits the advice.",
        ),
        (("Choose history days", "days ", True),
         ("Open explicit Resources", "workspace predict", False)),
    ),
    "compare": (
        "Compare",
        (
            "Source: retained native series for up to six exact selected jobs; accounting RSS can supply a memory fallback.",
            "1. Mark the intended jobs in Jobs or History, then choose Compare marked. Do not rely on a shared display name to identify a run.",
            "2. Inspect state, elapsed time, CPUs, observed CPU mean/max, peak memory, memory percentage, GPU mean, core-hours, and sample counts.",
            "3. Compare CPU/RSS/GPU curves with their coverage. Curves align to each job's first recorded sample, which may be later than its actual start.",
            "GPU CSV traces are displayed in Job series, but are not imported into this native comparison. Application metric streams also remain separate Research evidence.",
            "An absent curve may mean that no native samples were retained. Mean and maximum summarize only observed samples; they are not complete-run guarantees.",
        ),
        (("Compare marked jobs", "compare marked", False),
         ("Choose explicit job IDs", "compare ", True)),
    ),
}


def topic_keys():
    """Return stable topic identifiers without importing view implementations."""
    return (tuple(_GENERAL) + tuple("research." + key for key in _RESEARCH)
            + tuple("analytics." + key for key in _ANALYTICS))


def topic(key="overview"):
    """Return an independent title/lines/actions document for one setup page.

    An action is ``(label, command, prompt)``. ``prompt=True`` opens the command
    palette for the user's required path/identity; it never executes a partial
    command. Unknown topics fail explicitly rather than showing unrelated help.
    """
    if key in _GENERAL:
        title, lines, actions = _GENERAL[key]
    elif key.startswith("research.") and key[9:] in _RESEARCH:
        view = key[9:]
        label, lines, actions = _RESEARCH[view]
        title = "Research / " + label
        actions = (("Open " + label, "workspace " + view, False),
                   ("Connect data and validate a sample", "connectdata", False)) + actions
    elif key.startswith("analytics.") and key[10:] in _ANALYTICS:
        view = key[10:]
        label, lines, actions = _ANALYTICS[view]
        title = "Analytics / " + label
        actions = (("Open " + label, "view " + view, False),
                   ("Inspect independent recording readiness", "connectdata", False)) + actions
    else:
        raise ValueError("Unknown setup topic: " + str(key))
    return {"title": title, "lines": list(lines), "actions": list(actions)}
