---
name: visa-slot-booker
description: Watches the US visa scheduling portal for open OFC/consular dates with visa-booker/slot_monitor.py (the portal's own availability API, via the logged-in Tandem tab), then books the first one in the shared Tandem browser. Use when the human asks to watch for, grab, or reschedule a visa appointment.
---

You find and book a US visa appointment for the human, on their own account, in
the shared Tandem browser they are watching live.

**How slots are found:** with `visa-booker/slot_monitor.py`, which polls the
portal's own availability endpoints from inside the already-logged-in Tandem tab.
Do NOT use alert emails, checkvisaslots.com, or any other third-party slot site,
and do NOT reload the portal's calendar yourself to look for dates — the portal
throttles calendar views per account (PSE0501) and every extra view extends it.

## Setup — once, at the start

1. Read `visa-booker/config.json`. If it is missing, tell the human to copy
   `visa-booker/config.example.json` to `config.json` and fill it in, then stop.
   Never print the password or security answers back in chat.
2. Load the browser tools with ONE ToolSearch call:
   `select:mcp__browser__browser_navigate,mcp__browser__browser_snapshot,mcp__browser__browser_click,mcp__browser__browser_type,mcp__browser__browser_take_screenshot,mcp__browser__browser_mouse_click_xy,mcp__browser__browser_wait_for,mcp__browser__browser_select_option`
3. Make sure a tab is open on the portal and logged in (`portal_url` from the
   config). If you see the sign-in form, log in once with the config's
   credentials; answer security questions ONLY from `security_answers` — an
   unlisted question means stop and ask the human.

## Watch loop

Build the monitor command from the config's `monitor` block and run it with Bash
(from the repo root):

```bash
python visa-booker/slot_monitor.py \
  --via-browser <monitor.via_browser> \
  --base-url '<monitor.base_url>' --method <monitor.method> \
  --endpoint <each of monitor.endpoints> \
  [--param k=v ...] [--data '<monitor.data>'] \
  --label '<locations joined by ", ">' \
  --events visa-booker/events.jsonl --output visa-booker/available_slots.txt \
  --count 5 --delay <monitor.delay_seconds>
```

It ends with a machine-readable line, and its exit code tells you what to do:

| Exit | Last line | Meaning → action |
|------|-----------|------------------|
| 0  | `RESULT: SLOTS_FOUND 0 …` | nothing open → run it again (same command) |
| 10 | `RESULT: SLOTS_FOUND n earliest=…` | dates are open → **book now** (below) |
| 2  | `RESULT: STOPPED http=401/403/429` | logged out or throttled → check the tab; re-login once if it's the sign-in form, otherwise stop and report |
| 0 with `status=network_error` in the log | e.g. "no open tab on …" in `available_slots.txt` | the portal tab was closed or navigated away → open `portal_url` again, then retry |

Keep looping until slots are found, the human stops you, or you hit a stop
condition above. Don't narrate each empty check — one short status line every
few runs is enough. The `available_slots.txt` journal has the raw responses if
you need to see what the portal returned.

## Booking — the moment the monitor reports slots

Read `visa-booker/booking_instruction.md` and `visa-booker/booking_playbook.md`
(the LESSONS block), and follow the instruction exactly, substituting the
`{{…}}` placeholders from the config yourself:

- `{{slot_context}}` = the monitor's `RESULT:` line plus the last line of `events.jsonl`
- `{{locations}}` = the config's `locations`, in order
- `{{schedule_url}}`, `{{portal_url}}`, `{{username}}`, `{{password}}`,
  `{{applicant_name}}`, `{{visa_type}}`, `{{security_answers}}` = straight from the config
- `reschedule: true` → you are MOVING an existing appointment, never creating a second one
- `auto_confirm: false` (the default) → stop at the final confirm page and ask the human

Speed matters at the date → time → submit step; verification matters after it.

## Finish

End with exactly one machine-readable line, as `booking_instruction.md` specifies:
`RESULT_CODE: BOOKED — …`, `RESULT_CODE: FAILED — …`, or `RESULT_CODE: NEEDS_INPUT — …`.
After a BOOKED result, stop watching — never book twice.
