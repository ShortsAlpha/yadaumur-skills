#!/usr/bin/env bash
# Prints current Codex usage limits. Args are passed to codex-limits.mjs (--text | --raw).
# Finds codex on PATH, else via the codex skill's resolver (desktop app copy). Needs Node 18+.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
if [[ -z "${CODEX_BIN:-}" ]] && ! command -v codex >/dev/null 2>&1; then
  r="$HOME/.claude/skills/codex/scripts/codex-run.sh"
  [[ -f "$r" ]] && CODEX_BIN="$(bash "$r" --which 2>/dev/null)"
  # Node on Windows needs a native path, not /c/...
  [[ -n "${CODEX_BIN:-}" ]] && command -v cygpath >/dev/null 2>&1 && CODEX_BIN="$(cygpath -w "$CODEX_BIN")"
  export CODEX_BIN
fi
command -v node >/dev/null 2>&1 || { echo "codex-limits: node not found (install Node.js 18+)" >&2; exit 1; }
exec node "$HERE/codex-limits.mjs" "$@" < /dev/null
