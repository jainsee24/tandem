{{headline}}. slot_monitor.py just saw open dates on the portal's own availability API and slots vanish in minutes. Act first, verify after. Minimize turns: chain actions without narrating.

WHAT THE MONITOR SAW (from the portal's schedule endpoints, seconds ago):
----------------------------------------
{{slot_context}}
----------------------------------------
{{playbook}}
  Visa: {{visa_type}}   |   Applicant: {{applicant_name}}
  Locations to try, in order: {{locations}}

YOUR FIRST ACTION — before ANY screenshot, check, or detour:
Navigate the current tab DIRECTLY to {{schedule_url}} — the fastest route to the calendar. If this browser is already logged in it lands straight on {{landing}}.
FALLBACK ONLY IF the direct hop does NOT land on the scheduling page:
  - "Sorry, you have been blocked" (Cloudflare WAF on a cold deep link): do NOT reload that URL. Load the portal HOME {{portal_url}}, then click {{menu_item}} in the left-hand menu — the in-page click opens the same page.
  - Sign-in form: log in (see LOGIN SCREEN below), then click {{menu_item}} from the home page.
  - "You're offline / read only" page: that is the portal's cached shell, not a real load — reload once.
Do NOT spoof headers, user agent, or IP — the portal's terms suspend accounts for that.
{{reschedule_block}}
THEN, AS FAST AS POSSIBLE (other applicants are grabbing these dates right now):
1. START WITH VAC/OFC (biometrics). In the OFC post / location dropdown select the first location from the list above. Calendar loads → if ANY date is available, click the earliest one, click the first available time, and submit. Any date, any time qualifies — never compare, never scroll for "better".
2. NO DATES THERE? Switch the dropdown to the NEXT location in the list. CHECK EACH LOCATION AT MOST ONCE — never re-select one you already checked, never loop back "to be sure". If none of them has a date, book nothing and report FAILED with what each calendar showed.
3. OFC submitted → WITHOUT PAUSING continue to the CONSULAR (interview) step the portal offers next. Start with the consulate matching the OFC location; no dates there → try the other allowed consulates once each. Pick ANY available date the portal allows (it must be after the OFC date — the portal enforces this), first available time, submit.
4. FINISH THE WHOLE BOOKING: click through EVERY confirmation page until the portal actually shows the appointments as booked, and capture the confirmation number. A booking abandoned at a confirm screen is a lost slot.
5. {{mode_line}}

FALLBACKS — only when you actually hit them, never preemptively:
- LOGIN SCREEN: sign in ONCE — Portal {{portal_url}}, Username {{username}}, Password {{password}}. Solve image CAPTCHAs visually. Security questions (answer ONLY from this mapping, NEVER guess an unlisted one — report NEEDS_INPUT instead; a wrong answer can lock the account):
{{security_answers}}
  If the login callback dies on a Cloudflare 524 / blank page, reload the portal home and sign in again — the session was never established.
- SERVER ERRORS — Cloudflare 524, HTTP 500/502/503, "Error ID ...", blank/timeout pages: the PORTAL buckling under load, which happens exactly when slots open. It is not a block on you and it clears in bursts. PERSIST: refresh every ~20 seconds for up to 10 minutes (a plain F5 often works where a fresh navigation times out). Only report FAILED after ~10 minutes of continuous failure.
- "You have exceeded the limit for viewing this page" / PSE0501: the portal's per-account calendar throttle — every reload EXTENDS it. Do NOT reload. Wait 90s, go to the portal home and click through the menu ONCE more. Still there → STOP and report FAILED — rate limit exceeded (PSE0501).
- CLOUDFLARE "Error 1015" (rate-limited): wait 30s, reload ONCE. Still 1015 → STOP and report FAILED — cloudflare 1015. Rapid retries earn long bans.
- CLOUDFLARE "Just a moment…" / "Verify you are human": the checkbox is INVISIBLE to browser_snapshot — ALWAYS browser_take_screenshot first and LOOK. Checkbox shown → click it by pixel (~21px right of the widget's left edge, vertically centred); it never clears on its own. If it re-issues, click each new one promptly (up to ~8). No widget (bare spinner) → wait_for 8 then screenshot again, up to ~5 times; reload home once if still hung.
- Logged out mid-flow: log back in with the same credentials and continue where you were.

HARD SAFETY RULES:
  - {{safety_line}}
  - Each location gets checked ONCE. If nothing anywhere has an available date, book nothing — report FAILED with what you saw.
  - This is real; fees are non-refundable and a wrong booking can cancel an existing appointment. Fast, but precise.

WHEN DONE, end your final message with EXACTLY one of these lines (machine-read):
  RESULT_CODE: BOOKED — <location>, <OFC date + consular date>, <confirmation number or "no number shown">
  RESULT_CODE: FAILED — <one-line reason>
  RESULT_CODE: NEEDS_INPUT — <what you need from me>
