"""
evidence/sources/manual_url.py — user-supplied public URLs and pasted text (DESIGN_54 §3).

Each URL comes with a declared role (company / supplier / customer / industry / press / social) and optionally the related ticker
it is about. Exactly that URL is fetched once (its own host is the allowed host; the deny list still applies). The tier is decided
later by the rules from the host and the declared role; social hosts become tier 5 and therefore UNCONFIRMED.
Pasted text (`manual_texts`) has no fetchable source and is always stored as UNCONFIRMED, tier 4, so it can inform the reader but
never a ladder rung.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Callable, List, Optional

from evidence import LIMITS, ROLES
from evidence import extract as X
from evidence import net
from evidence.rules import is_earnings, is_guidance
from evidence.sources.base import AdapterResult, SourceItem


class ManualAdapter:
    name = "manual_url"

    def __init__(self, *, fetch_fn: Callable = net.fetch, now_fn: Optional[Callable[[], datetime]] = None):
        self.fetch_fn, self.now_fn = fetch_fn, now_fn or (lambda: datetime.now(timezone.utc))

    def run(self, ticker: str, urls: List[dict], texts: List[dict], as_of: date) -> AdapterResult:
        res = AdapterResult(adapter=self.name)
        now = self.now_fn()
        retrieved = now.isoformat(timespec="seconds")
        seen = set()
        for spec in (urls or [])[:LIMITS["manual_urls"]]:
            url = str(spec.get("url") or "").strip()
            role = str(spec.get("role") or "").strip().lower()
            if not url or url in seen or role not in ROLES:
                res.notes.append(f"manual URL skipped (missing url, duplicate, or role not in {ROLES}): {url or '?'}")
                continue
            seen.add(url)
            subject = str(spec.get("related_ticker") or ticker).upper()
            host = net.host_of(url)
            fr = self.fetch_fn(url, purpose=f"manual {role} URL for {subject}", allowed_hosts=[host] if host else [], now_fn=self.now_fn)
            idx = res.add_fetch(fr)
            if not fr.ok:
                continue
            text, title, meta = X.parse_html(fr.text)
            if not text.strip():
                fr.status, fr.error = "PARSE_FAILED", "no readable text"
                continue
            d = X.extract_date(meta, text)
            if d is not None and d > as_of:
                d = None
            body = X.snippet(text, LIMITS["text_chars"] - len(title) - 3)
            full = (f"{title}: " if title else "") + body
            kind = "GUIDANCE" if is_guidance(full) else "EARNINGS" if is_earnings(title or full[:400]) else "PAGE"
            res.items.append(SourceItem(subject_ticker=subject, source_type="MANUAL_URL", source_name=host, source_url=fr.final_url, source_identifier=None,
                                        published_at=d.isoformat() if d else None, retrieved_at=retrieved, evidence_date=(d or now.date()).isoformat(),
                                        kind=kind, text=full, role=role, source_hash=fr.content_hash, fetch_index=idx, date_source="source" if d else "retrieval",
                                        extra={"note": str(spec.get("note") or "")}))
        for spec in (texts or [])[:LIMITS["manual_urls"]]:
            text = str(spec.get("text") or "").strip()
            role = str(spec.get("role") or "").strip().lower()
            if not text or role not in ROLES:
                res.notes.append("manual text skipped (empty or role not declared)")
                continue
            subject = str(spec.get("related_ticker") or ticker).upper()
            res.items.append(SourceItem(subject_ticker=subject, source_type="MANUAL_TEXT", source_name="user-supplied text (unverified)", source_url=None,
                                        source_identifier=None, published_at=None, retrieved_at=retrieved, evidence_date=now.date().isoformat(), kind="TEXT",
                                        text=X.snippet(text, LIMITS["text_chars"]), role=role, source_hash=None, fetch_index=None, date_source="retrieval",
                                        extra={"note": str(spec.get("note") or "")}))
        return res
