#!/usr/bin/env bash
# Preview a Raven migration journal; deletion stays in the built-in engine.
set -euo pipefail

usage() {
    cat <<'HELP'
Usage: scripts/rollback-config.sh --journal FILE [--apply]

Preview migration-owned outputs and identity/hash conflicts without changing
files. --apply invokes Raven's built-in rollback, which removes only unchanged
outputs owned by that migration. Originals and existing Raven config survive.

  --journal FILE                 Explicit migration journal returned by apply
  --apply                        Perform rollback after reviewing the preview
  -h, --help                     Show this help

Keep the journal: repeating --apply after a successful rollback is harmless.
Edited or replaced outputs produce conflicts; they are never overwritten.
HELP
}

fail() { printf 'Raven rollback: %s\n' "$*" >&2; exit 2; }
script_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
journal=
apply=false
while (( $# )); do
    case $1 in
        --journal)
            [[ $# -ge 2 && -n $2 && $2 != --* ]] || fail "--journal requires a value"
            [[ -z $journal ]] || fail "--journal was supplied twice"
            journal=$2; shift 2 ;;
        --apply) apply=true; shift ;;
        -h|--help) usage; exit 0 ;;
        *) fail "Unknown option: $1 (use --help)" ;;
    esac
done
[[ -n $journal ]] || fail "--journal is required (use --help)"
if $apply; then exec "$script_dir/ravenl" migrate --rollback "$journal"; fi

source_root=$(CDPATH= cd -- "$script_dir/.." && pwd)
python=python3
if [[ -x $source_root/.venv/bin/python ]]; then python=$source_root/.venv/bin/python; fi
"$python" -B - "$source_root" "$journal" <<'PY'
import hashlib
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, sys.argv[1])
from raven import raven_migration as migration

try:
    path = Path(sys.argv[2]).absolute()
    journal = migration._journal_read(path)
    plan = journal["plan"]
    allowed = {record["destination"]: record["sha256"] for record in plan["files"]}
    if plan["converted_config"] is not None:
        allowed[plan["destination_config"]] = hashlib.sha256(migration.canonical(plan["converted_config"]) + b"\n").hexdigest()
    if plan.get("activation"):
        allowed[plan["activation"]] = hashlib.sha256(migration._activation_bytes(plan)).hexdigest()
    outputs = list(journal["outputs"])
    pending = journal.get("pending")
    if pending and pending["path"] not in {item["path"] for item in outputs}:
        outputs.append(pending)
    rows = []
    for output in outputs:
        status = "missing"
        if allowed.get(output["path"]) != output["sha256"]:
            status = "conflict"
        elif os.path.lexists(output["path"]):
            try:
                _, current = migration._read(output["path"])
                status = "unchanged" if all(current[key] == output[key] for key in ("device", "inode", "sha256")) else "conflict"
            except (OSError, ValueError):
                status = "conflict"
        rows.append({"path": output["path"], "status": status})
    if pending and pending.get("staging"):
        stage, destination = Path(pending["staging"]), Path(pending["path"])
        status = "missing"
        if stage.parent != destination.parent or not stage.name.startswith("." + destination.name + ".raven-"):
            status = "conflict"
        elif os.path.lexists(stage):
            try:
                _, current = migration._read(stage)
                status = "unchanged" if all(current[key] == pending[key] for key in ("device", "inode", "sha256")) else "conflict"
            except (OSError, ValueError):
                status = "conflict"
        rows.append({"path": str(stage), "status": status, "staging": True})
    conflicts = sum(item["status"] == "conflict" for item in rows)
    print(json.dumps({"schema": "raven.rollback-preview/v1", "journal": str(path),
                      "state": journal["state"], "preview": True, "outputs": rows,
                      "conflicts": conflicts, "originals": "unchanged"}, indent=2))
    raise SystemExit(2 if conflicts else 0)
except (OSError, ValueError, TypeError, KeyError) as exc:
    print("Raven rollback: " + str(exc), file=sys.stderr)
    raise SystemExit(2)
PY
