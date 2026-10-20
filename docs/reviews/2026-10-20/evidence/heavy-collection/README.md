# Heavy and collection measurements

These drivers compare equivalent fixtures against `b98742f` and the review
working tree. Run each phase serially from outside either source checkout, with
`PYTHONPATH` selecting the package and an output JSON path as the argument.
Coordinate a quiet window before running them. The raw reports predate the
final package move and pure-port extractions; rerun them before associating
numbers with a final delivered commit.

| Fixture | Before | Review working tree | Tradeoff |
| --- | ---: | ---: | --- |
| Heavy task median, 50 actual local tasks | 84.66 ms | 5.71 ms | Linux single-threaded fork path; interpreter fallback remains |
| Worker plus child CPU, same 50 tasks | 4.23 s | 0.29 s | Fixed operation imports are shared by the worker |
| Worker peak RSS | 18,816 KiB | 17,412 KiB | Statistics operation fixture only |
| Driver peak RSS, Heavy fixture | 14,848 KiB | 40,180 KiB | Whole integrated revision; increase retained in evidence |
| 100 advisory admissions | 81.50 ms | 18.70 ms | Thread-local connections retained until close/thread exit |
| SQLite connections after setup, same 100 admissions | 200 | 0 | Separate claim/publication transactions preserved |
| Warm remote helper median, 25 reads | 123.06 ms | 71.42 ms | Actual isolated subprocess transport |
| Warm remote command bytes per read | 115,826 | 8,396 | Same confined read operation |
| First-host remote setup | 292 ms / 1 process | 839 ms / 8 processes | One bounded content-addressed upload |

RSS is the process high-water mark in KiB on this Linux host. Driver Python
allocation peaks use a separate `tracemalloc` phase, reported in bytes; they do
not measure native allocations or subprocess RSS. Remote helper CPU and peak
RSS come from actual local child processes. These fixtures do not qualify a
live SSH network, scheduler, GPU, cancellation latency or shared filesystem.
Connection counts are an I/O proxy; physical storage I/O was not measured.
