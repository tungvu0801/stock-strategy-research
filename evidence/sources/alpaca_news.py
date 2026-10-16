"""
evidence/sources/alpaca_news.py — the existing Alpaca news feed (DESIGN_54 §3). Reuses data.news.get_recent_news (the project's
existing client; no new network path). Each headline becomes one FACT item whose hash is that of the record itself (headline, url,
timestamp); the tier is 3 when the article's source is on the press allowlist, else 4. The call is recorded as a pseudo fetch row
("alpaca://news/<ticker>") so the packet shows that the feed was consulted, with EMPTY when it returned nothing.
"""
from __future__ import annotations

import hashlib
from datetime import date, datetime, timezone
from typing import Callable, List, Optional

from evidence import LIMITS
from evidence import extract as X
from evidence.net import FetchResult
from evidence.sources.base import AdapterResult, SourceItem, iso_day


def _default_news(symbol: str, limit: int) -> List[dict]:
    from data.news import get_recent_news                   # local import: the existing client is only touched when actually used
    return get_recent_news(symbol, limit=limit)


class NewsAdapter:
    name = "alpaca_news"

    def __init__(self, *, news_fn: Optional[Callable[[str, int], List[dict]]] = None, now_fn: Optional[Callable[[], datetime]] = None):
        self.news_fn, self.now_fn = news_fn or _default_news, now_fn or (lambda: datetime.now(timezone.utc))

    def run(self, ticker: str, as_of: date, limit: Optional[int] = None) -> AdapterResult:
        res = AdapterResult(adapter=self.name)
        now = self.now_fn()
        retrieved = now.isoformat(timespec="seconds")
        limit = int(limit or LIMITS["headlines"])
        fr = FetchResult(status="OK", url=f"alpaca://news/{ticker}", final_url=f"alpaca://news/{ticker}", host="alpaca", purpose=f"{ticker} headlines",
                         started_at=retrieved)
        try:
            articles = self.news_fn(ticker, limit) or []
        except Exception as exc:  # noqa: BLE001 - the feed failing is a recorded result
            fr.status, fr.error = "HTTP_ERROR", f"{type(exc).__name__}: {exc}"[:300]
            res.add_fetch(fr)
            return res
        if not articles:
            fr.status, fr.error = "EMPTY", "no headlines returned (no credentials, no news, or feed error)"
        idx = res.add_fetch(fr)
        for a in articles[:limit]:
            headline = str(a.get("headline") or "").strip()
            if not headline:
                continue
            created = a.get("created_at")
            published = created.isoformat(timespec="seconds") if hasattr(created, "isoformat") else (str(created) if created else None)
            day = iso_day(created) or now.date().isoformat()
            if day > as_of.isoformat():
                continue                                                      # not point-in-time for this packet
            summary = str(a.get("summary") or "").strip()
            text = X.snippet(headline + (f" — {summary}" if summary else ""), LIMITS["text_chars"])
            rec = f"{headline}|{a.get('url') or ''}|{published or ''}"
            res.items.append(SourceItem(subject_ticker=ticker, source_type="ALPACA_NEWS", source_name=str(a.get("source") or "Alpaca news"),
                                        source_url=a.get("url"), source_identifier=None, published_at=published, retrieved_at=retrieved, evidence_date=day,
                                        kind="HEADLINE", text=text, role="press", source_hash=hashlib.sha256(rec.encode("utf-8")).hexdigest(),
                                        fetch_index=idx, date_source="source" if created else "retrieval", extra={"press_source": str(a.get("source") or "")}))
        return res
