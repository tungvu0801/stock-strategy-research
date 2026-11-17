"""md_eval/cash_rate.py - the return on idle cash. ZERO is the base case. A constant annual rate is a sensitivity and is labelled HYPOTHETICAL
on every output. A real series (e.g. T-bills) is accepted only when its sha256 is in REGISTERED_SERIES_SHA256, which is empty: no series has
been registered (that would be new data access, a separate gate)."""
from __future__ import annotations

import hashlib
import json
from typing import Dict

from md_eval import CASH_RATE_HYPOTHETICAL

SESSIONS_PER_YEAR = 252
REGISTERED_SERIES_SHA256: frozenset = frozenset()


class CashRateRefused(ValueError):
    pass


def series_sha256(series: Dict[str, float]) -> str:
    return hashlib.sha256(json.dumps(series, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


class CashRate:
    def __init__(self, mode: str, annual: float = 0.0, series: Dict[str, float] = None, sha256: str = ""):
        if mode not in ("ZERO", "HYPOTHETICAL_CONSTANT", "REGISTERED_SERIES"):
            raise CashRateRefused(f"unknown cash-rate mode {mode!r}")
        if mode == "REGISTERED_SERIES":                                  # LOW-2: the checks live in the constructor, so no path skips them
            if series_sha256(dict(series or {})) != sha256:
                raise CashRateRefused("the cash-rate series does not match its sha256")
            if sha256 not in REGISTERED_SERIES_SHA256:
                raise CashRateRefused("the cash-rate series is not registered by sha256 (no series is registered: new data access is a separate gate)")
        self.mode, self.annual, self._series, self.sha256 = mode, float(annual), dict(series or {}), sha256
        self._daily = (1.0 + self.annual) ** (1.0 / SESSIONS_PER_YEAR) - 1.0 if mode == "HYPOTHETICAL_CONSTANT" else 0.0

    @property
    def label(self) -> str:
        return {"ZERO": "ZERO_BASE_CASE", "HYPOTHETICAL_CONSTANT": "HYPOTHETICAL", "REGISTERED_SERIES": "REGISTERED_SERIES"}[self.mode]

    def flags(self) -> list:
        return [CASH_RATE_HYPOTHETICAL] if self.mode == "HYPOTHETICAL_CONSTANT" else []

    def daily(self, session: str) -> float:
        if self.mode == "REGISTERED_SERIES":
            return float(self._series[session])
        return self._daily

    def identity(self) -> dict:
        return {"mode": self.mode, "label": self.label, "annual": self.annual, "sha256": self.sha256}


ZERO = CashRate("ZERO")


def hypothetical(annual_rate: float) -> CashRate:
    return CashRate("HYPOTHETICAL_CONSTANT", annual=annual_rate)


def registered_series(series: Dict[str, float], sha256: str) -> CashRate:
    return CashRate("REGISTERED_SERIES", series=series, sha256=sha256)    # the constructor refuses a mismatched or unregistered series
