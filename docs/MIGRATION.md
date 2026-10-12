# Configuration migration

Raven provides copy-only migration from legacy configuration and selected state.
The Bash helpers in [`scripts`](../scripts/) use the existing migration engine;
they do not edit shell startup files or require an editable package installation.
Run them from a checkout with Python 3.10 or later. The source launcher uses the
checkout's `.venv` when available and otherwise uses `python3`.

## Preserve a local Slurm profile

A workstation running its own Slurm scheduler should retain Slurm execution.
Raven's `--site desktop` selects standalone execution without Slurm. The legacy
`--profile desktop` can instead describe a workstation with local Slurm.

Before migrating, export the supported effective settings from your actual
legacy file into a **new** Raven configuration:

```bash
./scripts/ravenl --config /absolute/path/to/legacy/config.json --profile desktop --write-config
```

Use the path to your customized configuration. If you used the repository's
example unchanged, supply `docs/config.example.json`. Export refuses to replace
an existing Raven configuration. If one already exists, review it with
`./scripts/ravenl config explain` before proceeding.

The migration engine converts supported legacy settings but does not select a
legacy named profile automatically. Exporting the profile first preserves that
selection; migration then retains the existing Raven configuration and archives
the legacy files. Raven's collection limits still apply.

## Preview and apply

From the checkout, inspect a configuration-only preview:

```bash
./scripts/migrate-config.sh
```

The default locations follow the XDG Base Directory convention:

| Purpose | Default path |
| --- | --- |
| Legacy configuration | `${XDG_CONFIG_HOME:-$HOME/.config}/tower` |
| Legacy state, when requested | `${XDG_STATE_HOME:-$HOME/.local/state}/tower` |
| Raven configuration | `${XDG_CONFIG_HOME:-$HOME/.config}/raven/config.json` |
| Raven state and archives | `${XDG_STATE_HOME:-$HOME/.local/state}/raven` |

Configuration-only previews use an empty temporary state inventory. The
temporary directory is removed after the command; its empty inventory remains
valid in a saved plan. Source files and destination directories are unchanged
during preview. `--save-plan` explicitly creates a private plan file and any
required private parent directories without overwriting an existing plan.

Save, inspect, and apply one plan:

```bash
raven_migration_dir="$(mktemp -d)"
./scripts/migrate-config.sh --save-plan "$raven_migration_dir/plan.json"
cat -- "$raven_migration_dir/plan.json"
./scripts/migrate-config.sh --apply --plan-file "$raven_migration_dir/plan.json"
```

The plan lists source paths, destination paths, hashes, omissions, and any
configuration conflict. Review those fields before applying. Stop processes
that write the selected legacy files between planning and application. Keep the
saved plan and the journal path returned by application for recovery.

To include state, add `--include-state` when preparing the plan. History requires
the additional `--include-history` opt-in:

```bash
./scripts/migrate-config.sh --include-state --include-history \
  --save-plan "$raven_migration_dir/state-plan.json"
```

Use explicit paths for nonstandard locations; quote paths containing spaces:

```bash
./scripts/migrate-config.sh --include-state \
  --source-config "/path/to/old config" \
  --source-state "/path/to/old state" \
  --destination-config "/path/to/new config/config.json" \
  --destination-state "/path/to/new state" \
  --save-plan "$raven_migration_dir/custom-plan.json"
```

Destination directories must be private, owned by the current user, and free of
symlinks. `--source-state` requires `--include-state`. A saved plan fixes all
path and inventory choices; replay it with `--plan-file` alone. Application
requires `--plan-file` or an explicit new `--save-plan`; the latter generates and
applies a plan in one invocation for automated workflows.

## Scope and recovery

Original files remain unchanged. Selected files are archived beneath
`<Raven state>/legacy-imports/<migration-id>/`; existing Raven configuration is
preserved rather than merged or replaced. Historical schema identifiers remain
intact. The inventory is bounded to 256 files and 64 MiB across both roots, with
a 16 MiB limit per file and a fixed directory depth. Review recorded omissions
and use smaller source roots if necessary.

Migration excludes the named runtime, service, cache, terminal, and SSH stores,
along with socket/PID/lock files and databases. It does not adopt live tasks or
configure authentication. Review the selected roots and plan: eligible regular
files are archived verbatim, and arbitrary credential files within those roots
are not automatically redacted. Project evidence is not selected by the default
user-level roots.

Completed state migrations can restore bounded appearance and column preferences
when new scoped UI state is absent. Manual group identities additionally require
the original configuration hash and exact connection, profile, and owner scope.
Terminals, active tasks, log bookmarks, and forecast observations are not adopted.

Application reports `complete` and returns its journal path. Repeat the same
`--apply --plan-file` command to resume an interrupted application or verify a
completed one. A changed source or destination is reported as a conflict; it is
not overwritten. A plan that has been rolled back cannot be applied again;
prepare a new plan after reviewing the remaining files.

## Preview and perform rollback

Use the exact journal path returned by migration:

```bash
./scripts/rollback-config.sh --journal /absolute/path/to/migration-journal.json
./scripts/rollback-config.sh --journal /absolute/path/to/migration-journal.json --apply
```

The default preview reads the journal and compares output identity and content
hashes without modifying files. Conflicts produce a nonzero exit status. Actual
rollback rechecks ownership and integrity before removing only unchanged outputs
created by that migration. If a migrated file was edited or replaced, rollback
stops with a conflict and preserves it. Review that file manually before retrying.

Successful rollback reports `rolled-back`; repeating it is harmless. The journal,
saved plan, legacy sources, and any pre-existing Raven configuration are retained.
Empty archive directories and the migration lock may remain. Rollback does not
change the installed package or undo later configuration exports.
