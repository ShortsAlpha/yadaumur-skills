#!/usr/bin/env node
// Read the current Codex (ChatGPT login) usage limits via `codex app-server` JSON-RPC over stdio.
// Read-only: initialize -> initialized -> account/rateLimits/read. No thread, no model turn, no usage spent.
// Never call account/rateLimitResetCredit/consume (spends a reset credit) or account/logout from here.
// Usage: node codex-limits.mjs [--text] [--raw]
//   (default) compact JSON for agents      --text  one Turkish summary line      --raw  raw result (ids removed)
// Env: CODEX_BIN (default "codex"), CODEX_RL_TIMEOUT_MS (default 20000).
import { spawn, spawnSync } from "node:child_process";
import readline from "node:readline";

const args = new Set(process.argv.slice(2));
const bin = process.env.CODEX_BIN || "codex";
const timeoutMs = Number(process.env.CODEX_RL_TIMEOUT_MS || 20000);
const isWin = process.platform === "win32";
const t0 = Date.now();

// npm installs a codex.cmd shim on Windows, which needs a shell; pass one quoted command string.
const child = isWin
  ? spawn(`"${bin}" app-server`, { shell: true, windowsHide: true, stdio: ["pipe", "pipe", "pipe"] })
  : spawn(bin, ["app-server"], { stdio: ["pipe", "pipe", "pipe"] });

let stderr = "";
child.stderr.on("data", (d) => { if (stderr.length < 4000) stderr += d; });
child.on("error", (e) => fail(`cannot start ${bin}: ${e.message}`));

const send = (msg) => child.stdin.write(JSON.stringify(msg) + "\n");
const stop = () => {
  try { child.stdin.end(); } catch {}            // stdin EOF makes app-server exit on its own
  setTimeout(() => {                             // fallback if it lingers
    if (child.exitCode !== null) return;
    if (isWin) spawnSync("taskkill", ["/pid", String(child.pid), "/T", "/F"], { stdio: "ignore" });
    else child.kill("SIGTERM");
  }, 3000).unref();
};
function fail(message, code = 1) {
  process.stderr.write(`codex-limits: ${message}\n${stderr ? "stderr: " + stderr.slice(0, 800) + "\n" : ""}`);
  stop();
  process.exitCode = code;
}
const timer = setTimeout(() => fail(`timed out after ${timeoutMs} ms`, 2), timeoutMs);

const win = (w, now) => w && {
  usedPercent: w.usedPercent,
  remainingPercent: Math.max(0, 100 - w.usedPercent),
  windowMinutes: w.windowDurationMins,          // 300 = 5 h, 10080 = 7 d
  resetsAt: w.resetsAt,
  resetsAtIso: w.resetsAt ? new Date(w.resetsAt * 1000).toISOString() : null,
  resetsInSeconds: w.resetsAt ? Math.max(0, w.resetsAt - now) : null,
};
const snap = (s, now) => s && {
  limitId: s.limitId,
  planType: s.planType,
  primary: win(s.primary, now),
  secondary: win(s.secondary, now),
  credits: s.credits,                            // { hasCredits, unlimited, balance (string) }
  rateLimitReachedType: s.rateLimitReachedType,  // null or rate_limit_reached / workspace_*
  spendControlReached: s.spendControlReached,
  individualLimit: s.individualLimit,
};
const dur = (sec) => {
  if (sec == null) return "?";
  const d = Math.floor(sec / 86400), h = Math.floor((sec % 86400) / 3600), m = Math.floor((sec % 3600) / 60);
  return d ? `${d} g ${h} sa` : h ? `${h} sa ${m} dk` : `${m} dk`;
};
const label = (w) => w?.windowMinutes === 300 ? "5 saat" : w?.windowMinutes === 10080 ? "haftalık" : `${w?.windowMinutes} dk`;

readline.createInterface({ input: child.stdout }).on("line", (line) => {
  let msg;
  try { msg = JSON.parse(line); } catch { return; }
  if (msg.id === 1) {
    if (msg.error) return fail(`initialize failed: ${JSON.stringify(msg.error)}`);
    send({ method: "initialized" });
    send({ method: "account/rateLimits/read", id: 2, params: { excludeResetCreditDetails: true } });
  } else if (msg.id === 2) {
    clearTimeout(timer);
    if (msg.error) return fail(`account/rateLimits/read failed: ${JSON.stringify(msg.error)}`);
    const r = msg.result;
    const now = Math.floor(Date.now() / 1000);
    if (args.has("--raw")) {
      const out = { ...r, accountId: r.accountId ? "<omitted>" : null };
      process.stdout.write(JSON.stringify(out, null, 2) + "\n");
      return stop();
    }
    const byId = r.rateLimits ? r.rateLimitsByLimitId || {} : {};
    const out = {
      source: "codex app-server account/rateLimits/read",
      fetchedAt: new Date(now * 1000).toISOString(),
      elapsedMs: Date.now() - t0,
      ordinaryUsageAllowed: r.ordinaryUsageAllowed,
      ...snap(r.rateLimits, now),
      resetCreditsAvailable: r.rateLimitResetCredits ? Number(r.rateLimitResetCredits.availableCount) : null,
      otherBuckets: Object.fromEntries(Object.entries(byId)
        .filter(([k]) => k !== r.rateLimits?.limitId).map(([k, v]) => [k, snap(v, now)])),
    };
    if (args.has("--text")) {
      const parts = [out.primary, out.secondary].filter(Boolean)
        .map((w) => `${label(w)} %${w.usedPercent} (sıfırlanma ${dur(w.resetsInSeconds)})`);
      const extra = out.resetCreditsAvailable ? `, sıfırlama kredisi ${out.resetCreditsAvailable}` : "";
      const blocked = out.rateLimitReachedType ? ` — LİMİT DOLU (${out.rateLimitReachedType})` : "";
      process.stdout.write(`Codex (${out.planType ?? "?"}): ${parts.join(", ")}${extra}${blocked}\n`);
    } else {
      process.stdout.write(JSON.stringify(out, null, 2) + "\n");
    }
    stop();
  }
  // Notifications (account/updated, account/rateLimits/updated, remoteControl/status/changed, ...) are ignored.
});

send({ method: "initialize", id: 1,
       params: { clientInfo: { name: "usage_limits_skill", title: null, version: "1.0.0" }, capabilities: null } });
