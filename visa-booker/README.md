# Visa slot booker

Finds open US visa appointment dates and books one with a Tandem agent, in the
browser you're watching.

- **Finding slots:** `slot_monitor.py` polls the scheduling portal's own
  availability API (the OFC `schedule-days` / `schedule-entries` endpoints). It sends those requests from your logged-in Tandem
  tab over CDP, so they use the session you already have. No alert emails, no
  third-party slot sites, and nothing is copied out of the browser.
- **Booking:** when the monitor sees dates, `booker.py` starts a Tandem agent
  session with a step-by-step booking instruction (`booking_instruction.md` plus
  the lessons in `booking_playbook.md`), then reads the agent's `RESULT_CODE:`
  line.

Use it only for your own application, on your own account, within the portal's
terms. The monitor won't poll faster than every 10 s and stops on 401/403/429.
It's paused while the agent books, so the two don't share the portal's
per-account calendar quota.

## Setup

1. Run Tandem (`./run.sh` in the repo root) and log in to the portal in its
   browser. Leave a tab open on the portal.
2. `cp visa-booker/config.example.json visa-booker/config.json` and fill it in.
   `config.json` is git-ignored. It holds your credentials, so keep it private.
3. Fill in the `monitor` block from your own browser: open DevTools → Network
   on the OFC calendar page, pick a location, and copy the availability request
   (URL, method, query params, form body) into `base_url` / `method` / `params` / `data`.
   `{endpoint}` in `base_url` is replaced by each entry of `endpoints`.
   `via_browser` is Tandem's Chrome DevTools URL (`http://localhost:$CDP_PORT`).
4. Check it with a single poll:

   ```bash
   python visa-booker/slot_monitor.py --via-browser http://localhost:9222 \
     --base-url '<base_url>' --method POST --data '<data>' --count 1 --delay 10
   ```

   Look at `available_slots.txt` to see exactly what the portal returned.

## Run

Two ways, depending on how much you want to watch.

**Headless daemon.** Monitor and booking run unattended:

```bash
DRY_RUN=1 python visa-booker/booker.py run   # rehearse: logs the instruction, never fires
python visa-booker/booker.py run             # for real
python visa-booker/booker.py render          # print the instruction the agent would get
python visa-booker/booker.py fire            # start a booking attempt right now
python visa-booker/booker.py run --no-monitor  # only tail events.jsonl (bring your own slot source)
```

**Claude Code agent.** One agent watches and books, and you can talk to it as it works:

```bash
claude --agent visa-slot-booker --mcp-config .mcp.json
```

The agent definition is in `.claude/agents/visa-slot-booker.md`. It runs
`slot_monitor.py` in a loop and books in the shared browser once it reports slots.

## Config reference

| key | meaning |
|-----|---------|
| `schedule_url` | OFC scheduling page. Add `?reschedule=true` (or set `reschedule: true`) to move an existing appointment instead of booking a new one. |
| `locations` | OFC locations to try, in order. Each is checked once per attempt. |
| `auto_confirm` | `false` (default): the agent stops at the final confirm page and asks you. `true`: it submits. |
| `security_answers` | keyword → answer. The agent never guesses an unlisted question. |
| `cooldown_minutes` | after an attempt, ignore new sightings for this long. |
| `rate_limit_hold_minutes` | after a PSE0501 / 1015 / WAF result, pause auto-booking for this long. |
| `notify_webhook` | optional URL. Each status update is POSTed as `{"text": …, "content": …}` (works with Slack and Discord webhooks). |

## Files

| file | what it is |
|------|------------|
| `slot_monitor.py` | bounded availability poller: offline simulation by default, `--live` / `--via-browser` to poll for real |
| `booker.py` | monitor → guards → Tandem agent → result |
| `booking_instruction.md` | instruction template (`{{placeholders}}` filled from config) |
| `booking_playbook.md` | portal quirks learned from past runs, injected into every attempt |
| `config.example.json` | config template |
