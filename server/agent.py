"""Claude Code session runner.

Runs `claude -p` as a *persistent* streaming session (`--input-format stream-json`)
so the user can keep talking to the agent in the same chat: the first instruction
starts the session, and follow-ups are fed into the live process as new user turns
mid-session, retaining full conversation + browser context. The session stays open
(status "ready") between turns until the user stops it or it goes idle past the
timeout.
"""
import asyncio
import json
import logging
import os
import re
import signal
import time
from pathlib import Path

log = logging.getLogger("agent")

REPO_ROOT = Path(__file__).resolve().parent.parent
TASKS_FILE = REPO_ROOT / "tasks.json"
OPERATOR_MD = REPO_ROOT / ".claude" / "agents" / "browser-operator.md"
HISTORY_LIMIT = 100


def _operator_rules() -> str:
    text = OPERATOR_MD.read_text()
    return re.sub(r"\A---\n.*?\n---\n", "", text, flags=re.DOTALL).strip()


def _tool_summary(name: str, inp: dict) -> str:
    for key in ("url", "element", "text", "ref", "command", "file_path", "prompt", "expression"):
        if key in inp and isinstance(inp[key], str) and inp[key]:
            return inp[key][:120]
    s = json.dumps(inp)
    return s[:120] if s != "{}" else ""


def _user_message(text: str) -> bytes:
    return (json.dumps({
        "type": "user",
        "message": {"role": "user", "content": [{"type": "text", "text": text}]},
    }) + "\n").encode()


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

    async def start(self, instruction: str) -> dict:
        """Begin a fresh session (ending any existing one first)."""
        if self.is_alive():
            await self.stop()
        session = {
            "id": f"s{int(time.time() * 1000)}",
            "instruction": instruction,    # the first turn
            "status": "running",
            "started": time.time(),
            "ended": None,
            "events": [],
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
            "--mcp-config", ".mcp.json",
            "--append-system-prompt", _operator_rules(),
        ]
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

        self._emit(session, {"kind": "user", "text": instruction})
        self._emit(session, {"kind": "status", "text": "agent starting…"})
        await self._write(instruction)
        self._consumer = asyncio.create_task(self._consume(session))
        self._watchdog = asyncio.create_task(self._idle_watchdog(session))
        if self.on_state:
            self.on_state()
        return session

    async def say(self, text: str) -> bool:
        """Send a follow-up instruction into the live session."""
        if not self.is_alive() or not self.proc or self.proc.stdin is None:
            return False
        s = self.current
        s["turns"].append({"instruction": text, "result": None})
        self._emit(s, {"kind": "user", "text": text})
        s["status"] = "running"
        self._last_activity = time.monotonic()
        await self._write(text)
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
            return json.loads(TASKS_FILE.read_text())
        except json.JSONDecodeError:
            return []

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
            # turn finished, but the session stays open for follow-ups
            if session["status"] == "running":
                session["status"] = "ready"
                if self.on_state:
                    self.on_state()

    def _finalize(self, session: dict, status: str, result: str | None):
        if session["status"] in ("running", "ready"):
            session["status"] = status
        if result is not None and not session.get("result"):
            session["result"] = result
            if status == "failed":
                self._emit(session, {"kind": "result", "ok": False, "text": result})
        session["ended"] = time.time()
        self._persist(session)
        if self.on_state:
            self.on_state()

    def _persist(self, session: dict):
        record = {k: v for k, v in session.items() if k != "events"}
        hist = self.history()
        hist.insert(0, record)
        try:
            TASKS_FILE.write_text(json.dumps(hist[:HISTORY_LIMIT], indent=1))
        except OSError:
            log.exception("could not persist tasks.json")
