---
name: vault-live
description: Keep the second-brain vault current DURING long project sessions, without waiting for the user to say "bitir". Checkpoint the project's version/branch note, card `## Durum`, YouTube episode notes and receipts at every milestone (meaningful commit, feature done, bug verified fixed, decision made, TestFlight/review/release, user correction) and whenever the vault-live hook says a checkpoint is due or the context was just compacted. Use in any session working on a project or YouTube channel that has (or should have) a vault card.
---

# vault-live: the vault stays current while you work

The user works in very long sessions (a whole app version can be 150 hours in one chat, with many context compactions). Never leave vault updates to the end. Write small, correct updates at milestones, so a compaction or a crash loses nothing.

Vault (`<vault>` below): `$SECOND_BRAIN_DIR` if set, else `~/second_brain`, else `C:\second_brain` on Windows (the hook also honours `VAULT_LIVE_VAULT`). CLI `beyin` = `<vault>/beyin.py` run with Python 3.9+ (Windows `py -3 <vault>/beyin.py`, macOS `/opt/homebrew/bin/python3 <vault>/beyin.py`). Rules of record: the user's global CLAUDE.md and `rehber/proje-kartlari.md` in the vault (read the latter once per session if you haven't).

Expected vault layout (Turkish folder names, used as is): `projects/<folder>/<folder>.md` (card), `projects/<folder>/Kurallar.md`, `projects/<folder>/surumler/`, `projects/<folder>/dallar/`, `projects/Projeler.md` (map), `youtube/<kanal>/`, `knowledge/concepts/`, `rehber/`, `📥 000-Inbox/`.

## 1. At session start (once)

1. In the vault: `git pull --rebase --autostash`, `beyin sync`, `beyin context "<proje> <konu>"` (Turkish query).
2. Find the targets and keep them in mind for the whole session:
   - **Card:** `projects/<folder>/<folder>.md` (folder = project folder name; check aliases and `projects/Projeler.md` if not found). Missing → create it now (rules in `proje-kartlari.md`). YouTube: `youtube/<kanal>/<kanal>.md` + the episode note in `bolumler/`.
   - **Line note:** which version/branch is being worked on (user's words → git branch → `MARKETING_VERSION`/package version). `projects/<folder>/surumler/<folder> <version>.md` or `dallar/<folder> <branch>.md`. Missing and real work is starting → create it (`status: geliştiriliyor`) and link it under the card's `## Sürümler`/`## Dallar`.
   - **Receipt session id:** `bash ~/.claude/skills/vault-live/scripts/vault-live.sh receipt-session` (same id Beyin's bridge injects as `Receipt session=`; never invent one). If the session's folder is outside Beyin's project roots (no `Receipt session=` line was injected at start), Beyin has no session under that id: skip receipts and rely on the notes.
3. Read the card, your line note and the previous version's `## Açık kalan`.

## 2. When to checkpoint

Checkpoint right after any of these, before moving on:
- a meaningful commit/push, a feature finished, a bug fixed **and verified**;
- a product/architecture decision, a scope change, a plan agreed with the user;
- a version state change: TestFlight build, sent to review, rejected/approved, released (`status` changes);
- a user correction or new rule (→ project `Kurallar.md` or the card, as `proje-kartlari.md` says);
- the **vault-live hook** says a checkpoint is due, or you see that the context was just compacted;
- before you tell the user a long stretch of work is done.

Not for every reply, small fixes, or exploration. One checkpoint can cover several milestones if they happened together.

## 3. What to write (small, factual, Turkish prose; code, commands, terms stay in their own language)

- **Line note (always, if work happened on a line):** a dated bullet in `## Çalışma notları` (`- 2026-11-14: …`, 1–3 sentences: what changed, why, what's next). Keep `## Eklendi / Değişti / Düzeltildi` current in App Store "What's New" style, and `## Açık kalan` true (remove done items). Bump `updated`.
- **Card `## Durum` (only lasting changes):** feature shipped, version state change, repo/name change, architecture decision, project paused. Rewrite the relevant bullet in place (current state, dated), don't append a log; link the line note. Bump `updated`.
- **YouTube:** progress goes into the episode note (`## Üretim` dated bullets, `status`), the channel card keeps at most one line per episode.
- **Lasting learning** (a gotcha, a technique, a decision with reasons that outlives this project) → `knowledge/concepts/` via `note-create`.
- Never: code, transcripts, reasoning dumps, tool logs, secrets, `.env` values, API keys, personal data beyond what the vault already holds.

**How:** existing notes only through `beyin note-read` (get `sha256`) then `beyin note-edit --file <req.json>` with `{"source","expected_sha256","edits":[{"old","new"}]}` (each `old` unique); new cards/line notes with `note-edit` + `expected_sha256: null` + `content`; `knowledge/` with `note-create`. Request files go in `<vault>/.beyin-note-write/`, delete them afterwards. On exit 2 (`conflict`): re-read and rebuild the edit; never fall back to direct file writes. In orchestrator mode the main agent writes these notes itself (they are text, not code), keeping each edit small.

## 4. Receipts

- Major milestones only (version state change, a feature shipped, end of a working day, the user leaving): `beyin receipt --harness claude --session <receipt-session> --ref <line note or card path>` with a short summary that ends in `Öğrenilen: <one sentence>` or `Öğrenilen: yok (<reason>)`. `event_id` must end with the first 8 chars of the session id and be unique per receipt (e.g. `<folder>-<YYYYMMDD>-<n>-<sid8>`).
- No receipt for routine checkpoints: the line note already holds the progress.

## 5. After writing

1. `bash ~/.claude/skills/vault-live/scripts/vault-live.sh mark` (tells the hook the vault is current: resets its commit/time counters for every project this session is tracking). If the hook's message shows a `mark --session <id>` command, run exactly that one; it works from any folder.
2. Don't run git in the vault yourself: the vault's own auto-sync commits and pushes every 15 minutes, and two git processes would collide. Exception: when the user is about to switch devices or asks, run `git add -A`, `git commit -m "<özet>"`, `git pull --rebase`, `git push` once.
3. Tell the user nothing, or at most one short line ("Vault güncellendi: my-app 1.3 notu, kart Durum") when a card/state changed. Never interrupt the work for it.

## The hook (what it does, for your understanding)

`vault-live` hooks run on every Stop and on SessionStart after a compaction, in project repos only (not the vault, not scratch folders, not the home folder; non-git folders only under the `roots` listed in `~/.claude/vault-live/config.json`). They count this line's new commits and active working minutes (idle gaps and resumes don't count) since the last `mark`; when ≥ 3 commits or ≥ 90 active min with new changes have accumulated, they stop the turn once with a short "vault checkpoint due" reason, and after a compaction they inject a reminder to checkpoint first. Subagents never get these prompts: only the main agent writes the vault. Do the checkpoint, run `mark`, then finish what you were doing. If the hook fires but nothing worth writing happened, just run `mark`.
