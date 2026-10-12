# Booking playbook — lessons from past runs

The booker injects the LESSONS block into every booking instruction. Add a
lesson after each attempt that taught you something; keep each ONE actionable
line, most useful first. These are portal quirks, not personal details.

<!-- LESSONS:BEGIN -->
- GO DIRECTLY TO /en-US/ofc-schedule/ FIRST (fastest): a logged-in session lands straight on the calendar. ONLY if it returns Cloudflare "Sorry, you have been blocked" (WAF), fall back to portal home → click Schedule/Reschedule Appointment — the in-page click carries the referer and passes.
- A page saying "You're offline. This is a read only version" is the portal's service-worker cache, NOT a real response — reload once before trusting anything on it.
- A "Just a moment / Verifying you are human" page MAY have a checkbox, and the checkbox is INVISIBLE to browser_snapshot — ALWAYS browser_take_screenshot FIRST and LOOK. Checkbox shown → CLICK it (~21px from the widget's left edge, vertically centered), it never self-clears. NO widget (bare spinner) → it self-solves: wait_for 8 + SCREENSHOT AGAIN each cycle (a checkbox often appears seconds in → click it), up to ~5×; never wait_for without screenshotting. Deciding "no checkbox, just wait" from the title/snapshot made a run wait forever on a click-needing page.
- PORTAL 500/502/503/524/"Error ID"/blank fires EXACTLY when slots open (everyone piles on). It is transient — persist: reload home every ~20s for up to ~10 min; a plain F5 often works where a fresh navigation times out. A human refreshing got through while the agent had quit. Do NOT report FAILED early.
- "You have exceeded the limit for viewing this page" / PSE0501 is the PORTAL's PER-ACCOUNT calendar throttle — every reload EXTENDS it (13 reloads/10 min kept it lit). Wait 90s, ONE menu click-through, else FAILED — rate limit. The bridge then holds further attempts.
- CLOUDFLARE 1015 is a rate-limit on US specifically: wait 60s, reload ONCE, third strike → FAILED. Rapid retries earned a long ban.
- A TOOL TIMEOUT (navigate/click/wait_for/screenshot "Timeout exceeded") is usually the portal origin hanging ~100s before a 524, NOT a dead browser and NOT a failed click — a Turnstile click that times out actually WORKED. Wait_for 25 until calls succeed, snapshot, then reload home.
- LOGIN PERSISTS in the Chrome profile, so a booking usually lands already logged in — go straight to the /ofc-schedule/ deep link (menu click only if WAF-blocked). If the sign-in callback dies on 524/500, the session never established: reload home and sign in again (login attempts are capped PER DAY — never hammer).
- SPEED AT THE TIME-SLOT STEP MATTERS MOST: when a date is real, other bots race you at the time-slot pick and submit. Chain date→time→submit without extra snapshots; a slot seen can vanish mid-submit.
- SMALL-COUNT SIGHTINGS (~1-6 slots) have historically NOT converted (calendar already empty on arrival); the one success came from a 20-slot sighting. Still try, but expect empties on tiny counts.
- CHECK ONLY THE LISTED LOCATIONS, each at most once, never loop back "to be sure" — that wastes the seconds that decide the booking and risks the rate-limit.
<!-- LESSONS:END -->
