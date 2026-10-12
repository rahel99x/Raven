# GPU adapter fixtures

These deterministic fixtures exercise the upstream command-line output formats
used by Raven's GPU adapters. Device identities and readings are synthetic;
they are not measurements from hardware available in CI.

## AMD

The [AMD SMI implementation](https://github.com/ROCm/amdsmi/blob/amd-staging/amdsmi_cli/amdsmi_commands.py)
defines the fixture format. `list` emits `gpu`, `bdf`, `uuid`, and `partition_id`.
`metric --usage --mem-usage --json` emits `usage.gfx_activity` and
`mem_usage.used_vram` / `total_vram`. The represented AMD CLI uses an `MB` label
after dividing byte counts by `1024**2`; Raven preserves that meaning.

## Intel

The [Intel XPU-SMI guide](https://github.com/intel/xpumanager/blob/master/doc/smi_user_guide.md)
describes device discovery. The
[statistics implementation](https://github.com/intel/xpumanager/blob/master/cli/src/comlet_statistics.cpp)
defines the represented counters.

Only `device_level` values are treated as whole-device utilization. Tile-only values
and avg/min/max statistics do not substitute for a current device measurement.
