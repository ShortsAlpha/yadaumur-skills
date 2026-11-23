#!/usr/bin/env python3
"""Tests for disk_cleanup.py. Temp folders only: a synthetic home with DISK_CLEANUP_HOME and
DISK_CLEANUP_PLATFORM=darwin, fake free space, no external tools (xcrun, brew, npm are never run;
git runs only on a temp repo). Never touches the real home, ~/.claude or settings.json.

Run: py -3 test_disk_cleanup.py   (macOS: python3 test_disk_cleanup.py)
"""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import plistlib
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unicodedata
import unittest
from unittest import mock

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("disk_cleanup", str(HERE / "disk_cleanup.py"))
dc = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(dc)
DAY = 86400
GB = 1000 ** 3
REAL_HOME = os.path.realpath(str(Path.home()))


def _rmtree(path):
    def retry(func, target, _):
        os.chmod(target, stat.S_IWRITE)
        func(target)
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=retry)
    else:
        shutil.rmtree(path, onerror=retry)


def make_link(target, link):
    """A directory symlink; on Windows without symlink rights, a junction. Skips the test if neither works."""
    try:
        os.symlink(target, link, target_is_directory=True)
        return
    except (OSError, NotImplementedError):
        pass
    if os.name == "nt":
        try:
            import _winapi
            _winapi.CreateJunction(str(target), str(link))
            return
        except Exception:
            pass
    raise unittest.SkipTest("cannot create directory links here")


class Base(unittest.TestCase):
    FREE = 200 * GB
    TOTAL = 245 * GB

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dc-")).resolve()
        self.home = self.tmp / "home"
        self.tmpdir = self.tmp / "tmp"
        self.outside = self.tmp / "outside"
        self.systmp = self.tmp / "systmp"
        for folder in (self.home, self.tmpdir, self.outside, self.systmp):
            folder.mkdir()
        env = {k: v for k, v in os.environ.items() if not k.startswith(("DISK_CLEANUP", "GIT_"))}
        env.update(DISK_CLEANUP_TEST="1", DISK_CLEANUP_HOME=str(self.home), DISK_CLEANUP_TMPDIR=str(self.tmpdir),
                   DISK_CLEANUP_SYSTMP=str(self.systmp), DISK_CLEANUP_PLATFORM="darwin",
                   DISK_CLEANUP_FAKE_DISK="%d,%d" % (self.FREE, self.TOTAL),
                   DISK_CLEANUP_PROCS="", DISK_CLEANUP_PATH="", HOME=str(self.home), USERPROFILE=str(self.home))
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.assertNotEqual(os.path.realpath(dc.home()), REAL_HOME)
        self.assertTrue(dc.state_dir().startswith(str(self.tmp)))
        self.now = time.time()

    def tearDown(self):
        _rmtree(self.tmp)

    # ------------------------------------------------------------ helpers

    def cli(self, *args, stdin="", env=None):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, env or {}), mock.patch.object(sys, "stdin", io.StringIO(stdin)), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = dc.main(list(args))
        return code, out.getvalue(), err.getvalue()

    def h(self, rel):
        return self.home / rel

    def mk(self, path, data=b"x" * 100):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def age(self, path, days):
        stamp = time.time() - days * DAY
        path = str(path)
        for root, dirs, files in os.walk(path, topdown=False):
            for name in files + dirs:
                full = os.path.join(root, name)
                if not os.path.islink(full):
                    os.utime(full, (stamp, stamp))
        os.utime(path, (stamp, stamp))

    def scan(self, *flags):
        code, out, err = self.cli("scan", "--json", *flags)
        self.assertEqual(code, 0, err)
        return json.loads(out)

    def ids(self, result):
        return {item["id"]: item["tier"] for item in result["items"]}

    def item(self, result, item_id):
        return next(i for i in result["items"] if i["id"] == item_id)

    def setup_git(self):
        git = shutil.which("git")
        if not git:
            self.skipTest("git not installed")
        gitconfig = self.tmp / "gitconfig"
        gitconfig.write_text("", encoding="utf-8")
        os.environ.update(GIT_CONFIG_GLOBAL=str(gitconfig), GIT_CONFIG_NOSYSTEM="1",
                          GIT_CEILING_DIRECTORIES=str(self.tmp), GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.com",
                          GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.com")
        patcher = mock.patch.object(dc, "which", lambda name: git if name == "git" else None)
        patcher.start()
        self.addCleanup(patcher.stop)
        return git

    def git(self, *args, cwd):
        subprocess.run(["git"] + list(args), cwd=str(cwd), check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       timeout=60)

    def write_config(self, **cfg):
        self.mk(Path(dc.state_dir()) / "config.json", json.dumps(cfg).encode())

    def write_state(self, **state):
        self.mk(Path(dc.state_dir()) / "state.json", json.dumps(state).encode())

    def read_state(self):
        path = Path(dc.state_dir()) / "state.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

    def history(self):
        path = Path(dc.state_dir()) / "history.jsonl"
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []

    def fixture(self):
        """A synthetic Mac home: DerivedData, projects, caches, temp folders, personal data."""
        dd = self.h("Library/Developer/Xcode/DerivedData")
        existing = self.h("Developer/old-ios/App.xcodeproj")
        self.mk(existing / "project.pbxproj")
        for name, workspace in (("Gone-abc", self.tmp / "gone" / "Gone.xcodeproj"), ("Old-def", existing),
                                ("Live-ghi", existing)):
            self.mk(dd / name / "Build" / "Products" / "x.o")
            with open(str(dd / name / "info.plist"), "wb") as handle:
                plistlib.dump({"WorkspacePath": str(workspace)}, handle)
        dev = self.h("Developer")
        self.mk(dev / "old-web/package.json", b"{}")
        self.mk(dev / "old-web/src/app.js")
        self.mk(dev / "old-web/node_modules/a/index.js")
        self.mk(dev / "old-web/.next/cache/x")
        self.mk(dev / "new-web/package.json", b"{}")
        self.mk(dev / "new-web/node_modules/x/index.js")
        self.mk(dev / "old-swift/Package.swift")
        self.mk(dev / "old-swift/.build/debug/x")
        self.mk(dev / "old-ios/Podfile")
        self.mk(dev / "old-ios/Pods/A/x")
        self.mk(dev / "old-ios/build/x")
        self.mk(dev / "old-misc/README.md")
        self.mk(dev / "old-misc/build/x")          # no marker: not an artifact
        self.mk(dev / "old-misc/node_modules/x")   # no package.json: not an artifact
        self.mk(dev / "old-mono/package.json", b"{}")
        self.mk(dev / "old-mono/node_modules/y")
        self.mk(dev / "old-mono/packages/ui/package.json", b"{}")
        self.mk(dev / "old-mono/packages/ui/node_modules/x")
        self.mk(dev / "old-electron/package.json", b"{}")  # electron-builder: build/ holds real sources
        self.mk(dev / "old-electron/build/icon.icns")
        self.mk(dev / "old-electron/build/entitlements.mac.plist")
        self.mk(self.h(".npm/_cacache/index"))
        self.mk(self.h(".npm/_npx/abc/x"))
        self.mk(self.h("Library/Caches/Yarn/x"))
        self.mk(self.h("Library/Caches/ms-playwright/chromium/x"))
        self.mk(self.h("Library/Developer/Xcode/Archives/2026/App.xcarchive/x"))
        self.mk(self.h(".Trash/old.zip"))
        self.mk(self.tmpdir / "claude/old-sess/scratchpad/x")
        self.mk(self.tmpdir / "claude/new-sess/x")
        self.mk(self.systmp / "claude-501/old-sess/x")
        self.mk(self.systmp / "claude-501/new-sess/x")
        self.mk(self.systmp / "other/old/x")
        transcripts = self.h(".claude/projects/-x-app")
        self.mk(transcripts / "old.jsonl")
        self.mk(transcripts / "memory/MEMORY.md")
        for path in (dd / "Gone-abc", dd / "Old-def", dev / "old-web", dev / "old-swift", dev / "old-ios",
                     dev / "old-misc", dev / "old-mono", dev / "old-electron", self.tmpdir / "claude/old-sess",
                     self.systmp / "claude-501/old-sess", self.systmp / "other", transcripts):
            self.age(path, 60 if "claude" not in str(path) else 10)
        self.mk(transcripts / "new.jsonl")

    EXPECTED = {"derived-data-orphans": "A", "derived-data-stale": "A", "npm-cache": "A", "yarn-cache": "A",
                "claude-temp": "A", "derived-data-active": "B", "playwright-browsers": "B",
                "project:Developer/old-web": "B", "project:Developer/old-swift": "B",
                "project:Developer/old-ios": "B", "project:Developer/old-mono": "B",
                "project:Developer/old-mono/packages/ui": "B",
                "xcode-archives": "C", "trash": "C", "claude-transcripts": "C"}


class ScanTest(Base):
    def test_scan_finds_expected_ids_and_tiers(self):
        self.fixture()
        result = self.scan()
        self.assertEqual(self.ids(result), self.EXPECTED)
        dd = self.h("Library/Developer/Xcode/DerivedData")
        self.assertEqual(self.item(result, "derived-data-orphans")["paths"], [str(dd / "Gone-abc")])
        self.assertEqual(self.item(result, "derived-data-stale")["paths"], [str(dd / "Old-def")])
        self.assertEqual(self.item(result, "derived-data-active")["paths"], [str(dd / "Live-ghi")])
        ios = self.item(result, "project:Developer/old-ios")["paths"]
        self.assertEqual(sorted(os.path.basename(p) for p in ios), ["Pods", "build"])
        self.assertEqual(self.item(result, "project:Developer/old-mono")["paths"],
                         [str(self.h("Developer/old-mono/node_modules"))])
        self.assertEqual(self.item(result, "project:Developer/old-mono/packages/ui")["paths"],
                         [str(self.h("Developer/old-mono/packages/ui/node_modules"))])
        self.assertEqual(sorted(self.item(result, "claude-temp")["paths"]),
                         sorted([str(self.tmpdir / "claude" / "old-sess"),
                                 str(self.systmp / "claude-501" / "old-sess")]))
        self.assertGreaterEqual(result["duration_s"], 0)
        self.assertEqual([os.path.basename(p) for p in self.item(result, "claude-transcripts")["paths"]],
                         ["old.jsonl"])
        item = self.item(result, "npm-cache")
        for key in ("id", "tier", "title", "note", "paths", "commands", "size_bytes", "size_complete", "access",
                    "mtime", "age_days", "deletable", "meta"):
            self.assertIn(key, item)
        self.assertGreater(item["size_bytes"], 0)
        self.assertTrue(item["size_complete"])
        self.assertTrue((Path(dc.state_dir()) / "last-scan.json").exists())
        self.assertAlmostEqual(self.read_state()["last_scan"], self.now, delta=60)
        self.assertEqual(self.history()[-1]["type"], "scan")

    def test_include_active_lists_active_projects(self):
        self.fixture()
        self.assertNotIn("project:Developer/new-web", self.ids(self.scan()))
        result = self.scan("--include-active")
        self.assertEqual(self.ids(result)["project:Developer/new-web"], "B")
        self.assertTrue(self.item(result, "project:Developer/new-web")["meta"]["active"])

    def test_text_output(self):
        self.fixture()
        code, out, _ = self.cli("scan")
        self.assertEqual(code, 0)
        self.assertIn("Disk: free 200.0 GB of 245.0 GB (82 %)", out)
        self.assertIn("A  cache", out)
        self.assertIn("C  does not come back", out)
        self.assertIn("project:Developer/old-web", out)
        self.assertRegex(out, r"Scan took \d+ s\.")

    def test_hide_and_exclude_at_scan(self):
        self.fixture()
        self.write_config(hide=["yarn-cache"], exclude=["~/Developer/old-web/node_modules", "~/.npm"])
        result = self.scan()
        self.assertNotIn("yarn-cache", self.ids(result))
        self.assertNotIn("npm-cache", self.ids(result))
        web = self.item(result, "project:Developer/old-web")["paths"]
        self.assertEqual([os.path.basename(p) for p in web], [".next"])

    def test_no_access_is_reported_not_a_crash(self):
        self.fixture()
        real_scandir = os.scandir
        trash = str(self.h(".Trash"))

        def scandir(path="."):
            if os.path.normcase(str(path)) == os.path.normcase(trash):
                raise PermissionError(1, "Operation not permitted", trash)
            return real_scandir(path)

        with mock.patch.object(dc.os, "scandir", scandir):
            result = self.scan()
        trash_item = self.item(result, "trash")
        self.assertEqual(trash_item["access"], "no access")
        self.assertFalse(trash_item["deletable"])

    def test_growth_and_free_space_change(self):
        self.fixture()
        self.scan()
        self.mk(self.h("Library/Caches/Yarn/big"), b"y" * 200000)
        with mock.patch.object(dc, "GROWTH_MIN_BYTES", 50000):
            result = self.scan()  # same fake disk: delta 0
            self.assertEqual(result["since_previous"]["delta_bytes"], 0)
            result = json.loads(self.cli("scan", "--json", env={"DISK_CLEANUP_FAKE_DISK": "%d,%d" % (
                190 * GB, self.TOTAL)})[1])
        self.assertEqual(result["since_previous"]["kind"], "scan")
        self.assertEqual(result["since_previous"]["delta_bytes"], -10 * GB)
        self.mk(self.h("Library/Caches/Yarn/bigger"), b"y" * 200000)
        with mock.patch.object(dc, "GROWTH_MIN_BYTES", 50000):
            result = self.scan()
        self.assertEqual([r["id"] for r in result["growth"]], ["yarn-cache"])
        scans = [r for r in self.history() if r["type"] == "scan"]
        self.assertEqual(len(scans), 4)
        self.assertIn("yarn-cache", scans[-1]["items"])

    def test_downloads_big_old_files(self):
        self.mk(self.h("Downloads/small.zip"))
        with open(str(self.h("Downloads/huge.iso")), "wb") as handle:
            handle.truncate(GB + 1)
        self.mk(self.h("Downloads/sub/x"))
        with open(str(self.h("Downloads/sub/disk.dmg")), "wb") as handle:
            handle.truncate(GB)
        self.assertFalse(any(i.startswith("download:") for i in self.ids(self.scan())))  # too new
        later = str(time.time() + 40 * DAY)
        code, out, err = self.cli("scan", "--json", env={"DISK_CLEANUP_NOW": later})
        ids = self.ids(json.loads(out))
        self.assertEqual(ids.get("download:huge.iso"), "C")
        self.assertEqual(ids.get("download:sub/disk.dmg"), "C")
        self.assertNotIn("download:small.zip", ids)


class CleanTest(Base):
    def test_dry_run_deletes_nothing(self):
        self.fixture()
        self.scan()
        before = sorted(str(p) for p in self.tmp.rglob("*"))
        code, out, err = self.cli("clean", "npm-cache", "derived-data-orphans", "project:Developer/old-web")
        self.assertEqual(code, 0, out + err)
        self.assertIn("Dry run", out)
        self.assertIn(str(self.h(".npm/_cacache")), out)
        self.assertIn(str(self.h("Developer/old-web/node_modules")), out)
        after = sorted(str(p) for p in self.tmp.rglob("*"))
        self.assertEqual(before, after)

    def test_yes_deletes_exactly_the_chosen_items(self):
        self.fixture()
        self.scan()
        before = set(str(p) for p in self.tmp.rglob("*"))
        with mock.patch.object(dc, "disk_usage", side_effect=[(100 * GB, self.TOTAL), (103 * GB, self.TOTAL)]):
            code, out, err = self.cli("clean", "npm-cache", "derived-data-orphans", "project:Developer/old-web",
                                      "--yes")
        self.assertEqual(code, 0, out + err)
        self.assertIn("really freed +3.0 GB", out)
        gone = [self.h(".npm/_cacache"), self.h(".npm/_npx"), self.h("Library/Developer/Xcode/DerivedData/Gone-abc"),
                self.h("Developer/old-web/node_modules"), self.h("Developer/old-web/.next")]
        for path in gone:
            self.assertFalse(path.exists(), path)
        after = set(str(p) for p in self.tmp.rglob("*"))
        removed = before - after
        for path in removed:
            self.assertTrue(any(path == str(g) or path.startswith(str(g) + os.sep) for g in gone), path)
        for path in (self.h(".npm"), self.h("Developer/old-web/package.json"), self.h("Developer/old-web/src/app.js"),
                     self.h("Library/Developer/Xcode/DerivedData/Old-def"), self.h("Developer/new-web/node_modules")):
            self.assertTrue(path.exists(), path)
        record = self.history()[-1]
        self.assertEqual(record["type"], "clean")
        self.assertEqual(sorted(record["ids"]), sorted(["npm-cache", "derived-data-orphans",
                                                        "project:Developer/old-web"]))
        self.assertEqual(record["freed"], 3 * GB)
        self.assertGreater(record["estimated"], 0)
        self.assertAlmostEqual(self.read_state()["last_clean"], self.now, delta=60)

    def test_tier_c_needs_permanent(self):
        self.fixture()
        self.scan()
        archives = self.h("Library/Developer/Xcode/Archives")
        code, out, _ = self.cli("clean", "xcode-archives", "--yes")
        self.assertEqual(code, 1)
        self.assertIn("--permanent", out)
        self.assertTrue(archives.exists())
        self.assertNotIn("last_clean", self.read_state())
        code, out, err = self.cli("clean", "xcode-archives", "trash", "--yes", "--permanent")
        self.assertEqual(code, 0, out + err)
        self.assertFalse(archives.exists())
        self.assertFalse(self.h(".Trash/old.zip").exists())
        self.assertTrue(self.h(".Trash").exists())

    def test_stale_or_missing_scan_is_refused(self):
        self.fixture()
        code, out, _ = self.cli("clean", "npm-cache", "--yes")
        self.assertEqual(code, 1)
        self.assertIn("run scan first", out)
        self.scan()
        later = str(time.time() + 25 * 3600)
        code, out, _ = self.cli("clean", "npm-cache", "--yes", env={"DISK_CLEANUP_NOW": later})
        self.assertEqual(code, 1)
        self.assertIn("rescan first", out)
        self.assertTrue(self.h(".npm/_cacache/index").exists())

    def test_unknown_id_and_changed_item(self):
        self.fixture()
        self.scan()
        code, out, _ = self.cli("clean", "nope", "--yes")
        self.assertEqual(code, 1)
        self.assertIn("not in the last scan", out)
        # touched since the scan: the stale DerivedData folder is now active and must not be deleted
        self.mk(self.h("Library/Developer/Xcode/DerivedData/Old-def/Logs/Build/new.xcactivitylog"))
        code, out, _ = self.cli("clean", "derived-data-stale", "--yes")
        self.assertEqual(code, 1)
        self.assertIn("no longer matches", out)
        self.assertTrue(self.h("Library/Developer/Xcode/DerivedData/Old-def/Build").exists())

    def test_symlink_inside_home_is_removed_as_a_link(self):
        target = self.outside / "yarn"
        self.mk(target / "keep.txt")
        self.h("Library/Caches").mkdir(parents=True)
        make_link(target, self.h("Library/Caches/Yarn"))
        self.assertIn("yarn-cache", self.ids(self.scan()))
        code, out, err = self.cli("clean", "yarn-cache", "--yes")
        self.assertEqual(code, 0, out + err)
        self.assertFalse(os.path.lexists(str(self.h("Library/Caches/Yarn"))))
        self.assertTrue((target / "keep.txt").exists())

    def test_path_resolving_outside_home_is_refused(self):
        target = self.outside / "cache"
        self.mk(target / "uv" / "keep.txt")
        make_link(target, self.h(".cache"))
        self.assertIn("uv-cache", self.ids(self.scan()))
        code, out, _ = self.cli("clean", "uv-cache")
        self.assertEqual(code, 1)
        self.assertIn("outside the home folder", out)
        code, out, _ = self.cli("clean", "uv-cache", "--yes")
        self.assertEqual(code, 1)
        self.assertTrue((target / "uv" / "keep.txt").exists())

    def test_excluded_after_the_scan_is_never_touched(self):
        self.fixture()
        self.scan()
        self.write_config(exclude=["~/Developer/old-web/node_modules"])
        code, out, err = self.cli("clean", "project:Developer/old-web", "--yes")
        self.assertEqual(code, 0, out + err)
        self.assertTrue(self.h("Developer/old-web/node_modules/a/index.js").exists())
        self.assertFalse(self.h("Developer/old-web/.next").exists())

    def test_check_path_protections(self):
        self.fixture()
        self.write_config(exclude=["~/Library/Caches/Yarn"])
        cfg, _ = dc.load_config()
        ctx = dc.Context(cfg)
        item = {"id": "x", "root": "home", "meta": {}}
        for rel in ("", "Library", "Library/Caches", "Developer", "Library/Developer/Xcode/DerivedData"):
            self.assertEqual(dc.check_path(str(self.h(rel)) if rel else str(self.home), item, ctx),
                             "protected folder", rel)
        self.assertEqual(dc.check_path(str(self.h("Library/Caches/Yarn/x")), item, ctx), "excluded in config")
        self.assertEqual(dc.check_path(str(self.outside), item, ctx), "outside the home folder")
        self.assertEqual(dc.check_path(str(self.tmpdir / "claude/old-sess"), item, ctx), "outside the home folder")
        self.assertIsNone(dc.check_path(str(self.tmpdir / "claude/old-sess"), dict(item, root="tmp"), ctx))
        self.assertEqual(dc.check_path(str(self.tmpdir / "claude"), dict(item, root="tmp"), ctx), "protected folder")
        project = {"id": "p", "root": "home", "meta": {"project_dir": str(self.h("Developer/old-web"))}}
        self.assertEqual(dc.check_path(str(self.h("Developer/old-web")), project, ctx), "protected folder")
        self.assertEqual(dc.check_path(str(self.h("Developer/old-ios/Pods")), project, ctx), "not inside its project")
        self.assertIsNone(dc.check_path(str(self.h("Developer/old-web/node_modules")), project, ctx))

    def test_in_use_items_are_skipped(self):
        self.fixture()
        self.scan()
        code, out, _ = self.cli("clean", "derived-data-orphans", "yarn-cache", "--yes",
                                env={"DISK_CLEANUP_PROCS": "Xcode,launchd"})
        self.assertEqual(code, 1)
        self.assertIn("in use: Xcode is running", out)
        self.assertTrue(self.h("Library/Developer/Xcode/DerivedData/Gone-abc").exists())
        self.assertFalse(self.h("Library/Caches/Yarn").exists())
        code, out, _ = self.cli("clean", "npm-cache", "--yes", env={"DISK_CLEANUP_PROCS": "npm"})
        self.assertEqual(code, 1)
        self.assertIn("npm is running", out)
        self.assertTrue(self.h(".npm/_cacache/index").exists())

    def test_tool_command_and_fallback(self):
        self.fixture()
        calls = []
        tools = {"npm": "/stub/npm"}
        with mock.patch.object(dc, "which", lambda name: tools.get(name)), \
                mock.patch.object(dc, "run_cmd", lambda argv, timeout: (calls.append(argv) or (0, "", ""))):
            self.scan()
            code, out, _ = self.cli("clean", "npm-cache")
            self.assertIn("run:    /stub/npm cache clean --force", out)
            code, out, err = self.cli("clean", "npm-cache", "--yes")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(calls, [["/stub/npm", "cache", "clean", "--force"]])
        self.assertTrue(self.h(".npm/_cacache/index").exists())  # the command's job (stubbed here)
        self.assertFalse(self.h(".npm/_npx").exists())           # not covered by the command: removed directly
        with mock.patch.object(dc, "which", lambda name: tools.get(name)), \
                mock.patch.object(dc, "run_cmd", lambda argv, timeout: (1, "", "npm ERR! boom")):
            self.scan()
            code, out, _ = self.cli("clean", "npm-cache", "--yes")
        self.assertEqual(code, 0, out)
        self.assertIn("npm failed: npm ERR! boom", out)
        self.assertFalse(self.h(".npm/_cacache").exists())       # fallback after the failed command

    def test_non_mac_refuses(self):
        self.fixture()
        env = {"DISK_CLEANUP_PLATFORM": "linux"}
        for args in (("scan",), ("clean", "npm-cache", "--yes"), ("snooze",), ("install-hook",)):
            code, out, _ = self.cli(*args, env=env)
            self.assertEqual(code, 1, args)
            self.assertIn("macOS only", out)
        self.assertFalse(Path(dc.state_dir()).exists())
        self.assertTrue(self.h(".npm/_cacache/index").exists())
        self.assertEqual(self.cli("status", env=env)[0], 0)
        self.assertEqual(self.cli("config", env=env)[0], 0)


class WorktreeTest(Base):
    def test_stale_worktrees(self):
        self.setup_git()
        repo = self.h("Developer/repo")
        self.mk(repo / "README.md")
        self.mk(repo / ".gitignore", b".env\nnode_modules/\n")
        self.git("init", "-q", cwd=repo)
        self.git("add", ".", cwd=repo)
        self.git("commit", "-qm", "init", cwd=repo)
        names = ("wt-clean", "wt-dirty", "wt-env", "wt-deps", "wt-fresh")
        for name in names:
            self.git("worktree", "add", "-q", "-b", name, os.path.join(".claude", "worktrees", name), cwd=repo)
        wts = repo / ".claude" / "worktrees"
        self.mk(wts / "wt-dirty/notes.txt")             # untracked: uncommitted changes
        self.mk(wts / "wt-env/.env", b"TOKEN=synthetic")  # ignored and not an artifact: must survive
        self.mk(wts / "wt-deps/node_modules/x/index.js")  # ignored artifact: removed with the worktree
        self.age(repo, 60)
        self.mk(wts / "wt-fresh/new.txt")
        os.utime(str(repo / ".git/worktrees/wt-fresh/index"))
        ids = self.ids(self.scan())
        prefix = "worktree:Developer/repo/.claude/worktrees/"
        for name in names[:-1]:
            self.assertEqual(ids.get(prefix + name), "B", name)
        self.assertNotIn(prefix + "wt-fresh", ids)
        code, out, err = self.cli("clean", *[prefix + n for n in names[:-1]])
        self.assertIn("wt-env: skip: ignored files that are not build artifacts: .env", out)
        code, out, err = self.cli("clean", *[prefix + n for n in names[:-1]], "--yes")
        self.assertEqual(code, 1, out + err)
        self.assertIn("wt-dirty: skipped: uncommitted changes", out)
        self.assertIn("wt-env: skipped: ignored files that are not build artifacts: .env", out)
        self.assertFalse((wts / "wt-clean").exists())
        self.assertFalse((wts / "wt-deps").exists())
        self.assertTrue((wts / "wt-dirty/notes.txt").exists())
        self.assertTrue((wts / "wt-env/.env").exists())
        self.assertTrue((repo / "README.md").exists())


class GitProjectTest(Base):
    def make_repo(self, rel, ignore=b"node_modules/\n/build/\n"):
        repo = self.h(rel)
        self.mk(repo / "package.json", b"{}")
        self.mk(repo / "Podfile")
        self.mk(repo / ".gitignore", ignore)
        self.mk(repo / "build/icon.icns")       # committed although build/ is ignored
        self.mk(repo / "Pods/A/x")              # committed Pods
        self.git("init", "-q", cwd=repo)
        self.git("add", ".", cwd=repo)
        self.git("add", "-f", "build/icon.icns", cwd=repo)
        self.git("commit", "-qm", "init", cwd=repo)
        self.mk(repo / "node_modules/x/index.js")  # ignored, untracked: a real artifact
        self.mk(repo / ".next/cache/x")            # not ignored: never listed inside git
        return repo

    def test_only_ignored_untracked_artifacts_in_git(self):
        self.setup_git()
        repo = self.make_repo("Developer/gitapp")
        self.age(repo, 60)
        result = self.scan()
        self.assertEqual(self.item(result, "project:Developer/gitapp")["paths"], [str(repo / "node_modules")])
        code, out, err = self.cli("clean", "project:Developer/gitapp", "--yes")
        self.assertEqual(code, 0, out + err)
        self.assertFalse((repo / "node_modules").exists())
        for rel in ("build/icon.icns", "Pods/A/x", ".next/cache/x", "package.json"):
            self.assertTrue((repo / rel).exists(), rel)

    def test_artifact_tracked_after_the_scan_is_kept(self):
        self.setup_git()
        repo = self.make_repo("Developer/gitapp")
        self.age(repo, 60)
        self.assertIn("project:Developer/gitapp", self.ids(self.scan("--include-active")))
        self.git("add", "-f", "node_modules/x/index.js", cwd=repo)
        self.git("commit", "-qm", "vendor", cwd=repo)
        code, out, _ = self.cli("clean", "project:Developer/gitapp", "--yes")
        self.assertEqual(code, 1)
        self.assertIn("no longer matches", out)
        self.assertTrue((repo / "node_modules/x/index.js").exists())

    def test_worktree_only_work_keeps_the_project_active(self):
        self.setup_git()
        repo = self.make_repo("Developer/gitapp")
        self.git("worktree", "add", "-q", "-b", "agent", os.path.join(".claude", "worktrees", "agent"), cwd=repo)
        self.age(repo, 60)
        self.assertIn("project:Developer/gitapp", self.ids(self.scan()))
        os.utime(str(repo / ".git/worktrees/agent/index"))  # an agent committed in its worktree just now
        self.assertNotIn("project:Developer/gitapp", self.ids(self.scan()))

    def test_build_ignored_only_globally_is_kept(self):
        self.setup_git()
        excludes = self.tmp / "global-ignore"
        excludes.write_text("build/\n", encoding="utf-8")
        (self.tmp / "gitconfig").write_text("[core]\n\texcludesFile = %s\n" % str(excludes).replace("\\", "/"),
                                            encoding="utf-8")
        app = self.h("Developer/electron-app")
        self.mk(app / "package.json", b"{}")
        self.mk(app / "main.js")
        self.git("init", "-q", cwd=app)
        self.git("add", ".", cwd=app)
        self.git("commit", "-qm", "init", cwd=app)
        self.mk(app / "build/icon.icns")              # never committed: the global ignore hides it
        ios = self.h("Developer/ios-app")
        self.mk(ios / "App.xcodeproj/project.pbxproj")
        self.git("init", "-q", cwd=ios)
        self.git("add", ".", cwd=ios)
        self.git("commit", "-qm", "init", cwd=ios)
        self.mk(ios / "build/App.app/x")              # Xcode build output: a global rule is enough
        self.age(self.h("Developer"), 60)
        ids = self.ids(self.scan())
        self.assertNotIn("project:Developer/electron-app", ids)
        self.assertIn("project:Developer/ios-app", ids)
        code, out, _ = self.cli("clean", "project:Developer/electron-app", "--yes")
        self.assertEqual(code, 1)
        self.assertTrue((app / "build/icon.icns").exists())
        self.mk(app / ".git/info/exclude", b"/build/\n")  # the repo's own rule: now it is an artifact
        self.age(app, 60)
        result = self.scan()
        self.assertEqual(self.item(result, "project:Developer/electron-app")["paths"], [str(app / "build")])

    def test_without_command_line_tools(self):
        self.mk(self.h("Developer/gitapp/.git/HEAD"))
        self.mk(self.h("Developer/gitapp/package.json"), b"{}")
        self.mk(self.h("Developer/gitapp/node_modules/x"))
        self.age(self.h("Developer/gitapp"), 60)
        with mock.patch.object(dc, "which", lambda name: "/usr/bin/" + name if name in ("git", "xcrun") else None), \
                mock.patch.object(dc, "command_line_tools_ok", lambda: False):
            self.assertIsNone(dc.tool("git"))
            result = self.scan()
        self.assertNotIn("project:Developer/gitapp", self.ids(result))
        self.assertTrue(any("Command Line Tools" in n for n in result["notes"]))
        with mock.patch.object(dc, "which", lambda name: "/usr/bin/git"), \
                mock.patch.object(dc, "command_line_tools_ok", lambda: True):
            self.assertEqual(dc.tool("git"), "/usr/bin/git")
        dc._CLT.clear()
        with mock.patch.object(dc.sys, "platform", "darwin"), \
                mock.patch.object(dc, "run_cmd", lambda argv, timeout: (2, "", "")):
            self.assertFalse(dc.command_line_tools_ok())
        dc._CLT.clear()


class ProjectRulesTest(Base):
    def test_root_is_never_a_project_and_deep_edits_count(self):
        dev = self.h("Developer")
        self.mk(dev / "package.json", b"{}")
        self.mk(dev / "node_modules/x")
        self.mk(dev / "active-app/package.json", b"{}")
        self.mk(dev / "active-app/node_modules/x")
        self.mk(dev / "active-app/src/deep/old.js")
        self.age(dev, 60)
        self.mk(dev / "active-app/src/deep/fresh.js")  # edited today, deep in the tree
        ids = self.ids(self.scan())
        self.assertNotIn("project:Developer", ids)
        self.assertNotIn("project:Developer/active-app", ids)
        result = self.scan("--include-active")
        self.assertIn("(active project)", self.item(result, "project:Developer/active-app")["note"])
        self.assertNotIn("project:Developer", self.ids(result))

    def test_build_outside_git_needs_an_xcode_or_gradle_marker(self):
        dev = self.h("Developer")
        self.mk(dev / "electron/package.json", b"{}")
        self.mk(dev / "electron/build/icon.icns")
        self.mk(dev / "android/build.gradle.kts")
        self.mk(dev / "android/build/out.apk")
        self.age(dev, 60)
        ids = self.ids(self.scan())
        self.assertNotIn("project:Developer/electron", ids)
        self.assertIn("project:Developer/android", ids)
        self.assertTrue(self.h("Developer/electron/build/icon.icns").exists())

    def test_darwin_case_and_unicode_normalisation(self):
        self.assertEqual(dc.norm("/Users/X/Caf\u00e9/Node_Modules"), dc.norm("/users/x/cafe\u0301/node_modules"))
        with mock.patch.dict(os.environ, {"DISK_CLEANUP_PLATFORM": "linux"}):
            self.assertNotEqual(dc.norm("/a/Caf\u00e9"), dc.norm("/a/cafe\u0301"))
        self.fixture()
        cafe = self.h("Developer/caf\u00e9-app")
        self.mk(cafe / "package.json", b"{}")
        self.mk(cafe / "node_modules/x")
        self.age(cafe, 60)
        self.assertIn("project:Developer/caf\u00e9-app", self.ids(self.scan()))
        self.write_config(exclude=["~/developer/OLD-WEB/node_modules",
                                   "~/Developer/" + unicodedata.normalize("NFD", "caf\u00e9-app"),
                                   "Developer/old-swift/.build"])  # relative: taken from the home folder
        result = self.scan()
        ids = self.ids(result)
        self.assertNotIn("project:Developer/caf\u00e9-app", ids)
        self.assertNotIn("project:Developer/old-swift", ids)
        self.assertEqual([os.path.basename(p) for p in self.item(result, "project:Developer/old-web")["paths"]],
                         [".next"])
        cfg, _ = dc.load_config()
        self.assertEqual(dc.expand("Developer/x"), os.path.join(str(self.home), "Developer", "x"))


class SafetyDetailTest(Base):
    def test_tier_c_from_an_edited_scan_still_needs_permanent(self):
        self.fixture()
        self.scan()
        path = Path(dc.state_file("last-scan.json"))
        data = json.loads(path.read_text(encoding="utf-8"))
        for item in data["items"]:
            if item["id"] == "trash":
                item["tier"] = "A"
        path.write_text(json.dumps(data), encoding="utf-8")
        code, out, _ = self.cli("clean", "trash", "--yes")
        self.assertEqual(code, 1)
        self.assertIn("--permanent", out)
        self.assertTrue(self.h(".Trash/old.zip").exists())

    def test_runtime_that_became_tier_c_needs_permanent(self):
        calls = []
        state = {"devices": []}

        def fake_run(argv, timeout):
            calls.append(argv)
            if argv[1:] == ["simctl", "list", "-j", "devices"]:
                return 0, json.dumps({"devices": {"com.apple.CoreSimulator.SimRuntime.iOS-17-0": state["devices"]}}), ""
            if argv[1:] == ["simctl", "runtime", "list", "-j"]:
                return 0, json.dumps({"ABCD": {"identifier": "ABCD", "version": "17.0", "sizeBytes": 7 * GB,
                                               "runtimeIdentifier": "com.apple.CoreSimulator.SimRuntime.iOS-17-0"}}), ""
            return 0, "", ""

        with mock.patch.object(dc, "which", lambda name: "/stub/xcrun" if name == "xcrun" else None), \
                mock.patch.object(dc, "run_cmd", fake_run):
            self.assertEqual(self.ids(self.scan())["sim-runtime:iOS-17.0"], "B")
            state["devices"] = [{"udid": "U1", "isAvailable": True, "state": "Shutdown"}]
            code, out, _ = self.cli("clean", "sim-runtime:iOS-17.0", "--yes")
            self.assertEqual(code, 1)
            self.assertIn("--permanent", out)
            self.assertNotIn(["/stub/xcrun", "simctl", "runtime", "delete", "ABCD"], calls)
            code, out, _ = self.cli("clean", "sim-runtime:iOS-17.0", "--yes", "--permanent")
            self.assertEqual(code, 0, out)
        self.assertIn(["/stub/xcrun", "simctl", "runtime", "delete", "ABCD"], calls)

    def test_orphan_needs_a_certainly_missing_workspace(self):
        dd = self.h("Library/Developer/Xcode/DerivedData/Private-abc")
        workspace = str(self.h("Documents/App/App.xcodeproj"))
        self.mk(dd / "Build/x")
        with open(str(dd / "info.plist"), "wb") as handle:
            plistlib.dump({"WorkspacePath": workspace}, handle)
        self.age(dd, 60)
        real_stat = os.stat

        def stat(path, *args, **kwargs):
            if str(path) == workspace:
                raise PermissionError(1, "Operation not permitted", workspace)
            return real_stat(path, *args, **kwargs)

        with mock.patch.object(dc.os, "stat", stat):
            ids = self.ids(self.scan())
        self.assertNotIn("derived-data-orphans", ids)
        self.assertIn("derived-data-stale", ids)
        self.assertIn("derived-data-orphans", self.ids(self.scan()))  # really missing: an orphan

    def test_rmtree_handler(self):
        tree = self.h("Library/Caches/Yarn")
        locked = tree / "v6" / "pkg"
        self.mk(locked / "file.txt")
        os.chmod(str(locked / "file.txt"), stat.S_IREAD)
        os.chmod(str(locked), stat.S_IREAD | stat.S_IEXEC)  # posix: a read-only parent blocks unlink
        self.assertEqual(dc.remove_path(str(tree)), [])
        self.assertFalse(tree.exists())
        errors = []
        retry = dc.rmtree_retry(str(self.home), errors)
        retry(os.open, str(self.home / "x"), (OSError, OSError(13, "denied"), None))  # no TypeError
        self.assertEqual(len(errors), 1)
        outside = self.outside / "target"
        self.mk(outside / "keep.txt")
        root = self.h("Library/Caches/Other")
        root.mkdir(parents=True)
        make_link(outside, root / "link")
        chmods = []
        real_chmod = os.chmod
        def chmod(path, *args, **kwargs):
            chmods.append(str(path))
            return real_chmod(path, *args, **kwargs)

        with mock.patch.object(dc.os, "chmod", chmod):
            retry = dc.rmtree_retry(str(root), errors)
            retry(os.unlink, str(root / "link"), None)
        self.assertTrue(all(os.path.normcase(c) == os.path.normcase(str(root)) for c in chmods), chmods)
        self.assertTrue((outside / "keep.txt").exists())

    def test_scan_time_budget_and_lower_bounds(self):
        self.fixture()
        with mock.patch.object(dc, "SCAN_TOTAL", 0):
            result = self.scan()
        npm = self.item(result, "npm-cache")
        self.assertFalse(npm["size_complete"])
        self.assertIn("not measured", npm["note"])

    def test_downloads_skip_bundles_and_vanishing_files(self):
        with open(str(self.mk(self.h("Downloads/Old.photoslibrary/database/big.bin"))), "wb") as handle:
            handle.truncate(GB)
        with open(str(self.mk(self.h("Downloads/sub/disk.dmg"))), "wb") as handle:
            handle.truncate(GB)
        later = str(time.time() + 40 * DAY)
        ids = self.ids(json.loads(self.cli("scan", "--json", env={"DISK_CLEANUP_NOW": later})[1]))
        self.assertIn("download:sub/disk.dmg", ids)
        self.assertFalse(any("photoslibrary" in i for i in ids))

        class Vanished:
            name, path = "gone.iso", str(self.h("Downloads/gone.iso"))

            def is_file(self, follow_symlinks=True):
                return True

            def stat(self, follow_symlinks=True):
                raise FileNotFoundError(2, "No such file", self.path)

        real_scandir = os.scandir
        downloads = os.path.normcase(str(self.h("Downloads")))

        @contextlib.contextmanager
        def fake(path):
            yield iter([Vanished()])

        def scandir(path="."):
            return fake(path) if os.path.normcase(str(path)) == downloads else real_scandir(path)

        with mock.patch.object(dc.os, "scandir", scandir):
            code, out, err = self.cli("scan", "--json", env={"DISK_CLEANUP_NOW": later})
        self.assertEqual(code, 0, err)

    def test_process_names(self):
        names = dc.names_from_args([
            "/usr/bin/java -Xmx2g -cp /x/gradle-launcher.jar org.gradle.launcher.daemon.bootstrap.GradleDaemon 8.5",
            "node /opt/homebrew/lib/node_modules/corepack/dist/pnpm.cjs install",
            "node /usr/local/lib/node_modules/corepack/dist/yarn.js",
            "/usr/bin/python3 -m pip install x",
            "/bin/bash /opt/homebrew/Library/Homebrew/brew.sh cleanup"])
        for name in ("GradleDaemon", "pnpm", "yarn", "pip", "brew", "java"):
            self.assertIn(name, names)
        self.assertNotIn("go", dc.names_from_args(["/usr/bin/say go-build-cache --going"]))

    PROFILE = (";; Seatbelt profile for a simulator helper.\n"
               ";; XCODE_APP is resolved at launch, not hard-coded to\n"
               ";; /Applications/Xcode.app: an Xcode-beta or a second Xcode are normal.\n"
               "(version 1)\n"
               "(allow process-exec\n"
               "  (subpath \"/Applications/Xcode.app/Contents/MacOS/Xcode\")\n"
               "  (subpath (param \"XCODE_APP\")))\n"
               ";; Xcode Simulator xcodebuild npm\n"
               "/Applications/Xcode.app/Contents/Developer/usr/bin/xcodebuild\n")
    WRAPPED = ("/Applications/Some.app/Contents/Helpers/disclaimer -- /usr/bin/sandbox-exec -p " + PROFILE +
               " -D XCODE_APP=/Applications/Xcode.app -D HOME_DIR=/Users/someone /opt/helper/bin/sim-helper")
    XCODE = ("Xcode", "xcodebuild", "Simulator")

    def test_process_names_ignore_embedded_text(self):
        for escaped in (False, True):  # ps prints a line break inside an argument as \012
            line = self.WRAPPED.replace("\n", "\\012") if escaped else self.WRAPPED
            names = dc.names_from_args([line])
            for name in self.XCODE + ("npm",):
                self.assertNotIn(name, names, (escaped, name))
            self.assertIn("disclaimer", names)
            self.assertIn("sandbox-exec", names)
        for line in ('/usr/bin/log stream --predicate process contains "Xcode" and subsystem == "x"',
                     "/usr/bin/say Xcode is done", "/usr/bin/env XCODE=/Applications/Xcode.app/Contents/MacOS/Xcode",
                     "/usr/bin/git commit -m Xcode xcodebuild Simulator fixes",
                     "/bin/zsh -c source /tmp/snap.sh\\012export X=1\\012xcodebuild -scheme App"):
            self.assertFalse(set(self.XCODE) & dc.names_from_args([line]), line)

    def test_process_names_still_find_running_tools(self):
        dev = "/Applications/Xcode.app/Contents/Developer"
        names = dc.names_from_args([
            "/Applications/Xcode.app/Contents/MacOS/Xcode",
            dev + "/usr/bin/xcodebuild -scheme App -destination platform=iOS Simulator,name=iPhone 16",
            dev + "/Applications/Simulator.app/Contents/MacOS/Simulator -CurrentDeviceUDID ABC"])
        for name in self.XCODE:
            self.assertIn(name, names)
        self.assertIn("Xcode", dc.names_from_args(["/Applications/Xcode 16.app/Contents/MacOS/Xcode"]))
        self.assertIn("npm", dc.names_from_args(["npm run dev"]))
        self.assertIn("npm", dc.names_from_args(["/bin/bash -c npm run dev"]))
        self.assertIn("npm", dc.names_from_args(["node /opt/homebrew/lib/node_modules/npm/bin/npm-cli.js ci"]))
        self.assertIn("playwright", dc.names_from_args(["node /x/node_modules/playwright/cli.js test"]))
        self.assertIn("pip", dc.names_from_args(["python3.12 -m pip install -r req.txt"]))
        self.assertIn("pod", dc.names_from_args(["/usr/bin/ruby /opt/homebrew/bin/pod install"]))
        self.assertIn("brew", dc.names_from_args(["/bin/bash brew.sh update"]))
        self.assertIn("gradlew", dc.names_from_args(["/bin/sh ./gradlew assembleDebug"]))
        self.assertIn("gradle", dc.names_from_args(["/usr/bin/java -cp /a/b.jar:/u/.gradle/w/gradle-wrapper.jar "
                                                    "org.gradle.wrapper.GradleWrapperMain build"]))
        self.assertIn("xcodebuild", dc.names_from_args(
            ["/Applications/Some.app/Contents/Helpers/disclaimer -- " + dev + "/usr/bin/xcodebuild test"]))

    def test_ps_continuation_lines_are_one_process(self):
        out = ("    1 /sbin/launchd\n"
               "  950 " + self.WRAPPED + "\n"
               "12 apples\n"
               "98765 /usr/bin/true\n"
               "123456 /usr/local/bin/go build ./...\n")
        procs = dc.split_ps(out)
        self.assertEqual([pid for pid, _ in procs], ["1", "950", "98765", "123456"])
        self.assertEqual(procs[1][1], self.WRAPPED + "\n12 apples")
        names = dc.names_from_args([args for _, args in procs])
        self.assertFalse(set(self.XCODE) & names)
        self.assertNotIn("apples", names)
        self.assertIn("go", names)
        with mock.patch.dict(os.environ, {"DISK_CLEANUP_TEST": "0"}), \
                mock.patch.object(dc, "run_cmd", return_value=(0, out, "")), \
                mock.patch.object(dc, "which", return_value="ps"):
            running = dc.running_names()
        self.assertIn("go", running)
        self.assertNotIn("Xcode", running)

    def test_test_env_needs_the_test_switch(self):
        self.assertEqual(dc.test_env("DISK_CLEANUP_PROCS"), "")
        with mock.patch.dict(os.environ, {"DISK_CLEANUP_TEST": "0"}):
            self.assertIsNone(dc.test_env("DISK_CLEANUP_PROCS"))
            self.assertEqual(dc.platform(), sys.platform)
            self.assertIsNone(dc.test_env("DISK_CLEANUP_HOME"))
            self.assertEqual(dc.home(), os.path.abspath(str(Path.home())))  # HOME is the fake home here

    def test_same_temp_folder_is_listed_once(self):
        self.mk(self.systmp / "claude/old-sess/x")
        self.age(self.systmp / "claude", 10)
        result = json.loads(self.cli("scan", "--json", env={"DISK_CLEANUP_TMPDIR": str(self.systmp)})[1])
        self.assertEqual(self.item(result, "claude-temp")["paths"], [str(self.systmp / "claude" / "old-sess")])

    def test_system_tmp_only_claude_folders_of_this_user(self):
        self.fixture()
        cfg, _ = dc.load_config()
        ctx = dc.Context(cfg)
        item = {"id": "claude-temp", "root": "tmp", "meta": {}}
        self.assertIsNone(dc.check_path(str(self.systmp / "claude-501/old-sess"), item, ctx))
        self.assertEqual(dc.check_path(str(self.systmp / "other/old"), item, ctx), "outside the Claude temp folders")
        self.assertEqual(dc.check_path(str(self.systmp / "claude-501"), item, ctx), "protected folder")
        self.scan()
        code, out, err = self.cli("clean", "claude-temp", "--yes")
        self.assertEqual(code, 0, out + err)
        self.assertFalse((self.systmp / "claude-501/old-sess").exists())
        self.assertTrue((self.systmp / "claude-501/new-sess").exists())
        self.assertTrue((self.systmp / "other/old/x").exists())


class HookTest(Base):
    def hook(self, event="UserPromptSubmit", free=None, env=None, raw=None, **payload):
        data = raw if raw is not None else json.dumps(dict(payload, hook_event_name=event))
        extra = dict(env or {})
        if free is not None:
            extra["DISK_CLEANUP_FAKE_DISK"] = "%d,%d" % (free, self.TOTAL)
        code, out, err = self.cli("hook", stdin=data, env=extra)
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        return json.loads(out) if out.strip() else None

    def context(self, result):
        return result["hookSpecificOutput"]["additionalContext"]

    def test_never_checked_nudges_once_then_throttles(self):
        result = self.hook()
        self.assertEqual(result["hookSpecificOutput"]["hookEventName"], "UserPromptSubmit")
        text = self.context(result)
        self.assertIn("free 200.0 GB of 245.0 GB (82 %)", text)
        self.assertIn("last cleanup never", text)
        self.assertIn("Finish the user's current request first", text)
        self.assertIn("run snooze", text)
        self.assertAlmostEqual(self.read_state()["last_nag"], self.now, delta=60)
        self.assertIsNone(self.hook())
        self.assertIsNone(self.hook("SessionStart", source="startup"))

    def test_interval(self):
        self.write_state(last_scan=self.now - 3 * DAY, last_clean=self.now - 20 * DAY)
        self.assertIsNone(self.hook())
        self.write_state(last_scan=self.now - 8 * DAY, last_clean=self.now - 9 * DAY)
        result = self.hook("SessionStart", source="startup")
        self.assertEqual(result["hookSpecificOutput"]["hookEventName"], "SessionStart")
        self.assertIn("last cleanup 9 d ago", self.context(result))

    def test_low_free_space(self):
        self.write_state(last_scan=self.now)
        self.assertIsNone(self.hook(free=30 * GB))
        self.assertIsNotNone(self.hook(free=20 * GB))
        self.write_state(last_scan=self.now)
        self.write_config(min_free_gb=5, min_free_pct=20, critical_free_gb=1)
        self.assertIsNotNone(self.hook(free=40 * GB))  # 16 % < 20 %

    def test_snooze_and_critical(self):
        code, out, _ = self.cli("snooze")
        self.assertEqual(code, 0)
        self.assertAlmostEqual(self.read_state()["snooze_until"], self.now + 7 * DAY, delta=60)
        self.assertIsNone(self.hook(free=20 * GB))
        result = self.hook(free=5 * GB)
        self.assertIn("critically low", self.context(result))
        self.assertIn("right away", self.context(result))
        state = self.read_state()
        state["last_nag"] = self.now - 3 * 3600
        self.write_state(**state)
        self.assertIsNotNone(self.hook(free=5 * GB))   # critical: 2 h throttle
        state = self.read_state()
        state["last_nag"] = self.now - 3600
        self.write_state(**state)
        self.assertIsNone(self.hook(free=5 * GB))
        state["last_nag"], state["snooze_until"] = self.now - 3 * 3600, None
        self.write_state(**state)
        self.assertIsNone(self.hook(free=20 * GB))      # not critical: 20 h throttle

    def test_silent_cases(self):
        self.assertIsNone(self.hook("SessionStart", source="compact"))
        self.assertIsNone(self.hook(env={"DISK_CLEANUP_PLATFORM": "linux"}))
        self.assertIsNone(self.hook("Stop"))
        self.assertIsNone(self.hook(raw="not json"))
        self.assertIsNone(self.hook(raw=""))
        self.assertFalse((Path(dc.state_dir()) / "state.json").exists())

    def test_corrupt_state_fails_open(self):
        self.mk(Path(dc.state_dir()) / "state.json", b"{broken")
        self.assertIsNone(self.hook(free=5 * GB))
        self.mk(Path(dc.state_dir()) / "config.json", b"[1]")
        (Path(dc.state_dir()) / "state.json").unlink()
        self.assertIsNone(self.hook(free=5 * GB))

    def test_status(self):
        self.write_state(last_scan=self.now - 8 * DAY, snooze_until=self.now + DAY)
        code, out, _ = self.cli("status", env={"DISK_CLEANUP_FAKE_DISK": "%d,%d" % (5 * GB, self.TOTAL)})
        self.assertEqual(code, 0)
        self.assertIn("check due:  yes (CRITICAL)", out)
        self.assertIn("snooze:     until", out)
        self.assertIn("hook:       not installed", out)
        code, out, _ = self.cli("config")
        self.assertEqual(code, 0)
        self.assertIn("config.json", out)
        self.assertEqual(json.loads(out[out.index("{"):])["interval_days"], 7)


class InstallTest(Base):
    def settings(self):
        return Path(dc.settings_path())

    def test_install_idempotent_with_backup_and_uninstall(self):
        other = {"type": "command", "command": "echo other"}
        original = {"model": "x", "hooks": {"SessionStart": [{"hooks": [other]}],
                                            "PreToolUse": [{"matcher": "Bash", "hooks": [other]}]}}
        self.mk(self.settings(), json.dumps(original, indent=2).encode())
        original_bytes = self.settings().read_bytes()
        code, out, _ = self.cli("install-hook")
        self.assertEqual(code, 0)
        self.assertIn("installed", out)
        data = json.loads(self.settings().read_text(encoding="utf-8"))
        for event in ("SessionStart", "UserPromptSubmit"):
            ours = [h for g in data["hooks"][event] for h in g["hooks"] if dc.TAG in h["command"]]
            self.assertEqual(len(ours), 1, event)
            self.assertEqual(ours[0]["command"], dc.HOOK_COMMAND)
            self.assertEqual(ours[0]["timeout"], dc.HOOK_TIMEOUT)
        self.assertIn(other, data["hooks"]["SessionStart"][0]["hooks"])
        self.assertEqual(data["hooks"]["PreToolUse"], original["hooks"]["PreToolUse"])
        self.assertEqual(data["model"], "x")
        backup = Path(str(self.settings()) + ".disk-cleanup.bak")
        self.assertEqual(backup.read_bytes(), original_bytes)
        installed = self.settings().read_bytes()
        code, out, _ = self.cli("install-hook")
        self.assertIn("already installed", out)
        self.assertEqual(self.settings().read_bytes(), installed)
        self.assertIn("installed (SessionStart, UserPromptSubmit)", self.cli("status")[1])
        code, out, _ = self.cli("uninstall-hook")
        self.assertIn("removed 2", out)
        self.assertEqual(json.loads(self.settings().read_text(encoding="utf-8")), original)
        self.assertIn("removed 0", self.cli("uninstall-hook")[1])

    def test_install_without_settings(self):
        code, out, err = self.cli("install-hook")
        self.assertEqual(code, 0, err)
        self.assertTrue(self.settings().exists())
        self.assertFalse(Path(str(self.settings()) + ".disk-cleanup.bak").exists())
        self.assertIn("does not exist yet", err)


if __name__ == "__main__":
    unittest.main(verbosity=2)
