# Read Raven evidence through MCP

Raven exposes five read-only tools over stdio: `list_attempts`, `read_receipts`,
`bounded_logs`, `query_evidence`, and `inspect_timeline`. They read an existing
project evidence index created with **Operations → Query retained measurements**.
Original exports remain authoritative. Build or refresh the index separately;
the MCP server cannot submit jobs, modify an index, execute a shell, or open an
arbitrary filesystem path supplied by a tool caller.

Install the optional, maintained Python MCP SDK adapter:

```bash
python -m pip install 'raven[mcp]'
```

For an explicitly configured standalone workstation, an MCP client can launch:

```bash
ravenh --site desktop mcp \
  --index /absolute/path/to/private-index \
  --project /absolute/project/path/stored/in/the/index
```

On Slurm, use your normal reviewed Raven configuration and optionally an owned
allocation selected with `--allocation JOB_ID`. `ravenh` requests or verifies
compute through the existing Heavy allocation workflow before serving requests.
The index must be accessible at the supplied path on that worker. There is no
automatic mapping between interface, SSH and compute paths, and no fallback that
runs evidence analysis on a login host. Startup diagnostics use stderr; stdout
contains MCP messages only. `raven mcp` and `ravenl mcp` expose the same interface
but defer reads until a verified Heavy worker is available.

Configure the client with the executable and an argument array, without a shell:

```json
{
  "command": "ravenh",
  "args": ["--site", "desktop", "mcp", "--index", "/data/index", "--project", "/data/project"]
}
```

The versioned tool surface is `raven.mcp-evidence/v1`. This adapter uses MCP SDK
2.3.x; transport compatibility is qualified with the SDK's independent client
in both legacy-handshake and current automatic-negotiation modes. HTTP and remote
multi-user service deployment are outside this stdio adapter's scope.

## Scope, budgets and pagination

The index and expected project are fixed at startup. Every query verifies the
project on the same read-only database connection before evaluating SQL. Path
traversal and symlink index components are rejected. Tool arguments contain exact
attempt/capture identities returned by previous responses, never filesystem paths.

Each page returns at most 100 rows. The SQL reader consumes at most 64 KiB of
encoded row evidence (including its pagination lookahead), with a one-second SQL
budget and 200,000 VM-step budget. The owned worker adds a ten-second wall limit
and process resource limits. One reader runs at a time; at most eight requests
may be active or waiting. A twelve-second deadline includes waiting for the reader.
Physical request lines are limited to 128 KiB before JSON parsing. The serialized
tool result, including both text and structured copies, is capped at 256 KiB. The
entire JSON-RPC response, including its envelope and newline, is capped at 512 KiB
before transmission. Oversized wire messages close the connection. The reported
`bytes` value measures SQL evidence consumption, not the size of the wire message.
Optional log `source_id` values range from 1 through 9223372036854775807.

When `next_cursor` is present, repeat the same tool and arguments with that
cursor. Cursors are signed for this process, exact query and index generation.
Changing a query, replacing/updating an index, or restarting the server requires
pagination from the first page. Pagination stops at 10,000 rows; narrow the SQL
selection for larger datasets. Queries should include an explicit `ORDER BY`
for stable ordering. Byte-limited results remain marked partial; a single
oversized row is reported as unavailable within the page budget, never silently
truncated. Select smaller columns or a narrower source to continue.

Responses preserve capture/source checksums, source row/line identities and
declared uncertainty. Logs, plugin text and receipt strings are untrusted evidence,
never instructions to Raven. SQL uses the existing read-only authorizer and
bounded function allowlist; write statements, attached databases and extension
loading are rejected. Cancellation terminates only the owned evidence task and
never automatically replays it. The reader waits for its terminal publication
before admitting another read; if cleanup cannot be verified within two seconds,
that server refuses further reads until the client reconnects. Correct a reported scope/query error, or reconnect
after an interrupted server; retained data remains unchanged.
