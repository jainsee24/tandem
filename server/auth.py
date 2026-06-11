"""Password login with a signed session cookie."""
import hmac
import os
import secrets

from dotenv import load_dotenv
from itsdangerous import BadSignature, TimestampSigner

load_dotenv()

PASSWORD = os.environ.get("PASSWORD", "change-me")
COOKIE = "abh_session"
MAX_AGE = 7 * 86400
_signer = TimestampSigner(secrets.token_hex(32))  # sessions reset on restart


def check_password(candidate: str) -> bool:
    return hmac.compare_digest(candidate.encode(), PASSWORD.encode())


def make_cookie() -> str:
    return _signer.sign(b"ok").decode()


def cookie_valid(value: str | None) -> bool:
    if not value:
        return False
    try:
        _signer.unsign(value.encode(), max_age=MAX_AGE)
        return True
    except BadSignature:
        return False


LOGIN_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>Tandem</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" type="image/svg+xml" href="/static/favicon.svg">
<style>
  body {{ margin:0; height:100vh; display:grid; place-items:center;
         background:#0e1014; color:#e6e8ee;
         font-family:system-ui,-apple-system,sans-serif; }}
  form {{ background:#1a1d23; padding:2.4rem 2.6rem; border-radius:16px;
          border:1px solid #2a2e37; display:flex; flex-direction:column;
          gap:.9rem; min-width:300px; box-shadow:0 20px 60px rgba(0,0,0,.4); }}
  .head {{ display:flex; align-items:center; gap:11px; margin-bottom:.5rem; }}
  .head img {{ width:34px; height:34px; border-radius:9px; }}
  .head .t {{ display:flex; flex-direction:column; line-height:1.15; }}
  .head .t b {{ font-size:1.15rem; font-weight:700; }}
  .head .t span {{ font-size:.72rem; color:#8b919e; }}
  input {{ background:#111317; color:#e6e8ee; border:1px solid #2a2e37;
           border-radius:9px; padding:.7rem .85rem; font-size:.95rem; }}
  input:focus {{ outline:none; border-color:#6c8cff; }}
  button {{ background:#6c8cff; color:#0c0e12; border:none; border-radius:9px;
            padding:.7rem; font-weight:650; font-size:.95rem; cursor:pointer; }}
  button:hover {{ background:#809cff; }}
  .err {{ color:#ff7a7a; font-size:.85rem; min-height:1em; margin:0; }}
</style></head>
<body><form method="post" action="/login">
  <div class="head">
    <img src="/static/favicon.svg" alt="">
    <div class="t"><b>Tandem</b><span>one browser, two drivers</span></div>
  </div>
  <input type="password" name="password" placeholder="password" autofocus autocomplete="current-password">
  <p class="err">{error}</p>
  <button>Enter</button>
</form></body></html>"""
