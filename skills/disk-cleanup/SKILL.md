---
name: disk-cleanup
description: Find and remove regenerable disk clutter on macOS that coding agents and dev tools leave behind (Xcode DerivedData, simulator clones and runtimes, npm/pnpm/Homebrew/pip and other caches, node_modules and build folders of inactive projects, stale agent worktrees and temp dirs), always asking the user before deleting anything. Use when the disk-cleanup hook says a storage check is due, when the user mentions disk space or storage ("disk doldu", "depolama", "yer aç", "yer kalmadı", "temizlik yap", "storage full"), when a command fails with "No space left on device" / ENOSPC, and at the end of a long task that built or installed a lot (look back at what it left). macOS only.
---

# Disk cleanup (macOS)

Agents and dev tools fill the disk with things that come back on their own: build products, caches, simulator clones, dependency folders. This skill looks back at them, shows the user what can go and how much it frees, and deletes only what the user approves. Scanning is read-only and needs no permission; deleting always does.

Script: `bash ~/.claude/skills/disk-cleanup/scripts/disk-cleanup.sh <command>`. State and settings are machine-local in `~/.claude/disk-cleanup/` (`config.json` is the user's, editable).

## When

- The `disk-cleanup` hook added "storage check due" to the context: finish the user's current request first, then do this skill at a natural pause. A **critical** (very low disk) notice is told to the user right away, before continuing other work.
- The user asks about disk space, or something failed for lack of space.
- At the end of a long task that ran builds, installs, simulators, renders or many worktrees: offer a look-back scan in one line.

## Steps

1. **Scan** (read-only, up to ~2.5 minutes): `disk-cleanup.sh scan` (`--json` when you need exact values). Run it with a 10-minute Bash timeout (`timeout: 600000`) or in the background; the default 2-minute timeout can kill it. In the Claude desktop app also call `mcp__ccd_host__get_storage_usage` (deferred; load with ToolSearch) for the app's own data: worktrees, conversation history, scratch workspaces.
2. **Show** the user, in the user's language and compactly:
   - free space now, and how it changed since the last scan/cleanup;
   - one table per tier with the items, sizes and a few words on what happens after deletion;
   - the growth line from the scan ("since last time DerivedData +12 GB"): that is the look-back, name which work produced it if you know (this session's builds, a render, an install).
   - Tiers: **A** = cache, comes back on its own (only costs a re-download/rebuild); **B** = rebuildable but costs real time, or a judgment call (inactive projects' `node_modules`/builds, unused simulator runtimes, browser/model downloads, stale agent worktrees); **C** = does not come back, or belongs to another app (Xcode Archives, transcripts, the Trash, big old Downloads): listed so the user knows.
   - Sizes are estimates: `du` counts APFS clones twice, so the real gain is usually smaller. Say so once.
3. **Ask** with `AskUserQuestion` (multiSelect), recommendation first, e.g. "All of A (31 GB, recommended)", then the biggest B items one per option with their size and cost ("node_modules, my-app, 2.1 GB: comes back on the next npm install"). Don't offer C items in the same question: mention them in a line; if the user wants one gone, confirm that exact item separately and say it is permanent and cannot be undone.
4. **Delete** what was chosen:
   - `disk-cleanup.sh clean <id>...` first (dry run: shows the exact paths and commands), then the same with `--yes`, again with a 10-minute timeout or in the background (tool commands such as `brew cleanup` can be slow). Tier C only with `--yes --permanent`, after the separate confirmation above.
   - Items skipped as "in use" (Xcode or a simulator running, a package manager busy) or for safety (a worktree with uncommitted changes or git-ignored files that aren't build artifacts, such as `.env`) are not errors: tell the user why and offer them again later. "No longer matches its rule: rescan" means the folder changed since the scan (for example git now tracks it): rescan before offering it again.
   - A delete that fails with "Operation not permitted" (macOS privacy protection or a sandbox): report it and let the user decide; don't work around permissions.
   - The desktop app's idle worktrees go through `mcp__ccd_host__clean_up_worktrees` (the app shows its own approval card); never remove the app's data folders by hand.
5. **Report** the real freed space (the script measures the free-space difference) next to the estimate, and anything skipped or failed with the reason.
6. If the user doesn't want a cleanup now: `disk-cleanup.sh snooze` (default 7 days; `--days N`). That also stops the hook from asking again until then.

## Prevention (suggest; change settings only on the user's yes)

- Large `~/.claude/projects` (transcripts): Claude Code's `cleanupPeriodDays` setting (default 30) deletes old ones by itself; a lower value keeps it smaller.
- After an Xcode update: remove unavailable simulators and runtimes no device uses; keep one current iOS runtime unless the user needs an older one for testing.
- Desktop app: Settings › Storage › "Clean up inactive sessions" (same as `clean_up_worktrees`).
- If one item keeps coming back huge, say which tool produces it rather than deleting it every week.

## Rules

- Delete only through `clean` with ids from the latest scan, or through the app/tool's own command. No hand-written `rm -rf`, no `sudo`, nothing under `/System` or `/Library` except via `xcrun simctl`.
- Never delete project sources, media, documents, `.git` folders or anything the scan did not list. A project's build folders are listed only for inactive projects unless the user asks (`scan --include-active`).
- Approval is per run: an earlier "yes" does not cover a later scan.
- "Don't suggest this again" → add the path to `exclude` or the item id to `hide` in `~/.claude/disk-cleanup/config.json`. Thresholds (`interval_days`, `min_free_gb`, `stale_days`, `project_roots`, ...) live there too; `disk-cleanup.sh config` prints them.
- `disk-cleanup.sh status` shows free space, the last scan/cleanup, whether a check is due and the hook state.
- Not on macOS: say this skill is macOS-only and stop.
