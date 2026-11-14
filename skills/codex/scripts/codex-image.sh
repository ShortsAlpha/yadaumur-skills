#!/usr/bin/env bash
# Generate one image with Codex's built-in image tool (gpt-image, billed to the ChatGPT plan, no API key).
# Usage: codex-image.sh "<prompt>" <out.png> [reference-image ...]
# Prints the absolute path of the saved PNG on success. Takes ~1-2 minutes.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"

if [[ $# -lt 2 ]]; then echo "usage: codex-image.sh \"<prompt>\" <out.png> [ref-image ...]" >&2; exit 2; fi
PROMPT="$1"; OUT="$2"; shift 2

OUTDIR="$(cd "$(dirname "$OUT")" 2>/dev/null && pwd || { mkdir -p "$(dirname "$OUT")" && cd "$(dirname "$OUT")" && pwd; })"
NAME="$(basename "$OUT")"
LOG="$(mktemp)"; LAST="$(mktemp)"

IMGARGS=()
for r in "$@"; do IMGARGS+=(-i "$r"); done

INSTR="\$imagegen ${PROMPT}

Use the built-in image generation tool (never an API script). If reference images are attached, use them as visual references. Generate exactly one image, copy the resulting PNG into the current working directory as ${NAME} (overwrite if present), and reply with only its absolute path."

if ! bash "$HERE/codex-run.sh" exec --skip-git-repo-check --ephemeral -s workspace-write \
      -C "$OUTDIR" -o "$LAST" "$INSTR" ${IMGARGS[@]+"${IMGARGS[@]}"} >"$LOG" 2>&1; then
  # prompt must come before -i: --image takes multiple values and would swallow it
  echo "codex-image: codex exec failed; log tail:" >&2; tail -30 "$LOG" >&2; exit 1
fi

if [[ -f "$OUTDIR/$NAME" ]]; then
  echo "$OUTDIR/$NAME"
else
  echo "codex-image: no file at $OUTDIR/$NAME. Codex said:" >&2; cat "$LAST" >&2; tail -30 "$LOG" >&2; exit 1
fi
rm -f "$LOG" "$LAST"
