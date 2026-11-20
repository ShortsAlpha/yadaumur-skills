#!/usr/bin/env python3
"""disk-cleanup: finds regenerable disk hogs that dev tools and coding agents leave behind on macOS
(Xcode DerivedData, simulators, package caches, build artifacts of inactive projects, stale agent
worktrees and temp folders), shows them and deletes only what the user approves.

Commands (through disk-cleanup.sh):
  scan [--json] [--include-active]    read-only: measure every catalog target, write last-scan.json,
                                      show growth and the free-space change since the previous scan/clean
  clean <id>... [--yes] [--permanent] dry run by default; --yes deletes (needs a scan younger than 24 h);
                                      tier C items also need --permanent
  status                              free space, last scan/clean, whether a check is due, snooze, trend, hook
  snooze [--days N]                   no reminders for N days (default 7); critical alerts still come
  config                              effective config and its path
  hook                                SessionStart / UserPromptSubmit hook (reads the hook JSON on stdin)
  install-hook / uninstall-hook       add / remove the hook in ~/.claude/settings.json

Tiers: A = cache (comes back by itself), B = rebuildable but costs time or is a judgment call,
C = does not come back or belongs to another app (deleted only with --permanent).
The real gain is measured as the change in free space (statvfs) before/after: du double-counts APFS clones.

macOS only: everything except status/config refuses to run elsewhere; the hook exits 0 silently.
The hook fails open: any error exits 0 without output (logged to <state dir>/error.log).
Python 3.9+, stdlib only. State and config are machine-local in ~/.claude/disk-cleanup/.
Env for tests, honoured only together with DISK_CLEANUP_TEST=1: DISK_CLEANUP_HOME (home folder),
DISK_CLEANUP_PLATFORM (sys.platform), DISK_CLEANUP_NOW (clock, epoch seconds), DISK_CLEANUP_TMPDIR ($TMPDIR),
DISK_CLEANUP_SYSTMP (/private/tmp), DISK_CLEANUP_FAKE_DISK ("free,total" in bytes), DISK_CLEANUP_PROCS
(comma-separated running process names), DISK_CLEANUP_PATH (PATH for external tools).
"""
import argparse
import contextlib
import fnmatch
import glob
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import traceback
import unicodedata
import uuid

sys.dont_write_bytecode = True

GB = 1000 ** 3  # decimal, like Finder and the disk size on the box
MB = 1000 ** 2
DAY = 86400
DEFAULTS = {"interval_days": 7, "min_free_gb": 25, "min_free_pct": 10, "critical_free_gb": 10,
            "nag_hours": 20, "critical_nag_hours": 2, "stale_days": 30,
            "project_roots": None, "exclude": [], "hide": []}
NUMBER_KEYS = ("interval_days", "min_free_gb", "min_free_pct", "critical_free_gb", "nag_hours",
               "critical_nag_hours", "stale_days")
LIST_KEYS = ("project_roots", "exclude", "hide")
DEFAULT_ROOTS = ("Developer", "Projects", "code", "src", "dev", "repos")
SCAN_MAX_AGE = DAY            # clean --yes only trusts a scan younger than this
SCAN_TOTAL = 130              # seconds for a whole scan (discovery + sizing); unfinished sizes are lower bounds
TARGET_TIMEOUT = 30           # seconds to size one target
QUERY_TIMEOUT = 10            # read-only external queries (xcrun simctl list, brew --cache, tmutil, git)
COMMAND_TIMEOUT = 600         # deleting commands (brew cleanup, simctl runtime delete, ...)
WALK_MAX_DEPTH = 6            # project walk below each project root
WALK_MAX_DIRS = 50000
WALK_BUDGET = 20              # seconds for the project walk
ACTIVITY_MAX_ENTRIES = 20000  # per project; a project too big to check is treated as active
CLAUDE_TEMP_DAYS = 7
DOWNLOAD_MIN_BYTES = GB
DOWNLOAD_DAYS = 30
TRANSCRIPT_MIN_AGE = DAY      # never list a transcript touched in the last day (a session may be using it)
GROWTH_MIN_BYTES = 500 * MB
NEW_ITEM_MIN_BYTES = GB
HISTORY_KEEP = 500
LOCK_STALE = 30
HOOK_EVENTS = ("SessionStart", "UserPromptSubmit")
HOOK_COMMAND = "bash ~/.claude/skills/disk-cleanup/scripts/disk-cleanup.sh hook || true"
TAG = "disk-cleanup.sh hook"  # identifies our handlers in settings.json
HOOK_TIMEOUT = 10
TIER_TITLES = {"A": "A  cache: comes back by itself; deleting costs only a re-download or rebuild",
               "B": "B  rebuildable, but costs real time or is a judgment call",
               "C": "C  does not come back (or belongs to another app): deleted only with --permanent"}
XCODE = ("Xcode", "xcodebuild", "Simulator")
# Folders that are never deleted, nor anything that contains them (home-relative).
HOME_PROTECTED = ("", "Library", "Library/Caches", "Library/Developer", "Library/Developer/Xcode",
                  "Library/Developer/Xcode/DerivedData", "Library/Developer/Xcode/UserData",
                  "Library/Developer/CoreSimulator", "Library/Developer/CoreSimulator/Devices",
                  "Library/Application Support", "Library/pnpm", ".claude", ".claude/projects", ".claude/skills",
                  ".cache", ".npm", ".bun", ".bun/install", ".gradle", ".codex", "Downloads", ".Trash",
                  "Desktop", "Documents", "Pictures", "Movies", "Music", "Applications", ".ssh", ".config",
                  ".local")
HOME_PROTECTED_GLOBS = ("Library/Developer/Xcode/*DeviceSupport",)

# Simple targets: one item per row, skipped when none of its paths exist. A row's command (run when its
# tool is installed) covers all its paths unless `covers` names a subset; without the tool, or when the
# command fails, the covered paths are removed directly. Paths are home-relative.
CATALOG = (
    {"id": "xctest-devices", "tier": "A", "title": "XCTest clone devices",
     "paths": ["Library/Developer/XCTestDevices"], "in_use": XCODE,
     "note": "Simulator clones from parallel test runs; recreated on the next parallel test run."},
    {"id": "xcode-previews", "tier": "A", "title": "SwiftUI preview data",
     "paths": ["Library/Developer/Xcode/UserData/Previews"], "in_use": XCODE,
     "note": "Preview simulators and builds; rebuilt the next time a preview opens."},
    {"id": "core-simulator-caches", "tier": "A", "title": "CoreSimulator caches",
     "paths": ["Library/Developer/CoreSimulator/Caches"], "in_use": XCODE,
     "note": "Simulator dyld caches; regenerated on the next simulator boot and regrow quickly."},
    {"id": "swiftpm-cache", "tier": "A", "title": "SwiftPM cache", "paths": ["Library/Caches/org.swift.swiftpm"],
     "in_use": XCODE + ("swift", "swift-build", "swift-package"),
     "note": "Package clones and binary artifacts; re-downloaded on the next package resolve."},
    {"id": "cocoapods-cache", "tier": "A", "title": "CocoaPods cache", "paths": ["Library/Caches/CocoaPods"],
     "in_use": ("pod",), "note": "Downloaded pods; re-downloaded on the next pod install."},
    {"id": "homebrew-cache", "tier": "A", "title": "Homebrew cache", "paths": ["Library/Caches/Homebrew"],
     "path_command": ["brew", "--cache"], "tool": "brew", "command": ["brew", "cleanup", "--prune=all"],
     "in_use": ("brew", "brew.sh"),
     "note": "Downloaded bottles and sources (brew cleanup also drops old formula versions); "
             "re-downloaded only when needed again."},
    {"id": "npm-cache", "tier": "A", "title": "npm cache", "paths": [".npm/_cacache", ".npm/_npx"],
     "tool": "npm", "command": ["npm", "cache", "clean", "--force"], "covers": [".npm/_cacache"],
     "in_use": ("npm", "npx"),
     "note": "Package cache and npx installs; re-downloads on the next npm install / npx run."},
    {"id": "pnpm-store", "tier": "A", "title": "pnpm store", "paths": ["Library/pnpm/store"],
     "tool": "pnpm", "command": ["pnpm", "store", "prune"], "in_use": ("pnpm",),
     "note": "pnpm store; prune frees only packages no project references (without pnpm the folder is "
             "removed and refilled on the next install)."},
    {"id": "yarn-cache", "tier": "A", "title": "Yarn cache", "paths": ["Library/Caches/Yarn"], "in_use": ("yarn",),
     "note": "Re-downloads on the next yarn install."},
    {"id": "bun-cache", "tier": "A", "title": "Bun cache", "paths": [".bun/install/cache"], "in_use": ("bun",),
     "note": "Re-downloads on the next bun install."},
    {"id": "pip-cache", "tier": "A", "title": "pip cache", "paths": ["Library/Caches/pip"], "in_use": ("pip", "pip3"),
     "note": "Downloaded wheels; re-downloaded on the next pip install."},
    {"id": "uv-cache", "tier": "A", "title": "uv cache", "paths": [".cache/uv"], "tool": "uv",
     "command": ["uv", "cache", "clean"], "in_use": ("uv",), "note": "Re-downloads on the next uv sync / install."},
    {"id": "go-build-cache", "tier": "A", "title": "Go build cache", "paths": ["Library/Caches/go-build"],
     "tool": "go", "command": ["go", "clean", "-cache"], "in_use": ("go",),
     "note": "Compiled packages; the next go build recompiles them."},
    {"id": "gradle-cache", "tier": "A", "title": "Gradle caches", "paths": [".gradle/caches"],
     "in_use": ("gradle", "gradlew"), "note": "Dependencies and build cache; re-downloaded on the next Gradle build."},
    {"id": "node-gyp-cache", "tier": "A", "title": "node-gyp headers", "paths": ["Library/Caches/node-gyp"],
     "in_use": ("node-gyp",), "note": "Node headers for native modules; re-downloaded when one is built."},
    {"id": "electron-cache", "tier": "A", "title": "Electron downloads", "paths": ["Library/Caches/electron"],
     "note": "Electron binaries; re-downloaded on the next install that needs them."},
    {"id": "playwright-browsers", "tier": "B", "title": "Playwright browsers",
     "paths": ["Library/Caches/ms-playwright"], "in_use": ("playwright",),
     "note": "Browsers for Playwright; npx playwright install fetches them again (hundreds of MB each)."},
    {"id": "puppeteer-cache", "tier": "B", "title": "Puppeteer browsers", "paths": [".cache/puppeteer"],
     "note": "Chrome builds for Puppeteer; re-downloaded on the next install."},
    {"id": "huggingface-cache", "tier": "B", "title": "Hugging Face cache", "paths": [".cache/huggingface"],
     "note": "Downloaded models and datasets; re-downloaded when used again (can be many GB)."},
    {"id": "xcode-archives", "tier": "C", "title": "Xcode archives", "paths": ["Library/Developer/Xcode/Archives"],
     "in_use": XCODE, "note": "Archived builds with their dSYMs, needed to symbolicate crashes of shipped builds; "
                              "gone for good once deleted."},
    {"id": "codex-sessions", "tier": "C", "title": "Codex session logs", "paths": [".codex/sessions"],
     "in_use": ("codex",), "note": "Codex CLI session transcripts; gone for good once deleted."},
    {"id": "claude-vm-bundles", "tier": "C", "title": "Claude desktop VM",
     "paths": ["Library/Application Support/Claude/vm_bundles"], "deletable": False,
     "note": "The Claude desktop app's VM image; manage it from the app (not deleted here)."},
)
# Artifact folder name -> marker files its parent folder must hold. Inside a git repo the folder must also be
# ignored by git and hold no tracked file; outside git, `build` needs the stricter BUILD_MARKERS_NO_GIT (an
# npm project's build/ often holds real sources such as icons, entitlements or build scripts).
PROJECT_ARTIFACTS = {
    "node_modules": ("package.json",), ".next": ("package.json",), ".nuxt": ("package.json",),
    ".svelte-kit": ("package.json",), ".turbo": ("package.json",), ".parcel-cache": ("package.json",),
    ".expo": ("package.json",), ".build": ("Package.swift",), "Pods": ("Podfile",),
    "build": ("*.xcodeproj", "*.xcworkspace", "package.json", "build.gradle", "build.gradle.kts"),
    "target": ("Cargo.toml",), ".venv": ("pyproject.toml", "requirements.txt", "setup.py"),
    "venv": ("pyproject.toml", "requirements.txt", "setup.py"),
}
BUILD_MARKERS_NO_GIT = ("*.xcodeproj", "*.xcworkspace", "build.gradle", "build.gradle.kts")
# Git-ignored names a worktree may hold and still be removed (generated, comes back by itself).
WORKTREE_IGNORED_OK = set(PROJECT_ARTIFACTS) | {"__pycache__", ".DS_Store", ".pytest_cache", ".mypy_cache",
                                                ".ruff_cache"}
BUNDLE_EXTENSIONS = (".app", ".photoslibrary", ".fcpbundle", ".logicx", ".imovielibrary", ".musiclibrary",
                     ".tvlibrary", ".bundle", ".pkg", ".band", ".xcarchive", ".framework", ".plugin")
SCRIPT_EXTENSIONS = (".js", ".cjs", ".mjs", ".py", ".sh", ".rb")


class Refused(Exception):
    """A user-facing refusal: one line, exit 1."""


class LockBusy(Exception):
    pass


# ---------------------------------------------------------------- basics

def test_env(name):
    """A test-only override: ignored unless DISK_CLEANUP_TEST=1 is set too."""
    if os.environ.get("DISK_CLEANUP_TEST") != "1":
        return None
    return os.environ.get(name)


def now():
    value = test_env("DISK_CLEANUP_NOW")
    if value:
        try:
            return float(value)
        except ValueError:
            pass
    return time.time()


def platform():
    return test_env("DISK_CLEANUP_PLATFORM") or sys.platform


def is_mac():
    return platform() == "darwin"


def home():
    custom = test_env("DISK_CLEANUP_HOME")
    return os.path.abspath(os.path.expanduser(custom) if custom else str(Path.home()))


def tmpdir():
    custom = test_env("DISK_CLEANUP_TMPDIR") or os.environ.get("TMPDIR")
    return os.path.abspath(custom or tempfile.gettempdir())


def systmp():
    """/private/tmp on macOS (Claude Code also keeps claude* folders there), else None."""
    custom = test_env("DISK_CLEANUP_SYSTMP")
    if custom:
        return os.path.abspath(custom)
    return "/private/tmp" if sys.platform == "darwin" and os.path.isdir("/private/tmp") else None


def owned_by_me(path):
    getuid = getattr(os, "getuid", None)
    if getuid is None:
        return True  # Windows (tests): no uid to compare
    try:
        return os.lstat(path).st_uid == getuid()
    except OSError:
        return False


def state_dir():
    return os.path.join(home(), ".claude", "disk-cleanup")


def state_file(name):
    return os.path.join(state_dir(), name)


def hpath(rel):
    return os.path.join(home(), *rel.split("/")) if rel else home()


def expand(value):
    """A config path: `~` and relative paths are taken from the home folder, not the current folder."""
    if value == "~":
        return home()
    if value.startswith("~/"):
        value = value[2:]
    elif value.startswith("~"):
        value = os.path.expanduser(value)
    return os.path.abspath(value if os.path.isabs(value) else os.path.join(home(), value))


def which(name):
    custom = test_env("DISK_CLEANUP_PATH")
    if custom is not None:
        return shutil.which(name, path=custom) if custom else None
    return shutil.which(name)


_CLT = []


def command_line_tools_ok():
    """On macOS, /usr/bin/git, xcrun and python3 are stubs that pop an install dialog until the Command Line
    Tools are installed; `xcode-select -p` tells silently whether they are."""
    if sys.platform != "darwin":
        return True
    if not _CLT:
        code, _, _ = run_cmd(["/usr/bin/xcode-select", "-p"], QUERY_TIMEOUT)
        _CLT.append(code == 0)
    return _CLT[0]


def tool(name):
    """Path of an external tool, or None; never a /usr/bin stub of the missing Command Line Tools."""
    path = which(name)
    if path and path.rsplit("/", 1)[0] == "/usr/bin" and name in ("git", "xcrun") \
            and not command_line_tools_ok():
        return None
    return path


def run_cmd(argv, timeout):
    """(returncode, stdout, stderr); returncode None when the command is missing or timed out."""
    try:
        done = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, "", "timed out after %d s" % timeout
    except OSError as exc:
        return None, "", str(exc)
    return (done.returncode, done.stdout.decode("utf-8", "replace"), done.stderr.decode("utf-8", "replace"))


def disk_usage():
    """(free, total) bytes of the volume holding the home folder (statvfs f_bavail on macOS)."""
    fake = test_env("DISK_CLEANUP_FAKE_DISK")
    if fake:
        free, total = (int(float(v)) for v in fake.split(","))
        return free, total
    usage = shutil.disk_usage(home())
    return usage.free, usage.total


def atomic_write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temporary = "%s.%s.tmp" % (path, uuid.uuid4().hex)
    try:
        with open(temporary, "w", encoding="utf-8", newline="\n") as out:
            out.write(text)
            out.flush()
            os.fsync(out.fileno())
        for attempt in range(5):
            try:
                os.replace(temporary, path)
                break
            except PermissionError:  # Windows sharing violation (antivirus, indexer)
                if attempt == 4:
                    raise
                time.sleep(0.05)
    finally:
        if os.path.exists(temporary):
            try:
                os.unlink(temporary)
            except OSError:
                pass


def read_json(path):
    with open(path, encoding="utf-8-sig") as handle:
        return json.load(handle)


def log_error(text):
    try:
        path = state_file("error.log")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if os.path.exists(path) and os.path.getsize(path) > 256 * 1024:
            os.replace(path, path + ".old")
        with open(path, "a", encoding="utf-8") as out:
            out.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), text.rstrip()))
    except Exception:
        pass


def fmt_size(value, lower_bound=False):
    if value is None:
        return "?"
    prefix = ">=" if lower_bound else ""
    if value >= GB:
        return "%s%.1f GB" % (prefix, value / GB)
    if value >= MB:
        return "%s%d MB" % (prefix, round(value / MB))
    return "%s<1 MB" % prefix if value else "%s0 MB" % prefix


def fmt_signed(value):
    return ("+" if value >= 0 else "-") + fmt_size(abs(value))


def fmt_age(seconds):
    if seconds is None:
        return "-"
    if seconds < 2 * 3600:
        return "%d min" % max(0, seconds // 60)
    if seconds < 2 * DAY:
        return "%d h" % (seconds // 3600)
    return "%d d" % (seconds // DAY)


def fmt_time(epoch):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(epoch))


def free_line(free, total):
    pct = 100.0 * free / total if total else 0
    return "free %s of %s (%d %%)" % (fmt_size(free), fmt_size(total), round(pct))


# ---------------------------------------------------------------- config, state, history

def config_path():
    return state_file("config.json")


def load_config():
    """(config, warnings). A missing file means defaults; invalid values are ignored with a warning."""
    data, warnings = {}, []
    path = config_path()
    if os.path.exists(path):
        data = read_json(path)
        if not isinstance(data, dict):
            raise ValueError("%s must hold a JSON object" % path)
    cfg = {k: (list(v) if isinstance(v, list) else v) for k, v in DEFAULTS.items()}
    for key, value in data.items():
        if key in NUMBER_KEYS:
            if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
                cfg[key] = value
            else:
                warnings.append("%s: expected a number >= 0, using %s" % (key, DEFAULTS[key]))
        elif key in LIST_KEYS:
            if isinstance(value, list) and all(isinstance(v, str) for v in value):
                cfg[key] = list(value)
            else:
                warnings.append("%s: expected a list of strings, ignored" % key)
        else:
            warnings.append("unknown key %s, ignored" % key)
    if cfg["project_roots"] is None:
        cfg["project_roots"] = ["~/" + name for name in DEFAULT_ROOTS if os.path.isdir(hpath(name))]
    return cfg, warnings


def load_state(strict):
    """The state ({} when absent). Corrupt: raise when strict (the hook fails open), else move it aside."""
    path = state_file("state.json")
    if not os.path.exists(path):
        return {}
    try:
        data = read_json(path)
        if not isinstance(data, dict):
            raise ValueError("state.json is not a JSON object")
        return data
    except (ValueError, UnicodeDecodeError) as exc:
        if strict:
            raise
        target = path + ".corrupt"
        os.replace(path, target)
        print("disk-cleanup: state.json was unreadable (%s); moved to %s, starting fresh" % (exc, target),
              file=sys.stderr)
        return {}


def save_state(state):
    atomic_write(state_file("state.json"), json.dumps(state, indent=1, sort_keys=True) + "\n")


@contextlib.contextmanager
def state_lock(wait):
    """A lock file so parallel sessions don't race on state.json (and don't all nudge at once)."""
    path = state_file("state.lock")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    deadline = time.monotonic() + wait
    while True:
        try:
            handle = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(path) > LOCK_STALE:
                    os.unlink(path)
                    continue
            except OSError:
                pass
            if time.monotonic() >= deadline:
                raise LockBusy("state.lock is held by another process")
            time.sleep(0.05)
    try:
        yield
    finally:
        os.close(handle)
        try:
            os.unlink(path)
        except OSError:
            pass


def update_state(**changes):
    with state_lock(wait=5):
        state = load_state(strict=False)
        state.update(changes)
        save_state(state)
        return state


def append_history(record):
    path = state_file("history.jsonl")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8", newline="\n") as out:
        out.write(json.dumps(record, sort_keys=True) + "\n")
    if os.path.getsize(path) > 4 * 1024 * 1024:
        lines = open(path, encoding="utf-8").read().splitlines()[-HISTORY_KEEP:]
        atomic_write(path, "\n".join(lines) + "\n")


def read_history():
    path = state_file("history.jsonl")
    records = []
    if not os.path.exists(path):
        return records
    with open(path, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict) and record.get("type") in ("scan", "clean"):
                records.append(record)
    return records


def record_free(record):
    return record.get("free_after") if record.get("type") == "clean" else record.get("free")


# ---------------------------------------------------------------- paths and safety

def norm(path):
    """A path for comparisons only. macOS (APFS is case-insensitive and stores names in either Unicode form):
    NFD + casefold; elsewhere os.path.normcase."""
    path = os.path.normpath(path)
    if platform() == "darwin":
        return unicodedata.normalize("NFD", unicodedata.normalize("NFD", path).casefold())
    return os.path.normcase(path)


def real(path):
    return norm(os.path.realpath(path))


def real_loc(path):
    """Where `path` is, with its parents resolved but not its last component (a link stays a link)."""
    path = os.path.normpath(os.path.abspath(path))
    return norm(os.path.join(os.path.realpath(os.path.dirname(path)), os.path.basename(path)))


def under(child, parent):
    """child is strictly inside parent (both normalized)."""
    if child == parent:
        return False
    try:
        return os.path.commonpath([child, parent]) == parent
    except ValueError:
        return False


def rel_home(path):
    try:
        rel = os.path.relpath(path, home())
    except ValueError:
        return path
    return path if rel.startswith("..") else rel.replace(os.sep, "/")


def is_link(path):
    """A symlink, or a directory junction on Windows (which Python reports as a plain directory)."""
    if os.path.islink(path):
        return True
    if os.name != "nt":
        return False
    isjunction = getattr(os.path, "isjunction", None)
    if isjunction is not None:
        return isjunction(path)
    try:
        return bool(getattr(os.lstat(path), "st_reparse_tag", 0))
    except OSError:
        return False


def entry_is_link(entry):
    return entry.is_symlink() or (os.name == "nt" and is_link(entry.path))


def entry_is_dir(entry):
    """A real directory (not a link to one)."""
    return entry.is_dir(follow_symlinks=False) and not entry_is_link(entry)


def recent_mtime(path, depth):
    """Latest mtime of `path` and its entries down to `depth` levels (links are not followed)."""
    try:
        st = os.lstat(path)
    except OSError:
        return None
    best = st.st_mtime
    if depth > 0 and stat.S_ISDIR(st.st_mode) and not is_link(path):
        try:
            with os.scandir(path) as entries:
                for count, entry in enumerate(entries):
                    if count >= 2000:
                        break
                    value = recent_mtime(entry.path, depth - 1)
                    if value is not None and value > best:
                        best = value
        except OSError:
            pass
    return best


def is_dir(path):
    return os.path.isdir(path) and not is_link(path)


class Context:
    def __init__(self, cfg, include_active=False):
        self.cfg = cfg
        self.now = now()
        self.home = home()
        self.home_real = real(self.home)
        self.tmp = tmpdir()
        self.systmp = systmp()
        self.deadline = time.monotonic() + SCAN_TOTAL
        self.notes = []
        # Claude Code temp roots: $TMPDIR/claude and, on macOS, /private/tmp/claude* folders of this user.
        containers = [os.path.join(self.tmp, "claude")]
        if self.systmp:
            containers += sorted(p for p in glob.glob(os.path.join(self.systmp, "claude*"))
                                 if is_dir(p) and owned_by_me(p))
        self.tmp_containers, seen = [], set()
        for container in containers:  # /tmp/claude and /private/tmp/claude are the same folder
            if real(container) not in seen:
                seen.add(real(container))
                self.tmp_containers.append(container)
        self.tmp_roots = [real(p) for p in self.tmp_containers]
        self.include_active = include_active
        self.stale = cfg["stale_days"] * DAY
        self.roots = [expand(r) for r in cfg["project_roots"]]
        self.excludes = [real(expand(e)) for e in cfg["exclude"]]
        self.hide = set(cfg["hide"])
        protected = [hpath(rel) for rel in HOME_PROTECTED]
        for pattern in HOME_PROTECTED_GLOBS:
            protected += glob.glob(hpath(pattern))
        protected += self.roots + [self.tmp] + self.tmp_containers + ([self.systmp] if self.systmp else [])
        self.protected = [real(p) for p in protected]
        self.roots_real = [real(r) for r in self.roots]

    def time_left(self):
        return self.deadline - time.monotonic()

    def excluded(self, path, containing=True):
        """`path` is excluded, inside an excluded path or (with `containing`) contains one."""
        if not self.excludes:
            return False
        loc = real_loc(path)
        return any(loc == ex or under(loc, ex) or (containing and under(ex, loc)) for ex in self.excludes)


def check_path(path, item, ctx):
    """None when `path` may be deleted for `item`, else the reason it may not."""
    if not os.path.lexists(path):
        return "gone"
    loc = real_loc(path)
    project = (item.get("meta") or {}).get("project_dir")
    protected = ctx.protected + ([real(project)] if project else [])
    if any(loc == p or under(p, loc) for p in protected):
        return "protected folder"
    if item.get("root") == "tmp":
        if not any(under(loc, root) for root in ctx.tmp_roots):
            return "outside the Claude temp folders"
        if ctx.systmp and under(loc, real(ctx.systmp)) and not owned_by_me(path):
            return "not owned by this user"
    elif not under(loc, ctx.home_real):
        return "outside the home folder"
    if ctx.excluded(path):
        return "excluded in config"
    if project:
        if not under(loc, real(project)):
            return "not inside its project"
        if not any(under(real(project), root) for root in ctx.roots_real):
            return "project is not inside a project root"
    return None


def describe_error(exc):
    if isinstance(exc, tuple):  # rmtree's onerror passes sys.exc_info()
        exc = exc[1]
    if isinstance(exc, OSError):
        return exc.strerror or str(exc)
    return "%s: %s" % (type(exc).__name__, exc)


def chmod_no_follow(path, mode):
    if os.chmod in os.supports_follow_symlinks:
        os.chmod(path, mode, follow_symlinks=False)
    elif not is_link(path):
        os.chmod(path, mode)


def rmtree_retry(root, errors):
    """rmtree error handler: retries a failed unlink/rmdir once after making its parent folder (inside the tree
    being deleted, never a link) writable, and on Windows clearing the read-only flag of the entry itself.
    Never follows links. Anything else (an unreadable folder, a second failure) is recorded in `errors`."""
    root_norm = norm(root)

    def retry(func, target, info):
        if func not in (os.unlink, os.rmdir, os.remove):
            errors.append("%s: %s" % (target, describe_error(info)))
            return
        try:
            parent = os.path.dirname(target)
            if parent and (norm(parent) == root_norm or under(norm(parent), root_norm)) and not is_link(parent):
                chmod_no_follow(parent, stat.S_IMODE(os.lstat(parent).st_mode) | stat.S_IRWXU)
            if os.name == "nt" and not is_link(target):
                os.chmod(target, stat.S_IWRITE)  # Windows: the read-only flag sits on the entry itself
            func(target)
        except Exception as exc:
            errors.append("%s: %s" % (target, describe_error(exc)))
    return retry


def remove_path(path):
    """Delete one path; a link is removed as a link. Returns a list of error strings (never raises)."""
    errors = []
    try:
        if is_link(path) or not os.path.isdir(path):
            try:
                os.unlink(path)
            except (IsADirectoryError, PermissionError):
                if not (os.name == "nt" and is_link(path)):
                    raise
                os.rmdir(path)  # a directory link on Windows: removes the link, not its target
            return errors
        retry = rmtree_retry(path, errors)
        if sys.version_info >= (3, 12):
            shutil.rmtree(path, onexc=retry)
        else:
            shutil.rmtree(path, onerror=retry)
    except Exception as exc:
        errors.append("%s: %s" % (path, describe_error(exc)))
    return errors


# ---------------------------------------------------------------- in-use checks

class Activity:
    """Running processes and booted simulators, read once per command."""

    def __init__(self):
        self._names = False
        self._booted = None

    def names(self):
        if self._names is False:
            self._names = running_names()
        return self._names

    def booted(self):
        if self._booted is None:
            self._booted = booted_simulators()
        return self._booted

    def reason(self, item):
        names = item.get("in_use") or ()
        if not names:
            return None
        running = self.names()
        if running is None:
            return "could not list running processes"
        for name in names:
            if name in running:
                return "%s is running" % name
        if any(name in XCODE for name in names) and self.booted():
            return "a simulator is booted"
        return None


def names_from_args(lines):
    """Tool names found anywhere in process command lines, on word and path boundaries: every path part,
    without a script extension (pnpm.cjs -> pnpm, brew.sh -> brew), and the last part of a dotted name
    (org.gradle...GradleDaemon -> GradleDaemon). `python -m pip` gives pip."""
    names = set()
    for line in lines:
        for word in line.split():
            for part in word.split("/"):
                if not part:
                    continue
                names.add(part)
                base, ext = os.path.splitext(part)
                if ext in SCRIPT_EXTENSIONS and base:
                    names.add(base)
                if "." in part:
                    names.add(part.rsplit(".", 1)[-1])
    return names


def running_names():
    custom = test_env("DISK_CLEANUP_PROCS")
    if custom is not None:
        return {name.strip() for name in custom.split(",") if name.strip()}
    ps = "/bin/ps" if os.path.exists("/bin/ps") else which("ps")
    if not ps:
        return None
    code, out, _ = run_cmd([ps, "-axo", "pid=,args="], QUERY_TIMEOUT)
    if code != 0:
        return None
    mine = {str(os.getpid()), str(os.getppid())}
    lines = []
    for line in out.splitlines():
        pid, _, args = line.strip().partition(" ")
        if pid not in mine:
            lines.append(args)
    return names_from_args(lines)


def booted_simulators():
    if test_env("DISK_CLEANUP_PROCS") is not None or not tool("xcrun"):
        return 0
    data = simctl_json(["list", "-j", "devices", "booted"])
    devices = (data or {}).get("devices") or {}
    return sum(1 for group in devices.values() if isinstance(group, list)
               for device in group if isinstance(device, dict) and device.get("state") == "Booted")


def simctl_json(args):
    code, out, _ = run_cmd([tool("xcrun") or "xcrun", "simctl"] + args, QUERY_TIMEOUT)
    if code != 0:
        return None
    try:
        return json.loads(out)
    except ValueError:
        return None


# ---------------------------------------------------------------- catalog discovery

def make_item(item_id, tier, title, note, paths=(), **extra):
    item = {"id": item_id, "tier": tier, "title": title, "note": note, "paths": list(paths),
            "commands": [], "remove": list(paths), "fallback": [], "in_use": [], "deletable": True,
            "root": "home", "meta": {}, "size_bytes": None, "size_complete": True, "access": "ok", "mtime": None}
    item.update(extra)
    item["in_use"] = list(item["in_use"])
    return item


def wants(only, *keys):
    """Should a finder producing ids `keys` (exact ids, or prefixes ending in ':') run?"""
    if only is None:
        return True
    return any(i == k or (k.endswith(":") and i.startswith(k)) for i in only for k in keys)


def find_catalog(ctx, only):
    items = []
    for row in CATALOG:
        if not wants(only, row["id"]):
            continue
        rels = list(row["paths"])
        paths = [hpath(rel) for rel in rels]
        tool_name = row.get("tool")
        if row.get("path_command") and tool(tool_name):
            code, out, _ = run_cmd([tool(tool_name)] + row["path_command"][1:], QUERY_TIMEOUT)
            custom = out.strip().splitlines()[0] if code == 0 and out.strip() else ""
            if custom and under(real(custom), ctx.home_real):
                paths = [custom]
                rels = [None]
        present = [p for p in paths if os.path.lexists(p)]
        if not present:
            continue
        extra = {"in_use": row.get("in_use", ()), "deletable": row.get("deletable", True)}
        if not extra["deletable"]:
            extra.update(remove=[])
        elif tool_name and tool(tool_name):
            covered = row.get("covers")
            cover_paths = [p for p, rel in zip(paths, rels) if covered is None or rel in covered]
            extra.update(commands=[row["command"]], fallback=[p for p in present if p in cover_paths],
                         remove=[p for p in present if p not in cover_paths])
        items.append(make_item(row["id"], row["tier"], row["title"], row["note"], present, **extra))
    return items


def workspace_path(folder):
    try:
        with open(os.path.join(folder, "info.plist"), "rb") as handle:
            value = plistlib.load(handle).get("WorkspacePath")
        return value if isinstance(value, str) and value else None
    except Exception:
        return None


def workspace_gone(path):
    """Only a path that certainly does not exist is gone: a privacy-protected folder (~/Desktop, ~/Documents
    without access) raises PermissionError, which does not mean the project was deleted."""
    if path.startswith("/Volumes/"):
        return False  # an unmounted external disk
    try:
        os.stat(path)
        return False
    except (FileNotFoundError, NotADirectoryError):
        return True
    except OSError:
        return False


def find_derived_data(ctx, only):
    if not wants(only, "derived-data-orphans", "derived-data-stale", "derived-data-active"):
        return []
    root = hpath("Library/Developer/Xcode/DerivedData")
    groups = {"orphans": [], "stale": [], "active": []}
    try:
        entries = sorted(os.scandir(root), key=lambda e: e.name)
    except OSError:
        return []
    for entry in entries:
        link = entry_is_link(entry)
        if not (link or entry.is_dir(follow_symlinks=False)):
            continue
        workspace = workspace_path(entry.path) if not link else None
        age = ctx.now - (recent_mtime(entry.path, 2) or ctx.now)
        if workspace and workspace_gone(workspace):
            groups["orphans"].append(entry.path)
        elif age >= ctx.stale:
            groups["stale"].append(entry.path)
        else:
            groups["active"].append(entry.path)
    days = ctx.cfg["stale_days"]
    specs = (("orphans", "A", "DerivedData of deleted projects",
              "Build data of projects that no longer exist on disk; nothing will rebuild it."),
             ("stale", "A", "DerivedData not built for %d+ days" % days,
              "Build data of projects not built lately; such a project's next build starts from scratch."),
             ("active", "B", "DerivedData of recent projects",
              "Build data of recently built projects; the next build is a full rebuild and indexing restarts."))
    return [make_item("derived-data-" + key, tier, title, note, groups[key], in_use=XCODE)
            for key, tier, title, note in specs if groups[key]]


def find_device_support(ctx, only):
    if not wants(only, "ios-device-support"):
        return []
    paths = []
    for folder in sorted(glob.glob(hpath("Library/Developer/Xcode/*DeviceSupport"))):
        try:
            entries = sorted(os.scandir(folder), key=lambda e: e.name)
        except OSError:
            continue
        for entry in entries:
            if entry_is_dir(entry) and ctx.now - (recent_mtime(entry.path, 1) or ctx.now) >= ctx.stale:
                paths.append(entry.path)
    if not paths:
        return []
    return [make_item("ios-device-support", "A", "Device support files (old OS versions)",
                      "Debug symbols copied from devices; Xcode copies them again when such a device is connected.",
                      paths, in_use=XCODE)]


def note_missing_clt(ctx, what):
    message = "Xcode Command Line Tools not installed (xcode-select -p failed): %s skipped" % what
    if message not in ctx.notes:
        ctx.notes.append(message)


def find_simulators(ctx, only):
    if not wants(only, "simulators-unavailable", "sim-runtime:") or not which("xcrun"):
        return []
    if not tool("xcrun"):
        note_missing_clt(ctx, "simulator items")
        return []
    items = []
    data = simctl_json(["list", "-j", "devices"])
    devices = (data or {}).get("devices") or {}
    if not isinstance(devices, dict):
        devices = {}
    if wants(only, "simulators-unavailable"):
        dead = [d for group in devices.values() if isinstance(group, list) for d in group
                if isinstance(d, dict) and d.get("isAvailable") is False and isinstance(d.get("udid"), str)]
        if dead:
            paths = [p for p in (hpath("Library/Developer/CoreSimulator/Devices/" + d["udid"]) for d in dead)
                     if is_dir(p)]
            items.append(make_item(
                "simulators-unavailable", "A", "Unavailable simulators (%d)" % len(dead),
                "Simulators whose runtime is gone; removed with xcrun simctl delete unavailable.", paths,
                commands=[["xcrun", "simctl", "delete", "unavailable"]], remove=[], in_use=XCODE,
                size_bytes=None if paths else 0))
    if wants(only, "sim-runtime:"):
        runtimes = simctl_json(["runtime", "list", "-j"])
        if isinstance(runtimes, dict):
            seen = set()
            for key, runtime in sorted(runtimes.items()):
                if not isinstance(runtime, dict):
                    continue
                ident = str(runtime.get("identifier") or key)
                rid = str(runtime.get("runtimeIdentifier") or "")
                name = rid.rsplit(".", 1)[-1] or ident
                version = runtime.get("version")
                label = "%s %s" % (name.split("-")[0], version) if version else name
                item_id = "sim-runtime:" + label.replace(" ", "-")
                if item_id in seen:
                    item_id += "-" + str(runtime.get("build") or ident[:8])
                seen.add(item_id)
                used = len(devices.get(rid) or [])
                deletable = runtime.get("deletable", True) is not False
                tier = "B" if used == 0 else "C"
                note = ("Simulator runtime no simulator uses; reinstalling it from Xcode > Settings > Components "
                        "is a multi-GB download." if used == 0 else
                        "Simulator runtime still used by %d simulator(s); they become unavailable, and reinstalling "
                        "is a multi-GB download." % used)
                if not deletable:
                    note += " simctl marks it as not deletable."
                size = runtime.get("sizeBytes")
                items.append(make_item(item_id, tier, "Simulator runtime %s" % label, note,
                                       commands=[["xcrun", "simctl", "runtime", "delete", ident]] if deletable else [],
                                       deletable=deletable, in_use=XCODE, meta={"runtime": ident},
                                       size_bytes=size if isinstance(size, int) else None))
    return items


def find_claude_temp(ctx, only):
    """Children of $TMPDIR/claude and of this user's /private/tmp/claude* folders, idle for CLAUDE_TEMP_DAYS."""
    if not wants(only, "claude-temp"):
        return []
    paths = []
    for container in ctx.tmp_containers:
        try:
            entries = sorted(os.scandir(container), key=lambda e: e.name)
        except OSError:
            continue
        system = ctx.systmp and under(real(container), real(ctx.systmp))
        paths += [e.path for e in entries
                  if ctx.now - (recent_mtime(e.path, 3) or ctx.now) >= CLAUDE_TEMP_DAYS * DAY
                  and (not system or owned_by_me(e.path))]
    if not paths:
        return []
    return [make_item("claude-temp", "A", "Old Claude Code temp folders",
                      "Temp and scratch folders of agent sessions idle for %d+ days." % CLAUDE_TEMP_DAYS,
                      paths, root="tmp")]


def marker_ok(names, patterns):
    return any(fnmatch.fnmatchcase(name, pattern) for name in names for pattern in patterns)


def artifact_markers(name, repo):
    if name == "build" and repo is None:
        return BUILD_MARKERS_NO_GIT
    return PROJECT_ARTIFACTS[name]


def git_root_above(path, ctx):
    """The nearest folder at or above `path`, but below the home folder, holding a .git entry, or None.
    A repo at the home folder itself is a dotfiles repo, not a project."""
    current = os.path.abspath(path)
    while under(real(current), ctx.home_real):
        if os.path.lexists(os.path.join(current, ".git")):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    return None


def walk_projects(ctx):
    """([(artifact, project_dir, repo or None, repo_rules)], [git repos]) under the project roots. The project of an
    artifact is the folder that holds it (and its marker); a project_roots entry itself is never a project."""
    found, repos = [], []
    deadline = min(time.monotonic() + WALK_BUDGET, ctx.deadline)
    visited = [0]
    roots = {norm(r) for r in ctx.roots}

    def walk(folder, depth, repo):
        visited[0] += 1
        if visited[0] > WALK_MAX_DIRS or time.monotonic() > deadline:
            return
        try:
            with os.scandir(folder) as it:
                entries = list(it)
        except OSError:
            return
        names = [e.name for e in entries]
        git = next((e for e in entries if e.name == ".git"), None)
        if git is not None:
            repo = folder
            if git.is_dir(follow_symlinks=False):
                repos.append(folder)
        is_root = norm(folder) in roots
        for entry in entries:
            if not entry_is_dir(entry):
                continue
            if entry.name in PROJECT_ARTIFACTS:
                if not is_root and marker_ok(names, artifact_markers(entry.name, repo)) \
                        and not ctx.excluded(entry.path):
                    # an npm project's build/ may hold real sources: only the repo's own ignore rules count
                    repo_rules = entry.name == "build" and not marker_ok(names, BUILD_MARKERS_NO_GIT)
                    found.append((entry.path, folder, repo, repo_rules))
                continue  # never descend into artifact folders
            if entry.name.startswith(".") or depth >= WALK_MAX_DEPTH or ctx.excluded(entry.path, containing=False):
                continue
            walk(entry.path, depth + 1, repo)

    for root in ctx.roots:
        if is_dir(root):
            walk(root, 0, git_root_above(root, ctx))
    return found, repos


def ignore_rule_is_the_repos(source, repo):
    """The rule comes from a .gitignore inside the repo or from .git/info/exclude, not a global excludes file."""
    source = source.strip().strip('"')
    full = os.path.normpath(source if os.path.isabs(source) else os.path.join(repo, source))
    if os.path.basename(full) == ".gitignore" and under(real(full), real(repo)):
        return True
    return any(real(full) == real(os.path.join(gitdir, "info", "exclude")) for gitdir in git_dirs(repo))


def git_artifact_ok(git, repo, path, repo_rules=False):
    """Inside a git repo an artifact folder must be ignored by git and hold no tracked file. With `repo_rules`,
    the ignore rule must come from the repo itself (`git check-ignore -v` names its source)."""
    code, out, _ = run_cmd([git, "-C", repo, "check-ignore", "-v", "--", path], QUERY_TIMEOUT)
    if code != 0:
        return False
    if repo_rules:
        line = out.splitlines()[0] if out.strip() else ""
        match = re.match(r"^(.*?):(\d+):", line.split("\t", 1)[0])
        if not match or not ignore_rule_is_the_repos(match.group(1), repo):
            return False
    code, out, _ = run_cmd([git, "-C", repo, "ls-files", "--", path], QUERY_TIMEOUT)
    return code == 0 and not out.strip()


def git_dirs(repo):
    """The repo's git dir and, for a linked worktree or submodule, its common dir."""
    dot = os.path.join(repo, ".git")
    if is_dir(dot):
        return [dot]
    try:
        with open(dot, encoding="utf-8") as handle:
            line = handle.readline().strip()
    except OSError:
        return []
    if not line.startswith("gitdir:"):
        return []
    gitdir = line[len("gitdir:"):].strip()
    gitdir = os.path.normpath(gitdir if os.path.isabs(gitdir) else os.path.join(repo, gitdir))
    dirs = [gitdir]
    try:
        with open(os.path.join(gitdir, "commondir"), encoding="utf-8") as handle:
            common = handle.readline().strip()
        dirs.append(os.path.normpath(common if os.path.isabs(common) else os.path.join(gitdir, common)))
    except OSError:
        pass
    return dirs


def git_activity(repo):
    """Latest git activity of a repo: index, HEAD log, ref logs, and the index and HEAD log of every linked
    worktree (work done only in an agent worktree still counts)."""
    times = []
    for gitdir in git_dirs(repo):
        for rel in ("index", "logs/HEAD"):
            try:
                times.append(os.lstat(os.path.join(gitdir, rel)).st_mtime)
            except OSError:
                pass
        refs = recent_mtime(os.path.join(gitdir, "logs", "refs"), 6)
        if refs:
            times.append(refs)
        for worktree in glob.glob(os.path.join(gitdir, "worktrees", "*")):
            for rel in ("index", "logs/HEAD"):
                try:
                    times.append(os.lstat(os.path.join(worktree, rel)).st_mtime)
                except OSError:
                    pass
    return max(times) if times else None


def tree_activity(folder, deadline):
    """(latest mtime in the source tree, complete): every file and folder except artifact folders and .git,
    bounded by ACTIVITY_MAX_ENTRIES and the deadline."""
    try:
        best = os.lstat(folder).st_mtime
    except OSError:
        return None, False
    count, stack = 0, [folder]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                entries = list(it)
        except OSError:
            continue
        for entry in entries:
            count += 1
            if count > ACTIVITY_MAX_ENTRIES or time.monotonic() > deadline:
                return best, False
            if entry.name == ".git" or (entry.name in PROJECT_ARTIFACTS and entry.is_dir(follow_symlinks=False)):
                continue
            try:
                mtime = entry.stat(follow_symlinks=False).st_mtime
            except OSError:
                continue
            if mtime > best:
                best = mtime
            if entry_is_dir(entry):
                stack.append(entry.path)
    return best, True


def project_activity(project, repo, deadline):
    last, complete = tree_activity(project, deadline)
    if repo:
        git_last = git_activity(repo)
        if git_last and (last is None or git_last > last):
            last = git_last
    return last, complete


def worktree_activity(path):
    """Latest activity in one linked worktree: its files and its own index and HEAD log (not the shared repo's,
    which work in any other worktree refreshes)."""
    last, _ = tree_activity(path, time.monotonic() + 5)
    times = [last] if last is not None else []
    for gitdir in git_dirs(path)[:1]:
        for rel in ("index", "logs/HEAD"):
            try:
                times.append(os.lstat(os.path.join(gitdir, rel)).st_mtime)
            except OSError:
                pass
    return max(times) if times else None


def worktree_blocker(git, path):
    """Why a worktree must not be removed (None when it may): `git worktree remove` without --force refuses
    uncommitted changes but deletes git-ignored files, so ignored files other than build artifacts block it."""
    # --no-optional-locks: a plain `git status` refreshes the index, which would make the worktree look active
    code, out, err = run_cmd([git, "--no-optional-locks", "-C", path, "status", "--porcelain", "--ignored"],
                             QUERY_TIMEOUT)
    if code != 0:
        return "git status failed: %s" % (summarize(err) or "exit %s" % code)
    dirty, ignored = [], []
    for line in out.splitlines():
        if len(line) < 4:
            continue
        name = line[3:].strip().strip('"')
        if line.startswith("!! "):
            parts = [p for p in name.rstrip("/").split("/") if p]
            if not any(p in WORKTREE_IGNORED_OK for p in parts):
                ignored.append(name)
        else:
            dirty.append(name)
    if dirty:
        return "uncommitted changes"
    if ignored:
        return "ignored files that are not build artifacts: %s%s" % (
            ", ".join(ignored[:3]), " (+%d more)" % (len(ignored) - 3) if len(ignored) > 3 else "")
    return None


def find_projects(ctx, only):
    if not wants(only, "project:", "worktree:"):
        return []
    found, repos = walk_projects(ctx)
    items = []
    needs_git = bool(repos) or any(repo for _, _, repo, _ in found)
    git = tool("git") if needs_git else None
    if needs_git and not git:
        if which("git"):
            note_missing_clt(ctx, "build artifacts inside git repos and agent worktrees")
        else:
            ctx.notes.append("git not found: build artifacts inside git repos and agent worktrees skipped")
    if wants(only, "project:"):
        projects = {}
        for artifact, project, repo, repo_rules in found:
            if repo is not None:
                if not git:
                    continue  # cannot tell generated from committed: never list it
                if ctx.time_left() <= 0 or not git_artifact_ok(git, repo, artifact, repo_rules):
                    continue
            projects.setdefault((project, repo), []).append(artifact)
        for (project, repo), artifacts in sorted(projects.items(), key=lambda kv: kv[0][0]):
            last, complete = project_activity(project, repo, ctx.deadline)
            last = last or ctx.now
            active = not complete or ctx.now - last < ctx.stale
            if active and not ctx.include_active:
                continue
            kinds = ", ".join(sorted({os.path.basename(a) for a in artifacts}))
            state = ""
            if not complete:
                state = " (activity unknown: too large to check in time; treated as active)"
            elif active:
                state = " (active project)"
            note = "%s; recreated by the project's install/build. Last activity %s ago%s." % (
                kinds, fmt_age(ctx.now - last), state)
            items.append(make_item("project:" + rel_home(project), "B", "Build artifacts of %s" % rel_home(project),
                                   note, sorted(artifacts), meta={"project_dir": project, "active": active}))
    if wants(only, "worktree:") and git:
        for repo in sorted(repos):
            base = os.path.join(repo, ".claude", "worktrees")
            try:
                entries = sorted(os.scandir(base), key=lambda e: e.name)
            except OSError:
                continue
            for entry in entries:
                if not entry_is_dir(entry) or not os.path.isfile(os.path.join(entry.path, ".git")):
                    continue
                if ctx.excluded(entry.path):
                    continue
                last = worktree_activity(entry.path) or ctx.now
                if ctx.now - last < ctx.stale:
                    continue
                items.append(make_item(
                    "worktree:" + rel_home(entry.path), "B", "Agent worktree %s" % rel_home(entry.path),
                    "Worktree of %s untouched for %s; removed with git worktree remove only when it has no "
                    "uncommitted changes and no ignored files other than build artifacts (those go with it); "
                    "its branch and commits are kept." % (rel_home(repo), fmt_age(ctx.now - last)),
                    [entry.path], remove=[], commands=[["git", "-C", repo, "worktree", "remove", entry.path]],
                    meta={"repo": repo, "worktree": entry.path}))
    return items


def find_personal(ctx, only):
    """Tier C: transcripts, Trash, big old downloads, Time Machine local snapshots (report only)."""
    items = []
    if wants(only, "claude-transcripts"):
        paths = []
        for project in sorted(glob.glob(hpath(".claude/projects/*"))):
            if not is_dir(project):
                continue
            try:
                entries = sorted(os.scandir(project), key=lambda e: e.name)
            except OSError:
                continue
            paths += [e.path for e in entries if e.name != "memory"
                      and ctx.now - (recent_mtime(e.path, 1) or ctx.now) >= TRANSCRIPT_MIN_AGE]
        if paths:
            items.append(make_item(
                "claude-transcripts", "C", "Claude Code transcripts",
                "Session history (resume, /history); memory folders and today's sessions are kept. Claude Code "
                "deletes transcripts older than cleanupPeriodDays (default 30) by itself.", paths))
    if wants(only, "trash"):
        trash = hpath(".Trash")
        if is_dir(trash):
            try:
                with os.scandir(trash) as it:
                    paths = sorted(e.path for e in it if e.name != ".DS_Store")
                if paths:
                    items.append(make_item("trash", "C", "Trash", "Emptying the Trash is permanent.", paths))
            except PermissionError:
                items.append(make_item("trash", "C", "Trash",
                                       "No access without Full Disk Access; empty it from the Finder.",
                                       deletable=False, remove=[], access="no access"))
            except OSError:
                pass
    if wants(only, "download:"):
        downloads = hpath("Downloads")
        for path, age in sorted(big_old_files(downloads, ctx)):
            rel = os.path.relpath(path, downloads).replace(os.sep, "/")
            items.append(make_item("download:" + rel, "C", "Download %s" % rel,
                                   "Large file in Downloads, %s old; gone for good once deleted." % fmt_age(age),
                                   [path]))
    if wants(only, "time-machine-snapshots") and tool("tmutil"):
        code, out, _ = run_cmd([tool("tmutil"), "listlocalsnapshots", "/"], QUERY_TIMEOUT)
        count = sum(1 for line in out.splitlines() if "com.apple.TimeMachine" in line) if code == 0 else 0
        if count:
            items.append(make_item("time-machine-snapshots", "C", "Time Machine local snapshots (%d)" % count,
                                   "macOS purges them by itself when space is needed (report only).",
                                   deletable=False, remove=[]))
    return items


def big_old_files(folder, ctx, depth=0):
    """[(path, age)] of big old files; macOS bundles (libraries, apps, packages) are not looked into."""
    try:
        with os.scandir(folder) as it:
            entries = list(it)
    except OSError:
        return []
    found = []
    for entry in entries:
        try:
            if entry.is_file(follow_symlinks=False):
                st = entry.stat(follow_symlinks=False)
                age = ctx.now - max(st.st_mtime, st.st_ctime)
                if st.st_size >= DOWNLOAD_MIN_BYTES and age >= DOWNLOAD_DAYS * DAY:
                    found.append((entry.path, age))
            elif entry_is_dir(entry) and depth < 2 and not entry.name.lower().endswith(BUNDLE_EXTENSIONS):
                found += big_old_files(entry.path, ctx, depth + 1)
        except OSError:
            continue
    return found


FINDERS = (find_derived_data, find_device_support, find_simulators, find_catalog, find_claude_temp,
           find_projects, find_personal)


def discover(ctx, only=None):
    """Every target that exists now (no sizes). Hidden ids and excluded paths are dropped."""
    items = []
    for finder in FINDERS:
        items += finder(ctx, only)
    kept = []
    for item in items:
        if item["id"] in ctx.hide:
            continue
        if any(ctx.excluded(p) for p in item["paths"]):
            if item["commands"]:
                continue  # a command would touch the excluded path too
            for key in ("paths", "remove", "fallback"):
                item[key] = [p for p in item[key] if not ctx.excluded(p)]
            if not item["paths"]:
                continue
        times = [recent_mtime(p, 1) for p in item["paths"]]
        times = [t for t in times if t is not None]
        item["mtime"] = max(times) if times else None
        kept.append(item)
    return kept


# ---------------------------------------------------------------- sizing

def walk_size(paths, deadline):
    """(bytes, complete, unreadable) by walking the tree; links are not followed, hard links count once."""
    total, complete, unreadable, seen = 0, True, False, set()
    stack = list(paths)
    while stack:
        if time.monotonic() > deadline:
            return total, False, unreadable
        path = stack.pop()
        try:
            st = os.lstat(path)
        except OSError:
            unreadable = True
            continue
        if st.st_nlink > 1 and not stat.S_ISDIR(st.st_mode):
            key = (st.st_dev, st.st_ino)
            if key in seen:
                continue
            seen.add(key)
        blocks = getattr(st, "st_blocks", None)
        total += blocks * 512 if blocks is not None else st.st_size
        if stat.S_ISDIR(st.st_mode) and not is_link(path):
            try:
                with os.scandir(path) as entries:
                    stack.extend(e.path for e in entries)
            except OSError:
                unreadable = True
    return total, complete, unreadable


def du_size(paths, timeout):
    """(bytes, complete, unreadable) with `du -P -k -d 1`. On timeout, the finished parts are a lower bound."""
    totals, children, unreadable, complete = {}, {}, False, True
    for start in range(0, len(paths), 200):
        chunk = paths[start:start + 200]
        proc = subprocess.Popen(["du", "-P", "-k", "-d", "1"] + chunk, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            out, err = proc.communicate(timeout=max(1, timeout))
        except subprocess.TimeoutExpired:
            proc.kill()
            out, err = proc.communicate()
            complete = False
        if err.strip():
            unreadable = True
        for line in out.decode("utf-8", "replace").splitlines():
            size, _, path = line.partition("\t")
            if not size.isdigit():
                continue
            if path in chunk:
                totals[path] = int(size) * 1024
            else:
                parent = next((p for p in chunk if path.startswith(p.rstrip("/") + "/")), None)
                if parent:
                    children[parent] = children.get(parent, 0) + int(size) * 1024
        if not complete:
            break
    total = sum(totals.get(p, children.get(p, 0)) for p in paths)
    if any(p not in totals for p in paths):
        complete = False
    return total, complete, unreadable


def measure(item, budget_end):
    if not item["paths"]:
        return
    readable, blocked = [], 0
    for path in item["paths"]:
        if is_dir(path):
            try:
                with os.scandir(path) as it:
                    next(it, None)
            except PermissionError:
                blocked += 1
                continue
            except OSError:
                continue
        if os.path.lexists(path):
            readable.append(path)
    if blocked:
        item["access"] = "no access" if not readable else "partial"
    remaining = budget_end - time.monotonic()
    if remaining <= 0:
        item.update(size_bytes=0, size_complete=False)
        item["note"] += " (not measured: scan time budget used up)"
        return
    timeout = min(TARGET_TIMEOUT, remaining)
    if not readable:
        item.update(size_bytes=0, size_complete=False)
        return
    if sys.platform == "darwin" and os.path.exists("/usr/bin/du"):
        size, complete, unreadable = du_size(readable, timeout)
    else:
        size, complete, unreadable = walk_size(readable, time.monotonic() + timeout)
    item["size_bytes"] = size
    item["size_complete"] = complete and not blocked
    if unreadable and item["access"] == "ok":
        item["access"] = "partial"


# ---------------------------------------------------------------- scan

PUBLIC_KEYS = ("id", "tier", "title", "note", "paths", "commands", "size_bytes", "size_complete", "access",
               "mtime", "age_days", "deletable", "meta")


def public(item):
    return {key: item.get(key) for key in PUBLIC_KEYS}


def growth(items, previous):
    if not previous:
        return []
    before = previous.get("items") or {}
    rows = []
    for item in items:
        size = item.get("size_bytes") or 0
        old = before.get(item["id"])
        if isinstance(old, (int, float)):
            if size - old >= GROWTH_MIN_BYTES and size >= old * 1.1:
                rows.append({"id": item["id"], "previous_bytes": int(old), "bytes": size, "delta": int(size - old)})
        elif size >= NEW_ITEM_MIN_BYTES:
            rows.append({"id": item["id"], "previous_bytes": None, "bytes": size, "delta": size})
    return sorted(rows, key=lambda r: -r["delta"])


def cmd_scan(as_json, include_active):
    started = time.monotonic()
    cfg, warnings = load_config()
    ctx = Context(cfg, include_active)
    free, total = disk_usage()
    items = discover(ctx)
    for item in items:
        measure(item, ctx.deadline)
        item["age_days"] = round((ctx.now - item["mtime"]) / DAY, 1) if item["mtime"] else None
    items.sort(key=lambda i: (i["tier"], not i["deletable"], -(i["size_bytes"] or 0), i["id"]))
    history = read_history()
    previous_scan = next((r for r in reversed(history) if r["type"] == "scan"), None)
    previous = history[-1] if history else None
    rows = growth(items, previous_scan)
    since = None
    if previous and isinstance(record_free(previous), (int, float)):
        since = {"kind": previous["type"], "time": previous.get("time"),
                 "free_bytes": int(record_free(previous)), "delta_bytes": int(free - record_free(previous))}
    totals = {tier: sum(i["size_bytes"] or 0 for i in items if i["tier"] == tier and i["deletable"])
              for tier in "ABC"}
    result = {"version": 1, "time": ctx.now, "free_bytes": free, "total_bytes": total,
              "include_active": include_active, "items": [public(i) for i in items], "growth": rows,
              "since_previous": since, "totals": totals, "warnings": warnings, "notes": ctx.notes,
              "duration_s": round(time.monotonic() - started, 1)}
    atomic_write(state_file("last-scan.json"), json.dumps(result, indent=1) + "\n")
    append_history({"type": "scan", "time": ctx.now, "free": free, "total": total,
                    "items": {i["id"]: i["size_bytes"] for i in items if i["size_bytes"] is not None}})
    update_state(last_scan=ctx.now)
    if as_json:
        print(json.dumps(result, indent=1))
        return 0
    print_scan(result, cfg)
    return 0


def print_scan(result, cfg):
    free, total = result["free_bytes"], result["total_bytes"]
    low = free < cfg["min_free_gb"] * GB or (total and 100.0 * free / total < cfg["min_free_pct"])
    print("Disk: %s%s" % (free_line(free, total), " (low)" if low else ""))
    since = result["since_previous"]
    if since:
        print("Since the previous %s (%s ago): free space %s" % (
            since["kind"], fmt_age(result["time"] - (since["time"] or result["time"])),
            fmt_signed(since["delta_bytes"])))
    if result["growth"]:
        parts = ["%s %s%s" % (r["id"], fmt_signed(r["delta"]), "" if r["previous_bytes"] is not None else " (new)")
                 for r in result["growth"][:8]]
        print("Grew since the previous scan: " + ", ".join(parts))
    for warning in result["warnings"]:
        print("config: " + warning)
    for note in result.get("notes") or []:
        print("note: " + note)
    width = max([len(i["id"]) for i in result["items"]] + [10])
    for tier in "ABC":
        rows = [i for i in result["items"] if i["tier"] == tier]
        if not rows:
            continue
        print()
        print(TIER_TITLES[tier])
        for item in rows:
            size = fmt_size(item["size_bytes"], not item["size_complete"])
            age = "%s" % ("%d d" % item["age_days"] if item["age_days"] is not None else "-")
            flags = []
            if not item["deletable"]:
                flags.append("report only")
            if item["access"] != "ok":
                flags.append(item["access"])
            print("  %-*s %11s %6s  %s%s" % (width, item["id"], size, age, item["note"],
                                             " [%s]" % ", ".join(flags) if flags else ""))
    t = result["totals"]
    print()
    print("Deletable: A %s, B %s, C %s (du estimates; APFS clones can make the real gain smaller)."
          % (fmt_size(t["A"]), fmt_size(t["B"]), fmt_size(t["C"])))
    print("Scan took %d s." % round(result.get("duration_s") or 0))
    if not result["items"]:
        print("Nothing to clean was found.")
    else:
        print("Dry run: disk-cleanup.sh clean <id>...   delete: add --yes (tier C also needs --permanent)")


# ---------------------------------------------------------------- clean

def load_scan(ctx):
    path = state_file("last-scan.json")
    if not os.path.exists(path):
        raise Refused("no scan yet: run scan first")
    scan = read_json(path)
    age = ctx.now - float(scan.get("time") or 0)
    if age > SCAN_MAX_AGE or age < -300:
        raise Refused("the last scan is %s old: rescan first (clean uses only a scan younger than 24 h)"
                      % fmt_age(abs(age)))
    return scan


def plan_item(item_id, scanned, fresh, ctx, permanent, activity):
    """(plan or None, problem). A plan lists the commands to run and the paths to remove."""
    if scanned is None:
        return None, "not in the last scan (unknown id, or rescan)"
    if not scanned.get("deletable", True):
        return None, "report only: no delete method"
    if fresh is None:
        return None, "no longer matches its rule (changed since the scan): rescan"
    if not fresh.get("deletable", True):
        return None, "report only: no delete method"
    if "C" in (scanned.get("tier"), fresh.get("tier")) and not permanent:
        return None, "tier C (does not come back): add --permanent to delete it"
    reason = activity.reason(fresh)
    if reason:
        return None, "in use: " + reason
    blocker = worktree_check(fresh)
    if blocker:
        return None, blocker
    allowed = set(norm(p) for p in scanned.get("paths") or [])
    commands, fallback_paths, remove_paths, refused = [], [], [], []
    for argv in fresh["commands"]:
        exe = tool(argv[0])
        if exe:
            commands.append([exe] + argv[1:])
    use_fallback = not commands and bool(fresh["fallback"])
    for path in fresh["remove"] + (fresh["fallback"] if use_fallback else []):
        if norm(path) not in allowed:
            continue
        problem = check_path(path, fresh, ctx)
        if problem == "gone":
            continue
        if problem:
            refused.append((path, problem))
        else:
            remove_paths.append(path)
    if not commands and fresh["commands"] and not fresh["fallback"]:
        return None, "%s not found" % fresh["commands"][0][0]
    plan = {"id": item_id, "commands": commands, "remove": remove_paths, "refused": refused,
            "fallback": [p for p in fresh["fallback"] if norm(p) in allowed and not check_path(p, fresh, ctx)]
            if commands else [],
            "item": fresh, "estimate": scanned.get("size_bytes") or 0}
    if not commands and not remove_paths:
        if refused:
            return None, "refused: " + "; ".join("%s (%s)" % (rel_home(p), why) for p, why in refused)
        return plan, None  # nothing left: already gone
    return plan, None


def summarize(text):
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return (lines[-1][:200]) if lines else ""


def worktree_check(item):
    """For a worktree item: why it must be skipped now (None when it may be removed)."""
    path = (item.get("meta") or {}).get("worktree")
    if not path:
        return None
    git = tool("git")
    if not git:
        return "git not available"
    return worktree_blocker(git, path)


def execute(plan, ctx):
    """(status, detail): status is done / partly done / failed / skipped."""
    item, notes, failed = plan["item"], [], False
    blocker = worktree_check(item)  # once more, right before removing
    if blocker:
        return "skipped", blocker
    command_ok = True
    for argv in plan["commands"]:
        code, out, err = run_cmd(argv, COMMAND_TIMEOUT)
        if code == 0:
            notes.append("ran %s" % " ".join([os.path.basename(argv[0])] + argv[1:]))
            continue
        command_ok = False
        message = summarize(err) or summarize(out) or "exit %s" % code
        if argv[1:2] == ["-C"] and "worktree" in argv and ("modified or untracked" in err or "is dirty" in err):
            return "skipped", "uncommitted changes"
        notes.append("%s failed: %s" % (os.path.basename(argv[0]), message))
        break
    removals = list(plan["remove"]) + ([] if command_ok else list(plan["fallback"]))
    if not command_ok and not plan["fallback"]:
        failed = True
    removed = 0
    for path in removals:
        problem = check_path(path, item, ctx)  # once more, right before deleting
        if problem == "gone":
            continue
        if problem:
            notes.append("refused %s (%s)" % (rel_home(path), problem))
            failed = True
            continue
        errors = remove_path(path)  # never raises; errors come back as text
        if errors:
            failed = True
            notes.append("could not fully remove %s (%s)" % (rel_home(path), errors[0]))
        else:
            removed += 1
    if removed:
        notes.append("removed %d path(s)" % removed)
    for path, why in plan["refused"]:
        notes.append("refused %s (%s)" % (rel_home(path), why))
        failed = True
    did_something = removed or (plan["commands"] and command_ok)
    if failed:
        return ("partly done" if did_something else "failed"), "; ".join(notes)
    return "done", "; ".join(notes) or "nothing left to delete"


def cmd_clean(ids, yes, permanent):
    cfg, _ = load_config()
    base = Context(cfg)
    scan = load_scan(base)
    ctx = Context(cfg, bool(scan.get("include_active")))
    ids = list(dict.fromkeys(ids))
    by_id = {item["id"]: item for item in scan.get("items") or [] if isinstance(item, dict) and "id" in item}
    fresh = {item["id"]: item for item in discover(ctx, only=set(ids))}
    activity = Activity()
    plans, problems = [], []
    for item_id in ids:
        plan, problem = plan_item(item_id, by_id.get(item_id), fresh.get(item_id), ctx, permanent, activity)
        if problem:
            problems.append((item_id, problem))
        else:
            plans.append(plan)
    if not yes:
        print("Dry run: nothing is deleted. Add --yes to delete.")
        for plan in plans:
            print("%s (est. %s)" % (plan["id"], fmt_size(plan["estimate"])))
            for argv in plan["commands"]:
                print("  run:    %s" % " ".join(argv))
            for path in plan["fallback"]:
                print("  remove: %s (only if the command fails)" % path)
            for path in plan["remove"]:
                print("  remove: %s%s" % (path, " (link only)" if os.path.islink(path) else ""))
            for path, why in plan["refused"]:
                print("  refuse: %s (%s)" % (path, why))
            if not plan["commands"] and not plan["remove"]:
                print("  nothing left to delete")
        for item_id, problem in problems:
            print("%s: skip: %s" % (item_id, problem))
        print("Estimated total: %s" % fmt_size(sum(p["estimate"] for p in plans)))
        return 1 if problems or any(p["refused"] for p in plans) else 0
    free_before, total = disk_usage()
    results, estimated, bad = {}, 0, len(problems)
    for item_id, problem in problems:
        results[item_id] = "skipped"
        print("%s: skipped: %s" % (item_id, problem))
    for plan in plans:
        try:
            status, detail = execute(plan, ctx)
        except Exception as exc:  # keep going with the other items
            status, detail = "failed", "%s: %s" % (type(exc).__name__, exc)
        results[plan["id"]] = status
        if status in ("done", "partly done"):
            estimated += plan["estimate"]
        if status != "done":
            bad += 1
        print("%s: %s: %s" % (plan["id"], status, detail))
    free_after, total = disk_usage()
    freed = free_after - free_before
    done = [i for i, s in results.items() if s in ("done", "partly done")]
    append_history({"type": "clean", "time": ctx.now, "ids": done, "requested": ids, "results": results,
                    "estimated": estimated, "freed": freed, "free_before": free_before, "free_after": free_after,
                    "total": total})
    if done:
        update_state(last_clean=ctx.now)
    print("Estimated %s (du); really freed %s (free space %s -> %s)."
          % (fmt_size(estimated), fmt_signed(freed), fmt_size(free_before), fmt_size(free_after)))
    return 1 if bad else 0


# ---------------------------------------------------------------- due logic, hook

def due_info(cfg, state, free, total, at):
    last_scan = state.get("last_scan") if isinstance(state.get("last_scan"), (int, float)) else None
    last_clean = state.get("last_clean") if isinstance(state.get("last_clean"), (int, float)) else None
    last_nag = state.get("last_nag") if isinstance(state.get("last_nag"), (int, float)) else None
    snooze = state.get("snooze_until") if isinstance(state.get("snooze_until"), (int, float)) else None
    last = max([t for t in (last_scan, last_clean) if t is not None], default=None)
    pct = 100.0 * free / total if total else 100.0
    reasons = []
    if last is None:
        reasons.append("never checked")
    elif at - last >= cfg["interval_days"] * DAY:
        reasons.append("%s since the last scan/cleanup (interval %s d)" % (fmt_age(at - last), cfg["interval_days"]))
    if free < cfg["min_free_gb"] * GB:
        reasons.append("free space below %s GB" % cfg["min_free_gb"])
    if pct < cfg["min_free_pct"]:
        reasons.append("free space below %s %%" % cfg["min_free_pct"])
    critical = free < cfg["critical_free_gb"] * GB
    if critical and not reasons:
        reasons.append("free space below %s GB" % cfg["critical_free_gb"])
    snoozed = snooze is not None and snooze > at
    throttle = (cfg["critical_nag_hours"] if critical else cfg["nag_hours"]) * 3600
    throttled = last_nag is not None and at - last_nag < throttle
    due = bool(reasons) and (critical or not snoozed)
    return {"due": due, "critical": critical, "reasons": reasons, "snoozed": snoozed, "snooze_until": snooze,
            "throttled": throttled, "nag": due and not throttled, "last_clean": last_clean, "last_scan": last_scan,
            "last_nag": last_nag, "pct": pct}


def hook_message(info, free, total, at):
    last = info["last_clean"]
    cleanup = "last cleanup %s ago" % fmt_age(at - last) if last else "last cleanup never"
    head = "Storage check due (disk-cleanup%s): %s, %s." % (
        ", critical" if info["critical"] else "", free_line(free, total), cleanup)
    if info["critical"]:
        return (head + " Disk space is critically low: tell the user about it right away, before continuing. "
                "Then use the disk-cleanup skill: run the read-only scan, show the user what can be deleted and "
                "ask before deleting anything.")
    return (head + " Finish the user's current request first. Then use the disk-cleanup skill: run the read-only "
            "scan, show the user what can be deleted and ask before deleting anything; if they don't want it "
            "now, run snooze.")


def run_hook():
    try:
        if not is_mac():
            return 0
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
        event = payload.get("hook_event_name") if isinstance(payload, dict) else None
        if event not in HOOK_EVENTS:
            return 0
        if event == "SessionStart" and payload.get("source") == "compact":
            return 0
        cfg, _ = load_config()
        free, total = disk_usage()
        at = now()
        with state_lock(wait=0.5):
            state = load_state(strict=True)
            info = due_info(cfg, state, free, total, at)
            if not info["nag"]:
                return 0
            state["last_nag"] = at
            save_state(state)
        out = {"hookSpecificOutput": {"hookEventName": event, "additionalContext": hook_message(info, free, total, at)}}
        sys.stdout.write(json.dumps(out) + "\n")
    except Exception:
        log_error("hook: " + traceback.format_exc())
    return 0


# ---------------------------------------------------------------- status, snooze, config

def cmd_status():
    cfg, warnings = load_config()
    free, total = disk_usage()
    at = now()
    state = load_state(strict=False)
    info = due_info(cfg, state, free, total, at)
    print("disk:       %s%s" % (free_line(free, total), "" if is_mac() else " (not macOS: the skill only runs there)"))
    for label, key in (("last scan", "last_scan"), ("last clean", "last_clean"), ("last nudge", "last_nag")):
        value = info[key]
        print("%-11s %s" % (label + ":", "%s (%s ago)" % (fmt_time(value), fmt_age(at - value)) if value else "never"))
    if info["due"]:
        verdict = "yes%s: %s" % (" (CRITICAL)" if info["critical"] else "", "; ".join(info["reasons"]))
        if info["throttled"]:
            verdict += " (already nudged; next nudge after %s h)" % (
                cfg["critical_nag_hours"] if info["critical"] else cfg["nag_hours"])
    elif info["reasons"]:
        verdict = "snoozed (%s)" % "; ".join(info["reasons"])
    else:
        verdict = "no"
    print("check due:  " + verdict)
    snooze = info["snooze_until"]
    print("snooze:     %s" % ("until %s (critical alerts ignore it)" % fmt_time(snooze) if info["snoozed"] else "none"))
    points = [(r.get("time"), record_free(r)) for r in read_history()
              if isinstance(r.get("time"), (int, float)) and isinstance(record_free(r), (int, float))
              and at - r["time"] <= 30 * DAY]
    if len(points) >= 2:
        (t0, f0), (t1, f1) = points[0], points[-1]
        rate = (f1 - f0) / max((t1 - t0) / DAY, 1)
        shown = points[-5:]
        print("trend:      %s (%s/day over %s)" % (
            " -> ".join("%s %s" % (time.strftime("%m-%d", time.localtime(t)), fmt_size(f)) for t, f in shown),
            fmt_signed(rate), fmt_age(t1 - t0)))
    elif points:
        print("trend:      one record so far (%s free on %s)" % (fmt_size(points[0][1]), fmt_time(points[0][0])))
    else:
        print("trend:      no history yet (run scan)")
    print("hook:       %s" % hook_status())
    print("config:     %s (%s)" % (config_path(), "exists" if os.path.exists(config_path()) else "defaults"))
    for warning in warnings:
        print("            " + warning)
    return 0


def cmd_snooze(days):
    if days <= 0:
        raise Refused("--days must be > 0")
    until = now() + days * DAY
    update_state(snooze_until=until)
    print("Snoozed until %s; a critically low disk (< %s GB free) still gets a reminder."
          % (fmt_time(until), load_config()[0]["critical_free_gb"]))
    return 0


def cmd_config():
    cfg, warnings = load_config()
    effective = dict(cfg, project_roots=[expand(r) for r in cfg["project_roots"]],
                     exclude=[expand(e) for e in cfg["exclude"]])
    print("config file: %s (%s)" % (config_path(), "exists" if os.path.exists(config_path()) else
                                    "not found, defaults in use"))
    print(json.dumps(effective, indent=2, sort_keys=True))
    for warning in warnings:
        print("warning: " + warning)
    return 0


# ---------------------------------------------------------------- settings.json hook

def settings_path():
    return os.path.join(home(), ".claude", "settings.json")


def installed_script():
    return os.path.join(home(), ".claude", "skills", "disk-cleanup", "scripts", "disk-cleanup.sh")


def is_ours(handler):
    return isinstance(handler, dict) and TAG in str(handler.get("command") or "")


def strip_ours(groups, replacement=None):
    """(groups, removed): our handlers taken out; the first one is swapped for `replacement` in place."""
    out, removed, placed = [], 0, replacement is None
    for group in groups:
        handlers = group.get("hooks") if isinstance(group, dict) else None
        if not isinstance(handlers, list):
            out.append(group)
            continue
        kept = []
        for handler in handlers:
            if not is_ours(handler):
                kept.append(handler)
                continue
            removed += 1
            if not placed:
                kept.append(replacement)
                placed = True
        if kept or not handlers:  # a group that held only our handlers goes away
            out.append(dict(group, hooks=kept))
    return out, removed


def read_settings(path):
    if not os.path.exists(path):
        return {}
    data = read_json(path)
    if not isinstance(data, dict):
        raise ValueError("settings.json is not a JSON object")
    hooks = data.get("hooks", {})
    if not isinstance(hooks, dict) or any(not isinstance(groups, list) for groups in hooks.values()):
        raise ValueError("settings.json has an unexpected hooks layout")
    return data


def write_settings(path, original, data):
    text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    if original is not None and original == text.encode("utf-8"):
        return False
    if original is not None:
        with open(path + ".disk-cleanup.bak", "wb") as out:
            out.write(original)
            out.flush()
            os.fsync(out.fileno())
    # A linked settings.json (dotfiles) keeps its link: write the file it points to.
    atomic_write(os.path.realpath(path) if os.path.islink(path) else path, text)
    return True


def remove_ours(hooks, keep=()):
    total = 0
    for event in list(hooks):
        if event in keep:
            continue
        groups, removed = strip_ours(hooks[event])
        if removed:
            total += removed
            if groups:
                hooks[event] = groups
            else:
                del hooks[event]
    return total


def cmd_install_hook():
    path = settings_path()
    original = open(path, "rb").read() if os.path.exists(path) else None
    data = read_settings(path)
    if not os.path.isfile(installed_script()):
        print("disk-cleanup: warning: %s does not exist yet (link the skill first: run install.sh)"
              % installed_script(), file=sys.stderr)
    hooks = data.setdefault("hooks", {})
    for event in HOOK_EVENTS:
        handler = {"type": "command", "command": HOOK_COMMAND, "timeout": HOOK_TIMEOUT}
        groups, removed = strip_ours(hooks.get(event, []), handler)
        if not removed:
            groups.append({"hooks": [handler]})
        hooks[event] = groups
    remove_ours(hooks, keep=HOOK_EVENTS)  # a stray copy under another event
    changed = write_settings(path, original, data)
    print("disk-cleanup hook (%s) %s in %s" % (", ".join(HOOK_EVENTS), "installed" if changed else "already installed",
                                                path))
    return 0


def cmd_uninstall_hook():
    path = settings_path()
    if not os.path.exists(path):
        print("disk-cleanup: %s does not exist; nothing to remove" % path)
        return 0
    original = open(path, "rb").read()
    data = read_settings(path)
    hooks = data.get("hooks")
    total = remove_ours(hooks) if isinstance(hooks, dict) else 0
    if total and not hooks:
        del data["hooks"]
    if total:
        write_settings(path, original, data)
    print("disk-cleanup: removed %d hook handler(s) from %s" % (total, path))
    return 0


def hook_status():
    try:
        data = read_settings(settings_path())
    except Exception as exc:
        return "unknown (%s)" % exc
    events = sorted({event for event, groups in data.get("hooks", {}).items() for group in groups
                     if isinstance(group, dict) for handler in (group.get("hooks") or []) if is_ours(handler)})
    if not events:
        return "not installed (run: disk-cleanup.sh install-hook)"
    missing = [e for e in HOOK_EVENTS if e not in events]
    return "installed (%s)%s" % (", ".join(events), "; missing " + ", ".join(missing) if missing else "")


# ---------------------------------------------------------------- main

def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    parser = argparse.ArgumentParser(prog="disk-cleanup", description="Finds regenerable disk hogs on macOS and "
                                     "deletes only what the user approves.")
    sub = parser.add_subparsers(dest="command")
    scan = sub.add_parser("scan", help="read-only: measure every target and write last-scan.json")
    scan.add_argument("--json", action="store_true", help="print the result as JSON")
    scan.add_argument("--include-active", action="store_true", help="also list build artifacts of active projects")
    clean = sub.add_parser("clean", help="dry run by default; --yes deletes")
    clean.add_argument("ids", nargs="+", metavar="id")
    clean.add_argument("--yes", action="store_true", help="really delete (needs a scan younger than 24 h)")
    clean.add_argument("--permanent", action="store_true", help="allow tier C items (they do not come back)")
    sub.add_parser("status", help="free space, last scan/clean, due state, snooze, trend, hook")
    snooze = sub.add_parser("snooze", help="no reminders for a while (critical alerts still come)")
    snooze.add_argument("--days", type=float, default=7)
    sub.add_parser("config", help="print the effective config and its path")
    sub.add_parser("hook", help="SessionStart / UserPromptSubmit hook entry point (hook JSON on stdin)")
    sub.add_parser("install-hook", help="add the hook to ~/.claude/settings.json")
    sub.add_parser("uninstall-hook", help="remove it again")
    args = parser.parse_args(argv)
    if args.command == "hook":
        return run_hook()
    commands = {"scan": lambda: cmd_scan(args.json, args.include_active),
                "clean": lambda: cmd_clean(args.ids, args.yes, args.permanent),
                "status": cmd_status, "snooze": lambda: cmd_snooze(args.days), "config": cmd_config,
                "install-hook": cmd_install_hook, "uninstall-hook": cmd_uninstall_hook}
    if args.command not in commands:
        parser.print_help()
        return 2
    if args.command not in ("status", "config") and not is_mac():
        print("disk-cleanup: macOS only (this is %s); status and config work everywhere" % platform())
        return 1
    try:
        return commands[args.command]()
    except Refused as exc:
        print("disk-cleanup: %s" % exc)
        return 1
    except Exception as exc:
        print("disk-cleanup %s failed: %s: %s" % (args.command, type(exc).__name__, exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
