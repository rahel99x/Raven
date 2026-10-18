# Reliability and research guides

Use the [operation catalog](../reference/operations.md) for exact commands and
fields. The guides below explain evidence formats, prerequisites, recovery and
qualification limits. An available form does not establish live-site or scientific
efficacy; each guide identifies its supported boundary.

## Connections, ownership and retained evidence

| Task | Guide |
| --- | --- |
| Understand configuration precedence and plugin trust | [Configuration trust](configuration-trust.md) |
| Reuse an authenticated SSH connection | [SSH connections](ssh-connections.md) |
| Inspect structured Slurm data and visible fallback | [Slurm readers](slurm-readers.md) |
| Retain recordings for the correct execution attempt | [Recording storage](recording-storage.md) |
| Query a bounded project evidence index | [Evidence queries](evidence-queries.md) |
| Inspect what was known at a historical observation time | [Historical observations](historical-observations.md) |
| Search logs or compare messages across ranks | [Log search](log-search.md) · [Rank comparison](rank-log-comparison.md) |
| Record artifact receipts and verify provenance | [Signed evidence](signed-evidence.md) |
| Read retained evidence from an MCP client | [Read-only MCP](mcp-evidence.md) |

## Measurements and scientific comparisons

| Task | Guide |
| --- | --- |
| Review bounded memory, scratch or same-host MPI preflight | [Allocation health](allocation-health.md) |
| Observe an existing owned process without claiming exact stacks | [Process observations](process-observations.md) |
| Inspect trace ranges and bounded retained summaries | [Trace summaries](trace-summaries.md) |
| Detect changes or compare declared measurement phases | [Signal analysis](signal-analysis.md) |
| Compare queue scenarios and model limitations | [Queue model](queue-model.md) · [Forecast scenarios](forecast-scenarios.md) |
| Review checkpoint assumptions and failure causes | [Checkpoint advice](checkpoint-advice.md) |
| Plan and analyze an explicit factorial experiment | [Factorial experiments](factorial-experiments.md) |
| Review and control a bounded campaign policy | [Campaign policy](campaign-policy.md) |

## Interface performance and optional services

| Task | Guide |
| --- | --- |
| Measure interface stalls and worker activity | [UI performance](ui-performance.md) · [Self-tracing](self-tracing.md) |
| Understand synchronized terminal updates | [Synchronized output](synchronized-output.md) |
| Try optional raster plots and fallback behavior | [Terminal raster](terminal-raster.md) |
| Reattach to an explicitly started session service | [Session service](session-service.md) |
| Evaluate shared-host collection and visibility policy | [Shared-host collector](shared-host-collector.md) |
| Run a compatible optional WASI analysis adapter | [WASI analysis](wasi-analysis.md) |
| Understand protocol and validation boundaries | [Protocol boundaries](protocol-boundaries.md) |

## Qualification and installation evidence

| Task | Guide |
| --- | --- |
| Run disposable real Slurm lifecycle checks | [Slurm qualification](slurm-qualification.md) |
| Verify tested release contents and provenance | [Release inventory](release-inventory.md) |
| Evaluate the unpublished core/lab extraction | [Distribution experiment](distribution-experiment.md) |
| Prepare the five established usability journeys | [Usability study](usability-study.md) |

The [implementation review ledger](../implementation/review-2026-10/coverage.json)
accounts for the accepted requirements and their evidence. Container scheduling,
local process measurements, synthetic scientific fixtures and human/site
qualification remain separate results.
