# Documentation style

This project uses controlled technical English guided by ASD-STE100 principles.
The objective is clear, consistent operating instructions.
This document defines the project's writing conventions.
It is not a declaration of formal ASD-STE100 conformity.
A formal conformity assessment requires the applicable standard and dictionary review.

## Use consistent technical names

Use the same name for the same item throughout a procedure.
Preserve identifiers that the user must type.
Treat commands, file paths, key names, Slurm terms, and metric identifiers as technical names.

| Term | Meaning |
| --- | --- |
| Raven | The application; `slurm-tower` is the distribution and `tower` is its import namespace and compatibility command |
| Page | A main application view, such as Jobs or History |
| Workspace | A Research subview, such as Experiment or Workflow |
| Panel | A separately focused area within a page |
| Overlay | A temporary interactive view above a page |
| Command palette | The command entry interface opened with `:` |
| Source | A scheduler measurement source or a declared log file; specify which |
| Run | One project execution attempt identified by `run_id` |
| Job | A scheduler record identified by its actual Slurm job ID |
| Artifact | A result file declared in an output contract |
| Sample | One recorded measurement at a stated time |
| Retained window | The content currently held in a bounded in-memory buffer |
| Source position | A location in the underlying file rather than the displayed page |
| Unknown | A value that the available evidence does not establish |
| Follow | Keep the log display at newly appended content |
| Mark | Select a job for a later group action |
| Line selection | Select original log lines for copying |

Use `Logs` for the page name in prose.
Use `log` for the command-line page identifier.
Use `Dependencies` in explanatory prose.
Use `deps` for the command-line identifier.

## Write direct sentences

1. Use active voice when an actor is known.
2. Use a direct command for each instruction.
3. Give one instruction in each sentence.
4. Prefer short sentences.
5. Put a condition before the instruction that depends on it.
6. Use specific nouns instead of ambiguous pronouns.
7. State the result separately from the action.
8. Keep sentences in paragraphs when they explain one subject.

Use numbered steps for procedures.
Use tables for controls, mappings, and comparisons.
Use lists for parallel items.
Avoid decorative claims and unexplained adjectives.

| Avoid | Use |
| --- | --- |
| The configuration should then be loaded by the user. | Open the configuration file. |
| Simply press the appropriate shortcut. | Press `Ctrl-B`. |
| This seamlessly restores everything. | Raven restores the selected job, filter, and scroll position. |
| The metric is bad. | The sample is unavailable. Inspect Sources for the read error. |
| Copy the log. | Press `Y` to copy the entire selected log file. |

## Structure each guide

Provide these elements where they apply:

1. Purpose.
2. Prerequisites.
3. Controls and command syntax.
4. Numbered operating procedure.
5. Expected result.
6. Limits and evidence scope.
7. Troubleshooting.
8. Links to related guides.

Put the prerequisite before the procedure.
Put the expected result after the procedure.
State a recovery action for a known failure.
Do not mix installation instructions with unrelated implementation details.

## Specify controls precisely

Put literal commands, paths, and keys in code formatting.
Use uppercase placeholders for values that the user supplies.
Explain placeholder units and valid ranges.
State whether an index starts at zero or one.
State whether a date or timestamp uses local time or UTC.
State whether an action changes a preference, a local file, or a scheduler job.

A leading `:` means that the user must open the command palette.
The user types the command text without that leading character.
Command examples in a terminal shell must use the `tower` launcher or another explicit program.
Do not place palette commands in a Bash code block.

Describe uppercase and lowercase keys separately.
Describe an overlay's keys in that overlay's guide.
Do not imply that a contextual key has the same function everywhere.

## Report data and limits accurately

Distinguish a displayed value from its underlying measurement.
Distinguish a retained log window from the complete file.
Distinguish observed completion from confirmed terminal accounting state.
Distinguish script preparation from scheduler submission.
Distinguish an unavailable value from a measured zero.

State coverage when an operation is bounded.
Name omitted sources when the interface reports them.
Do not convert unknown values into zero for an example.
Do not claim that a chart proves an unobserved event.

Document implemented behavior.
Do not present an intention, prototype, or stub as a completed feature.
Match examples to actual command parsing and help text. Use Raven consistently
in current product prose. Keep historical release names, literal command and
module identifiers, and published evidence-schema identifiers intact.

For research and evaluation documents, distinguish implementation, measured
results, and hypotheses. State the workload, runtime, baseline, sample scope,
and limitations needed to reproduce a result. Cite existing records rather
than inventing paper titles, venue acceptance, external evaluations, or novelty
claims. Keep documentation suitable for review as a software artifact.

## Review documentation changes

1. Check every local Markdown link.
2. Check links to section headings.
3. Compare key names with the configured default bindings.
4. Compare command names with the command registry.
5. Compare CLI options with `tower --help`.
6. Run examples against simulated data where they do not change real jobs.
7. Check that each procedure identifies its expected result.
8. Check that source identity, limits, and unknown states are described correctly.

Do not claim formal ASD-STE100 approval from this review alone.
