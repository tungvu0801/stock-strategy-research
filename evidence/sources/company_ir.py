"""
evidence/sources/company_ir.py — the company's own investor-relations page(s) (DESIGN_54 §3). Exactly the URLs configured in
config.EVIDENCE_IR_URLS[ticker] or supplied with the request are fetched: no crawling, no link following, at most LIMITS["ir_urls"].
The host of each declared URL is the allowed host for that fetch (the user declared it as the company's IR site); tier 1.
A page that cannot be retrieved leaves only its fetch row (status + error) — never a guessed item.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Callable, List, Optional

from evidence import LIMITS
from evidence import extract as X
from evidence import net
from evidence.rules import is_earnings, is_guidance
from evidence.sources.base import AdapterResult, SourceItem


class IrAdapter:
    name = "company_ir"

    def __init__(self, *, fetch_fn: Callable = net.fetch, now_fn: Optional[Callable[[], datetime]] = None):
        self.fetch_fn, self.now_fn = fetch_fn, now_fn or (lambda: datetime.now(timezone.utc))

    def run(self, ticker: str, urls: List[str], as_of: date) -> AdapterResult:
        res = AdapterResult(adapter=self.name)
        retrieved_dt = self.now_fn()
        retrieved = retrieved_dt.isoformat(timespec="seconds")
        for url in list(dict.fromkeys(urls))[:LIMITS["ir_urls"]]:
            host = net.host_of(url)
            fr = self.fetch_fn(url, purpose=f"{ticker} investor relations page", allowed_hosts=[host] if host else [], now_fn=self.now_fn)
            idx = res.add_fetch(fr)
            if not fr.ok:
                continue
            text, title, meta = X.parse_html(fr.text)
            if not text.strip():
                fr.status, fr.error = "PARSE_FAILED", "no readable text"
                continue
            d = X.extract_date(meta, text)
            if d is not None and d > as_of:
                d = None                                                        # a page dated after as_of is not point-in-time evidence
            body = X.snippet(text, LIMITS["text_chars"] - len(title) - 3)
            full = (f"{title}: " if title else "") + body
            kind = "GUIDANCE" if is_guidance(full) else "EARNINGS" if is_earnings(title or full[:400]) else "PAGE"
            res.items.append(SourceItem(subject_ticker=ticker, source_type="IR_RELEASE", source_name=host, source_url=fr.final_url, source_identifier=None,
                                        published_at=d.isoformat() if d else None, retrieved_at=retrieved, evidence_date=(d or retrieved_dt.date()).isoformat(),
                                        kind=kind, text=full, role="company", source_hash=fr.content_hash, fetch_index=idx,
                                        date_source="source" if d else "retrieval"))
        return res
