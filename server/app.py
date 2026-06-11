"""FastAPI wiring: routes, websockets, lifecycle."""
import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import auth
from .agent import TaskRunner
from .chrome import ChromeManager
from .cdp import CDPClient

load_dotenv()
logging.basicConfig(level=getattr(logging, __import__("os").environ.get("LOG_LEVEL", "INFO")))
# the agent tab-follow poll hits chrome's /json every 0.6s; silence per-request noise
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("app")

REPO_ROOT = Path(__file__).resolve().parent.parent
STATIC = REPO_ROOT / "static"

chrome = ChromeManager(
    headless=os.environ.get("HEADLESS", "0") == "1",
    chrome_bin=os.environ.get("CHROME_BIN", "/snap/bin/chromium"),
    profile_dir=REPO_ROOT / "chrome-profile",
    xvfb_display=os.environ.get("XVFB_DISPLAY", ":99"),
)
cdp = CDPClient()
runner = TaskRunner(timeout_min=int(os.environ.get("TASK_TIMEOUT_MIN", "30")))

agent_clients: set[WebSocket] = set()


class ScreenClient:
    """A connected viewer. Holds only the LATEST frame; a dedicated writer task
    sends it and drops any frames produced while a slow send is in flight. This
    coalescing is what keeps the view real-time instead of accumulating lag."""

    def __init__(self, ws: WebSocket):
        self.ws = ws
        self._latest: bytes | None = None
        self._event = asyncio.Event()
        self._alive = True

    def push_frame(self, frame: bytes):
        self._latest = frame
        self._event.set()

    async def writer(self):
        while self._alive:
            await self._event.wait()
            self._event.clear()
            frame, self._latest = self._latest, None
            if frame is not None:
                await self.ws.send_bytes(frame)

    async def send_json(self, payload: dict):
        await self.ws.send_json(payload)


screen_clients: set[ScreenClient] = set()


def _broadcast_frame(frame: bytes):
    for c in screen_clients:
        c.push_frame(frame)


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
        "task": {k: v for k, v in cur.items() if k != "events"} if cur else None,
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


@app.post("/api/tasks")
async def start_task(request: Request):
    if not authed(request):
        return RedirectResponse("/login", status_code=307)
    body = await request.json()
    instruction = str(body.get("instruction", "")).strip()
    if not instruction:
        return JSONResponse({"error": "empty instruction"}, status_code=400)
    # start() ends any existing session and begins a fresh one
    task = await runner.start(instruction)
    return {"id": task["id"]}


@app.post("/api/tasks/say")
async def say_task(request: Request):
    """Send a follow-up instruction into the live session (mid-session steering)."""
    if not authed(request):
        return RedirectResponse("/login", status_code=307)
    body = await request.json()
    text = str(body.get("text", "")).strip()
    if not text:
        return JSONResponse({"error": "empty message"}, status_code=400)
    ok = await runner.say(text)
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
    return {"history": runner.history(), "current": _task_state()["task"]}


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
        while True:
            msg = await ws.receive_json()
            await cdp.handle_ui_message(msg)
    except WebSocketDisconnect:
        pass
    except Exception:
        log.exception("screen ws error")
    finally:
        client._alive = False
        writer.cancel()
        screen_clients.discard(client)
        await cdp.remove_viewer()


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
            for ev in runner.current["events"][-200:]:
                await ws.send_json({"type": "event", "taskId": runner.current["id"], "event": ev})
        while True:
            await ws.receive_text()  # keepalive only; clients don't send
    except WebSocketDisconnect:
        pass
    finally:
        agent_clients.discard(ws)
