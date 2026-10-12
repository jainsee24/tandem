# Tandem: ultra-fast + undetectable + clipboard/keyboard fixes — design

Date: 2026-09-08. Applies to every Tandem instance; several can run byte-identical code from separate checkouts; only `.env` and the CDP port in
`.mcp.json` differ.

## What was actually wrong (measured on the live F-1 Chrome via CDP)

| Symptom | Root cause |
|---|---|
| Can't type `.` (also `,` `-` `/` `;` `=`) on phone | `sendKey()` derives the Windows virtual-key code from `charCodeAt(0)`; `.` is 46 = **VK_DELETE**, `,`=44 PrintScreen, `-`=45 Insert, `/`=47 Help. Chrome executes the key code, not the text. |
| Can't copy/paste | Ctrl+V is forwarded as a key; the remote Chrome pastes *its own* (empty, Xvfb) X clipboard. Ctrl+C copies into the remote clipboard the viewer can never read. Two machines, two clipboards, no bridge. |
| Feels slow / laggy | Frames are pushed as fast as `send_bytes` returns, i.e. into the kernel socket buffer — on a phone/tunnel several MB of stale JPEG queue up before backpressure kicks in. No client ack, no adaptive quality, streaming continues when the tab is hidden. |
| Agent slow | "Default model" = the user's global `claude-fable-5-1[1m]` at `effortLevel: high`; the operator also inherits the user's 35 PreToolUse hooks and 5 plugins (52 tools, 52 slash commands in its prompt) on every turn. Cost of a trivial turn: $0.12 vs $0.06 lean. |
| Detected as a bot | UA is `Mozilla/5.0 (X11; **Ubuntu**; Linux …) Chrome/150` (the snap-Chromium tell — real Chrome never says Ubuntu); `Sec-CH-UA`/`userAgentData.brands` = `Chromium` only, no `Google Chrome`; `innerHeight (800) > outerHeight (799)` because the viewport is faked with `Emulation.setDeviceMetricsOverride` instead of sizing the window; `hardwareConcurrency` spoofed with a JS prototype getter (fingerprintable) instead of the native CDP override; stealth init script only registered on the *viewer's* tab session (other tabs / OOPIFs such as the Turnstile iframe get the raw fingerprint); Playwright's `Runtime.enable` makes `console.*` argument serialization observable (classic CDP leak); right-clicks open an invisible native menu that grabs input. The egress IP (a university/datacenter ASN) remains the one signal code can't fix — `CHROME_PROXY` exists for that. |

## Design

### 1. Keyboard + clipboard (static/app.js, server/cdp.py)

* All keyboard input, desktop and mobile, goes through the hidden `#kbcatch` element
  (now a `<textarea>`), which is focused whenever the canvas is clicked/tapped. This is
  the only element type on which every browser reliably fires `copy`/`cut`/`paste`.
* **Typing:** `keydown` with a real `key` (not `Unidentified`/229) is forwarded as
  keyDown/keyUp with a correct US-layout table (`code`, `windowsVirtualKeyCode`, `text`) —
  `.` → `Period`/190, etc. `preventDefault()` stops the textarea from also inserting it.
  Soft keyboards (keyCode 229) fall through to `beforeinput`: `insertText` data of length 1
  → key events; longer (autocorrect, paste) → `Input.insertText`; `deleteContentBackward`
  → Backspace; `insertLineBreak` → Enter; IME composition is forwarded at `compositionend`.
* **Paste:** Ctrl/Cmd+V is *not* forwarded. The native `paste` event fires on the
  textarea; its `clipboardData` text is sent as `{type:"insertText"}` → `Input.insertText`
  (fires real input events in the remote page). Works on desktop and phone (long-press
  paste).
* **Copy/cut:** the server pushes `{type:"selection", text}` to the viewer after every
  mouseReleased / keyUp (debounced, one `Runtime.evaluate` reading `getSelection()` or the
  focused input's selected range). The client caches it; the `copy`/`cut` handlers call
  `clipboardData.setData("text/plain", cached)` synchronously — no permission prompt, no
  async gesture loss. Ctrl+X is still forwarded so the remote actually cuts.
* Right-button presses are swallowed (they open an invisible native menu that steals the
  next click).

### 2. Frame pipeline (server/app.py, server/cdp.py, static/app.js)

* **Ack flow control:** the client sends `{type:"ack"}` after each frame is painted; the
  server holds at most one un-acked frame per viewer (1.5 s safety timeout) and always
  sends the newest frame. Latency can no longer accumulate in socket buffers.
* **Adaptive quality:** the server measures send→ack RTT per viewer. Sustained RTT above
  250 ms steps the screencast down a tier (q60@1280×800 → q45@1024×640 → q35@800×500);
  sustained RTT below 100 ms steps back up. Screencast params are per page, so the
  slowest viewer's tier wins. The canvas keeps its 1280×800 coordinate space; smaller
  frames are scaled on paint.
* **Hidden tab = no stream:** `visibilitychange` sends `{type:"view", on:false/true}`; the
  server counts it as a viewer leaving/returning, so a backgrounded phone costs nothing.
* **Faster decode:** `createImageBitmap` when available (off-main-thread JPEG decode),
  falling back to the `Image`/objectURL path.
* **Chrome flags** for responsiveness (none visible to page JS):
  `--disable-background-timer-throttling --disable-renderer-backgrounding
  --disable-backgrounding-occluded-windows --disable-hang-monitor
  --disable-ipc-flooding-protection --disable-sync --disable-component-update
  --disable-default-apps --metrics-recording-only`.

### 3. Agent speed (server/agent.py, server/app.py, static/index.html, static/app.js)

* Dropdown values encode `model|effort` (the API takes them as two fields; automated
  triggers that still post `"model":"opus"` keep working and get the default effort):
  * `claude-opus-5|medium` — **Opus 5 · fast (default)**
  * `claude-opus-5|high` — Opus 5 · thorough
  * `claude-fable-5-1|medium` — Fable 5.1 · smartest
  * `claude-sonnet-5|low` — Sonnet 5 · fastest
  * `claude-haiku-4-5|low` — Haiku 4.5 · cheapest
* `--effort <level>` is passed through; `DEFAULT_MODEL` / `DEFAULT_EFFORT` env knobs.
* `--settings '{"fastMode":true}'` is passed for Opus models (`AGENT_FAST_MODE=0` to
  disable). The org currently has fast mode off (`penguinModeOrgEnabled:false`), so it is
  a no-op today and switches on automatically the day it is enabled; verified harmless.
* Lean session: `--setting-sources ""` (no inherited hooks/plugins/global model),
  `--tools Read,ToolSearch` (built-ins reduced from 52 to the two it uses),
  `--disable-slash-commands`. `--bare` was tested and rejected: it breaks OAuth.
* Playwright MCP gets `--output-dir output/` so screenshots stop landing in the repo root
  (800+ PNGs there today); `output/` is gitignored.

### 4. Anti-detection (server/chrome.py, server/cdp.py, stealth.js, .mcp.json)

* **Per-target stealth session.** The browser-level CDP socket now does
  `Target.setAutoAttach {flatten, waitForDebuggerOnStart}` and, for every `page` and
  `iframe` target (recursively, so OOPIFs like Cloudflare Turnstile are covered), applies
  before the first script runs:
  * `Emulation.setUserAgentOverride` — UA with the `Ubuntu;` token removed,
    `acceptLanguage: en-US,en;q=0.9`, and `userAgentMetadata` brands
    `[grease, Chromium/150, Google Chrome/150]` + fullVersionList, taken from the real
    browser version at startup so it tracks snap updates.
  * `Emulation.setAutomationOverride {enabled:false}` and
    `Emulation.setHardwareConcurrencyOverride {8}` — native overrides, not JS patches.
  * `Page.addScriptToEvaluateOnNewDocument` with stealth.js + select-shim.js.
  * then `Runtime.runIfWaitingForDebugger`.
  This replaces both the per-tab injection in `PageSession.open` and the
  `--init-script` args in `.mcp.json` (single injection path, no double-patching).
* **Real viewport, no metrics override.** Xvfb becomes 1440×900 (a common laptop size);
  at startup Chrome's window is resized via `Browser.setWindowBounds` so the *content*
  area is exactly 1280×800, measured and verified. `innerHeight ≤ outerHeight` like a real
  window; `screen` is 1440×900. If the measurement fails, the old metrics override is
  applied from the stealth session as a fallback.
* `--user-agent` is also set at launch (covers service-worker fetches the per-target
  override doesn't).
* `stealth.js`: drops the `hardwareConcurrency` prototype patch (native now); adds
  `console.*` argument sanitising (primitives pass through, objects/errors are
  stringified without touching getters) so Playwright's `Runtime.enable` cannot be
  observed through console serialisation; keeps webdriver/deviceMemory/WebGL/chrome.runtime.
* Not done, documented: rebrowser-style `Runtime.enable` removal (bundled playwright-core
  1.61-alpha, no compatible patch), residential egress (`CHROME_PROXY`), real GPU.

### 5. Rollout

* Code is edited in one checkout, committed, then synced (code files only) to any other
  instances; each `.mcp.json` keeps its port.
* Restart each instance (checked idle first) via its service manager. Chrome profiles
  (logins) persist.
* Verification: `/api/chats/...` route on every instance; CDP probe shows clean UA/brands,
  inner≤outer, hw=8 native; a keyboard probe types `a.b,c-d/e` into a test page; clipboard
  round-trip; an end-to-end agent task with the new command line.
