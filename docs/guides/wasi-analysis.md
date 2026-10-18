# Experimental WASI analysis adapters

Raven can execute one deliberately narrow class of optional adapter: a pure
analysis module that summarizes an existing finite numeric series. This is an
explicit reviewed operation, not a conversion of Python, native, MPI or CUDA
plugins. Existing Python plugins still execute as trusted Python code.

## Install and prepare the example

Install the reviewed release into the same environment used by Raven's Heavy
worker. The optional dependency is pinned to the runtime qualified for this ABI:

```bash
python -m pip install '.[terminal,wasi]'
python - <<'PY'
import hashlib, json
from pathlib import Path
import wasmtime

module = bytes(wasmtime.wat2wasm(Path('examples/wasi/f64-summary.wat').read_text()))
path = Path('f64-summary.wasm').resolve()
path.write_bytes(module)
Path('wasi-series.json').write_text(json.dumps({
    'schema': 'raven.f64-series/v1', 'values': [1.25, -4.0, 9.0, 0.5]
}))
Path('wasi-analysis.json').write_text(json.dumps({
    'schema': 'raven.wasi-analysis/v1', 'module': str(path),
    'sha256': hashlib.sha256(module).hexdigest(),
    'abi': 'raven.f64-summary/wasi-preview1-v1',
    'limits': {'memory_bytes': 1048576, 'fuel': 1000000,
               'wall_seconds': 5, 'output_bytes': 1024}
}, indent=2))
PY
```

In Raven's command palette, open `:ops wasi-analysis`. Choose the manifest and
series JSON, inspect the exact runtime, module/input digests and limits, then
choose **Run** and review **Apply**. An optional new result directory retains
`analysis.json` and a publication receipt. Existing destinations are never
overwritten. The palette equivalent is:

```text
:ops run wasi-analysis manifest=/absolute/wasi-analysis.json input=/absolute/wasi-series.json action=run destination=/absolute/new-result
```

Managed Raven dispatches inspection and execution to its verified Heavy worker.
Light reports the missing compute capability. An unavailable or mismatched
runtime gives an installation diagnostic; Raven never silently executes the
module as a Python plugin. Paths must exist on the reviewed execution host.

## Supported ABI and limits

The module is compiled WebAssembly v1, at most 1 MiB, exporting one `memory` and
`_start: () -> ()`. Only three WASI Preview1 imports are supplied:

| Import | Capability |
| --- | --- |
| `fd_read` | Read bounded immutable input from descriptor 0 only |
| `fd_write` | Append bounded in-memory output to descriptors 1 or 2 only |
| `proc_exit` | End this analysis, with zero indicating success |

There is no general WASI environment. No directories are preopened. File/path
operations, symlinks, sockets, environment variables, clocks and randomness have
no host import. Other descriptors return `EBADF`; unknown imports fail before
the module starts. Each host call validates pointers, at most 1,024 iovecs and
1 MiB aggregate bytes; host calls perform no blocking external I/O.

Input is little-endian `u32 count` followed by `count` IEEE754 f64 values. The
JSON source contains 1–65,535 finite numbers and is limited to 1 MiB. Integer
values must be exactly representable as f64; for example `2**60` is exact but
`2**53 + 1` is refused instead of silently rounded. Deliberate rounding belongs
in the source program. Output is
exactly 32 bytes: little-endian `u64 count, f64 sum, f64 minimum, f64 maximum`.
The supplied example sums in input order and retains the first extrema on ties,
including signed zero. This is ordinary floating-point summation, not a claim
of exact real arithmetic or improved numerical stability.

The manifest caps guest linear memory at 32 MiB, fuel at 100 million, wall time
at 30 seconds and guest output at 64 KiB. The isolated runner additionally caps
address space at 256 MiB (or a stricter inherited worker limit), tables, instances,
stack, CPU time and open descriptors. Compilation counts against wall time.
The parent independently streams and caps stdout/stderr and kills the runner on
deadline or cancellation. The runner remains inside the worker's process group,
so worker cancellation cannot leave an independently detached guest behind.
Fuel is only an instruction budget; it does not substitute for wall time or host
I/O bounds.

## Trust, recovery and maintenance

The threat model is an untrusted module/input running inside an explicitly
approved analysis operation. The Wasmtime native runtime, Python runner and host
kernel are trusted components. The boundary limits the module's granted authority;
it is not a guarantee against runtime vulnerabilities, kernel bugs, side channels
or every denial of service. The f64 summary is the adapter's claim. Raven checks
its structure, counts and finite values, but an arbitrary module can return
scientifically false numbers. Digests establish which bytes were reviewed.

Review does not execute guest code. Apply rereads and hashes the module/input,
checks the runtime/version and requires the same worker identity. Changed data or
limits require a new review. A failed or cancelled attempt publishes no successful
result; no action retries automatically. If an interruption follows publication,
inspect the existing receipt before choosing a new destination.

The qualified runtime is `wasmtime==49.0.0`. Monitor Wasmtime/Bytecode Alliance
security advisories and Python package notices. Disable the optional operation
when an applicable unresolved vulnerability is identified. Updating the pin
requires rerunning hostile fixtures, real worker integration, output fidelity and
resource measurements before release; installing a different version alone does
not qualify it. Keep the core monitor installed when removing the `wasi` extra.
Arbitrary WASI applications and production cluster deployment remain unqualified.
