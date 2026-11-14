---
name: agent-budget
description: Decide, before and during any multi-step task, how much quota the work may use and which model and effort each subagent gets (Haiku / Sonnet / Opus 5.5, low→xhigh), based on the user's live Claude and Codex limits and burn rate. Use at the start of every task that will spawn subagents, run a workflow, run a big Codex or image batch, or take more than a few minutes of tool work; and again before each new wave of subagents. Warns the user when the 5-hour or weekly limit may not last; otherwise decides on its own and reports the plan in one line. Never puts a subagent on Fable unless the user asks, even when the main session runs on Fable.
---

# Agent budget: pace the quota, right-size every subagent

This skill is the user's standing, explicit instruction to choose each subagent's **model and effort per spawn** (Agent tool `model` + `effort`) according to the rules below. Decide yourself; ask only in the cases listed under "When to ask".

Skip the whole procedure for trivial turns (a question, a read, a handful of tool calls, no subagents).

## 1. Read limits (fresh)

Use the `usage-limits` skill's two sources: Claude via `mcp__ccd_session_mgmt__get_usage` (load with ToolSearch `select:mcp__ccd_session_mgmt__get_usage`), Codex via `bash ~/.claude/skills/usage-limits/scripts/codex-limits.sh`. Codex only matters if you might offload to it.

## 2. Classify the budget state

For each window compute **pace gap** = used % − elapsed % of the window.
- 5-hour window: elapsed % = (300 min − minutes until reset) / 300 × 100.
- Weekly window: elapsed % = (7 d − time until reset) / 7 d × 100.
- Example: 5 h used 25 %, resets in 1 h 40 m → elapsed 67 % → gap −42 (well under pace). Weekly used 67 %, resets in 2 d 5 h → elapsed 68 % → gap −1 (on pace).

| State | Any of these |
|---|---|
| **Red** | 5 h ≥ 90 %; weekly (all models) ≥ 90 %; a window blocked; or your estimate (step 3) would push 5 h past 100 % before its reset or weekly past 95 % |
| **Yellow** | 5 h ≥ 70 %; 5 h gap ≥ +20 while used ≥ 30 %; weekly ≥ 80 %; weekly gap ≥ +10 |
| **Green** | otherwise |

The "Weekly · Fable" window only matters when a Fable-run step is involved (it is shared with Mythos on Max plans).

## 3. Estimate the job (rough, in % of the 5-hour window)

| Size | Typical shape | Rough cost |
|---|---|---|
| S | one focused change or answer, ≤ 1 subagent | 1–5 % |
| M | multi-file change, 2–4 subagents, an hour of work | 5–20 % |
| L | many subagents / a workflow / long research or batch | 20–60 %+ |

Opus xhigh subagents cost several times a Sonnet or Haiku one; parallel agents burn the window fastest. Refine estimates with the calibration log (step 7).

## 4. Pick model + effort per subagent

First: does it need a subagent at all? Every subagent pays a startup cost before doing anything (measured 2026-11-14, a no-tool one-line Haiku reply): `general-purpose` ~55k tokens, the lean types (`coder`, `scout`, `reviewer`, `researcher`) ~13k. Prefer the lean types. A non-code lookup you can finish in ~1–3 small tool calls is cheaper done inline. Code changes are always delegated (see 4b), however small: batch them instead. Otherwise spawn for breadth (many files/pages), parallelism, or an independent read.

Classify each subagent's task, not the whole job. Be stingy with Haiku: the user does not want "it's simple" to become a reflex.
- **Mechanical** (Haiku): pure retrieval or transformation with nothing to judge and an output nobody acts on blindly - list/locate files, grep for a symbol, copy data out of a known place, rename/format/convert by a fixed rule. If the subagent must interpret, choose, summarise for a decision, write prose or code, or if a mistake would be silent: it is not mechanical.
- **Standard**: a well-specified implementation, tests, docs, a clear refactor, a scoped research summary, fixing a known bug, reading code to explain how it works.
- **Hard**: ambiguous or cross-cutting design, tricky debugging, security-sensitive work, architecture or product decisions, adversarial review, anything where a wrong answer is costly or hard to spot.
- When unsure between two tiers, take the higher one.

| Task | Every budget state (default) | Only when the user asks to save quota |
|---|---|---|
| Mechanical | haiku · low | haiku · low |
| Standard | opus · high (final reviewer: xhigh) | sonnet · high (Red: sonnet · medium, or Codex) |
| Hard | opus · xhigh | opus · high (Red: opus · medium) |

- `opus` = Opus 5.5. Everything that isn't mechanical runs on Opus 5.5 in every budget state (the user's standing preference), with the speed rules below deciding high vs xhigh.
- **No automatic downgrade** (user's choice, 2026-11-15): a Yellow or Red budget never moves a subagent to Sonnet, a lower effort or Codex on its own. Use the right-hand column only after the user says so ("kota az, ucuz modellere geç", "Sonnet'le devam et"), for the work they named. The budget state still drives the warning, the Red question and the handoff (sections 6–7).

### Fable rule (user's choice, 2026-11-15: a Fable main session spawned Fable subagents on its own)
- **Subagents never run on Fable unless the user asks.** The main session running on Fable is the user's choice for talking to you, not a request for Fable subagents. Only the user's own words in this conversation count ("alt ajanları Fable'la aç", "bunu Fable yapsın"), and only for the work they named. The table above never yields Fable, whatever the main model is.
- **Always pass `model` explicitly on every spawn**: every Agent call of every type (`coder`, `scout`, `reviewer`, `researcher`, `general-purpose`, `Explore`, `Plan`, `claude`, plugin agents) and every `agent()` in a workflow. An omitted model falls back to the type's default or to the main session's model, which may be Fable.
- **No forks while the main session is on Fable**: a fork always runs on the main model and ignores `model`.
- **A hook enforces this** (`scripts/agent_guard.py`, a PreToolUse hook on Agent/Task/Workflow installed by this repo's `install.sh`): it denies a spawn without `model`, a Fable model unless the user's latest message mentions Fable, a fork while the session runs on Fable, and a workflow script that names a Fable model (in a Fable session also one with an `agent()` call that has no `model`). When it denies, fix the call (add the model from the table); don't work around it. If the user did ask for Fable in an earlier message, ask them to confirm in a new one.

### Speed rules (user's choice, 2026-11-14: xhigh everywhere made a 25-minute job take 50)
- **Effort by role:** a `coder` in a write → fix → retry loop, `scout` and `researcher` run at **high**. **xhigh** is for the final `reviewer` pass and for genuinely hard design/debugging (the "Hard" row), in every budget state.
- **One fix round:** send every finding from a review to the coder in one brief, decided up front (don't trickle follow-ups). Re-review only if the verdict was "redo" or the fixes touched risky logic (hooks, data deletion, settings, auth); a small fix gets the coder's own test run, not a second review.
- **Minor findings wait:** low-severity, non-blocking findings are not fixed now; record them in the card/line note's `## Açık kalan` and tell the user in one line.
- **Proportionate briefs:** ask for the tests and checks the risk warrants. Exhaustive extras (concurrency stress, revert-to-prove-tests, cross-version runs) only for code that runs on every turn, deletes data, or touches global settings.
- **ETA up front:** if the job will likely take > 10 minutes, say the expected duration in the plan line ("~25 dk sürer"); if it overruns noticeably, say so with a one-line reason.
- Forks inherit the main model (Fable, if that's what the session runs on), ignore `model` and `effort` and carry the whole conversation: don't use them for delegated work; spawn a named type with `model` + `effort`.
- Parallelism: no extra limit beyond the session's own guidance in Green and Yellow; in Red (after the user chose to continue) 1 subagent at a time and no workflows, unless the user says otherwise.
- If the user names a model/effort for this request, use theirs (and warn if the state is Red).

## 4b. Orchestrator mode (the user's standing rule)

The main agent the user talks to is the decision-maker between the user and the subagents. It **never writes or edits code or scripts itself**, and keeps its own context small:
- **Delegate with the lean agent types** (defined in this repo's `agents/` folder, linked as `~/.claude/agents/`; they load only the tools they need, so they start much smaller than `general-purpose`): `coder` (writes/edits code and scripts, runs tests), `scout` (read-only: find, read, explain, summarise files/logs/vault), `reviewer` (independent check of a coder's work: diff, tests, bugs), `researcher` (web/docs research). Their frontmatter defaults match the default column; still pass `model` + `effort` from the table on every spawn. If a type is missing (session started before it existed), use `general-purpose` with the same model/effort and paste the role's rules into the brief.
- **Brief like a peer:** goal, exact files/area, constraints, how to verify, and the report format. The subagent sees nothing of the conversation. Shape the brief to the model it runs on (section 4c).
- **Read reports, not files.** Don't open large files or full diffs yourself; send a `scout` to read and a `reviewer` to verify. Small reads (a config value, a short file, `git diff --stat`) are fine.
- **Verify before reporting to the user:** anything non-trivial a `coder` did gets a `reviewer` pass (same tier as the work, at least sonnet·high); report the reviewer's verdict, not the coder's claim.
- **Batch small edits:** several tiny code changes go to one `coder` brief, not one spawn each (each spawn has a fixed startup cost).
- The main agent may still write non-code text itself: vault notes, plans, short messages, prompt/config files for agents and skills.

## 4c. Brief by model (user's choice, 2026-11-15: main session on Fable 5.2, subagents on Opus / Sonnet / Haiku)

A subagent knows only its brief and its agent file, so the **facts** are the same for every model: the goal and why it matters, the exact files/area, constraints, what "done" means, the report format. Don't thin these out for a stronger model. What changes with the model is how much of the **method and the judgment calls** you settle for it:
- **Opus 5.5 (and Fable, when the user asked for it): outcome brief.** The facts, then let it choose the method. No step-by-step recipe unless the order really matters, no CAPS / "CRITICAL" / "don't be lazy" / "think hard": current models follow such text literally, and over-prescriptive prompts lower their quality. Leave judgment calls to it unless the user already made them, and pass those decisions on as decided.
- **Sonnet 5.5: outcome brief + settled choices.** The facts, plus: decide the open choices yourself (which approach, which files, what not to touch); a scope limit ("only these files; no refactors or extras, mention them in the report instead"); the exact check that proves it done (test/build command); when to stop and ask. Never write "minimize tool calls" or "use tools only if necessary": it obeys that literally and skips needed checks.
- **Haiku 5.5: exact procedure.** Mechanical tasks only (section 4). Exact inputs (paths, globs, patterns), the steps in order, an exact output template, and what to write when something is missing ("not found", never a guess). Add "Finish every item; don't add anything that wasn't asked." Keep the brief short: long prompts make it stop early. If it searches the web, give today's date.
- **For every model:**
  - Keep cautions few and scoped. A "never" rule says why and carries its exception in the same sentence; name the steps that need no approval (e.g. retrying a command that is safe to repeat). Cautions pile up across briefs, notes and reviewer conditions, and agents then hold back routine work.
  - Pass a reviewer's conditions to a coder only where they apply.
  - Ask for the report in plain, self-contained sentences, key point first, within the line limit.

**Main session on Fable 5.2** (the user's default model for talking to you): `high` effort is the right level; `xhigh` only for genuinely hard turns (gains above `high` are uneven, the cost isn't). Fable delegates readily: the "does it need a subagent" check in section 4 still applies, and don't send a reviewer to re-check your own notes or plans. Fable also draws from the regular weekly limit and costs about twice Opus per token, so in Red mention once that switching the main session to Opus 5.5 would stretch the budget (the user decides; you don't change it).

## 5. Codex as an option when Claude is tight

If Claude is Yellow/Red and Codex's 5 h window is < 70 % and not blocked, you may **offer** (in the plan line or the Red question) to send self-contained side work (second-opinion review, isolated research, image generation, a contained implementation in its own folder) to Codex via the `codex` skill. Do it only when the user agrees or asked for Codex. Map effort with `-c model_reasoning_effort=medium|high`. Keep work that needs this conversation's context on Claude.

## 6. Tell the user (one line), or ask

**Inform (default, no question):** at the start of the job, one Turkish line with limits and the plan, e.g.
> Kota: 5 saat %25 (tempo iyi), haftalık %67 (tempoda). Plan: scout (Opus 5.5·high) kodu tarar, coder (Opus 5.5·high) uygular, reviewer (Opus 5.5·xhigh) doğrular. ~20 dk sürer.

In Yellow, add a short warning to that line: which window is high, its reset time, and that the plan keeps the same models unless the user wants to save quota, e.g. `Haftalık %82 (sarı, Pazar 01:00'de sıfırlanır); modeller aynı kalıyor, tasarruf istersen söyle.`

**When to ask** (the decision is the user's; use AskUserQuestion or the wrap-up question):
- State is Red, or your estimate says the job won't fit in the remaining 5 h / weekly budget. Say what it will likely cost, where the limits stand, the reset time (in the user's local time), and offer: continue on the same models / save quota with cheaper models (right-hand column) / start after the reset / shrink the scope / move side work to Codex.
- You genuinely can't tell whether the job is S or L and the state is Yellow.
Never ask just to confirm a model choice.

## 7. During and after the job

- Re-read limits before each new wave of subagents and roughly every 30–45 min of a long job (every ~15 min once the 5 h window is ≥ 70 %). If the state worsened, say so in one line with the reset time and keep the same models unless the user asks to save quota (Red still triggers the question in section 6).
- **Handoff threshold:** if the 5 h window reaches ≥ 85 % (or weekly ≥ 95 %) and the job won't finish in what's left: write the handoff note now with the `devir` skill (the session can't write anything once the limit hits 100 %), then spawn nothing new, finish only the current step, and tell the user the reset time and that they can continue on another account. Send a `PushNotification` for it (see `devir`).
- Whenever you stop to ask the user something that blocks the job (Red budget, open decision) and they may be away, also send a `PushNotification`.
- If the main session itself runs on Fable/Opus at xhigh and the state is Red, recommend (don't do) lowering the session model/effort: the main agent's own turns are often the biggest consumer.
- Calibration: after any job that spawned subagents and moved the 5 h window by ≥ 5 points, append one line to `~/.claude/skills/agent-budget/calibration-<hostname>.log`:
  `YYYY-MM-DD | <5-8 word task summary, no secrets> | <plan, e.g. 2×haiku·low + 1×opus·xhigh> | Δ5h +N | Δweekly +M`
  Read the last ~20 lines of these logs when estimating similar jobs.

## Terminal `claude` (no get_usage tool)

Claude limits can't be read there. Route by task as usual (default column). For M/L jobs, tell the user the numbers are unavailable and suggest they check `/usage`.
