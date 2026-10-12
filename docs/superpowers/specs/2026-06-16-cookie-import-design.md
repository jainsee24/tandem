# Import session (paste cookie JSON → inject → open site)

**Date:** 2026-06-16
**Status:** Approved for planning
**Component:** agent-browser-hosted (Tandem)

## Problem

The user is often already logged into a site in their *own* local browser and
wants the hosted agent browser to act as that logged-in user without
re-authenticating. The intuitive ask — "a button on the agent page that reads
the cookies of a site I have open in another tab" — is **impossible** by browser
design:

1. **Same-origin policy** — JavaScript served from the agent-browser origin
   (`tandem.example.com:8080`) cannot read `document.cookie` of any other
   origin. There is no web API for "read another tab's cookies."
2. **httpOnly** — virtually all real auth/session cookies are `httpOnly` and are
   invisible to *any* page JavaScript, even same-origin. Only the browser engine
   and installed extensions can read them.

So the session bytes we need are exactly the ones the browser hides from pages.
The feasible path is to let the user **export** the session from their local
browser (with a cookie-export extension, which *does* have privileged cookie
access) and **paste** it into the agent UI, where the server injects it into the
hosted Chromium via CDP.

## Chosen approach

**Paste cookie JSON.** No extra client software to build; the user keeps full
control of exactly what is shared.

- Source tool: **Cookie-Editor** browser extension (its JSON export; the
  EditThisCookie shape is near-identical and parses the same way). Other formats
  (e.g. Netscape `cookies.txt`) are explicitly **out of scope**.
- Post-import behavior: **auto-open the site.** After injecting, open
  `https://<primary-cookie-domain>/` in a new hosted tab so the user immediately
  sees the logged-in state in the live screencast.
- A companion **Clear-all cookies** control (browser-wide wipe) is included as a
  safety valve, behind an explicit confirm.

Approaches considered and rejected:
- *Browser extension that POSTs cookies* — more robust/one-click, but requires
  building and installing an extension. Rejected for now in favour of zero-build.
- *Log in inside the agent browser* — simplest/most secure (no cookies leave the
  machine) and already possible, but does not satisfy "reuse my existing
  session without re-auth."
- *CDP bridge to the user's local Chrome* — requires relaunching local Chrome
  with a debug port and same-host reachability; fragile. Rejected.

## Architecture

The server already runs Chromium with CDP on `:9222` against a **persistent
profile** (`chrome-profile/`), and `CDPClient` holds a browser-level websocket
with a generic `cmd(method, params)` method. Cookies can therefore be written
profile-wide with a single `Storage.setCookies` call — no page session or
`Network` domain needed — and they persist across restarts because the profile
is on disk.

### Components

#### `server/cookies.py` (new — pure translation logic, no I/O)

```
to_cdp_cookies(raw: list[dict], now: float) -> ImportResult
```

`ImportResult` carries: `cdp_cookies: list[dict]`, `skipped: list[{name, reason}]`,
`primary_domain: str | None`.

Per-record mapping (Cookie-Editor record → CDP `CookieParam`):

| Cookie-Editor field | CDP field | Rule |
|---|---|---|
| `name`, `value` | `name`, `value` | required; record skipped (`reason: "no name"`) if `name` missing |
| `domain` | `domain` | kept verbatim, including any leading dot |
| `path` | `path` | default `"/"` if absent |
| `httpOnly` | `httpOnly` | bool |
| `sameSite` | `sameSite` | **resolved first**, case-insensitively: `no_restriction`→`None`, `lax`→`Lax`, `strict`→`Strict`; `unspecified`, empty string, or absent → field omitted |
| `secure` | `secure` | bool; **forced `true` when the resolved `sameSite == "None"`** (Chrome rejects `SameSite=None` without `Secure`). Computed *after* `sameSite` so the dependency order is unambiguous |
| `expirationDate` (epoch secs) | `expires` | passed through; if `session == true` or `expirationDate` absent → `expires` omitted (session cookie); if `expirationDate < now` → record skipped (`reason: "expired"`) |

The cookie's own `domain` is kept **verbatim including any leading dot** (CDP
wants the dotted domain). `primary_domain` is a *separate* derived value — the
most frequent cookie `domain` among accepted cookies with any leading dot
**stripped** — used only to build the auto-open URL `https://<primary_domain>/`.
The dot-stripping applies only to `primary_domain`, never to the cookies' own
`domain` fields.

This module is pure and isolated: input is a parsed list + a clock value, output
is a dataclass. It can be reasoned about and spot-checked without running the
server.

#### `server/cdp.py` (two thin helpers on `CDPClient`)

- `async def set_cookies(self, cookies: list[dict]) -> None` — `await
  self.cmd("Storage.setCookies", {"cookies": cookies})` on the browser socket
  (default browser context = the persistent profile).
- `async def open_url(self, url: str) -> None` — `await
  self.cmd("Target.createTarget", {"url": url})`. The existing
  `Target.targetCreated` handler auto-switches the screencast to the new tab and
  starts streaming for active viewers, so no extra wiring is needed. This is safe
  even when there is no current active page (cold start) — the handler covers
  that case — so the auto-open path needs no special guard.

#### `server/app.py` (new route)

```
POST /api/cookies/import     (gated by existing authed(request))
  body:  {"raw": "<pasted text>"}
  steps: 1. reject if body > 1 MB
         2. json.loads(raw); 400 if not a JSON array
         3. result = to_cdp_cookies(parsed, time.time())
         4. cdp.set_cookies(result.cdp_cookies)
         5. if result.primary_domain: cdp.open_url(f"https://{primary_domain}/")
  resp:  {"ok": true, "domain": <primary_domain>, "set": <int>,
          "skipped": [{name, reason}...], "opened": <url|null>}
         or {"error": "<message>"} with 400
```

```
POST /api/cookies/clear      (gated by authed)
  steps: cdp.cmd("Storage.clearCookies")   # browser-wide
  resp:  {"ok": true}
```

#### Frontend (`static/index.html`, `style.css`, `app.js`)

- A **🍪 Import session** button in the header (next to the brand/hint), opening
  a modal overlay matching the existing dark theme.
- Modal contents: a `<textarea>` paste box, an **Import** primary button, a
  small one-line hint ("Export with Cookie-Editor → paste here"), a result area,
  and a separate **Clear all cookies** danger button that requires a confirm
  (clears every site's login in the hosted profile).
- `app.js`: on Import, send `{"raw": textarea.value}` to `/api/cookies/import`,
  render the summary (`Imported N cookies for <domain>`, plus any skipped), and
  close the modal. On Clear, confirm then `POST /api/cookies/clear`.

### Data flow

```
Cookie-Editor (local browser) --export JSON--> clipboard
  --> paste into modal --> POST /api/cookies/import {raw}
      --> to_cdp_cookies() --> Storage.setCookies (persistent profile)
      --> Target.createTarget https://<domain>/
          --> targetCreated handler switches screencast
              --> live view shows logged-in page
```

## Error handling

- Body > 1 MB → 400 `{"error": "payload too large"}` (guard against accidental
  giant pastes).
- `raw` not valid JSON / not a list → 400 `{"error": "expected a JSON array of
  cookies (Cookie-Editor export)"}`.
- Empty array or zero accepted cookies → 200 with `set: 0` and the skipped list
  so the UI can explain why nothing happened (e.g. all expired).
- `Storage.setCookies` CDP failure → 502 `{"error": "could not set cookies"}`;
  the per-cookie rejection (rare with correct mapping) is logged at WARNING
  without values.
- All routes return the existing 307→/login redirect when unauthenticated.

## Security

- Both routes sit behind the existing password cookie (`authed(request)`).
- **Cookie values are never logged.** Logs carry only counts and the primary
  domain.
- Pasted cookies are live account credentials; they are written to the on-disk
  profile and persist until expiry or an explicit clear. This is inherent to the
  feature's goal. The Clear-all button is the deliberate teardown path. The
  README/help line will note that imported sessions grant full account access in
  the hosted browser.

## Testing

Per the project's prototype-tooling convention, **no test scaffolding** (`tests/`,
mocks, CI) is added. Verification is manual against the running server:

1. Export a real low-value login via Cookie-Editor, paste, Import; confirm the
   auto-opened tab shows the logged-in state in the live view.
2. Confirm a malformed paste returns a clear error and changes nothing.
3. Confirm Clear-all wipes the session (site shows logged-out after reload).

The `to_cdp_cookies` mapping (the only non-trivial logic) is pure and will be
spot-checked with a one-off invocation on a representative export during
implementation, not committed as a suite.

## Files touched

| File | Change |
|---|---|
| `server/cookies.py` | new — `to_cdp_cookies` + `ImportResult` |
| `server/cdp.py` | add `set_cookies()`, `open_url()` |
| `server/app.py` | add `POST /api/cookies/import`, `POST /api/cookies/clear` |
| `static/index.html` | header button + import modal markup |
| `static/style.css` | modal + button styles |
| `static/app.js` | modal open/close, import POST + render, clear+confirm |
| `README.md` | short "Import session" usage + security note |

## Out of scope (YAGNI)

- Browser-extension-based one-click push.
- Netscape `cookies.txt` and other export formats.
- Per-domain selective clearing (only browser-wide clear is provided).
- Encrypting cookies at rest in the profile (the profile is already the trust
  boundary for this single-user tool).
