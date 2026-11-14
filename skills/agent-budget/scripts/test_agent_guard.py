#!/usr/bin/env python3
"""Tests for agent_guard.py (stdlib unittest; temp dirs and synthetic transcripts only).

Run: py -3 test_agent_guard.py   (macOS: /opt/homebrew/bin/python3 test_agent_guard.py)
"""
import base64
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "agent_guard.py"
LAUNCHER = HERE / "agent-guard.sh"
sys.path.insert(0, str(HERE))
import agent_guard  # noqa: E402


def decoded(command):
    match = re.search(r"-EncodedCommand\s+([A-Za-z0-9+/=]+)", command)
    return base64.b64decode(match.group(1)).decode("utf-16le") if match else command


# ---------------------------------------------------------------- synthetic transcript entries

def prompt(text):
    return {"type": "user", "origin": {"kind": "human"}, "promptSource": "sdk",
            "message": {"role": "user", "content": text}}


def prompt_blocks(*texts):
    return {"type": "user", "origin": {"kind": "human"},
            "message": {"role": "user", "content": [{"type": "text", "text": t} for t in texts]}}


def tool_result(text):
    return {"type": "user", "toolUseResult": {"stdout": text},
            "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": text}]}}


def meta(text):
    return {"type": "user", "isMeta": True, "message": {"role": "user", "content": text}}


def peer(text):
    return {"type": "user", "isMeta": True, "origin": {"kind": "peer", "from": "a1"},
            "message": {"role": "user", "content": text}}


def notification(text):
    return {"type": "user", "origin": {"kind": "task-notification"},
            "message": {"role": "user", "content": "<task-notification>%s</task-notification>" % text}}


def compact_summary(text):
    return {"type": "user", "isCompactSummary": True, "isVisibleInTranscriptOnly": True,
            "message": {"role": "user", "content": text}}


def assistant(model, text="ok"):
    return {"type": "assistant", "message": {"role": "assistant", "model": model,
                                             "content": [{"type": "text", "text": text}]}}


def attachment(text):
    return {"type": "attachment", "attachment": {"type": "skill_listing", "content": text}}


def queued(prompt_value, origin=None, mode="prompt", **extra):
    """A message typed while the agent was working (stored only as an attachment)."""
    attached = {"type": "queued_command", "commandMode": mode, "prompt": prompt_value, "delivery_id": "d1",
                "origin": origin if origin is not None else {"kind": "human"}, "humanTurn": True}
    attached.update(extra)
    return {"type": "attachment", "attachment": attached}


def agent_written(text, **fields):
    """A user entry written by an agent or the app: no origin, non-human turnOrigin/promptSource."""
    entry = {"type": "user", "message": {"role": "user", "content": text}}
    entry.update(fields)
    return entry


def image_prompt(*texts):
    blocks = [{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"}}]
    return {"type": "user", "origin": {"kind": "human"},
            "message": {"role": "user", "content": blocks + [{"type": "text", "text": t} for t in texts]}}


SKILLS_REMINDER = ("<system-reminder>The following skills are available: agent-budget: never puts a subagent "
                   "on Fable unless the user asks.</system-reminder>")


class GuardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="agent-guard-test-"))
        self.env = {k: v for k, v in os.environ.items() if not k.startswith(("CLAUDE", "AGENT_GUARD"))}
        self.env["AGENT_GUARD_SETTINGS"] = str(self.tmp / "settings.json")
        self.env["PYTHONIOENCODING"] = "utf-8"

    def tearDown(self):
        shutil.rmtree(str(self.tmp), ignore_errors=True)

    def transcript(self, *entries):
        self.count = getattr(self, "count", 0) + 1
        path = self.tmp / ("session-%d.jsonl" % self.count)
        with path.open("w", encoding="utf-8", newline="\n") as out:
            for entry in entries:
                out.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return str(path)

    def run_hook(self, payload, env=None, raw=None):
        data = raw if raw is not None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        done = subprocess.run([sys.executable, str(SCRIPT), "hook", "pre-tool-use"], input=data,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=dict(self.env, **(env or {})),
                              timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stderr, b"")
        return done.stdout.decode("utf-8")

    def agent(self, transcript, tool="Agent", **tool_input):
        tool_input.setdefault("prompt", "do the thing")
        return {"session_id": "s1", "hook_event_name": "PreToolUse", "tool_name": tool,
                "tool_input": tool_input, "transcript_path": transcript, "cwd": str(self.tmp)}

    def assertDenied(self, out, contains="agent-budget guard"):
        data = json.loads(out)
        spec = data["hookSpecificOutput"]
        self.assertEqual(spec["hookEventName"], "PreToolUse")
        self.assertEqual(spec["permissionDecision"], "deny")
        self.assertIn(contains, spec["permissionDecisionReason"])

    def assertAllowed(self, out):
        self.assertEqual(out, "")

    # ------------------------------------------------------------ Agent / Task

    def test_missing_or_empty_model_is_denied(self):
        t = self.transcript(prompt("refactor this"), assistant("claude-opus-5-5"))
        self.assertDenied(self.run_hook(self.agent(t, subagent_type="coder")), "pass `model` explicitly")
        self.assertDenied(self.run_hook(self.agent(t, subagent_type="coder", model="")), "pass `model`")
        self.assertDenied(self.run_hook(self.agent(t, tool="Task", subagent_type="coder", model="  ")), "`model`")

    def test_non_fable_models_are_allowed(self):
        t = self.transcript(prompt("refactor this"), assistant("claude-fable-5-2"))
        for model in ("opus", "sonnet", "haiku", "claude-opus-5-5"):
            self.assertAllowed(self.run_hook(self.agent(t, subagent_type="coder", model=model)))
            self.assertAllowed(self.run_hook(self.agent(t, tool="Task", subagent_type="scout", model=model)))

    def test_fable_model_is_denied(self):
        t = self.transcript(prompt("refactor this"), assistant("claude-opus-5-5"))
        for model in ("fable", "Fable", "claude-fable-5-2"):
            self.assertDenied(self.run_hook(self.agent(t, subagent_type="coder", model=model)), "only when the user")
        self.assertDenied(self.run_hook(self.agent(t, tool="Task", subagent_type="coder", model="fable")))

    def test_fable_allowed_when_latest_prompt_asks_for_it(self):
        t = self.transcript(prompt("merhaba"), assistant("claude-fable-5-2"),
                            prompt("alt ajanları Fable'la aç"), assistant("claude-fable-5-2"),
                            tool_result("listing"), assistant("claude-fable-5-2"))
        self.assertAllowed(self.run_hook(self.agent(t, subagent_type="coder", model="fable")))
        self.assertAllowed(self.run_hook(self.agent(t, subagent_type="coder", model="claude-fable-5-2")))
        # text blocks (e.g. a prompt with an image) count too, reminders inside are ignored
        t = self.transcript(prompt_blocks(SKILLS_REMINDER, "bunu FABLE yapsın"), assistant("x"))
        self.assertAllowed(self.run_hook(self.agent(t, subagent_type="coder", model="fable")))

    def test_fable_denied_when_only_an_older_prompt_asked(self):
        t = self.transcript(prompt("Fable'la aç"), assistant("claude-fable-5-2"),
                            prompt("şimdi testleri yaz"), assistant("claude-fable-5-2"))
        self.assertDenied(self.run_hook(self.agent(t, subagent_type="coder", model="fable")))

    def test_fable_mentioned_only_in_injected_context_is_denied(self):
        cases = [
            [prompt(SKILLS_REMINDER + "\nkodu düzelt")],
            [prompt("kodu düzelt"), assistant("x"), tool_result("model: fable is configured")],
            [prompt("kodu düzelt"), meta("Fable was mentioned by a hook")],
            [prompt("kodu düzelt"), peer("the coder ran on Fable")],
            [prompt("kodu düzelt"), notification("Fable task finished")],
            [prompt("kodu düzelt"), compact_summary("the user asked for Fable earlier")],
            [prompt("kodu düzelt"), attachment("agent-budget: Fable rule")],
            [prompt("kodu düzelt"), prompt(SKILLS_REMINDER)],
            [prompt("kodu düzelt"),
             prompt("<command-name>/model</command-name><command-args>fable</command-args>")],
            [prompt("kodu düzelt"), prompt("<local-command-stdout>Set model to Fable</local-command-stdout>")],
            [prompt("kodu düzelt"), dict(prompt("Fable'la aç"), isSidechain=True)],
        ]
        for entries in cases:
            with self.subTest(last=entries[-1]):
                t = self.transcript(*(entries + [assistant("claude-fable-5-2")]))
                self.assertDenied(self.run_hook(self.agent(t, subagent_type="coder", model="fable")))

    def test_wrapper_entries_are_skipped_when_looking_for_the_latest_prompt(self):
        t = self.transcript(prompt("Fable'la aç"), assistant("x"), tool_result("r"),
                            prompt("<local-command-caveat>Caveat</local-command-caveat>"),
                            notification("done"), peer("report"))
        self.assertAllowed(self.run_hook(self.agent(t, subagent_type="coder", model="fable")))

    # ------------------------------------------------------------ forks

    def test_fork_on_fable_main_is_denied(self):
        t = self.transcript(prompt("bunu araştır"), assistant("claude-fable-5-2"), tool_result("r"))
        self.assertDenied(self.run_hook(self.agent(t, subagent_type="fork")), "forks ignore `model`")
        # the model passed to a fork is ignored by the harness, so it does not help
        self.assertDenied(self.run_hook(self.agent(t, subagent_type="fork", model="opus")), "fork")
        # ... unless the user asked for Fable
        t = self.transcript(prompt("Fable'la fork aç"), assistant("claude-fable-5-2"))
        self.assertAllowed(self.run_hook(self.agent(t, subagent_type="fork")))

    def test_fork_on_other_main_models_is_allowed(self):
        t = self.transcript(prompt("bunu araştır"), assistant("claude-fable-5-2"), assistant("claude-opus-5-5"),
                            assistant("<synthetic>"))
        self.assertAllowed(self.run_hook(self.agent(t, subagent_type="fork")))

    def test_fork_with_unknown_main_model_is_allowed(self):
        t = self.transcript(prompt("bunu araştır"))
        self.assertAllowed(self.run_hook(self.agent(t, subagent_type="fork")))
        self.assertAllowed(self.run_hook(self.agent(str(self.tmp / "missing.jsonl"), subagent_type="fork")))

    # ------------------------------------------------------------ Workflow

    def test_workflow(self):
        t = self.transcript(prompt("iş akışını çalıştır"), assistant("claude-opus-5-5"))
        fable = "await agent({ prompt: 'x', model: 'fable' })"
        fable_id = 'agent({prompt: "x", model: "claude-fable-5-2"})'
        clean = "await agent({ prompt: 'x', model: 'opus' }); await agent({ prompt: 'y' })"
        self.assertDenied(self.run_hook(self.agent(t, tool="Workflow", script=fable)), "workflow")
        self.assertDenied(self.run_hook(self.agent(t, tool="Workflow", script=fable_id)), "workflow")
        self.assertAllowed(self.run_hook(self.agent(t, tool="Workflow", script=clean)))
        (self.tmp / "flow.js").write_text("const a = await agent({model:`Fable`, prompt: 'p'})\n", encoding="utf-8")
        self.assertDenied(self.run_hook(self.agent(t, tool="Workflow", scriptPath=str(self.tmp / "flow.js"))))
        self.assertDenied(self.run_hook(self.agent(t, tool="Workflow", scriptPath="flow.js")))  # relative to cwd
        self.assertAllowed(self.run_hook(self.agent(t, tool="Workflow", scriptPath=str(self.tmp / "nope.js"))))
        t = self.transcript(prompt("workflow'u Fable ile çalıştır"), assistant("claude-opus-5-5"))
        self.assertAllowed(self.run_hook(self.agent(t, tool="Workflow", script=fable)))

    # ------------------------------------------------------------ fail open

    def test_other_tools_and_bad_input_allow_silently(self):
        t = self.transcript(prompt("x"), assistant("claude-fable-5-2"))
        self.assertAllowed(self.run_hook({"tool_name": "Bash", "tool_input": {"command": "ls"}, "transcript_path": t}))
        self.assertAllowed(self.run_hook({"tool_name": "Read", "tool_input": {"model": "fable"}}))
        self.assertAllowed(self.run_hook(None, raw=b"not json {"))
        self.assertAllowed(self.run_hook(None, raw=b""))
        self.assertAllowed(self.run_hook(None, raw=b"[1, 2]"))
        self.assertAllowed(self.run_hook({"tool_name": "Agent", "tool_input": "oops"}))
        # missing / unreadable transcript: the rules still apply, the override just cannot be found
        missing = str(self.tmp / "missing.jsonl")
        self.assertAllowed(self.run_hook(self.agent(missing, subagent_type="coder", model="opus")))
        self.assertDenied(self.run_hook(self.agent(missing, subagent_type="coder", model="fable")))
        self.assertDenied(self.run_hook(self.agent(None, subagent_type="coder")))
        garbage = self.tmp / "garbage.jsonl"
        garbage.write_bytes(b"\xff\xfe garbage\n{not json\n\n[]\n")
        self.assertDenied(self.run_hook(self.agent(str(garbage), subagent_type="coder", model="fable")))

    def test_emergency_switch(self):
        t = self.transcript(prompt("x"), assistant("claude-fable-5-2"))
        payload = self.agent(t, subagent_type="coder", model="fable")
        self.assertAllowed(self.run_hook(payload, env={"AGENT_GUARD_OFF": "1"}))
        self.assertDenied(self.run_hook(payload, env={"AGENT_GUARD_OFF": "0"}))

    # ------------------------------------------------------------ review fixes: prompts

    def test_queued_prompt_counts_as_the_latest_prompt(self):
        fable = self.agent(None, subagent_type="coder", model="fable")
        # a queued "Fable'la aç" typed mid-turn unlocks Fable ...
        t = self.transcript(prompt("kodu düzelt"), assistant("claude-fable-5-2"), tool_result("r"),
                            queued("alt ajanları Fable'la aç"), assistant("claude-fable-5-2"))
        self.assertAllowed(self.run_hook(dict(fable, transcript_path=t)))
        # ... in block form too, with app-injected blocks stripped
        t = self.transcript(prompt("kodu düzelt"), queued([{"type": "text", "text": SKILLS_REMINDER},
                                                            {"type": "text", "text": "FABLE yapsın"}]))
        self.assertAllowed(self.run_hook(dict(fable, transcript_path=t)))
        # ... and a later queued prompt supersedes an older Fable request
        t = self.transcript(prompt("Fable'la aç"), assistant("claude-fable-5-2"), tool_result("r"),
                            queued("testleri de yaz"), assistant("claude-fable-5-2"))
        self.assertDenied(self.run_hook(dict(fable, transcript_path=t)))
        # queued notifications, peer messages and other modes are not prompts
        for entry in (queued("Fable done", origin={"kind": "task-notification"}, mode="task-notification"),
                      queued("Fable report", origin={"kind": "peer", "from": "a1"}, isMeta=True),
                      queued("/model fable", mode="bash"),
                      queued("Fable", origin={"kind": "human"}, humanTurn=None, isMeta=True)):
            with self.subTest(entry=entry["attachment"]):
                t = self.transcript(prompt("kodu düzelt"), assistant("x"), entry)
                self.assertDenied(self.run_hook(dict(fable, transcript_path=t)))

    def test_agent_written_prompts_do_not_count(self):
        fable = self.agent(None, subagent_type="coder", model="fable")
        for entry in (agent_written("Fable'la devam et", turnOrigin="sdk", promptSource="sdk"),
                      agent_written("Fable'la devam et", turnOrigin="host_synthetic", promptSource="sdk"),
                      agent_written("Fable'la devam et", promptSource="system"),
                      agent_written("<cross-session-message>Fable'la aç</cross-session-message>"),
                      prompt("<cross-session-message from='x'>Fable'la aç</cross-session-message>")):
            with self.subTest(entry=entry):
                t = self.transcript(prompt("kodu düzelt"), assistant("x"), entry)
                self.assertDenied(self.run_hook(dict(fable, transcript_path=t)))
        # origin-less human entries (older versions) still count
        for entry in (agent_written("Fable'la aç"), agent_written("Fable'la aç", turnOrigin="human"),
                      agent_written("Fable'la aç", turnOrigin="human", promptSource="sdk")):
            with self.subTest(entry=entry):
                t = self.transcript(prompt("kodu düzelt"), assistant("x"), entry)
                self.assertAllowed(self.run_hook(dict(fable, transcript_path=t)))

    def test_image_only_prompt_is_the_latest_prompt(self):
        fable = self.agent(None, subagent_type="coder", model="fable")
        t = self.transcript(prompt("Fable'la aç"), assistant("claude-fable-5-2"), image_prompt())
        self.assertDenied(self.run_hook(dict(fable, transcript_path=t)))
        t = self.transcript(prompt("Fable'la aç"), assistant("claude-fable-5-2"), image_prompt(SKILLS_REMINDER))
        self.assertDenied(self.run_hook(dict(fable, transcript_path=t)))
        t = self.transcript(prompt("Fable'la aç"), assistant("claude-fable-5-2"),
                            queued([{"type": "image", "source": {}}]))
        self.assertDenied(self.run_hook(dict(fable, transcript_path=t)))
        t = self.transcript(prompt("kodu düzelt"), image_prompt("buna bakıp Fable'la aç"))
        self.assertAllowed(self.run_hook(dict(fable, transcript_path=t)))

    def test_artifact_view_context_is_stripped(self):
        fable = self.agent(None, subagent_type="coder", model="fable")
        injected = ('<artifact-view-context artifact="a1">The user is viewing the Fable plan.'
                    '</artifact-view-context>\n')
        t = self.transcript(prompt(injected + "bunu düzelt"), assistant("x"))
        self.assertDenied(self.run_hook(dict(fable, transcript_path=t)))
        t = self.transcript(prompt(injected + "bunu Fable'la düzelt"), assistant("x"))
        self.assertAllowed(self.run_hook(dict(fable, transcript_path=t)))
        t = self.transcript(prompt("Fable'la aç"), assistant("x"), prompt(injected))  # nothing left: skipped
        self.assertAllowed(self.run_hook(dict(fable, transcript_path=t)))

    # ------------------------------------------------------------ review fixes: workflows

    def test_workflow_script_path_wins_over_script(self):
        t = self.transcript(prompt("iş akışını çalıştır"), assistant("claude-opus-5-5"))
        (self.tmp / "bad.js").write_text("await agent({prompt: 'p', model: 'fable'})\n", encoding="utf-8")
        (self.tmp / "good.js").write_text("await agent({prompt: 'p', model: 'opus'})\n", encoding="utf-8")
        bad_inline = "await agent({prompt: 'p', model: 'fable'})"
        self.assertDenied(self.run_hook(self.agent(t, tool="Workflow", scriptPath=str(self.tmp / "bad.js"),
                                                   script="await agent({prompt: 'p', model: 'opus'})")))
        self.assertAllowed(self.run_hook(self.agent(t, tool="Workflow", scriptPath=str(self.tmp / "good.js"),
                                                    script=bad_inline)))
        # an unreadable scriptPath falls back to script
        self.assertDenied(self.run_hook(self.agent(t, tool="Workflow", scriptPath=str(self.tmp / "nope.js"),
                                                   script=bad_inline)))

    def test_workflow_name_resolution(self):
        t = self.transcript(prompt("iş akışını çalıştır"), assistant("claude-opus-5-5"))
        home = self.tmp / "home"
        project = self.tmp / "project"
        (project / ".claude" / "workflows").mkdir(parents=True)
        (home / ".claude" / "workflows").mkdir(parents=True)
        (project / ".claude" / "workflows" / "local.mjs").write_text("agent({model: 'claude-fable-5-2'})\n",
                                                                    encoding="utf-8")
        (home / ".claude" / "workflows" / "global.js").write_text("agent({model: 'Fable'})\n", encoding="utf-8")
        (home / ".claude" / "workflows" / "ok.js").write_text("agent({model: 'haiku'})\n", encoding="utf-8")
        env = {"HOME": str(home), "USERPROFILE": str(home)}

        def run(name):
            payload = self.agent(t, tool="Workflow", name=name)
            payload["cwd"] = str(project)
            return self.run_hook(payload, env=env)

        self.assertDenied(run("local"))
        self.assertDenied(run("global"))
        self.assertAllowed(run("ok"))
        self.assertAllowed(run("builtin-without-file"))

    def test_workflow_string_literals(self):
        t = self.transcript(prompt("iş akışını çalıştır"), assistant("claude-opus-5-5"))
        cases = [
            ("const M = 'fable'\nawait agent({prompt: 'p', model: M})", True),
            ("const model = \"Claude-Fable-5-2\"\nawait agent({prompt: 'p', model})", True),
            ("await agent({prompt: `${x}`, model: ` fable `})", True),
            ("await agent({prompt: 'Compare with claude-fable-5-2 and Fable output', model: 'opus'})", False),
            ("// model: 'fable' was too slow\nawait agent({prompt: 'p', model: 'sonnet'})", False),
            ("await agent({prompt: \"it's the fable of the fox\", model: 'haiku'})", False),
        ]
        for script, denied in cases:
            with self.subTest(script=script):
                out = self.run_hook(self.agent(t, tool="Workflow", script=script))
                if denied:
                    self.assertDenied(out, "workflow")
                else:
                    self.assertAllowed(out)

    def test_workflow_agent_calls_without_model_on_a_fable_main(self):
        fable_main = self.transcript(prompt("iş akışını çalıştır"), assistant("claude-fable-5-2"))
        opus_main = self.transcript(prompt("iş akışını çalıştır"), assistant("claude-opus-5-5"))
        missing = [
            "await agent({prompt: 'p'})",
            "await agent({prompt: 'p', model: 'opus'}); await agent({prompt: 'q (with a paren'})",
            "await agent({...opts})",
            "const r = agent({prompt: 'p', model: 'opus'",  # unbalanced: cannot decide
        ]
        present = [
            "await agent({prompt: 'p', model: 'opus'})",
            "await agent({prompt: 'p (model?)', 'model': 'sonnet'}); await agent('q', {model})",
            "function agent(x) { return x }\nawait agent({prompt: 'p', model: 'haiku'})",
            "// agent({prompt: 'p'})\nconst x = 1",
            "await agent({prompt: `${a}`, model: 'opus'})",
        ]
        for script in missing:
            with self.subTest(script=script):
                self.assertDenied(self.run_hook(self.agent(fable_main, tool="Workflow", script=script)),
                                  "every agent() call")
                self.assertAllowed(self.run_hook(self.agent(opus_main, tool="Workflow", script=script)))
        for script in present:
            with self.subTest(script=script):
                self.assertAllowed(self.run_hook(self.agent(fable_main, tool="Workflow", script=script)))
        asked = self.transcript(prompt("bunu Fable'la çalıştır"), assistant("claude-fable-5-2"))
        self.assertAllowed(self.run_hook(self.agent(asked, tool="Workflow", script=missing[0])))

    # ------------------------------------------------------------ backward scan

    def test_backward_scan_across_chunks(self):
        entries = [prompt("p%d" % i) for i in range(30)] + [tool_result("y" * 5000), assistant("m")]
        path = self.transcript(*entries)
        saved = agent_guard.CHUNK, agent_guard.MAX_SCAN
        try:
            agent_guard.CHUNK = 37
            got = list(agent_guard.reverse_entries(path))
            self.assertEqual(got, list(reversed(entries)))
            transcript = agent_guard.Transcript(path)
            self.assertEqual((transcript.latest_prompt, transcript.model), ("p29", "m"))
            agent_guard.MAX_SCAN = 6000  # only the tail is read: the prompts are out of reach
            transcript = agent_guard.Transcript(path)
            self.assertEqual((transcript.latest_prompt, transcript.model), (None, "m"))
        finally:
            agent_guard.CHUNK, agent_guard.MAX_SCAN = saved

    # ------------------------------------------------------------ installer

    def run_cli(self, *args):
        done = subprocess.run([sys.executable, str(SCRIPT)] + list(args), stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, env=self.env, timeout=60)
        return done.returncode, done.stdout.decode("utf-8"), done.stderr.decode("utf-8")

    @staticmethod
    def ours(data):
        return [(event, group.get("matcher"), handler)
                for event, groups in data.get("hooks", {}).items() for group in groups
                for handler in group.get("hooks", []) if "agent_guard.py" in decoded(handler.get("command", ""))]

    def foreign_settings(self):
        return {
            "model": "opus",
            "hooks": {
                "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "python3 /x/beyin_v3_bridge.py pre"}]}],
                "Stop": [{"hooks": [{"type": "command", "command": "python3 /x/beyin_v3_bridge.py stop"}]},
                         {"hooks": [{"type": "command", "command": "python3 /x/vault_live.py hook stop", "timeout": 5}]}],
                "SessionStart": [{"hooks": [{"type": "command", "command": "python3 /x/vault_live.py hook session-start"}]}],
            },
            "permissions": {"allow": ["Bash(ls:*)"]},
        }

    def test_install_idempotent_and_uninstall(self):
        settings = self.tmp / "settings.json"
        original = self.foreign_settings()
        original_bytes = json.dumps(original, indent=2).encode("utf-8")
        settings.write_bytes(original_bytes)

        code, out, err = self.run_cli("install-hook")
        self.assertEqual(code, 0, err)
        self.assertIn("installed", out)
        first = settings.read_bytes()
        data = json.loads(first.decode("utf-8"))
        self.assertEqual(list(data), list(original))
        self.assertEqual(data["model"], "opus")
        self.assertEqual(data["permissions"], original["permissions"])
        for event in ("Stop", "SessionStart"):
            self.assertEqual(data["hooks"][event], original["hooks"][event])
        self.assertEqual(data["hooks"]["PreToolUse"][0], original["hooks"]["PreToolUse"][0])
        found = self.ours(data)
        self.assertEqual(len(found), 1)
        event, matcher, handler = found[0]
        self.assertEqual((event, matcher, handler["timeout"]), ("PreToolUse", "Agent|Task|Workflow", 10))
        command = decoded(handler["command"])
        self.assertIn(os.path.join(".claude", "skills", "agent-budget", "scripts", "agent_guard.py"), command)
        self.assertIn("'hook' 'pre-tool-use'; exit 0" if os.name == "nt" else "hook pre-tool-use", command)
        self.assertEqual((self.tmp / "settings.json.agent-guard.bak").read_bytes(), original_bytes)

        code, out, _ = self.run_cli("install-hook")
        self.assertIn("already installed", out)
        self.assertEqual(settings.read_bytes(), first)

        # a stale path / matcher of ours is re-pointed in place, not duplicated
        stale = json.loads(first.decode("utf-8"))
        stale["hooks"]["PreToolUse"][1] = {"matcher": "Agent", "hooks": [
            {"type": "command", "command": "python3 /old/place/agent_guard.py hook pre-tool-use"}]}
        stale["hooks"]["PreToolUse"].append({"matcher": "Edit", "hooks": [{"type": "command", "command": "echo other"}]})
        settings.write_text(json.dumps(stale, indent=2) + "\n", encoding="utf-8")
        self.run_cli("install-hook")
        data = json.loads(settings.read_text(encoding="utf-8"))
        self.assertEqual(len(self.ours(data)), 1)
        self.assertEqual(data["hooks"]["PreToolUse"][1]["matcher"], "Agent|Task|Workflow")
        self.assertEqual(data["hooks"]["PreToolUse"][1]["hooks"][0]["command"], handler["command"])
        self.assertEqual(data["hooks"]["PreToolUse"][2]["hooks"][0]["command"], "echo other")

        code, out, err = self.run_cli("status")
        self.assertEqual(code, 0, err)
        self.assertIn("PreToolUse matcher=Agent|Task|Workflow", out)

        code, out, err = self.run_cli("uninstall-hook")
        self.assertEqual(code, 0, err)
        self.assertIn("removed 1", out)
        data = json.loads(settings.read_text(encoding="utf-8"))
        expected = self.foreign_settings()
        expected["hooks"]["PreToolUse"].append({"matcher": "Edit", "hooks": [{"type": "command", "command": "echo other"}]})
        self.assertEqual(data, expected)
        before = settings.read_bytes()
        code, out, _ = self.run_cli("uninstall-hook")
        self.assertIn("removed 0", out)
        self.assertEqual(settings.read_bytes(), before)

    def test_install_into_missing_settings_and_shared_group(self):
        code, out, err = self.run_cli("install-hook")
        self.assertEqual(code, 0, err)
        data = json.loads((self.tmp / "settings.json").read_text(encoding="utf-8"))
        self.assertEqual(list(data["hooks"]), ["PreToolUse"])
        self.assertFalse((self.tmp / "settings.json.agent-guard.bak").exists())
        # ours sharing a group with a foreign handler: the foreign one stays, ours gets its own group
        mixed = {"hooks": {"PreToolUse": [{"matcher": "Agent", "hooks": [
            {"type": "command", "command": "python3 /x/agent_guard.py hook pre-tool-use"},
            {"type": "command", "command": "python3 /x/beyin_v3_bridge.py"}]}]}}
        (self.tmp / "settings.json").write_text(json.dumps(mixed), encoding="utf-8")
        self.run_cli("install-hook")
        data = json.loads((self.tmp / "settings.json").read_text(encoding="utf-8"))
        groups = data["hooks"]["PreToolUse"]
        self.assertEqual(groups[0], {"matcher": "Agent", "hooks": [
            {"type": "command", "command": "python3 /x/beyin_v3_bridge.py"}]})
        self.assertEqual(len(self.ours(data)), 1)
        self.assertEqual(groups[1]["matcher"], "Agent|Task|Workflow")

    def test_end_to_end_installed_command(self):
        """Install through the launcher, then pipe a payload into the exact command it wrote."""
        if not agent_guard.installed_script().is_file():
            self.skipTest("skill not linked into ~/.claude/skills yet (run install.sh)")
        bash = shutil.which("bash")
        if bash and LAUNCHER.is_file():
            done = subprocess.run([bash, str(LAUNCHER), "install-hook"], stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, env=self.env, timeout=60)
        else:
            done = subprocess.run([sys.executable, str(SCRIPT), "install-hook"], stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, env=self.env, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        data = json.loads((self.tmp / "settings.json").read_text(encoding="utf-8"))
        command = self.ours(data)[0][2]["command"]
        t = self.transcript(prompt("kodu düzelt"), assistant("claude-fable-5-2"))
        shells = [None] + ([bash] if bash else [])  # the platform shell, and bash (Claude Code's on Windows)
        for shell in shells:
            for payload, denied in ((self.agent(t, subagent_type="coder", model="fable"), True),
                                    (self.agent(t, subagent_type="coder", model="sonnet"), False)):
                argv = [shell, "-c", command] if shell else command
                done = subprocess.run(argv, shell=shell is None, input=json.dumps(payload).encode("utf-8"),
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self.env, timeout=60)
                with self.subTest(shell=shell, denied=denied):
                    self.assertEqual(done.returncode, 0, done.stderr)
                    out = done.stdout.decode("utf-8")
                    if denied:
                        self.assertDenied(out)
                    else:
                        self.assertEqual(out.strip(), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
