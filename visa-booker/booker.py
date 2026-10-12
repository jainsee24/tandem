"""Visa slot booker: slot_monitor.py finds open dates → Tandem agent books them.

    python booker.py run                 start the monitor and book on the first sighting
    python booker.py run --no-monitor    only tail events.jsonl (feed it from any source)
    python booker.py fire '<event json>' book one sighting now (same guards as `run`)
    python booker.py render              print the instruction the agent would get

Slots are found by polling the portal's own availability API with your own
logged-in session (slot_monitor.py), not by alert emails or third-party slot
sites. Every sighting is appended to events.jsonl; when one passes the guards
below, a Tandem agent session is started (POST /api/tasks) with a fully
specified booking instruction rendered from booking_instruction.md.

Safety design:
  * One-shot `booked` latch — never books twice.
  * `in_progress` mutex — one attempt at a time (Tandem runs one session anyway).
  * Per-location cooldown — a sighting that just failed doesn't re-fire every poll.
  * Rate-limit hold — a PSE0501 / 1015 / WAF result pauses auto-booking, since
    every further calendar hit extends the lockout.
  * The monitor is paused (SIGSTOP) while the agent books, so the two never
    compete for the same per-account calendar quota.
  * The agent must end with a machine-readable `RESULT_CODE:` line.

Env overrides: CONFIG_FILE, EVENTS_LOG, STATE_FILE, DRY_RUN=1 (log the
instruction, never touch Tandem).
"""

import argparse
import json
import logging
import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

HERE = Path(__file__).parent
CONFIG_FILE = Path(os.environ.get("CONFIG_FILE", HERE / "config.json"))
EVENTS_LOG = Path(os.environ.get("EVENTS_LOG", HERE / "events.jsonl"))
STATE_FILE = Path(os.environ.get("STATE_FILE", HERE / ".booker_state.json"))
TEMPLATE_FILE = HERE / "booking_instruction.md"
PLAYBOOK_FILE = HERE / "booking_playbook.md"
DRY_RUN = os.environ.get("DRY_RUN") == "1"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("visa-booker")


# ---------------------------------------------------------------------------
# Config + state
# ---------------------------------------------------------------------------
def load_config():
    with open(CONFIG_FILE) as f:
        return json.load(f)


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        # start from the end of the log so old sightings don't fire
        offset = EVENTS_LOG.stat().st_size if EVENTS_LOG.exists() else 0
        return {"offset": offset, "booked": False, "in_progress": False,
                "last_fired": {}, "history": []}


def save_state(state):
    tmp = STATE_FILE.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(state, f, indent=1)
    tmp.replace(STATE_FILE)


# ---------------------------------------------------------------------------
# The booking instruction handed to the Tandem agent
# ---------------------------------------------------------------------------
def is_reschedule(cfg):
    flag = cfg.get("reschedule")
    if flag is not None:
        return bool(flag)
    return "reschedule=true" in (cfg.get("schedule_url") or "")


def load_playbook():
    try:
        txt = PLAYBOOK_FILE.read_text()
    except OSError:
        return ""
    if "<!-- LESSONS:BEGIN -->" in txt and "<!-- LESSONS:END -->" in txt:
        txt = txt.split("<!-- LESSONS:BEGIN -->", 1)[1].split("<!-- LESSONS:END -->", 1)[0]
    return txt.strip()


def describe_event(ev):
    if not ev:
        return "(manual run — no monitor sighting; check every listed location)"
    lines = [f"{ev.get('label') or ev.get('endpoint')}: {ev.get('slot_count')} slot(s), "
             f"earliest {ev.get('earliest_date') or 'unknown'}"]
    for slot in (ev.get("slots") or [])[:10]:
        lines.append("  " + json.dumps(slot, ensure_ascii=False)[:200])
    return "\n".join(lines)


def build_instruction(ev, cfg):
    resched = is_reschedule(cfg)
    lessons = load_playbook()
    name = cfg["applicant_name"]
    fields = {
        "headline": ("SPEED-CRITICAL US visa appointment RESCHEDULE" if resched
                     else "SPEED-CRITICAL US visa booking"),
        "slot_context": describe_event(ev),
        "playbook": (f"\nPLAYBOOK — hard-won lessons from past runs on this portal. "
                     f"Apply them:\n{lessons}\n" if lessons else ""),
        "visa_type": cfg["visa_type"],
        "applicant_name": name,
        "locations": ", ".join(cfg.get("locations") or []) or "any location the portal offers",
        "schedule_url": cfg["schedule_url"],
        "portal_url": cfg["portal_url"],
        "landing": ("the OFC RESCHEDULE page for the appointment already held" if resched
                    else "the OFC (VAC/biometrics) scheduling page"),
        "menu_item": ('"Reschedule Appointment" (if the menu has no such item, open '
                      '"Schedule Appointment" and pick the reschedule option there)'
                      if resched else '"Schedule Appointment"'),
        "reschedule_block": (f"""
RESCHEDULE MODE — {name} ALREADY HAS a confirmed appointment. This flow MOVES it; it never creates a second one. Every "book"/"submit" below means "reschedule the existing appointment onto the new date".
  - A portal warning that the current appointment will be cancelled/replaced is EXPECTED: proceed through it.
  - If no listed location has an available date, change NOTHING and report FAILED — the existing appointment stays intact.
""" if resched else ""),
        "mode_line": ("Book FULLY and AUTOMATICALLY — complete every final submit/confirm "
                      "yourself. Do NOT wait for me." if cfg.get("auto_confirm") else
                      "Fill everything up to the FINAL confirm page, then STOP and report "
                      "RESULT_CODE: NEEDS_INPUT so I can confirm before submitting."),
        "username": cfg["username"],
        "password": cfg["password"],
        "security_answers": "\n".join(
            f'   - If asked "{q}" (or similar), answer: {a}'
            for q, a in (cfg.get("security_answers") or {}).items()) or "   (none configured)",
        "safety_line": ("Reschedule EXACTLY ONE appointment set. Never create an additional one."
                        if resched else
                        "Book EXACTLY ONE complete appointment set (one OFC + its consular "
                        "interview). Never two."),
    }
    text = TEMPLATE_FILE.read_text()
    return re.sub(r"\{\{(\w+)\}\}", lambda m: str(fields.get(m.group(1), m.group(0))), text)


# ---------------------------------------------------------------------------
# Tandem + notifications
# ---------------------------------------------------------------------------
def tandem_session(cfg):
    s = requests.Session()
    s.post(f"{cfg['tandem_url']}/login", data={"password": cfg["tandem_password"]},
           timeout=15, allow_redirects=False)
    return s


def fire_tandem(cfg, instruction):
    s = tandem_session(cfg)
    r = s.post(f"{cfg['tandem_url']}/api/tasks",
               json={"instruction": instruction, "model": cfg.get("model") or ""}, timeout=30)
    r.raise_for_status()
    return s, r.json().get("id")


def wait_for_result(s, cfg, task_id):
    deadline = time.monotonic() + cfg.get("attempt_timeout_minutes", 30) * 60
    while time.monotonic() < deadline:
        time.sleep(15)
        try:
            cur = s.get(f"{cfg['tandem_url']}/api/tasks", timeout=15).json().get("current") or {}
        except Exception as e:
            log.warning("poll error: %s", e)
            continue
        if cur.get("id") and cur.get("id") != task_id:
            return "preempted", "tandem session was replaced by another task"
        if cur.get("id") == task_id and cur.get("status") in ("ready", "stopped", "failed"):
            return cur.get("status"), cur.get("result") or ""
    return "timeout", ""


def parse_result_code(text):
    for line in reversed((text or "").splitlines()):
        line = line.strip().lstrip("*# ").strip()
        if line.upper().startswith("RESULT_CODE:"):
            body = line.split(":", 1)[1].strip()
            code = body.split()[0].strip(" —-").upper() if body else ""
            return code, body
    return None, ""


def notify(cfg, message):
    """Log, and POST to `notify_webhook` if set (Slack/Discord/ntfy-style)."""
    log.info("NOTIFY: %s", message)
    url = cfg.get("notify_webhook")
    if not url:
        return
    try:
        requests.post(url, json={"text": message, "content": message}, timeout=20)
    except Exception as e:
        log.warning("notify failed: %s", e)


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------
# The portal melts (524/5xx) right when slots open. A FAILED for one of these
# is the site's outage, not a verdict on the slot, so re-fire once after a pause.
TRANSIENT_RE = re.compile(
    r"(?i)\b5[0-2][0-9]\b|timed?[ -]?out|never loaded|did ?n[o']t load|"
    r"blank page|unreachable|connection (reset|refused|error)|error id\b")

# Per-account throttles: every further calendar hit extends them. Hold instead.
RATE_LIMIT_RE = re.compile(
    r"(?i)exceeded the limit|PSE0501|\b1015\b|rate.?limit|have been blocked|WAF")


def is_rate_limited(detail):
    return bool(RATE_LIMIT_RE.search(detail or ""))


def is_transient_failure(code, detail):
    return code == "FAILED" and not is_rate_limited(detail) and bool(TRANSIENT_RE.search(detail or ""))


def hold_minutes_left(state):
    until = state.get("hold_until")
    if not until:
        return 0
    left = (datetime.fromisoformat(until) - datetime.now(timezone.utc)).total_seconds()
    return int(left // 60) + 1 if left > 0 else 0


def cooldown_active(key, cfg, state):
    ts = state["last_fired"].get(key)
    if not ts:
        return False
    age = (datetime.now(timezone.utc) - datetime.fromisoformat(ts)).total_seconds()
    return age < cfg.get("cooldown_minutes", 10) * 60


def suppressed_reason(ev, cfg, state):
    key = ev.get("label") or ev.get("endpoint") or "manual"
    if state.get("booked"):
        return "already booked (latch on)"
    if state.get("in_progress"):
        return "an attempt is already in progress"
    left = hold_minutes_left(state)
    if left:
        return f"rate-limit hold for {left} more min"
    if cooldown_active(key, cfg, state):
        return f"{key} cooldown active"
    return None


# ---------------------------------------------------------------------------
# One attempt
# ---------------------------------------------------------------------------
def attempt_booking(ev, cfg, state, monitor=None):
    key = ev.get("label") or ev.get("endpoint") or "manual"
    instruction = build_instruction(ev, cfg)
    if DRY_RUN:
        log.info("[DRY_RUN] would fire Tandem for %s. Instruction:\n%s", key, instruction)
        return

    notify(cfg, f"🚨 {cfg['visa_type']} slot seen ({key}, earliest "
                f"{ev.get('earliest_date') or '?'}). Tandem agent is booking now — "
                f"watch {cfg['tandem_url']}")
    state["in_progress"] = True
    state["last_fired"][key] = datetime.now(timezone.utc).isoformat()
    save_state(state)
    if monitor:
        monitor.pause()

    code = detail = status = None
    try:
        for attempt in range(1 + cfg.get("transient_retries", 1)):
            s, task_id = fire_tandem(cfg, instruction)
            status, result = wait_for_result(s, cfg, task_id)
            code, detail = parse_result_code(result)
            if not (is_transient_failure(code, detail) and attempt < cfg.get("transient_retries", 1)):
                break
            delay = cfg.get("transient_retry_delay_seconds", 75)
            notify(cfg, f"🔁 portal outage ({detail[:100]}) — re-firing in {delay}s.")
            time.sleep(delay)
    except Exception as e:
        log.exception("attempt errored")
        status, detail = "error", str(e)
    finally:
        state["in_progress"] = False
        if monitor:
            monitor.resume()

    state["history"].append({"ts": datetime.now(timezone.utc).isoformat(), "key": key,
                             "date": ev.get("earliest_date"), "status": status,
                             "code": code, "detail": detail})
    if code == "BOOKED":
        state["booked"] = True
        notify(cfg, f"✅ VISA BOOKED — {detail}\nAuto-booking is now off (booked latch). "
                    f"Verify at {cfg['portal_url']}")
    elif code == "NEEDS_INPUT":
        notify(cfg, f"🟡 The agent needs you: {detail}\nOpen {cfg['tandem_url']} — "
                    f"the live session is waiting.")
    elif is_rate_limited(detail):
        until = datetime.now(timezone.utc) + timedelta(minutes=cfg.get("rate_limit_hold_minutes", 45))
        state["hold_until"] = until.isoformat()
        notify(cfg, f"❌ Rate-limited by the portal ({(detail or '')[:120]}). Auto-booking "
                    f"is on hold until {until.astimezone().strftime('%H:%M %Z')}; don't open "
                    f"the scheduling page by hand before then either.")
    else:
        notify(cfg, f"❌ Booking did not complete ({code or status}): "
                    f"{detail or 'see Tandem'}. Still monitoring.")
    save_state(state)


# ---------------------------------------------------------------------------
# Monitor child + event tail
# ---------------------------------------------------------------------------
class Monitor:
    """slot_monitor.py as a child process writing sightings to EVENTS_LOG."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.proc = None

    def command(self):
        m = self.cfg["monitor"]
        cmd = [sys.executable, str(HERE / "slot_monitor.py"),
               "--base-url", m["base_url"], "--method", m.get("method", "GET"),
               "--events", str(EVENTS_LOG), "--output", str(HERE / "available_slots.txt"),
               "--label", m.get("label") or ", ".join(self.cfg.get("locations") or []),
               "--delay", str(m.get("delay_seconds", 60)), "--count", "1", "--forever"]
        for ep in m.get("endpoints") or []:
            cmd += ["--endpoint", ep]
        for k, v in (m.get("params") or {}).items():
            cmd += ["--param", f"{k}={v}"]
        if m.get("data") is not None:
            cmd += ["--data", m["data"]]
        cmd += ["--via-browser", m["via_browser"]] if m.get("via_browser") else ["--live"]
        return cmd

    def ensure_running(self):
        if self.proc and self.proc.poll() is None:
            return
        if self.proc is not None:
            rc = self.proc.returncode
            notify(self.cfg, f"⚠️ slot monitor exited ({rc}) — "
                             + ("the portal session looks logged out or rate-limited; log in "
                                f"again in Tandem ({self.cfg['tandem_url']})."
                                if rc == 2 else "restarting in 5 min."))
            time.sleep(300)
        self.proc = subprocess.Popen(self.command(), cwd=str(HERE))
        log.info("monitor started (pid %s)", self.proc.pid)

    def pause(self):
        if self.proc and self.proc.poll() is None:
            self.proc.send_signal(signal.SIGSTOP)

    def resume(self):
        if self.proc and self.proc.poll() is None:
            self.proc.send_signal(signal.SIGCONT)

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.resume()
            self.proc.terminate()


def read_new_events(state):
    if not EVENTS_LOG.exists():
        return
    if EVENTS_LOG.stat().st_size < state["offset"]:   # rotated/truncated
        state["offset"] = 0
    with open(EVENTS_LOG) as f:
        f.seek(state["offset"])
        while True:
            line = f.readline()
            if not line or not line.endswith("\n"):
                break
            state["offset"] = f.tell()
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def run(with_monitor):
    cfg = load_config()
    state = load_state()
    state["in_progress"] = False      # a crash mid-attempt must not wedge the mutex
    monitor = Monitor(cfg) if with_monitor else None
    log.info("visa-booker up: booked=%s locations=%s auto_confirm=%s dry_run=%s monitor=%s",
             state["booked"], cfg.get("locations"), cfg.get("auto_confirm"), DRY_RUN,
             "on" if monitor else "off (tailing %s)" % EVENTS_LOG)
    try:
        while True:
            if monitor and not state.get("booked"):
                monitor.ensure_running()
            for ev in read_new_events(state):
                reason = suppressed_reason(ev, cfg, state)
                if reason:
                    log.info("sighting %s ignored — %s", ev.get("label"), reason)
                    continue
                attempt_booking(ev, cfg, state, monitor)
            if state.get("booked") and monitor:
                monitor.stop()
            save_state(state)
            time.sleep(5)
    finally:
        if monitor:
            monitor.stop()


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--no-monitor", action="store_true")
    f = sub.add_parser("fire")
    f.add_argument("event", nargs="?", default="{}")
    sub.add_parser("render")
    args = ap.parse_args()

    if args.cmd == "run":
        run(not args.no_monitor)
    elif args.cmd == "render":
        print(build_instruction({}, load_config()))
    else:
        cfg, state = load_config(), load_state()
        ev = json.loads(args.event)
        reason = suppressed_reason(ev, cfg, state)
        if reason:
            print(f"SUPPRESSED: {reason}")
            return 3
        attempt_booking(ev, cfg, state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
