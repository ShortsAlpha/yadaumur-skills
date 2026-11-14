#!/usr/bin/env python3
"""vault-live: keeps the second-brain vault current during very long Claude Code sessions.

Hooks (written into ~/.claude/settings.json by `install-hooks`):
  hook stop           Stop: when enough new commits or active minutes with tree changes have
                      piled up since the last `mark` in a project, block the turn once with
                      "vault checkpoint due" (throttled). Active minutes cap each gap.
  hook session-start  SessionStart: record the session's cwd and baseline; right after a
                      compaction, ask the agent to checkpoint before it continues; on
                      startup/resume/clear, point the agent at an open handoff note (Devir.md
                      with status "açık") for the project, if the vault has one.
  Subagent payloads (agent_id/agent_type) are ignored: only the main agent writes the vault.
Agent CLI (through vault-live.sh):
  mark [--session ID] the vault is current: reset the counters of every project of this session
  receipt-session     the Beyin receipt session id (same formula as beyin_v3_bridge.py)
  status              state of this session/project, for debugging
  install-hooks / uninstall-hooks

Hooks fail open: any error exits 0 without output (logged to <state dir>/error.log).
Python 3.9+, stdlib only. State is machine-local: ~/.claude/vault-live/{state,config}.json.
Env for tests: VAULT_LIVE_HOME (state/config dir), VAULT_LIVE_NOW (clock, epoch seconds),
VAULT_LIVE_SETTINGS (settings.json path), VAULT_LIVE_VAULT (one more vault path to exclude, and the
vault searched for handoff notes instead of the default one).
Vault location: VAULT_LIVE_VAULT, else SECOND_BRAIN_DIR, else ~/second_brain, else C:/second_brain on
Windows. Non-git project folders: list their parent folders as "roots" in config.json (default none).
"""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import time
import traceback
import unicodedata
import uuid

sys.dont_write_bytecode = True

DEFAULTS = {"commits": 3, "minutes": 90, "nongit_minutes": 120, "min_block_interval_minutes": 30,
            "idle_gap_minutes": 30}
PRUNE_DAYS = 30
EXCLUDED_PARTS = ("scratch-workspaces", "appdata", "library")  # path components, case-insensitive
MARK_CMD = "bash ~/.claude/skills/vault-live/scripts/vault-live.sh mark"
TAG = "vault_live.py"  # identifies our handlers in settings.json
HOOK_EVENTS = (("Stop", "stop"), ("SessionStart", "session-start"))
GIT_TIMEOUT = 3
SHA = re.compile(r"[0-9a-f]{40,64}")
HANDOFF_GLOBS = ("projects/*/Devir.md", "youtube/*/Devir.md")
HANDOFF_OPEN = "açık"
HANDOFF_SOURCES = ("startup", "resume", "clear")
HANDOFF_MAX_FILES = 500  # safety cap only: a fresh clone gives every note the same recent mtime
HANDOFF_MAX_BYTES = 8192  # the frontmatter is at the top; never read a whole large note
HANDOFF_FROM_CHARS = 120


# ---------------------------------------------------------------- basics

def now():
    value = os.environ.get("VAULT_LIVE_NOW")
    if value:
        try:
            return float(value)
        except ValueError:
            pass
    return time.time()


def home_dir():
    custom = os.environ.get("VAULT_LIVE_HOME")
    return Path(custom).expanduser() if custom else Path.home() / ".claude" / "vault-live"


def state_path():
    return home_dir() / "state.json"


def load_config():
    cfg = dict(DEFAULTS, roots=[])
    try:
        raw = json.loads((home_dir() / "config.json").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        raw = {}
    if isinstance(raw, dict):
        for key in DEFAULTS:
            value = raw.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
                cfg[key] = value
        roots = raw.get("roots")
        if isinstance(roots, list) and all(isinstance(root, str) for root in roots):
            cfg["roots"] = roots
    return cfg


def log_line(text):
    """Best-effort diagnostic line in <state dir>/error.log; never raises."""
    try:
        path = home_dir() / "error.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_file() and path.stat().st_size > 64000:
            path.write_text("", encoding="utf-8")
        with path.open("a", encoding="utf-8") as out:
            out.write("%s %s\n" % (time.strftime("%Y-%m-%dT%H:%M:%S"), text))
    except Exception:
        pass


def log_error(where, exc):
    frames = traceback.extract_tb(exc.__traceback__)
    at = "%s:%s" % (frames[-1].name, frames[-1].lineno) if frames else "?"
    log_line("%s %s at %s: %s" % (where, type(exc).__name__, at, str(exc)[:300]))


# ---------------------------------------------------------------- paths and projects

def norm(path):
    return os.path.normcase(os.path.normpath(str(path)))


def is_under(child, parent, strict=False):
    c, p = norm(child), norm(parent)
    if c == p:
        return not strict
    return c.startswith(p.rstrip("\\/") + os.sep)


def safe_resolve(path):
    try:
        return Path(path).expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        return Path(os.path.abspath(str(path)))


def strict_resolve(value):
    """Same resolution as beyin_v3_bridge.working_directory: absolute, existing, resolved."""
    if not isinstance(value, str) or not value:
        return None
    try:
        path = Path(value)
        return path.resolve(strict=True) if path.is_absolute() else None
    except (RuntimeError, OSError, ValueError):
        return None


def hook_cwd(payload):
    """The hook's working directory, in beyin_v3_bridge.working_directory's order."""
    value = payload.get("cwd")
    if value is None:
        value = os.environ.get("CLAUDE_PROJECT_DIR")
    if value is None:
        try:
            value = os.getcwd()
        except OSError:
            return None
    return value if isinstance(value, str) and value else None


def default_vaults():
    """The default vault locations, in search order: ~/second_brain, then C:/second_brain on Windows."""
    paths = [Path.home() / "second_brain"]
    if os.name == "nt":
        paths.append(Path("C:/second_brain"))
    return paths


def vault_paths():
    """Every vault location that is never a project: the defaults, SECOND_BRAIN_DIR and VAULT_LIVE_VAULT."""
    paths = default_vaults()
    for variable in ("SECOND_BRAIN_DIR", "VAULT_LIVE_VAULT"):
        extra = os.environ.get(variable)
        if extra:
            paths.append(Path(extra))
    return [safe_resolve(path) for path in paths]


def excluded(path):
    """Home (or a folder above it), the vault, scratch/AppData/Library folders are never projects."""
    if is_under(safe_resolve(Path.home()), path):
        return True
    if any(is_under(path, vault) for vault in vault_paths()):
        return True
    return any(part.lower() in EXCLUDED_PARTS for part in Path(path).parts)


def git(cwd, *args, raw=False):
    """(returncode, stdout) of a read-only git command; (None, '') when git cannot run."""
    options = {"stdin": subprocess.DEVNULL, "stdout": subprocess.PIPE,
               "stderr": subprocess.DEVNULL, "timeout": GIT_TIMEOUT}
    if os.name == "nt":
        options["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
    try:
        done = subprocess.run(["git", "--no-optional-locks", "-C", str(cwd)] + list(args), **options)
    except (OSError, subprocess.SubprocessError):
        return None, ""
    out = done.stdout.decode("utf-8", "replace")
    return done.returncode, out if raw else out.strip()


def is_subagent(payload):
    """Subagent hook payloads carry agent_id; only the main agent writes the vault.
    agent_type alone also appears in main sessions started with --agent, so it does not count."""
    return bool(payload.get("agent_id"))


def find_project(cwd, cfg, extra=None):
    """(root, kind, head) for a project directory, else None; kind is 'git' or 'dir'.

    A dict passed as `extra` receives "main_root": the main checkout of a git worktree's repository
    (the folder holding the common .git), or None when unknown."""
    if not cwd:
        return None
    path = safe_resolve(cwd)
    if not path.is_dir():
        return None
    # One process for all: an unborn HEAD still prints the toplevel and the common dir, then fails on
    # HEAD. --git-common-dir may be relative to the cwd (no --path-format: older gits lack it).
    code, out = git(path, "rev-parse", "--show-toplevel", "--git-common-dir", "HEAD")
    lines = out.splitlines()
    if code is not None and lines and os.path.isabs(lines[0]):
        root = safe_resolve(lines[0])
        if not excluded(root):
            if isinstance(extra, dict) and len(lines) > 1 and lines[1].strip():
                common = safe_resolve(path / lines[1].strip())
                extra["main_root"] = common.parent if common.name.lower() == ".git" else None
            head = lines[2].strip() if code == 0 and len(lines) > 2 else ""
            return root, "git", head if SHA.fullmatch(head) else None
    for raw in cfg["roots"]:
        base = safe_resolve(raw)
        if is_under(path, base, strict=True):
            root = base / path.parts[len(base.parts)]
            return (root, "dir", None) if not excluded(root) else None
    return None


def git_head(root):
    code, out = git(root, "rev-parse", "-q", "--verify", "HEAD")
    return out if code == 0 and SHA.fullmatch(out) else None


def count_commits(root, base, head, since_ts):
    """Commits new to this line since the mark: not merges, not patch-equivalent to a commit on the
    mark's side (--cherry-pick --right-only), and authored after the mark minus a minute.

    A rebase or amend keeps the old author date, a branch switch or pull brings older commits:
    none of them counts as new work. Without a usable mark commit, the date filter alone decides."""
    if not head or head == base:
        return 0
    since = int(since_ts - 60)
    args = ["log", "--no-merges", "--format=%at", "--max-count=500", "--since=@%d" % since]
    code = None
    if base:
        code, out = git(root, *(args + ["--cherry-pick", "--right-only", "%s...%s" % (base, head)]))
    if code != 0:
        code, out = git(root, *(args + [head]))
    if code != 0:
        return 0
    return sum(1 for line in out.split() if line.isdigit() and int(line) >= since)


def tree_fingerprint(root):
    """'' for a clean tree (tracked files only), else a hash of the changed paths with their size
    and mtime: a file that merely stays modified is no new work, a further edit to it is.
    None when git cannot tell."""
    code, out = git(root, "status", "--porcelain", "-z", "--untracked-files=no", raw=True)
    if code != 0:
        return None
    tokens, parts, index = out.split("\0"), [], 0
    while index < len(tokens):
        token = tokens[index]
        index += 1
        if len(token) < 4:
            continue
        status, path = token[:2], token[3:]
        if "R" in status or "C" in status:
            index += 1  # -z puts the rename/copy source in the next field
        try:
            info = os.lstat(os.path.join(str(root), path))
            parts.append("%s\t%s\t%d\t%d" % (status, path, info.st_size, info.st_mtime_ns))
        except OSError:
            parts.append("%s\t%s\tgone" % (status, path))
    if not parts:
        return ""
    return hashlib.sha256("\n".join(sorted(parts)).encode("utf-8", "replace")).hexdigest()[:16]


# ---------------------------------------------------------------- state

def empty_state():
    return {"version": 1, "sessions": {}, "projects": {}}


class StateUnreadable(Exception):
    """state.json exists but cannot be read; it must never be overwritten then."""


def quarantine_state(path, exc):
    """Move a corrupt state.json aside as state.json.corrupt-<epoch>; the caller starts fresh."""
    target = path.with_name("%s.corrupt-%d" % (path.name, int(time.time())))
    if target.exists():
        target = target.with_name(target.name + "-" + uuid.uuid4().hex[:6])
    try:
        _safe_replace(path, target)
    except OSError as error:
        raise StateUnreadable("%s is corrupt and could not be moved aside (%s)" % (path, error))
    log_line("state.json was corrupt (%s: %s); moved to %s, starting fresh"
             % (type(exc).__name__, str(exc)[:200], target.name))


def load_state(quarantine=False):
    """The state ({} when absent). A read error such as a Windows sharing violation is retried once,
    then raises StateUnreadable so nothing overwrites the file. Corrupt JSON reads as empty; with
    `quarantine` (only under the state lock) it is also moved aside so the next write starts fresh."""
    path = state_path()
    for attempt in (0, 1):
        try:
            raw = path.read_bytes()
            break
        except FileNotFoundError:
            return empty_state()
        except OSError as exc:
            if attempt:
                raise StateUnreadable("%s is unreadable (%s: %s)" % (path, type(exc).__name__, exc))
            time.sleep(0.1)  # a sharing violation (antivirus, indexer) is usually gone by now
    try:
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("not a JSON object")
    except ValueError as exc:  # includes UnicodeDecodeError
        if quarantine:
            quarantine_state(path, exc)
        return empty_state()
    for key in ("sessions", "projects"):
        if not isinstance(data.get(key), dict):
            data[key] = {}
    data["version"] = 1
    return data


def _num(value, default=0.0):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else default


def prune(state, at):
    cutoff = at - PRUNE_DAYS * 86400
    state["sessions"] = {sid: rec for sid, rec in state["sessions"].items()
                         if isinstance(rec, dict) and _num(rec.get("ts")) >= cutoff}
    projects = {}
    for key, project in state["projects"].items():
        if not isinstance(project, dict) or not isinstance(project.get("sessions"), dict):
            continue
        sessions = {sid: entry for sid, entry in project["sessions"].items()
                    if isinstance(entry, dict) and _num(entry.get("seen_ts")) >= cutoff}
        if sessions:
            projects[key] = dict(project, sessions=sessions)
    state["projects"] = projects


def _safe_replace(source, destination):
    """os.replace, retried briefly for a Windows sharing violation (antivirus, indexer)."""
    attempts = 5 if os.name == "nt" else 1
    for attempt in range(attempts):
        try:
            os.replace(str(source), str(destination))
            return
        except PermissionError as exc:
            if attempt == attempts - 1 or getattr(exc, "winerror", None) not in (5, 32, 33):
                raise
            time.sleep(0.015 * (2 ** attempt))


def atomic_write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as out:
            out.write(text)
            out.flush()
            os.fsync(out.fileno())
        _safe_replace(temporary, path)
    finally:
        if temporary.exists():
            try:
                temporary.unlink()
            except OSError:
                pass


class StateLock:
    """Short exclusive lock around read-modify-write; a busy or stale lock never costs the turn."""

    def __init__(self):
        self.path = home_dir() / "state.json.lock"
        self.fd = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + 0.5
        while True:
            try:
                self.fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                return self
            except (FileExistsError, PermissionError):
                try:
                    if time.time() - self.path.stat().st_mtime > 10:
                        self.path.unlink()
                        continue
                except OSError:
                    pass
                if time.monotonic() > deadline:
                    return self  # proceed unlocked
                time.sleep(0.02)
            except OSError:
                return self

    def __exit__(self, *exc):
        if self.fd is not None:
            os.close(self.fd)
            try:
                self.path.unlink()
            except OSError:
                pass


def update_state(mutate):
    with StateLock():
        state = load_state(quarantine=True)
        result = mutate(state)
        prune(state, now())
        atomic_write(state_path(), json.dumps(state, ensure_ascii=False, indent=1) + "\n")
    return result


def project_key(root):
    return norm(root)


def get_entry(state, root, sid):
    project = state["projects"].get(project_key(root))
    if not isinstance(project, dict) or not isinstance(project.get("sessions"), dict):
        return None
    entry = project["sessions"].get(sid)
    return entry if isinstance(entry, dict) else None


def ensure_entry(state, root, kind, sid, cwd, head, at, dirty=None):
    """The (session, project) entry, created with a baseline on first sight."""
    project = state["projects"].get(project_key(root))
    if not isinstance(project, dict) or not isinstance(project.get("sessions"), dict):
        project = state["projects"][project_key(root)] = {"root": str(root), "kind": kind, "sessions": {}}
    project["root"], project["kind"] = str(root), kind
    entry = project["sessions"].get(sid)
    if not isinstance(entry, dict):
        entry = project["sessions"][sid] = {
            "cwd": cwd, "baseline_ts": at, "baseline_head": head, "mark_ts": at, "mark_head": head,
            "mark_dirty": dirty, "active_s": 0.0, "last_event_ts": at,
            "marked": False, "last_block_ts": None, "seen_ts": at}
    return entry


def reset_entry(entry, at, head, dirty):
    """A mark: the vault is current for this session and project."""
    entry.update(mark_ts=at, mark_head=head, mark_dirty=dirty, active_s=0.0, last_event_ts=at,
                 marked=True, seen_ts=at)


def advance(entry, at, cfg, count_gap=True):
    """(active seconds, last event) after an event at `at`: the gap since the session's previous
    Stop/SessionStart/mark counts as work, capped at idle_gap_minutes (a long turn still counts
    some, a night away no more than that); a resumed session's gap counts nothing."""
    last = _num(entry.get("last_event_ts"), _num(entry.get("seen_ts"), at))
    active = _num(entry.get("active_s"))
    gap = at - last
    if count_gap and gap > 0:
        active += min(gap, cfg["idle_gap_minutes"] * 60)
    return active, max(last, at)


# ---------------------------------------------------------------- evaluation

def throttled(entry, cfg, at):
    last = entry.get("last_block_ts")
    return isinstance(last, (int, float)) and at - last < cfg["min_block_interval_minutes"] * 60


def evaluate(entry, root, kind, head, cfg, active_s):
    """What accumulated since the last mark (or baseline) and whether a checkpoint is due.

    minutes are active minutes; "dirty" means the tracked tree changed since the mark."""
    info = {"kind": kind, "minutes": max(0.0, active_s / 60), "commits": None,
            "dirty": None, "due": False, "why": ""}
    if kind == "git":
        info["commits"] = count_commits(root, entry.get("mark_head"), head, _num(entry.get("mark_ts")))
        if info["commits"] >= cfg["commits"]:
            info["due"], info["why"] = True, "commits"
        elif info["minutes"] >= cfg["minutes"]:
            if info["commits"] == 0:
                fingerprint = tree_fingerprint(root)
                info["dirty"] = bool(fingerprint) and fingerprint != entry.get("mark_dirty")
            if info["commits"] > 0 or info["dirty"]:
                info["due"], info["why"] = True, "time"
    elif info["minutes"] >= cfg["nongit_minutes"]:
        info["due"], info["why"] = True, "time"
    return info


def mark_command(sid):
    return "%s --session %s" % (MARK_CMD, shlex.quote(sid))


def _plural(count, word):
    return "%d %s%s" % (count, word, "" if count == 1 else "s")


def block_reason(name, info, marked, sid):
    minutes = int(info["minutes"])
    if info["why"] == "commits":
        what = "%s in %s" % (_plural(info["commits"], "commit"), name)
    elif info["kind"] == "git":
        extra = _plural(info["commits"], "commit") if info["commits"] else "uncommitted changes"
        what = "%d active min of work (%s) in %s" % (minutes, extra, name)
    else:
        what = "%d active min of work in %s" % (minutes, name)
    since = "since the last vault checkpoint" if marked else "since vault-live started tracking this session"
    return ("vault-live: %s %s. Do a vault-live checkpoint (skill vault-live), then run `%s`; "
            "if nothing is worth writing, just run mark." % (what, since, mark_command(sid)))


# ---------------------------------------------------------------- handoff notes

def handoff_vault():
    """The vault searched for handoff notes: VAULT_LIVE_VAULT, else SECOND_BRAIN_DIR, else the first
    default location that exists (else the first default)."""
    for variable in ("VAULT_LIVE_VAULT", "SECOND_BRAIN_DIR"):
        extra = os.environ.get(variable)
        if extra:
            return Path(extra).expanduser()
    defaults = default_vaults()
    return next((path for path in defaults if path.is_dir()), defaults[0])


def read_frontmatter(path):
    """The JSON object between a leading '---' line and the next '---' line, else None."""
    try:
        with path.open("rb") as handle:
            raw = handle.read(HANDOFF_MAX_BYTES)
    except OSError:
        return None
    lines = raw.decode("utf-8", "replace").lstrip("\ufeff").splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            try:
                data = json.loads("\n".join(lines[1:index]))
            except (ValueError, RecursionError):  # RecursionError: absurdly deep nesting
                return None
            return data if isinstance(data, dict) else None
    return None


def nfc(value):
    """NFC-normalized and stripped: a macOS-written (NFD) 'açık' equals the composed one."""
    return unicodedata.normalize("NFC", value).strip()


def path_basename(value):
    """Last component of a Windows or POSIX path string, whatever platform this runs on."""
    parts = [part for part in re.split(r"[\\/]+", value.strip()) if part]
    return parts[-1] if parts else ""


def handoff_names(root, main_root=None):
    """Folder names a handoff note may use for this project: the root's, and for a git worktree also
    the main checkout's (from the common .git dir, or the folder before .claude/worktrees)."""
    names = {nfc(root.name).casefold()}
    if main_root is not None:
        names.add(nfc(Path(main_root).name).casefold())
    parts = Path(root).parts
    for index in range(1, len(parts) - 1):
        if parts[index].lower() == ".claude" and parts[index + 1].lower() == "worktrees":
            names.add(nfc(parts[index - 1]).casefold())
    names.discard("")
    return names


def handoff_matches(meta, names):
    status = meta.get("status")
    if not isinstance(status, str) or nfc(status) != HANDOFF_OPEN:
        return False
    project = meta.get("project")
    if isinstance(project, str) and nfc(project).casefold() in names:
        return True
    cwd = meta.get("cwd")
    return isinstance(cwd, str) and nfc(path_basename(nfc(cwd))).casefold() in names


def find_handoff(root, main_root=None):
    """(vault-relative path, meta, mtime) of the newest open handoff note for project `root`, else None."""
    vault = handoff_vault()
    if not vault.is_dir():
        return None
    names = handoff_names(root, main_root)
    candidates = []
    for pattern in HANDOFF_GLOBS:
        for path in vault.glob(pattern):
            try:
                candidates.append((path.stat().st_mtime, path))
            except OSError:
                continue
    candidates.sort(key=lambda item: item[0], reverse=True)
    for mtime, path in candidates[:HANDOFF_MAX_FILES]:  # newest first: the first match wins
        meta = read_frontmatter(path)
        if meta is not None and handoff_matches(meta, names):
            try:
                relative = path.relative_to(vault).as_posix()
            except ValueError:
                relative = path.as_posix()
            return relative, meta, mtime
    return None


def handoff_reminder(root, main_root=None):
    found = find_handoff(root, main_root)
    if not found:
        return None
    relative, meta, mtime = found
    when = meta.get("from")
    # One short line: the note's text never reshapes the injected context.
    when = " ".join(when.split())[:HANDOFF_FROM_CHARS].strip() if isinstance(when, str) else ""
    if not when:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(mtime))
    return ("vault-live: there is an open handoff note for this project (%s) from %s, at %s in the vault. "
            "Read it first (skill `devir`): verify the git state, then continue from its next step "
            "and mark it `devralındı`." % (root.name, when, relative))


# ---------------------------------------------------------------- hooks

def hook_stop(payload):
    if payload.get("stop_hook_active") is True or is_subagent(payload):
        return None
    sid = payload.get("session_id")
    if not isinstance(sid, str) or not sid or sid == "unknown":
        return None
    cfg, at = load_config(), now()
    cwd = hook_cwd(payload)
    found = find_project(cwd, cfg)
    if not found:
        return None
    root, kind, head = found
    entry = get_entry(load_state(), root, sid)  # StateUnreadable: no write at all
    if entry is None:  # first sight: baseline only
        dirty = tree_fingerprint(root) if kind == "git" else None
        update_state(lambda state: ensure_entry(state, root, kind, sid, cwd, head, at, dirty))
        return None
    active, _ = advance(entry, at, cfg)
    info = None if throttled(entry, cfg, at) else evaluate(entry, root, kind, head, cfg, active)

    def record(state):
        current = ensure_entry(state, root, kind, sid, cwd, head, at)
        current["active_s"], current["last_event_ts"] = advance(current, at, cfg)
        current["seen_ts"] = at
        if not (info and info["due"]):
            return False
        # A mark or another block that landed meanwhile wins.
        if current.get("mark_ts") != entry.get("mark_ts") or current.get("last_block_ts") != entry.get("last_block_ts"):
            return False
        current["last_block_ts"] = at
        return True

    if not update_state(record):
        return None
    return {"decision": "block", "reason": block_reason(root.name, info, entry.get("marked") is True, sid)}


def hook_session_start(payload):
    if is_subagent(payload):
        return None
    sid = payload.get("session_id")
    if not isinstance(sid, str) or not sid or sid == "unknown":
        return None
    cfg, at = load_config(), now()
    cwd = hook_cwd(payload)
    resolved = strict_resolve(cwd)  # what Beyin's bridge hashes into the receipt session
    extra = {}
    found = find_project(cwd, cfg, extra)
    source = payload.get("source")
    try:
        dirty = None
        if found and found[1] == "git" and get_entry(load_state(), found[0], sid) is None:
            dirty = tree_fingerprint(found[0])

        def record(state):
            if resolved is not None:
                state["sessions"][sid] = {"cwd": str(resolved), "ts": at}
            if found:
                entry = ensure_entry(state, found[0], found[1], sid, cwd, found[2], at, dirty)
                # A resumed session was away: the gap before it is no work.
                entry["active_s"], entry["last_event_ts"] = advance(entry, at, cfg, count_gap=source != "resume")
                entry["seen_ts"] = at

        if resolved is not None or found:
            update_state(record)
    except StateUnreadable as exc:
        log_error("hook session-start", exc)  # keep state untouched, still remind after a compaction
    if not found:
        return None
    texts = []
    if source == "compact":
        texts.append("vault-live: the context of this session was just compacted (project %s). Before continuing, "
                     "do a vault-live checkpoint (skill vault-live) so nothing from before the compaction is lost, "
                     "then run `%s`; if the vault is already current, just run mark."
                     % (found[0].name, mark_command(sid)))
    if source in HANDOFF_SOURCES:
        try:
            text = handoff_reminder(found[0], extra.get("main_root"))
            if text:
                texts.append(text)
        except Exception as exc:  # a broken vault never costs the other reminders
            log_error("hook session-start handoff", exc)
    if not texts:
        return None
    # One JSON object on stdout, whatever the number of reminders.
    return {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "\n\n".join(texts)}}


def run_hook(event):
    try:
        if hasattr(sys.stdin, "reconfigure"):
            sys.stdin.reconfigure(encoding="utf-8-sig", errors="replace")
        payload = json.loads(sys.stdin.read(1000000).lstrip("\ufeff").strip() or "{}")
        if not isinstance(payload, dict):
            return 0
        output = hook_stop(payload) if event == "stop" else hook_session_start(payload)
        if output:
            sys.stdout.write(json.dumps(output) + "\n")
            sys.stdout.flush()
    except Exception as exc:  # a hook must never break the session
        log_error("hook " + event, exc)
    return 0


# ---------------------------------------------------------------- agent CLI

def receipt_session_id(cwd, session_id):
    """beyin_v3_bridge: sha256(json.dumps([str(cwd), session_id])).hexdigest()[:24]."""
    return hashlib.sha256(json.dumps([str(cwd), session_id]).encode()).hexdigest()[:24]


def cli_target():
    return os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()


def project_facts(root, kind):
    """(HEAD, tree fingerprint) to store with a mark."""
    if kind != "git":
        return None, None
    return git_head(root), tree_fingerprint(root)


def cmd_mark(session=None):
    """With a session id: every project this session is tracked in, whatever the cwd.
    Without one: every tracked session of the cwd's (or CLAUDE_PROJECT_DIR's) project."""
    cfg, at = load_config(), now()
    sid = session or os.environ.get("CLAUDE_CODE_SESSION_ID") or None
    if sid:
        state = load_state()
        facts = {}
        for key, project in state["projects"].items():
            if (isinstance(project, dict) and isinstance(project.get("sessions"), dict)
                    and isinstance(project["sessions"].get(sid), dict)):
                root = Path(str(project.get("root") or key))
                facts[key] = (root.name,) + project_facts(root, project.get("kind"))
        if not facts:
            print("vault-live: session %s has no tracked project yet; nothing to mark." % sid)
            return 0

        def mark_session(state):
            for key, (_, head, dirty) in facts.items():
                entry = ((state["projects"].get(key) or {}).get("sessions") or {}).get(sid)
                if isinstance(entry, dict):
                    reset_entry(entry, at, head, dirty)

        update_state(mark_session)
        names = ", ".join(sorted(fact[0] for fact in facts.values()))
        print("vault-live: marked %s for session %s; counters reset." % (names, sid))
        return 0

    target = cli_target()
    found = find_project(target, cfg)
    if not found:
        print("vault-live: %s is not a tracked project and no session id is set; nothing to mark." % target)
        return 0
    root, kind, head = found
    dirty = tree_fingerprint(root) if kind == "git" else None

    def mark_project(state):
        project = state["projects"].get(project_key(root)) or {}
        entries = [e for e in (project.get("sessions") or {}).values() if isinstance(e, dict)]
        for entry in entries:
            reset_entry(entry, at, head, dirty)
        return len(entries)

    count = update_state(mark_project)
    if count:
        print("vault-live: no session id; marked all %d tracked session(s) of %s." % (count, root.name))
    else:
        print("vault-live: no session id and no tracked session for %s; nothing marked." % root.name)
    return 0


def cmd_receipt_session():
    sid = os.environ.get("CLAUDE_CODE_SESSION_ID")
    if not sid:
        print("vault-live: CLAUDE_CODE_SESSION_ID is not set; run this from a Claude Code Bash tool call "
              "(or use the 'Receipt session=' value Beyin injected).", file=sys.stderr)
        return 1
    try:
        record = load_state()["sessions"].get(sid)
    except StateUnreadable:
        record = None
    candidates = [record.get("cwd") if isinstance(record, dict) else None,
                  os.environ.get("CLAUDE_PROJECT_DIR")]
    try:
        candidates.append(os.getcwd())
    except OSError:
        pass
    for candidate in candidates:
        cwd = strict_resolve(candidate)
        if cwd is not None:
            print(receipt_session_id(cwd, sid))
            return 0
    print("vault-live: cannot resolve the session's working directory.", file=sys.stderr)
    return 1


def _when(ts):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if isinstance(ts, (int, float)) else "never"


def cmd_status():
    cfg, at = load_config(), now()
    sid = os.environ.get("CLAUDE_CODE_SESSION_ID") or None
    target = cli_target()
    found = find_project(target, cfg)
    print("vault-live status  (state %s)" % state_path())
    print("thresholds: commits %s, minutes %s (active, with changes), nongit_minutes %s, block interval %s min, "
          "idle gap %s min, roots %s"
          % (cfg["commits"], cfg["minutes"], cfg["nongit_minutes"], cfg["min_block_interval_minutes"],
             cfg["idle_gap_minutes"], ", ".join(cfg["roots"]) or "-"))
    if not found:
        print("%s: not a tracked project (home, vault, scratch, or a non-git folder outside the roots)." % target)
        return 0
    root, kind, head = found
    print("project: %s (%s), HEAD %s" % (root, kind, head[:8] if head else "-"))
    state = load_state()
    project = state["projects"].get(project_key(root)) or {}
    sessions = project.get("sessions") or {}
    chosen = {sid: sessions.get(sid)} if sid else dict(sessions)
    if not chosen or not any(isinstance(e, dict) for e in chosen.values()):
        print("session %s: not tracked yet (the next Stop or SessionStart hook records a baseline)." % (sid or "-"))
        return 0
    for session, entry in chosen.items():
        if not isinstance(entry, dict):
            print("session %s: not tracked yet." % session)
            continue
        info = evaluate(entry, root, kind, head, cfg, advance(entry, at, cfg)[0])
        if info["kind"] == "git" and info["dirty"] is None:
            fingerprint = tree_fingerprint(root)
            info["dirty"] = bool(fingerprint) and fingerprint != entry.get("mark_dirty")
        print("session %s:" % session)
        print("  baseline %s (HEAD %s); last mark %s (HEAD %s)%s" % (
            _when(entry.get("baseline_ts")), (entry.get("baseline_head") or "-")[:8],
            _when(entry.get("mark_ts")), (entry.get("mark_head") or "-")[:8],
            "" if entry.get("marked") else " [never marked]"))
        counts = "%d active min since mark" % int(info["minutes"])
        if kind == "git":
            counts += ", %d new commit(s), tracked tree changed since mark: %s" % (
                info["commits"], "yes" if info["dirty"] else "no")
        print("  " + counts)
        print("  last block: %s" % _when(entry.get("last_block_ts")))
        if info["due"] and throttled(entry, cfg, at):
            verdict = "due, but throttled (blocked less than %s min ago)" % cfg["min_block_interval_minutes"]
        else:
            verdict = ("DUE (%s)" % info["why"]) if info["due"] else "not due"
        print("  checkpoint: " + verdict)
    return 0


# ---------------------------------------------------------------- settings.json hooks

def settings_path():
    custom = os.environ.get("VAULT_LIVE_SETTINGS")
    return Path(custom).expanduser() if custom else Path.home() / ".claude" / "settings.json"


def installed_script():
    """The skill's path through the ~/.claude/skills link, so the hook follows wherever the skill is linked from."""
    return Path.home() / ".claude" / "skills" / "vault-live" / "scripts" / "vault_live.py"


def interpreter():
    if os.name != "nt" and os.access("/opt/homebrew/bin/python3", os.X_OK):
        return "/opt/homebrew/bin/python3"  # survives brew upgrades, unlike a versioned Cellar path
    if not sys.executable:
        raise ValueError("Cannot determine the Python interpreter path")
    return os.path.abspath(sys.executable)


def hook_command(argv):
    """Shell command for a hook, in the same form as beyin_v3_bridge.shell_command.

    The tail always exits 0: a missing script or interpreter must never become exit code 2,
    which Claude Code would treat as a blocking Stop and repeat."""
    if any(any(c in str(value) for c in "\r\n\x00") for value in argv):
        raise ValueError("Unsupported command path")
    if os.name != "nt":
        return shlex.join([str(value) for value in argv]) + " || true"
    script = "& " + " ".join("'" + str(v).replace("'", "''") + "'" for v in argv) + "; exit 0"
    encoded = base64.b64encode(script.encode("utf-16le")).decode()
    system = Path(os.environ.get("SYSTEMROOT") or os.environ.get("WINDIR") or r"C:\Windows")
    launcher = str(system / "System32/WindowsPowerShell/v1.0/powershell.exe").replace("\\", "/")
    return subprocess.list2cmdline([launcher]) + " -NoProfile -NonInteractive -EncodedCommand " + encoded


def decoded_command(command):
    if not isinstance(command, str):
        return ""
    match = re.search(r"-EncodedCommand\s+([A-Za-z0-9+/=]+)", command, re.IGNORECASE)
    if match:
        try:
            return base64.b64decode(match.group(1)).decode("utf-16le")
        except Exception:
            pass
    return command


def is_ours(handler):
    return isinstance(handler, dict) and any(
        TAG in decoded_command(handler.get(key)) or TAG in str(handler.get(key) or "")
        for key in ("command", "commandWindows"))


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
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8-sig"))
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
        backup = path.with_name(path.name + ".vault-live.bak")
        with backup.open("wb") as out:
            out.write(original)
            out.flush()
            os.fsync(out.fileno())
    # A linked settings.json (dotfiles) keeps its link: write the file it points to.
    atomic_write(path.resolve() if path.is_symlink() else path, text)
    return True


def cmd_install_hooks():
    path = settings_path()
    original = path.read_bytes() if path.exists() else None
    data = read_settings(path)
    script = installed_script()
    if not script.is_file():
        print("vault-live: warning: %s does not exist yet (link the skill first: run install.sh)"
              % script, file=sys.stderr)
    python = interpreter()
    timeout = 20 if os.name == "nt" else 5
    hooks = data.setdefault("hooks", {})
    for event, arg in HOOK_EVENTS:
        handler = {"type": "command", "command": hook_command([python, str(script), "hook", arg]),
                   "timeout": timeout}
        groups, removed = strip_ours(hooks.get(event, []), handler)
        if not removed:
            groups.append({"hooks": [handler]})
        hooks[event] = groups
    ours = {event for event, _ in HOOK_EVENTS}
    for event in list(hooks):
        if event not in ours:
            groups, removed = strip_ours(hooks[event])
            if removed:
                if groups:
                    hooks[event] = groups
                else:
                    del hooks[event]
    changed = write_settings(path, original, data)
    print("vault-live hooks (Stop, SessionStart) %s in %s" % ("installed" if changed else "already installed", path))
    return 0


def cmd_uninstall_hooks():
    path = settings_path()
    if not path.exists():
        print("vault-live: %s does not exist; nothing to remove" % path)
        return 0
    original = path.read_bytes()
    data = read_settings(path)
    hooks = data.get("hooks")
    total = 0
    if isinstance(hooks, dict):
        for event in list(hooks):
            groups, removed = strip_ours(hooks[event])
            if removed:
                total += removed
                if groups:
                    hooks[event] = groups
                else:
                    del hooks[event]
        if total and not hooks:
            del data["hooks"]
    if total:
        write_settings(path, original, data)
    print("vault-live: removed %d hook handler(s) from %s" % (total, path))
    return 0


# ---------------------------------------------------------------- main

def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    parser = argparse.ArgumentParser(prog="vault-live", description="Keeps the vault current during long sessions.")
    sub = parser.add_subparsers(dest="command")
    hook = sub.add_parser("hook", help="Claude Code hook entry point (reads the hook JSON on stdin)")
    hook.add_argument("event", choices=("stop", "session-start"))
    mark = sub.add_parser("mark", help="the vault is current: reset this session's counters (all its projects)")
    mark.add_argument("--session", help="session id (default: $CLAUDE_CODE_SESSION_ID; without one, "
                                        "every session of the current project)")
    sub.add_parser("receipt-session", help="print the Beyin receipt session id of this session")
    sub.add_parser("status", help="show the state of this session/project")
    sub.add_parser("install-hooks", help="add the Stop and SessionStart hooks to ~/.claude/settings.json")
    sub.add_parser("uninstall-hooks", help="remove them again")
    args = parser.parse_args(argv)
    if args.command == "hook":
        return run_hook(args.event)
    commands = {"mark": lambda: cmd_mark(args.session), "receipt-session": cmd_receipt_session, "status": cmd_status,
                "install-hooks": cmd_install_hooks, "uninstall-hooks": cmd_uninstall_hooks}
    if args.command not in commands:
        parser.print_help()
        return 2
    try:
        return commands[args.command]()
    except Exception as exc:
        print("vault-live %s failed: %s: %s" % (args.command, type(exc).__name__, exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
