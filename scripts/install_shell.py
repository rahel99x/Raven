#!/usr/bin/env python3
"""Print, preview, or install an optional Raven source-checkout alias."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import shlex
import stat
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
BEGIN = "# >>> Raven alias >>>"
END = "# <<< Raven alias <<<"


def shell_block(root: Path | None = ROOT, profile: str = "carc") -> str:
    location = shlex.quote(str(root)) if root is not None else '"$HOME/projects/Raven"'
    default_profile = shlex.quote(profile)
    return f'''{BEGIN}
# The checkout's launcher uses its own .venv; your active environment stays intact.
export RAVEN_ROOT={location}
unalias raven dash dash2 2>/dev/null || :
unset -f raven dash dash2 2>/dev/null || :
_raven() {{
  local root="$RAVEN_ROOT"
  local profile={default_profile}
  profile="${{RAVEN_PROFILE:-$profile}}"
  local account="${{RAVEN_ACCOUNT-}}"
  if [ "$profile" = carc ] && [ -z "${{RAVEN_CONFIG-}}" ] && [ "${{RAVEN_ACCOUNT+x}}" != x ]; then
    account="${{CARC_ACCOUNT:-}}"
  fi
  if [ ! -x "$root/scripts/raven" ]; then
    printf 'raven: checkout not found at %s; set RAVEN_ROOT to your Raven directory.\\n' "$root" >&2
    return 127
  fi
  local -a command=("$root/scripts/raven")
  if [ -n "${{RAVEN_CONFIG-}}" ]; then
    command+=(--config "$RAVEN_CONFIG")
    if [ -n "${{RAVEN_PROFILE-}}" ]; then
      command+=(--site "$RAVEN_PROFILE")
    fi
  else
    command+=(--config "${{RAVEN_CONFIG:-$root/docs/config.example.json}}" --profile "$profile")
  fi
  command+=(--unicode)
  if [ -n "$account" ]; then
    command+=(--account "$account")
  fi
  "${{command[@]}}" "$@"
}}
alias raven='_raven'
{END}'''


def render(text: str, block: str) -> str:
    lines = text.splitlines(keepends=True)
    starts = [i for i, line in enumerate(lines) if line.rstrip('\r\n') == BEGIN]
    ends = [i for i, line in enumerate(lines) if line.rstrip('\r\n') == END]
    if starts or ends:
        if len(starts) != 1 or len(ends) != 1 or starts[0] >= ends[0]:
            raise ValueError("Existing Raven markers are incomplete or duplicated; no files changed.")
        del lines[starts[0]:ends[0] + 1]
    # Preserve existing shell code verbatim. Runtime unalias/unset handles old
    # definitions without attempting to parse arbitrary Bash functions/heredocs.
    base = "".join(lines).rstrip('\r\n')
    return (base + "\n\n" if base else "") + block + "\n"


def validate(text: str) -> None:
    result = subprocess.run(["bash", "-n"], input=text.encode("utf-8", "surrogateescape"), capture_output=True)
    if result.returncode:
        raise ValueError("Bash syntax check failed; no files changed:\n" + result.stderr.decode("utf-8", "replace"))


def apply(path: Path, original: bytes, updated: bytes) -> Path | None:
    if original == updated:
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    backup = None
    if path.exists():
        if path.read_bytes() != original:
            raise ValueError("The Bash configuration changed during preparation; no update applied.")
        with tempfile.NamedTemporaryFile(prefix=path.name + ".raven-", suffix=".bak", dir=path.parent, delete=False) as f:
            backup = Path(f.name)
            f.write(original)
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(prefix="." + path.name + ".raven-", dir=path.parent, delete=False) as f:
            temporary = Path(f.name)
            os.fchmod(f.fileno(), mode)
            f.write(updated)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
    return backup


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bashrc", type=Path, default=Path.home() / ".bashrc", help="Bash configuration to update; symlinks are preserved")
    parser.add_argument("--profile", default="carc", help="default Raven profile for the alias; RAVEN_PROFILE can override it")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--output", type=Path, help="write a complete updated preview to a new file")
    mode.add_argument("--apply", action="store_true", help="back up and atomically update the Bash configuration")
    args = parser.parse_args(argv)
    try:
        block = shell_block(ROOT, args.profile)
        if not args.output and not args.apply:
            print(block)
            return 0
        path = args.bashrc.expanduser().resolve()
        if path.exists() and not path.is_file():
            raise ValueError(f"Not a regular Bash configuration: {path}")
        original = path.read_bytes() if path.exists() else b""
        updated = render(original.decode("utf-8", "surrogateescape"), block)
        validate(updated)
        data = updated.encode("utf-8", "surrogateescape")
        if args.output:
            output = args.output.expanduser()
            with output.open("xb") as f:
                os.chmod(output, 0o600)
                f.write(data)
            print(f"Preview: {output}. Existing configuration unchanged.")
        else:
            backup = apply(path, original, data)
            print(f"Updated: {path}" if original != data else f"Already configured: {path}")
            if backup:
                print(f"Backup: {backup}")
            print(f"Reload in your Bash session: source {shlex.quote(str(args.bashrc.expanduser()))}")
        return 0
    except (OSError, ValueError) as exc:
        print(f"Shell setup: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
