"""historical_data/status.py - P4 status objects: a missing value is {"status": "UNKNOWN" | "UNAVAILABLE", "reason": "<CODE>"}.

It is never 0, False, an empty list or a forward-filled value (contract v1 P4). NO_BAR is the execution channel's answer for a session
without a bar (contract v2 A2) and EARNINGS_DATA_UNAVAILABLE is every v1 adapter's scheduled-events answer (contract section 3.5)."""
from __future__ import annotations

import re
from dataclasses import dataclass

UNKNOWN, UNAVAILABLE = "UNKNOWN", "UNAVAILABLE"
STATUSES = (UNKNOWN, UNAVAILABLE)
CODE_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")


@dataclass(frozen=True)
class Missing:
    status: str
    reason: str

    def __post_init__(self):
        if self.status not in STATUSES:
            raise ValueError(f"a missing value's status is UNKNOWN or UNAVAILABLE, not {self.status!r}")
        if not isinstance(self.reason, str) or not CODE_RE.match(self.reason):
            raise ValueError(f"a missing value's reason is an UPPER_CASE code, not {self.reason!r}")

    def to_json(self) -> dict:
        return {"status": self.status, "reason": self.reason}


def is_missing(value) -> bool:
    return isinstance(value, Missing)


def unknown(reason: str) -> Missing:
    return Missing(UNKNOWN, reason)


def unavailable(reason: str) -> Missing:
    return Missing(UNAVAILABLE, reason)


NO_BAR = Missing(UNAVAILABLE, "NO_BAR")
EARNINGS_DATA_UNAVAILABLE = Missing(UNAVAILABLE, "EARNINGS_DATA_UNAVAILABLE")
