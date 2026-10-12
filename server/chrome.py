"""Chrome process manager: launch with CDP enabled, watch, relaunch on crash.

Anti-bot-detection: by default Chrome runs *headful* inside an auto-started
virtual display (Xvfb). Headless Chrome is trivially fingerprinted — its
User-Agent literally contains "HeadlessChrome" and it has no real WebGL/GPU
context. Running headful under Xvfb yields a clean "Chrome/<v>" UA and a real
ANGLE/SwiftShader WebGL renderer, so sites treat it as an ordinary browser.

Set HEADLESS=1 to fall back to the old (faster, detectable) headless mode.
If $DISPLAY is already set, that real display is used instead of Xvfb.
"""
import asyncio
import logging
import os
import re
import shutil
import socket
import subprocess
from pathlib import Path

import httpx

log = logging.getLogger("chrome")

# Configurable so tandem doesn't fight another CDP client (e.g. a separate
# automation Chrome) that may already hold the default 9222. Keep this in sync
# with the --cdp-endpoint port in .mcp.json.
CDP_PORT = int(os.environ.get("CDP_PORT", "9222"))
CDP_HTTP = f"http://localhost:{CDP_PORT}"
# The virtual display is a plausible laptop panel; the Chrome *window* inside it
# is sized so its content area is exactly VIEWPORT_W×VIEWPORT_H (see cdp.py
# fit_window). Keeping screen > window is what a real desktop looks like —
# the old 1280×800 screen + forced 1280×800 viewport produced
# innerHeight > outerHeight, a well-known automation tell.
SCREEN_W, SCREEN_H = 1920, 1080
VIEWPORT_W, VIEWPORT_H = 1280, 800
# Chrome's own frame + tab strip + toolbar height under Xvfb (no window
# manager, so chrome draws its own title bar): measured 143px. cdp.py
# re-measures on connect and corrects the window bounds.
CHROME_UI_H_GUESS = 143


def chrome_major(chrome_bin: str) -> str:
    """Major version of the installed browser ("150"), for the UA string."""
    try:
        out = subprocess.run([chrome_bin, "--version"], capture_output=True,
                             text=True, timeout=10).stdout
        m = re.search(r"(\d+)\.\d+\.\d+\.\d+", out)
        if m:
            return m.group(1)
    except Exception:
        pass
    return "150"


def clean_user_agent(major: str) -> str:
    """Stock Google Chrome UA for this platform. The snap/Ubuntu build advertises
    itself as "(X11; Ubuntu; Linux x86_64)" — real Chrome never carries the
    "Ubuntu" token, so bot scorers treat it as a packaged Chromium."""
    return (f"Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            f"(KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36")


class ChromeManager:
    def __init__(self, headless: bool, chrome_bin: str, profile_dir: Path,
                 xvfb_display: str = ":99"):
        self.headless = headless
        self.chrome_bin = chrome_bin
        self.profile_dir = profile_dir
        self.xvfb_display = xvfb_display
        self.proc: subprocess.Popen | None = None
        self.xvfb: subprocess.Popen | None = None
        self.display: str | None = None  # display passed to chrome, if any
        self.on_restart = None  # async callback, set by app wiring
        self._stopping = False

    # ---------- virtual display ----------

    def _ensure_display(self):
        """Pick the display Chrome should use. Start Xvfb if we need to."""
        if self.headless:
            self.display = None
            return
        real = os.environ.get("DISPLAY")
        if real:
            self.display = real  # user has a real X server; use it
            return
        # headful but no display → run our own virtual one
        if self.xvfb is None or self.xvfb.poll() is not None:
            if not shutil.which("Xvfb"):
                log.warning("Xvfb not found; falling back to headless mode")
                self.headless = True
                self.display = None
                return
            num = self.xvfb_display.lstrip(":").split(".")[0]
            if self._display_live(num):
                # a live X server is already on this display (e.g. our own from a
                # previous run that survived) — reuse it rather than fail
                self.display = self.xvfb_display
                self.xvfb = None
                log.info("reusing existing display %s", self.xvfb_display)
                return
            # not live: a hard kill can leave BOTH a stale lock and socket that
            # block a fresh Xvfb — clear them before starting
            Path(f"/tmp/.X{num}-lock").unlink(missing_ok=True)
            Path(f"/tmp/.X11-unix/X{num}").unlink(missing_ok=True)
            self.xvfb = subprocess.Popen(
                ["Xvfb", self.xvfb_display, "-screen", "0",
                 f"{SCREEN_W}x{SCREEN_H}x24", "-nolisten", "tcp"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            self._wait_for_xvfb()
            log.info("started Xvfb on %s (pid %s)", self.xvfb_display, self.xvfb.pid)
        self.display = self.xvfb_display

    @staticmethod
    def _display_live(num: str) -> bool:
        """True if something is actually serving the X display socket."""
        path = f"/tmp/.X11-unix/X{num}"
        if not os.path.exists(path):
            return False
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            s.settimeout(0.5)
            s.connect(path)  # refused → stale socket file, not a live server
            return True
        except OSError:
            return False
        finally:
            s.close()

    def _wait_for_xvfb(self, timeout: float = 8.0):
        num = self.xvfb_display.lstrip(":").split(".")[0]
        sock = Path(f"/tmp/.X11-unix/X{num}")
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            if sock.exists():
                return
            if self.xvfb and self.xvfb.poll() is not None:
                raise RuntimeError("Xvfb exited during startup")
            import time as _t
            _t.sleep(0.1)
        raise RuntimeError("Xvfb did not become ready")

    # ---------- chrome ----------

    def _args(self) -> list[str]:
        args = [
            self.chrome_bin,
            f"--remote-debugging-port={CDP_PORT}",
            "--remote-allow-origins=*",
            f"--user-data-dir={self.profile_dir.resolve()}",
            "--no-first-run",
            "--no-default-browser-check",
            f"--window-size={VIEWPORT_W},{VIEWPORT_H + CHROME_UI_H_GUESS}",
            "--window-position=0,0",
            # anti-automation-fingerprint
            "--disable-blink-features=AutomationControlled",
            "--lang=en-US",
            "--disable-features=Translate,AutomationControlled",
            f"--user-agent={clean_user_agent(chrome_major(self.chrome_bin))}",
            # responsiveness: never throttle timers/rendering because a tab or
            # window "isn't visible" (under Xvfb nothing is, as far as chrome
            # knows), and skip background services the pages never need. None
            # of these is observable from page JS.
            "--disable-background-timer-throttling",
            "--disable-renderer-backgrounding",
            "--disable-backgrounding-occluded-windows",
            "--disable-hang-monitor",
            "--disable-ipc-flooding-protection",
            "--disable-sync",
            "--disable-component-update",
            "--disable-default-apps",
            "--metrics-recording-only",
            # a restart after SIGTERM otherwise shows the "restore pages?"
            # infobar, which shrinks the content area until someone closes it
            "--hide-crash-restore-bubble",
            "--disable-session-crashed-bubble",
        ]
        # Egress IP is the one bot signal a fingerprint patch can't fix: this box
        # sits on a university/datacenter ASN, and some Cloudflare zones mitigate that ASN with
        # an interactive challenge no amount of stealth clears. Point this at a
        # residential proxy/VPN to leave via a clean IP.
        #   CHROME_PROXY=socks5://127.0.0.1:1080   (or http://host:port)
        proxy = os.environ.get("CHROME_PROXY", "").strip()
        if proxy:
            args.append(f"--proxy-server={proxy}")
            log.info("chrome routing through proxy %s", proxy)
        if self.headless:
            # broken NVIDIA driver hangs renderer GPU context creation in headless
            args += ["--headless=new", "--disable-gpu"]
        else:
            # software GL so WebGL has a real renderer string without touching
            # the broken NVIDIA stack
            args += ["--use-gl=angle", "--use-angle=swiftshader"]
        args.append("about:blank")
        return args

    async def start(self):
        # refuse to adopt a foreign chrome already holding the CDP port
        if await self._port_in_use():
            subprocess.run(["pkill", "-f", str(self.profile_dir.resolve())], check=False)
            await asyncio.sleep(1)
            if await self._port_in_use():
                raise RuntimeError(
                    f"port {CDP_PORT} is held by another process; kill it first"
                )
        self._ensure_display()
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        env = {**os.environ}
        if self.display:
            env["DISPLAY"] = self.display
        else:
            env.pop("DISPLAY", None)
        self.proc = subprocess.Popen(
            self._args(),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            env=env,
        )
        await self._wait_until_ready()
        log.info("chrome ready (pid %s, %s)", self.proc.pid,
                 "headless" if self.headless else f"headful on {self.display}")

    async def _port_in_use(self) -> bool:
        try:
            async with httpx.AsyncClient() as client:
                await client.get(f"{CDP_HTTP}/json/version", timeout=1)
            return True
        except httpx.HTTPError:
            return False

    async def _wait_until_ready(self, timeout: float = 15.0):
        async with httpx.AsyncClient() as client:
            deadline = asyncio.get_event_loop().time() + timeout
            while True:
                try:
                    r = await client.get(f"{CDP_HTTP}/json/version", timeout=2)
                    if r.status_code == 200:
                        return
                except httpx.HTTPError:
                    pass
                if asyncio.get_event_loop().time() > deadline:
                    raise RuntimeError(f"chrome did not become ready on :{CDP_PORT}")
                await asyncio.sleep(0.25)

    async def stop(self):
        self._stopping = True
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        if self.xvfb and self.xvfb.poll() is None:
            self.xvfb.terminate()

    async def watchdog(self):
        """Relaunch chrome if it dies; notify via on_restart."""
        while True:
            await asyncio.sleep(2)
            if self._stopping:
                return
            if self.proc is not None and self.proc.poll() is not None:
                log.warning("chrome died (exit %s), relaunching", self.proc.returncode)
                try:
                    await self.start()
                except Exception:
                    log.exception("chrome relaunch failed, retrying")
                    continue
                if self.on_restart:
                    try:
                        await self.on_restart()
                    except Exception:
                        log.exception("on_restart callback failed")
