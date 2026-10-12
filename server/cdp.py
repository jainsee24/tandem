"""CDP client: screencast streaming, user input injection, tab management,
and the per-target stealth session.

Architecture note: a browser-level websocket handles target discovery, tab
create/close/activate and — with `Target.setAutoAttach` (flatten) — a
long-lived "stealth" session on EVERY page and out-of-process iframe, through
which the anti-fingerprint overrides and init scripts are applied before a
document's first script runs. The active tab additionally gets its own
page-level websocket (PageSession) for screencast/input/navigation — page-level
sockets survive cross-process navigations. Chrome silently drops the
*response* of any command that triggers a cross-process swap, so commands
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
from pathlib import Path
from urllib.parse import quote_plus

import httpx
import websockets

from .chrome import CDP_HTTP, CDP_PORT, SCREEN_H, SCREEN_W, VIEWPORT_H, VIEWPORT_W

log = logging.getLogger("cdp")

# Screencast tiers, best first. The server steps a page down a tier when a
# viewer's frame acks lag (phone on a bad link) and back up when they recover;
# the canvas keeps its 1280×800 coordinate space and scales smaller frames.
# Tier 0's quality is overridable with SCREENCAST_QUALITY.
SCREENCAST_TIERS = [
    (int(os.environ.get("SCREENCAST_QUALITY", "60")), VIEWPORT_W, VIEWPORT_H),
    (45, 1024, 640),
    (35, 800, 500),
]


def _is_page(info: dict) -> bool:
    return info.get("type") == "page" and not info.get("url", "").startswith(
        ("devtools://", "chrome-extension://")
    )


def _asset(name: str) -> str:
    try:
        return (Path(__file__).resolve().parent.parent / name).read_text()
    except OSError:
        log.warning("init script %s missing", name)
        return ""


# anti-bot fingerprint patches + the <select> shim, applied to every page and
# iframe target through the stealth session (single injection path — the
# Playwright MCP no longer injects them separately, so nothing is patched twice).
STEALTH_JS = _asset("stealth.js")
SELECT_SHIM_JS = _asset("select-shim.js")
INIT_JS = "\n;\n".join(s for s in (STEALTH_JS, SELECT_SHIM_JS) if s)

# what a page reports for CPU count; the box has 96 cores
SPOOF_HW_CONCURRENCY = 8
# plain list: chrome appends the q-values itself (";q=0.9" here would double up)
ACCEPT_LANGUAGE = "en-US,en"

# selection text of the focused input, else the document selection — pushed to
# viewers so their local Ctrl+C can copy what they selected in the remote page
SELECTION_JS = """(() => {
  const a = document.activeElement;
  if (a && (a.tagName === 'INPUT' || a.tagName === 'TEXTAREA') && a.selectionStart != null
      && a.selectionEnd > a.selectionStart) return a.value.substring(a.selectionStart, a.selectionEnd);
  const s = window.getSelection ? String(window.getSelection()) : '';
  return s;
})()"""


class PageSession:
    """Dedicated websocket to one tab (the one the viewer is looking at)."""

    def __init__(self, target_id: str, on_frame):
        self.target_id = target_id
        self.on_frame = on_frame
        self.ws = None
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future] = {}
        self._reader_task = None
        self.screencasting = False
        self.tier = 0
        self.closed = False
        self.last_frame = 0.0
        # CSS size of the viewport the frames depict, from screencast metadata.
        # Chrome's content area changes under us (an infobar such as "restore
        # pages?" appearing or closing shifts it by ~56px), and a frame drawn
        # into a canvas of the wrong aspect puts every click off — so the
        # viewer is told the real size and maps clicks with it.
        self.viewport: tuple[int, int] | None = None
        self.on_viewport = None   # callback((w, h))
        # main-frame load lifecycle → the viewer's progress bar
        self.main_frame_id: str | None = None
        self.loading = False
        self.on_loading = None    # callback(state: "start" | "dom" | "done")

    async def open(self):
        url = f"ws://localhost:{CDP_PORT}/devtools/page/{self.target_id}"
        self.ws = await websockets.connect(url, max_size=32 * 1024 * 1024)
        self._reader_task = asyncio.create_task(self._reader())
        await self.cmd("Page.enable")
        try:
            tree = await self.cmd("Page.getFrameTree", timeout=3)
            self.main_frame_id = tree["frameTree"]["frame"]["id"]
        except Exception:
            self.main_frame_id = None

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

    async def evaluate(self, expression: str, timeout: float = 3):
        r = await self.cmd("Runtime.evaluate",
                           {"expression": expression, "returnByValue": True},
                           timeout=timeout)
        return r.get("result", {}).get("value")

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
                    if not msg["params"]["frame"].get("parentId"):
                        self.main_frame_id = msg["params"]["frame"].get("id", self.main_frame_id)
                        if self.screencasting:
                            self.screencasting = False
                            await self.start_screencast()
                elif msg.get("method") in ("Page.frameStartedLoading", "Page.frameStoppedLoading"):
                    fid = msg["params"].get("frameId")
                    if fid == self.main_frame_id or self.main_frame_id is None:
                        started = msg["method"] == "Page.frameStartedLoading"
                        if started != self.loading:
                            self.loading = started
                            self._loading_state("start" if started else "done")
                elif msg.get("method") == "Page.domContentEventFired":
                    if self.loading:
                        self._loading_state("dom")
                elif msg.get("method") == "Page.loadEventFired":
                    if self.loading:
                        self.loading = False
                        self._loading_state("done")
                elif msg.get("method") == "Page.screencastFrame":
                    params = msg["params"]
                    self.last_frame = time.monotonic()
                    md = params.get("metadata") or {}
                    try:
                        vp = (int(md.get("deviceWidth") or 0), int(md.get("deviceHeight") or 0))
                    except (TypeError, ValueError):
                        vp = (0, 0)
                    if vp[0] and vp[1] and vp != self.viewport:
                        self.viewport = vp
                        if self.on_viewport:
                            self.on_viewport(vp)
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

    def _loading_state(self, state: str):
        if self.on_loading:
            try:
                self.on_loading(state)
            except Exception:
                pass

    async def start_screencast(self):
        if self.screencasting:
            return
        self.screencasting = True
        self.last_frame = time.monotonic()
        quality, w, h = SCREENCAST_TIERS[self.tier]
        await self.cmd(
            "Page.startScreencast",
            {"format": "jpeg", "quality": quality, "maxWidth": w, "maxHeight": h,
             "everyNthFrame": 1},
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

    async def set_tier(self, tier: int):
        tier = max(0, min(tier, len(SCREENCAST_TIERS) - 1))
        if tier == self.tier:
            return
        self.tier = tier
        if self.screencasting:
            await self.stop_screencast()
            await self.start_screencast()


class CDPClient:
    def __init__(self):
        self.ws = None  # browser-level socket: target discovery + tab ops + stealth sessions
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future] = {}
        self._reader_task = None
        self._event_task = None
        self._events: asyncio.Queue | None = None
        self.targets: dict[str, dict] = {}  # targetId -> {"url","title"} (page targets)
        self.page: PageSession | None = None
        self.viewers = 0
        self.on_frame = None      # callback(jpeg_bytes)
        self.on_tabs = None       # callback(tabs_payload)
        self.on_selection = None  # callback(text) — remote selection changed
        self.on_viewport = None   # callback((w, h)) — frames' CSS size changed
        self.on_dialog = None     # callback(payload) — native dialog opened/closed
        self.on_loading = None    # callback(state) — active tab's load progress
        # dialogs currently open, keyed by targetId, so a viewer that connects
        # mid-dialog still gets shown it
        self.open_dialogs: dict[str, dict] = {}
        self._refit_task: asyncio.Task | None = None
        self._last_fit = 0.0
        self._closing = False
        self._switch_lock = asyncio.Lock()
        self._health_task = None
        self._ua_override: dict | None = None   # Emulation.setUserAgentOverride params
        self._metrics_fallback = False          # window couldn't be fitted; force viewport
        self._focus_emulation = False
        self._stealth_sessions: dict[str, str] = {}  # sessionId -> targetId
        self._sel_task: asyncio.Task | None = None
        self._last_selection = ""

    @property
    def active_target_id(self) -> str | None:
        return self.page.target_id if self.page else None

    # ---------- connection ----------

    async def connect(self):
        self._closing = False
        async with httpx.AsyncClient() as client:
            r = await client.get(f"{CDP_HTTP}/json/version", timeout=5)
            ver = r.json()
        self.ws = await websockets.connect(ver["webSocketDebuggerUrl"], max_size=32 * 1024 * 1024)
        self._events = asyncio.Queue()
        self._reader_task = asyncio.create_task(self._reader())
        self._event_task = asyncio.create_task(self._event_loop())
        self.targets.clear()
        self.page = None
        self._stealth_sessions.clear()
        await self.cmd("Target.setDiscoverTargets", {"discover": True})
        await asyncio.sleep(0.3)  # let discovery events land
        if not self.targets:
            await self.cmd("Target.createTarget", {"url": "about:blank"})
            await asyncio.sleep(0.3)
        first = next(iter(self.targets), None)
        if first:
            await self.switch_to(first)
        # stealth prep needs an un-overridden page to read the real client
        # hints from, and a page to measure the window with — do it before the
        # auto-attach applies overrides to everything
        try:
            await self._prepare_stealth(ver)
        except Exception:
            log.exception("stealth preparation failed; continuing with defaults")
        await self.cmd("Target.setAutoAttach",
                       {"autoAttach": True, "waitForDebuggerOnStart": True, "flatten": True})
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
        for t in (self._reader_task, self._event_task, self._health_task, self._sel_task):
            if t:
                t.cancel()
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

    async def cmd(self, method: str, params: dict | None = None, wait: bool = True,
                  session_id: str | None = None, timeout: float = 10) -> dict:
        mid = next(self._ids)
        msg = {"id": mid, "method": method, "params": params or {}}
        if session_id:
            msg["sessionId"] = session_id
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

        sid = msg.get("sessionId")

        # ---- native dialogs, reported by the stealth session of any page ----
        # alert/confirm/prompt/beforeunload and the file picker are browser
        # widgets outside the surface the screencast captures: the viewer would
        # see a frozen page and nothing else. They are surfaced in the UI
        # instead (file choosers are intercepted, so the GTK picker never opens).
        if method == "Page.javascriptDialogOpening" and sid:
            tid = self._stealth_sessions.get(sid, "")
            payload = {"type": "dialog", "dialog": "js", "targetId": tid,
                       "kind": params.get("type"), "message": params.get("message", ""),
                       "default": params.get("defaultPrompt", ""), "url": params.get("url", "")}
            self.open_dialogs[tid] = payload
            if self.on_dialog:
                self.on_dialog(payload)
            return
        if method == "Page.javascriptDialogClosed" and sid:
            tid = self._stealth_sessions.get(sid, "")
            if self.open_dialogs.get(tid, {}).get("dialog") == "js":
                self.open_dialogs.pop(tid, None)
                if self.on_dialog:
                    self.on_dialog({"type": "dialog", "dialog": "closed", "targetId": tid})
            return
        if method == "Page.fileChooserOpened" and sid:
            tid = self._stealth_sessions.get(sid, "")
            payload = {"type": "dialog", "dialog": "file", "targetId": tid,
                       "multiple": params.get("mode") == "selectMultiple",
                       "backendNodeId": params.get("backendNodeId")}
            self.open_dialogs[tid] = payload
            if self.on_dialog:
                self.on_dialog(payload)
            return

        if method == "Target.attachedToTarget":
            # runs in its own task: the setup awaits several commands and must
            # not block delivery of other events (or its own responses)
            asyncio.create_task(self._stealth_attach(params))

        elif method == "Target.detachedFromTarget":
            self._stealth_sessions.pop(params.get("sessionId", ""), None)

        elif method == "Target.targetCreated":
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
            if self.open_dialogs.pop(tid, None) and self.on_dialog:
                self.on_dialog({"type": "dialog", "dialog": "closed", "targetId": tid})
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

    # ---------- stealth: per-target overrides ----------

    async def _prepare_stealth(self, version: dict):
        """Build the user-agent override from the REAL browser (so it tracks
        snap updates), then size the window so the content area is exactly
        the viewport without any emulation."""
        page = self.page
        ua = version.get("User-Agent", "") or (await page.evaluate("navigator.userAgent") or "")
        # the launcher already passes a clean --user-agent; this is belt and braces
        ua = ua.replace("Ubuntu; ", "").replace("Ubuntu ", "")
        hints = None
        try:
            r = await page.cmd("Runtime.evaluate", {
                "expression":
                    "navigator.userAgentData.getHighEntropyValues(['fullVersionList',"
                    "'platformVersion','architecture','bitness','uaFullVersion','model'])"
                    ".then(v => Object.assign({brands: navigator.userAgentData.brands}, v))",
                "awaitPromise": True, "returnByValue": True}, timeout=3)
            hints = r.get("result", {}).get("value")
        except Exception as e:
            log.warning("could not read client hints (%s); using computed brands", e)
        m = re.search(r"Chrome/(\d+)", ua)
        major = m.group(1) if m else "150"
        full = None
        brands = []
        full_list = []
        if isinstance(hints, dict):
            brands = list(hints.get("brands") or [])
            full_list = list(hints.get("fullVersionList") or [])
            full = hints.get("uaFullVersion")
        if not brands:
            brands = [{"brand": "Not;A=Brand", "version": "8"}, {"brand": "Chromium", "version": major}]
        if not full:
            full = f"{major}.0.0.0"
        if not full_list:
            full_list = [{"brand": b["brand"], "version": (full if b["brand"] == "Chromium" else b["version"])}
                         for b in brands]

        def with_chrome(lst, ver):
            if any(b.get("brand") == "Google Chrome" for b in lst):
                return lst
            out = []
            for b in lst:
                out.append(b)
                if b.get("brand") == "Chromium":
                    out.append({"brand": "Google Chrome", "version": ver})
            return out

        self._ua_override = {
            "userAgent": ua,
            "acceptLanguage": ACCEPT_LANGUAGE,
            "platform": "Linux x86_64",
            "userAgentMetadata": {
                "brands": with_chrome(brands, major),
                "fullVersionList": with_chrome(full_list, full),
                "fullVersion": full,
                "platform": "Linux",
                "platformVersion": (hints or {}).get("platformVersion") or "6.17.0",
                "architecture": (hints or {}).get("architecture") or "x86",
                "model": "",
                "mobile": False,
                "bitness": (hints or {}).get("bitness") or "64",
                "wow64": False,
            },
        }
        log.info("stealth UA: %s / brands %s", ua,
                 [b["brand"] for b in self._ua_override["userAgentMetadata"]["brands"]])
        await self.fit_window()
        try:
            self._focus_emulation = not bool(await page.evaluate("document.hasFocus()"))
        except Exception:
            self._focus_emulation = False
        if self._focus_emulation:
            log.info("window has no X focus; enabling focus emulation per target")

    async def fit_window(self):
        """Resize the real Chrome window so its content area is VIEWPORT_W×H.
        Falls back to a device-metrics override on every target if that can't
        be achieved (e.g. a window manager refusing the size)."""
        page = self.page
        if not page:
            return
        dims = None
        try:
            win = await self.cmd("Browser.getWindowForTarget", {"targetId": page.target_id})
            wid = win["windowId"]
            await asyncio.sleep(0.5)          # let the initial layout settle
            for _ in range(4):
                dims = await page.evaluate("[innerWidth, innerHeight, outerWidth, outerHeight]")
                iw, ih, ow, oh = dims
                if (iw, ih) == (VIEWPORT_W, VIEWPORT_H):
                    self._metrics_fallback = False
                    log.info("window fitted: content %dx%d, window %dx%d", iw, ih, ow, oh)
                    return
                b = (await self.cmd("Browser.getWindowBounds", {"windowId": wid}))["bounds"]
                # window bounds and inner size differ by chrome's own UI; grow or
                # shrink by exactly what's missing, but never beyond the screen
                # (a window larger than the screen gets clipped and the
                # measurement stops meaning anything)
                new_w = min(SCREEN_W, int(b.get("width", ow)) + (VIEWPORT_W - iw))
                new_h = min(SCREEN_H, int(b.get("height", oh)) + (VIEWPORT_H - ih))
                await self.cmd("Browser.setWindowBounds",
                               {"windowId": wid,
                                "bounds": {"left": 0, "top": 0, "width": new_w, "height": new_h,
                                           "windowState": "normal"}})
                await asyncio.sleep(0.6)
            log.warning("could not fit window to %dx%d (got %s); using metrics override",
                        VIEWPORT_W, VIEWPORT_H, dims)
        except Exception as e:
            log.warning("fit_window failed (%s); using metrics override", e)
        self._metrics_fallback = True

    async def _stealth_attach(self, params: dict):
        sid = params.get("sessionId")
        info = params.get("targetInfo", {})
        ttype = info.get("type")
        waiting = params.get("waitingForDebugger", False)
        try:
            if ttype in ("page", "iframe"):
                self._stealth_sessions[sid] = info.get("targetId", "")
                await self._apply_stealth(sid, ttype)
        except Exception as e:
            log.warning("stealth setup failed for %s %s: %s", ttype, info.get("url", "")[:60], e)
        finally:
            if waiting:
                try:
                    await self.cmd("Runtime.runIfWaitingForDebugger", session_id=sid, wait=False)
                except Exception:
                    pass
            if ttype not in ("page", "iframe"):
                # workers etc.: nothing to do, don't keep a session open on them
                try:
                    await self.cmd("Target.detachFromTarget", {"sessionId": sid}, wait=False)
                except Exception:
                    pass

    async def _apply_stealth(self, sid: str, ttype: str):
        s = lambda m, p=None: self.cmd(m, p, session_id=sid, timeout=5)  # noqa: E731
        # init scripts only fire while the Page domain is enabled on the
        # session that registered them (blink checks the agent's enabled flag)
        await s("Page.enable")
        if self._ua_override:
            await s("Emulation.setUserAgentOverride", self._ua_override)
        await s("Emulation.setAutomationOverride", {"enabled": False})
        await s("Emulation.setHardwareConcurrencyOverride",
                {"hardwareConcurrency": SPOOF_HW_CONCURRENCY})
        if self._focus_emulation:
            await s("Emulation.setFocusEmulationEnabled", {"enabled": True})
        if self._metrics_fallback and ttype == "page":
            await s("Emulation.setDeviceMetricsOverride",
                    {"width": VIEWPORT_W, "height": VIEWPORT_H, "deviceScaleFactor": 1,
                     "mobile": False})
        if ttype == "page":
            # no native picker; Page.fileChooserOpened is raised instead and the
            # viewer uploads a file from their own device (see file_reply)
            try:
                await s("Page.setInterceptFileChooserDialog", {"enabled": True})
            except Exception as e:
                log.debug("file chooser interception unavailable: %s", e)
        if INIT_JS:
            await s("Page.addScriptToEvaluateOnNewDocument", {"source": INIT_JS})
            if ttype == "page":
                # the document already loaded in this tab predates the
                # registration — shim it live (self-guarded, idempotent)
                try:
                    await s("Runtime.evaluate", {"expression": SELECT_SHIM_JS})
                except Exception:
                    pass
        # out-of-process iframes (e.g. Cloudflare Turnstile) are separate
        # targets under this one: attach to them too, same treatment
        await s("Target.setAutoAttach",
                {"autoAttach": True, "waitForDebuggerOnStart": True, "flatten": True})

    # ---------- tab switching / screencast ----------

    async def switch_to(self, target_id: str):
        async with self._switch_lock:
            if target_id not in self.targets:
                return
            if self.page and self.page.target_id == target_id:
                return
            tier = self.page.tier if self.page else 0
            if self.page:
                await self.page.close()
                self.page = None
            page = PageSession(target_id, lambda b: self.on_frame and self.on_frame(b))
            try:
                await page.open()
            except Exception:
                # one retry: a fresh socket usually recovers a half-dead agent
                await page.close()
                page = PageSession(target_id, lambda b: self.on_frame and self.on_frame(b))
                await page.open()
            page.tier = tier
            page.on_viewport = self._viewport_changed
            page.on_loading = lambda st: self.on_loading and self.on_loading(st)
            self.page = page
            # the bar belongs to the tab on screen; a switch resets it
            if self.on_loading:
                self.on_loading("done")
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

    async def set_tier(self, tier: int):
        if self.page:
            await self.page.set_tier(tier)

    @property
    def tier(self) -> int:
        return self.page.tier if self.page else 0

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
            if msg.get("button") == "right":
                # a right-click opens chrome's NATIVE context menu, which the
                # screencast can't show and which then swallows the next click
                return
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
            if msg["event"] == "mouseReleased":
                self._schedule_selection()

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
                params["unmodifiedText"] = msg.get("unmodifiedText") or msg["text"]
            if msg.get("autoRepeat"):
                params["autoRepeat"] = True
            await page.cmd("Input.dispatchKeyEvent", params, wait=False)
            if msg["event"] == "keyUp":
                self._schedule_selection()

        elif t == "insertText":
            # the viewer's clipboard, pasted: fires real input events in the page
            text = msg.get("text") or ""
            if text:
                await page.cmd("Input.insertText", {"text": text}, wait=False)

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

        elif t == "stop":
            await page.cmd("Page.stopLoading", wait=False)

        elif t in ("back", "forward"):
            expr = "history.back()" if t == "back" else "history.forward()"
            await page.cmd("Runtime.evaluate", {"expression": expr}, wait=False)

        elif t == "tab.new":
            await self.cmd("Target.createTarget", {"url": "about:blank"})

        elif t == "tab.switch":
            await self.switch_to(msg["targetId"])

        elif t == "tab.close":
            await self.cmd("Target.closeTarget", {"targetId": msg["targetId"]})

        elif t == "dialog.reply":
            await self.dialog_reply(msg.get("targetId") or self.active_target_id,
                                    bool(msg.get("accept")), msg.get("text"))

        elif t == "file.reply":
            await self.file_reply(msg.get("targetId") or self.active_target_id,
                                  msg.get("paths") or [], msg.get("backendNodeId"))

    @property
    def viewport(self) -> tuple[int, int]:
        return (self.page.viewport if self.page and self.page.viewport
                else (VIEWPORT_W, VIEWPORT_H))

    def _viewport_changed(self, vp: tuple[int, int]):
        if self.on_viewport:
            self.on_viewport(vp)
        # the frames say the content area drifted (infobar came or went):
        # re-fit the window so it is 1280×800 again — at most once per few
        # seconds, and never while a fit is already running
        if vp != (VIEWPORT_W, VIEWPORT_H) and not self._metrics_fallback:
            if (self._refit_task and not self._refit_task.done()) or \
               time.monotonic() - self._last_fit < 5:
                return
            self._last_fit = time.monotonic()
            self._refit_task = asyncio.create_task(self._refit())

    async def _refit(self):
        await asyncio.sleep(1.0)      # let a transient (mid-navigation) size settle
        if self.page and self.page.viewport and self.page.viewport != (VIEWPORT_W, VIEWPORT_H):
            log.info("viewport drifted to %sx%s; re-fitting window", *self.page.viewport)
            await self.fit_window()

    def _session_for(self, target_id: str | None) -> str | None:
        for sid, tid in self._stealth_sessions.items():
            if tid == target_id:
                return sid
        return None

    async def dialog_reply(self, target_id: str | None, accept: bool, text: str | None):
        """Answer an alert/confirm/prompt/beforeunload the viewer saw."""
        sid = self._session_for(target_id)
        if not sid:
            return
        params = {"accept": accept}
        if text is not None:
            params["promptText"] = str(text)
        try:
            await self.cmd("Page.handleJavaScriptDialog", params, session_id=sid, timeout=5)
        except Exception as e:
            log.warning("handleJavaScriptDialog failed: %s", e)
        self.open_dialogs.pop(target_id, None)

    async def file_reply(self, target_id: str | None, paths: list[str], backend_node_id):
        """Feed files (already stored on this host) to the intercepted file
        input. An empty list cancels — the input is simply left untouched."""
        sid = self._session_for(target_id)
        self.open_dialogs.pop(target_id, None)
        if self.on_dialog:
            self.on_dialog({"type": "dialog", "dialog": "closed", "targetId": target_id})
        if not sid or not paths or not backend_node_id:
            return
        try:
            await self.cmd("DOM.setFileInputFiles",
                           {"files": paths, "backendNodeId": backend_node_id},
                           session_id=sid, timeout=10)
        except Exception as e:
            log.warning("setFileInputFiles failed: %s", e)

    def _schedule_selection(self):
        """Read the remote selection shortly after an input event settles and
        push it to viewers if it changed (their Ctrl+C copies from this cache)."""
        if self._sel_task and not self._sel_task.done():
            return
        self._sel_task = asyncio.create_task(self._push_selection())

    async def _push_selection(self):
        await asyncio.sleep(0.08)
        page = self.page
        if not page or not self.on_selection:
            return
        try:
            text = await page.evaluate(SELECTION_JS, timeout=2)
        except Exception:
            return
        if not isinstance(text, str):
            text = ""
        if text != self._last_selection:
            self._last_selection = text
            self.on_selection(text)

    async def _verify_navigation(self, target_id: str, url: str):
        """Chrome occasionally drops a navigate on the floor; resend once.
        Only http(s) targets are checked, and only by host + path: chrome
        normalises what it reports (data: URLs, fragments, redirects), and a
        false mismatch here re-navigates — which also dismisses any dialog the
        page has meanwhile opened."""
        await asyncio.sleep(2.5)
        if not url.lower().startswith(("http://", "https://")):
            return
        current = self.targets.get(target_id, {}).get("url", "")
        if current.lower().startswith(("http://", "https://")):
            # any http(s) page means the navigation took effect (redirects,
            # sign-in bounces, trailing slashes and fragments all count)
            return
        if self.page and self.page.target_id == target_id:
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
