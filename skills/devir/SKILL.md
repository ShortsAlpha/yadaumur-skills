---
name: devir
description: Hand work over between sessions without losing the thread - when a Claude account's limit is about to run out (the user may switch between several accounts), when switching device (e.g. Windows ↔ Mac), when the context is nearly full, or when the user says "devir", "devret", "hesap değiştireceğim", "diğer hesaba geçiyorum". Writes one handoff note per project in the vault; a new session picks it up automatically and continues. Also sends a phone/desktop notification when a decision is needed, a long job finished or work stopped.
---

# devir: hand over, pick up, notify

The user runs very long sessions and may rotate between several Claude accounts and devices. A handoff note carries the **in-flight state** (what is half-done right now, the exact next step); the durable record stays in the vault notes kept by `vault-live`. Don't duplicate: link to the line note instead of copying it.

## When to write a handoff (proactively, don't wait to be asked)

- **Limit about to end:** `agent-budget` sees the Claude 5-hour window ≥ 85 % (or weekly ≥ 95 %) during a job that won't finish in the remaining budget. Write the handoff **now**, while you still can: at 100 % the session can't even write it.
- The user says they're switching account/device or stopping for the day mid-work.
- The context window is ≥ 85 % full (check with `mcp__ccd_session_mgmt__get_usage` → `context`) and the job continues.
- Not for finished work: when a job is done, `vault-live` records it and no handoff is needed (close any open one, see below).

## Where

One note per project, overwritten each time (never a growing pile):
- Code project: `projects/<folder>/Devir.md`
- YouTube channel: `youtube/<kanal>/Devir.md`
- No card (scratch work, a one-off): `📥 000-Inbox/<YYYY-MM-DD> Devir <kısa konu>.md`
If the repo itself keeps a `HANDOFF.md`, update that too with the same content.

Paths are relative to the vault (location and layout: see `vault-live`). Write through `beyin note-read` + `note-edit` (`expected_sha256: null` + `content` when the note doesn't exist yet), as `vault-live` describes. Then, because the next session may start on another device within minutes, push right away: in the vault `git add -A`, `git commit -m "Devir: <proje>"`, `git pull --rebase`, `git push` (this is the one case where you run git in the vault yourself).

## Format (Turkish; code, paths, commands as is; keep it under ~60 lines)

```
---
{
  "kind": "note",
  "title": "Devir: <proje>",
  "project": "<folder or channel>",
  "status": "açık",
  "from": "<device> · <plan from get_usage> · <date time, user's local time>",
  "cwd": "<absolute project path on that device>",
  "branch": "<git branch>",
  "line_note": "<vault path of the version/branch note>",
  "visibility": "internal",
  "updated": "<YYYY-MM-DD>"
}
---

# Devir: <proje>

Kart: [[<card>]] · Sürüm notu: [[<line note>]]

## Nerede kaldık
2-4 cümle: hedef, şu an hangi adımdayız, neden durduk (kota / cihaz / bağlam).

## Yarım kalan
- Tam olarak ne yarım: dosya:satır, çalışan/kırık test, uygulanmamış diff, açık PR/derleme.

## Sıradaki adımlar
1. İlk yapılacak şey, kopyala-çalıştır komutla.
2. …

## Açık kararlar
- Kullanıcının vermesi gereken kararlar ve seçenekler.

## Alt ajanlar ve arka plan
- Hangi alt ajan neyi yapıyordu, sonucu geldi mi; arka planda çalışan komut, dev server, Codex işi.

## Dikkat
- Tuzaklar, denenip olmayanlar, dokunulmaması gerekenler.
```
No secrets, no transcript, no code blocks longer than a command.

After writing: tell the user in one line where it is and what to do ("Devir notu hazır: projects/my-app/Devir.md. Diğer hesapla bu klasörde yeni oturum aç, kaldığım yerden devam eder.") and send a notification (below).

## Picking up (new session)

The `vault-live` SessionStart hook tells you when the project has an open handoff note, but it reads the local vault before your start-of-session `git pull`: after a device switch the note may only arrive with that pull. So after the pull, check `projects/<folder>/Devir.md` (or the channel's) yourself, always when the user mentions continuing or switching. Then:
1. Read the note (and the line note it links). Verify reality before trusting it: `git status`, branch, last commit, whether the listed half-done work is still as described.
2. Start your first reply with one line: "Devirden devam: <nerede kaldık>. Sıradaki adım: <1. adım>." and continue the work unless an open decision blocks it (then ask).
3. Mark it taken: `status` → `devralındı`, add `"to": "<device> · <plan> · <date time>"`, via `note-edit`. If you later hand over again, overwrite the whole note with the new state and `status: açık`.
4. When the job is finished and recorded by `vault-live`, set `status: kapandı`.

## Notifications

Use the `PushNotification` tool (load with ToolSearch `select:PushNotification`). It reaches the phone only when Remote Control is on for the session and the phone app is signed in to the same account and organization running it (on Team/Enterprise plans Remote Control stays off until the org owner enables it); otherwise it shows on the desktop or is skipped (that's fine, don't retry). When you hand over because a limit is ending, the phone is usually still on the old account: say the essentials in the chat message too. Send only for:
- a decision is needed and work is blocked (red budget, a handoff written because the limit is ending, an open question);
- a long job (≥ ~15 min) finished while the user was likely away;
- work stopped on an error you can't resolve.
One line, < 200 chars, lead with what to act on: "Kota %88: devir notu hazır (my-app), diğer hesaba geç." Never for routine progress or quick answers.
