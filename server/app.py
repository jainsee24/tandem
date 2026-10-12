"""FastAPI wiring: routes, websockets, lifecycle."""
import asyncio
import logging
import os
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import auth
from . import transcript
from .agent import TaskRunner
from .chrome import ChromeManager
from .cdp import SCREENCAST_TIERS, CDPClient

load_dotenv()
logging.basicConfig(level=getattr(logging, __import__("os").environ.get("LOG_LEVEL", "INFO")))
# the agent tab-follow poll hits chrome's /json every 0.6s; silence per-request noise
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("app")

REPO_ROOT = Path(__file__).resolve().parent.parent
STATIC = REPO_ROOT / "static"
# files attached in the chat composer / chosen for a file input. Must live
# under $HOME: the snap-confined chromium reads File objects from /tmp as empty.
UPLOADS = REPO_ROOT / "uploads"
MAX_UPLOAD_BYTES = 50 * 1024 * 1024      # per file

chrome = ChromeManager(
    headless=os.environ.get("HEADLESS", "0") == "1",
    chrome_bin=os.environ.get("CHROME_BIN", "/snap/bin/chromium"),
    profile_dir=Path(os.environ.get("CHROME_PROFILE_DIR") or (REPO_ROOT / "chrome-profile")),
    xvfb_display=os.environ.get("XVFB_DISPLAY", ":99"),
)
cdp = CDPClient()
runner = TaskRunner(timeout_min=int(os.environ.get("TASK_TIMEOUT_MIN", "30")))

agent_clients: set[WebSocket] = set()


class ScreenClient:
    """A connected viewer. Holds only the LATEST frame; a dedicated writer task
    sends it and drops any frames produced while a send is outstanding. This
    coalescing is what keeps the view real-time instead of accumulating lag.

    Flow control: the browser acks every frame it has painted, and at most one
    frame is in flight per viewer (with a safety timeout). Without the ack,
    `send_bytes` returns as soon as the bytes hit the kernel socket buffer,
    so on a phone or through a tunnel megabytes of stale JPEG queued up before
    any backpressure appeared — that was the "browser feels slow" lag.
    The send→ack round trip also drives the adaptive screencast tier."""

    ACK_TIMEOUT = 1.5          # s; a lost ack must not freeze the stream
    SLOW_RTT, FAST_RTT = 0.25, 0.10   # s; hysteresis for stepping tiers
    SLOW_N, FAST_N = 5, 40            # consecutive frames before stepping

    def __init__(self, ws: WebSocket):
        self.ws = ws
        self._latest: bytes | None = None
        self._event = asyncio.Event()
        self._ack = asyncio.Event()
        self._alive = True
        self.watching = True       # false while the viewer's tab is hidden
        self._sent_at = 0.0
        self._slow = 0
        self._fast = 0
        self.want_tier = 0         # this viewer's preferred screencast tier

    def push_frame(self, frame: bytes):
        self._latest = frame
        self._event.set()

    def acked(self):
        rtt = time.monotonic() - self._sent_at
        self._ack.set()
        if rtt > self.SLOW_RTT:
            self._slow += 1
            self._fast = 0
            if self._slow >= self.SLOW_N:
                self._slow = 0
                self.want_tier = min(self.want_tier + 1, len(SCREENCAST_TIERS) - 1)
                _retune_tier()
        elif rtt < self.FAST_RTT:
            self._fast += 1
            self._slow = 0
            if self._fast >= self.FAST_N:
                self._fast = 0
                if self.want_tier > 0:
                    self.want_tier -= 1
                    _retune_tier()
        else:
            self._slow = self._fast = 0

    async def writer(self):
        while self._alive:
            await self._event.wait()
            self._event.clear()
            frame, self._latest = self._latest, None
            if frame is None:
                continue
            self._ack.clear()
            self._sent_at = time.monotonic()
            await self.ws.send_bytes(frame)
            try:
                await asyncio.wait_for(self._ack.wait(), timeout=self.ACK_TIMEOUT)
            except asyncio.TimeoutError:
                pass

    async def send_json(self, payload: dict):
        await self.ws.send_json(payload)


screen_clients: set[ScreenClient] = set()


def _broadcast_frame(frame: bytes):
    for c in screen_clients:
        if c.watching:
            c.push_frame(frame)


def _retune_tier():
    """Screencast params are per page, so the slowest watching viewer wins."""
    want = max((c.want_tier for c in screen_clients if c.watching), default=0)
    if want != cdp.tier:
        asyncio.create_task(cdp.set_tier(want))


def _broadcast_selection(text: str):
    _broadcast_tabs({"type": "selection", "text": text})


def _viewport_payload(vp=None) -> dict:
    w, h = vp or cdp.viewport
    return {"type": "viewport", "w": w, "h": h}


def _broadcast_viewport(vp):
    _broadcast_tabs(_viewport_payload(vp))


def _broadcast_dialog(payload: dict):
    _broadcast_tabs(payload)


def _broadcast_loading(state: str):
    _broadcast_tabs({"type": "loading", "state": state})


def _broadcast_tabs(payload: dict):
    for c in list(screen_clients):
        asyncio.create_task(_send_or_drop_screen(c, payload))


async def _send_or_drop_screen(c: "ScreenClient", payload: dict):
    try:
        await c.send_json(payload)
    except Exception:
        screen_clients.discard(c)


def _broadcast(clients: set[WebSocket], payload: dict):
    for ws in list(clients):
        asyncio.create_task(_send_or_drop(ws, clients, payload))


async def _send_or_drop(ws: WebSocket, clients: set[WebSocket], payload: dict):
    try:
        await ws.send_json(payload)
    except Exception:
        clients.discard(ws)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await chrome.start()
    await cdp.connect()
    cdp.on_frame = _broadcast_frame
    cdp.on_tabs = _broadcast_tabs
    cdp.on_selection = _broadcast_selection
    cdp.on_viewport = _broadcast_viewport
    cdp.on_dialog = _broadcast_dialog
    cdp.on_loading = _broadcast_loading
    chrome.on_restart = cdp.reconnect
    runner.on_event = lambda tid, ev: _broadcast(agent_clients, {"type": "event", "taskId": tid, "event": ev})
    runner.on_state = lambda: _broadcast(agent_clients, _task_state())
    watchdog = asyncio.create_task(chrome.watchdog())
    follow = asyncio.create_task(_follow_agent_tabs())
    yield
    watchdog.cancel()
    follow.cancel()
    await runner.stop()
    await cdp.close()
    await chrome.stop()


def _task_state() -> dict:
    cur = runner.current
    return {
        "type": "task_state",
        "running": runner.is_alive(),                       # session is live
        "status": cur["status"] if cur else "idle",         # running | ready | …
        "task": {k: v for k, v in cur.items()
                 if k not in ("events", "prior")} if cur else None,
    }


async def _follow_agent_tabs():
    """While a task runs, keep the screencast on chrome's front-most tab,
    since the agent may activate an existing tab (no CDP event for that).
    Polled tightly so the human sees the agent's actions with minimal lag."""
    while True:
        await asyncio.sleep(0.6)
        if runner.is_busy():
            await cdp.follow_front_tab()


app = FastAPI(lifespan=lifespan)


@app.middleware("http")
async def no_cache_static(request: Request, call_next):
    resp = await call_next(request)
    # the UI is a single-user app served live from disk; never let a phone cache
    # stale JS/CSS/HTML, or fixes won't reach the browser without a manual purge
    if request.url.path == "/" or request.url.path.startswith("/static"):
        resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return resp


def authed(request: Request) -> bool:
    return auth.cookie_valid(request.cookies.get(auth.COOKIE))


@app.get("/login")
async def login_page():
    return HTMLResponse(auth.LOGIN_PAGE.format(error=""))


@app.post("/login")
async def login(request: Request):
    form = await request.form()
    if auth.check_password(str(form.get("password", ""))):
        resp = RedirectResponse("/", status_code=303)
        resp.set_cookie(
            auth.COOKIE, auth.make_cookie(), max_age=auth.MAX_AGE,
            httponly=True, samesite="lax",
        )
        return resp
    return HTMLResponse(auth.LOGIN_PAGE.format(error="wrong password"), status_code=401)


@app.get("/")
async def index(request: Request):
    if not authed(request):
        return RedirectResponse("/login", status_code=307)
    return FileResponse(STATIC / "index.html")


def _safe_name(name: str) -> str:
    """Basename only, filesystem-safe, never empty."""
    name = os.path.basename(name or "")
    name = re.sub(r"[^\w.\- ]+", "_", name).strip() or "file"
    return name[:120]


def _valid_attachments(paths) -> list[str]:
    """Keep only paths that are real files inside UPLOADS — the client sends
    back the paths /api/uploads returned, and must not be able to point the
    agent at arbitrary files."""
    out = []
    for p in paths if isinstance(paths, list) else []:
        try:
            rp = Path(str(p)).resolve()
        except (OSError, ValueError):
            continue
        if rp.is_file() and rp.is_relative_to(UPLOADS):
            out.append(str(rp))
    return out


@app.post("/api/uploads")
async def upload_files(request: Request):
    """Receive composer attachments (multipart) and stash them under uploads/;
    returns the saved paths, which the client echoes back on /api/tasks(/say)."""
    if not authed(request):
        return RedirectResponse("/login", status_code=307)
    form = await request.form()
    files = form.getlist("files")
    if not files:
        return JSONResponse({"error": "no files"}, status_code=400)
    batch = UPLOADS / f"{int(time.time() * 1000)}"
    batch.mkdir(parents=True, exist_ok=True)
    saved = []
    for f in files:
        if isinstance(f, str):        # a plain form field, not a file part
            continue
        name = _safe_name(f.filename)
        dest = batch / name
        n = 1
        while dest.exists():          # same name attached twice in one batch
            dest = batch / f"{n}-{name}"
            n += 1
        size = 0
        with dest.open("wb") as out:
            while chunk := await f.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_UPLOAD_BYTES:
                    out.close()
                    dest.unlink(missing_ok=True)
                    return JSONResponse(
                        {"error": f"{name} exceeds {MAX_UPLOAD_BYTES // (1024*1024)} MB"},
                        status_code=413)
                out.write(chunk)
        saved.append({"name": name, "path": str(dest)})
    if not saved:
        return JSONResponse({"error": "no files"}, status_code=400)
    return {"files": saved}


@app.post("/api/tasks")
async def start_task(request: Request):
    if not authed(request):
        return RedirectResponse("/login", status_code=307)
    body = await request.json()
    instruction = str(body.get("instruction", "")).strip()
    if not instruction:
        return JSONResponse({"error": "empty instruction"}, status_code=400)
    # optional: resume a prior Claude conversation instead of starting fresh
    resume_id = str(body.get("resume", "")).strip() or None
    # optional: pick the Claude model (alias, id, or "id|effort") and effort
    model = str(body.get("model", "")).strip() or None
    effort = str(body.get("effort", "")).strip() or None
    # optional: files previously stored via /api/uploads
    attachments = _valid_attachments(body.get("attachments"))
    # only the Tandem UI marks itself "human"; the portal warmer and booking
    # triggers post without it and land in the automation retention bucket
    origin = "human" if str(body.get("origin", "")).strip() == "human" else "auto"
    if resume_id and not transcript.exists(REPO_ROOT, resume_id):
        return JSONResponse({"error": "that conversation is no longer on disk"},
                            status_code=404)
    # start() ends any existing session and begins a fresh (or resumed) one
    task = await runner.start(instruction, resume_id=resume_id, model=model,
                              effort=effort, attachments=attachments, origin=origin)
    return {"id": task["id"], "session_id": task.get("session_id")}


@app.post("/api/tasks/say")
async def say_task(request: Request):
    """Send a follow-up instruction into the live session (mid-session steering)."""
    if not authed(request):
        return RedirectResponse("/login", status_code=307)
    body = await request.json()
    text = str(body.get("text", "")).strip()
    if not text:
        return JSONResponse({"error": "empty message"}, status_code=400)
    attachments = _valid_attachments(body.get("attachments"))
    ok = await runner.say(text, attachments=attachments)
    if not ok:
        return JSONResponse({"error": "no live session"}, status_code=409)
    return {"ok": True}


@app.post("/api/tasks/stop")
async def stop_task(request: Request):
    if not authed(request):
        return RedirectResponse("/login", status_code=307)
    await runner.stop()
    return {"ok": True}


@app.get("/api/tasks")
async def list_tasks(request: Request):
    if not authed(request):
        return RedirectResponse("/login", status_code=307)
    hist = runner.history()
    for r in hist:                       # can this row still be continued?
        r["resumable"] = bool(r.get("session_id")
                              and transcript.exists(REPO_ROOT, r["session_id"]))
    last = runner.last_chat()
    return {
        "history": hist,
        "current": _task_state()["task"],
        # what the UI offers to continue when it opens with no live session
        "last_chat": {
            "session_id": last["session_id"],
            "title": last.get("title") or last.get("instruction") or "",
            "last_active": last.get("last_active") or last.get("started"),
            "origin": last.get("origin") or "auto",
        } if last and transcript.exists(REPO_ROOT, last["session_id"]) else None,
    }


@app.get("/api/chats/{session_id}/transcript")
async def chat_transcript(session_id: str, request: Request):
    """Replay a past conversation for the feed. The events come from claude's
    own session log, so reopening a chat shows what was actually said in it."""
    if not authed(request):
        return RedirectResponse("/login", status_code=307)
    if not transcript.exists(REPO_ROOT, session_id):
        return JSONResponse({"error": "no transcript for that conversation"},
                            status_code=404)
    return {
        "session_id": session_id,
        "title": transcript.first_user_text(REPO_ROOT, session_id),
        "events": transcript.events(REPO_ROOT, session_id),
    }


app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.websocket("/ws/screen")
async def ws_screen(ws: WebSocket):
    await ws.accept()
    if not auth.cookie_valid(ws.cookies.get(auth.COOKIE)):
        # accept-then-close so the client sees code 4401 and redirects to login
        await ws.close(code=4401)
        return
    client = ScreenClient(ws)
    screen_clients.add(client)
    await cdp.add_viewer()
    writer = asyncio.create_task(client.writer())
    try:
        await ws.send_json(cdp.tabs_payload())
        await ws.send_json(_viewport_payload())
        for d in list(cdp.open_dialogs.values()):   # a dialog already waiting
            await ws.send_json(d)
        if cdp.page and cdp.page.loading:
            await ws.send_json({"type": "loading", "state": "start"})
        while True:
            msg = await ws.receive_json()
            t = msg.get("type")
            if t == "ack":                      # frame painted; flow control
                client.acked()
            elif t == "view":                   # tab hidden/shown on the viewer
                on = bool(msg.get("on", True))
                if on != client.watching:
                    client.watching = on
                    if on:
                        await cdp.add_viewer()
                    else:
                        await cdp.remove_viewer()
                    _retune_tier()
            elif t == "file.reply":
                msg["paths"] = _valid_attachments(msg.get("paths"))
                await cdp.handle_ui_message(msg)
            else:
                await cdp.handle_ui_message(msg)
    except WebSocketDisconnect:
        pass
    except Exception:
        log.exception("screen ws error")
    finally:
        client._alive = False
        writer.cancel()
        screen_clients.discard(client)
        if client.watching:
            await cdp.remove_viewer()
        _retune_tier()


@app.websocket("/ws/agent")
async def ws_agent(ws: WebSocket):
    await ws.accept()
    if not auth.cookie_valid(ws.cookies.get(auth.COOKIE)):
        await ws.close(code=4401)
        return
    agent_clients.add(ws)
    try:
        await ws.send_json(_task_state())
        if runner.current:
            tid = runner.current["id"]
            # a resumed session replays the chat it continued first, so a page
            # reload rebuilds the whole conversation and not just this turn
            for ev in runner.current.get("prior") or []:
                await ws.send_json({"type": "event", "taskId": tid, "event": ev})
            for ev in runner.current["events"][-200:]:
                await ws.send_json({"type": "event", "taskId": tid, "event": ev})
        while True:
            await ws.receive_text()  # keepalive only; clients don't send
    except WebSocketDisconnect:
        pass
    finally:
        agent_clients.discard(ws)
