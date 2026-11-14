---
name: usage-limits
description: Read the user's live subscription limits - Claude plan limits (5-hour session, weekly all-models, weekly Fable, extra usage) and OpenAI Codex limits (5-hour and weekly windows, reset credits). Use when the user asks about their limits, usage, quota, "limitim", "kotam", "ne kadar kaldı", "Fable limitim", "Codex limitim", or before heavy/long work (many subagents, workflows, big Codex or image batches) and whenever another skill needs current limits to decide pacing. Read-only, costs no usage.
---

# Usage limits (Claude + Codex)

Two independent sources. Read both fresh each time you need them (numbers move during work); never guess or reuse numbers older than ~5 minutes.

## Claude plan limits

In the Claude desktop app (Code tab) use the app's own tool `mcp__ccd_session_mgmt__get_usage`. It is usually deferred: load it first with ToolSearch `select:mcp__ccd_session_mgmt__get_usage`, then call it with no arguments.

- `plan.windows[]`: `label` ("5-hour limit", "Weekly · all models", "Weekly · Fable", possibly others), `percentUsed`, `resetsAt` (ISO, UTC), `resetsIn`.
- `plan.extraUsage`: whether paid extra usage is on.
- `plan.status`: `ok`, `unavailable` (try again later, see note) or `not_applicable` (API key/gateway: no plan limits).
- `plan.plan`: the plan of the account logged in right now. The user may have several Claude accounts; limits are per account, so say which plan the numbers belong to.
- Bonus: `context` shows how full this session's context window is.

Terminal `claude` (no desktop tool): there is no supported way to read the Fable weekly limit there. Say so and ask the user to run `/usage`; do not read OAuth tokens or call undocumented endpoints.

## Codex limits

```bash
bash ~/.claude/skills/usage-limits/scripts/codex-limits.sh --text   # one Turkish summary line
bash ~/.claude/skills/usage-limits/scripts/codex-limits.sh          # JSON for decisions
```
- ~1 s, read-only (Codex app-server `account/rateLimits/read`; no model turn, no usage spent). Needs Node 18+ and a logged-in Codex (`codex` on PATH or the ChatGPT desktop app).
- JSON: `planType`, `primary` (5 h window) and `secondary` (weekly), each with `usedPercent`, `remainingPercent`, `resetsAtIso`, `resetsInSeconds`; `rateLimitReachedType` non-null = blocked now; `ordinaryUsageAllowed`; `resetCreditsAvailable` (free full resets the user can redeem in Codex; never redeem them yourself).
- Exit 1/2 = Codex not installed, not logged in or timed out: report that, don't retry in a loop.

## Reporting to the user

Answer in Turkish with one compact table, the user's local time for resets (time zone from the user's global CLAUDE.md or the environment). Convert the UTC `resetsAt` values yourself by the zone's offset; Git Bash `date` has no tz data and silently prints UTC for `TZ=<Area/City>`. Example:

| Limit | Kullanılan | Sıfırlanma |
|---|---|---|
| Claude 5 saat | %24 | 20:50 (1 sa 45 dk) |
| Claude haftalık (tüm modeller) | %67 | Sal 01:00 (2 g 5 sa) |
| Claude haftalık (Fable) | %0 | … |
| Codex 5 saat | %9 | … |
| Codex haftalık | %1 | … |

Then one line of judgement only if something matters (a window ≥80 %, or a weekly window that will run out before its reset at the current pace).

## For other skills (pacing contract)

Skills that need limits call this skill's two sources directly, right before deciding. Shared thresholds:
- < 70 %: normal.
- 70–89 %: mention it; prefer cheaper paths (fewer parallel agents, lower effort, Codex for side work if its limits are healthier).
- ≥ 90 % on a 5-hour window: pause heavy work until reset or switch provider; ≥ 90 % on a weekly window: tell the user before any large job.
- A blocked window (`rateLimitReachedType` set, or 100 %): don't start work on that provider.
