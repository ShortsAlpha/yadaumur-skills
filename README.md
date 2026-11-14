# yadaumur-skills

A personal set of [Claude Code](https://docs.claude.com/en/docs/claude-code) skills, four lean subagents and two hooks, published as-is. They are what I use every day, not a polished product: read them, take what is useful, adapt the rest.

- **Skills:** `agent-budget`, `usage-limits`, `codex`, `vault-live`, `devir`
- **Agents:** `coder`, `scout`, `reviewer`, `researcher`
- **Hooks:** the agent-budget guard (PreToolUse) and the vault-live checkpoint hooks (Stop, SessionStart)

Two of the skills, `vault-live` and `devir`, are built around my own notes vault (a Git-synced Markdown "second brain") and its private `beyin.py` CLI (`note-read`, `note-edit`, `receipt`, `sync`, `context`). That CLI is not part of this repo, so those two are a reference for building something similar, not plug-and-play. The other skills and the agents work on their own.

I work in Turkish, so some example lines in the skills (status lines, templates, trigger phrases) are Turkish. Where they define the output the skill should produce, they are kept as they are.

## Requirements

- Claude Code (the desktop app's Code tab gives `usage-limits` and `agent-budget` the live limit numbers; the terminal `claude` works with fewer features)
- Bash: macOS bash 3.2+ or Git Bash on Windows
- Python 3.9+, standard library only (Windows: `py -3`; macOS: `/opt/homebrew/bin/python3` or any `python3` >= 3.9)
- Optional: Node.js 18+ and the Codex CLI with a ChatGPT login (for `codex` and the Codex half of `usage-limits`)

## Install

```bash
git clone https://github.com/ShortsAlpha/yadaumur-skills.git claude-skills
bash claude-skills/install.sh            # or: bash claude-skills/install.sh --no-codex
```

`install.sh` works from wherever the repo is cloned. It:

1. links each `skills/<name>` folder to `~/.claude/skills/<name>` (symlink on macOS, junction on Windows);
2. links `agents/` to `~/.claude/agents`;
3. installs the vault-live hooks into `~/.claude/settings.json`, but only when a vault with `beyin.py` is found (same lookup as the hook: `$VAULT_LIVE_VAULT`, else `$SECOND_BRAIN_DIR`, else `~/second_brain`, else `C:\second_brain` on Windows); otherwise it skips them with a one-line message;
4. always installs the agent-budget guard hook into `~/.claude/settings.json` (it does not need a vault);
5. optionally makes sure the Codex CLI is installed and logged in and installs OpenAI's official Claude Code plugin (`codex@openai-codex`). This step never fails the install: it is skipped with a message when npm or the `claude` CLI is missing, or with `--no-codex`.

It is idempotent: a second run changes nothing. Links that point somewhere else (for example to an older checkout) are re-pointed to this clone. A real folder with content at `~/.claude/skills/<name>` or `~/.claude/agents` is never deleted: the script warns and skips it. Because the skills are linked, `git pull` in the clone updates them in place; start a new Claude Code session afterwards.

## Skills

### agent-budget

Decides, before and during multi-step work, how much quota a job may use and which model and effort each subagent gets (Haiku / Sonnet / Opus, low to xhigh), from the live Claude and Codex limits and the burn rate. Classifies the budget as Green / Yellow / Red by "pace gap" (used % minus elapsed % of each window), estimates the job size, picks model and effort per subagent from a table, and reports the plan in one line or asks when the budget is tight. It also holds the orchestrator rules (the main agent delegates code work to the lean agents and reads their reports) and how to write briefs for each model.

- **Triggers:** the start of any task that will spawn subagents, run a workflow or a big Codex/image batch, or take more than a few minutes; again before each new wave of subagents.
- **Depends on:** `usage-limits`; `devir` for the handoff threshold; optionally `codex` for offloading.
- **Hook:** `scripts/agent_guard.py` (see Hooks).
- Optional calibration log: `calibration-<hostname>.log` next to the skill. It is per-device local data (one file per machine, appended after big jobs) and is gitignored, so it never leaves the device.

### usage-limits

Reads the live subscription limits: Claude (5-hour, weekly all-models, weekly per-model windows, extra usage) through the desktop app's `get_usage` tool, and Codex (5-hour and weekly windows, reset credits) through the Codex app-server (`scripts/codex-limits.sh`, read-only, spends no usage).

- **Triggers:** questions about limits/usage/quota, and before heavy work; other skills call it for pacing.
- **Depends on:** the Claude desktop app for Claude limits; Node.js 18+ and a logged-in Codex for Codex limits.

### codex

Uses OpenAI Codex as a sub-agent (second opinions, adversarial review, parallel research, contained implementation) and generates images with Codex's built-in image tool, through the user's ChatGPT login (no API key). `scripts/codex-run.sh` finds the CLI on PATH or inside the ChatGPT/Codex desktop app; `scripts/codex-image.sh` generates one image.

- **Triggers:** "ask Codex/GPT", a second opinion from another model, or an image from ChatGPT/OpenAI.
- **Depends on:** the Codex CLI (npm `@openai/codex` or the desktop app) logged in with ChatGPT.

### vault-live

Keeps a Markdown notes vault current during very long sessions: checkpoint the project's version/branch note, project card and receipts at milestones (meaningful commit, feature done, decision, release, user correction), and whenever its hook says a checkpoint is due or the context was just compacted.

- **Triggers:** any session working on a project that has (or should have) a card in the vault.
- **Depends on:** the vault and its `beyin.py` CLI (not included). Vault location: `$SECOND_BRAIN_DIR`, else `~/second_brain`, else `C:\second_brain` on Windows (`VAULT_LIVE_VAULT` overrides it for the hook). It expects the vault layout described in its `SKILL.md` (Turkish folder names such as `projects/<folder>/`, `📥 000-Inbox/`).
- **Hooks:** Stop and SessionStart (see Hooks).

### devir

"Devir" is Turkish for handover. Writes one handoff note per project in the vault when a Claude limit is about to run out, when switching account or device, or when the context is nearly full; a new session picks it up and continues. Also sends a push notification when a decision is needed or a long job finished.

- **Triggers:** the agent-budget handoff threshold, a near-full context, or the user saying they are switching ("devir", "devret", ...).
- **Depends on:** the same vault and `beyin.py` CLI as `vault-live`; the vault-live SessionStart hook points new sessions at an open handoff note.

## Agents

Lean subagent definitions in `agents/`, linked as `~/.claude/agents`. Each loads only the tools it needs, so it starts with a much smaller context than `general-purpose`.

| Agent | Role | Tools | Default |
|---|---|---|---|
| `coder` | writes and edits code/scripts from a self-contained brief, runs the relevant tests | Read, Edit, Write, Glob, Grep, Bash, PowerShell | opus · high |
| `scout` | read-only search and summaries of code, files, logs | Read, Glob, Grep, Bash, PowerShell | opus · high |
| `reviewer` | independent check of another agent's work: diff, tests, bugs | Read, Glob, Grep, Bash, PowerShell | opus · xhigh |
| `researcher` | web and documentation research with sources | WebSearch, WebFetch, Read, Grep, Glob | opus · high |

`agent-budget` passes an explicit model and effort on every spawn; the frontmatter defaults are only a fallback.

## Hooks

Both hooks are written into `~/.claude/settings.json` by their own installers (called by `install.sh`; the vault-live hooks only when a vault with `beyin.py` exists, the guard always). They call the scripts through the `~/.claude/skills/...` links, so they follow the clone. Both fail open: any error allows the action silently. Existing hooks from other tools are left alone, and a backup of `settings.json` (`settings.json.agent-guard.bak` / `settings.json.vault-live.bak`) is written before a change.

**Agent-budget guard** (`PreToolUse`, matcher `Agent|Task|Workflow`): denies a subagent spawn without an explicit `model`, a Fable model unless the user's latest message asks for Fable, a fork while the main session runs on Fable, and a workflow script that puts an agent on Fable.

```bash
AGENT_GUARD_OFF=1                                                        # emergency switch: allow everything
bash ~/.claude/skills/agent-budget/scripts/agent-guard.sh status
bash ~/.claude/skills/agent-budget/scripts/agent-guard.sh uninstall-hook
```

**vault-live** (`Stop`, `SessionStart`): counts new commits and active working minutes per project since the last `mark`; at 3 commits or 90 active minutes with changes it stops the turn once with "vault checkpoint due". After a compaction it asks for a checkpoint; on startup it points at an open handoff note. It ignores the vault itself, the home folder, scratch folders and subagents. Non-git folders count as projects only under the `roots` listed in `~/.claude/vault-live/config.json` (e.g. `{"roots": ["~/code"]}`; thresholds can be set there too). State is machine-local in `~/.claude/vault-live/`.

```bash
bash ~/.claude/skills/vault-live/scripts/vault-live.sh status
bash ~/.claude/skills/vault-live/scripts/vault-live.sh uninstall-hooks
```

## Uninstall

```bash
bash ~/.claude/skills/agent-budget/scripts/agent-guard.sh uninstall-hook
bash ~/.claude/skills/vault-live/scripts/vault-live.sh uninstall-hooks
# then remove the links (only the links, not their targets):
#   macOS:   rm ~/.claude/skills/{agent-budget,codex,devir,usage-limits,vault-live} ~/.claude/agents
#   Windows: in PowerShell, (Get-Item -Force <link>).Delete() for each of them
```

## Tests

```bash
# from the repo root; Windows: py -3 instead of python3
python3 skills/agent-budget/scripts/test_agent_guard.py
python3 skills/vault-live/scripts/test_vault_live.py
```

Both suites use temporary folders only; they never touch the real `~/.claude`, `settings.json` or vault.

## Known limits

- `vault-live` and `devir` need the author's private vault tooling (`beyin.py`) and vault layout; without them they are documentation of an approach, not working tools.
- The skills assume the Claude desktop app for live Claude limits; in the terminal they fall back to asking you to run `/usage`.
- Model names in the skills (Opus, Sonnet, Haiku, Fable) and their rules reflect my own setup at the time of writing.
- The agent guard's Fable override is a plain text match: a negation ("don't use Fable") also unlocks it. Workflows that call `agent()` through an alias are not recognised.
- `install.sh` is tested on Windows (Git Bash) and written for macOS bash 3.2; Linux should work but is untested.

## License

MIT, see [LICENSE](LICENSE).
