"""
market_intel/news/dedup.py - Stage 7.2: deterministic news clustering (design C.10 "Deduplication"). Raw articles are always kept;
a duplicate gets a cluster id, it is never deleted.

Rules, in order (the first that joins two items is recorded as the item's `rule`):
  1. PROVIDER_ID     the same (provider, provider article id): revisions of one article;
  2. CANONICAL_URL   the same canonical URL (attribution.canonical_url);
  3. HEADLINE_HASH   the same sha256 of the normalised headline + source + published New York date;
  4. NEAR_DUPLICATE  word-set Jaccard of the normalised headlines (stop words removed, tickers kept) >= JACCARD_MIN, published within
                     WINDOW_HOURS of each other, sharing at least one symbol.

The representative of a cluster is the item first seen (earliest first_seen_at), ties to the lower tier, then the lower key; the
cluster id derives from it, so the same input always gives the same clusters. The thresholds are pre-registered (pinned by tests on
fixtures: same story / different outlets, revisions, near-duplicates, and different stories written from the same template).
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import datetime
from typing import Dict, Iterable, List, Optional, Sequence

from market_intel.news.attribution import canonical_url

JACCARD_MIN = 0.8
WINDOW_HOURS = 36
STOP_WORDS = frozenset(("a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "has", "have", "in", "is", "it", "its", "of", "on",
                        "or", "that", "the", "to", "was", "were", "will", "with", "after", "amid", "over", "says", "said", "report", "reports"))
_PUNCT = re.compile(r"[^\w\s$]")
_SPACE = re.compile(r"\s+")


def normalize_headline(headline: Optional[str]) -> str:
    """Lower-cased, accents folded, punctuation stripped (a $ cashtag sign kept), whitespace collapsed."""
    s = unicodedata.normalize("NFKD", str(headline or "")).encode("ascii", "ignore").decode("ascii").lower()
    return _SPACE.sub(" ", _PUNCT.sub(" ", s)).strip()


def headline_key(headline: Optional[str], source: Optional[str], published_at: Optional[datetime]) -> str:
    from market_intel.sources.common import ny_date
    day = ny_date(published_at).isoformat() if published_at else ""
    return hashlib.sha256(f"{normalize_headline(headline)}|{(source or '').strip().lower()}|{day}".encode("utf-8")).hexdigest()


def tokens(headline: Optional[str]) -> frozenset:
    return frozenset(w.lstrip("$") for w in normalize_headline(headline).split() if w.lstrip("$") and w.lstrip("$") not in STOP_WORDS)


def jaccard(a: frozenset, b: frozenset) -> float:
    return len(a & b) / len(a | b) if (a or b) else 0.0


class _Union:
    def __init__(self, keys: Iterable[str]):
        self.parent = {k: k for k in keys}

    def find(self, k: str) -> str:
        while self.parent[k] != k:
            self.parent[k] = self.parent[self.parent[k]]
            k = self.parent[k]
        return k

    def join(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[max(ra, rb)] = min(ra, rb)


def cluster(items: Sequence[dict]) -> List[dict]:
    """items: {key, provider, provider_id, headline, source, url, symbols, published_at (aware datetime), first_seen_at (aware), tier}.
    Returns, in input order, {key, cluster_id, representative, rule} - rule None for an item that joined nothing."""
    keys = [str(i["key"]) for i in items]
    if len(set(keys)) != len(keys):
        raise ValueError("item keys must be unique")
    by_key = {str(i["key"]): i for i in items}
    u, rule = _Union(keys), {}

    def link(a: str, b: str, why: str) -> None:
        if u.find(a) != u.find(b):
            u.join(a, b)
            for k in (a, b):
                rule.setdefault(k, why)
    groups: Dict[tuple, List[str]] = {}
    for k, i in by_key.items():
        groups.setdefault(("id", i.get("provider"), str(i.get("provider_id"))), []).append(k)
        c = canonical_url(i.get("url"))
        if c:
            groups.setdefault(("url", c), []).append(k)
        groups.setdefault(("hl", headline_key(i.get("headline"), i.get("source"), i.get("published_at"))), []).append(k)
    for (kind, *_), members in sorted(groups.items(), key=lambda kv: (["id", "url", "hl"].index(kv[0][0]), str(kv[0]))):
        why = {"id": "PROVIDER_ID", "url": "CANONICAL_URL", "hl": "HEADLINE_HASH"}[kind]
        for m in members[1:]:
            link(members[0], m, why)
    order = sorted(keys, key=lambda k: (0, by_key[k]["published_at"].timestamp(), k) if by_key[k].get("published_at") else (1, 0.0, k))
    toks = {k: tokens(by_key[k].get("headline")) for k in keys}
    syms = {k: {str(s).upper() for s in by_key[k].get("symbols") or []} for k in keys}
    for i, a in enumerate(order):                                          # near-duplicates inside the window (sorted: a bounded scan)
        ta = by_key[a].get("published_at")
        for b in order[i + 1:]:
            tb = by_key[b].get("published_at")
            if ta is None or tb is None:
                continue
            if (tb - ta).total_seconds() > WINDOW_HOURS * 3600:
                break
            if syms[a] & syms[b] and jaccard(toks[a], toks[b]) >= JACCARD_MIN:
                link(a, b, "NEAR_DUPLICATE")
    members: Dict[str, List[str]] = {}
    for k in keys:
        members.setdefault(u.find(k), []).append(k)
    rep_of, cid_of = {}, {}
    for root, ms in members.items():
        rep = min(ms, key=lambda k: (by_key[k]["first_seen_at"].timestamp(), int(by_key[k].get("tier") or 9), k))
        cid = "nc_" + hashlib.sha256(f"{by_key[rep].get('provider')}|{by_key[rep].get('provider_id')}|{rep}".encode("utf-8")).hexdigest()[:20]
        for m in ms:
            rep_of[m], cid_of[m] = rep, cid
    return [{"key": k, "cluster_id": cid_of[k], "representative": rep_of[k] == k, "rule": rule.get(k)} for k in keys]
