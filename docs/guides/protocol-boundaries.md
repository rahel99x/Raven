# Protocol and architecture boundaries

Raven's runtime remains dependency-free. Development tools validate the boundaries
that have been migrated; they do not imply that every existing module is typed or
that every published schema is a complete runtime policy.

## Framing and ownership

`raven.transport.framing` owns length framing, incremental buffering, and partial
writes. Existing imports from `raven.framing`, `raven.raven_protocol`,
`raven.raven_allocation`, and `raven.terminal_service` remain supported.

| Channel | Maximum frame | Receive queue | Document policy |
| --- | ---: | ---: | --- |
| Worker | 1 MiB | 8 MiB | Existing worker depth, string, record and version checks |
| Allocation | 1 MiB | 1 MiB + 65,540 bytes | Allocation-specific bounded documents and ownership fields |
| Terminal | 8 MiB | 8 MiB + 65,540 bytes | Up to 32 levels and 1,000,000 document nodes; bounded terminal grids |

The shared codec accepts a parser supplied by the channel. It does not interpret
actions, authorize cancellation, migrate evidence, or calculate receipt hashes.
Canonical evidence serialization remains in its existing domain module. Worker
peers still require a matching executable digest; wire compatibility does not
permit mixing unverified worker builds.

A decoded object is not an authorized request. Worker control envelopes use
`raven.protocol_contracts` and the generated
[`worker-control.v1.schema.json`](../schemas/worker-control.v1.schema.json).
Placement, task resources, operation policy, nonce, build, and ownership remain
separate checks. Additive descriptive fields belong in `extensions`; unknown
actions and outer action fields fail closed. Unknown major versions require an
explicit migration or a compatible installation. The current extraction changes
no persisted version and performs no implicit migration.

JSON Schema's mathematical integer type accepts `1.0`. The envelope contract
matches that structural definition; existing exact wire-type checks for handshake
versions remain stricter domain validation. Schema validation alone cannot prove
ownership, freshness, policy compliance, or allowed external effects.

## Incremental package migration

The dependency audit is reproducible:

```bash
python scripts/audit_architecture.py > architecture-audit.json
python scripts/check_quality.py
```

The audit reports static edges, conservative reachability, validator symbols,
schema-to-runtime literal references, and legacy annotation/line-length counts.
A literal reference identifies a review candidate, not proof that validation is
complete. Conditional imports are included, and dynamic plugin loading is outside
this static graph.

The first package seam is `raven.transport`: it depends only on the standard
library. Import-linter prevents it from depending on application owners. Six
existing core command handlers now dispatch through `command_registry`; the
controller still owns parsing, extension precedence, context and error feedback.
Mutation and review flows retain their existing handlers until separately
characterized. No second command registry is introduced.

Ruff and strict mypy cover the migrated transport modules and `command_handlers`.
The latter uses an explicit `Any` context pending a stable typed application
interface. The `partial` `py.typed` marker is shipped only in `raven.transport`;
it does not advertise the entire application as typed. Unmigrated files are not
reformatted to satisfy new boundary gates. Setuptools discovers `raven` and its
subpackages, and release checks must verify both wheel and sdist contents.

## Expression evaluation budgets

Expressions are read-only bounded calculations. Limits are 16,384 source
characters, 2,048 AST nodes, 100,000 evaluation steps, 100,000 collection items,
1,048,576 result-text characters and 4,096-bit integers. Nested comprehensions share one
budget. Expensive repetition, replacement and conversions are checked before
allocation; unsized iterators passed to native aggregates are rejected. String
percent formatting is excluded because its width can imply an unbounded
allocation. Budget exhaustion is a visible expression error and does not poison
subsequent evaluations. Reduce the selected data or calculate aggregates over a
bounded collection when a limit is reached.

## Validation limits

Property tests supplement the pre-existing every-boundary fragmentation tests.
Local subprocess tests exercise dropped acknowledgments, truncated writes,
SIGSTOP/SIGKILL, and cleanup. They do not qualify Slurm, GPUs, remote SSH, or
site-specific filesystem behavior. The bounded allocation ownership model under
[`docs/models/allocation`](../models/allocation/README.md) checks a finite
abstraction and includes a failing job-number-only negative control. It is not a
proof of all deployments or distributed liveness.
