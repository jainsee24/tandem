// Stealth init-script — runs before any page script (via Playwright MCP
// --init-script and our own CDP Page.addScriptToEvaluateOnNewDocument).
// Patches the browser fingerprints that flag a headless/server/automated
// browser, so sites treat the session as an ordinary human's machine.
//
// Targets the tells actually observed on this host:
//   - WebGL renderer = "SwiftShader" (software rendering → bot/VM signal)
//   - deviceMemory = 32 (datacenter, not a laptop). hardwareConcurrency (96)
//     is handled natively by CDP Emulation.setHardwareConcurrencyOverride in
//     cdp.py — a native override leaves no patched getter to fingerprint.
//   - missing window.chrome.runtime
//   - navigator.webdriver (locked false; also CDP Emulation.setAutomationOverride)
//   - console.* argument serialisation while a DevTools client (the agent's
//     Playwright) has Runtime enabled — the classic "CDP detected" probe
(() => {
  "use strict";

  // ---- make patched functions look native (defeats fn.toString() checks) ----
  const _toString = Function.prototype.toString;
  const _native = new WeakMap();
  Function.prototype.toString = function () {
    if (_native.has(this)) return _native.get(this);
    return _toString.call(this);
  };
  // hide that we patched toString itself
  _native.set(Function.prototype.toString, "function toString() { [native code] }");
  const asNative = (fn, name) => {
    _native.set(fn, `function ${name}() { [native code] }`);
    return fn;
  };

  const define = (obj, prop, getter) => {
    try {
      Object.defineProperty(obj, prop, { get: asNative(getter, "get " + prop), configurable: true, enumerable: true });
    } catch (e) {}
  };

  // ---- navigator.webdriver = false ----
  define(Navigator.prototype, "webdriver", () => false);

  // ---- realistic RAM (was 32 GB); CPU count is overridden natively ----
  define(Navigator.prototype, "deviceMemory", () => 8);

  // ---- blunt the Runtime.enable leak ----
  // With a DevTools client attached and Runtime enabled (Playwright does this
  // for every page), chrome serialises console.* arguments for the protocol,
  // which invokes getters on the logged objects. Detectors log an object with
  // a getter (or an Error with a `stack` accessor) and watch whether it fires.
  // Passing only primitives through means there is nothing to serialise.
  try {
    const safe = (v) => {
      const t = typeof v;
      if (v === null || t === "string" || t === "number" || t === "boolean" || t === "undefined" || t === "bigint") return v;
      if (t === "symbol") return String(v);
      if (t === "function") return "function " + (v.name || "") + "() { [native code] }";
      if (v instanceof Error) {
        try { return v.name + ": " + v.message; } catch (e) { return "Error"; }
      }
      try { return Object.prototype.toString.call(v); } catch (e) { return "[object]"; }
    };
    for (const m of ["log", "debug", "info", "warn", "error", "trace", "dir", "dirxml", "table", "group", "groupCollapsed", "assert"]) {
      const orig = console[m];
      if (typeof orig !== "function") continue;
      console[m] = asNative(function () {
        const args = new Array(arguments.length);
        for (let i = 0; i < arguments.length; i++) args[i] = safe(arguments[i]);
        return orig.apply(this, args);
      }, m);
    }
  } catch (e) {}

  // ---- window.chrome.runtime (real Chrome exposes it) ----
  try {
    window.chrome = window.chrome || {};
    if (!window.chrome.runtime) {
      window.chrome.runtime = { connect: () => {}, sendMessage: () => {}, onMessage: { addListener: () => {} } };
    }
  } catch (e) {}

  // ---- spoof WebGL vendor/renderer away from SwiftShader ----
  // a plausible Linux/Ubuntu Intel iGPU, consistent with the X11 Linux UA
  const GL_SPOOF = {
    37445: "Google Inc. (Intel)",                                   // UNMASKED_VENDOR_WEBGL
    37446: "ANGLE (Intel, Mesa Intel(R) UHD Graphics 630 (CFL GT2), OpenGL 4.6 (Core Profile) Mesa 23.2.1-1ubuntu3.1~22.04.2)", // UNMASKED_RENDERER_WEBGL
  };
  const patchGL = (proto) => {
    if (!proto || !proto.getParameter) return;
    const orig = proto.getParameter;
    proto.getParameter = asNative(function (p) {
      if (p in GL_SPOOF) return GL_SPOOF[p];
      return orig.apply(this, arguments);
    }, "getParameter");
  };
  try { patchGL(window.WebGLRenderingContext && WebGLRenderingContext.prototype); } catch (e) {}
  try { patchGL(window.WebGL2RenderingContext && WebGL2RenderingContext.prototype); } catch (e) {}
})();
