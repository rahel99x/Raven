# Diagnose Raven and inspect recorded execution

## Raven's own timeline

Start an interactive session with an explicit new report filename:

```bash
ravenh --ui-trace raven-timeline.json
```

Reproduce the delay, then quit normally. Open the resulting JSON file in
[Perfetto](https://ui.perfetto.dev/) with **Open trace file**. The same document
retains Raven's `phases`, `slowest`, and `transitions` summaries for existing
scripts. No extra Python dependency is required to record or export it.

The timeline contains nested UI phases and local sampler/background-worker
spans. Flow arrows connect task submission to execution, with separate
cancellation and rejection outcomes. Process and native-thread identifiers
separate concurrent work. All timestamps use one process-local monotonic clock.
A long `input_wait` is ordinarily idle time; a long `paint`, `compose`, or
`terminal_flush` identifies work or waiting in that instrumented phase. Wall
time and the executing thread's CPU time are separate measurements.

Capture is off by default. The recorder keeps at most 8,192 timeline events,
2,048 duration samples per phase, 64 slow events, and 128 page transitions.
`timeline.dropped_events` reports overwritten events; `pending_tasks` reports
work unfinished at capture end. Either makes `timeline.complete` false. It is
possible for the ring to retain a child after its parent was overwritten.
Totals in the phase summary cover the session; its percentiles cover only the
retained duration window.

The report is a new owner-readable/writable file. Report bytes are written only
after the interactive session exits, never during input handling or paint.
A forced process kill can leave the reserved file empty. The timeline omits
job IDs, command arguments, typed text, logs, and source paths. It does not
capture remote Heavy internals or reconstruct CPU call stacks. Opening a file
in a third-party viewer is your choice; inspect any diagnostic before sharing.

## Application trace exports

For an existing [recorded application trace](stack-tracing.md), export its
verified committed prefix without starting or changing the application:

```bash
raven trace export /project/.raven/traces/STREAM/manifest.json \
  --format perfetto --output application-timeline.json

raven trace export /project/.raven/traces/STREAM/manifest.json \
  --format speedscope --output application-stacks.json
```

Perfetto includes matched Python/cooperative intervals and observed events.
[Speedscope](https://www.speedscope.app/) exports are **evented wall-time
profiles**, available for Python frame recordings. They are not sampled CPU
profiles. Generator yield/resume intervals represent observed physical-thread
execution, not a complete logical async task. Legacy profile limitations remain
in the export; neither exporter adds missing history or fabricates closing
frames. Unmatched frame boundaries are counted in
`raven.unpaired_frame_events`. Cooperative/native-only traces use Perfetto.

Each export retains exact attempt/process identity, declared coverage, source
status, read/commit sequence, and the verified-prefix digest. Open recordings
and bounded reads remain explicitly partial. A failed integrity check refuses
export. Raw journals remain unchanged and authoritative.

The interface-host command has the same read limits as trace inspection:
512 events and 256 KiB in a managed session. An unmanaged offline invocation
reads at most 20,000 events and 8 MiB. The output is bounded and published as a
new private file; an existing destination is never overwritten. To inspect a
larger journal incrementally, use Raven's existing trace view. Application
exports can contain recorded function names, paths, and application-supplied
metadata; the privacy restriction on Raven's self-timeline does not redact
scientific evidence automatically.
