#!/usr/bin/env bash
# Embers installer. Uses pipx if present, else a private venv. Safe to re-run.
#
#   curl -fsSL https://raw.githubusercontent.com/hollyvon/embers/main/setup.sh | bash
#   ./setup.sh [--dry-run]        # from a checkout: installs that checkout
#
# --dry-run prints what would run and changes nothing.
set -euo pipefail

DRY=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY=1 ;;
    -h|--help) sed -n '2,8p' "$0" 2>/dev/null | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

run() { if [ "$DRY" = 1 ]; then echo "+ $*"; else "$@"; fi; }

# Install from this checkout when run inside one, else from PyPI.
HERE=""
if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "${BASH_SOURCE[0]}" ]; then
  HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fi
if [ -n "$HERE" ] && [ -f "$HERE/pyproject.toml" ] && [ -d "$HERE/embers" ]; then
  TARGET="$HERE"
else
  TARGET="embers-memory"
fi

# Prefer the newest Python 3.9+ on PATH (macOS ships 3.9; newer is better when present).
# $PYTHON overrides the search.
if [ -n "${PYTHON:-}" ]; then
  PY="$PYTHON"
else
  PY=""
  for cand in python3.14 python3.13 python3.12 python3.11 python3.10 python3; do
    if command -v "$cand" >/dev/null 2>&1 && "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then
      PY="$cand"; break
    fi
  done
  [ -n "$PY" ] || PY="python3"
fi
if ! command -v "$PY" >/dev/null 2>&1; then
  echo "python3 not found. Embers needs Python 3.9 or newer." >&2
  exit 1
fi
if ! "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)'; then
  echo "Embers needs Python 3.9 or newer (found: $("$PY" -V 2>&1))." >&2
  exit 1
fi

if command -v pipx >/dev/null 2>&1; then
  echo "Installing with pipx: $TARGET"
  run pipx install --force "$TARGET"
else
  VENV="${EMBERS_VENV:-$HOME/.local/share/embers/venv}"
  BIN="${EMBERS_BIN:-$HOME/.local/bin}"
  echo "pipx not found; installing into a venv: $VENV"
  run "$PY" -m venv "$VENV"
  run "$VENV/bin/python" -m pip install --upgrade "$TARGET"
  run mkdir -p "$BIN"
  run ln -sf "$VENV/bin/embers" "$BIN/embers"
  run ln -sf "$VENV/bin/mem" "$BIN/mem"
  case ":$PATH:" in
    *":$BIN:"*) ;;
    *) echo "Note: add $BIN to your PATH to run 'embers' from anywhere." ;;
  esac
fi

cat <<'EOM'

Embers is installed. Three commands to start:

  embers detect                    # find your Claude Code transcripts (ingests nothing)
  embers ingest                    # build the archive (incremental; re-run any time)
  embers search "phrase"           # find where something was said or decided

To let your assistant drive it:  embers install-skill
EOM
