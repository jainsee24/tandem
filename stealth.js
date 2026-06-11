// Stealth init-script — runs before any page script (via Playwright MCP
// --init-script and our own CDP Page.addScriptToEvaluateOnNewDocument).
// Patches the browser fingerprints that flag a headless/server/automated
// browser, so sites treat the session as an ordinary human's machine.
//
// Targets the tells actually observed on this host:
//   - WebGL renderer = "SwiftShader" (software rendering → bot/VM signal)
//   - hardwareConcurrency = 96, deviceMemory = 32 (datacenter, not a laptop)
//   - missing window.chrome.runtime
//   - navigator.webdriver (locked false)
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

  // ---- realistic CPU / RAM (was 96 cores / 32 GB) ----
  define(Navigator.prototype, "hardwareConcurrency", () => 8);
  define(Navigator.prototype, "deviceMemory", () => 8);

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
