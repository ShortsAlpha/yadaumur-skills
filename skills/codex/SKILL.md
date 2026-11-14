---
name: codex
description: Delegate work to OpenAI Codex (GPT models via the user's ChatGPT login) as a sub-agent, and generate images with Codex's built-in gpt-image tool. Use when the user asks to "ask Codex/GPT/ChatGPT", wants a second opinion or independent review from another model, wants a task run in parallel by GPT, or wants an image generated with ChatGPT/OpenAI/gpt-image (illustration, thumbnail art, background, key art, icon). Not for Gemini/Antigravity.
---

# Codex as sub-agent and image generator

Codex CLI runs on the user's ChatGPT login (`~/.codex/auth.json`, shared by the ChatGPT desktop app and the npm `@openai/codex` CLI). No API key: usage counts against the ChatGPT plan's Codex limits. Always go through the wrapper scripts (Bash tool); they find `codex` on PATH or inside the desktop app, on Windows and macOS. This skill lives in the skills repo (`skills/codex/`) and is linked into `~/.claude/skills/codex` on each device by its `install.sh`.

- `bash ~/.claude/skills/codex/scripts/codex-run.sh <codex args>` runs the CLI (`--which` prints the resolved path).
- `bash ~/.claude/skills/codex/scripts/codex-image.sh "<prompt>" <out.png> [ref.png ...]` generates one image.

## Ask / delegate (text)

```bash
bash ~/.claude/skills/codex/scripts/codex-run.sh exec --skip-git-repo-check --ephemeral \
  -s read-only -C "<project dir>" -o "<scratchpad>/codex-answer.md" "<self-contained prompt>"
```
Then read the `-o` file: it holds only the final answer (stdout has the full event log, ignore it).

- `-s read-only` for questions, research and reviews. `-s workspace-write` only when Codex should edit files in `-C`; never `danger-full-access` and never the `--dangerously-*` flags.
- Codex sees nothing of this conversation. Write the prompt like a brief: goal, files to look at, constraints, the exact output format wanted. Pipe long context via a file path it can read, not a giant argv.
- `--output-schema <schema.json>` forces a JSON answer when you need to parse it.
- `-i a.png` attaches images as input (e.g. "critique this thumbnail"). Put `-i` after the prompt: it takes several values and would swallow a prompt that follows it.
- Model and effort come from `~/.codex/config.toml`; override with `-m <model>` or `-c model_reasoning_effort=medium` for cheap quick asks.
- A simple ask takes ~10 s; a real task can take minutes. Anything likely over ~2 min: run the Bash call with `run_in_background: true` and keep working; several independent asks can run in parallel this way.
- If Codex edited files, review its diff yourself before reporting; its summary is not proof.

Good uses: second opinion on a design or bug, adversarial code review, parallel research, a self-contained chunk of implementation in its own folder. Don't give two writers (you and Codex) the same files at the same time.

## Images

```bash
bash ~/.claude/skills/codex/scripts/codex-image.sh "flat vector fox on books, cream background, square" "<dir>/fox.png"
```
- Takes ~1–2 min; prints the absolute path. Look at the result with Read before handing it on; regenerate with a sharper prompt if it misses.
- Put size/aspect, style, text to render (exact wording in quotes), and what to avoid in the prompt. Reference images: extra args after the output path.
- For several images, run several calls in background in parallel, each with its own output file.
- Image generation uses Codex limits faster than text. Originals also accumulate under `~/.codex/generated_images/` (~1 MB each); mention it if the user generates a lot.

## Gemini / Nano Banana

Not automated. The Antigravity CLI route was tried and removed on 2026-11-14 (a small weekly image quota, plus Google terms). The user generates Gemini images by hand in the Gemini app; when they want that, write them ready-to-paste prompts instead of generating. This skill's image script is the only automated image route while the ChatGPT plan lasts.
