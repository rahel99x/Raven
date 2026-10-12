#!/usr/bin/env bash
# Review and apply Raven's built-in copy-only migration from a source checkout.
set -euo pipefail

usage() {
    cat <<'HELP'
Usage: scripts/migrate-config.sh [OPTIONS]

Preview legacy configuration migration by default. Original files and existing
Raven configuration are preserved. No shell startup files are changed.

  --plan                         Preview only (the default)
  --save-plan FILE               Save a new private plan; never overwrite FILE
  --plan-file FILE               Read a previously saved plan
  --apply                        Apply --plan-file, or create/apply --save-plan
  --include-state                Also inventory the legacy state directory
  --include-history              Include bounded history; requires --include-state
  --source-config DIR            Previous config root; required for a new plan
  --source-state DIR             Previous state root; required with --include-state
  --destination-config FILE      Raven config (default: XDG config/raven/config.json)
  --destination-state DIR        Raven state/archive root (default: XDG state/raven)
  -h, --help                     Show this help

Review:  scripts/migrate-config.sh --source-config /previous/config --save-plan /private/directory/plan.json
Apply:   scripts/migrate-config.sh --apply --plan-file /private/directory/plan.json
Resume:  Repeat the same apply command; retain the returned journal path.

Source directories are explicit; Raven never inventories its own default state
as a migration source. --source-state requires --include-state. A saved plan fixes
all source and destination choices; do not combine it with inventory/path options.
HELP
}

fail() { printf 'Raven migration: %s\n' "$*" >&2; exit 2; }
require_value() { [[ $# -ge 2 && -n $2 && $2 != --* ]] || fail "$1 requires a value"; }

script_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
action=--plan
explicit_plan=false
include_state=false
include_history=false
source_config_set=false
source_state_set=false
inventory_options=false
plan_file=
save_plan=
arguments=()

while (( $# )); do
    case $1 in
        --plan) explicit_plan=true; shift ;;
        --apply) action=--apply; shift ;;
        --include-state) include_state=true; inventory_options=true; shift ;;
        --include-history) include_history=true; inventory_options=true; arguments+=("$1"); shift ;;
        --plan-file)
            require_value "$@"; [[ -z $plan_file ]] || fail "--plan-file was supplied twice"
            plan_file=$2; shift 2 ;;
        --save-plan)
            require_value "$@"; [[ -z $save_plan ]] || fail "--save-plan was supplied twice"
            save_plan=$2; shift 2 ;;
        --source-config|--source-state|--destination-config|--destination-state)
            require_value "$@"; inventory_options=true
            if [[ $1 == --source-config ]]; then source_config_set=true; fi
            if [[ $1 == --source-state ]]; then source_state_set=true; fi
            arguments+=("$1" "$2"); shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) fail "Unknown option: $1 (use --help)" ;;
    esac
done

if $explicit_plan && [[ $action == --apply ]]; then fail "Choose --plan or --apply"; fi
if [[ -n $plan_file && -n $save_plan ]]; then fail "Choose --plan-file or --save-plan"; fi
if [[ -n $plan_file ]] && $inventory_options; then fail "A saved plan already fixes paths and inventory options"; fi
if $source_state_set && ! $include_state; then fail "--source-state requires --include-state"; fi
if $include_history && ! $include_state; then fail "--include-history requires --include-state"; fi
if [[ $action == --apply && -z $plan_file && -z $save_plan ]]; then
    fail "--apply requires a reviewed --plan-file or an explicit new --save-plan"
fi
if [[ -z $plan_file ]]; then
    if ! $source_config_set; then fail "A new plan requires --source-config"; fi
    if $include_state && ! $source_state_set; then fail "--include-state requires --source-state"; fi
fi

empty_state=
cleanup() { if [[ -n $empty_state ]]; then rmdir -- "$empty_state"; fi; }
trap cleanup EXIT
if [[ -n $plan_file ]]; then
    arguments+=(--plan-file "$plan_file")
elif ! $include_state; then
    # The migration engine inventories two roots. An empty temporary state root
    # selects configuration only without reproducing its inventory/conversion.
    empty_state=$(mktemp -d "${TMPDIR:-/tmp}/raven-config-only.XXXXXXXX")
    arguments+=(--source-state "$empty_state")
fi
if [[ -n $save_plan ]]; then arguments+=(--save-plan "$save_plan"); fi
"$script_dir/ravenl" migrate "$action" "${arguments[@]}"
