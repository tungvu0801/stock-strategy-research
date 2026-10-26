"""
unified_portfolio/fixture.py - a SYNTHETIC, gateway-shaped Robinhood portfolio for previews and tests (ROBINHOOD_PORTFOLIO_SOURCE=fixture).

The file holds {"portfolio": <gateway /portfolio data>, "positions": <gateway /positions data>, "fetched_at": ISO time or null}. It is never a
real account: the source is ROBINHOOD_FIXTURE and every label says SYNTHETIC. Only the two reads the adapter uses exist; there is no other method.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from portfolio.provider import GatewayResult, PortfolioUnavailable


class FixturePortfolioProvider:
    synthetic = True

    def __init__(self, path: str):
        self._path = Path(path) if path else None

    def _load(self) -> dict:
        if self._path is None or not self._path.is_file():
            raise PortfolioUnavailable("DATA_UNAVAILABLE", "The synthetic Robinhood fixture file is not set or does not exist.")
        return json.loads(self._path.read_text(encoding="utf-8"))

    def _result(self, key: str) -> GatewayResult:
        d = self._load()
        at = d.get("fetched_at") or datetime.now(timezone.utc).isoformat(timespec="seconds")
        return GatewayResult(status=str(d.get("status") or "OK"), data=d.get(key), fetched_at=at, cache_age_s=0, truncated=False, message="synthetic fixture")

    def get_portfolio(self) -> GatewayResult:
        return self._result("portfolio")

    def get_positions(self) -> GatewayResult:
        return self._result("positions")
