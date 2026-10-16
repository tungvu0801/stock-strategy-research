"""
evidence/net.py — the ONLY network path of the evidence layer (DESIGN_54 §4).

    fetch(url, purpose=..., allowed_hosts=(...)) -> FetchResult

Rules enforced BEFORE anything is sent: config.EVIDENCE_NETWORK_ENABLED must be on (tests force it off), the scheme must be
https, the host must match one of the adapter's allowed hosts and none of config.EVIDENCE_DENY_HOSTS. One attempt per URL
(no retry loop), bounded timeout and body size, redirects followed only within the same host (at most 3), 401 / 402 / 403 /
429 recorded as ACCESS_DENIED and never retried. Every call returns a FetchResult that the engine stores verbatim as an
evidence_source_fetches row — successful or not — so a missing source is visible, never silently replaced by model knowledge.

`WIRE` is the real transport (urllib, no redirect following); tests and the browser harness replace it with a fake that serves
fixtures, and tests/conftest.py installs a tripwire that fails any test reaching the real one.
"""
from __future__ import annotations

import hashlib
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Callable, Dict, Iterable, Optional, Tuple

import config

MAX_REDIRECTS = 3
DENIED_HTTP = (401, 402, 403, 429)

# (status_code, headers{lower: value}, body_bytes) for one request; no redirect following inside the wire
WireResponse = Tuple[int, Dict[str, str], bytes]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401 - urllib hook
        return None


def _real_wire(url: str, headers: Dict[str, str], timeout_s: float, max_bytes: int) -> WireResponse:
    opener = urllib.request.build_opener(_NoRedirect)
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with opener.open(req, timeout=timeout_s) as resp:
            body = resp.read(max_bytes + 1)
            return resp.status, {k.lower(): v for k, v in resp.headers.items()}, body
    except urllib.error.HTTPError as exc:                       # 3xx (not followed), 4xx, 5xx all arrive here
        body = exc.read(max_bytes + 1) if exc.fp is not None else b""
        return exc.code, {k.lower(): v for k, v in (exc.headers or {}).items()}, body


WIRE: Callable[[str, Dict[str, str], float, int], WireResponse] = _real_wire


@dataclass
class FetchResult:
    status: str                       # one of evidence.FETCH_STATUSES
    url: str
    final_url: str
    host: str
    purpose: str
    http_status: Optional[int] = None
    bytes: int = 0
    content_hash: Optional[str] = None
    content_type: str = ""
    text: str = ""
    started_at: str = ""
    duration_ms: int = 0
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "OK"

    def row(self) -> dict:
        d = asdict(self)
        d.pop("text", None)
        return d


def host_of(url: str) -> str:
    try:
        return (urllib.parse.urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def host_matches(host: str, patterns: Iterable[str]) -> bool:
    host = (host or "").lower()
    for p in patterns:
        p = (p or "").lower().strip()
        if p and (host == p or host.endswith("." + p)):
            return True
    return False


def _decode(body: bytes, content_type: str) -> str:
    charset = "utf-8"
    for part in content_type.split(";"):
        part = part.strip().lower()
        if part.startswith("charset="):
            charset = part[len("charset="):].strip('"') or "utf-8"
    try:
        return body.decode(charset, errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")


def fetch(url: str, *, purpose: str, allowed_hosts: Iterable[str], headers: Optional[Dict[str, str]] = None,
          timeout_s: Optional[float] = None, max_bytes: Optional[int] = None, now_fn: Callable[[], datetime] = None) -> FetchResult:
    """One bounded GET. Never raises; every outcome is a FetchResult with an explicit status."""
    started = (now_fn or (lambda: datetime.now(timezone.utc)))()
    timeout_s = float(timeout_s if timeout_s is not None else config.EVIDENCE_FETCH_TIMEOUT_S)
    max_bytes = int(max_bytes if max_bytes is not None else config.EVIDENCE_MAX_BYTES)
    host = host_of(url)
    res = FetchResult(status="OK", url=url, final_url=url, host=host, purpose=purpose, started_at=started.isoformat(timespec="seconds"))
    t0 = time.perf_counter()

    def done(status: str, error: str = "", **kw) -> FetchResult:
        res.status, res.error = status, error[:300]
        for k, v in kw.items():
            setattr(res, k, v)
        res.duration_ms = int((time.perf_counter() - t0) * 1000)
        return res

    if not config.EVIDENCE_NETWORK_ENABLED:
        return done("DISABLED", "EVIDENCE_NETWORK_ENABLED is off")
    scheme = urllib.parse.urlsplit(url).scheme.lower() if "://" in url else ""
    if scheme != "https" or not host:
        return done("HOST_NOT_ALLOWED", f"only https URLs with a host are fetched (got scheme {scheme or 'none'})")
    if host_matches(host, config.EVIDENCE_DENY_HOSTS):
        return done("HOST_DENIED", f"{host} is on EVIDENCE_DENY_HOSTS (paywalled / access-controlled / social)")
    if not host_matches(host, allowed_hosts):
        return done("HOST_NOT_ALLOWED", f"{host} is not an allowed host for {purpose}")

    hdrs = {"User-Agent": config.SEC_USER_AGENT or "stock-agent-evidence/5.4", "Accept": "text/html,application/json,text/plain;q=0.9,*/*;q=0.5"}
    hdrs.update(headers or {})
    current = url
    for _hop in range(MAX_REDIRECTS + 1):
        try:
            code, rh, body = WIRE(current, hdrs, timeout_s, max_bytes)
        except socket.timeout:
            return done("TIMEOUT", f"no response within {timeout_s:.0f}s")
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, socket.timeout) or "timed out" in str(reason).lower():
                return done("TIMEOUT", f"no response within {timeout_s:.0f}s")
            return done("HTTP_ERROR", f"{type(reason).__name__}: {reason}")
        except Exception as exc:  # noqa: BLE001 - any transport failure is a recorded result, never an exception
            return done("HTTP_ERROR", f"{type(exc).__name__}: {exc}")
        if code in (301, 302, 303, 307, 308):
            location = rh.get("location", "")
            target = urllib.parse.urljoin(current, location) if location else ""
            if not target or host_of(target) != host or urllib.parse.urlsplit(target).scheme.lower() != "https":
                return done("REDIRECT_BLOCKED", f"redirect to {host_of(target) or 'nowhere'} refused (same https host only)", http_status=code)
            current = target
            continue
        res.http_status, res.final_url = code, current
        if code in DENIED_HTTP:
            return done("ACCESS_DENIED", f"HTTP {code}: access-controlled source; not retried")
        if code != 200:
            return done("HTTP_ERROR", f"HTTP {code}")
        if len(body) > max_bytes:
            return done("TOO_LARGE", f"body exceeds {max_bytes} bytes", bytes=len(body))
        if not body.strip():
            return done("EMPTY", "empty body")
        ctype = rh.get("content-type", "")
        return done("OK", bytes=len(body), content_hash=hashlib.sha256(body).hexdigest(), content_type=ctype, text=_decode(body, ctype))
    return done("REDIRECT_BLOCKED", f"more than {MAX_REDIRECTS} redirects")
