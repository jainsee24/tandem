"""Claude Code session runner.

Runs `claude -p` as a *persistent* streaming session (`--input-format stream-json`)
so the user can keep talking to the agent in the same chat: the first instruction
starts the session, and follow-ups are fed into the live process as new user turns
mid-session, retaining full conversation + browser context. The session stays open
(status "ready") between turns until the user stops it or it goes idle past the
timeout.

A conversation is written to tasks.json as soon as it has an id and re-written
after every turn, keyed on the Claude session id, so a chat is continuable the
moment it starts and a resumed chat updates its existing row instead of forking
a new one. What was *said* in a chat is not stored here — see transcript.py.
"""
import asyncio
import json
import logging
import os
import re
import signal
import time
from pathlib import Path

from .transcript import _tool_summary, events as transcript_events, first_user_text

log = logging.getLogger("agent")

REPO_ROOT = Path(__file__).resolve().parent.parent
TASKS_FILE = Path(os.environ.get("TASKS_FILE") or (REPO_ROOT / "tasks.json"))
OPERATOR_MD = REPO_ROOT / ".claude" / "agents" / "browser-operator.md"
# Retention is per origin: agent-initiated runs (the portal warmer nudging this
# instance every couple of hours, a booking trigger firing) used to fill a single
# flat 100-slot list within a day and evict every chat the human had had, which
# made "continue our last chat" impossible. The two kinds no longer compete.
# Agent speed knobs. "Default model" in the UI used to mean the user's global
# Claude Code default — which on this box is claude-fable-5-1[1m] at effort
# high: the smartest, slowest, priciest combination, with a 1M context that
# only made long sessions slower. Opus 5 at medium effort is the fast default;
# the dropdown offers the rest. Both are env-overridable per instance.
DEFAULT_MODEL = os.environ.get("DEFAULT_MODEL", "claude-opus-5")
DEFAULT_EFFORT = os.environ.get("DEFAULT_EFFORT", "medium")
EFFORTS = ("low", "medium", "high", "xhigh", "max")
# Fast mode (Opus 5/4.8 only) is an org-level switch; when it's off Claude
# Code silently runs at standard speed, so asking for it is harmless and the
# agent speeds up by itself the day the org enables it. AGENT_FAST_MODE=0 opts out.
FAST_MODE = os.environ.get("AGENT_FAST_MODE", "1") != "0"
MODEL_ALIASES = {"opus": "claude-opus-5", "sonnet": "claude-sonnet-5",
                 "haiku": "claude-haiku-4-5", "fable": "claude-fable-5-1"}
HUMAN_LIMIT = 60
AUTO_LIMIT = 40
MAX_TURNS = 30            # per persisted record
MAX_RESULT_CHARS = 4000   # per persisted result string


def resolve_model(model: str | None, effort: str | None) -> tuple[str, str]:
    """Normalise what the UI / automation posted into (model id, effort).
    ``model`` may be an alias ("opus"), a full id, or "id|effort" as the UI's
    dropdown encodes it; an explicit ``effort`` wins over the encoded one."""
    model = (model or "").strip()
    enc_effort = None
    if "|" in model:
        model, enc_effort = model.split("|", 1)
    model = MODEL_ALIASES.get(model, model) or DEFAULT_MODEL
    effort = (effort or enc_effort or DEFAULT_EFFORT).strip().lower()
    if effort not in EFFORTS:
        effort = DEFAULT_EFFORT
    return model, effort


def _operator_rules() -> str:
    text = OPERATOR_MD.read_text()
    return re.sub(r"\A---\n.*?\n---\n", "", text, flags=re.DOTALL).strip()


def _user_message(text: str) -> bytes:
    return (json.dumps({
        "type": "user",
        "message": {"role": "user", "content": [{"type": "text", "text": text}]},
    }) + "\n").encode()


def _agent_text(text: str, attachments: list[str] | None) -> str:
    """The message actually fed to the agent: the instruction plus a note
    pointing at any files the user attached. The agent reads them itself
    (its Read tool handles text, images, and PDFs) and can upload them to
    sites via the browser file-upload tool when the task calls for it."""
    if not attachments:
        return text
    listing = "\n".join(f"- {p}" for p in attachments)
    return (
        f"{text}\n\n"
        f"The user attached the following file(s) with this message. Read them "
        f"(the Read tool handles text, images, and PDFs) and use their contents "
        f"for the task. If the task requires uploading a file to a website, "
        f"upload it from these exact paths:\n{listing}"
    )


class TaskRunner:
    """A single live agent session that the user can keep steering."""

    # status values: running (turn active) · ready (alive, awaiting input) ·
    #                 stopped · failed
    def __init__(self, timeout_min: int = 30):
        self.timeout_min = timeout_min
        self.current: dict | None = None
        self.proc: asyncio.subprocess.Process | None = None
        self.on_event = None   # callback(session_id, event)
        self.on_state = None   # callback() on any status change
        self._consumer = None
        self._watchdog = None
        self._last_activity = 0.0

    # ---------- public api ----------

    def is_alive(self) -> bool:
        return self.current is not None and self.current["status"] in ("running", "ready")

    # kept for callers that just want "can't start a fresh one silently"
    def is_busy(self) -> bool:
        return self.is_alive()

    async def start(self, instruction: str, resume_id: str | None = None,
                    model: str | None = None, effort: str | None = None,
                    attachments: list[str] | None = None,
                    origin: str = "auto") -> dict:
        """Begin a session (ending any existing one first).

        If ``resume_id`` is given, resume that prior Claude conversation
        (``claude --resume``) so the agent retains its full chat + browser
        context; ``instruction`` becomes the next turn in that conversation.
        Otherwise a fresh conversation is started.

        ``model`` is an optional Claude model alias/id (e.g. "opus", "sonnet",
        "claude-fable-5-1") passed through as ``--model``, and ``effort`` the
        reasoning effort (``--effort``); see resolve_model for the defaults.

        ``attachments`` is an optional list of absolute paths to files the
        user attached in the UI; the agent is told to read them.

        ``origin`` is "human" for the Tandem UI and "auto" for anything else
        (the portal warmer, booking triggers); it only decides which retention
        bucket the conversation lands in.
        """
        if self.is_alive():
            await self.stop()
        model, effort = resolve_model(model, effort)
        # Replaying the resumed chat gives the UI something to show: without it a
        # continued conversation opens on an empty feed and looks like it lost
        # everything, even though the agent still has the full context.
        prior = transcript_events(REPO_ROOT, resume_id) if resume_id else []
        session = {
            "id": f"s{int(time.time() * 1000)}",
            "instruction": instruction,    # the first turn
            # the chat's name in history: the opening message of the whole
            # conversation, which for a resumed chat predates this turn
            "title": (first_user_text(REPO_ROOT, resume_id) if resume_id else "") or instruction,
            "status": "running",
            "started": time.time(),
            "last_active": time.time(),
            "ended": None,
            "session_id": resume_id,       # Claude session id (--resume keeps it)
            "resumed_from": resume_id,     # which conversation this continued, if any
            "origin": origin,
            "model": model,                # claude model id
            "effort": effort,              # reasoning effort level
            "attachments": attachments or [],  # files attached to the first turn
            "events": [],
            "prior": prior,                # replayed turns from before this resume
            "turns": [],                   # [{instruction, result}]
            "result": None,                # last turn's result
        }
        self.current = session
        self._last_activity = time.monotonic()
        cmd = [
            "claude", "-p",
            "--input-format", "stream-json",
            "--output-format", "stream-json", "--verbose",
            "--dangerously-skip-permissions",
            # MCP tools are NOT governed by --tools (that flag only selects
            # BUILT-IN tools). They go through the permission allowlist, and on
            # Claude Code 2.1.x --dangerously-skip-permissions no longer auto-
            # approves an MCP tool loaded mid-session via ToolSearch — every
            # browser_* call was denied, so the agent did nothing.
            "--allowedTools", "mcp__browser",
            "--mcp-config", ".mcp.json",
            # Without this the operator ALSO inherits whatever MCP servers the
            # user's own claude config has (superpowers-chrome, gmail, …). It
            # then burns whole turns trying to drive the browser through the
            # WRONG server's tools, or through Bash, before finding ours.
            "--strict-mcp-config",
            # Lean session: no inherited user settings (35 PreToolUse hooks that
            # each spawn a shell per tool call, 5 plugins' worth of skills, the
            # global model default), and only the two built-in tools it uses —
            # Read for attachments, ToolSearch to load the browser tools.
            # Measured: the same trivial turn costs half the tokens.
            # (--bare would be leaner still but breaks OAuth; tested.)
            "--setting-sources", "",
            "--tools", "Read,ToolSearch",
            "--disable-slash-commands",
            # belt and braces should the tool list ever widen again
            "--disallowed-tools", "Bash", "Agent", "Task", "Write", "Edit", "Workflow",
            "--append-system-prompt", _operator_rules(),
            "--model", model,
            "--effort", effort,
        ]
        if FAST_MODE and "opus" in model:
            cmd += ["--settings", json.dumps({"fastMode": True})]
        if resume_id:
            cmd += ["--resume", resume_id]
        try:
            self.proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=REPO_ROOT,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
                limit=64 * 1024 * 1024,
                env={**os.environ, "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"},
            )
        except FileNotFoundError:
            self._finalize(session, "failed", "claude CLI not found on PATH")
            return session

        if resume_id:
            self._emit(session, {"kind": "status",
                                 "text": f"↩ continuing this chat "
                                         f"({len(prior)} earlier messages restored)"
                                         if prior else "↩ continuing this chat"})
        self._emit(session, {"kind": "user", "text": instruction,
                             "attachments": [Path(p).name for p in attachments or []]})
        self._emit(session, {"kind": "status",
                             "text": "resuming previous conversation…" if resume_id
                                     else "agent starting…"})
        await self._write(_agent_text(instruction, attachments))
        self._consumer = asyncio.create_task(self._consume(session))
        self._watchdog = asyncio.create_task(self._idle_watchdog(session))
        # A chat used to reach tasks.json only when it ended, so a hard restart
        # (or simply walking away from a live session) left nothing to continue.
        self._persist(session)
        if self.on_state:
            self.on_state()
        return session

    async def say(self, text: str, attachments: list[str] | None = None) -> bool:
        """Send a follow-up instruction into the live session."""
        if not self.is_alive() or not self.proc or self.proc.stdin is None:
            return False
        s = self.current
        s["turns"].append({"instruction": text, "result": None,
                           "attachments": attachments or []})
        self._emit(s, {"kind": "user", "text": text,
                       "attachments": [Path(p).name for p in attachments or []]})
        s["status"] = "running"
        self._last_activity = time.monotonic()
        await self._write(_agent_text(text, attachments))
        if self.on_state:
            self.on_state()
        return True

    async def stop(self):
        if self.proc and self.proc.returncode is None:
            try:
                if self.proc.stdin and not self.proc.stdin.is_closing():
                    self.proc.stdin.close()
            except Exception:
                pass
            try:
                os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)
            except ProcessLookupError:
                pass
        if self.current and self.current["status"] in ("running", "ready"):
            self._finalize(self.current, "stopped", self.current.get("result"))

    def history(self) -> list[dict]:
        if not TASKS_FILE.exists():
            return []
        try:
            hist = json.loads(TASKS_FILE.read_text())
        except json.JSONDecodeError:
            return []
        return hist if isinstance(hist, list) else []

    def last_chat(self) -> dict | None:
        """The conversation a returning user most likely means by "last chat":
        their most recent one, falling back to the newest of any origin on an
        instance that is only ever driven by automation."""
        hist = self.history()
        # skip only a session that is genuinely still running — the chat the
        # user just closed is precisely the one they mean by "last chat"
        live = self.current["session_id"] if (self.current and self.is_alive()) else None
        usable = [r for r in hist if r.get("session_id") and r["session_id"] != live]
        return next((r for r in usable if r.get("origin") == "human"),
                    usable[0] if usable else None)

    # ---------- internals ----------

    async def _write(self, text: str):
        try:
            self.proc.stdin.write(_user_message(text))
            await self.proc.stdin.drain()
        except Exception:
            log.exception("failed writing to agent stdin")

    def _emit(self, session: dict, event: dict):
        session["events"].append(event)
        self._last_activity = time.monotonic()
        if self.on_event:
            self.on_event(session["id"], event)

    async def _idle_watchdog(self, session: dict):
        """End the session if it sits with no activity past the timeout."""
        limit = self.timeout_min * 60
        while self.current is session and session["status"] in ("running", "ready"):
            await asyncio.sleep(15)
            if time.monotonic() - self._last_activity > limit:
                self._emit(session, {"kind": "status",
                                     "text": f"session idle > {self.timeout_min} min — closing"})
                await self.stop()
                return

    async def _consume(self, session: dict):
        assert self.proc and self.proc.stdout
        buf = b""
        try:
            while True:
                chunk = await self.proc.stdout.read(65536)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    raw, buf = buf.split(b"\n", 1)
                    self._process_line(session, raw)
        except Exception:
            log.exception("session consumer crashed")
        await self.proc.wait()
        # stdout closed → process ended
        if session["status"] in ("running", "ready"):
            status = "stopped" if self.proc.returncode in (-15, 143, 0) else "failed"
            reason = None
            if status == "failed" and self.proc.stderr:
                try:
                    reason = (await self.proc.stderr.read()).decode(errors="replace")[-400:]
                except Exception:
                    pass
            self._finalize(session, status, reason or session.get("result"))

    def _process_line(self, session: dict, raw: bytes):
        line = raw.decode(errors="replace").strip()
        if not line:
            return
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            return
        self._handle_msg(session, msg)

    def _handle_msg(self, session: dict, msg: dict):
        t = msg.get("type")
        # the session id rides on init (and every result); capture it so this
        # conversation can later be resumed via --resume
        sid = msg.get("session_id")
        if sid and session.get("session_id") != sid:
            session["session_id"] = sid
            self._persist(session)
        if t == "system" and msg.get("subtype") == "init":
            self._emit(session, {"kind": "status", "text": "agent ready"})
        elif t == "assistant":
            for block in msg.get("message", {}).get("content", []):
                if block.get("type") == "text" and block.get("text", "").strip():
                    self._emit(session, {"kind": "text", "text": block["text"]})
                elif block.get("type") == "tool_use":
                    self._emit(session, {
                        "kind": "tool",
                        "name": block.get("name", "?"),
                        "summary": _tool_summary(block.get("name", ""), block.get("input", {}) or {}),
                    })
        elif t == "result":
            ok = not msg.get("is_error", False)
            result = msg.get("result") or ""
            session["result"] = result
            if session["turns"]:
                session["turns"][-1]["result"] = result
            self._emit(session, {"kind": "result", "ok": ok, "text": result})
            session["last_active"] = time.time()
            # turn finished, but the session stays open for follow-ups
            if session["status"] == "running":
                session["status"] = "ready"
                if self.on_state:
                    self.on_state()
            # persist AFTER the status flip, or a finished turn is recorded as
            # still running and history misreports every live-but-idle chat
            self._persist(session)

    def _finalize(self, session: dict, status: str, result: str | None):
        if session["status"] in ("running", "ready"):
            session["status"] = status
        if result is not None and not session.get("result"):
            session["result"] = result
            if status == "failed":
                self._emit(session, {"kind": "result", "ok": False, "text": result})
        session["ended"] = time.time()
        session["last_active"] = time.time()
        self._persist(session)
        if self.on_state:
            self.on_state()

    def _trim(self, text):
        if isinstance(text, str) and len(text) > MAX_RESULT_CHARS:
            return text[:MAX_RESULT_CHARS] + "\n… (truncated)"
        return text

    def _record(self, session: dict) -> dict:
        """The on-disk form of a session: no feed (transcript.py replays that
        from claude's own log) and bounded in size, since this file is rewritten
        after every turn."""
        rec = {k: v for k, v in session.items() if k not in ("events", "prior")}
        rec["result"] = self._trim(rec.get("result"))
        rec["turns"] = [{**t, "result": self._trim(t.get("result"))}
                        for t in (rec.get("turns") or [])[-MAX_TURNS:]]
        return rec

    @staticmethod
    def _prune(hist: list[dict]) -> list[dict]:
        kept, seen = [], {}
        for r in hist:
            origin = r.get("origin") or "auto"
            limit = HUMAN_LIMIT if origin == "human" else AUTO_LIMIT
            if seen.get(origin, 0) < limit:
                seen[origin] = seen.get(origin, 0) + 1
                kept.append(r)
        return kept

    def _persist(self, session: dict):
        """Upsert this conversation's row. Keyed on the Claude session id, which
        ``--resume`` preserves — so continuing a chat updates the chat you
        continued rather than forking a near-duplicate row beside it."""
        record = self._record(session)
        hist = self.history()
        sid = record.get("session_id")
        idx = None
        if sid:
            idx = next((i for i, r in enumerate(hist) if r.get("session_id") == sid), None)
        if idx is None:
            idx = next((i for i, r in enumerate(hist) if r.get("id") == record["id"]), None)
        if idx is not None:
            prev = hist.pop(idx)
            # a chat keeps the identity it was born with across resumes
            record["title"] = prev.get("title") or prev.get("instruction") or record["title"]
            record["started"] = prev.get("started", record["started"])
            record["origin"] = prev.get("origin", record["origin"])
            if prev.get("id") != record["id"]:
                record["turns"] = ((prev.get("turns") or [])
                                   + record["turns"])[-MAX_TURNS:]
        hist.insert(0, record)
        try:
            TASKS_FILE.write_text(json.dumps(self._prune(hist), indent=1))
        except OSError:
            log.exception("could not persist tasks.json")
