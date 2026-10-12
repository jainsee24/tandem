/* agent-browser frontend: screencast canvas, input forwarding, tabs, agent panel */
"use strict";

const $ = (id) => document.getElementById(id);
const canvas = $("screen"), ctx = canvas.getContext("2d");
// CSS size of the remote viewport. The server tells us the real one from the
// screencast metadata (chrome's content area shifts when an infobar appears
// or closes); the canvas is resized to match so clicks map 1:1.
let VW = 1280, VH = 800;
function setViewport(w, h) {
  if (!w || !h || (w === VW && h === VH)) return;
  VW = w; VH = h;
  canvas.width = w; canvas.height = h;
  canvas.style.aspectRatio = w + " / " + h;
  $("canvas-wrap").style.aspectRatio = w + " / " + h;
}

/* ---------------- screen websocket ---------------- */

let screenWS = null;
let tabsState = { tabs: [], activeTargetId: null };

// Frame rendering. createImageBitmap decodes the JPEG off the main thread
// where available; older mobile Safari falls back to objectURL + Image. Either
// way we coalesce to the newest frame so latency never builds up, and ack
// every painted frame so the server sends the next one only when this one is
// actually on screen (flow control — see ScreenClient in app.py).
const frameImg = new Image();
let frameUrl = null, decoding = false, pendingBuf = null;
const hasBitmap = typeof createImageBitmap === "function";

function painted() {
  decoding = false;
  if (screenWS && screenWS.readyState === WebSocket.OPEN) screenWS.send('{"type":"ack"}');
  if (pendingBuf) { const b = pendingBuf; pendingBuf = null; paintFrame(b); }
}
frameImg.onload = () => {
  ctx.drawImage(frameImg, 0, 0, VW, VH);
  if (frameUrl) { URL.revokeObjectURL(frameUrl); frameUrl = null; }
  painted();
};
frameImg.onerror = () => {
  if (frameUrl) { URL.revokeObjectURL(frameUrl); frameUrl = null; }
  painted();
};
function paintFrame(buf) {
  if (decoding) { pendingBuf = buf; return; }   // keep only the newest
  decoding = true;
  const blob = new Blob([buf], { type: "image/jpeg" });
  if (hasBitmap) {
    createImageBitmap(blob).then((bmp) => {
      ctx.drawImage(bmp, 0, 0, VW, VH);        // frames may be a smaller tier
      bmp.close && bmp.close();
      painted();
    }, () => { frameUrl = URL.createObjectURL(blob); frameImg.src = frameUrl; });
    return;
  }
  frameUrl = URL.createObjectURL(blob);
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
    const msg = JSON.parse(e.data);          // text = tab updates / selection
    if (msg.type === "tabs") {
      tabsState = msg;
      renderTabs();
    } else if (msg.type === "selection") {
      remoteSelection = msg.text || "";      // what Ctrl+C will copy
    } else if (msg.type === "viewport") {
      setViewport(msg.w, msg.h);
    } else if (msg.type === "dialog") {
      showDialog(msg);
    } else if (msg.type === "loading") {
      setLoading(msg.state);
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

// a hidden tab (phone locked, switched away) doesn't need frames: tell the
// server so it stops the screencast until we're back
document.addEventListener("visibilitychange", () => {
  if (screenWS && screenWS.readyState === WebSocket.OPEN) {
    screenWS.send(JSON.stringify({ type: "view", on: !document.hidden }));
  }
});

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

/* ---------------- native dialogs (alert / confirm / prompt / file picker) ----------------
   Chrome draws these outside the screencast, so the server intercepts them
   over CDP and we render them here. Replies go back as dialog.reply /
   file.reply; a file is first uploaded to the host, then fed to the page's
   <input type=file>. */

let curDialog = null;

function showDialog(d) {
  const box = $("dialog");
  if (d.dialog === "closed") {
    if (!curDialog || curDialog.targetId === d.targetId) { curDialog = null; box.classList.add("hidden"); }
    return;
  }
  curDialog = d;
  const isFile = d.dialog === "file";
  const kind = d.kind || "";
  let host = "";
  try { host = d.url ? new URL(d.url).host : ""; } catch (_) {}
  $("dlg-title").textContent = isFile ? "Choose a file for the page"
    : kind === "beforeunload" ? "Leave this page?" : (host || "The page") + " says";
  $("dlg-msg").textContent = isFile ? (d.multiple ? "The page asks for one or more files." : "The page asks for a file.")
    : kind === "beforeunload" ? "Changes you made may not be saved." : (d.message || "");
  $("dlg-input").classList.toggle("hidden", kind !== "prompt");
  $("dlg-file").classList.toggle("hidden", !isFile);
  $("dlg-file-input").multiple = !!d.multiple;
  $("dlg-file-input").value = "";
  $("dlg-cancel").classList.toggle("hidden", kind === "alert");
  $("dlg-ok").textContent = isFile ? "Upload" : kind === "beforeunload" ? "Leave" : "OK";
  if (kind === "prompt") $("dlg-input").value = d.default || "";
  box.classList.remove("hidden");
  if (kind === "prompt") $("dlg-input").focus();
  else if (!isFile) $("dlg-ok").focus();
}

function closeDialog() {
  curDialog = null;
  $("dialog").classList.add("hidden");
  focusKeys();
}

async function answerDialog(accept) {
  const d = curDialog;
  if (!d) return;
  if (d.dialog === "file") {
    let paths = [];
    if (accept) {
      const files = [...$("dlg-file-input").files];
      if (!files.length) { toast("pick a file first"); return; }
      const fd = new FormData();
      for (const f of files) fd.append("files", f);
      $("dlg-ok").disabled = true;
      try {
        const r = await fetch("/api/uploads", { method: "POST", body: fd });
        if (!r.ok) throw new Error("upload failed (" + r.status + ")");
        paths = (await r.json()).files.map((f) => f.path);
      } catch (err) { toast(String(err.message || err)); $("dlg-ok").disabled = false; return; }
      $("dlg-ok").disabled = false;
    }
    send({ type: "file.reply", targetId: d.targetId, backendNodeId: d.backendNodeId, paths });
    closeDialog();
    return;
  }
  send({ type: "dialog.reply", targetId: d.targetId, accept,
         text: d.kind === "prompt" ? $("dlg-input").value : null });
  closeDialog();
}
$("dlg-ok").onclick = () => answerDialog(true);
$("dlg-cancel").onclick = () => answerDialog(false);
$("dlg-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter") { e.preventDefault(); answerDialog(true); }
  if (e.key === "Escape") { e.preventDefault(); answerDialog(false); }
});
$("dialog").addEventListener("keydown", (e) => {
  if (e.key === "Escape" && curDialog && curDialog.kind !== "alert") { e.preventDefault(); answerDialog(false); }
});

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
$("nav-back").onclick = () => { setLoading("start"); send({ type: "back" }); };
$("nav-fwd").onclick = () => { setLoading("start"); send({ type: "forward" }); };
$("nav-reload").onclick = () => { setLoading("start"); send({ type: "reload" }); };
$("nav-stop").onclick = () => { send({ type: "stop" }); setLoading("done"); };

/* ---- page-load progress bar (like chrome's) ----
   "start" creeps toward 80% over a few seconds, "dom" jumps to 90%, "done"
   fills and fades. The server sends the real main-frame events; navigation
   buttons start the bar optimistically so it reacts on the click itself. */
let loadingTimer = null;
function setLoading(state) {
  const box = $("progress"), bar = $("progress-bar");
  clearTimeout(loadingTimer);
  box.classList.remove("loading", "dom", "done");
  $("nav-reload").classList.toggle("hidden", state !== "done");
  $("nav-stop").classList.toggle("hidden", state === "done");
  if (state === "start") {
    bar.style.transition = "none"; bar.style.width = "0"; bar.style.opacity = "1";
    void bar.offsetWidth;                      // restart the css transition
    bar.style.transition = "";
    box.classList.add("loading"); bar.style.width = "80%";
    // a page that never fires its events shouldn't spin forever
    loadingTimer = setTimeout(() => setLoading("done"), 45000);
  } else if (state === "dom") {
    box.classList.add("dom"); bar.style.width = "92%";
  } else {
    box.classList.add("done"); bar.style.width = "100%";
    loadingTimer = setTimeout(() => { box.classList.remove("done"); bar.style.width = "0"; }, 700);
  }
}

function go() {
  const url = $("urlbar").value.trim();
  if (!url) return;
  setLoading("start");
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

// All keyboard input goes through the hidden textarea (#kbcatch): it is the
// one element type on which every browser fires keydown, beforeinput AND
// copy/cut/paste reliably, on desktop and on phones alike.
const kb = $("kbcatch");
const wrap = $("canvas-wrap");
function focusKeys() {
  kb.focus({ preventScroll: true });
}
kb.addEventListener("focus", () => wrap.classList.add("kb-focus"));
kb.addEventListener("blur", () => wrap.classList.remove("kb-focus"));

canvas.addEventListener("mousedown", (e) => {
  focusKeys();
  e.preventDefault();
  if (e.button === 2) return;   // right-click: native menu would be invisible (server drops it too)
  send({ type: "mouse", event: "mousePressed", ...coords(e),
         button: BTN[e.button] || "left", buttons: e.buttons,
         clickCount: e.detail || 1, modifiers: modifiers(e) });
});
canvas.addEventListener("mouseup", (e) => {
  e.preventDefault();
  if (e.button === 2) return;
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

/* ---- keyboard: US-layout key table ----
   Chrome acts on windowsVirtualKeyCode + modifiers, not on the text. The old
   code derived the code from charCodeAt(0), so '.' (46) arrived as VK_DELETE,
   ',' as PrintScreen, '-' as Insert, '/' as Help — that is why '.' could not
   be typed. This table is what a real US keyboard sends. */
const KEYS = {};
function defKey(key, code, vk, shifted) {
  KEYS[key] = { key, code, vk, text: key };
  if (shifted) KEYS[shifted] = { key: shifted, code, vk, text: shifted, shift: true };
}
for (let i = 0; i < 26; i++) {
  const lo = String.fromCharCode(97 + i), up = String.fromCharCode(65 + i);
  KEYS[lo] = { key: lo, code: "Key" + up, vk: 65 + i, text: lo };
  KEYS[up] = { key: up, code: "Key" + up, vk: 65 + i, text: up, shift: true };
}
")!@#$%^&*(".split("").forEach((sym, d) => defKey(String(d), "Digit" + d, 48 + d, sym));
defKey("`", "Backquote", 192, "~");  defKey("-", "Minus", 189, "_");
defKey("=", "Equal", 187, "+");      defKey("[", "BracketLeft", 219, "{");
defKey("]", "BracketRight", 221, "}"); defKey("\\", "Backslash", 220, "|");
defKey(";", "Semicolon", 186, ":");  defKey("'", "Quote", 222, '"');
defKey(",", "Comma", 188, "<");      defKey(".", "Period", 190, ">");
defKey("/", "Slash", 191, "?");      defKey(" ", "Space", 32);
const SPECIAL = {
  Backspace: ["Backspace", 8], Tab: ["Tab", 9], Enter: ["Enter", 13], Escape: ["Escape", 27],
  Delete: ["Delete", 46], Insert: ["Insert", 45], Home: ["Home", 36], End: ["End", 35],
  PageUp: ["PageUp", 33], PageDown: ["PageDown", 34],
  ArrowLeft: ["ArrowLeft", 37], ArrowUp: ["ArrowUp", 38], ArrowRight: ["ArrowRight", 39], ArrowDown: ["ArrowDown", 40],
  Shift: ["ShiftLeft", 16], Control: ["ControlLeft", 17], Alt: ["AltLeft", 18], Meta: ["MetaLeft", 91],
  CapsLock: ["CapsLock", 20], F1: ["F1", 112], F2: ["F2", 113], F3: ["F3", 114], F4: ["F4", 115],
  F5: ["F5", 116], F6: ["F6", 117], F7: ["F7", 118], F8: ["F8", 119], F9: ["F9", 120],
  F10: ["F10", 121], F11: ["F11", 122], F12: ["F12", 123],
};
function keyInfo(key) {
  if (KEYS[key]) return KEYS[key];
  if (SPECIAL[key]) return { key, code: SPECIAL[key][0], vk: SPECIAL[key][1], text: key === "Enter" ? "\r" : null };
  if (key.length === 1) return { key, code: "", vk: 0, text: key };   // non-US char: text only
  return { key, code: "", vk: 0, text: null };
}

// type a single character / named key: keyDown (with text) + keyUp
function sendKey(key, mods) {
  const k = keyInfo(key);
  const m = (mods || 0) | (k.shift ? 8 : 0);
  const down = { type: "key", event: "keyDown", key: k.key, code: k.code,
                 windowsVirtualKeyCode: k.vk, modifiers: m };
  if (k.text && !(m & 6)) { down.text = k.text; down.unmodifiedText = k.text; }
  send(down);
  send({ type: "key", event: "keyUp", key: k.key, code: k.code, windowsVirtualKeyCode: k.vk, modifiers: m });
}

// text of any length (paste, autocorrect, IME): one insertText, real input events
function sendText(text) {
  if (!text) return;
  if (text.length === 1 && text !== "\n") { sendKey(text, 0); return; }
  send({ type: "insertText", text });
}

// desktop keys. keyCode 229 / "Unidentified" = a soft keyboard composing —
// those come through beforeinput instead.
kb.addEventListener("keydown", (e) => {
  if (e.isComposing || e.keyCode === 229 || e.key === "Unidentified" || e.key === "Process") return;
  const mod = e.ctrlKey || e.metaKey;
  const k = e.key.toLowerCase();
  const clip = mod && !e.altKey && (k === "v" || k === "c" || k === "x");
  if (clip) {
    // let the browser's own copy/cut/paste events fire on the textarea (so
    // no preventDefault); the handlers below bridge them to the remote page
    if (k === "v") return;                       // paste: NOT forwarded as a key
    if (!remoteSelection) { e.preventDefault(); toast("nothing selected in the page"); return; }
    // stage the remote selection in the textarea so the native copy/cut has
    // something to take even in browsers that skip the copy event otherwise;
    // Ctrl+C/X still go to the remote page too (cut must delete there)
    kb.value = remoteSelection;
    kb.select();
  } else {
    e.preventDefault();
  }
  const info = keyInfo(e.key);
  const msg = { type: "key", event: "keyDown", key: e.key, code: e.code || info.code,
                windowsVirtualKeyCode: e.keyCode || info.vk, modifiers: modifiers(e),
                autoRepeat: e.repeat };
  if (info.text && !mod) { msg.text = info.text; msg.unmodifiedText = info.text; }
  send(msg);
});
kb.addEventListener("keyup", (e) => {
  if (e.isComposing || e.keyCode === 229 || e.key === "Unidentified" || e.key === "Process") return;
  const info = keyInfo(e.key);
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "v") return;
  e.preventDefault();
  send({ type: "key", event: "keyUp", key: e.key, code: e.code || info.code,
         windowsVirtualKeyCode: e.keyCode || info.vk, modifiers: modifiers(e) });
});

// soft keyboards (phones) and anything the keydown path didn't consume
kb.addEventListener("beforeinput", (e) => {
  const t = e.inputType || "";
  if (t === "insertCompositionText") return;          // wait for compositionend
  e.preventDefault();
  if (t === "insertText" || t === "insertFromPaste" || t === "insertFromDrop" || t === "insertReplacementText") {
    let data = e.data;
    if (data == null && e.dataTransfer) data = e.dataTransfer.getData("text/plain");
    sendText(data || "");
  } else if (t === "insertLineBreak" || t === "insertParagraph") sendKey("Enter", 0);
  else if (t === "deleteContentBackward" || t === "deleteWordBackward") sendKey("Backspace", 0);
  else if (t === "deleteContentForward" || t === "deleteWordForward") sendKey("Delete", 0);
  kb.value = "";
});
kb.addEventListener("compositionend", (e) => {
  sendText(e.data || "");
  kb.value = "";
});
kb.addEventListener("input", () => {
  // a browser that didn't honour preventDefault on beforeinput would leave
  // text here; never let it accumulate (it is invisible anyway)
  if (kb.value && !kb.__composing) kb.value = "";
});
kb.addEventListener("compositionstart", () => { kb.__composing = true; });
kb.addEventListener("compositionend", () => { kb.__composing = false; });

/* ---- clipboard bridge ----
   The remote Chrome and this device have different clipboards. Paste: our
   clipboard text is inserted into the remote page. Copy/cut: the server keeps
   us posted about the remote selection (after every click / key release), and
   the copy event hands that text to the local clipboard synchronously — no
   permission prompt, works in every browser. */
let remoteSelection = "";
kb.addEventListener("paste", (e) => {
  e.preventDefault();
  const text = e.clipboardData ? e.clipboardData.getData("text/plain") : "";
  if (text) sendText(text);
  else toast("clipboard has no text to paste");
});
function handleCopy(e) {
  if (!remoteSelection) { e.preventDefault(); toast("nothing selected in the page"); return; }
  e.preventDefault();
  e.clipboardData.setData("text/plain", remoteSelection);
  setTimeout(() => { kb.value = ""; }, 0);
  toast("copied");
}
kb.addEventListener("copy", handleCopy);
kb.addEventListener("cut", handleCopy);

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
    if (isTouch) focusKeys();
  }
  tStart = null;
  e.preventDefault();
}, { passive: false });

// phone toolbar helpers: paste needs a user gesture on iOS, so offer a button
$("nav-paste").onclick = async () => {
  try {
    const text = await navigator.clipboard.readText();
    if (text) { sendText(text); toast("pasted"); } else toast("clipboard is empty");
  } catch (_) {
    toast("long-press the page and choose Paste instead");
  }
  focusKeys();
};

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
      // a live session is the chat we continue once it goes quiet, so remember
      // its id the moment we hear about it
      if (msg.task && msg.task.session_id) {
        armResume(msg.task.session_id, msg.task.title || msg.task.instruction);
      }
      applyState(msg.status || (msg.running ? "running" : "idle"));
      if (!bootRestored) {
        bootRestored = true;
        if (!sessionAlive) restoreLastChat();
      }
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

/* ---------------- continuing a closed chat ----------------
   A chat used to end when its session ended: the composer always started a
   brand-new, context-free conversation, so coming back to Tandem and typing
   the next message silently threw the whole chat away. The last conversation
   now stays armed — Run continues it (claude --resume, full context) until you
   explicitly ask for a new one. */
let resumeSid = null, resumeTitle = "", bootRestored = false;

function armResume(sid, title) {
  if (!sid) return;
  resumeSid = sid;
  resumeTitle = (title || "").replace(/\s+/g, " ").trim();
  renderResumeBar();
}

function disarmResume() {
  resumeSid = null;
  resumeTitle = "";
  renderResumeBar();
}

function renderResumeBar() {
  const bar = $("resume-bar");
  // while a session is live the follow-up path already keeps context; the bar
  // matters when nothing is running and Run would otherwise start over
  const show = !!resumeSid && !sessionAlive;
  bar.classList.toggle("hidden", !show);
  if (show) {
    $("resume-title").textContent = resumeTitle || "previous chat";
  }
}

// pull a past conversation back into the feed and arm it for the next message
async function openChat(sid, title) {
  const r = await fetch("/api/chats/" + encodeURIComponent(sid) + "/transcript");
  if (!r.ok) { toast("that chat's transcript is gone"); return false; }
  const data = await r.json();
  $("feed").innerHTML = "";
  lastText = "";
  for (const ev of data.events || []) addEvent(ev);
  addEvent({ kind: "status", text: "— end of previous chat · your next message continues it —" });
  armResume(sid, title || data.title);
  return true;
}

// on load with nothing running, reopen the chat we were last in
async function restoreLastChat() {
  try {
    const r = await fetch("/api/tasks");
    if (!r.ok) return;
    const d = await r.json();
    if (d.current && (d.current.status === "running" || d.current.status === "ready")) return;
    if (!d.last_chat) return;
    await openChat(d.last_chat.session_id, d.last_chat.title);
  } catch (_) { /* offline; the bar just stays hidden */ }
}

function applyState(status) {
  sessionAlive = (status === "running" || status === "ready");
  // model is fixed once a conversation starts; only selectable when idle
  $("model").disabled = sessionAlive;
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
  renderResumeBar();
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
    if (ev.attachments && ev.attachments.length) {
      const a = document.createElement("div");
      a.className = "ev-attach";
      a.textContent = "\u{1F4CE} " + ev.attachments.join(", ");
      el.appendChild(a);
    }
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

/* ---------------- attachments ---------------- */

// files picked in the composer, uploaded when the message is sent
let pendingFiles = [];

$("attach").onclick = () => $("file-input").click();
$("file-input").addEventListener("change", () => {
  for (const f of $("file-input").files) pendingFiles.push(f);
  $("file-input").value = "";   // allow re-picking the same file later
  renderChips();
});

function renderChips() {
  const box = $("attach-chips");
  box.innerHTML = "";
  pendingFiles.forEach((f, i) => {
    const chip = document.createElement("span");
    chip.className = "chip";
    const name = document.createElement("span");
    name.className = "chip-name"; name.textContent = f.name;
    const x = document.createElement("button");
    x.className = "chip-x"; x.textContent = "×";
    x.title = "remove";
    x.onclick = () => { pendingFiles.splice(i, 1); renderChips(); };
    chip.append(name, x);
    box.appendChild(chip);
  });
  box.classList.toggle("hidden", !pendingFiles.length);
}

// push pending files to the server; returns their saved paths for the message
async function uploadPending() {
  if (!pendingFiles.length) return [];
  const fd = new FormData();
  for (const f of pendingFiles) fd.append("files", f);
  const r = await fetch("/api/uploads", { method: "POST", body: fd });
  if (!r.ok) {
    let msg = "upload failed (" + r.status + ")";
    try { msg = (await r.json()).error || msg; } catch (_) {}
    throw new Error(msg);
  }
  return (await r.json()).files.map((f) => f.path);
}

function clearAttachments() {
  pendingFiles = [];
  renderChips();
}

async function submit() {
  const text = $("instruction").value.trim();
  if (!text) {
    if (pendingFiles.length) toast("tell the agent what to do with the attached file(s)");
    return;
  }
  let attachments;
  try { attachments = await uploadPending(); }
  catch (err) { toast(String(err.message || err)); return; }
  const J = { method: "POST", headers: { "Content-Type": "application/json" } };
  if (sessionAlive) {
    // follow-up into the live session — the server echoes it into the feed
    const r = await fetch("/api/tasks/say", { ...J, body: JSON.stringify({ text, attachments }) });
    if (r.ok) { $("instruction").value = ""; clearAttachments(); }
    else toast("couldn't send — session not active");
  } else if (resumeSid) {
    // no live process, but we know which conversation we were in: continue it
    // (claude --resume) so the agent keeps the whole chat, not just this line
    const body = { instruction: text, resume: resumeSid, origin: "human",
                   model: $("model").value, attachments };
    const r = await fetch("/api/tasks", { ...J, body: JSON.stringify(body) });
    if (r.ok) { $("instruction").value = ""; clearAttachments(); }
    else if (r.status === 404) {
      // its transcript is gone — say so rather than silently starting fresh
      disarmResume();
      toast("that chat can no longer be continued — press Run again for a new one");
    } else toast("couldn't continue (" + r.status + ")");
  } else {
    // fresh session: clear the feed; the server emits the first user bubble
    $("feed").innerHTML = "";
    const r = await fetch("/api/tasks", { ...J, body: JSON.stringify({ instruction: text, origin: "human", model: $("model").value, attachments }) });
    if (r.ok) { $("instruction").value = ""; clearAttachments(); }
    else toast("failed to start (" + r.status + ")");
  }
}
$("run").onclick = submit;

// reopen a past conversation from the history panel. This used to refuse to do
// anything unless you had already typed the next message, which made the button
// look broken; it now just loads the chat and arms it, and Run carries on from
// there (with whatever you type next resumed into the same claude session).
async function continueConversation(sessionId, title) {
  if (!await openChat(sessionId, title)) return;
  $("history").classList.add("hidden");   // collapse the panel
  showAgentView(true);                    // focus the agent feed on mobile
  $("instruction").focus();
  if (sessionAlive) {
    // a different session is still running; it has to end before we can resume
    toast("stop the running session, then Run to continue this chat");
  }
}

$("instruction").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); submit(); }
});

$("stop").onclick = () => fetch("/api/tasks/stop", { method: "POST" });

// start a fresh conversation: end any live session, wipe the feed, go idle.
// (the next Run then begins a brand-new chat with no prior context)
$("new-chat").onclick = async () => {
  if (sessionAlive) await fetch("/api/tasks/stop", { method: "POST" });
  $("feed").innerHTML = "";
  $("instruction").value = "";
  clearAttachments();
  lastText = "";
  disarmResume();
  $("history").classList.add("hidden");
  applyState("idle");
  $("instruction").focus();
};

$("resume-new").onclick = () => $("new-chat").click();

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
    instr.className = "h-instr"; instr.textContent = t.title || t.instruction;
    const meta = document.createElement("span");
    meta.className = "h-meta";
    const when = t.last_active || t.started;
    meta.textContent = t.status + (t.model ? " · " + t.model : "")
      + (t.origin === "auto" ? " · automated" : "")
      + (when ? " · " + new Date(when * 1000).toLocaleString() : "");
    row.append(instr, meta);
    const res = document.createElement("div");
    res.className = "h-result"; res.textContent = t.result || "(no result)";
    item.append(row, res);
    item.onclick = () => item.classList.toggle("open");
    // reopen this exact conversation (only while claude still has its transcript)
    if (t.resumable) {
      const cont = document.createElement("button");
      cont.className = "h-continue";
      cont.textContent = "Continue";
      cont.title = "reopen this chat and carry on from where it left off";
      cont.onclick = (e) => { e.stopPropagation(); continueConversation(t.session_id, t.title || t.instruction); };
      item.append(cont);
    }
    h.appendChild(item);
  }
  if (!h.children.length) h.innerHTML = '<div class="hist-item"><span class="h-meta">no past tasks</span></div>';
  h.classList.remove("hidden");
};

/* ---------------- boot ---------------- */

// remember the chosen model across reloads
const savedModel = localStorage.getItem("tandem_model");
if (savedModel && [...$("model").options].some((o) => o.value === savedModel)) {
  $("model").value = savedModel;      // old values ("", "opus") fall back to the default
}
$("model").addEventListener("change", () => localStorage.setItem("tandem_model", $("model").value));

connectScreen();
connectAgent();
