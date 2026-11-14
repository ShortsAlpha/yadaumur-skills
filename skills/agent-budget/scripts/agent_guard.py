#!/usr/bin/env python3
"""agent-guard: a PreToolUse hook that keeps subagents off Fable unless the user asked for it.

Hook (written into ~/.claude/settings.json by `install-hook`, matcher Agent|Task|Workflow):
  hook pre-tool-use   reads the hook JSON on stdin; prints a PreToolUse "deny" decision or nothing.
Rules for Agent / Task:
  1. subagent_type "fork": a fork runs on the main session's model (it ignores `model`), so it is
     denied while the session runs on Fable (the newest assistant entry's message.model), unless
     the user asked for Fable. Unknown main model: allowed.
  2. `model` missing or empty: denied (an omitted model can inherit the main model).
  3. `model` names Fable (alias `fable`, ids like claude-fable-...): denied unless the user asked.
Rules for Workflow (script = the file at tool_input.scriptPath, else tool_input.script, else the
  named workflow <cwd>/.claude/workflows/<name>.js|.mjs or ~/.claude/workflows/<name>.js|.mjs;
  a built-in workflow without a file cannot be checked and is allowed):
  4. a string literal in the script that is exactly "fable" or "claude-fable..." (trimmed, any
     case) is denied unless the user asked. Prose that merely mentions Fable is not.
  5. while the main session runs on Fable, an agent(...) call without a `model` key is denied
     (best-effort paren matching; a call that cannot be parsed is denied too). Never in a
     non-Fable session.
User override: the user's latest genuine prompt in the transcript mentions "fable" (any case, any
  suffix, e.g. "Fable'la"). Genuine prompt = a "user" entry, or a queued_command attachment
  (commandMode "prompt", a message typed while the agent was working), written by the human:
  not meta, not a compaction summary, not a sidechain, origin.kind "human" (or, without origin,
  turnOrigin absent/"human" and a human promptSource). App-injected blocks (<system-reminder>,
  <artifact-view-context>) are removed first; harness wrappers (<command-name>,
  <task-notification>, <cross-session-message>, ...) and entries made only of tool_result blocks
  are skipped. An image-only prompt is still the latest prompt (no override). The transcript is
  scanned backwards from its end and never more than MAX_SCAN bytes.
Known limits: a negation ("Fable kullanma") also unlocks Fable; a workflow whose agent() is
  called through an alias, or whose model comes from a variable set outside the script, is not
  recognised.

Fails open: bad input, a missing transcript, an unknown tool or any error allows silently.
Emergency switch: AGENT_GUARD_OFF=1 allows everything.
CLI (through agent-guard.sh): install-hook | uninstall-hook | status
Python 3.9+, stdlib only. Env for tests: AGENT_GUARD_SETTINGS (settings.json path).
"""
import argparse
import base64
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import time
import uuid

sys.dont_write_bytecode = True

TAG = "agent_guard.py"  # identifies our handler in settings.json
EVENT = "PreToolUse"
MATCHER = "Agent|Task|Workflow"
TIMEOUT = 10
AGENT_TOOLS = ("Agent", "Task")
MAX_SCAN = 128 * 1024 * 1024  # never read more than this from the end of a transcript
CHUNK = 1024 * 1024
MAX_SCRIPT = 2 * 1024 * 1024

REASON_NO_MODEL = ("agent-budget guard: pass `model` explicitly on every subagent spawn (haiku / sonnet / "
                   "opus per the agent-budget table). An omitted model can inherit the main session's "
                   "model, which may be Fable.")
REASON_FABLE = ("agent-budget guard: subagents run on Fable only when the user's latest message asks for "
                "Fable. Use opus / sonnet / haiku, or ask the user.")
REASON_FORK = ("agent-budget guard: this session runs on Fable and a fork would too (forks ignore `model`). "
               "Spawn a named agent type with an explicit non-Fable model instead.")
REASON_WORKFLOW = ("agent-budget guard: this workflow puts an agent on a Fable model; subagents run on Fable "
                   "only when the user's latest message asks for Fable. Use opus / sonnet / haiku in the "
                   "script, or ask the user.")
REASON_WORKFLOW_NO_MODEL = ("agent-budget guard: this session runs on Fable and an agent() call in this "
                            "workflow has no `model`, so it would inherit Fable. Add an explicit non-Fable "
                            "`model` (opus / sonnet / haiku) to every agent() call.")

# App-injected blocks inside a user's message: not the user's words.
INJECTED = re.compile(r"<(system-reminder|artifact-view-context)\b[^>]*>.*?(</\1\s*>|\Z)",
                      re.DOTALL | re.IGNORECASE)
# Harness-written user entries (slash commands, local command output, notifications, events, ...).
WRAPPER = re.compile(
    r"<(command-[a-z-]+|local-command-[a-z-]+|bash-[a-z-]+|task-notification|side-sessions-event|"
    r"ci-monitor-event|teammate-message|user-prompt-submit-hook|cross-session-message|scheduled-task|"
    r"create-pr-command|[a-z-]+-(event|notification))\b",
    re.IGNORECASE)
HUMAN_PROMPT_SOURCES = ("sdk", "human", "user", "cli", "terminal", "ide", "repl")
FABLE_LITERAL = re.compile(r"fable|claude-fable[\w.-]*")
WORKFLOW_EXTS = (".js", ".mjs")


def mentions_fable(value):
    return isinstance(value, str) and "fable" in value.casefold()


# ---------------------------------------------------------------- transcript

def reverse_entries(path):
    """JSON objects of a JSONL file, newest first, reading at most MAX_SCAN bytes from the end."""
    with open(path, "rb") as handle:
        handle.seek(0, os.SEEK_END)
        pos = handle.tell()
        read, rest = 0, b""
        while pos > 0 and read < MAX_SCAN:
            size = min(CHUNK, pos, MAX_SCAN - read)
            pos -= size
            handle.seek(pos)
            rest = handle.read(size) + rest
            read += size
            lines = rest.split(b"\n")
            rest = lines[0]  # may be the tail of a longer line: completed by the next chunk
            for line in reversed(lines[1:]):
                entry = parse_line(line)
                if entry is not None:
                    yield entry
        if pos == 0:
            entry = parse_line(rest)
            if entry is not None:
                yield entry


def parse_line(line):
    line = line.strip()
    if not line.startswith(b"{") or (b'"user"' not in line and b'"assistant"' not in line
                                     and b'"queued_command"' not in line):
        return None
    try:
        entry = json.loads(line.decode("utf-8", "replace"))
    except ValueError:
        return None
    return entry if isinstance(entry, dict) else None


def content_prompt(content):
    """The user's words in message content: text, "" for a media-only prompt, None for no prompt."""
    media = False
    if isinstance(content, str):
        texts = [content]
    elif isinstance(content, list):
        blocks = [block for block in content if isinstance(block, dict)]
        texts = [block.get("text") for block in blocks
                 if block.get("type") == "text" and isinstance(block.get("text"), str)]
        kinds = {block.get("type") for block in blocks}
        media = bool(kinds & {"image", "document"}) and "tool_result" not in kinds
    else:
        return None
    kept = []
    for text in texts:
        text = INJECTED.sub("", text).strip()
        if text and not WRAPPER.match(text):
            kept.append(text)
    if kept:
        return "\n".join(kept)
    return "" if media else None  # an image-only prompt is still the user's latest turn


def human_origin(record):
    """origin.kind "human"; without origin: turnOrigin absent/"human" and a human promptSource."""
    origin = record.get("origin")
    if origin is not None:
        return isinstance(origin, dict) and origin.get("kind") == "human"
    if record.get("turnOrigin") not in (None, "human"):
        return False  # sdk / host_synthetic / peer / scheduled ...: written by an agent or the app
    source = record.get("promptSource")
    return source is None or source in HUMAN_PROMPT_SOURCES


def prompt_text(entry):
    """The user's own words in a transcript entry ("" for an image-only prompt), or None if the
    entry is not a genuine prompt."""
    if entry.get("isMeta") or entry.get("isCompactSummary") or entry.get("isVisibleInTranscriptOnly") \
            or entry.get("isSidechain"):
        return None
    if entry.get("type") == "attachment":  # a message the user typed while the agent was working
        queued = entry.get("attachment")
        if not isinstance(queued, dict) or queued.get("type") != "queued_command" \
                or queued.get("commandMode") != "prompt" or queued.get("isMeta"):
            return None
        if queued.get("origin") is None and not queued.get("humanTurn"):
            return None
        if not human_origin(queued):
            return None
        return content_prompt(queued.get("prompt"))
    if entry.get("type") != "user" or not human_origin(entry):
        return None
    message = entry.get("message")
    return content_prompt(message.get("content") if isinstance(message, dict) else None)


def main_model(entry):
    if entry.get("type") != "assistant" or entry.get("isSidechain"):
        return None
    message = entry.get("message")
    model = message.get("model") if isinstance(message, dict) else None
    if isinstance(model, str) and model and not model.startswith("<"):  # "<synthetic>" entries
        return model
    return None


class Transcript:
    """Lazily finds the latest genuine prompt and the main model in one backward scan."""

    def __init__(self, path):
        self.path = path
        self._done = False
        self._prompt = None
        self._model = None

    def _scan(self):
        if self._done:
            return
        self._done = True
        if not isinstance(self.path, str) or not self.path or not os.path.isfile(self.path):
            return
        try:
            for entry in reverse_entries(self.path):
                if self._model is None:
                    self._model = main_model(entry)
                if self._prompt is None:
                    self._prompt = prompt_text(entry)
                if self._model is not None and self._prompt is not None:
                    break
        except OSError:
            pass

    @property
    def latest_prompt(self):
        self._scan()
        return self._prompt

    @property
    def model(self):
        self._scan()
        return self._model

    def on_fable(self):
        return mentions_fable(self.model)

    def user_asked_for_fable(self):
        return mentions_fable(self.latest_prompt)


# ---------------------------------------------------------------- workflow scripts

def read_text(path):
    try:
        with Path(path).open("rb") as handle:
            return handle.read(MAX_SCRIPT).decode("utf-8", "replace")
    except (OSError, ValueError):
        return None


def workflow_script(tool_input, cwd):
    """The workflow's source: scriptPath first, then script, then the named workflow's file.
    None when there is nothing to check (e.g. a built-in workflow)."""
    base = Path(cwd) if isinstance(cwd, str) and cwd else None
    script_path = tool_input.get("scriptPath")
    if isinstance(script_path, str) and script_path.strip():
        path = Path(script_path).expanduser()
        if not path.is_absolute() and base is not None:
            path = base / path
        text = read_text(path)
        if text is not None:
            return text
    script = tool_input.get("script")
    if isinstance(script, str) and script:
        return script
    name = tool_input.get("name")
    if isinstance(name, str) and name.strip() and ".." not in name:
        roots = ([base / ".claude" / "workflows"] if base is not None else []) + \
                [Path.home() / ".claude" / "workflows"]
        for root in roots:
            for ext in WORKFLOW_EXTS:
                candidate = root / (name.strip() + ext)
                if candidate.is_file():
                    text = read_text(candidate)
                    if text is not None:
                        return text
    return None


def js_scan(src):
    """(literals, code) for JavaScript source, roughly: the contents of every string/template
    literal, and the source with comments and literal contents blanked to spaces (same length,
    so parens can be matched on `code`). Identifier-like literals ('model') are kept as keys."""
    literals, out, i, n = [], list(src), 0, len(src)

    def blank(start, end):
        for k in range(start, min(end, n)):
            if out[k] != "\n":
                out[k] = " "

    while i < n:
        c = src[i]
        if src.startswith("//", i):
            end = src.find("\n", i)
            end = n if end < 0 else end
            blank(i, end)
            i = end
        elif src.startswith("/*", i):
            end = src.find("*/", i + 2)
            end = n if end < 0 else end + 2
            blank(i, end)
            i = end
        elif c in "'\"`":
            j, parts = i + 1, []
            while j < n and src[j] != c:
                if src[j] == "\\" and j + 1 < n:
                    parts.append(src[j + 1])
                    j += 2
                elif c != "`" and src[j] == "\n":
                    break
                elif c == "`" and src.startswith("${", j):
                    literals.append("".join(parts))  # template text before ${...}
                    parts, depth, j = [], 1, j + 2
                    while j < n and depth:
                        depth += {"{": 1, "}": -1}.get(src[j], 0)
                        j += 1
                else:
                    parts.append(src[j])
                    j += 1
            literal = "".join(parts)
            literals.append(literal)
            if not re.fullmatch(r"[A-Za-z_$][\w$]*", literal):
                blank(i + 1, j)
            i = j + 1
        else:
            i += 1
    return literals, "".join(out)


AGENT_CALL = re.compile(r"(?<![\w$])agent\s*\(")
MODEL_KEY = re.compile(r"""(?<![\w$.])(model|'model'|"model"|`model`)\s*[:,}]""")


def fable_literal(literals):
    return any(FABLE_LITERAL.fullmatch(text.strip().casefold()) for text in literals)


def agent_call_without_model(code):
    """True if some agent(...) call has no `model` key, or a call cannot be parsed."""
    for match in AGENT_CALL.finditer(code):
        if re.search(r"\bfunction\s*\*?\s*$", code[:match.start()]):
            continue  # the definition, not a call
        depth, end = 0, None
        for k in range(match.end() - 1, len(code)):
            if code[k] == "(":
                depth += 1
            elif code[k] == ")":
                depth -= 1
                if depth == 0:
                    end = k
                    break
        if end is None:
            return True  # unbalanced: cannot decide
        if not MODEL_KEY.search(code[match.end():end]):
            return True
    return False


def decide(payload):
    """The deny reason for a hook payload, or None to allow."""
    if not isinstance(payload, dict):
        return None
    tool = payload.get("tool_name")
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return None
    transcript = Transcript(payload.get("transcript_path"))
    if tool in AGENT_TOOLS:
        kind = tool_input.get("subagent_type")
        if isinstance(kind, str) and kind.strip().casefold() == "fork":
            if transcript.on_fable() and not transcript.user_asked_for_fable():
                return REASON_FORK
            return None
        model = tool_input.get("model")
        if model is None or not str(model).strip():
            return REASON_NO_MODEL
        if mentions_fable(str(model)) and not transcript.user_asked_for_fable():
            return REASON_FABLE
        return None
    if tool == "Workflow":
        script = workflow_script(tool_input, payload.get("cwd"))
        if not script:
            return None  # nothing to check (built-in workflow, unreadable file)
        literals, code = js_scan(script)
        if fable_literal(literals):
            return None if transcript.user_asked_for_fable() else REASON_WORKFLOW
        if transcript.on_fable() and agent_call_without_model(code) and not transcript.user_asked_for_fable():
            return REASON_WORKFLOW_NO_MODEL
        return None
    return None


def run_hook():
    try:
        if os.environ.get("AGENT_GUARD_OFF", "").strip() not in ("", "0"):
            return 0
        raw = sys.stdin.buffer.read().decode("utf-8-sig", "replace")
        reason = decide(json.loads(raw))
        if reason:
            sys.stdout.write(json.dumps({"hookSpecificOutput": {
                "hookEventName": EVENT, "permissionDecision": "deny",
                "permissionDecisionReason": reason}}) + "\n")
            sys.stdout.flush()
    except Exception:
        pass  # fail open: never break a session
    return 0


# ---------------------------------------------------------------- settings.json hook

def settings_path():
    custom = os.environ.get("AGENT_GUARD_SETTINGS")
    return Path(custom).expanduser() if custom else Path.home() / ".claude" / "settings.json"


def installed_script():
    """The skill's path through the ~/.claude/skills link, so the hook follows wherever the skill is linked from."""
    return Path.home() / ".claude" / "skills" / "agent-budget" / "scripts" / "agent_guard.py"


def interpreter():
    if os.name != "nt" and os.access("/opt/homebrew/bin/python3", os.X_OK):
        return "/opt/homebrew/bin/python3"  # survives brew upgrades, unlike a versioned Cellar path
    if not sys.executable:
        raise ValueError("Cannot determine the Python interpreter path")
    return os.path.abspath(sys.executable)


def hook_command(argv):
    """Shell command for the hook (same form as vault_live.hook_command); the tail always exits 0."""
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
    """(groups, removed): our handlers taken out. With `replacement`, the first group that held only
    our handler becomes our group (correct matcher) in place, else our group is appended."""
    out, removed, placed = [], 0, replacement is None
    for group in groups:
        handlers = group.get("hooks") if isinstance(group, dict) else None
        if not isinstance(handlers, list):
            out.append(group)
            continue
        kept = [handler for handler in handlers if not is_ours(handler)]
        count = len(handlers) - len(kept)
        removed += count
        if count and not kept and not placed:
            out.append(dict(group, matcher=MATCHER, hooks=[replacement]))
            placed = True
        elif kept or not handlers:  # a group that held only our handlers goes away
            out.append(dict(group, hooks=kept))
    if not placed:
        out.append({"matcher": MATCHER, "hooks": [replacement]})
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


def write_settings(path, original, data):
    text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    if original is not None and original == text.encode("utf-8"):
        return False
    if original is not None:
        backup = path.with_name(path.name + ".agent-guard.bak")
        with backup.open("wb") as out:
            out.write(original)
            out.flush()
            os.fsync(out.fileno())
    # A linked settings.json (dotfiles) keeps its link: write the file it points to.
    atomic_write(path.resolve() if path.is_symlink() else path, text)
    return True


def remove_ours(hooks, keep_event=None):
    total = 0
    for event in list(hooks):
        if event == keep_event:
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
    original = path.read_bytes() if path.exists() else None
    data = read_settings(path)
    script = installed_script()
    if not script.is_file():
        print("agent-guard: warning: %s does not exist yet (link the skill first: run install.sh)"
              % script, file=sys.stderr)
    handler = {"type": "command", "command": hook_command([interpreter(), str(script), "hook", "pre-tool-use"]),
               "timeout": TIMEOUT}
    hooks = data.setdefault("hooks", {})
    hooks[EVENT], _ = strip_ours(hooks.get(EVENT, []), handler)
    remove_ours(hooks, keep_event=EVENT)  # a stray copy under another event
    changed = write_settings(path, original, data)
    print("agent-guard hook (PreToolUse %s) %s in %s"
          % (MATCHER, "installed" if changed else "already installed", path))
    return 0


def cmd_uninstall_hook():
    path = settings_path()
    if not path.exists():
        print("agent-guard: %s does not exist; nothing to remove" % path)
        return 0
    original = path.read_bytes()
    data = read_settings(path)
    hooks = data.get("hooks")
    total = remove_ours(hooks) if isinstance(hooks, dict) else 0
    if total and not hooks:
        del data["hooks"]
    if total:
        write_settings(path, original, data)
    print("agent-guard: removed %d hook handler(s) from %s" % (total, path))
    return 0


def cmd_status():
    path = settings_path()
    data = read_settings(path)
    found = [(event, group.get("matcher"), handler)
             for event, groups in data.get("hooks", {}).items() for group in groups if isinstance(group, dict)
             for handler in (group.get("hooks") or []) if is_ours(handler)]
    print("settings: %s" % path)
    print("script:   %s (%s)" % (installed_script(), "found" if installed_script().is_file() else "MISSING"))
    print("switch:   AGENT_GUARD_OFF=%s" % (os.environ.get("AGENT_GUARD_OFF") or "(unset)"))
    if not found:
        print("hook:     not installed (run: agent-guard.sh install-hook)")
    for event, matcher, handler in found:
        print("hook:     %s matcher=%s timeout=%s" % (event, matcher, handler.get("timeout")))
        print("command:  %s" % decoded_command(handler.get("command")))
    return 0


# ---------------------------------------------------------------- main

def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    parser = argparse.ArgumentParser(prog="agent-guard",
                                     description="Keeps subagents off Fable unless the user asks for it.")
    sub = parser.add_subparsers(dest="command")
    hook = sub.add_parser("hook", help="Claude Code hook entry point (reads the hook JSON on stdin)")
    hook.add_argument("event", choices=("pre-tool-use",))
    sub.add_parser("install-hook", help="add the PreToolUse hook to ~/.claude/settings.json")
    sub.add_parser("uninstall-hook", help="remove it again")
    sub.add_parser("status", help="show whether the hook is installed")
    args = parser.parse_args(argv)
    if args.command == "hook":
        return run_hook()
    commands = {"install-hook": cmd_install_hook, "uninstall-hook": cmd_uninstall_hook, "status": cmd_status}
    if args.command not in commands:
        parser.print_help()
        return 2
    try:
        return commands[args.command]()
    except Exception as exc:
        print("agent-guard %s failed: %s: %s" % (args.command, type(exc).__name__, exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
