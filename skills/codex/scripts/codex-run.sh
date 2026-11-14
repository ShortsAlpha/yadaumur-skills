#!/usr/bin/env bash
# Finds the Codex CLI and runs it with the given args. Works on Windows (Git Bash) and macOS.
# Order: $CODEX_BIN, `codex` on PATH (npm i -g @openai/codex), the copy bundled with the
# ChatGPT/Codex desktop app (its folder changes on app updates, so it is resolved each time).
set -euo pipefail

find_codex() {
  if [[ -n "${CODEX_BIN:-}" && -x "${CODEX_BIN}" ]]; then echo "$CODEX_BIN"; return; fi
  if command -v codex >/dev/null 2>&1; then command -v codex; return; fi
  local cfg="$HOME/.codex/config.toml" p
  if [[ -f "$cfg" ]]; then
    p=$(sed -n "s/^CODEX_CLI_PATH *= *['\"]\(.*\)['\"].*/\1/p" "$cfg" | head -1)
    if [[ -n "$p" ]]; then
      p=$(cygpath -u "$p" 2>/dev/null || echo "$p")
      [[ -x "$p" ]] && { echo "$p"; return; }
    fi
  fi
  p=$(ls -t "$HOME"/AppData/Local/OpenAI/Codex/bin/*/codex.exe 2>/dev/null | head -1 || true)
  [[ -n "$p" ]] && { echo "$p"; return; }
  # macOS app bundles (ChatGPT.app / Codex.app), system-wide or per-user, plus Homebrew
  for p in /Applications/*.app/Contents/Resources/codex "$HOME"/Applications/*.app/Contents/Resources/codex \
           /Applications/*.app/Contents/Resources/bin/codex /opt/homebrew/bin/codex /usr/local/bin/codex; do
    [[ -x "$p" ]] && { echo "$p"; return; }
  done
  echo "codex-run: Codex CLI not found (ChatGPT app not installed or not updated?)" >&2
  return 1
}

CODEX=$(find_codex)
if [[ "${1:-}" == "--which" ]]; then echo "$CODEX"; exit 0; fi
exec "$CODEX" "$@" < /dev/null
