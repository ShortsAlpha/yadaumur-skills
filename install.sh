#!/usr/bin/env bash
# Sets up these Claude Code skills, agents and hooks on this device (macOS bash 3.2+ or Windows Git Bash):
#   1. links every skills/<name> folder into ~/.claude/skills/<name>
#   2. links agents/ as ~/.claude/agents
#      Existing links/junctions that point elsewhere (e.g. an older checkout) are re-pointed here.
#      A real (non-link) folder with content is never touched: it is reported and skipped.
#   3. adds the vault-live hooks (Stop, SessionStart) to ~/.claude/settings.json (idempotent), only when
#      a notes vault with beyin.py exists ($VAULT_LIVE_VAULT, $SECOND_BRAIN_DIR, ~/second_brain, C:\second_brain)
#   4. adds the agent-budget guard (PreToolUse on Agent|Task|Workflow) to ~/.claude/settings.json (idempotent)
#   5. macOS only: adds the disk-cleanup reminder hook (SessionStart, UserPromptSubmit) to ~/.claude/settings.json
#   6. optional: Codex CLI on PATH (npm i -g @openai/codex) and OpenAI's official Claude Code plugin
#      (codex@openai-codex); skipped with a message when npm / the claude CLI is missing, or with --no-codex
# Safe to run again: a second run changes nothing.
# Usage: bash install.sh [--no-codex]
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
NO_CODEX=0
for arg in "$@"; do
  case "$arg" in
    --no-codex) NO_CODEX=1 ;;
    -h|--help) sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "install.sh: unknown option $arg (try --help)" >&2; exit 2 ;;
  esac
done

ok()   { printf '  \033[32mOK\033[0m   %s\n' "$*"; }
skip() { printf '  \033[36m--\033[0m   %s\n' "$*"; }
warn() { printf '  \033[33m!!\033[0m   %s\n' "$*"; }
win()  { case "$(uname -s)" in MINGW*|MSYS*|CYGWIN*) return 0 ;; *) return 1 ;; esac; }

# link_dir <src> <dest> <label>: makes <dest> a link (symlink; junction on Windows) to <src>.
link_dir() {
  src="$1"; dest="$2"; label="$3"
  if win; then
    wsrc="$(cygpath -w "$src" | sed "s/'/''/g")"; wdest="$(cygpath -w "$dest" | sed "s/'/''/g")"
    result="$(powershell.exe -NoProfile -NonInteractive -Command "
      \$ErrorActionPreference = 'Stop'
      \$s = '$wsrc'; \$d = '$wdest'
      try {
        \$i = Get-Item -LiteralPath \$d -Force -ErrorAction SilentlyContinue
        \$old = ''
        if (\$i) {
          if (\$i.LinkType) {
            \$t = [string](@(\$i.Target)[0]) -replace '^\\\\\\?\\?\\\\', ''
            if (\$t.TrimEnd('\\') -ieq \$s.TrimEnd('\\')) { 'same'; exit }
            \$old = \$t; \$i.Delete()
          } elseif (\$i.PSIsContainer -and -not (Get-ChildItem -LiteralPath \$d -Force)) {
            Remove-Item -LiteralPath \$d -Force
          } else { 'notlink'; exit }
        }
        New-Item -ItemType Junction -Path \$d -Target \$s | Out-Null
        if (\$old) { 'relinked ' + \$old } else { 'created' }
      } catch { 'error ' + \$_.Exception.Message }
    " 2>&1 | tr -d '\r')"
  else
    if [ -L "$dest" ]; then
      cur="$(readlink "$dest")"
      if [ "$cur" = "$src" ]; then result="same"
      else rm "$dest" && ln -s "$src" "$dest" && result="relinked $cur" || result="error could not re-link"; fi
    elif [ -d "$dest" ]; then
      if [ -z "$(ls -A "$dest" 2>/dev/null)" ]; then
        rmdir "$dest" && ln -s "$src" "$dest" && result="created" || result="error could not link"
      else result="notlink"; fi
    elif [ -e "$dest" ]; then result="notlink"
    else ln -s "$src" "$dest" && result="created" || result="error could not link"; fi
  fi
  case "$result" in
    same)       ok "$label already linked -> $src" ;;
    created)    ok "$label linked -> $src" ;;
    relinked*)  ok "$label re-linked -> $src (was ${result#relinked })" ;;
    notlink)    warn "$label: $dest exists and is not a link; move it away and rerun (skipped)" ;;
    *)          warn "$label: ${result#error } (skipped)" ;;
  esac
}

echo "1) Skills -> ~/.claude/skills"
SK="$HOME/.claude/skills"
mkdir -p "$SK"
for d in "$HERE"/skills/*/; do
  [ -f "$d/SKILL.md" ] || continue
  src="${d%/}"; name="$(basename "$src")"
  link_dir "$src" "$SK/$name" "$name"
done

echo "2) Agents -> ~/.claude/agents"
link_dir "$HERE/agents" "$HOME/.claude/agents" "agents"

# run_step <label> <command...>: runs a Python installer; reports its output, or why it failed.
run_step() {
  label="$1"; shift
  out="$("$@" 2>&1)"; rc=$?
  if [ "$rc" = 0 ]; then ok "$out"; return; fi
  [ -n "$out" ] || out="no output (exit code $rc; is Python 3.9+ installed and runnable, e.g. 'py -3 --version' / 'python3 --version'?)"
  warn "$label failed (exit $rc): $out"
}

# find_vault: the notes vault, in vault_live.py's order: $VAULT_LIVE_VAULT, else $SECOND_BRAIN_DIR,
# else ~/second_brain, else C:\second_brain on Windows. Prints the path; empty if none is set or exists.
find_vault() {
  for v in "${VAULT_LIVE_VAULT:-}" "${SECOND_BRAIN_DIR:-}"; do
    if [ -n "$v" ]; then
      if win; then v="$(cygpath -u "$v" 2>/dev/null || echo "$v")"; fi
      case "$v" in "~"|"~/"*) v="$HOME${v#\~}" ;; esac
      echo "$v"; return
    fi
  done
  for v in "$HOME/second_brain" "/c/second_brain"; do
    if [ "$v" = "/c/second_brain" ] && ! win; then continue; fi
    if [ -d "$v" ]; then echo "$v"; return; fi
  done
}

echo "3) vault-live hooks"
VAULT="$(find_vault)"
if [ -n "$VAULT" ] && [ -f "$VAULT/beyin.py" ]; then
  run_step "vault-live install-hooks" bash "$HERE/skills/vault-live/scripts/vault-live.sh" install-hooks
else
  skip "no vault with beyin.py found (${VAULT:-none of \$VAULT_LIVE_VAULT, \$SECOND_BRAIN_DIR, ~/second_brain}); vault-live hooks not installed"
fi

echo "4) agent-budget guard hook"
run_step "agent-guard install-hook" bash "$HERE/skills/agent-budget/scripts/agent-guard.sh" install-hook

echo "5) disk-cleanup hook (macOS only)"
if [ "$(uname -s)" = "Darwin" ]; then
  run_step "disk-cleanup install-hook" bash "$HERE/skills/disk-cleanup/scripts/disk-cleanup.sh" install-hook
else
  skip "macOS only; disk-cleanup hook not installed on this system"
fi

echo "6) Codex CLI and the official Codex plugin (optional)"
if [ "$NO_CODEX" = 1 ]; then
  skip "skipped (--no-codex)"
else
  if command -v codex >/dev/null 2>&1; then ok "codex on PATH: $(codex --version 2>/dev/null)"
  elif command -v npm >/dev/null 2>&1; then
    if npm install -g @openai/codex >/dev/null 2>&1; then ok "installed $(codex --version 2>/dev/null)"
    else warn "npm install -g @openai/codex failed (permissions? on macOS try: brew install codex)"; fi
  else skip "no npm: Codex CLI not installed (install Node.js, then rerun)"; fi
  if command -v codex >/dev/null 2>&1; then
    if codex login status </dev/null 2>&1 | grep -qi "logged in"; then ok "$(codex login status </dev/null 2>&1 | head -1)"
    else warn "Codex not logged in: run  codex login  and sign in with your ChatGPT account"; fi
  fi
  if ! command -v claude >/dev/null 2>&1; then
    skip "claude CLI not on PATH; in Claude Code run: /plugin marketplace add openai/codex-plugin-cc  then  /plugin install codex@openai-codex"
  elif claude plugin list 2>/dev/null | grep -q "codex@openai-codex"; then ok "codex@openai-codex plugin installed"
  else
    claude plugin marketplace add openai/codex-plugin-cc >/dev/null 2>&1
    if claude plugin install codex@openai-codex >/dev/null 2>&1; then ok "codex@openai-codex plugin installed"
    else warn "plugin install failed; try /plugin install codex@openai-codex inside Claude Code"; fi
  fi
fi
echo "Done. Start a new Claude Code session to pick up skills, agents and hooks."
