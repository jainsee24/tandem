# agent-browser-hosted Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A password-gated web app embedding a live, user-controllable Chrome (CDP screencast) that Claude Code agents can drive via Playwright MCP to perform natural-language tasks.

**Architecture:** One FastAPI process manages a long-lived Chrome (`--remote-debugging-port=9222`, persistent profile). A CDP client streams `Page.screencastFrame` JPEGs to the UI over `/ws/screen` and injects user input via the `Input` domain. `POST /api/tasks` spawns `claude -p ... --output-format stream-json --dangerously-skip-permissions`; Playwright MCP (declared in `.mcp.json`, `--cdp-endpoint http://localhost:9222`) gives the agent the same browser. Task events stream to the UI over `/ws/agent`.

**Tech Stack:** Python 3.10+, FastAPI, uvicorn, `websockets`, httpx, itsdangerous, python-dotenv; vanilla JS/HTML/CSS frontend (no build step); Chrome (`/usr/bin/google-chrome`); Claude Code CLI; `@playwright/mcp` via npx.

**Spec:** `docs/superpowers/specs/2026-06-10-agent-browser-hosted-design.md`

**Per user preference: NO test scaffolding.** Each task ends with a manual verification step instead of unit tests.

---

## File Structure

```
agent-browser-hosted/
├── server/
│   ├── __init__.py          (empty)
│   ├── app.py               FastAPI wiring: routes, WebSockets, startup/shutdown
│   ├── chrome.py            Chrome process manager (launch, health, relaunch)
│   ├── cdp.py               CDP client: screencast, input, tabs, navigation
│   ├── agent.py             claude -p task runner + tasks.json history
│   └── auth.py              password login + signed-cookie session
├── static/
│   ├── index.html           single-page UI (login handled by server-rendered page)
│   ├── app.js               canvas streaming, input capture, tabs, agent panel
│   └── style.css            dark theme
├── .claude/agents/browser-operator.md   agent persona/rules (markdown, user-editable)
├── .mcp.json                Playwright MCP → cdp-endpoint 9222
├── requirements.txt
├── .env.example             PASSWORD, PORT=8080, HEADFUL=0, TASK_TIMEOUT_MIN=30
├── .gitignore               chrome-profile/, tasks.json, .env, __pycache__
├── run.sh
└── README.md
```

Runtime artifacts (git-ignored): `chrome-profile/` (persistent logins), `tasks.json` (task history).

---

### Task 1: Scaffold

**Files:** Create everything except the five `server/*.py` modules' real logic and `static/*` content (stubs OK where noted).

- [ ] **Step 1: Create directory tree and config files**

`requirements.txt`:
```
fastapi
uvicorn[standard]
websockets
httpx
itsdangerous
python-dotenv
```

`.env.example`:
```
PASSWORD=change-me
PORT=8080
HEADFUL=0
TASK_TIMEOUT_MIN=30
CHROME_BIN=/usr/bin/google-chrome
```

`.gitignore`: `chrome-profile/`, `tasks.json`, `.env`, `__pycache__/`, `*.pyc`, `.venv/`

`run.sh` (chmod +x): create `.venv` if missing, `pip install -r requirements.txt -q`, copy `.env.example` → `.env` if missing (warn loudly to set PASSWORD), then `exec .venv/bin/uvicorn server.app:app --host 0.0.0.0 --port ${PORT:-8080}`. Load PORT by sourcing `.env`.

`.mcp.json`:
```json
{
  "mcpServers": {
    "browser": {
      "command": "npx",
      "args": ["-y", "@playwright/mcp@latest", "--cdp-endpoint", "http://localhost:9222"]
    }
  }
}
```

- [ ] **Step 2: Write `.claude/agents/browser-operator.md`**

Frontmatter: `name: browser-operator`, `description: Drives the shared hosted Chrome via the 'browser' MCP tools to complete web tasks.`, `tools: ...` omitted (inherit all). Body rules: always take a page snapshot before acting; act on the user's logged-in sessions — never log out or change account settings; fill forms field-by-field and re-snapshot to verify values; after submitting, verify success from the resulting page; if blocked (CAPTCHA, 2FA, missing credential/file), STOP and report exactly what is needed; finish with a concise report of actions taken and outcome. Note that files on this workstation may be uploaded with the file-upload tool when the task requires it (e.g. a resume).

- [ ] **Step 3: Verify + commit**

Run: `cd /home/sejain/repos/agent-browser-hosted && python3 -m venv .venv && .venv/bin/pip install -q -r requirements.txt && echo OK` → `OK`. Also `npx -y @playwright/mcp@latest --help | head -5` to confirm npx can fetch it (requires node; if missing, install via apt/nvm and record in README).
Commit: `git add -A && git commit -m "scaffold: config, deps, agent definition, mcp config"`

---

### Task 2: Chrome manager (`server/chrome.py`)

**Files:** Create `server/__init__.py`, `server/chrome.py`

- [ ] **Step 1: Implement `ChromeManager`**

```python
class ChromeManager:
    def __init__(self, headful: bool, chrome_bin: str, profile_dir: Path): ...
    async def start(self): ...      # launch + wait_until_ready
    async def stop(self): ...
    async def watchdog(self): ...   # loop: if proc exited, relaunch + fire on_restart callback
    on_restart: callable | None     # set by cdp client to reconnect
```

Launch args: `--remote-debugging-port=9222 --remote-allow-origins=* --user-data-dir=<profile> --no-first-run --no-default-browser-check --window-size=1280,800` plus `--headless=new` unless headful. Use `subprocess.Popen(..., start_new_session=True)`. `wait_until_ready`: poll `http://localhost:9222/json/version` with httpx up to 15 s. Watchdog polls `proc.poll()` every 2 s; on death: relaunch, await ready, call `on_restart`.

- [ ] **Step 2: Verify + commit**

Run: `.venv/bin/python -c "import asyncio; from pathlib import Path; from server.chrome import ChromeManager; cm=ChromeManager(False,'/usr/bin/google-chrome',Path('chrome-profile')); asyncio.run(cm.start()); print('ready')"` → `ready`, then `curl -s localhost:9222/json/version | head -c 200` shows Chrome JSON. Kill leftover chrome afterwards (`pkill -f remote-debugging-port=9222`).
Commit: `git add server && git commit -m "feat: chrome process manager with watchdog"`

---

### Task 3: CDP client (`server/cdp.py`)

**Files:** Create `server/cdp.py`. This is the trickiest module — implement exactly as below.

- [ ] **Step 1: Implement `CDPClient` core (connect + message routing)**

```python
class CDPClient:
    async def connect(self):
        # GET http://localhost:9222/json/version -> webSocketDebuggerUrl
        # websockets.connect(url, max_size=20*1024*1024)
        # start reader task; Target.setDiscoverTargets {"discover": True}
        # pick first page target (or create one via Target.createTarget about:blank), switch to it
    async def cmd(self, method, params=None, session_id=None) -> dict:
        # incrementing id; future in self._pending[id]; send; await with timeout 10s
    # reader task: msg with "id" -> resolve future; msg with "method" -> dispatch:
    #   Target.targetCreated/targetDestroyed/targetInfoChanged -> update self.targets (only type=="page", url not devtools://), push tab list to UI; on created page target: switch_to(it)
    #   Page.screencastFrame (matching active session) -> on_frame callback; ack via Page.screencastFrameAck {sessionId: params["sessionId"]}
```

`self.targets: dict[targetId, {"url","title"}]` ordered. `on_frame(b64_jpeg: str)` and `on_tabs(list)` are callbacks set by app.py.

- [ ] **Step 2: Implement tab attach/switch + screencast lifecycle**

```python
async def switch_to(self, target_id):
    # stop screencast on old session (ignore errors)
    # Target.attachToTarget {targetId, flatten: True} -> sessionId; store active_target_id/active_session
    # on session: Page.enable; Emulation.setDeviceMetricsOverride {width:1280,height:800,deviceScaleFactor:1,mobile:False}
    # Target.activateTarget {targetId}
    # if self.viewers > 0: await self._start_screencast()
async def _start_screencast(self):
    # Page.startScreencast {format:"jpeg", quality:70, maxWidth:1280, maxHeight:800, everyNthFrame:1} on active session
def add_viewer(self)/remove_viewer(self):
    # refcount; start screencast on 0->1, Page.stopScreencast on 1->0
```

- [ ] **Step 3: Implement input + navigation + tab ops**

One entrypoint consumed by app.py: `async def handle_ui_message(self, msg: dict)`. Mapping:

| UI msg `type` | CDP call (on active session unless noted) |
|---|---|
| `mouse` | `Input.dispatchMouseEvent` with `{type: msg.event, x, y, button: msg.get("button","none"), buttons, clickCount, deltaX, deltaY, modifiers}` — events: mousePressed/mouseReleased/mouseMoved/mouseWheel |
| `key` | `Input.dispatchKeyEvent` `{type: msg.event, key, code, windowsVirtualKeyCode, modifiers, text}` — for keyDown include `text` only when provided by UI (printable) |
| `navigate` | `Page.navigate {url}` (prepend `https://` if no scheme; if input has no dot and no scheme, treat as Google search URL) |
| `reload` | `Page.reload` |
| `back`/`forward` | `Page.getNavigationHistory` → if neighbor entry exists `Page.navigateToHistoryEntry` |
| `tab.new` | `Target.createTarget {url: "about:blank"}` (targetCreated handler switches to it) |
| `tab.switch` | `switch_to(msg.targetId)` |
| `tab.close` | `Target.closeTarget`; if it was active, switch to first remaining (create about:blank if none) |

Also `async def reconnect(self)` — full re-connect used by chrome watchdog `on_restart` and on reader-task death (retry loop with 1 s backoff, max 30 tries), then re-push tabs and notify UI.

- [ ] **Step 4: Verify + commit**

Write throwaway `scripts/smoke_cdp.py` (git-ignored or deleted after): start ChromeManager + CDPClient, `handle_ui_message({"type":"navigate","url":"https://example.com"})`, `add_viewer()`, set `on_frame` to save first frame to `/tmp/frame.jpg`, sleep 3 s.
Run: `.venv/bin/python scripts/smoke_cdp.py && file /tmp/frame.jpg` → `JPEG image data`. View the image to confirm example.com rendered. Delete script.
Commit: `git add server && git commit -m "feat: CDP client - screencast, input, tabs, navigation"`

---

### Task 4: Auth + app skeleton (`server/auth.py`, `server/app.py`)

**Files:** Create `server/auth.py`, `server/app.py`

- [ ] **Step 1: Implement auth**

`auth.py`: load `.env` (python-dotenv). `SECRET` = random per-start (`secrets.token_hex(32)`) — sessions reset on restart, fine. `make_cookie()` → `itsdangerous.TimestampSigner(SECRET).sign(b"ok")`. `check(request_or_ws) -> bool`: unsign cookie `abh_session` with `max_age=7*86400`, constant-time password compare uses `hmac.compare_digest`. Routes in app.py: `GET /login` (dark-themed inline-HTML password form), `POST /login` (form field `password`; success → set cookie HttpOnly SameSite=Lax, `RedirectResponse("/", status_code=303)`; failure → form with error). Dependency `require_auth` for HTTP → 307 to `/login`; for WS → `await ws.close(code=4401)`.

- [ ] **Step 2: App skeleton + lifecycle**

`app.py`: FastAPI with lifespan: on startup instantiate `ChromeManager` (env-driven) + `CDPClient`, `await chrome.start()`, `await cdp.connect()`, create watchdog task, wire `chrome.on_restart = cdp.reconnect`; on shutdown stop both. Routes: `GET /` → `static/index.html` (auth-gated), `/static/*` mounted, `/login`. Hold `viewers: set[WebSocket]` for screen and `agent_clients: set[WebSocket]` module-level (single-process app).

- [ ] **Step 3: Verify + commit**

Run: `.venv/bin/uvicorn server.app:app --port 8080 &`; `curl -s -o /dev/null -w '%{http_code} %{redirect_url}\n' localhost:8080/` → `307 .../login`; `curl -s -X POST -d password=$PASSWORD -o /dev/null -w '%{http_code}' localhost:8080/login` → `303`; with cookie jar, `GET /` → 200. Kill server.
Commit: `git add server && git commit -m "feat: password auth and app skeleton"`

---

### Task 5: Screen WebSocket + browser UI

**Files:** Modify `server/app.py`; Create `static/index.html`, `static/style.css`, `static/app.js`

- [ ] **Step 1: `/ws/screen` endpoint**

On connect (auth-checked): add to viewers, `cdp.add_viewer()`, send `{"type":"tabs", ...}` snapshot. Loop: `receive_json()` → `cdp.handle_ui_message(msg)`. On disconnect: remove, `cdp.remove_viewer()`. `cdp.on_frame` → broadcast `{"type":"frame","data":b64}` to all viewers; `cdp.on_tabs` → broadcast tab list + `activeTargetId`. Broadcasts use a helper that drops dead sockets.

- [ ] **Step 2: UI layout (`index.html` + `style.css`)**

Dark theme (bg `#111317`, panels `#1a1d23`, accent `#6c8cff`, system-ui font). Grid: header bar (app name, connection dot) / main row = browser pane (flex-1) + agent panel (380 px, right). Browser pane: tab strip (scrollable, each tab = title + ✕, plus ＋ button), toolbar (◀ ▶ ⟳ buttons + URL input), canvas container with `<canvas id="screen" tabindex="0">` letterboxed to 1280:800 aspect, "reconnecting…" overlay div, "Agent is driving" banner (hidden by default). Agent panel: status row, activity feed (scrollable), textarea + Run button + Stop button, collapsible "History" section.

- [ ] **Step 3: `app.js` — streaming + input capture**

- WS connect to `wss?://host/ws/screen` with auto-reconnect (1 s backoff, show overlay while closed).
- `frame` msg → `img.src = "data:image/jpeg;base64,"+data`; on load draw to 1280×800 canvas.
- Mouse: listen on canvas; scale `(e.offsetX / canvas.clientWidth) * 1280` etc.; map mousedown/up → mousePressed/mouseReleased with `button` (left/middle/right), `clickCount` (use `e.detail`), mousemove (throttle ~60 Hz) → mouseMoved, wheel → mouseWheel with `deltaX/deltaY` (negate per CDP convention: CDP deltaY>0 scrolls up, so send `-e.deltaY`), contextmenu preventDefault.
- Keyboard: canvas focused → keydown/keyup preventDefault; send `{type:"key", event, key, code, windowsVirtualKeyCode: e.keyCode, modifiers}` and include `text: e.key` on keydown when `e.key.length === 1`.
- Modifiers bitmask: Alt=1, Ctrl=2, Meta=4, Shift=8.
- Toolbar: URL input Enter → `navigate`; buttons → back/forward/reload; tab strip renders from `tabs` msgs, click → `tab.switch`, ✕ → `tab.close`, ＋ → `tab.new`.

- [ ] **Step 4: Verify + commit**

Run server, open `http://localhost:8080`, log in. Verify: live page visible; type a URL (e.g. `news.ycombinator.com`) and navigate; click links; type into a search box; scroll; open/close/switch tabs; second browser window shows the same stream. Use the superpowers-chrome browsing skill (or your own browser) for this check.
Commit: `git add -A && git commit -m "feat: live browser pane - screencast, input, tabs"`

---

### Task 6: Agent runner + agent UI

**Files:** Create `server/agent.py`; Modify `server/app.py`, `static/app.js`, `static/index.html`

- [ ] **Step 1: Implement `TaskRunner` (`server/agent.py`)**

```python
class TaskRunner:
    current: dict | None   # running task record
    async def start(self, instruction: str) -> dict | error if busy
    async def stop(self)
    on_event: callable     # app.py broadcasts to /ws/agent
```

`start`: build prompt = instruction (verbatim). Read `.claude/agents/browser-operator.md`, strip YAML frontmatter, pass body via `--append-system-prompt` so the operator rules govern the top-level agent while the markdown stays user-editable. Command:

```python
["claude", "-p", instruction,
 "--output-format", "stream-json", "--verbose",
 "--dangerously-skip-permissions",
 "--mcp-config", ".mcp.json",
 "--append-system-prompt", operator_rules]
```

`asyncio.create_subprocess_exec(..., cwd=REPO_ROOT, stdout=PIPE, stderr=PIPE, start_new_session=True)`. Reader task parses NDJSON lines:
- `type=="system" subtype=="init"` → event `{kind:"status", text:"agent started"}`
- `type=="assistant"` → for each content block: text → `{kind:"text", text}`; tool_use → `{kind:"tool", name, summary}` where summary = compact one-liner of key input fields (e.g. url, element, text — truncate 120 chars)
- `type=="result"` → `{kind:"result", ok: not is_error, text: result}`; finalize record
Timeout (`TASK_TIMEOUT_MIN`) and `stop()` both: `os.killpg(os.getpgid(proc.pid), signal.SIGTERM)`, mark status `stopped`/`timeout`. If the process exits without emitting a `result` line (crash/OOM), finalize as `failed` on stdout EOF with the tail of stderr as the reason — never leave a task stuck "running". Every event appended to the task record; on finalize, append record to `tasks.json` (read-modify-write, keep last 100).

Note (intentional spec deviation): the spec described delegating to the `browser-operator` subagent via the Task tool; instead the operator rules are injected with `--append-system-prompt` so the top-level agent acts directly — fewer hops and every event streams to the UI. The markdown file remains the single editable source of the agent's behavior.

- [ ] **Step 2: API + `/ws/agent`**

`POST /api/tasks {instruction}` → 409 if busy else `{id}`. `POST /api/tasks/stop`. `GET /api/tasks` → history (most recent first) + current. `/ws/agent`: auth-checked, add to clients, push `{"type":"task_state", running: bool, task: current}` on connect; `on_event` broadcasts `{"type":"event", taskId, event}` plus `task_state` changes.

- [ ] **Step 3: Agent panel UI**

Run button → POST, disable while running, show "Agent is driving" banner over browser pane (non-blocking, pointer-events none). Feed renders events: text blocks as chat bubbles, tool calls as muted monospace one-liners (`▸ browser_click — "Apply now" button`), result as green/red terminal bubble. Stop button → `/api/tasks/stop`. History section: collapsed list from GET /api/tasks (instruction, status, time); click expands final result text.

- [ ] **Step 4: Verify end-to-end + commit**

With server running and UI open: submit task `Go to https://example.com and tell me the exact text of the main heading.` Watch: feed shows tool calls, canvas shows the agent navigating, result bubble contains "Example Domain". Check `tasks.json` has the record. Then verify Stop: start task `Browse hacker news and summarize top 5 stories`, hit Stop mid-run → status `stopped`, claude process gone (`pgrep -f "claude -p"` empty).
Commit: `git add -A && git commit -m "feat: claude -p task runner with live event feed"`

---

### Task 7: Hardening + README

**Files:** Modify `server/app.py`, `server/cdp.py`, `static/app.js`, `README.md`

- [ ] **Step 1: Resilience checks**

- Kill Chrome manually (`pkill -f remote-debugging-port=9222`) while UI open → watchdog relaunches, CDP reconnects, overlay clears, frames resume (logins persist via profile).
- Refresh UI mid-task → feed resumes from `task_state` + subsequent events.
- Screencast stops when last viewer disconnects (check Chrome CPU drops) and resumes on reconnect.
- Agent switching to an *existing* tab: CDP fires no event for mere activation, so while a task is running, poll `GET http://localhost:9222/json` (MRU-ordered) every ~2 s and switch the screencast to the front-most page target if it differs from the active one.
Fix whatever breaks.

- [ ] **Step 2: README**

Sections: what it is (1 paragraph + architecture diagram from spec), quickstart (`cp .env.example .env`, set PASSWORD, `./run.sh`), exposing it (ngrok/cloudflared one-liners + warning that password is the only barrier), how agents work (edit `.claude/agents/browser-operator.md`, add more agent files), HEADFUL mode note, troubleshooting (node/npx required for Playwright MCP; port 9222 must stay firewalled/localhost-only).

- [ ] **Step 3: Final verify + commit**

Full pass: fresh `./run.sh` from clean shell, login from a second device on LAN (or `curl -b` simulation), run one real task. 
Commit: `git add -A && git commit -m "docs: README; hardening fixes"`

---

## Execution notes

- Port 9222 binds localhost by default — never expose it; only the FastAPI port is public.
- If `--headless=new` breaks a login flow, set `HEADFUL=1` in `.env` (needs a display/Xvfb) — no code change.
- Playwright MCP may open its own tab; the targetCreated handler auto-switches the screencast to it by design.
