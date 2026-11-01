"""
market_intel/news/attribution.py - Stage 7.2: source attribution of an article (design C.10 "Source attribution").

* Every article keeps provider, the original `source` field, author, canonical URL, host, published_at, first_seen_at and tier.
* The tier is the evidence layer's own (evidence.rules.tier_for with source_type ALPACA_NEWS: 3 on the press allowlist, 4 otherwise, 5
  for a social host) - never a second tier table. Tier 5 is never a catalyst.
* A link on EVIDENCE_DENY_HOSTS is metadata only: it is flagged (deny_host) and never fetched or quoted.
* Only the headline and a <= 300-character snippet of the summary are kept (a licensing choice: no full text).
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Iterable, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import config
from evidence.net import host_matches
from evidence.rules import tier_for

SNIPPET_MAX = 300
TRACKING_PARAMS = ("fbclid", "gclid", "ref", "cmpid")                          # plus every utm_* parameter
_SPACE = re.compile(r"\s+")


def canonical_url(url: Optional[str]) -> Optional[str]:
    """Lower-case scheme and host, `www.` stripped, tracking parameters (utm_*, fbclid, gclid, ref, cmpid) and the fragment dropped, the
    remaining parameters sorted, the trailing slash removed. None for anything that is not an http(s) URL with a host."""
    if not isinstance(url, str) or not url.strip():
        return None
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return None
    scheme, host = parts.scheme.lower(), (parts.hostname or "").lower()
    if scheme not in ("http", "https") or not host:
        return None
    host = host[4:] if host.startswith("www.") else host
    if parts.port:
        host = f"{host}:{parts.port}"
    query = sorted((k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                   if not k.lower().startswith("utm_") and k.lower() not in TRACKING_PARAMS)
    path = parts.path.rstrip("/")
    return urlunsplit((scheme, host, path, urlencode(query), ""))


def host_of(url: Optional[str]) -> str:
    c = canonical_url(url)
    return urlsplit(c).hostname or "" if c else ""


def snippet(text: Optional[str], limit: int = SNIPPET_MAX) -> str:
    """Whitespace collapsed, at most `limit` characters (cut at a word boundary when one is near, with an ellipsis)."""
    s = _SPACE.sub(" ", str(text or "")).strip()
    if len(s) <= limit:
        return s
    cut = s[:limit - 1]
    sp = cut.rfind(" ")
    return (cut[:sp] if sp >= limit * 0.6 else cut).rstrip() + "…"


def attribute(*, provider: str, source: Optional[str], author: Optional[str], url: Optional[str], published_at: Optional[datetime],
              first_seen_at: datetime, deny_hosts: Optional[Iterable[str]] = None) -> dict:
    """The attribution of one article. The tier uses the evidence rules unchanged (subject and ticker blank: a news provider's article is
    attributed to its outlet, not to a company's own domain)."""
    deny = tuple(config.EVIDENCE_DENY_HOSTS if deny_hosts is None else deny_hosts)
    host = host_of(url)
    return {"provider": provider, "source": (source or "").strip()[:120], "author": (author or "").strip()[:200], "canonical_url": canonical_url(url),
            "host": host, "published_at": published_at.isoformat() if published_at else None, "first_seen_at": first_seen_at.isoformat(),
            "tier": tier_for(host=host, subject_ticker="", ticker="", source_type="ALPACA_NEWS", press_source=source or ""),
            "deny_host": bool(host) and host_matches(host, deny)}
