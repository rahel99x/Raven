#!/usr/bin/env bash
set -euo pipefail
raven_example_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
exec python3 "$raven_example_dir/view.py" "$@"
