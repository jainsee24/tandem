"""CDP client: screencast streaming, user input injection, tab management.

Architecture note: a browser-level websocket handles target discovery and
tab create/close/activate. The active tab gets its own page-level websocket
(PageSession) for screencast/input/navigation — page-level sockets survive
cross-process navigations. Chrome (at least 138 headless=new) silently drops
the *response* of any command that triggers a cross-process swap, so commands
whose result we don't need are sent fire-and-forget (wait=False).
"""
import asyncio
import base64
import itertools
import json
import logging
import os
import re
import time
from urllib.parse import quote_plus

import httpx
import websockets

from .chrome import CDP_HTTP, CDP_PORT

log = logging.getLogger("cdp")

VIEWPORT_W, VIEWPORT_H = 1280, 800
# JPEG quality: lower = smaller frames = lower latency / higher fps over the wire.
# 60 is a good "feels live" tradeoff; override with SCREENCAST_QUALITY env.
SCREENCAST_QUALITY = int(os.environ.get("SCREENCAST_QUALITY", "60"))


def _is_page(info: dict) -> bool:
    return info.get("type") == "page" and not info.get("url", "").startswith(
        ("devtools://", "chrome-extension://")
    )


class PageSession:
    """Dedicated websocket to one tab."""

    def __init__(self, target_id: str, on_frame):
        self.target_id = target_id
        self.on_frame = on_frame
        self.ws = None
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future] = {}
        self._reader_task = None
        self.screencasting = False
        self.closed = False
        self.last_frame = 0.0

    async def open(self):
        url = f"ws://localhost:{CDP_PORT}/devtools/page/{self.target_id}"
        self.ws = await websockets.connect(url, max_size=32 * 1024 * 1024)
        self._reader_task = asyncio.create_task(self._reader())
        await self.cmd("Page.enable")
        await self.cmd(
            "Emulation.setDeviceMetricsOverride",
            {"width": VIEWPORT_W, "height": VIEWPORT_H, "deviceScaleFactor": 1, "mobile": False},
        )

    async def close(self):
        self.closed = True
        if self._reader_task:
            self._reader_task.cancel()
        if self.ws:
            try:
                await self.ws.close()
            except Exception:
                pass

    async def cmd(self, method: str, params: dict | None = None, wait: bool = True, timeout: float = 10):
        mid = next(self._ids)
        msg = {"id": mid, "method": method, "params": params or {}}
        if not wait:
            await self.ws.send(json.dumps(msg))
            return {}
        fut = asyncio.get_event_loop().create_future()
        self._pending[mid] = fut
        await self.ws.send(json.dumps(msg))
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        finally:
            self._pending.pop(mid, None)

    async def _reader(self):
        try:
            async for raw in self.ws:
                msg = json.loads(raw)
                if "id" in msg:
                    fut = self._pending.get(msg["id"])
                    if fut and not fut.done():
                        if "error" in msg:
                            fut.set_exception(RuntimeError(str(msg["error"])))
                        else:
                            fut.set_result(msg.get("result", {}))
                elif msg.get("method") == "Page.frameNavigated":
                    # a cross-process swap silently kills an active screencast;
                    # re-arm it (idempotent) on every top-frame navigation
                    if self.screencasting and not msg["params"]["frame"].get("parentId"):
                        self.screencasting = False
                        await self.start_screencast()
                elif msg.get("method") == "Page.screencastFrame":
                    params = msg["params"]
                    self.last_frame = time.monotonic()
                    # ack FIRST so chrome can start rendering the next frame while
                    # we decode/broadcast this one — keeps the pipeline full
                    await self.ws.send(json.dumps({
                        "id": next(self._ids),
                        "method": "Page.screencastFrameAck",
                        "params": {"sessionId": params["sessionId"]},
                    }))
                    if self.on_frame:
                        # decode base64 once, here; clients get raw JPEG bytes
                        self.on_frame(base64.b64decode(params["data"]))
        except (websockets.ConnectionClosed, asyncio.CancelledError):
            pass
        except Exception:
            log.exception("page session reader crashed (%s)", self.target_id)

    async def start_screencast(self):
        if self.screencasting:
            return
        self.screencasting = True
        self.last_frame = time.monotonic()
        await self.cmd(
            "Page.startScreencast",
            {
                "format": "jpeg",
                "quality": SCREENCAST_QUALITY,
                "maxWidth": VIEWPORT_W,
                "maxHeight": VIEWPORT_H,
                "everyNthFrame": 1,
            },
            wait=False,
        )

    async def stop_screencast(self):
        if not self.screencasting:
            return
        self.screencasting = False
        try:
            await self.cmd("Page.stopScreencast", wait=False)
        except Exception:
            pass


class CDPClient:
    def __init__(self):
        self.ws = None  # browser-level socket: target discovery + tab ops
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future] = {}
        self._reader_task = None
        self._event_task = None
        self._events: asyncio.Queue | None = None
        self.targets: dict[str, dict] = {}  # targetId -> {"url","title"} (page targets)
        self.page: PageSession | None = None
        self.viewers = 0
        self.on_frame = None  # callback(b64_jpeg)
        self.on_tabs = None   # callback(tabs_payload)
        self._closing = False
        self._switch_lock = asyncio.Lock()
        self._health_task = None

    @property
    def active_target_id(self) -> str | None:
        return self.page.target_id if self.page else None

    # ---------- connection ----------

    async def connect(self):
        self._closing = False
        async with httpx.AsyncClient() as client:
            r = await client.get(f"{CDP_HTTP}/json/version", timeout=5)
            ws_url = r.json()["webSocketDebuggerUrl"]
        self.ws = await websockets.connect(ws_url, max_size=32 * 1024 * 1024)
        self._events = asyncio.Queue()
        self._reader_task = asyncio.create_task(self._reader())
        self._event_task = asyncio.create_task(self._event_loop())
        self.targets.clear()
        self.page = None
        await self.cmd("Target.setDiscoverTargets", {"discover": True})
        await asyncio.sleep(0.3)  # let discovery events land
        if not self.targets:
            await self.cmd("Target.createTarget", {"url": "about:blank"})
            await asyncio.sleep(0.3)
        first = next(iter(self.targets), None)
        if first:
            await self.switch_to(first)
        self._health_task = asyncio.create_task(self._screencast_health())
        log.info("cdp connected, %d page target(s)", len(self.targets))

    async def _screencast_health(self):
        """Re-arm the screencast if frames stall while someone is watching.

        Cross-process navigations (and other chrome quirks on this box) can
        silently kill an active screencast with no event we can trust; a
        re-arm is idempotent and immediately produces a fresh frame.
        """
        while True:
            await asyncio.sleep(3)
            page = self.page
            if not page or self.viewers == 0 or not page.screencasting:
                continue
            if time.monotonic() - page.last_frame > 3:
                page.screencasting = False
                try:
                    await page.start_screencast()
                except Exception:
                    pass

    async def close(self):
        self._closing = True
        if self.page:
            await self.page.close()
            self.page = None
        if self._reader_task:
            self._reader_task.cancel()
        if self._event_task:
            self._event_task.cancel()
        if self._health_task:
            self._health_task.cancel()
        if self.ws:
            try:
                await self.ws.close()
            except Exception:
                pass

    async def reconnect(self):
        """Full reconnect after chrome restart or socket drop."""
        try:
            await self.close()
        except Exception:
            pass
        for attempt in range(30):
            try:
                await self.connect()
                self._push_tabs()
                if self.viewers > 0 and self.page:
                    await self.page.start_screencast()
                return
            except Exception as e:
                log.warning("cdp reconnect attempt %d failed: %s", attempt + 1, e)
                await asyncio.sleep(1)
        log.error("cdp reconnect gave up after 30 attempts")

    async def cmd(self, method: str, params: dict | None = None, wait: bool = True) -> dict:
        mid = next(self._ids)
        msg = {"id": mid, "method": method, "params": params or {}}
        if not wait:
            await self.ws.send(json.dumps(msg))
            return {}
        fut = asyncio.get_event_loop().create_future()
        self._pending[mid] = fut
        await self.ws.send(json.dumps(msg))
        try:
            return await asyncio.wait_for(fut, timeout=10)
        finally:
            self._pending.pop(mid, None)

    async def _reader(self):
        try:
            async for raw in self.ws:
                msg = json.loads(raw)
                if "id" in msg:
                    fut = self._pending.get(msg["id"])
                    if fut and not fut.done():
                        if "error" in msg:
                            fut.set_exception(RuntimeError(str(msg["error"])))
                        else:
                            fut.set_result(msg.get("result", {}))
                else:
                    # never await event handling here: handlers issue cdp commands
                    # whose responses this loop must stay free to receive
                    self._events.put_nowait(msg)
        except (websockets.ConnectionClosed, asyncio.CancelledError):
            pass
        except Exception:
            log.exception("cdp reader crashed")
        if not self._closing:
            log.warning("cdp socket dropped, reconnecting")
            asyncio.create_task(self.reconnect())

    # ---------- events ----------

    async def _event_loop(self):
        while True:
            msg = await self._events.get()
            try:
                await self._on_event(msg)
            except asyncio.CancelledError:
                return
            except Exception:
                log.exception("event handler failed: %s", msg.get("method"))

    async def _on_event(self, msg: dict):
        method = msg.get("method")
        params = msg.get("params", {})

        if method == "Target.targetCreated":
            info = params["targetInfo"]
            if _is_page(info):
                is_new = info["targetId"] not in self.targets
                self.targets[info["targetId"]] = {"url": info["url"], "title": info["title"]}
                self._push_tabs()
                if is_new and self.page is not None:
                    # follow newly opened tabs (incl. ones the agent opens)
                    await self.switch_to(info["targetId"])

        elif method == "Target.targetInfoChanged":
            info = params["targetInfo"]
            if _is_page(info) and info["targetId"] in self.targets:
                self.targets[info["targetId"]] = {"url": info["url"], "title": info["title"]}
                self._push_tabs()

        elif method == "Target.targetDestroyed":
            tid = params["targetId"]
            if tid in self.targets:
                del self.targets[tid]
                self._push_tabs()
                if self.page and tid == self.page.target_id:
                    await self.page.close()
                    self.page = None
                    nxt = next(iter(self.targets), None)
                    if nxt:
                        await self.switch_to(nxt)
                    else:
                        await self.cmd("Target.createTarget", {"url": "about:blank"})
                        # targetCreated handler will switch to it... unless page is None
                        await asyncio.sleep(0.3)
                        nxt = next(iter(self.targets), None)
                        if nxt:
                            await self.switch_to(nxt)

    def _push_tabs(self):
        if self.on_tabs:
            self.on_tabs(self.tabs_payload())

    def tabs_payload(self) -> dict:
        return {
            "type": "tabs",
            "tabs": [
                {"targetId": tid, "url": t["url"], "title": t["title"]}
                for tid, t in self.targets.items()
            ],
            "activeTargetId": self.active_target_id,
        }

    # ---------- tab switching / screencast ----------

    async def switch_to(self, target_id: str):
        async with self._switch_lock:
            if target_id not in self.targets:
                return
            if self.page and self.page.target_id == target_id:
                return
            if self.page:
                await self.page.close()
                self.page = None
            page = PageSession(target_id, lambda b64: self.on_frame and self.on_frame(b64))
            try:
                await page.open()
            except Exception:
                # one retry: a fresh socket usually recovers a half-dead agent
                await page.close()
                page = PageSession(target_id, lambda b64: self.on_frame and self.on_frame(b64))
                await page.open()
            self.page = page
            try:
                await self.cmd("Target.activateTarget", {"targetId": target_id})
            except Exception:
                pass
            if self.viewers > 0:
                await page.start_screencast()
            self._push_tabs()

    async def add_viewer(self):
        self.viewers += 1
        if self.viewers == 1 and self.page:
            try:
                await self.page.start_screencast()
            except Exception:
                log.exception("start screencast failed")

    async def remove_viewer(self):
        self.viewers = max(0, self.viewers - 1)
        if self.viewers == 0 and self.page:
            await self.page.stop_screencast()

    # ---------- UI message handling ----------

    async def handle_ui_message(self, msg: dict):
        try:
            await self._handle_ui_message(msg)
        except Exception as e:
            log.warning("ui message %s failed: %r", msg.get("type"), e)

    async def _handle_ui_message(self, msg: dict):
        t = msg.get("type")
        page = self.page
        if page is None and t not in ("tab.new",):
            return

        if t == "mouse":
            params = {
                "type": msg["event"],
                "x": msg["x"],
                "y": msg["y"],
                "button": msg.get("button", "none"),
                "buttons": msg.get("buttons", 0),
                "clickCount": msg.get("clickCount", 0),
                "modifiers": msg.get("modifiers", 0),
            }
            if msg["event"] == "mouseWheel":
                params["deltaX"] = msg.get("deltaX", 0)
                params["deltaY"] = msg.get("deltaY", 0)
            if msg["event"] != "mouseMoved":
                log.debug("mouse %s @(%s,%s) btn=%s", msg["event"], params["x"], params["y"], params["button"])
            # wait=False: a click can trigger a cross-process navigation that
            # swallows the response
            await page.cmd("Input.dispatchMouseEvent", params, wait=False)

        elif t == "key":
            params = {
                "type": msg["event"],
                "key": msg.get("key", ""),
                "code": msg.get("code", ""),
                "windowsVirtualKeyCode": msg.get("windowsVirtualKeyCode", 0),
                "nativeVirtualKeyCode": msg.get("windowsVirtualKeyCode", 0),
                "modifiers": msg.get("modifiers", 0),
            }
            if msg.get("text"):
                params["text"] = msg["text"]
            await page.cmd("Input.dispatchKeyEvent", params, wait=False)

        elif t == "navigate":
            url = (msg.get("url") or "").strip()
            if not url:
                return
            if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", url):
                if "." in url and " " not in url:
                    url = "https://" + url
                else:
                    url = "https://www.google.com/search?q=" + quote_plus(url)
            await page.cmd("Page.navigate", {"url": url}, wait=False)
            asyncio.create_task(self._verify_navigation(page.target_id, url))

        elif t == "reload":
            await page.cmd("Page.reload", wait=False)

        elif t in ("back", "forward"):
            expr = "history.back()" if t == "back" else "history.forward()"
            await page.cmd("Runtime.evaluate", {"expression": expr}, wait=False)

        elif t == "tab.new":
            await self.cmd("Target.createTarget", {"url": "about:blank"})

        elif t == "tab.switch":
            await self.switch_to(msg["targetId"])

        elif t == "tab.close":
            await self.cmd("Target.closeTarget", {"targetId": msg["targetId"]})

    async def _verify_navigation(self, target_id: str, url: str):
        """Chrome occasionally drops a navigate on the floor; resend once."""
        await asyncio.sleep(2.5)
        current = self.targets.get(target_id, {}).get("url", "")
        if current.rstrip("/") != url.rstrip("/") and self.page and self.page.target_id == target_id:
            log.warning("navigation to %s did not take effect, retrying", url)
            try:
                await self.page.cmd("Page.navigate", {"url": url}, wait=False)
            except Exception:
                pass

    # ---------- MRU follow (agent switching to existing tabs) ----------

    async def follow_front_tab(self):
        """Poll chrome's MRU-ordered target list; switch screencast to the front tab."""
        try:
            async with httpx.AsyncClient() as client:
                r = await client.get(f"{CDP_HTTP}/json", timeout=3)
            pages = [p for p in r.json() if _is_page(p)]
            if pages and pages[0]["id"] != self.active_target_id and pages[0]["id"] in self.targets:
                await self.switch_to(pages[0]["id"])
        except Exception:
            pass
