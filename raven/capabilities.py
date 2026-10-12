"""Canonical ownership and traceability for the 56 overlapping proposals.

Lookup is immutable metadata only: it never imports adapters, collects data,
opens a menu, or schedules work. Operation associations preserve the original
proposal references without counting each appearance as a new implementation.
"""
from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType


@dataclass(frozen=True, slots=True)
class CapabilityArea:
    key: str
    title: str
    responsibility: str


@dataclass(frozen=True, slots=True)
class Proposal:
    id: str
    title: str
    area: str
    delivery: str
    operations: tuple[str, ...]
    scope: str
    boundary: str
    gates: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()
    related: tuple[str, ...] = ()


AREAS = (
    CapabilityArea("evidence-results", "Evidence and results", "Execution identity, provenance, source indexes, lineage, portable records, and reusable derived results."),
    CapabilityArea("collection-integrations", "Collection and integrations", "Collectors, tracing adapters, capability discovery, exact read identity, subscriptions, and source admission."),
    CapabilityArea("investigation", "Investigation", "Evidence-driven timelines, specialized analyzers, diagnostic planning, and checkable recommendations."),
    CapabilityArea("experiments", "Experiments", "Trial preparation, reviewed execution, repeated measurement, scientific checks, minimization, and simulation."),
    CapabilityArea("controlled-actions", "Controlled actions", "Exact-scope review, cooperative site policy, safe-point changes, receipts, and uncertain-outcome handling."),
    CapabilityArea("terminal-workspace", "Terminal workspace", "Commands, navigation, pane documents, gestures, charts, and accessible terminal controls."),
    CapabilityArea("execution-state", "Execution and state", "Bounded workers, immutable publication, latency accounting, durable settings, and reconciliation."),
    CapabilityArea("delivery-qualification", "Delivery and qualification", "Distribution, documentation, starter kits, support records, regression corpora, and independent qualification."),
)

# These IDs are stable references, not a count of distinct shipped features.
# V6 IDs follow the original ten named proposals in their ledger table order.
PROPOSALS = (
    Proposal(
        id='V5-TRACE',
        title='Prepared real-time stack tracing',
        area='collection-integrations',
        delivery='partial',
        operations=('trace-inspect', 'trace-capture', 'trace-snapshot'),
        scope='Prepared CPython monitoring/profile and native instrumentation, scoped PMPI, exact attempt/process journals, incremental readers, and reviewed local Python snapshots.',
        boundary='Not a universal lossless tracer: no GPU-device stacks, retrospective unprepared history, automatic global MPI launch, or per-event durable acknowledgement. Budget exhaustion is partial and crashes may leave uncommitted tails.',
        gates=('site',),
        aliases=('v5-trace', 'stack-tracing'),
        related=('F17', 'F30')),
    Proposal(
        id='V6-01',
        title='Causal explorer',
        area='investigation',
        delivery='supported',
        operations=('causal-delay',),
        scope='Declared trace dependency/phase relations, with evidence gaps preserved',
        boundary='Unsupported relations and missing evidence remain gaps; no whole-program proof of causality.',
        gates=('site',),
        aliases=('v6-causal-explorer',),
        related=('F22', 'V6-02')),
    Proposal(
        id='V6-02',
        title='Collective auditor',
        area='investigation',
        delivery='supported',
        operations=('collective-audit',),
        scope='Supported declared communicator/rank/sequence records; incomplete evidence remains incomplete',
        boundary='Missing ranks, unsupported callbacks and loss prevent a complete audit.',
        gates=('site',),
        aliases=('v6-collective-auditor',),
        related=('F22', 'V6-01')),
    Proposal(
        id='V6-03',
        title='Execution simulator',
        area='experiments',
        delivery='supported',
        operations=('execution-simulation',),
        scope='Declared fixed-workload DAG/resource model with calibration and held-out checks',
        boundary='The fixed-workload model requires calibration and held-out acceptance; arbitrary scheduler/program prediction is unsupported.',
        gates=('research',),
        aliases=('v6-execution-simulator',),
        related=('F25',)),
    Proposal(
        id='V6-04',
        title='Numerical robustness',
        area='experiments',
        delivery='supported',
        operations=('numerical-stability',),
        scope='Supplied repeated results and declared scientific tolerances',
        boundary='Scientific tolerances must be supplied; no universal numerical-error guarantee.',
        gates=('research',),
        aliases=('v6-numerical-robustness',),
        related=('F24',)),
    Proposal(
        id='V6-05',
        title='Minimal reproducer',
        area='experiments',
        delivery='supported',
        operations=('minimize-reproducer',),
        scope='Reviewed line-deletion trials with an explicit repeated failure oracle',
        boundary='Requires an explicit repeated failure oracle; no universal input/rank/language minimizer.',
        gates=('site',),
        aliases=('v6-minimal-reproducer',),
        related=('F23', 'V6-06')),
    Proposal(
        id='V6-06',
        title='Performance regression',
        area='experiments',
        delivery='supported',
        operations=('regression-investigation', 'regression-sweep'),
        scope='Imported measurements or reviewed immutable Git executions with repeated trials, uncertainty and scientific checks',
        boundary='Repeated measured trials preserve uncertainty; no automatic attribution to one changed line.',
        gates=('research',),
        aliases=('v6-performance-regression',),
        related=('F23', 'V6-05')),
    Proposal(
        id='V6-07',
        title='I/O coordination',
        area='controlled-actions',
        delivery='supported',
        operations=('io-coordination',),
        scope='Cooperative bounded permits at declared application safe points',
        boundary='Applications must declare safe points and handle deadlines and uncertain outcomes.',
        gates=('site',),
        aliases=('v6-i-o-coordination',),
        related=('F28', 'V6-08')),
    Proposal(
        id='V6-08',
        title='Live steering',
        area='controlled-actions',
        delivery='supported',
        operations=('runtime-steering',),
        scope='Application-declared parameters, safe points, epochs, deadlines and outcomes',
        boundary='Application-owned safe points and existing communication groups are required; partial commits are uncertain.',
        gates=('site',),
        aliases=('v6-live-steering',),
        related=('F28', 'V6-07')),
    Proposal(
        id='V6-09',
        title='Lineage impact',
        area='evidence-results',
        delivery='supported',
        operations=('scientific-impact',),
        scope='Conservative impact propagation and revalidation over declared lineage',
        boundary='Missing lineage stays uncertain; proposed revalidation does not prove a scientific claim.',
        gates=('research',),
        aliases=('v6-lineage-impact',),
        related=('F27', 'R3')),
    Proposal(
        id='V6-10',
        title='Observability planner',
        area='investigation',
        delivery='supported',
        operations=('investigation-plan',),
        scope='Budgeted selection among declared configuration, repeat, trace and validation actions',
        boundary='Deterministic greedy planning over supplied action models; not an optimal learned investigator.',
        gates=('research',),
        aliases=('v6-observability-planner',),
        related=('F26', 'R2')),
    Proposal(
        id='F01',
        title='Task workspaces',
        area='terminal-workspace',
        delivery='supported',
        operations=(),
        scope='Shared task descriptors filter the searchable operation catalog; durable pinned tools have keyboard/mouse controls and reopen with current job context.',
        boundary='**External gate:** newcomer task study; no measured usability improvement claimed.',
        gates=('external-users',),
        aliases=('01', 'flagship-01'),
        related=('F03', 'F35')),
    Proposal(
        id='F02',
        title='Context/navigation',
        area='terminal-workspace',
        delivery='supported',
        operations=(),
        scope='Back/Forward and scoped saved locations remain available. Cached breadcrumbs show known cluster, matched project/run attempt and declared source; changed scheduler attempts suppress stale bindings.',
        boundary='Session back stacks remain transient; narrow screens omit optional context before hiding the current page/job.',
        gates=(),
        aliases=('02', 'flagship-02'),
        related=()),
    Proposal(
        id='F03',
        title='Command model',
        area='terminal-workspace',
        delivery='partial',
        operations=(),
        scope='Shared command descriptors supply command names, usage, palette search/completion, toolbar action metadata, task controls and generated command help; installed operation specifications supply forms and field choices.',
        boundary='**Partial:** contextual input handlers, raw-key dispatch and specialist controls remain module-owned with their own validation; trusted plugins keep their separate dispatch contract.',
        gates=(),
        aliases=('03', 'flagship-03'),
        related=('F01', 'F35')),
    Proposal(
        id='F04',
        title='Project guide',
        area='collection-integrations',
        delivery='supported',
        operations=('project-guide',),
        scope="`:projectguide ROOT` / `project-guide` checks declaration coverage and one exact sample's metrics, summary, output contents and bounded logs through confined local/SSH readers; executable Bash hooks and reviewed descriptor initialization.",
        boundary='Sample coverage is bounded and source-specific; native Research analyses still establish scientific applicability.',
        gates=(),
        aliases=('04', 'flagship-04'),
        related=()),
    Proposal(
        id='F05',
        title='Investigation timeline',
        area='investigation',
        delivery='supported',
        operations=('investigation-timeline',),
        scope='`investigation-timeline` verifies the selected attempt and joins supported scheduler events, bounded logs and registered trace evidence; explicit timeline manifests remain supported.',
        boundary='Original text, source order and unknown clock domains remain distinct; no automatic universal log parser or cross-clock causal order.',
        gates=(),
        aliases=('05', 'flagship-05'),
        related=()),
    Proposal(
        id='F06',
        title='Semantic inspection',
        area='evidence-results',
        delivery='supported',
        operations=('scientific-field',),
        scope='Evidence records retain units, source and coverage; existing raw/trend/range chart distinctions; `scientific-field` provides bounded field queries.',
        boundary='Full arbitrary-volume scientific visualization remains outside the terminal core.',
        gates=(),
        aliases=('06', 'flagship-06'),
        related=('F15', 'R5')),
    Proposal(
        id='F07',
        title='Accessibility',
        area='terminal-workspace',
        delivery='supported',
        operations=(),
        scope='Existing keyboard traversal, ASCII, reader mode, reduced motion, focus, resizable panes and selection remain available.',
        boundary='**External gate:** real emulator, multiplexer, font and assistive-technology qualification.',
        gates=('site',),
        aliases=('07', 'flagship-07'),
        related=()),
    Proposal(
        id='F08',
        title='Gesture state',
        area='terminal-workspace',
        delivery='partial',
        operations=(),
        scope='One pointer lease owns motion/release across eleven control families in native and direct dispatch, with shared cancellation and fresh-gesture reset. Pane-scoped zoom retains the shared exact metric acquisition identity.',
        boundary='**Partial:** geometry, attempt checks, previews, rollback and validation remain in the existing per-control state machines; terminal byte decoding retains its established protocol.',
        gates=(),
        aliases=('08', 'flagship-08'),
        related=('F10',)),
    Proposal(
        id='F09',
        title='Immutable publication',
        area='execution-state',
        delivery='partial',
        operations=(),
        scope='Store-owned revisioned immutable metric publications and CPU/GPU histories feed shared native-series, resource-column, comparison and Advisor preparations. Supported nested corrections invalidate source revisions; publication and preparation retention have bounded shared limits.',
        boundary='**Partial:** unmanaged extension writes retain exact content checking or uncached fallback; other Store fields and full document composition are not universally incremental.',
        gates=(),
        aliases=('09', 'flagship-09'),
        related=('F10', 'F12')),
    Proposal(
        id='F10',
        title='Virtual documents',
        area='terminal-workspace',
        delivery='supported',
        operations=(),
        scope='Explicit pane contexts replace whole-App copies. Research and Analytics Details and native Job Series share document transforms for rows, controls, charts and scrollbars, with bounded visible-band rendering and first-publication, growth, resize and attempt-change handling.',
        boundary='Bounded documents may recompose to establish valid geometry after a first publication or scrolled resize. Other panes retain specialized renderers; no unbounded or universally incremental document promise.',
        gates=(),
        aliases=('10', 'flagship-10'),
        related=('F08', 'F09', 'F12')),
    Proposal(
        id='F11',
        title='Work admission',
        area='execution-state',
        delivery='supported',
        operations=(),
        scope='Nested cluster readers use the shared scheduler and cooperative child gathering; settings use one coalesced background writer.',
        boundary='Adapter/service processes retain their explicit resource contracts; not every external tool obeys Python worker counts.',
        gates=(),
        aliases=('11', 'flagship-11'),
        related=('F21', 'F30')),
    Proposal(
        id='F12',
        title='Latency budgets',
        area='execution-state',
        delivery='supported',
        operations=(),
        scope='Existing bounded UI timings, p95/p99, sustained-input and changing-UI harnesses; new snapshot comparison and qualification controls.',
        boundary='**External gate:** release-target performance on actual terminals and SSH; targets are not results.',
        gates=('site',),
        aliases=('12', 'flagship-12'),
        related=('F09', 'F10')),
    Proposal(
        id='F13',
        title='State/reconnect',
        area='execution-state',
        delivery='supported',
        operations=(),
        scope='Atomic versioned/checksummed UI state, last-good recovery, older-state compatibility, future-schema preservation and final background-save drain.',
        boundary='Filesystem/node failure can still lose uncommitted state; external transports have separate replay contracts.',
        gates=(),
        aliases=('13', 'flagship-13'),
        related=()),
    Proposal(
        id='F14',
        title='Incident reproduction',
        area='delivery-qualification',
        delivery='partial',
        operations=(),
        scope='Existing replay, input traces, source fixtures and sequential input regressions; bounded support evidence and qualification reports.',
        boundary='**Partial:** no deterministic recording of every OS scheduling event or arbitrary external application.',
        gates=(),
        aliases=('14', 'flagship-14'),
        related=('F32', 'F37')),
    Proposal(
        id='F15',
        title='Evidence records',
        area='evidence-results',
        delivery='supported',
        operations=('evidence',),
        scope='`evidence` reads versioned identity, provenance, clock, unit, coverage and integrity records.',
        boundary='A valid schema checks supplied evidence; it cannot prove a producer measured correctly.',
        gates=(),
        aliases=('15', 'flagship-15'),
        related=('F06', 'F26', 'R1', 'R5')),
    Proposal(
        id='F16',
        title='Capability manifests',
        area='collection-integrations',
        delivery='supported',
        operations=('adapter', 'site-policy'),
        scope='Adapter/source capabilities and site policy admission distinguish unsupported and unavailable observations.',
        boundary='Claims must be qualified against the installed runtime, build and site.',
        gates=(),
        aliases=('16', 'flagship-16'),
        related=('F17', 'F18', 'F38', 'R4')),
    Proposal(
        id='F17',
        title='Instrumentation federation',
        area='collection-integrations',
        delivery='partial',
        operations=('diagnostic-recipe',),
        scope='`diagnostic-recipe` checks explicit tool capabilities and conflicts. `raven trace mpi --source` emits a scoped C PMPI wrapper for supported world-communicator collectives.',
        boundary='**Partial:** no universal Caliper/OMPT/Kokkos/GPU injector. PMPI supports its declared C signatures/rank limit; unsupported operations produce gaps.',
        gates=(),
        aliases=('17', 'flagship-17'),
        related=('F16', 'F30', 'R4', 'V5-TRACE')),
    Proposal(
        id='F18',
        title='Adapter SDK',
        area='collection-integrations',
        delivery='supported',
        operations=('adapter',),
        scope='`adapter` inspects pinned external manifests; reviewed describe/validate/run use bounded NDJSON, deadlines and process-group cleanup.',
        boundary='Installed code remains trusted; isolation is not a security sandbox.',
        gates=(),
        aliases=('18', 'flagship-18'),
        related=('F16', 'F38')),
    Proposal(
        id='F19',
        title='Site telemetry',
        area='collection-integrations',
        delivery='supported',
        operations=('site-telemetry',),
        scope='`site-telemetry` inspects or explicitly queries Prometheus range data with exact cluster/job/attempt labels, bounded HTTP responses and optional site policy/shared cache.',
        boundary='**External gate:** target-site authentication, deployment and label mappings; no blanket Jobstats, ClusterCockpit, LLview or slurmrestd client certification.',
        gates=('site',),
        aliases=('19', 'flagship-19'),
        related=('F21',)),
    Proposal(
        id='F20',
        title='Portable records',
        area='evidence-results',
        delivery='supported',
        operations=('research-portable',),
        scope='`research-portable` imports supported Perfetto JSON, Caliper JSON, OTLP JSON spans and literal-key Workflow Run RO-Crate; reviewed bundle export.',
        boundary='Binary Perfetto/protobuf, arbitrary Caliper formats and general JSON-LD expansion are not implied.',
        gates=(),
        aliases=('20', 'flagship-20'),
        related=('F27',)),
    Proposal(
        id='F21',
        title='Shared queries/API',
        area='collection-integrations',
        delivery='supported',
        operations=(),
        scope='Versioned structured commands and a bounded subscription/admission broker with exact request identity and freshness.',
        boundary='A user-scoped broker is not an institution-wide authenticated monitoring service.',
        gates=(),
        aliases=('21', 'flagship-21'),
        related=('F11', 'F19', 'F30')),
    Proposal(
        id='F22',
        title='Causal/protocol diagnosis',
        area='investigation',
        delivery='partial',
        operations=('causal-delay', 'collective-audit'),
        scope='`causal-delay` and `collective-audit` analyze declared trace relations and communicator evidence.',
        boundary='**Partial:** abstain on loss, missing ranks or unsupported callbacks; no whole-program proof of causality.',
        gates=(),
        aliases=('22', 'flagship-22'),
        related=('V6-01', 'V6-02')),
    Proposal(
        id='F23',
        title='Reproducers/regressions',
        area='experiments',
        delivery='supported',
        operations=('diagnostic-campaign', 'minimize-reproducer', 'regression-sweep', 'regression-investigation'),
        scope='Reviewed `diagnostic-campaign`, `minimize-reproducer` and `regression-sweep`; immutable Git snapshots run measured repeats with scientific checks; `regression-investigation` also evaluates supplied measurements.',
        boundary='Line-deletion minimization needs an explicit repeated failure oracle; no universal input/rank/language minimizer or automatic attribution to one changed line.',
        gates=(),
        aliases=('23', 'flagship-23'),
        related=('V6-05', 'V6-06')),
    Proposal(
        id='F24',
        title='Scientific stability',
        area='experiments',
        delivery='supported',
        operations=('numerical-stability', 'acceptance'),
        scope='`numerical-stability`, declared scientific checks, existing acceptance and measured numerical demo.',
        boundary='No invented scientific threshold; no universal numerical-error bound or automatic perturbation of arbitrary binaries.',
        gates=(),
        aliases=('24', 'flagship-24'),
        related=('V6-04',)),
    Proposal(
        id='F25',
        title='Counterfactual experiments',
        area='experiments',
        delivery='supported',
        operations=('execution-simulation',),
        scope='`execution-simulation` checks declared calibration and held-out acceptance, withdraws failed predictions and can publish reviewed immutable model/scenario/prediction records.',
        boundary='**Research gate:** limited fixed-workload model; missing policy/evidence stays unqualified. No arbitrary-program or scheduler prediction.',
        gates=('research',),
        aliases=('25', 'flagship-25'),
        related=('V6-03',)),
    Proposal(
        id='F26',
        title='Investigation planning',
        area='investigation',
        delivery='supported',
        operations=('investigation-plan',),
        scope='`investigation-plan` chooses declared information actions under explicit cost/risk budgets.',
        boundary='Deterministic greedy planning over supplied models; not a learned optimal Bayesian investigator.',
        gates=(),
        aliases=('26', 'flagship-26'),
        related=('F15', 'R1', 'R2', 'V6-10')),
    Proposal(
        id='F27',
        title='Claim impact',
        area='evidence-results',
        delivery='supported',
        operations=('scientific-impact',),
        scope='`scientific-impact` propagates declared lineage changes and prepares conservative revalidation checks.',
        boundary='Missing lineage remains uncertain; proposed checks do not prove a scientific claim.',
        gates=(),
        aliases=('27', 'flagship-27'),
        related=('F20', 'R3', 'V6-09')),
    Proposal(
        id='F28',
        title='Cooperative controls',
        area='controlled-actions',
        delivery='supported',
        operations=('runtime-steering', 'io-coordination', 'scientific-field'),
        scope='`runtime-steering`, `io-coordination`, `scientific-field`, and `raven runtime` expose safe points and bounded summaries. `CollectiveRuntime` uses application-owned gather/broadcast for shared validation, commit epochs and outcomes.',
        boundary='Private leader-local mailbox; existing MPI/PyTorch process group is required. Partial commits become uncertain, not rolled back or automatically retried.',
        gates=(),
        aliases=('28', 'flagship-28'),
        related=('V6-07', 'V6-08')),
    Proposal(
        id='F29',
        title='Site policy',
        area='controlled-actions',
        delivery='supported',
        operations=('site-policy',),
        scope='`site-policy` evaluates explicit allowlists and collection limits; deployment guide states responsibilities.',
        boundary='Cooperative policy is not OS-enforced privilege isolation.',
        gates=(),
        aliases=('29', 'flagship-29'),
        related=('F31',)),
    Proposal(
        id='F30',
        title='Observability accounting',
        area='collection-integrations',
        delivery='supported',
        operations=(),
        scope='Request admission exposes source limits, delivery capability, duration/storage/resource budgets and refusal reasons.',
        boundary='Estimated cost requires target-system measurement; strict completeness is never inferred from interval alone.',
        gates=(),
        aliases=('30', 'flagship-30'),
        related=('F11', 'F17', 'F21', 'V5-TRACE')),
    Proposal(
        id='F31',
        title='Action lifecycle',
        area='controlled-actions',
        delivery='supported',
        operations=(),
        scope='Existing reviewed plans plus new adapters use exact scope, content/identity revalidation and explicit outcomes.',
        boundary='Multi-step scheduler/filesystem effects are not a distributed transaction; uncertain outcomes require reconciliation.',
        gates=(),
        aliases=('31', 'flagship-31'),
        related=('F29',)),
    Proposal(
        id='F32',
        title='Support records',
        area='delivery-qualification',
        delivery='partial',
        operations=('support-bundle',),
        scope='`support-bundle` prepares selected structured, redacted reports in a new reviewed directory.',
        boundary='**Partial:** no unauthenticated shared session, universal redactor or automatic upload. Review before sharing.',
        gates=(),
        aliases=('32', 'flagship-32'),
        related=('F14', 'F37')),
    Proposal(
        id='F33',
        title='Distribution',
        area='delivery-qualification',
        delivery='supported',
        operations=(),
        scope='Offline setup, clean-install checks, per-artifact/member SHA256 inventory, core license/dependency/build metadata and minimal SPDX 2.3 record with verification.',
        boundary='Hashes detect changes, not publisher authenticity. Signing and separately installed external runtime/tool inventories remain release/site responsibilities.',
        gates=(),
        aliases=('33', 'flagship-33'),
        related=('F34', 'F36')),
    Proposal(
        id='F34',
        title='Qualification kit',
        area='delivery-qualification',
        delivery='supported',
        operations=('qualification',),
        scope='`scripts/qualify_flagship.py` checks docs, CLI, ten pages, real PTY and optional wheel; explicit existing-allocation manifest adds one read-only identity query.',
        boundary='**External gate:** real GPU, multi-node, production SSH and site policy tests remain explicit untested entries.',
        gates=('site',),
        aliases=('34', 'flagship-34'),
        related=('F33', 'F36')),
    Proposal(
        id='F35',
        title='Task docs',
        area='delivery-qualification',
        delivery='supported',
        operations=(),
        scope='Short task-based README, runbook, specialized guides, administrator/support/qualification documentation, this ledger and generated operation fields/defaults.',
        boundary='Schema and control changes require reference regeneration and compatibility review.',
        gates=(),
        aliases=('35', 'flagship-35'),
        related=('F01', 'F03')),
    Proposal(
        id='F36',
        title='Starter kits',
        area='delivery-qualification',
        delivery='supported',
        operations=(),
        scope='Bash/Python reporting, numerical/Git-regression demos, Python/native and scoped PMPI trace launch, optional MPI reduction and PyTorch training controls. Two local Open MPI ranks were exercised for the supported trace wrapper.',
        boundary='**External gate:** multi-node/PyTorch/CUDA/NCCL and site qualification; CUDA metric tests use fake events and do not certify hardware sampling.',
        gates=('site',),
        aliases=('36', 'flagship-36'),
        related=('F33', 'F34')),
    Proposal(
        id='F37',
        title='Reliability corpus',
        area='delivery-qualification',
        delivery='supported',
        operations=(),
        scope='Existing public source/input/performance fixtures plus persistence, nested-budget, partial trace, adapter and diagnostic-process regressions.',
        boundary='**External gate:** publish target-system tail latency, memory/RPC cost and real workload outcomes.',
        gates=('site',),
        aliases=('37', 'flagship-37'),
        related=('F14', 'F32')),
    Proposal(
        id='F38',
        title='Support policy',
        area='delivery-qualification',
        delivery='supported',
        operations=(),
        scope='Documented schema/API boundaries, deprecation/fixture expectations and separate trusted-plugin contract.',
        boundary='Maintainer capacity and long-term compatibility depend on actual maintained releases.',
        gates=('maintenance',),
        aliases=('38', 'flagship-38'),
        related=('F16', 'F18')),
    Proposal(
        id='F39',
        title='External adoption',
        area='delivery-qualification',
        delivery='procedure',
        operations=('qualification',),
        scope='Qualification procedure and five-user-journey evaluation protocol are provided.',
        boundary='**External gate:** independent labs/operators have not been recruited or evaluated by this implementation.',
        gates=('external-users',),
        aliases=('39', 'flagship-39'),
        related=('F40',)),
    Proposal(
        id='F40',
        title='Outcome evaluation',
        area='delivery-qualification',
        delivery='procedure',
        operations=(),
        scope='Research hypotheses retain explicit baselines, assumptions, negative-result and evaluation requirements in the dossier.',
        boundary='**External gate:** no scientific study, superiority result or world-first claim has been established.',
        gates=('research',),
        aliases=('40', 'flagship-40'),
        related=('F39',)),
    Proposal(
        id='R1',
        title='Checkable, expiring recommendations',
        area='investigation',
        delivery='supported',
        operations=('recommendation',),
        scope='`recommendation` / `raven.recommendation-review/v1` checks supplied identity, evidence, applicability, expiration and acceptance.',
        boundary='Compare incorrect endorsement and resource cost with expert/rule baselines; a predicate checker is not a proof of universal validity.',
        gates=('research',),
        aliases=('research-1',),
        related=('F15', 'F26')),
    Proposal(
        id='R2',
        title='Choose the next information action',
        area='investigation',
        delivery='supported',
        operations=('investigation-plan',),
        scope='`investigation-plan` / `raven.investigation-plan/v1` ranks declared actions within budgets.',
        boundary='Evaluate information utility, calibration, noise and regret. Current planner is deterministic greedy, not optimal learning.',
        gates=('research',),
        aliases=('research-2',),
        related=('F26', 'V6-10')),
    Proposal(
        id='R3',
        title='Conservative revalidation',
        area='evidence-results',
        delivery='supported',
        operations=('scientific-impact',),
        scope='`scientific-impact` / `raven.scientific-lineage/v1` traverses declared dependencies and proposes checks.',
        boundary='Compare against full recomputation with seeded parser/input/policy changes; no unjustified minimality claim.',
        gates=('research',),
        aliases=('research-3',),
        related=('F27', 'V6-09')),
    Proposal(
        id='R4',
        title='Compile diagnostic intent',
        area='investigation',
        delivery='supported',
        operations=('diagnostic-recipe',),
        scope='`diagnostic-recipe` checks `raven.diagnostic-intent/v1` against supplied site capabilities.',
        boundary='Execute reviewed recipes on independently configured sites; portability of JSON alone is not diagnostic equivalence.',
        gates=('research',),
        aliases=('research-4',),
        related=('F16', 'F17')),
    Proposal(
        id='R5',
        title='Query-preserving summaries',
        area='evidence-results',
        delivery='supported',
        operations=('information-query',),
        scope='`information-query` checks supported declared summaries and answers against their contract.',
        boundary='Validate query families against complete reference data and rare events; cannot answer arbitrary discarded-history queries.',
        gates=('research',),
        aliases=('research-5',),
        related=('F06', 'F15')),
)

EXPECTED_IDS = frozenset(("V5-TRACE",) + tuple(f"V6-{index:02}" for index in range(1, 11))
                         + tuple(f"F{index:02}" for index in range(1, 41))
                         + tuple(f"R{index}" for index in range(1, 6)))


def _reference(value):
    if not isinstance(value, str):
        raise ValueError("A capability reference must be text.")
    return value.strip().casefold()


def validate(*, proposals=PROPOSALS, areas=AREAS, operation_keys=None):
    """Return deterministic catalog errors; adapter imports are opt-in here."""
    if operation_keys is None:
        from .operations import catalog
        operation_keys = {item["key"] for item in catalog()}
    else:
        operation_keys = set(operation_keys)
    errors, aliases = [], {}
    ids = [item.id for item in proposals]
    if len(ids) != len(set(ids)):
        errors.append("Duplicate proposal ID")
    if set(ids) != EXPECTED_IDS:
        errors.append("Proposal IDs must contain exactly the 56 original entries")
    area_keys = [item.key for item in areas]
    if len(area_keys) != 8 or len(set(area_keys)) != 8:
        errors.append("Exactly eight distinct owning areas are required")
    for item in proposals:
        if item.area not in area_keys:
            errors.append(item.id + ": unknown owning area " + item.area)
        if item.delivery not in ("supported", "partial", "procedure"):
            errors.append(item.id + ": invalid delivery status")
        if not item.scope or not item.boundary:
            errors.append(item.id + ": scope and remaining boundary are required")
        if any(gate not in ("site", "research", "external-users", "maintenance") for gate in item.gates):
            errors.append(item.id + ": unknown qualification gate")
        if len(set(item.operations)) != len(item.operations):
            errors.append(item.id + ": duplicate operation association")
        for operation in item.operations:
            if operation not in operation_keys:
                errors.append(item.id + ": unknown operation " + operation)
        if item.id in item.related or len(set(item.related)) != len(item.related):
            errors.append(item.id + ": invalid repeated/self overlap reference")
        for related in item.related:
            if related not in ids:
                errors.append(item.id + ": unknown overlap reference " + related)
        for reference in (item.id,) + item.aliases:
            key = _reference(reference)
            if not key or key in aliases and aliases[key] != item.id:
                errors.append(item.id + ": ambiguous alias " + reference)
            aliases[key] = item.id
    if {item.area for item in proposals} != set(area_keys):
        errors.append("Every owning area must own at least one proposal")
    return tuple(errors)


_BY_REFERENCE = MappingProxyType({_reference(reference): item for item in PROPOSALS
                                 for reference in (item.id,) + item.aliases})
_BY_AREA = MappingProxyType({area.key: tuple(item for item in PROPOSALS if item.area == area.key)
                            for area in AREAS})
_BY_OPERATION = MappingProxyType({operation: tuple(item for item in PROPOSALS if operation in item.operations)
                                 for operation in sorted({key for item in PROPOSALS for key in item.operations})})


def lookup(reference: str) -> Proposal:
    """Resolve a stable original ID or documented alias, without adapter work."""
    try:
        return _BY_REFERENCE[_reference(reference)]
    except KeyError as exc:
        raise ValueError("Unknown capability reference: " + reference) from exc


def for_area(key: str) -> tuple[Proposal, ...]:
    """Return each proposal owned by one area exactly once."""
    try:
        return _BY_AREA[_reference(key)]
    except KeyError as exc:
        raise ValueError("Unknown capability area: " + key) from exc


def for_operation(key: str) -> tuple[Proposal, ...]:
    """Return proposal associations; repeated use is not another operation."""
    return _BY_OPERATION.get(_reference(key), ())
