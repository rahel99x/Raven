# Collection runtime and remote reader cache

Raven shares eligible scheduler observations between its processes on the same
Linux host. Its private cache uses short SQLite WAL transactions: admission is
committed before collection begins, and publication verifies that the same
owner still holds the lease. Cancellation does not let another collector
replace work that has not drained.

The cache uses `synchronous=NORMAL` because observations can be collected again.
It is an advisory coordination cache, not the durable record of a submitted
scientific job. Threads reuse their own connections; an external scheduler read
never holds a database transaction. Explicit cache directories must remain
private and host local. Automatic runtime paths fall back to a fresh private
name if another user has occupied the usual name.

Where kernel boot/process or filesystem identity is unavailable, Raven uses an
in-memory process cache with the same bounded admission and cancellation
contract. It continues to coalesce readers in that process, but does not claim
to coordinate separate Raven processes. Native macOS qualification is separate
from tests that simulate unavailable Linux interfaces.

## SSH project reads

The first project read on an SSH host transfers a bounded, content-addressed
copy of Raven's confined reader implementation. Each upload command carries at
most 24 KiB of source data, avoiding the remote shell's single-argument limit.
Raven verifies its SHA-256 digest and publishes it atomically in a private
`raven-readers-*` directory beneath the remote temporary directory. It never
imports the user's project code.

Later reads send a small fixed bootstrap, the bundle digest and the requested
read operation. Compiled readers are cached separately for each Python cache
tag. A damaged bundle is replaced from the matching local installation; a
changed Raven installation gets a different digest. Old cache entries are
bounded and can be removed by the host without losing project data. Upload and
response bytes both count against the collection budget.

A cold host needs several short staging commands. Warm reads use one command;
there is no persistent remote daemon, package installation or scientific-job
submission. If staging is temporarily deferred by a collection quota, Raven
preserves that retry instruction and retries through its ordinary source lane.
