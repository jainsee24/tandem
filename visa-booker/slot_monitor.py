#!/usr/bin/env python3
"""Configurable, bounded API response recorder for authorized endpoints.

Defaults to an offline simulation. The token generated here is a correlation ID,
not an authentication credential.

Used by the visa booker to FIND slots (instead of alert emails or third-party
slot-tracking sites): it polls the scheduling portal's own availability
endpoints with YOUR OWN logged-in session and reports any dates it sees.

Two live transports:
  --live                 plain HTTPS requests (for APIs that accept them)
  --via-browser CDP_URL  runs fetch() inside the logged-in Tandem tab, so the
                         request carries that tab's own session — nothing is
                         copied out of the browser or spoofed.

Exit codes: 0 no slots seen, 10 slots seen, 2 stopped on 401/403/429.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import logging
import os
import random
import secrets
import threading
import time
import urllib.parse
import urllib.request
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

try:
    import requests
except ImportError:
    requests = None

try:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
except ImportError:
    HKDF = None
    hashes = None

try:
    from websockets.sync.client import connect as ws_connect
except ImportError:
    ws_connect = None

LOG = logging.getLogger('slot_monitor')
EXIT_SLOTS = 10
EXIT_STOPPED = 2
ENDPOINTS = (
    'get-family-ofc-schedule-days',
    'get-family-ofc-schedule-entries',
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


class Status(str, Enum):
    SUCCESS = 'success'
    HTTP_ERROR = 'http_error'
    NETWORK_ERROR = 'network_error'
    SIMULATED = 'simulated'


@dataclass(frozen=True)
class Settings:
    base_url: str = ('https://www.usvisascheduling.com/en-US/custom-actions/'
                     '?route=/api/v1/schedule-group/{endpoint}')
    output: Path = Path('available_slots.txt')
    count: int = 10
    delay: float = 15.0
    timeout: float = 20.0
    max_retries: int = 2
    live: bool = False
    endpoints: tuple[str, ...] = ENDPOINTS
    method: str = 'GET'
    params: tuple[tuple[str, str], ...] = ()
    data: str | None = None
    via_browser: str | None = None
    events: Path | None = None
    label: str = ''
    forever: bool = False

    def validate(self) -> None:
        if not 1 <= self.count <= 100:
            raise ValueError('count must be between 1 and 100')
        if self.delay < 1 or self.timeout <= 0:
            raise ValueError('delay must be >= 1 and timeout must be positive')
        if self.live and self.delay < 10:
            # Portals throttle calendar views per account; every extra hit
            # extends the lockout. Don't let a typo hammer someone's account.
            raise ValueError('live mode requires delay >= 10 seconds')
        if not 0 <= self.max_retries <= 3:
            raise ValueError('max_retries must be between 0 and 3')
        if self.live and not self.base_url.startswith('https://'):
            raise ValueError('live mode requires an HTTPS endpoint')
        if self.method not in ('GET', 'POST'):
            raise ValueError('method must be GET or POST')

    def url_for(self, endpoint: str) -> str:
        """`{endpoint}` in base_url is substituted (for portals that route by
        query string); otherwise the endpoint is appended as a path segment."""
        if '{endpoint}' in self.base_url:
            url = self.base_url.replace('{endpoint}', endpoint)
        else:
            url = self.base_url.rstrip('/') + '/' + endpoint
        if self.params:
            url += ('&' if '?' in url else '?') + urllib.parse.urlencode(self.params)
        return url


class SecureRequestTokenGenerator:
    """Produces 128-bit opaque correlation IDs; NOT server auth tokens."""

    def __init__(self) -> None:
        if HKDF is None:
            raise RuntimeError('Install cryptography: pip install cryptography')
        self._master = secrets.token_bytes(32)
        self._session = uuid.uuid4().bytes
        self._counter = 0
        self._lock = threading.Lock()

    def generate(self) -> str:
        with self._lock:
            self._counter += 1
            counter = self._counter
        context = b''.join((
            self._session,
            os.getpid().to_bytes(8, 'big'),
            counter.to_bytes(16, 'big'),
            time.time_ns().to_bytes(16, 'big'),
            time.monotonic_ns().to_bytes(16, 'big'),
            secrets.token_bytes(32),
        ))
        key = HKDF(
            algorithm=hashes.SHA512(), length=64,
            salt=secrets.token_bytes(32),
            info=b'slot-monitor/request-id/v1',
        ).derive(self._master)
        digest = hashlib.blake2b(context, key=key[:64], digest_size=32).digest()
        mac = hmac.new(key[32:], digest + context, hashlib.sha256).digest()
        return hashlib.sha3_256(mac + secrets.token_bytes(32)).digest()[:16].hex()


@dataclass(frozen=True)
class RequestRecord:
    index: int
    timestamp: str
    endpoint: str
    request_id: str
    status: Status
    http_status: int | None
    elapsed_ms: int
    slots: list[dict[str, Any]] = field(default_factory=list)
    response: Any = None
    error: str | None = None


class SlotExtractor:
    """Conservative parser: retains candidates without inventing availability."""

    @staticmethod
    def extract(data: Any) -> list[dict[str, Any]]:
        if not isinstance(data, (dict, list)):
            return []
        candidates: list[dict[str, Any]] = []
        queue = [data]
        visited = 0
        while queue and visited < 1000:
            item = queue.pop(0)
            visited += 1
            if isinstance(item, list):
                queue.extend(item[:200])
            elif isinstance(item, dict):
                lowered = {str(k).lower(): k for k in item}
                if any(k in lowered for k in ('date', 'time', 'available', 'starttime')):
                    candidates.append(item)
                else:
                    queue.extend(v for v in item.values() if isinstance(v, (dict, list)))
        return candidates[:100]


class TextJournal:
    """Append-only text journal with process-local serialization."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, record: RequestRecord) -> None:
        payload = asdict(record)
        payload['status'] = record.status.value
        block = (
            '=' * 72 + '\n'
            + f"SLOT CHECK #{record.index} | {record.timestamp}\n"
            + f"Endpoint: {record.endpoint}\n"
            + f"Request ID: {record.request_id}\n"
            + f"Status: {record.status.value} | HTTP: {record.http_status}\n"
            + f"Elapsed: {record.elapsed_ms}ms | Candidates: {len(record.slots)}\n"
            + '-' * 72 + '\n'
            + json.dumps(payload, indent=2, default=str, ensure_ascii=False)
            + '\n\n'
        )
        with self._lock:
            with self.path.open('a', encoding='utf-8') as f:
                f.write(block)
                f.flush()
                os.fsync(f.fileno())


class BrowserTransport:
    """Runs fetch() inside an already-open tab of the Tandem Chrome (via CDP).

    The tab must be on the portal's origin and logged in; the request then goes
    out exactly as the page's own XHRs do, with the session it already holds.
    """

    def __init__(self, cdp_http: str, origin: str) -> None:
        if ws_connect is None:
            raise RuntimeError('Install websockets: pip install websockets')
        self.cdp_http = cdp_http.rstrip('/')
        self.origin = origin

    def _page_ws(self) -> str:
        with urllib.request.urlopen(self.cdp_http + '/json/list', timeout=10) as r:
            targets = json.load(r)
        for t in targets:
            if t.get('type') == 'page' and str(t.get('url', '')).startswith(self.origin):
                return t['webSocketDebuggerUrl']
        raise RuntimeError(
            f'no open tab on {self.origin} — open the portal in Tandem and log in first')

    def fetch(self, url: str, method: str, headers: dict[str, str],
              body: str | None, timeout: float) -> tuple[int, str]:
        js = (
            '(async () => {'
            f' const r = await fetch({json.dumps(url)}, {{method: {json.dumps(method)},'
            f' headers: {json.dumps(headers)}, body: {json.dumps(body)},'
            ' credentials: "include"});'
            ' return JSON.stringify({status: r.status, text: (await r.text()).slice(0, 200000)});'
            '})()'
        )
        with ws_connect(self._page_ws(), max_size=None, open_timeout=10) as ws:
            ws.send(json.dumps({'id': 1, 'method': 'Runtime.evaluate', 'params': {
                'expression': js, 'awaitPromise': True, 'returnByValue': True,
                'timeout': int(timeout * 1000)}}))
            deadline = time.monotonic() + timeout + 5
            while time.monotonic() < deadline:
                msg = json.loads(ws.recv(timeout=max(0.1, deadline - time.monotonic())))
                if msg.get('id') != 1:
                    continue
                res = msg.get('result', {})
                if 'exceptionDetails' in res:
                    raise ConnectionError(res['exceptionDetails'].get('text', 'fetch failed'))
                out = json.loads(res['result']['value'])
                return int(out['status']), out['text']
        raise TimeoutError('browser fetch timed out')


class APIClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.session = (requests.Session()
                        if settings.live and not settings.via_browser and requests else None)
        self.browser = None
        if settings.via_browser:
            parts = urllib.parse.urlsplit(settings.base_url)
            self.browser = BrowserTransport(settings.via_browser,
                                            f'{parts.scheme}://{parts.netloc}')
        self.token_generator = SecureRequestTokenGenerator()

    def close(self) -> None:
        if self.session is not None:
            self.session.close()

    def __enter__(self) -> 'APIClient':
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _mock(self, endpoint: str, index: int) -> dict[str, Any]:
        return {
            'source': 'SIMULATED_NOT_REAL_AVAILABILITY',
            'endpoint': endpoint,
            'request_number': index,
            'availableDates': [],
            'slots': [],
        }

    def _send(self, url: str, headers: dict[str, str]) -> tuple[int, str]:
        if self.browser is not None:
            return self.browser.fetch(url, self.settings.method, headers,
                                      self.settings.data, self.settings.timeout)
        if requests is None:
            raise RuntimeError('Install requests: pip install requests')
        headers = {**headers, 'User-Agent': 'AuthorizedSlotRecorder/1.0'}
        response = self.session.request(self.settings.method, url, headers=headers,
                                        data=self.settings.data,
                                        timeout=self.settings.timeout)
        return response.status_code, response.text

    def execute(self, endpoint: str, index: int) -> RequestRecord:
        request_id = self.token_generator.generate()
        start = time.monotonic()
        response_data: Any = None
        code: int | None = None
        error: str | None = None
        status = Status.SIMULATED

        if not self.settings.live:
            response_data = self._mock(endpoint, index)
            code = 200
        else:
            # Use only documented, authorized APIs and your own valid credentials.
            # No server-issued scheduling Token is synthesized or guessed here.
            headers = {
                'Accept': 'application/json',
                'X-Request-ID': request_id,
            }
            if self.settings.data is not None:
                headers['Content-Type'] = 'application/x-www-form-urlencoded; charset=UTF-8'
            url = self.settings.url_for(endpoint)
            for attempt in range(self.settings.max_retries + 1):
                try:
                    code, text = self._send(url, headers)
                    try:
                        response_data = json.loads(text)
                    except ValueError:
                        response_data = {'raw_text': text[:20000]}
                    status = Status.SUCCESS if 200 <= code < 400 else Status.HTTP_ERROR
                    if code in (401, 403, 429):
                        error = 'Authentication, authorization, or rate-limit stop'
                        break
                    if code not in (500, 502, 503, 504) or attempt == self.settings.max_retries:
                        break
                except Exception as exc:  # network / CDP failure
                    status = Status.NETWORK_ERROR
                    error = f'{type(exc).__name__}: {exc}'
                    if attempt == self.settings.max_retries:
                        break
                time.sleep(min(30.0, 2 ** attempt + random.uniform(0.0, 0.5)))

        return RequestRecord(
            index=index,
            timestamp=utc_now(),
            endpoint=endpoint,
            request_id=request_id,
            status=status,
            http_status=code,
            elapsed_ms=int((time.monotonic() - start) * 1000),
            slots=SlotExtractor.extract(response_data),
            response=response_data,
            error=error,
        )


def earliest_date(slots: list[dict[str, Any]]) -> str | None:
    dates = []
    for slot in slots:
        for k, v in slot.items():
            if 'date' in str(k).lower() or str(k).lower() == 'starttime':
                if isinstance(v, str) and v:
                    dates.append(v)
    return min(dates) if dates else None


class EventLog:
    """One JSON line per check that saw real slots — what booker.py tails."""

    def __init__(self, path: Path, label: str) -> None:
        self.path = path
        self.label = label
        path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, record: RequestRecord) -> None:
        event = {
            'ts': record.timestamp,
            'label': self.label,
            'endpoint': record.endpoint,
            'slot_count': len(record.slots),
            'earliest_date': earliest_date(record.slots),
            'slots': record.slots[:20],
        }
        with self.path.open('a', encoding='utf-8') as f:
            f.write(json.dumps(event, default=str, ensure_ascii=False) + '\n')


def _kv(text: str) -> tuple[str, str]:
    key, sep, value = text.partition('=')
    if not sep:
        raise argparse.ArgumentTypeError(f'expected key=value, got {text!r}')
    return key, value


def parse_args() -> Settings:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--base-url', default=Settings.base_url,
                        help='endpoint prefix; may contain {endpoint}')
    parser.add_argument('--endpoint', action='append', dest='endpoints',
                        help='endpoint name (repeatable); default: the two OFC schedule endpoints')
    parser.add_argument('--method', default='GET', choices=('GET', 'POST'))
    parser.add_argument('--param', action='append', type=_kv, default=[],
                        help='query parameter key=value (repeatable)')
    parser.add_argument('--data', help='request body for POST (form-encoded string)')
    parser.add_argument('--output', type=Path, default=Path('available_slots.txt'))
    parser.add_argument('--events', type=Path, help='append a JSON line here whenever slots are seen')
    parser.add_argument('--label', default='', help='tag for events, e.g. the post/city being polled')
    parser.add_argument('--count', type=int, default=10)
    parser.add_argument('--forever', action='store_true',
                        help='ignore --count and keep checking until stopped (or a 401/403/429)')
    parser.add_argument('--delay', type=float, default=15.0)
    parser.add_argument('--timeout', type=float, default=20.0)
    parser.add_argument('--max-retries', type=int, default=2)
    parser.add_argument('--live', action='store_true', help='Opt in to authorized HTTPS requests')
    parser.add_argument('--via-browser', metavar='CDP_URL',
                        help='send requests from the logged-in Tandem tab (implies --live), '
                             'e.g. http://localhost:9222')
    args = parser.parse_args()
    return Settings(
        base_url=args.base_url, output=args.output, count=args.count,
        delay=args.delay, timeout=args.timeout,
        max_retries=args.max_retries, live=args.live or bool(args.via_browser),
        endpoints=tuple(args.endpoints or ENDPOINTS), method=args.method,
        params=tuple(args.param), data=args.data, via_browser=args.via_browser,
        events=args.events, label=args.label, forever=args.forever,
    )


def main() -> int:
    logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(message)s')
    settings = parse_args()
    settings.validate()
    journal = TextJournal(settings.output)
    events = EventLog(settings.events, settings.label) if settings.events else None
    seen = 0
    earliest: str | None = None
    with APIClient(settings) as client:
        i = 0
        while True:
            i += 1
            endpoint = settings.endpoints[(i - 1) % len(settings.endpoints)]
            record = client.execute(endpoint, i)
            journal.append(record)
            LOG.info('check=%d endpoint=%s status=%s candidates=%d',
                     i, endpoint, record.status.value, len(record.slots))
            if record.status is Status.SUCCESS and record.slots:
                seen += len(record.slots)
                first = earliest_date(record.slots)
                if first and (earliest is None or first < earliest):
                    earliest = first
                if events:
                    events.append(record)
            if record.http_status in (401, 403, 429):
                LOG.warning('Stopping after authentication or rate-limit response')
                print(f'RESULT: STOPPED http={record.http_status}')
                return EXIT_STOPPED
            if i >= settings.count and not settings.forever:
                break
            time.sleep(settings.delay)
    LOG.info('Responses saved to %s', settings.output)
    # machine-read by the visa-slot-booker agent
    print(f'RESULT: SLOTS_FOUND {seen} earliest={earliest or "-"}')
    return EXIT_SLOTS if seen else 0


if __name__ == '__main__':
    raise SystemExit(main())
