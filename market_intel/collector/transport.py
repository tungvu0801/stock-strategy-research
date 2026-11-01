"""
market_intel/collector/transport.py - Stage 7.3: the injectable transport and the error classes (DESIGN_73 section 3). No network code.

A transport takes the request an adapter built (host, endpoint, parameters, the credential NAMED - never its value) and returns a
Response, or raises TransportTimeout / TransportConnectionError. Stage 7.3 ships exactly two:

* NoTransport - the default. It refuses every request (REAL_NETWORK_NOT_APPROVED) and never opens anything;
* FakeTransport - tests only: scripted responses or a responder function, every request kept in `sent`. It honours the transport
  contract a real one must keep: a body is read to at most `max_bytes + 1` bytes and then marked `truncated` (memory stays bounded).

The runner accepts only an object whose type IS FakeTransport (not a subclass, not a look-alike): a real transport is a later, approved
stage (DESIGN_73 section 7). classify() maps every outcome to a Stage 7.0 fetch status and says whether the same request is retried.
"""
from __future__ import annotations

from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Callable, Deque, Dict, List, Optional, Tuple, Union

REAL_NETWORK_NOT_APPROVED = "REAL_NETWORK_NOT_APPROVED"
REQUEST_TIMEOUT_S = 20.0


class TransportError(Exception):
    """A request that produced no HTTP response."""


class TransportTimeout(TransportError):
    pass


class TransportConnectionError(TransportError):
    pass


@dataclass(frozen=True)
class Response:
    status: int
    headers: Mapping = field(default_factory=dict)
    body: bytes = b""
    truncated: bool = False                  # the transport stopped reading at max_bytes + 1


Outcome = Union[Response, BaseException]


class NoTransport:
    """The default: nothing is ever sent."""

    def send(self, request: dict, *, timeout_s: float, max_bytes: int) -> Response:
        raise PermissionError(f"{REAL_NETWORK_NOT_APPROVED}: Stage 7.3 ships no real transport")


class FakeTransport:
    """Scripted outcomes, in order, or `responder(request) -> Response | exception`. `latency_s` advances an injected fake clock."""

    def __init__(self, outcomes=(), *, responder: Optional[Callable[[dict], Outcome]] = None, clock=None, latency_s: float = 0.0):
        self._queue: Deque[Outcome] = deque(outcomes)
        self._responder = responder
        self._clock = clock
        self.latency_s = float(latency_s)
        self.sent: List[dict] = []

    def push(self, *outcomes: Outcome) -> None:
        self._queue.extend(outcomes)

    def send(self, request: dict, *, timeout_s: float, max_bytes: int) -> Response:
        self.sent.append({"host": request["host"], "endpoint": request["endpoint"], "params": dict(request["params"]),
                          "credential": request.get("credential"), "timeout_s": timeout_s, "max_bytes": max_bytes})
        if self._responder is not None:
            out = self._responder(request)
        elif self._queue:
            out = self._queue.popleft()
        else:
            out = TransportConnectionError("FAKE_TRANSPORT_EXHAUSTED")
        if self._clock is not None and self.latency_s:
            self._clock.tick(self.latency_s)
        if isinstance(out, BaseException):
            raise out
        if isinstance(out, Response) and isinstance(out.body, (bytes, bytearray)) and len(out.body) > max_bytes:
            return Response(out.status, out.headers, bytes(out.body[:max_bytes + 1]), True)     # a real transport stops reading here
        return out


def is_fake(transport) -> bool:
    return type(transport) is FakeTransport


# ------------------------------------------------------------------------------------------------ error classes
# fetch status -> (retried?, a provider failure for the breaker?)
CLASSES: Dict[str, Tuple[bool, bool]] = {
    "OK": (False, False),
    "TIMEOUT": (True, True),
    "ERROR": (True, True),
    "RATE_LIMITED": (True, True),
    "HTTP_ERROR_5XX": (True, True),
    "HTTP_ERROR": (False, True),
    "ACCESS_DENIED": (False, True),
    "TOO_LARGE": (False, True),
    "INVALID_RESPONSE": (False, True),
}


def contract_ok(resp) -> bool:
    return (isinstance(resp, Response) and isinstance(resp.status, int) and not isinstance(resp.status, bool) and 100 <= resp.status <= 599
            and isinstance(resp.body, (bytes, bytearray)) and isinstance(resp.headers, Mapping))


def classify(outcome, *, max_bytes: int) -> Tuple[str, str, bool]:
    """(fetch status, error code, retry the same request?) of one attempt, before its body is parsed. A 200 whose page is unusable becomes
    INVALID_RESPONSE after parsing (runner); a response breaking the transport contract is INVALID_RESPONSE now and never retried."""
    if isinstance(outcome, (TransportTimeout, TimeoutError)):
        return "TIMEOUT", f"TRANSPORT:{type(outcome).__name__}", True
    if isinstance(outcome, BaseException):
        return "ERROR", f"TRANSPORT:{type(outcome).__name__}", True
    if not contract_ok(outcome):
        return "INVALID_RESPONSE", "TRANSPORT_CONTRACT", False
    if outcome.truncated or len(outcome.body) > max_bytes:
        return "TOO_LARGE", f"BODY_OVER_{max_bytes}_BYTES", False
    s = outcome.status
    if s == 200:
        return "OK", "", False
    if s == 429:
        return "RATE_LIMITED", "HTTP_429", True
    if s in (401, 403):
        return "ACCESS_DENIED", f"HTTP_{s}", False
    if 500 <= s <= 599:
        return "HTTP_ERROR", f"HTTP_{s}", True
    return "HTTP_ERROR", f"HTTP_{s}", False
