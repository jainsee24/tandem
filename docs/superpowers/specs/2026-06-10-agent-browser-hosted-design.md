# agent-browser-hosted — Design Spec

Date: 2026-06-10
Status: Approved by user

## Goal

A self-hosted web app with a live, controllable browser embedded in its UI. The user
can browse and log into sites manually from any device; Claude Code agents can then
drive the *same* browser to perform tasks given in natural language (e.g. "apply to
this job with my resume"), in the style of browser-use's `apply_to_job.py` example,
but orchestrated through Claude Code.

## Key decisions (made with user)

1. **Browser embedding: CDP screencast.** Chrome runs on the workstation; the UI
   shows it via `Page.startScreencast` JPEG frames drawn to a `<canvas>`, with user
   mouse/keyboard forwarded back via the CDP `Input` domain. No Xvfb/VNC stack.
2. **Access: password only, public port.** App binds `0.0.0.0`; a single password
   (from `.env`) gates everything via a signed session cookie. User handles port
   exposure themselves (router/ngrok/cloudflared).
3. **Agent runner: `claude -p` subprocess** with `--dangerously-skip-permissions`
   and `--output-format stream-json`. Agents are markdown files in
   `.claude/agents/`; browser tools come from Playwright MCP attached to the shared
   Chrome via `.mcp.json`.

## Architecture

One Python process (FastAPI + uvicorn, default port 8080, overridable via `PORT`
in `.env`) plus one managed Chrome process.

```
Browser (any device) ⇄ HTTPS/WS ⇄ FastAPI app ⇄ CDP ws://localhost:9222 ⇄ Chrome
                                       │
                                       └─ spawns `claude -p` per task
                                              └─ Playwright MCP ⇄ same Chrome (CDP)
```

### Components

- **`server/chrome.py` — Chrome manager.** Launches Chrome with
  `--headless=new --remote-debugging-port=9222 --user-data-dir=./chrome-profile
  --remote-allow-origins=*`. The persistent profile keeps logins across restarts.
  Detects crashes and relaunches; a config flag (`HEADFUL=1`) switches to headful
  (for sites that block headless logins; user runs their own Xvfb/X in that case).
- **`server/cdp.py` — CDP client / screencast service.** Maintains a WebSocket to
  Chrome. Responsibilities:
  - Start/stop `Page.startScreencast` on the active tab; relay frames (base64 JPEG)
    to UI clients over `/ws/screen`; send `screencastFrameAck`.
  - Translate UI input messages (mouse move/click/wheel, key events, text input)
    into `Input.dispatchMouseEvent` / `Input.dispatchKeyEvent` /
    `Input.insertText`. The remote viewport is fixed (e.g. 1280×800) via
    `Emulation.setDeviceMetricsOverride`; the UI scales coordinates between the
    canvas size and that viewport.
  - Tab management via the `Target` domain: list tabs, create, close, activate.
    Track target lifecycle events so the screencast follows whichever tab becomes
    active — including tabs the agent opens or switches to.
  - Navigation commands from the URL bar: `Page.navigate`, back/forward/reload.
- **`server/agent.py` — task runner.** `POST /api/tasks` with an instruction spawns
  `claude -p "<instruction>" --output-format stream-json --verbose
  --dangerously-skip-permissions` with cwd = repo root. Parses the NDJSON event
  stream and relays assistant text, tool calls, and results to UI clients over
  `/ws/agent`. One task at a time (a second submission is rejected with a clear
  error while one runs). Stop button terminates the process group. Each task's
  events and final result are appended to `tasks.json` (JSON-file history).
- **`server/auth.py`.** Login page posts the password; on match (constant-time
  compare against `.env` value) sets a signed session cookie (itsdangerous-style).
  All HTTP routes and both WebSockets validate the cookie; failures redirect to
  login / close the socket.
- **`server/app.py`.** FastAPI wiring: routes, WebSockets, static files, startup
  (launch Chrome, connect CDP) and shutdown hooks.

### Agent definition & browser tools

- `.mcp.json` (repo root) defines a `browser` MCP server:
  `npx @playwright/mcp@latest --cdp-endpoint http://localhost:9222` — Playwright
  MCP attaches to the already-running Chrome, so the agent sees the user's
  logged-in sessions and the user watches the agent work live on the canvas.
  Playwright MCP may act on a tab of its own choosing (often a new one); the
  screencast follows the active tab, so the user still sees the agent's work.
  The agent can upload workstation-local files (e.g. a resume on disk) via
  Playwright MCP's file-upload tool.
- `.claude/agents/browser-operator.md` — the default agent: persona and rules for
  careful web operation (snapshot before acting, fill forms field-by-field,
  verify submissions, report what it did, state clearly when blocked e.g. by a
  CAPTCHA or a missing credential rather than guessing).
- The headless `claude -p` prompt template instructs it to delegate browser work
  to the `browser-operator` agent. More agents later = more markdown files.

### UI (single page, dark theme, no build step)

`static/index.html` + `app.js` + `style.css`, vanilla JS.

- **Main pane:** Chrome-like tab strip; URL bar with back/forward/reload; the
  live `<canvas>`. Canvas captures mouse/keyboard and forwards them; coordinates
  scaled between canvas size and remote viewport.
- **Right pane:** agent panel — instruction textarea + Run, live activity feed
  (assistant text, tool-call one-liners), Stop button, status indicator, and
  collapsible history of past tasks loaded from `tasks.json`.
- **"Agent is driving" banner** while a task runs; user input remains enabled
  (shared browser — the user can always take over).
- Login page: single password field, matching theme.

## Error handling

- Chrome crash → manager relaunches; UI shows a reconnecting overlay until frames
  resume.
- CDP socket drop → reconnect with backoff; screencast restarts on the active tab.
- `claude` process exit (error or kill) → task marked failed/stopped in history,
  UI feed shows the reason.
- WS clients can come and go freely; screencast runs only while ≥1 viewer or a
  task is active (saves CPU otherwise).

## Out of scope (YAGNI)

- Multiple concurrent agent tasks; multi-user accounts; HTTPS termination (user's
  tunnel handles it); mobile-optimized layout; test scaffolding (per user's
  standing preference for research/prototype projects); audio; browser
  extensions; piping files between the viewer's device and the hosted browser
  (uploads of files already on the workstation still work via Playwright MCP).

## Runbook

`run.sh`: checks `.env` (PASSWORD, PORT, HEADFUL), installs Python deps if needed,
starts uvicorn. Chrome is launched by the app itself.
