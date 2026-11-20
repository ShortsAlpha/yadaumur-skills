#!/usr/bin/env bash
# disk-cleanup: runs disk_cleanup.py (next to this file) with a suitable Python 3.9+.
#   Windows Git Bash: py -3   macOS: /opt/homebrew/bin/python3, else python3 >= 3.9
# Usage: bash ~/.claude/skills/disk-cleanup/scripts/disk-cleanup.sh
#          scan [--json] [--include-active] | clean <id>... [--yes] [--permanent] | status |
#          snooze [--days N] | config | hook | install-hook | uninstall-hook
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT="$DIR/disk_cleanup.py"
case "$(uname -s)" in
  MINGW*|MSYS*|CYGWIN*)
    if command -v py >/dev/null 2>&1; then exec py -3 "$SCRIPT" "$@"; fi ;;
esac
for py in /opt/homebrew/bin/python3 /usr/local/bin/python3 python3 python; do
  found="$(command -v "$py" 2>/dev/null)" || continue
  # Without the Command Line Tools, macOS's /usr/bin/python3 is a stub that pops an install dialog.
  if [ "$found" = /usr/bin/python3 ] && ! /usr/bin/xcode-select -p >/dev/null 2>&1; then continue; fi
  if "$found" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' >/dev/null 2>&1; then
    exec "$found" "$SCRIPT" "$@"
  fi
done
echo "disk-cleanup: no Python 3.9+ found (Windows: install Python so 'py -3' works; macOS: brew install python)" >&2
exit 1
