#!/usr/bin/env bash
# agent-guard: runs agent_guard.py (next to this file) with a suitable Python 3.9+.
#   Windows Git Bash: py -3   macOS: /opt/homebrew/bin/python3, else python3 >= 3.9
# Usage: bash ~/.claude/skills/agent-budget/scripts/agent-guard.sh install-hook|uninstall-hook|status
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT="$DIR/agent_guard.py"
case "$(uname -s)" in
  MINGW*|MSYS*|CYGWIN*)
    if command -v py >/dev/null 2>&1; then exec py -3 "$SCRIPT" "$@"; fi ;;
esac
for py in /opt/homebrew/bin/python3 python3 python; do
  if command -v "$py" >/dev/null 2>&1 &&
     "$py" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' >/dev/null 2>&1; then
    exec "$py" "$SCRIPT" "$@"
  fi
done
echo "agent-guard: no Python 3.9+ found (Windows: install Python so 'py -3' works; macOS: brew install python)" >&2
exit 1
