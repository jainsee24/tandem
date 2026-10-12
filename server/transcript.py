"""Rebuild a chat feed from a Claude Code session transcript.

tasks.json deliberately does not store the per-event feed (it would be huge),
but `claude` already keeps a complete JSONL transcript of every session under
``~/.claude/projects/<cwd-slug>/<session-id>.jsonl``. That file is the source
of truth for "what was actually said in this chat", so reopening or continuing
a closed conversation reads it back instead of persisting a second copy.
"""
import json
import logging
import re
from pathlib import Path

log = logging.getLogger("transcript")

MAX_EVENTS = 300          # keep the tail; a long chat would swamp the panel
COLLAPSE_RUN = 2          # tool runs longer than this collapse to one line
ATTACH_NOTE = "\n\nThe user attached the following file(s) with this message."


def _tool_summary(name: str, inp: dict) -> str:
    for key in ("url", "element", "text", "ref", "command", "file_path", "prompt", "expression"):
        if key in inp and isinstance(inp[key], str) and inp[key]:
            return inp[key][:120]
    s = json.dumps(inp)
    return s[:120] if s != "{}" else ""


def project_dir(cwd: Path) -> Path:
    """Where claude keeps transcripts for sessions started in ``cwd``. It slugs
    the absolute path, turning every non-alphanumeric character into '-'."""
    return Path.home() / ".claude" / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(cwd))


def path_for(cwd: Path, session_id: str) -> Path | None:
    """The transcript file for ``session_id``, or None if there isn't one.
    The id is pattern-checked because it reaches us from an HTTP path."""
    if not session_id or not re.fullmatch(r"[0-9a-fA-F-]{8,64}", session_id):
        return None
    p = project_dir(cwd) / f"{session_id}.jsonl"
    return p if p.is_file() else None


def exists(cwd: Path, session_id: str) -> bool:
    return path_for(cwd, session_id) is not None


def _raw_events(cwd: Path, session_id: str) -> list[dict]:
    p = path_for(cwd, session_id)
    if p is None:
        return []
    out: list[dict] = []
    try:
        with p.open() as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                out.extend(_row_events(row))
    except OSError:
        log.exception("could not read transcript %s", p)
        return []
    return out


def _collapse_tools(evs: list[dict]) -> list[dict]:
    """A replayed chat is for reading, not for watching: a long run of browser
    actions becomes one line so the conversation itself stays legible."""
    out, run = [], []

    def flush():
        if len(run) > COLLAPSE_RUN:
            out.append({"kind": "status",
                        "text": f"⋯ {len(run)} browser actions"})
        else:
            out.extend(run)
        run.clear()

    for ev in evs:
        if ev["kind"] == "tool":
            run.append(ev)
            continue
        flush()
        out.append(ev)
    flush()
    return out


def events(cwd: Path, session_id: str) -> list[dict]:
    """Replay a past conversation as feed events (same shape the live session
    emits), newest MAX_EVENTS kept. Returns [] when there is no transcript."""
    out = _collapse_tools(_raw_events(cwd, session_id))
    if len(out) > MAX_EVENTS:
        out = ([{"kind": "status", "text": f"… {len(out) - MAX_EVENTS} earlier "
                                           f"messages in this chat not shown"}]
               + out[-MAX_EVENTS:])
    return out


def _row_events(row: dict) -> list[dict]:
    kind = row.get("type")
    # transcripts also carry attachment/queue/latch bookkeeping rows — skip them
    if kind not in ("user", "assistant") or row.get("isSidechain") or row.get("isMeta"):
        return []
    content = (row.get("message") or {}).get("content")
    if kind == "user":
        # a user row carrying toolUseResult is a tool result being fed back to
        # the model, not something the human typed
        if row.get("toolUseResult") is not None:
            return []
        text = content if isinstance(content, str) else "\n".join(
            b.get("text", "") for b in content or [] if b.get("type") == "text")
        text = text.split(ATTACH_NOTE)[0].strip()
        return [{"kind": "user", "text": text}] if text else []
    evs = []
    for b in content or []:
        if b.get("type") == "text" and b.get("text", "").strip():
            evs.append({"kind": "text", "text": b["text"]})
        elif b.get("type") == "tool_use":
            evs.append({"kind": "tool", "name": b.get("name", "?"),
                        "summary": _tool_summary(b.get("name", ""), b.get("input", {}) or {})})
    return evs


def first_user_text(cwd: Path, session_id: str) -> str:
    """The opening human message — used as a chat's title in the history list."""
    for ev in _raw_events(cwd, session_id):
        if ev["kind"] == "user":
            return ev["text"]
    return ""
