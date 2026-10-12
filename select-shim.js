/* Tandem select shim — makes <select> dropdowns visible in the screencast.
 *
 * Chrome draws a select's option list as a *browser-process* native widget,
 * outside the renderer surface that Page.startScreencast captures. Proof: an
 * X11 grab of the Xvfb display shows the popup open while the screencast frame
 * from the same instant shows the select still closed. Worse, that invisible
 * popup holds a native grab, so the viewer's next synthesized clicks land on
 * the page underneath it instead of on an option.
 *
 * So: suppress the native popup (preventDefault on mousedown) and draw the
 * options as ordinary in-page DOM. The screencast then captures them for free,
 * and the viewer's existing click/key forwarding drives them like any other
 * element. Committing a choice sets selectedIndex and fires input+change, so
 * site JS sees exactly what a real selection produces.
 *
 * Injected into every document (cdp.py for manual pages, --init-script in
 * .mcp.json for agent pages). Selects inside a cross-origin iframe of an
 * ALREADY-loaded page are only covered from that frame's next navigation on.
 */
(function () {
  "use strict";
  if (window.__tandemSelectShim) return;
  window.__tandemSelectShim = true;

  var CSS =
    ".list{box-sizing:border-box;background:#fff;color:#202124;" +
    "border:1px solid #b9bcc4;border-radius:6px;box-shadow:0 8px 28px rgba(0,0,0,.3);" +
    "max-height:320px;overflow-y:auto;overflow-x:hidden;padding:4px 0;" +
    "font-family:system-ui,-apple-system,'Segoe UI',Roboto,Arial,sans-serif;" +
    "font-size:14px;line-height:1.35;-webkit-user-select:none;user-select:none}" +
    ".row{padding:6px 14px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;cursor:default}" +
    ".row.in{padding-left:26px}" +
    ".row.sel{background:#e8f0fe;font-weight:600}" +
    ".row.off{color:#9aa0a6}" +
    ".row.on,.row:not(.off):hover{background:#1a73e8;color:#fff}" +
    ".grp{padding:7px 10px 3px;font-size:12px;font-weight:700;color:#5f6368}" +
    ".list::-webkit-scrollbar{width:10px}" +
    ".list::-webkit-scrollbar-thumb{background:#c3c6cc;border-radius:6px}";

  var cur = null;   // { sel, host, list, rows, active, mo }
  var typeBuf = "", typeAt = 0;

  function shimmable(el) {
    return !!el && el.tagName === "SELECT" && !el.disabled && !el.multiple &&
           (el.size || 1) <= 1 && el.options.length > 0;
  }

  function selectIn(path) {
    for (var i = 0; i < path.length; i++) {
      if (path[i] && path[i].tagName === "SELECT") return path[i];
    }
    return null;
  }

  function pathOf(e) {
    return e.composedPath ? e.composedPath() : [e.target];
  }

  /* ---------- build ---------- */

  function buildRows(sel, list) {
    list.textContent = "";
    var rows = [];

    function addRow(opt, indented) {
      if (!opt || opt.tagName !== "OPTION") return;
      var r = document.createElement("div");
      r.className = "row" + (indented ? " in" : "") + (opt.disabled ? " off" : "") +
                    (opt.index === sel.selectedIndex ? " sel" : "");
      r.textContent = opt.label || opt.text || " ";
      r.__i = opt.index;
      r.__off = !!opt.disabled;
      if (!opt.disabled) rows.push(r);
      list.appendChild(r);
    }

    var kids = sel.children;
    for (var i = 0; i < kids.length; i++) {
      var k = kids[i];
      if (k.tagName === "OPTGROUP") {
        var lab = document.createElement("div");
        lab.className = "grp";
        lab.textContent = k.label || "";
        list.appendChild(lab);
        for (var j = 0; j < k.children.length; j++) addRow(k.children[j], true);
      } else {
        addRow(k, false);
      }
    }
    return rows;
  }

  function place(sel, host, list) {
    // reset any clamp from a previous placement, else repeated repositioning
    // (we now reposition on every scroll) ratchets the list smaller each time
    list.style.maxHeight = "";
    var r = sel.getBoundingClientRect();
    var de = document.documentElement;
    var vw = de.clientWidth || window.innerWidth;
    var vh = de.clientHeight || window.innerHeight;

    list.style.minWidth = Math.max(r.width, 140) + "px";
    list.style.maxWidth = Math.max(200, vw - 12) + "px";

    var below = vh - r.bottom - 6, above = r.top - 6;
    var h = host.getBoundingClientRect().height;
    var top;
    if (h <= below || below >= above) {
      if (h > below) list.style.maxHeight = Math.max(80, below) + "px";
      top = r.bottom + 2;
    } else {
      if (h > above) list.style.maxHeight = Math.max(80, above) + "px";
      h = host.getBoundingClientRect().height;
      top = Math.max(4, r.top - 2 - h);
    }
    var w = host.getBoundingClientRect().width;
    host.style.left = Math.max(4, Math.min(r.left, vw - w - 4)) + "px";
    host.style.top = Math.max(4, top) + "px";
  }

  function openPopup(sel) {
    close(false);

    var host = document.createElement("div");
    host.setAttribute("style",
      "position:fixed!important;left:0;top:0;margin:0!important;padding:0!important;" +
      "border:0!important;background:none!important;z-index:2147483647!important;" +
      "pointer-events:auto!important;visibility:visible!important;opacity:1!important;");
    var sh = host.attachShadow({ mode: "open" });
    var style = document.createElement("style");
    style.textContent = CSS;
    var list = document.createElement("div");
    list.className = "list";
    sh.appendChild(style);
    sh.appendChild(list);

    // inherit the field's text size (clamped — some sites use 9px or 32px)
    var cs = window.getComputedStyle(sel);
    var fs = parseFloat(cs.fontSize);
    if (fs) list.style.fontSize = Math.max(13, Math.min(20, fs)) + "px";

    var rows = buildRows(sel, list);
    document.documentElement.appendChild(host);
    place(sel, host, list);

    cur = { sel: sel, host: host, list: list, rows: rows, active: -1, mo: null,
            openedAt: Date.now() };

    // keep the selected row in view
    var selRow = list.querySelector(".sel");
    if (selRow) list.scrollTop = Math.max(0, selRow.offsetTop - list.clientHeight / 2);

    // options are often populated lazily (on focus/click, or by another field) —
    // re-render instead of showing a stale or empty list
    try {
      cur.mo = new MutationObserver(function () {
        if (!cur || cur.sel !== sel) return;
        var keep = cur.list.scrollTop;
        cur.rows = buildRows(sel, cur.list);
        cur.active = -1;
        cur.list.scrollTop = keep;
        place(sel, cur.host, cur.list);
      });
      cur.mo.observe(sel, { childList: true, subtree: true, characterData: true });
    } catch (_) {}

    list.addEventListener("click", function (e) {
      var t = e.target;
      while (t && t !== list && !("__i" in t)) t = t.parentNode;
      if (!t || t === list || t.__off) return;
      e.preventDefault();
      e.stopPropagation();
      commit(t.__i);
    }, true);

    try { sel.focus({ preventScroll: true }); } catch (_) { try { sel.focus(); } catch (__) {} }
  }

  function commit(index) {
    var sel = cur && cur.sel;
    close(false);
    if (!sel || index == null || index < 0 || index >= sel.options.length) return;
    if (sel.selectedIndex !== index) {
      sel.selectedIndex = index;
      sel.dispatchEvent(new Event("input", { bubbles: true, composed: true }));
      sel.dispatchEvent(new Event("change", { bubbles: true, composed: true }));
    }
  }

  function close() {
    if (!cur) return;
    if (cur.mo) { try { cur.mo.disconnect(); } catch (_) {} }
    if (cur.host && cur.host.parentNode) cur.host.parentNode.removeChild(cur.host);
    cur = null;
    typeBuf = "";
  }

  /* ---------- keyboard while open ---------- */

  function highlight(i) {
    if (!cur || !cur.rows.length) return;
    if (cur.active >= 0 && cur.rows[cur.active]) cur.rows[cur.active].classList.remove("on");
    cur.active = Math.max(0, Math.min(i, cur.rows.length - 1));
    var r = cur.rows[cur.active];
    r.classList.add("on");
    var top = r.offsetTop, bot = top + r.offsetHeight;
    if (top < cur.list.scrollTop) cur.list.scrollTop = top;
    else if (bot > cur.list.scrollTop + cur.list.clientHeight) {
      cur.list.scrollTop = bot - cur.list.clientHeight;
    }
  }

  function currentIndex() {
    if (cur.active >= 0) return cur.active;
    for (var i = 0; i < cur.rows.length; i++) {
      if (cur.rows[i].__i === cur.sel.selectedIndex) return i;
    }
    return -1;
  }

  /* ---------- listeners ---------- */

  document.addEventListener("mousedown", function (e) {
    if (cur) {
      if (pathOf(e).indexOf(cur.host) !== -1) return;   // inside the popup: let it click
      close();                                          // outside: dismiss, swallow the click
      e.preventDefault();
      e.stopPropagation();
      return;
    }
    if (e.button !== 0) return;
    var sel = selectIn(pathOf(e));
    if (!shimmable(sel)) return;
    e.preventDefault();      // <- this is what stops the invisible native popup
    openPopup(sel);
  }, true);

  document.addEventListener("keydown", function (e) {
    if (!cur) {
      // alt+arrow / F4 open the native popup too — take those over as well
      var sel = document.activeElement;
      if (shimmable(sel) && (e.key === "F4" || (e.altKey && (e.key === "ArrowDown" || e.key === "ArrowUp")))) {
        e.preventDefault();
        e.stopPropagation();
        openPopup(sel);
      }
      return;
    }
    var k = e.key, i = currentIndex(), handled = true;
    if (k === "Escape") close();
    else if (k === "ArrowDown") highlight(i + 1);
    else if (k === "ArrowUp") highlight(i <= 0 ? 0 : i - 1);
    else if (k === "Home" || k === "PageUp") highlight(0);
    else if (k === "End" || k === "PageDown") highlight(cur.rows.length - 1);
    else if (k === "Enter" || k === "Tab" || k === " ") {
      var row = cur.rows[i >= 0 ? i : 0];
      if (row) commit(row.__i); else close();
    } else if (k && k.length === 1 && !e.ctrlKey && !e.metaKey && !e.altKey) {
      var now = Date.now();
      typeBuf = (now - typeAt < 800 ? typeBuf : "") + k.toLowerCase();
      typeAt = now;
      for (var n = 0; n < cur.rows.length; n++) {
        if (cur.rows[n].textContent.trim().toLowerCase().indexOf(typeBuf) === 0) { highlight(n); break; }
      }
    } else handled = false;

    if (handled) { e.preventDefault(); e.stopPropagation(); }
  }, true);

  // A native popup stays glued to its field while the page scrolls, so FOLLOW
  // rather than close. Closing on any scroll made this unusable on a phone:
  // app.js turns a tap's finger-jitter into mouseWheel events, and the wheel's
  // scroll event lands ~30ms LATER — just after the tap opened the popup — so
  // it appeared and vanished within 60ms, every time. Scrolls coming from our
  // own list must be ignored too, or flicking through a long list dismisses it.
  function follow(e) {
    if (!cur) return;
    if (e && pathOf(e).indexOf(cur.host) !== -1) return;   // our own list scrolling
    var r = cur.sel.getBoundingClientRect();
    var vh = document.documentElement.clientHeight || window.innerHeight;
    if (r.bottom <= 0 || r.top >= vh) { close(); return; }  // field scrolled away
    place(cur.sel, cur.host, cur.list);
  }
  window.addEventListener("scroll", follow, true);
  window.addEventListener("resize", follow, true);
  window.addEventListener("wheel", function (e) {
    if (!cur) return;
    if (pathOf(e).indexOf(cur.host) !== -1) return;         // scrolling our list
    // absorb the residual wheel/momentum from the very tap that opened us
    if (Date.now() - cur.openedAt < 350) return;
    close();
  }, true);
  window.addEventListener("blur", function () { close(); });
})();
