# Platform evidence and site integration examples

Use these examples to inspect portable evidence, collection policy, telemetry,
adapter contracts, and support bundles. Run the commands from the repository
root in an installed Raven session. Inspecting the supplied fixtures does not
contact a cluster. A telemetry query or reviewed adapter run performs the
explicit operation described below.

These fixtures also document native adapter contracts. In managed Raven,
file-based analysis requires a supported admitted worker. Live telemetry Query
and external adapter Apply remain deferred until their governed transports are
implemented; Heavy admission does not enable them. See the
[execution contracts](../../docs/raven/execution-contracts.md).

Replace the example cluster, job ID, execution attempt, and paths before
using them for a real job. An attempt is the exact submit/start identity, not
only the reusable Slurm job number.

## Inspect and exchange evidence

Open **File → Research and cluster operations → Platform**, or use these Raven
palette commands:

```text
:ops run evidence path=examples/platform/evidence.json
:ops run qualification manifest=examples/platform/qualification.json
:ops research-portable
```

The qualification manifest pins four local input files by SHA256. Its checks
validate real format imports. It does not certify a live Slurm installation,
GPU, filesystem or terminal. If you edit a fixture, update its expected hash
only after reviewing the change.

In **Portable research evidence**, set the source format, source path, exact
cluster, job ID and attempt. Set an export format and a **new** destination
directory. Inspect the result, review the action, and apply it. Raven verifies
that the source did not change and writes a private bundle containing
`tower-evidence.json` plus the selected export. Existing destinations are
refused. Original external artifacts are referenced; they are not copied or
fetched automatically.

| Format | Supported input | Boundary |
| --- | --- | --- |
| `tower-evidence` | `tower.evidence/v1` | Unknown coverage/loss stays unknown; a digest checks integrity, not truth |
| `perfetto-json` | Chrome/Perfetto `traceEvents` JSON | Duration, counter and flow observations; other phases retained without invented meaning; no binary protobuf importer |
| `caliper-json-records` | Caliper `FORMAT json` records or object layout | Original records/attributes retained; no `.cali` or `json-split` parser; units are not guessed |
| `workflow-run-ro-crate` | Literal-key `@context`/`@graph`, root, metadata and CreateAction entities | No network JSON-LD expansion or full profile certification |
| `otlp-json-traces` | `resourceSpans` → `scopeSpans` → spans | Exact integer nanoseconds and span IDs; not an OTLP listener, profiler importer or lossless call recorder |

Native re-export requires the retained source content to match its digest.
Conversion of another evidence format to RO-Crate produces process-run metadata
and an accompanying evidence file. It does not invent the original application
inputs, outputs or scientific validity.

Formal contracts are available in [evidence.schema.json](../../docs/schemas/evidence.schema.json),
[site-policy.schema.json](../../docs/schemas/site-policy.schema.json),
[adapter.schema.json](../../docs/schemas/adapter.schema.json), and
[prometheus-query.schema.json](../../docs/schemas/prometheus-query.schema.json).
Runtime validation also checks cross-field constraints and document bounds.

## Configure a site collection policy

Copy and review [site-policy.json](site-policy.json). In the Raven configuration:

```toml
site_policy = "/absolute/path/to/site-policy.json"
collection_cache_dir = "/private/host-local/path/tower-collection"
```

The cache directory must be owned by the current user with mode `0700`, on a
host-local filesystem. Symbolic-link paths and known network filesystems
(including NFS, Lustre and GPFS) are refused. The same host/user's
Raven sessions can share scheduler read results. The cache binds scope, query,
capabilities, policy revision and host boot identity. A different host or boot
requires a fresh directory. Without `collection_cache_dir`, Raven selects a
host/boot-specific path under its state directory; verify that the state storage
is local before enabling this option.

The policy is opt-in. With no `site_policy`, existing scheduler behavior is
unchanged. It governs a conservative native read subset: `squeue`, `sacct`,
`sinfo`, `sstat`, and supported read-only `scontrol` commands. Mutation commands
are not cached. Reviewed actions obtain fresh observations and retain their
command cancellation/output limits. A configured policy can reject an operation
or interval; it never grants permissions that Slurm denies.

The broker coalesces matching readers, reserves bounded bytes and in-flight
work, and recovers leases whose process identity is no longer alive. A cancelled
waiter does not cancel another process's collection. An inaccessible/live owner
is not treated as dead; an abandoned live loader must be cancelled or its scope
invalidated. The in-process broker borrows the existing worker scheduler. The
cross-process cache executes on the caller's worker; it starts no extra pool.

Use `:ops site-policy` to inspect a policy, explicit source capabilities and
collection request without starting a collector. The provided `capabilities.json`
and `collection-request.json` can be inspected with `site-policy.json`:

```text
:ops run site-policy policy=examples/platform/site-policy.json capabilities=examples/platform/capabilities.json request=examples/platform/collection-request.json
```
These contracts describe configuration and observed capabilities. They do not
establish administrator authorization.

## Read an existing Prometheus site service

Edit [prometheus-query.json](prometheus-query.json). Set an explicit site-approved
HTTPS endpoint and PromQL expression. Every returned series must contain the
configured labels for the exact cluster, job ID and execution attempt. Add these
labels at the exporter/recording-rule layer if your service lacks them; Raven
does not infer attempts from a reused job number.

```text
:ops run site-telemetry manifest=/absolute/path/query.json action=inspect
:ops run site-telemetry manifest=/absolute/path/query.json action=query
```

Inspect performs no HTTP request. The native Query contract fetches the declared
range from the Raven host through a local connection context. Managed Raven
currently defers Query pending a governed network transport. Remote SSH contexts cannot
silently substitute local credentials. Metric names, units and gauge/counter/rate
semantics are explicit. `NaN` and infinite samples remain missing. The evaluation
step does not establish the exporter's real sampling interval.

For authentication, set one of these manifest entries:

```json
{"auth": {"kind": "bearer_env", "source": "SITE_PROMETHEUS_TOKEN"}}
```

```json
{"auth": {"kind": "bearer_file", "source": "/private/path/token"}}
```

A credential file must be owned by the current user, have no group/other
permissions, contain at most 16 KiB, and have no symbolic-link path. `ca_file`
can name the site's trusted CA file. Credentials are loaded only for the query;
the receipt contains neither token nor authorization header. Redirects are
refused, ambient proxy configuration is not inherited, and bearer credentials
require HTTPS. Plain HTTP requires explicit `allow_http: true` and cannot use
bearer authentication.

Queries have a server timeout parameter, client deadline, response-byte bound,
and a maximum of 20,000 normalized samples. A small trusted standard-library
HTTP process lets the existing worker enforce a wall-clock deadline even if DNS
or TLS stalls; credentials travel through its private stdin, not command-line
arguments. The process is reaped on cancellation or completion. The default example also declares
site admission limits. An optional `cache_directory` enables the same private
shared cache for identical telemetry queries and requires an inline policy.
Expensive PromQL expressions still require site-side query controls; a bounded
client cannot certify arbitrary server-side resource cost.

## Install and inspect an optional adapter

The standard-library-only core runs installed adapters in separate processes.
Dependency isolation is not a security sandbox. Install only code you trust.
Nothing in this example downloads or installs a package.

Prepare a host-specific manifest with hashes of the actual Python executable
and example script:

```sh
python examples/platform/make_adapter_manifest.py > /tmp/tower-adapter.json
```

Then enter these commands in the Raven palette:

```text
:ops run adapter manifest=/tmp/tower-adapter.json action=inspect
:ops run adapter manifest=/tmp/tower-adapter.json action=describe
:ops run adapter manifest=/tmp/tower-adapter.json action=run request=examples/platform/adapter-request.json
```

The native Describe, Validate, and Run contract prepares an action for review
before Apply. Managed Raven currently defers external adapter Apply. The example computes a SHA256 of explicit text. The process receives a
minimal environment plus the manifest's declared variables. Pin the executable
and any absolute file arguments; changed hashes require a new manifest/review.
Offline installation can provide the same pinned executable/assets in an
isolated environment. Raven does not resolve or download dependencies.

The protocol is newline-delimited JSON. Requests contain `schema`, `id`,
`method` and object `params`. Responses contain the same schema/id plus either
`type: result` and object `result`, `type: progress` and a number in `[0,1]`, or
`type: error` and printable `error`. Emit exactly one terminal result/error and
exit. No frames may follow it. Cancellation sends a `cancel` request for the same
ID before process-group termination after a short grace period. Frame, total
output, stderr and runtime bounds remain enforced while bytes arrive.

## Export a support bundle

Edit [support-manifest.json](support-manifest.json) to list only the evidence,
operation reports or qualification results you want to share. The bundle does
not crawl directories or collect environment variables or logs automatically.

```text
:ops run support-bundle manifest=examples/platform/support-manifest.json
```

Inspect the complete report. Common credential fields and assignments are
redacted, and the captured UI-state persistence status is included when
available. Redaction cannot identify every secret in free text. Set a new
**destination** directory, review the action, and apply it to save `support.json`.
Nothing is transmitted automatically. Redacted nested evidence can differ from
its original integrity digest; this bundle is a diagnostic copy, not an
unchanged original evidence archive.
