# yadaumur-skills

A personal set of [Claude Code](https://docs.claude.com/en/docs/claude-code) skills, four lean subagents and three hooks, published as-is. They are what I use every day, not a polished product: read them, take what is useful, adapt the rest.

- **Skills:** `agent-budget`, `usage-limits`, `codex`, `vault-live`, `devir`, `disk-cleanup`
- **Agents:** `coder`, `scout`, `reviewer`, `researcher`
- **Hooks:** the agent-budget guard (PreToolUse), the vault-live checkpoint hooks (Stop, SessionStart) and the disk-cleanup reminder (SessionStart, UserPromptSubmit; macOS only)

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
5. on macOS only, installs the disk-cleanup reminder hook into `~/.claude/settings.json` (elsewhere it prints a skip line);
6. optionally makes sure the Codex CLI is installed and logged in and installs OpenAI's official Claude Code plugin (`codex@openai-codex`). This step never fails the install: it is skipped with a message when npm or the `claude` CLI is missing, or with `--no-codex`.

It is idempotent: a second run changes nothing. Links that point somewhere else (for example to an older checkout) are re-pointed to this clone. A real folder with content at `~/.claude/skills/<name>` or `~/.claude/agents` is never deleted: the script warns and skips it. Because the skills are linked, `git pull` in the clone updates them in place; start a new Claude Code session afterwards.

## Skills

### agent-budget

Decides, before and during multi-step work, how much quota a job may use and which model and effort each subagent gets (Haiku / Sonnet / Opus, low to xhigh), from the live Claude and Codex limits and the burn rate. Classifies the budget as Green / Yellow / Red by "pace gap" (used % minus elapsed % of each window), estimates the job size, picks model and effort per subagent from a table, and reports the plan in one line or asks when the budget is tight. A tight budget brings a warning or a question, never an automatic switch to cheaper models: those are used only when the user asks to save quota. It also holds the orchestrator rules (the main agent delegates code work to the lean agents and reads their reports) and how to write briefs for each model.

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

### disk-cleanup

macOS only. Finds what coding agents and dev tools leave on the disk, shows it, and deletes only what the user approves. `scripts/disk-cleanup.sh scan` is read-only: it measures a catalog of targets and groups them by tier. **A** is cache that comes back by itself (Xcode DerivedData of deleted or long-unbuilt projects, XCTest clones, unavailable simulators, old device support files, SwiftUI previews, CoreSimulator caches, SwiftPM, CocoaPods, Homebrew, npm, pnpm, Yarn, Bun, pip, uv, Go, Gradle, node-gyp and Electron caches, old Claude Code temp folders). **B** can be rebuilt but costs real time or is a judgment call (DerivedData of recent projects, simulator runtimes no device uses, Playwright/Puppeteer browsers, Hugging Face models, `node_modules`/`.next`/`Pods`/`.build`/`target`/`.venv` and similar folders of projects with no activity for 30 days, stale agent worktrees under `<repo>/.claude/worktrees/`). A project is the folder that holds the artifact and its marker file (`package.json`, `Podfile`, `Cargo.toml`, ...); a `project_roots` entry itself is never one. Its activity is the newest file anywhere in its source tree plus the repo's git activity, including linked worktrees. Inside a git repo a folder is listed only when git ignores it and it holds no tracked file. Outside git, `build` is listed only next to an `*.xcodeproj`, `*.xcworkspace` or `build.gradle*`, because an npm project's `build/` can hold real sources. A worktree is removed only when it has no uncommitted changes and no git-ignored files other than build artifacts. **C** does not come back or belongs to another app (Xcode archives, runtimes still in use, Claude Code transcripts, Codex sessions, the Trash, files of 1 GB or more in Downloads older than 30 days); the Claude desktop VM and Time Machine local snapshots are listed for information only. Each scan also shows which items grew since the last scan and how free space changed since the last scan or cleanup. A scan takes at most about 2.5 minutes; a size not finished in time is shown as a lower bound.

```bash
bash ~/.claude/skills/disk-cleanup/scripts/disk-cleanup.sh scan [--json] [--include-active]
bash ~/.claude/skills/disk-cleanup/scripts/disk-cleanup.sh clean <id>... [--yes] [--permanent]   # dry run without --yes
bash ~/.claude/skills/disk-cleanup/scripts/disk-cleanup.sh status | snooze [--days N] | config
```

`clean` without `--yes` prints the exact paths and commands. With `--yes` it uses only ids from a scan younger than 24 hours. Before deleting, it checks each path again: the path must still match its rule, sit inside the home folder (temp items: inside `$TMPDIR/claude` or one of the user's own `/private/tmp/claude*` folders), and be neither protected nor excluded. On macOS, paths are compared case-insensitively and Unicode-normalised, and relative `exclude` / `project_roots` entries are taken from the home folder. Links are removed as links. Items are skipped while Xcode, the Simulator or their tool is running. When the tool is installed, its own command is used (`brew cleanup`, `npm cache clean`, `xcrun simctl`, `git worktree remove`). Tier C also needs `--permanent`, whether the item was C in the scan or is C now. The real gain is measured as the change in free space, because `du` counts APFS clones twice. Settings live in `~/.claude/disk-cleanup/config.json`: `interval_days` 7, `min_free_gb` 25, `min_free_pct` 10, `critical_free_gb` 10, `nag_hours` 20, `critical_nag_hours` 2, `stale_days` 30, `project_roots` (default: whichever of `~/Developer`, `~/Projects`, `~/code`, `~/src`, `~/dev`, `~/repos` exist), `exclude` (paths never touched) and `hide` (item ids never listed).

- **Triggers:** the hook's "storage check due" note, the user asking about disk space, or a "No space left on device" error.
- **Depends on:** macOS; Xcode command line tools for the simulator items; each package manager only for its own command (without it, the cache folder is removed directly).
- **Hook:** SessionStart and UserPromptSubmit (see Hooks).

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

The hooks are written into `~/.claude/settings.json` by their own installers, which `install.sh` calls: the guard always, the vault-live hooks only when a vault with `beyin.py` exists, the disk-cleanup hook only on macOS. They call the scripts through the `~/.claude/skills/...` links, so they follow the clone. All of them fail open: any error allows the action silently. Existing hooks from other tools are left alone, and a backup of `settings.json` (`settings.json.agent-guard.bak` / `settings.json.vault-live.bak` / `settings.json.disk-cleanup.bak`) is written before a change.

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

**disk-cleanup** (`SessionStart`, `UserPromptSubmit`; macOS only): reads only the free space and the state file, never scans. It adds a "storage check due" note to the context when the last scan or cleanup is `interval_days` old or free space is below `min_free_gb` / `min_free_pct`. The note tells the agent to finish the current request first and then offer the skill. It stays quiet while snoozed, and at most once per `nag_hours` across all sessions. Below `critical_free_gb` it ignores the snooze, repeats every `critical_nag_hours` and asks the agent to tell the user right away. It ignores `SessionStart` after a compaction. State is machine-local in `~/.claude/disk-cleanup/`.

```bash
bash ~/.claude/skills/disk-cleanup/scripts/disk-cleanup.sh status
bash ~/.claude/skills/disk-cleanup/scripts/disk-cleanup.sh uninstall-hook
```

## Uninstall

```bash
bash ~/.claude/skills/agent-budget/scripts/agent-guard.sh uninstall-hook
bash ~/.claude/skills/vault-live/scripts/vault-live.sh uninstall-hooks
bash ~/.claude/skills/disk-cleanup/scripts/disk-cleanup.sh uninstall-hook   # macOS
# then remove the links (only the links, not their targets):
#   macOS:   rm ~/.claude/skills/{agent-budget,codex,devir,disk-cleanup,usage-limits,vault-live} ~/.claude/agents
#   Windows: in PowerShell, (Get-Item -Force <link>).Delete() for each of them
```

## Tests

```bash
# from the repo root; Windows: py -3 instead of python3
python3 skills/agent-budget/scripts/test_agent_guard.py
python3 skills/vault-live/scripts/test_vault_live.py
python3 skills/disk-cleanup/scripts/test_disk_cleanup.py
```

The suites use temporary folders only; they never touch the real `~/.claude`, `settings.json` or vault. The disk-cleanup suite runs against a synthetic home with fake free space and never runs `xcrun`, `brew` or `npm` (git runs only on temporary repos). Its test-only environment overrides take effect only with `DISK_CLEANUP_TEST=1`.

## Known limits

- `vault-live` and `devir` need the author's private vault tooling (`beyin.py`) and vault layout; without them they are documentation of an approach, not working tools.
- The skills assume the Claude desktop app for live Claude limits; in the terminal they fall back to asking you to run `/usage`.
- Model names in the skills (Opus, Sonnet, Haiku, Fable) and their rules reflect my own setup at the time of writing.
- The agent guard's Fable override is a plain text match: a negation ("don't use Fable") also unlocks it. Workflows that call `agent()` through an alias are not recognised.
- `install.sh` is tested on Windows (Git Bash) and written for macOS bash 3.2; Linux should work but is untested.
- `disk-cleanup` has so far been tested only with synthetic fixtures on Windows, not on a real Mac. Its `du` sizing, the `xcrun simctl` / `brew` / `npm` / `tmutil` commands, the process check and the Full Disk Access cases still need checking on a real Mac.

## License

MIT, see [LICENSE](LICENSE).
