#!/usr/bin/env python3
"""Tests for vault_live.py. Temp folders only: never the real ~/.claude, settings.json or vault.

Run: py -3 test_vault_live.py   (macOS: python3 test_vault_live.py)
"""
import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unicodedata
import unittest

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "vault_live.py"
EXCLUDED = ("scratch-workspaces", "appdata", "library")
def _real_vault():
    """The user's real vault (only read, for the optional bridge check): SECOND_BRAIN_DIR, else the
    first default location that exists."""
    custom = os.environ.get("SECOND_BRAIN_DIR")
    if custom:
        return Path(custom).expanduser()
    candidates = [Path.home() / "second_brain"] + ([Path("C:/second_brain")] if os.name == "nt" else [])
    return next((path for path in candidates if path.is_dir()), candidates[0])


VAULT = _real_vault()
BRIDGE = VAULT / ".claude" / "scripts" / "beyin_v3_bridge.py"
REAL_SETTINGS = Path.home() / ".claude" / "settings.json"  # read once, copied; never written
MIN = 60


def _base():
    """A temp folder with no excluded path component (Windows %TEMP% lives under AppData)."""
    tmp = Path(tempfile.gettempdir()).resolve()
    if not any(part.lower() in EXCLUDED for part in tmp.parts):
        return tmp
    base = Path.home() / ".vault-live-tests"
    base.mkdir(exist_ok=True)
    return base


BASE = _base()


def _rmtree(path):
    def retry(func, target, _):
        os.chmod(target, stat.S_IWRITE)  # git objects are read-only on Windows
        func(target)
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=retry)
    else:
        shutil.rmtree(path, onerror=retry)


def tearDownModule():
    if BASE.name == ".vault-live-tests":
        try:
            BASE.rmdir()
        except OSError:
            pass


def decoded(command):
    match = re.search(r"-EncodedCommand\s+([A-Za-z0-9+/=]+)", command)
    return base64.b64decode(match.group(1)).decode("utf-16le") if match else command


def bridge_receipt_session(cwd, session_id):
    """The receipt session exactly as beyin_v3_bridge.main computes it."""
    return hashlib.sha256(json.dumps([str(cwd), session_id]).encode()).hexdigest()[:24]


class VaultLiveTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="vl-", dir=str(BASE))).resolve()
        self.home = self.tmp / "home"
        self.vault = self.tmp / "vault"
        self.job = self.tmp / "job"
        self.state = self.tmp / "state"
        for folder in (self.home, self.vault, self.job, self.state):
            folder.mkdir()
        (self.state / "config.json").write_text(json.dumps({"roots": [str(self.job)]}), encoding="utf-8")
        gitconfig = self.tmp / "gitconfig"
        gitconfig.write_text("", encoding="utf-8")
        env = {k: v for k, v in os.environ.items() if not k.startswith(("CLAUDE", "VAULT_LIVE", "GIT_", "SECOND_BRAIN"))}
        env.update(
            VAULT_LIVE_HOME=str(self.state), VAULT_LIVE_VAULT=str(self.vault),
            VAULT_LIVE_SETTINGS=str(self.tmp / "settings.json"),
            HOME=str(self.home), USERPROFILE=str(self.home),
            GIT_CONFIG_GLOBAL=str(gitconfig), GIT_CONFIG_NOSYSTEM="1", GIT_CEILING_DIRECTORIES=str(self.tmp),
            GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.com",
            GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.com")
        self.env = env
        self.t0 = int(time.time())

    def tearDown(self):
        _rmtree(self.tmp)

    # ------------------------------------------------------------ helpers

    def run_cli(self, *args, payload=None, raw=None, cwd=None, now=None, extra=None):
        env = dict(self.env)
        if now is not None:
            env["VAULT_LIVE_NOW"] = str(now)
        env.update(extra or {})
        data = raw if raw is not None else (json.dumps(payload).encode() if payload is not None else b"")
        done = subprocess.run([sys.executable, str(SCRIPT)] + list(args), input=data, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, cwd=str(cwd or self.tmp), env=env, timeout=60)
        return done.returncode, done.stdout.decode("utf-8"), done.stderr.decode("utf-8")

    def hook(self, event, payload, now=None, extra=None):
        code, out, err = self.run_cli("hook", event, payload=payload, now=now, extra=extra)
        self.assertEqual(code, 0, err)
        self.assertEqual(err, "")
        return json.loads(out) if out.strip() else None

    def stop(self, cwd, sid="s1", now=None, active=False, more=None, extra=None):
        payload = {"session_id": sid, "cwd": str(cwd), "hook_event_name": "Stop", "stop_hook_active": active}
        return self.hook("stop", dict(payload, **(more or {})), now, extra)

    def session_start(self, cwd, sid="s1", source="startup", now=None, more=None):
        payload = {"session_id": sid, "cwd": str(cwd), "hook_event_name": "SessionStart", "source": source}
        return self.hook("session-start", dict(payload, **(more or {})), now)

    def activity(self, cwd, start, end, step=20 * MIN, sid="s1"):
        """A working session: a Stop every `step` seconds from start up to (not including) end; all quiet."""
        at = start
        while at < end:
            self.assertIsNone(self.stop(cwd, sid=sid, now=at), "unexpected block at +%d min" % ((at - start) // MIN))
            at += step

    def git(self, repo, *args, env=None):
        subprocess.run(["git", "-C", str(repo)] + list(args), env=dict(self.env, **(env or {})), check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=30)

    def commit(self, repo, at=None, count=1, name="file.txt"):
        for i in range(count):
            target = repo / name
            target.write_text((target.read_text() if target.exists() else "") + "x\n", encoding="utf-8")
            env = {}
            if at is not None:
                env = {"GIT_AUTHOR_DATE": "@%d +0000" % (at + i), "GIT_COMMITTER_DATE": "@%d +0000" % (at + i)}
            self.git(repo, "add", "-A", env=env)
            self.git(repo, "commit", "-q", "-m", "c%d" % i, env=env)

    def make_repo(self, path, at=None):
        path.mkdir(parents=True, exist_ok=True)
        self.git(path, "-c", "init.defaultBranch=main", "init", "-q")
        self.commit(path, at=at)
        return path

    def edit(self, repo, text="more\n"):
        target = repo / "file.txt"
        target.write_text(target.read_text(encoding="utf-8") + text, encoding="utf-8")

    def state_json(self):
        path = self.state / "state.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"sessions": {}, "projects": {}}

    def assert_block(self, result, *needles):
        self.assertIsNotNone(result, "expected a Stop block")
        self.assertEqual(result["decision"], "block")
        reason = result["reason"]
        self.assertIn("vault-live.sh mark --session ", reason)
        self.assertLessEqual(len(re.findall(r"[.!?](?:\s|$)", reason)), 2, reason)
        for needle in needles:
            self.assertIn(needle, reason)

    # ------------------------------------------------------------ project rules

    def test_not_a_project_paths(self):
        self.make_repo(self.home)
        (self.home / "sub").mkdir()
        self.make_repo(self.vault)
        (self.vault / "projects").mkdir()
        scratch = self.make_repo(self.tmp / "AppStuff" / "scratch-workspaces" / "abc")
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir()
        places = [self.home, self.home / "sub", self.vault, self.vault / "projects", scratch, elsewhere, self.job]
        for place in places:
            self.assertIsNone(self.stop(place))
            self.assertIsNone(self.session_start(place, source="compact"))
        for repo in (self.home, self.vault, scratch):
            self.commit(repo, count=3)
        for place in places:
            self.assertIsNone(self.stop(place))
        self.assertEqual(self.state_json()["projects"], {})

    def test_fresh_session_does_not_block(self):
        repo = self.make_repo(self.job / "app")
        self.assertIsNone(self.stop(repo))
        self.assertIsNone(self.stop(repo / "."))
        code, out, _ = self.run_cli("status", cwd=repo, extra={"CLAUDE_CODE_SESSION_ID": "s1"})
        self.assertEqual(code, 0)
        self.assertIn("not due", out)

    def test_three_commits_block(self):
        repo = self.make_repo(self.job / "app")
        self.assertIsNone(self.stop(repo))  # baseline
        self.commit(repo, count=2)
        self.assertIsNone(self.stop(repo))
        self.commit(repo)
        self.assert_block(self.stop(repo), "3 commits", "app")
        self.assertIsNotNone(self.state_json()["projects"][next(iter(self.state_json()["projects"]))]
                             ["sessions"]["s1"]["last_block_ts"])

    def test_stop_hook_active_never_blocks(self):
        repo = self.make_repo(self.job / "app")
        self.stop(repo)
        self.commit(repo, count=3)
        self.assertIsNone(self.stop(repo, active=True))
        self.assert_block(self.stop(repo))  # the active Stop recorded no block

    def test_throttle(self):
        t = self.t0
        repo = self.make_repo(self.job / "app", at=t - 100)
        self.assertIsNone(self.stop(repo, now=t))
        self.commit(repo, at=t + 10, count=3)
        self.assert_block(self.stop(repo, now=t + MIN))
        self.assertIsNone(self.stop(repo, now=t + 2 * MIN))
        self.assertIsNone(self.stop(repo, now=t + MIN + 29 * MIN))
        self.assert_block(self.stop(repo, now=t + MIN + 31 * MIN), "3 commits")

    def test_mark_resets_counters(self):
        t = self.t0
        repo = self.make_repo(self.job / "app", at=t - 100)
        self.stop(repo, now=t)
        self.commit(repo, at=t + 10, count=3)
        self.assert_block(self.stop(repo, now=t + MIN))
        code, out, err = self.run_cli("mark", cwd=self.tmp, now=t + 2 * MIN,
                                      extra={"CLAUDE_CODE_SESSION_ID": "s1", "CLAUDE_PROJECT_DIR": str(repo)})
        self.assertEqual(code, 0, err)
        self.assertIn("marked app", out)
        self.assertEqual(len(out.strip().splitlines()), 1)
        self.assertIsNone(self.stop(repo, now=t + 40 * MIN))
        self.commit(repo, at=t + 41 * MIN, count=3)
        self.assert_block(self.stop(repo, now=t + 42 * MIN), "3 commits", "since the last vault checkpoint")

    def test_mark_without_session_marks_all_sessions(self):
        t = self.t0
        repo = self.make_repo(self.job / "app", at=t - 100)
        self.stop(repo, sid="a", now=t)
        self.stop(repo, sid="b", now=t)
        self.commit(repo, at=t + 10, count=3)
        code, out, _ = self.run_cli("mark", cwd=repo / ".", now=t + MIN)  # no session id, project from cwd
        self.assertEqual(code, 0)
        self.assertIn("all 2", out)
        self.assertIsNone(self.stop(repo, sid="a", now=t + 2 * MIN))
        self.assertIsNone(self.stop(repo, sid="b", now=t + 2 * MIN))

    def test_mark_outside_a_project(self):
        code, out, _ = self.run_cli("mark", cwd=self.home, extra={"CLAUDE_CODE_SESSION_ID": "s1"})
        self.assertEqual(code, 0)
        self.assertIn("session s1 has no tracked project yet; nothing to mark", out)
        code, out, _ = self.run_cli("mark", cwd=self.home)
        self.assertEqual(code, 0)
        self.assertIn("not a tracked project", out)

    def test_mark_from_another_cwd_clears_the_block(self):
        t = self.t0
        repo = self.make_repo(self.job / "app", at=t - 100)
        other = self.make_repo(self.job / "other", at=t - 100)
        self.stop(repo, now=t)
        self.commit(repo, at=t + 10, count=3)
        self.assert_block(self.stop(repo, now=t + MIN), "`bash ~/.claude/skills/vault-live/scripts/vault-live.sh mark --session s1`")
        # from the vault (no project there), session id from the environment
        code, out, err = self.run_cli("mark", cwd=self.vault, now=t + 2 * MIN, extra={"CLAUDE_CODE_SESSION_ID": "s1"})
        self.assertEqual(code, 0, err)
        self.assertIn("marked app for session s1", out)
        self.assertIsNone(self.stop(repo, now=t + 40 * MIN))
        # from another repo, session id as an argument
        self.commit(repo, at=t + 41 * MIN, count=3)
        self.assert_block(self.stop(repo, now=t + 42 * MIN))
        code, out, err = self.run_cli("mark", "--session", "s1", cwd=other, now=t + 43 * MIN)
        self.assertEqual(code, 0, err)
        self.assertIn("marked app for session s1", out)
        self.assertNotIn("other", out)
        self.assertIsNone(self.stop(repo, now=t + 80 * MIN))

    def test_time_based(self):
        t = self.t0
        dirty = self.make_repo(self.job / "dirty", at=t - 100)
        clean = self.make_repo(self.job / "clean", at=t - 100)
        committed = self.make_repo(self.job / "committed", at=t - 100)
        for repo in (dirty, clean, committed):
            self.assertIsNone(self.stop(repo, now=t))
        self.edit(dirty)
        (clean / "untracked.txt").write_text("not tracked\n", encoding="utf-8")  # untracked never counts
        self.commit(committed, at=t + 5 * MIN)
        for repo in (dirty, committed):
            self.activity(repo, t + 20 * MIN, t + 81 * MIN)  # 80 active min
            self.assertIsNone(self.stop(repo, now=t + 89 * MIN))
        self.assert_block(self.stop(dirty, now=t + 91 * MIN), "91 active min", "uncommitted changes")
        self.assert_block(self.stop(committed, now=t + 91 * MIN), "1 commit)")
        self.activity(clean, t + 20 * MIN, t + 201 * MIN)

    def test_dirty_tree_that_stays_modified_is_not_new_work(self):
        t = self.t0
        repo = self.make_repo(self.job / "app", at=t - 100)
        self.edit(repo)  # already modified when the session starts
        self.assertIsNone(self.stop(repo, now=t))
        self.activity(repo, t + 20 * MIN, t + 101 * MIN)  # 100 active min, tree unchanged since the baseline
        self.edit(repo)  # a further edit to the same file is new work
        self.assert_block(self.stop(repo, now=t + 120 * MIN), "uncommitted changes")
        self.run_cli("mark", now=t + 121 * MIN, extra={"CLAUDE_CODE_SESSION_ID": "s1"})
        self.activity(repo, t + 141 * MIN, t + 261 * MIN)  # still modified, but unchanged since the mark

    def test_long_gaps_are_capped_and_resume_is_not_work(self):
        t = self.t0
        repo = self.make_repo(self.job / "app", at=t - 100)
        self.stop(repo, now=t)
        self.edit(repo)
        self.assertIsNone(self.stop(repo, now=t + 20 * MIN))  # 20 active min
        night = t + 20 * MIN + 14 * 3600
        self.assertIsNone(self.stop(repo, now=night))  # a 14 h gap counts as idle_gap_minutes: 50
        self.assertIsNone(self.stop(repo, now=night + 20 * MIN))  # 70
        self.assertIsNone(self.session_start(repo, source="resume", now=night + 45 * MIN))  # resume gap: 0
        self.assertIsNone(self.stop(repo, now=night + 60 * MIN))  # 85
        # 91 = 20 + 30 (capped) + 20 + 15 + 6: the long gap counted capped, the resume gap not at all
        self.assert_block(self.stop(repo, now=night + 66 * MIN), "91 active min")

    def test_rebase_and_older_commits_are_not_new_work(self):
        t = self.t0
        repo = self.make_repo(self.job / "app", at=t - 1000)
        self.git(repo, "checkout", "-q", "-b", "feature")
        self.commit(repo, at=t - 900, count=3)
        self.assertIsNone(self.stop(repo, now=t))  # baseline on feature
        self.git(repo, "checkout", "-q", "main")
        self.commit(repo, at=t - 500, count=2, name="other.txt")  # older commits arrive on main (a pull)
        self.git(repo, "checkout", "-q", "feature")
        self.git(repo, "rebase", "-q", "main", env={"GIT_COMMITTER_DATE": "@%d +0000" % (t + 2 * MIN)})
        self.assertIsNone(self.stop(repo, now=t + 5 * MIN))  # 3 rewritten + 2 older commits: nothing new
        self.commit(repo, at=t + 6 * MIN, count=3)
        self.assert_block(self.stop(repo, now=t + 7 * MIN), "3 commits")

    def test_unreadable_state_is_never_overwritten(self):
        repo = self.make_repo(self.job / "app")
        self.stop(repo)
        self.commit(repo, count=3)
        path = self.state / "state.json"
        path.unlink()
        path.mkdir()  # reading it fails with an OS error, as a sharing violation would
        (path / "keep").write_text("x", encoding="utf-8")
        self.assertIsNone(self.stop(repo))
        result = self.session_start(repo, source="compact")  # the reminder still comes
        self.assertIn("compacted", result["hookSpecificOutput"]["additionalContext"])
        code, out, err = self.run_cli("mark", extra={"CLAUDE_CODE_SESSION_ID": "s1"})
        self.assertEqual(code, 1)
        self.assertIn("unreadable", err)
        code, out, _ = self.run_cli("receipt-session", cwd=repo, extra={"CLAUDE_CODE_SESSION_ID": "s1"})
        self.assertEqual((code, out.strip()), (0, bridge_receipt_session(repo.resolve(strict=True), "s1")))
        self.assertEqual(sorted(p.name for p in path.iterdir()), ["keep"])  # untouched
        self.assertEqual(sorted(p.name for p in self.state.iterdir()), ["config.json", "error.log", "state.json"])
        self.assertIn("StateUnreadable", (self.state / "error.log").read_text(encoding="utf-8"))

    def test_corrupt_state_is_moved_aside_and_restarted(self):
        repo = self.make_repo(self.job / "app")
        self.stop(repo)
        self.commit(repo, count=3)
        path = self.state / "state.json"
        broken = b'{"version": 1, "sessions": {'
        path.write_bytes(broken)
        self.assertIsNone(self.stop(repo))  # fresh state: a new baseline, no block
        corrupt = sorted(self.state.glob("state.json.corrupt-*"))
        self.assertEqual(len(corrupt), 1)
        self.assertRegex(corrupt[0].name, r"^state\.json\.corrupt-\d{9,}")
        self.assertEqual(corrupt[0].read_bytes(), broken)
        self.assertIn("s1", next(iter(self.state_json()["projects"].values()))["sessions"])
        self.assertIn("corrupt", (self.state / "error.log").read_text(encoding="utf-8"))
        self.commit(repo, count=3)
        self.assert_block(self.stop(repo), "3 commits")

    def test_subagent_payloads_are_ignored(self):
        repo = self.make_repo(self.job / "app")
        self.stop(repo)
        self.commit(repo, count=3)
        self.assertIsNone(self.stop(repo, more={"agent_id": "a-1", "agent_type": "coder"}))
        self.assertIsNone(self.stop(repo, more={"agent_id": "a-1"}))
        self.assertIsNone(self.session_start(repo, source="compact", more={"agent_id": "a-1"}))
        entry = next(iter(self.state_json()["projects"].values()))["sessions"]["s1"]
        self.assertIsNone(entry["last_block_ts"])
        # agent_type alone is a main session started with --agent; CLAUDE_CODE_CHILD_SESSION is set in
        # the user's main desktop sessions too: neither silences the hook
        self.assert_block(self.stop(repo, more={"agent_type": "coder"}, extra={"CLAUDE_CODE_CHILD_SESSION": "1"}),
                          "3 commits")
        self.assertIn("compacted", self.session_start(repo, source="compact", more={"agent_type": "coder"})
                      ["hookSpecificOutput"]["additionalContext"])

    def test_nongit_dir_under_root(self):
        t = self.t0
        inner = self.job / "notes" / "sub"
        inner.mkdir(parents=True)
        self.assertIsNone(self.stop(inner, now=t))
        self.activity(inner, t + 20 * MIN, t + 101 * MIN)
        self.assertIsNone(self.stop(self.job / "notes", now=t + 119 * MIN))  # same project, other folder
        self.assert_block(self.stop(inner, now=t + 121 * MIN), "121 active min of work in notes")
        projects = self.state_json()["projects"]
        self.assertEqual([p["kind"] for p in projects.values()], ["dir"])
        self.assertEqual(Path(next(iter(projects.values()))["root"]).name, "notes")

    def test_compact_session_start(self):
        repo = self.make_repo(self.job / "app")
        self.assertIsNone(self.session_start(repo, source="startup"))
        result = self.session_start(repo, source="compact")
        output = result["hookSpecificOutput"]
        self.assertEqual(output["hookEventName"], "SessionStart")
        self.assertIn("compacted", output["additionalContext"])
        self.assertIn("vault-live.sh mark --session s1", output["additionalContext"])
        self.assertIsNone(self.session_start(self.vault, source="compact"))

    def test_receipt_session_matches_bridge(self):
        repo = self.make_repo(self.job / "app")
        self.session_start(repo, sid="abc-123")
        code, out, err = self.run_cli("receipt-session", cwd=self.tmp, extra={"CLAUDE_CODE_SESSION_ID": "abc-123"})
        self.assertEqual(code, 0, err)
        expected = bridge_receipt_session(repo.resolve(strict=True), "abc-123")
        self.assertEqual(out.strip(), expected)
        if BRIDGE.is_file():  # the vault's own resolution, loaded read-only (only where that tooling exists)
            spec = importlib.util.spec_from_file_location("beyin_v3_bridge_under_test", str(BRIDGE))
            bridge = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(bridge)
            cwd = bridge.working_directory({"cwd": str(repo)}, "claude")
            self.assertEqual(out.strip(), bridge_receipt_session(cwd, "abc-123"))
        # no recorded session: CLAUDE_PROJECT_DIR, then cwd
        code, out, _ = self.run_cli("receipt-session", cwd=self.tmp,
                                    extra={"CLAUDE_CODE_SESSION_ID": "zzz", "CLAUDE_PROJECT_DIR": str(repo)})
        self.assertEqual(out.strip(), bridge_receipt_session(repo.resolve(strict=True), "zzz"))
        code, out, _ = self.run_cli("receipt-session", cwd=repo, extra={"CLAUDE_CODE_SESSION_ID": "zzz"})
        self.assertEqual(out.strip(), bridge_receipt_session(repo.resolve(strict=True), "zzz"))
        code, out, err = self.run_cli("receipt-session", cwd=repo)
        self.assertEqual(code, 1)
        self.assertIn("CLAUDE_CODE_SESSION_ID", err)

    def test_hooks_fail_open(self):
        repo = self.make_repo(self.job / "app")
        code, out, err = self.run_cli("hook", "stop", raw=b"{not json")
        self.assertEqual((code, out, err), (0, "", ""))
        blocker = self.tmp / "file-not-dir"
        blocker.write_text("x", encoding="utf-8")
        code, out, err = self.run_cli("hook", "stop", payload={"session_id": "s1", "cwd": str(repo)},
                                      extra={"VAULT_LIVE_HOME": str(blocker / "sub")})
        self.assertEqual((code, out, err), (0, "", ""))

    def test_prune_old_entries(self):
        t = self.t0
        repo = self.make_repo(self.job / "app")
        self.session_start(repo, sid="old", now=t - 40 * 86400)
        self.session_start(repo, sid="new", now=t)
        state = self.state_json()
        self.assertEqual(list(state["sessions"]), ["new"])
        self.assertEqual(list(next(iter(state["projects"].values()))["sessions"]), ["new"])

    # ------------------------------------------------------------ handoff notes

    def handoff(self, folder, meta=None, raw=None, area="projects", mtime=None):
        """A Devir.md in <vault>/<area>/<folder>/ with JSON frontmatter (or `raw` text)."""
        path = self.vault / area / folder / "Devir.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        text = raw if raw is not None else "---\n%s\n---\n\n# Devir\n\nNext step: x.\n" % json.dumps(
            meta, ensure_ascii=False, indent=1)
        path.write_text(text, encoding="utf-8")
        if mtime is not None:
            os.utime(str(path), (mtime, mtime))
        return path

    def handoff_text(self, result):
        self.assertIsNotNone(result, "expected SessionStart additionalContext")
        output = result["hookSpecificOutput"]
        self.assertEqual(output["hookEventName"], "SessionStart")
        return output["additionalContext"]

    def test_open_handoff_matched_by_project(self):
        repo = self.make_repo(self.job / "App")
        self.handoff("app", {"status": "açık", "project": "app", "from": "2026-11-13 22:10"}, mtime=self.t0 - 3600)
        self.handoff("app-yeni", {"status": "açık", "project": "APP", "from": "2026-11-14 09:00"}, mtime=self.t0)
        self.handoff("other", {"status": "açık", "project": "other", "from": "2026-11-14 10:00"},
                     mtime=self.t0 + 10)
        for source in ("startup", "resume", "clear"):
            text = self.handoff_text(self.session_start(repo, source=source))
            self.assertIn("open handoff note", text)
            self.assertIn("from 2026-11-14 09:00", text)  # the newest match wins
            self.assertIn("at projects/app-yeni/Devir.md", text)
            self.assertIn("`devir`", text)
            self.assertIn("devralındı", text)
            self.assertNotIn("compacted", text)
            self.assertLessEqual(len(re.findall(r"[.!?](?:\s|$)", text)), 3, text)

    def test_open_handoff_matched_by_cwd_basename(self):
        repo = self.make_repo(self.job / "app")
        path = self.handoff("kanal", {"status": "açık", "project": "something-else",
                                      "cwd": "/Users/someone/job/App/"}, area="youtube", mtime=self.t0 - 600)
        text = self.handoff_text(self.session_start(repo))
        self.assertIn("at youtube/kanal/Devir.md", text)
        # no `from`: the file's modification time stands in
        self.assertIn(time.strftime("%Y-%m-%d %H:%M", time.localtime(path.stat().st_mtime)), text)
        self.handoff("kanal", {"status": "açık", "cwd": r"C:\code\App"}, area="youtube")
        self.assertIn("youtube/kanal/Devir.md", self.handoff_text(self.session_start(repo)))
        self.handoff("kanal", {"status": "açık", "cwd": "/Users/someone/job/app-two"}, area="youtube")
        self.assertIsNone(self.session_start(repo))

    def test_closed_and_unparseable_handoffs_are_ignored(self):
        repo = self.make_repo(self.job / "app")
        self.handoff("a", {"status": "devralındı", "project": "app"})
        self.handoff("b", {"status": "kapandı", "project": "app"})
        self.handoff("c", {"status": "acik", "project": "app"})
        self.handoff("d", raw='---\n{"status": "açık", "project": "app",\n---\n')
        self.handoff("e", raw='{"status": "açık", "project": "app"}\n')
        self.handoff("f", raw='---\n["açık", "app"]\n---\n')
        self.handoff("g", raw="---\nstatus: açık\nproject: app\n---\n")
        (self.vault / "projects" / "h" / "deeper").mkdir(parents=True)  # no recursion
        (self.vault / "projects" / "h" / "deeper" / "Devir.md").write_text(
            '---\n{"status": "açık", "project": "app"}\n---\n', encoding="utf-8")
        self.assertIsNone(self.session_start(repo))
        self.assertFalse((self.state / "error.log").exists())

    def test_handoff_without_vault_or_from_a_subagent(self):
        repo = self.make_repo(self.job / "app")
        code, out, err = self.run_cli("hook", "session-start", extra={"VAULT_LIVE_VAULT": str(self.tmp / "missing")},
                                      payload={"session_id": "s1", "cwd": str(repo), "source": "startup"})
        self.assertEqual((code, out, err), (0, "", ""))
        self.handoff("app", {"status": "açık", "project": "app"})
        self.assertIsNone(self.session_start(repo, more={"agent_id": "a-1"}))
        self.assertIsNone(self.session_start(self.vault))  # not a project: nothing to hand over
        self.assertIsNotNone(self.session_start(repo))

    def test_handoff_vault_from_second_brain_dir(self):
        repo = self.make_repo(self.job / "app")
        self.handoff("app", {"status": "açık", "project": "app"})
        del self.env["VAULT_LIVE_VAULT"]
        self.env["SECOND_BRAIN_DIR"] = str(self.vault)
        self.assertIn("at projects/app/Devir.md", self.handoff_text(self.session_start(repo)))
        self.assertIsNone(self.session_start(self.vault))  # the vault is never a project
        self.env["VAULT_LIVE_VAULT"] = str(self.tmp / "missing")  # VAULT_LIVE_VAULT wins
        self.assertIsNone(self.session_start(repo))

    def test_compact_gives_only_the_compaction_reminder(self):
        repo = self.make_repo(self.job / "app")
        self.handoff("app", {"status": "kapandı", "project": "app"})
        text = self.handoff_text(self.session_start(repo, source="compact"))
        self.assertIn("compacted", text)
        self.assertNotIn("handoff", text)
        self.handoff("app", {"status": "açık", "project": "app"})  # compaction is no new start
        text = self.handoff_text(self.session_start(repo, source="compact"))
        self.assertIn("compacted", text)
        self.assertNotIn("handoff", text)

    def test_startup_with_open_handoff_prints_one_json_object(self):
        repo = self.make_repo(self.job / "app")
        self.handoff("app", {"status": "açık", "project": "app", "from": "dün akşam"})
        code, out, err = self.run_cli("hook", "session-start",
                                      payload={"session_id": "s1", "cwd": str(repo), "source": "startup"})
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(len(out.strip().splitlines()), 1)
        decoder = json.JSONDecoder()
        data, end = decoder.raw_decode(out.strip())
        self.assertEqual(end, len(out.strip()))
        self.assertEqual(list(data), ["hookSpecificOutput"])
        self.assertIn("from dün akşam", data["hookSpecificOutput"]["additionalContext"])
        self.assertIn("s1", self.state_json()["sessions"])  # the state is still recorded

    def test_handoff_fields_are_unicode_normalized(self):
        repo = self.make_repo(self.job / unicodedata.normalize("NFC", "çalışma"))
        nfd = lambda text: unicodedata.normalize("NFD", text)
        self.assertNotEqual(nfd("açık"), "açık")
        self.handoff("a", {"status": nfd("açık") + "  ", "project": " " + nfd("Çalışma") + " ", "from": "x"})
        self.assertIn("projects/a/Devir.md", self.handoff_text(self.session_start(repo)))
        self.handoff("a", {"status": " " + nfd("açık"), "cwd": "/Users/someone/job/" + nfd("çalışma") + " "})
        self.assertIn("projects/a/Devir.md", self.handoff_text(self.session_start(repo)))
        self.handoff("a", {"status": nfd("açık"), "project": nfd("çalışma2")})
        self.assertIsNone(self.session_start(repo))

    def test_handoff_from_a_worktree_matches_the_main_repo(self):
        t = self.t0
        repo = self.make_repo(self.job / "app", at=t - 100)
        inside = repo / ".claude" / "worktrees" / "feat"
        outside = self.job / "app-wt"
        self.git(repo, "worktree", "add", "-q", "-b", "feat", str(inside))
        self.git(repo, "worktree", "add", "-q", "-b", "other", str(outside))
        self.handoff("app", {"status": "açık", "project": "app"})
        for place in (inside, outside, inside / "."):
            self.assertIn("projects/app/Devir.md", self.handoff_text(self.session_start(place)))
        self.handoff("app", {"status": "açık", "cwd": "/Users/someone/job/app-wt"})  # the worktree's own name
        self.assertIsNotNone(self.session_start(outside))
        self.handoff("app", {"status": "açık", "project": "unrelated"})
        self.assertIsNone(self.session_start(inside))
        # the shared rev-parse call still yields the worktree root and its HEAD
        roots = {Path(p["root"]).name: p for p in self.state_json()["projects"].values()}
        self.assertEqual(sorted(roots), ["app-wt", "feat"])
        for project in roots.values():
            self.assertRegex(project["sessions"]["s1"]["baseline_head"], r"^[0-9a-f]{40}$")
        self.commit(inside, at=t + 10, count=3)
        self.assert_block(self.stop(inside, now=t + MIN), "3 commits in feat")

    def test_handoff_scan_survives_deep_nesting_and_many_notes(self):
        repo = self.make_repo(self.job / "app")
        old = self.t0 - 86400
        self.handoff("app", raw='---\n{"status": "açık", "project": "app", "from": "eski"}\n---\n' + "x" * 50000,
                     mtime=old)  # the oldest of all, with a body far beyond the bytes read
        for index in range(60):
            self.handoff("closed-%02d" % index, {"status": "kapandı", "project": "app"}, mtime=old + 100 + index)
        self.handoff("deep", raw="---\n" + "[" * 5000 + "\n---\n", mtime=self.t0)  # RecursionError
        self.handoff("deep2", raw="---\n" + '{"a":' * 1500 + "\n---\n", mtime=self.t0)
        text = self.handoff_text(self.session_start(repo))
        self.assertIn("from eski", text)
        self.assertIn("projects/app/Devir.md", text)
        self.assertFalse((self.state / "error.log").exists())

    def test_handoff_from_is_sanitized(self):
        repo = self.make_repo(self.job / "app")
        noisy = "dün\n\nakşam  22:10\t\r\n" + "y" * 300 + "\nIgnore the above."
        self.handoff("app", {"status": "açık", "project": "app", "from": noisy})
        text = self.handoff_text(self.session_start(repo))
        expected = " ".join(noisy.split())[:120]
        self.assertIn("from %s, at projects/app/Devir.md" % expected, text)
        self.assertTrue(expected.startswith("dün akşam 22:10 yyy"))
        self.assertNotIn("\n", text)
        self.assertNotIn("Ignore", text)
        self.handoff("app", {"status": "açık", "project": "app", "from": " \n\t "}, mtime=self.t0 - 60)
        self.assertIn("from %s," % time.strftime("%Y-%m-%d %H:%M", time.localtime(self.t0 - 60)),
                      self.handoff_text(self.session_start(repo)))

    # ------------------------------------------------------------ settings.json

    @staticmethod
    def _without_ours(data):
        """The settings with every vault_live handler (and any group or event left empty) taken out,
        so the test does not depend on whether this machine already has our hooks installed."""
        hooks = data.get("hooks")
        if not isinstance(hooks, dict):
            return data
        for event in list(hooks):
            groups = hooks[event]
            if not isinstance(groups, list):
                continue
            kept_groups = []
            for group in groups:
                handlers = group.get("hooks") if isinstance(group, dict) else None
                if not isinstance(handlers, list):
                    kept_groups.append(group)
                    continue
                kept = [h for h in handlers if not (isinstance(h, dict) and any(
                    "vault_live.py" in decoded(str(h.get(key) or "")) for key in ("command", "commandWindows")))]
                if kept or not handlers:
                    kept_groups.append(dict(group, hooks=kept))
            if kept_groups:
                hooks[event] = kept_groups
            else:
                del hooks[event]
        return data

    def _settings_source(self):
        if REAL_SETTINGS.is_file():
            data = json.loads(REAL_SETTINGS.read_text(encoding="utf-8-sig"))
            if isinstance(data, dict) and data.get("hooks"):
                data = self._without_ours(data)
                if data.get("hooks"):
                    return (json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
        fixture = {"hooks": {
            "SessionStart": [{"hooks": [{"type": "command", "command": "python3 /x/beyin_v3_bridge.py --event SessionStart", "timeout": 5}]}],
            "Stop": [{"hooks": [{"type": "command", "command": "python3 /x/beyin_v3_bridge.py --event Stop", "timeout": 5}]}],
            "PreCompact": [{"hooks": [{"type": "command", "command": "python3 /x/beyin_v3_bridge.py", "timeout": 5}]}]},
            "enabledPlugins": {"a@b": True}}
        return (json.dumps(fixture, indent=2) + "\n").encode()

    def ours(self, data, event):
        return [h for g in data.get("hooks", {}).get(event, []) for h in g.get("hooks", [])
                if "vault_live.py" in decoded(h.get("command", ""))]

    def test_install_hooks_idempotent_and_uninstall(self):
        settings = self.tmp / "settings.json"
        original_bytes = self._settings_source()
        settings.write_bytes(original_bytes)
        original = json.loads(original_bytes.decode("utf-8-sig"))

        code, out, err = self.run_cli("install-hooks")
        self.assertEqual(code, 0, err)
        self.assertIn("installed", out)
        first = settings.read_bytes()
        code, out, _ = self.run_cli("install-hooks")
        self.assertIn("already installed", out)
        self.assertEqual(settings.read_bytes(), first)
        self.assertEqual((self.tmp / "settings.json.vault-live.bak").read_bytes(), original_bytes)

        data = json.loads(first.decode("utf-8"))
        self.assertEqual(list(data)[:len(original)], list(original))  # top-level order kept
        for key in original:
            if key != "hooks":
                self.assertEqual(data[key], original[key])
        for event, groups in original.get("hooks", {}).items():
            # every existing group (Beyin's) unchanged and in place; ours only appended
            self.assertEqual(data["hooks"][event][:len(groups)], groups)
        for event, arg in (("Stop", "stop"), ("SessionStart", "session-start")):
            handlers = self.ours(data, event)
            self.assertEqual(len(handlers), 1)
            command = decoded(handlers[0]["command"])
            self.assertIn(os.path.join(".claude", "skills", "vault-live", "scripts", "vault_live.py"), command)
            self.assertIn(("'hook' '%s'" % arg) if os.name == "nt" else ("hook " + arg), command)
            self.assertEqual(data["hooks"][event][-1], {"hooks": handlers})
        for event in data["hooks"]:
            if event not in ("Stop", "SessionStart"):
                self.assertEqual(self.ours(data, event), [])

        # an outdated handler of ours is updated in place, not duplicated
        stale = json.loads(first.decode("utf-8"))
        stale["hooks"]["Stop"][-1]["hooks"][0]["command"] = "python3 /old/place/vault_live.py hook stop"
        stale["hooks"]["Stop"].append({"hooks": [{"type": "command", "command": "echo other"}]})
        settings.write_text(json.dumps(stale, indent=2) + "\n", encoding="utf-8")
        self.run_cli("install-hooks")
        data = json.loads(settings.read_text(encoding="utf-8"))
        self.assertEqual(len(self.ours(data, "Stop")), 1)
        self.assertEqual(data["hooks"]["Stop"][-1]["hooks"][0]["command"], "echo other")

        code, out, err = self.run_cli("uninstall-hooks")
        self.assertEqual(code, 0, err)
        self.assertIn("removed 2", out)
        data = json.loads(settings.read_text(encoding="utf-8"))
        expected = json.loads(original_bytes.decode("utf-8-sig"))
        expected.setdefault("hooks", {}).setdefault("Stop", []).append(
            {"hooks": [{"type": "command", "command": "echo other"}]})
        self.assertEqual(data, expected)
        self.assertEqual(list(data), list(expected))
        before = settings.read_bytes()
        code, out, _ = self.run_cli("uninstall-hooks")
        self.assertIn("removed 0", out)
        self.assertEqual(settings.read_bytes(), before)

    def test_uninstall_keeps_foreign_handlers_in_a_shared_group(self):
        settings = self.tmp / "settings.json"
        mixed = {"hooks": {"Stop": [{"hooks": [
            {"type": "command", "command": "python3 /x/vault_live.py hook stop"},
            {"type": "command", "command": "python3 /x/beyin_v3_bridge.py"}]}]}, "model": "x"}
        settings.write_text(json.dumps(mixed), encoding="utf-8")
        code, out, _ = self.run_cli("uninstall-hooks")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(settings.read_text(encoding="utf-8")),
                         {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "python3 /x/beyin_v3_bridge.py"}]}]},
                          "model": "x"})

    def test_install_into_missing_settings(self):
        code, out, err = self.run_cli("install-hooks")
        self.assertEqual(code, 0, err)
        data = json.loads((self.tmp / "settings.json").read_text(encoding="utf-8"))
        self.assertEqual(sorted(data["hooks"]), ["SessionStart", "Stop"])
        self.assertFalse((self.tmp / "settings.json.vault-live.bak").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
