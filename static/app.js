/* agent-browser frontend: screencast canvas, input forwarding, tabs, agent panel */
"use strict";

const $ = (id) => document.getElementById(id);
const canvas = $("screen"), ctx = canvas.getContext("2d");
const VW = 1280, VH = 800;

/* ---------------- screen websocket ---------------- */

let screenWS = null;
let tabsState = { tabs: [], activeTargetId: null };

// Frame rendering via objectURL + Image. This is universal — works on every
// mobile browser (incl. older iOS Safari, where createImageBitmap is flaky).
// We coalesce to the newest frame so latency never builds up.
const frameImg = new Image();
let frameUrl = null, decoding = false, pendingBuf = null;
frameImg.onload = () => {
  ctx.drawImage(frameImg, 0, 0, VW, VH);
  if (frameUrl) { URL.revokeObjectURL(frameUrl); frameUrl = null; }
  decoding = false;
  if (pendingBuf) { const b = pendingBuf; pendingBuf = null; paintFrame(b); }
};
frameImg.onerror = () => {
  if (frameUrl) { URL.revokeObjectURL(frameUrl); frameUrl = null; }
  decoding = false;
};
function paintFrame(buf) {
  if (decoding) { pendingBuf = buf; return; }   // keep only the newest
  decoding = true;
  frameUrl = URL.createObjectURL(new Blob([buf], { type: "image/jpeg" }));
  frameImg.src = frameUrl;
}

let wsReady = false;

function connectScreen() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  screenWS = new WebSocket(`${proto}://${location.host}/ws/screen`);
  screenWS.binaryType = "arraybuffer";
  screenWS.onopen = () => {
    wsReady = true;
    $("conn-dot").classList.add("on");
    $("overlay").classList.add("hidden");
  };
  screenWS.onmessage = (e) => {
    if (typeof e.data !== "string") {       // binary = screencast JPEG frame
      paintFrame(e.data);
      return;
    }
    const msg = JSON.parse(e.data);          // text = tab updates
    if (msg.type === "tabs") {
      tabsState = msg;
      renderTabs();
    }
  };
  const drop = (e) => {
    if (e && e.code === 4401) { location.href = "/login"; return; }
    wsReady = false;
    $("conn-dot").classList.remove("on");
    $("overlay").classList.remove("hidden");
    setTimeout(connectScreen, 1000);
  };
  screenWS.onclose = drop;
  screenWS.onerror = () => { try { screenWS.close(); } catch (_) {} };
}

function send(msg) {
  if (screenWS && screenWS.readyState === WebSocket.OPEN) {
    screenWS.send(JSON.stringify(msg));
  } else {
    // don't fail silently — tell the user why nothing happened
    toast("not connected — reconnecting to the browser…");
  }
}

let toastTimer = null;
function toast(text) {
  let el = $("toast");
  if (!el) {
    el = document.createElement("div");
    el.id = "toast";
    document.body.appendChild(el);
  }
  el.textContent = text;
  el.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove("show"), 2500);
}

/* ---------------- tabs + toolbar ---------------- */

function renderTabs() {
  const box = $("tabs");
  box.innerHTML = "";
  for (const t of tabsState.tabs) {
    const el = document.createElement("div");
    el.className = "tab" + (t.targetId === tabsState.activeTargetId ? " active" : "");
    const title = document.createElement("span");
    title.className = "title";
    title.textContent = t.title || t.url || "new tab";
    const close = document.createElement("button");
    close.className = "close";
    close.textContent = "×";
    close.onclick = (e) => { e.stopPropagation(); send({ type: "tab.close", targetId: t.targetId }); };
    el.append(title, close);
    el.onclick = () => send({ type: "tab.switch", targetId: t.targetId });
    box.appendChild(el);
    if (t.targetId === tabsState.activeTargetId && document.activeElement !== $("urlbar")) {
      $("urlbar").value = (t.url && t.url !== "about:blank") ? t.url : "";
    }
  }
}

$("tab-new").onclick = () => send({ type: "tab.new" });
$("nav-back").onclick = () => send({ type: "back" });
$("nav-fwd").onclick = () => send({ type: "forward" });
$("nav-reload").onclick = () => send({ type: "reload" });

function go() {
  const url = $("urlbar").value.trim();
  if (!url) return;
  send({ type: "navigate", url });
  $("urlbar").blur();        // dismisses the phone keyboard
}
$("nav-go").onclick = go;
$("urlbar").addEventListener("keydown", (e) => {
  if (e.key === "Enter") { e.preventDefault(); go(); }
});

/* ---------------- input forwarding ---------------- */

function modifiers(e) {
  return (e.altKey ? 1 : 0) | (e.ctrlKey ? 2 : 0) | (e.metaKey ? 4 : 0) | (e.shiftKey ? 8 : 0);
}
function coords(e) {
  const r = canvas.getBoundingClientRect();
  return {
    x: Math.round((e.clientX - r.left) / r.width * VW),
    y: Math.round((e.clientY - r.top) / r.height * VH),
  };
}
const BTN = { 0: "left", 1: "middle", 2: "right" };

canvas.addEventListener("mousedown", (e) => {
  canvas.focus();
  e.preventDefault();
  send({ type: "mouse", event: "mousePressed", ...coords(e),
         button: BTN[e.button] || "left", buttons: e.buttons,
         clickCount: e.detail || 1, modifiers: modifiers(e) });
});
canvas.addEventListener("mouseup", (e) => {
  e.preventDefault();
  send({ type: "mouse", event: "mouseReleased", ...coords(e),
         button: BTN[e.button] || "left", buttons: e.buttons,
         clickCount: e.detail || 1, modifiers: modifiers(e) });
});
let lastMove = 0;
canvas.addEventListener("mousemove", (e) => {
  const now = performance.now();
  if (now - lastMove < 16) return;
  lastMove = now;
  send({ type: "mouse", event: "mouseMoved", ...coords(e),
         buttons: e.buttons, modifiers: modifiers(e) });
});
canvas.addEventListener("wheel", (e) => {
  e.preventDefault();
  send({ type: "mouse", event: "mouseWheel", ...coords(e),
         deltaX: e.deltaX, deltaY: e.deltaY, modifiers: modifiers(e) });
}, { passive: false });
canvas.addEventListener("contextmenu", (e) => e.preventDefault());

canvas.addEventListener("keydown", (e) => {
  e.preventDefault();
  const msg = { type: "key", event: "keyDown", key: e.key, code: e.code,
                windowsVirtualKeyCode: e.keyCode, modifiers: modifiers(e) };
  if (e.key.length === 1 && !e.ctrlKey && !e.metaKey) msg.text = e.key;
  send(msg);
});
canvas.addEventListener("keyup", (e) => {
  e.preventDefault();
  send({ type: "key", event: "keyUp", key: e.key, code: e.code,
         windowsVirtualKeyCode: e.keyCode, modifiers: modifiers(e) });
});

/* ---------------- touch input (phones / tablets) ---------------- */

const isTouch = matchMedia("(pointer: coarse)").matches || "ontouchstart" in window;

function touchXY(t) {
  const r = canvas.getBoundingClientRect();
  return {
    x: Math.round((t.clientX - r.left) / r.width * VW),
    y: Math.round((t.clientY - r.top) / r.height * VH),
  };
}

let tStart = null, tMoved = false, tLastX = 0, tLastY = 0;

canvas.addEventListener("touchstart", (e) => {
  if (e.touches.length !== 1) return;
  const t = e.touches[0];
  tStart = touchXY(t); tMoved = false;
  tLastX = t.clientX; tLastY = t.clientY;
  e.preventDefault();
}, { passive: false });

canvas.addEventListener("touchmove", (e) => {
  if (e.touches.length !== 1 || !tStart) return;
  const t = e.touches[0];
  const dx = tLastX - t.clientX, dy = tLastY - t.clientY;
  if (Math.abs(dx) > 3 || Math.abs(dy) > 3) tMoved = true;
  const c = touchXY(t);
  // drag = scroll, like native touch scrolling
  send({ type: "mouse", event: "mouseWheel", x: c.x, y: c.y, deltaX: dx, deltaY: dy, modifiers: 0 });
  tLastX = t.clientX; tLastY = t.clientY;
  e.preventDefault();
}, { passive: false });

canvas.addEventListener("touchend", (e) => {
  if (!tStart) return;
  if (!tMoved) {
    // a tap = a left click at the touch point
    const p = tStart;
    send({ type: "mouse", event: "mousePressed", x: p.x, y: p.y, button: "left", buttons: 1, clickCount: 1, modifiers: 0 });
    send({ type: "mouse", event: "mouseReleased", x: p.x, y: p.y, button: "left", buttons: 0, clickCount: 1, modifiers: 0 });
    // bring up the soft keyboard in case they tapped a text field
    if (isTouch) $("kbcatch").focus();
  }
  tStart = null;
  e.preventDefault();
}, { passive: false });

/* ---------------- mobile soft-keyboard forwarding ---------------- */

const VK = { Backspace: 8, Tab: 9, Enter: 13, Escape: 27, " ": 32,
             ArrowLeft: 37, ArrowUp: 38, ArrowRight: 39, ArrowDown: 40, Delete: 46 };

function sendKey(key, text) {
  const vk = VK[key] != null ? VK[key] : (text ? text.toUpperCase().charCodeAt(0) : 0);
  const down = { type: "key", event: "keyDown", key, code: "", windowsVirtualKeyCode: vk, modifiers: 0 };
  if (text) down.text = text;
  send(down);
  send({ type: "key", event: "keyUp", key, code: "", windowsVirtualKeyCode: vk, modifiers: 0 });
}

const kb = $("kbcatch");
// soft keyboards report typed characters via 'input', not reliable keydown
kb.addEventListener("input", () => {
  for (const ch of kb.value) sendKey(ch, ch);
  kb.value = "";
});
// special keys (Backspace/Enter/arrows) do come through keydown
kb.addEventListener("keydown", (e) => {
  if (e.key in VK && e.key !== " ") {
    e.preventDefault();
    sendKey(e.key, null);
  }
});

/* ---------------- mobile layout (Browser / Agent tabs) ---------------- */

function showAgentView(on) {
  document.body.classList.toggle("show-agent", on);
  $("mtab-browser").classList.toggle("active", !on);
  $("mtab-agent").classList.toggle("active", on);
  if (on) $("agent-badge").classList.add("hidden");  // clear "new activity" dot
}
$("mtab-browser").onclick = () => showAgentView(false);
$("mtab-agent").onclick = () => showAgentView(true);

/* ---------------- agent panel ---------------- */

let agentWS = null;

function connectAgent() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  agentWS = new WebSocket(`${proto}://${location.host}/ws/agent`);
  agentWS.onmessage = (e) => {
    const msg = JSON.parse(e.data);
    if (msg.type === "task_state") {
      applyState(msg.status || (msg.running ? "running" : "idle"));
    } else if (msg.type === "event") {
      addEvent(msg.event);
    }
  };
  agentWS.onclose = (e) => {
    if (e.code === 4401) return; // screen socket handles the login redirect
    setTimeout(connectAgent, 1500);
  };
}

// session is "alive" while running (turn active) or ready (awaiting your next message)
let sessionAlive = false;

function applyState(status) {
  sessionAlive = (status === "running" || status === "ready");
  $("stop").classList.toggle("hidden", !sessionAlive);
  $("agent-banner").classList.toggle("hidden", status !== "running");
  $("run").textContent = sessionAlive ? "Send" : "Run";
  $("instruction").placeholder = sessionAlive
    ? "Add an instruction… e.g. now filter to nonstop, or stop and go back"
    : "Tell the agent what to do in the browser…\ne.g. open hacker news and summarize the top 5 stories";

  const st = $("task-status");
  if (status === "running") { st.className = "status running"; st.textContent = "working"; }
  else if (status === "ready") { st.className = "status ready"; st.textContent = "ready · type to steer"; }
  else if (status === "stopped") { st.className = "status stopped"; st.textContent = "stopped"; }
  else if (status === "failed") { st.className = "status failed"; st.textContent = "failed"; }
  else { st.className = "status idle"; st.textContent = "idle"; }
}

let lastText = "";

function addEvent(ev) {
  const feed = $("feed");
  // the agent emits its final answer as both a text block and the turn result;
  // skip the duplicate result bubble when it just repeats the last message
  if (ev.kind === "result" && ev.ok && (ev.text || "").trim() === lastText.trim()) {
    return;
  }
  if (ev.kind === "text") lastText = ev.text || "";
  const el = document.createElement("div");
  if (ev.kind === "text") {
    el.className = "ev text"; el.textContent = ev.text;
  } else if (ev.kind === "tool") {
    el.className = "ev tool"; el.textContent = ev.name + (ev.summary ? " — " + ev.summary : "");
  } else if (ev.kind === "user") {
    el.className = "ev user"; el.textContent = ev.text;
  } else if (ev.kind === "result") {
    el.className = "ev result" + (ev.ok ? "" : " error"); el.textContent = ev.text || (ev.ok ? "done" : "failed");
  } else {
    el.className = "ev status-line"; el.textContent = ev.text || "";
  }
  feed.appendChild(el);
  feed.scrollTop = feed.scrollHeight;
  // on a phone, flag new agent activity if the user is on the Browser tab
  if (!document.body.classList.contains("show-agent")) {
    $("agent-badge").classList.remove("hidden");
  }
}

async function submit() {
  const text = $("instruction").value.trim();
  if (!text) return;
  const J = { method: "POST", headers: { "Content-Type": "application/json" } };
  if (sessionAlive) {
    // follow-up into the live session — the server echoes it into the feed
    const r = await fetch("/api/tasks/say", { ...J, body: JSON.stringify({ text }) });
    if (r.ok) $("instruction").value = "";
    else toast("couldn't send — session not active");
  } else {
    // fresh session: clear the feed; the server emits the first user bubble
    $("feed").innerHTML = "";
    const r = await fetch("/api/tasks", { ...J, body: JSON.stringify({ instruction: text }) });
    if (r.ok) $("instruction").value = "";
    else toast("failed to start (" + r.status + ")");
  }
}
$("run").onclick = submit;

$("instruction").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); submit(); }
});

$("stop").onclick = () => fetch("/api/tasks/stop", { method: "POST" });

$("history-toggle").onclick = async () => {
  const h = $("history");
  if (!h.classList.contains("hidden")) { h.classList.add("hidden"); return; }
  const r = await fetch("/api/tasks");
  if (!r.ok) return;
  const data = await r.json();
  h.innerHTML = "";
  for (const t of data.history || []) {
    const item = document.createElement("div");
    item.className = "hist-item";
    const row = document.createElement("div");
    row.className = "h-row";
    const instr = document.createElement("span");
    instr.className = "h-instr"; instr.textContent = t.instruction;
    const meta = document.createElement("span");
    meta.className = "h-meta";
    meta.textContent = t.status + (t.started ? " · " + new Date(t.started * 1000).toLocaleString() : "");
    row.append(instr, meta);
    const res = document.createElement("div");
    res.className = "h-result"; res.textContent = t.result || "(no result)";
    item.append(row, res);
    item.onclick = () => item.classList.toggle("open");
    h.appendChild(item);
  }
  if (!h.children.length) h.innerHTML = '<div class="hist-item"><span class="h-meta">no past tasks</span></div>';
  h.classList.remove("hidden");
};

/* ---------------- boot ---------------- */

connectScreen();
connectAgent();
