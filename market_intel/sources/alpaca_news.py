"""
market_intel/sources/alpaca_news.py - Stage 7.2: Alpaca news REST -> NEWS observations. PARSE-ONLY (no network code; DESIGN_72 3.1).

Collection (design C.10): `symbols=<chunk>`, `start=<watermark>`, `end=now`, `sort=asc`, paging with next_page_token until exhausted -
a complete, windowed history instead of "the latest 10" (data/news.py, untouched). The page limit is the documented 50 (verify on the
first approved run).

Mapping:
* observed_at = published_at = the article's created_at; updated_at is kept in the payload. The natural key is (NEWS, ALPACA, created_at,
  article id): an edited headline or a new updated_at is a NEW REVISION of the same article, never a new article;
* available_at = retrieved_at = first_seen_at: our first retrieval, NEVER created_at - an article is never known before we saw it;
* attribution (news/attribution.py): source, author, canonical URL, host and the evidence tier; a deny-listed host is metadata only;
* only the headline and a <= 300-character summary snippet are kept; `content` (full text) is never requested or stored;
* quality codes: validate.news_codes (EMPTY_HEADLINE, MISSING_PUBLISHED, FUTURE_DATED, DENY_HOST_LINK) and BACKFILLED.

Response validation (fail closed): a page is a JSON object with exactly `news` (a list) and `next_page_token` (null or a token); an error
envelope is ERROR_ENVELOPE; more than PAGE_LIMIT articles is PAGE_TOO_LARGE. An article is an object of the known keys with an integer
id, string texts, RFC 3339 times with an offset and a list of string symbols - else MALFORMED_ROW / BAD_TIME; an article outside the
requested window is OUT_OF_RANGE, one tagged with none of the requested symbols OUT_OF_SCOPE.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable, List, Optional, Sequence

import config
from market_intel import validate as VA
from market_intel.models import Observation
from market_intel.news import attribution as AT
from market_intel.sources import common as C

PROVIDER = "ALPACA"
HOST = "data.alpaca.markets"
ENDPOINT = "/v1beta1/news"
KIND = "NEWS"
PAGE_LIMIT = 50                         # Alpaca's documented maximum per page (DESIGN_72 section 6: verify on the first approved run)
MAX_SYMBOLS = 50                        # self-imposed per request
ARTICLE_REQUIRED = frozenset(("id", "headline", "author", "created_at", "updated_at", "summary", "url", "symbols", "source"))
ARTICLE_ALLOWED = ARTICLE_REQUIRED | {"content", "images"}
PAGE_KEYS = frozenset(("news", "next_page_token"))


def _z(t: datetime) -> str:
    return t.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def request(symbols: Sequence[str], start: datetime, end: datetime, *, page_token: Optional[str] = None, limit: int = PAGE_LIMIT) -> dict:
    """One page of the windowed history. Alpaca's key headers are added by the transport (credential ALPACA_API_KEY), never here."""
    syms = sorted({str(s).upper() for s in symbols})
    if not syms or len(syms) > MAX_SYMBOLS or not 1 <= int(limit) <= PAGE_LIMIT:
        raise ValueError(f"1-{MAX_SYMBOLS} symbols and a limit of 1-{PAGE_LIMIT}")
    params = {"symbols": ",".join(syms), "start": _z(start), "end": _z(end), "sort": "asc", "limit": int(limit), "include_content": "false"}
    if page_token:
        params["page_token"] = page_token
    return C.request(PROVIDER, HOST, ENDPOINT, params, credential="ALPACA_API_KEY", requires=("REAL_FETCH",))


def _article_ok(a) -> bool:
    return (isinstance(a, dict) and ARTICLE_REQUIRED <= set(a) and set(a) <= ARTICLE_ALLOWED and isinstance(a["id"], int)
            and not isinstance(a["id"], bool) and all(isinstance(a[k], str) for k in ("headline", "author", "created_at", "updated_at", "summary", "url", "source"))
            and isinstance(a["symbols"], list) and all(isinstance(s, str) for s in a["symbols"]))


@C.never_raises
def parse_page(body, *, symbols: Iterable[str], retrieved_at: datetime, start: Optional[datetime] = None, end: Optional[datetime] = None,
               backfilled: bool = False, deny_hosts: Optional[Iterable[str]] = None, dataset_id: Optional[str] = None) -> C.Parsed:
    doc, bad = C.load_json(body)
    if bad:
        return C.page_problem(bad)
    if isinstance(doc, dict) and ("message" in doc or "code" in doc):
        return C.page_problem("ERROR_ENVELOPE")
    if not isinstance(doc, dict) or set(doc) != PAGE_KEYS or not isinstance(doc["news"], list):
        return C.page_problem("UNEXPECTED_SHAPE")
    token = doc["next_page_token"]
    if token is not None and not (isinstance(token, str) and C.TOKEN.match(token)):
        return C.page_problem("BAD_PAGE_TOKEN")
    if len(doc["news"]) > PAGE_LIMIT:
        return C.page_problem("PAGE_TOO_LARGE", articles=len(doc["news"]))
    wanted = {str(s).upper() for s in symbols}
    deny = tuple(config.EVIDENCE_DENY_HOSTS if deny_hosts is None else deny_hosts)
    out: List[Observation] = []
    problems: List[dict] = []
    for i, a in enumerate(doc["news"]):
        if not _article_ok(a):
            problems.append({"problem": "MALFORMED_ROW", "index": i})
            continue
        created, updated = C.utc_time(a["created_at"]), C.utc_time(a["updated_at"])
        if created is None or updated is None:
            problems.append({"problem": "BAD_TIME", "index": i})
            continue
        if (start is not None and created < start) or (end is not None and created > end):
            problems.append({"problem": "OUT_OF_RANGE", "index": i})
            continue
        tags = sorted({s.strip().upper() for s in a["symbols"] if s.strip()})
        if wanted and not wanted & set(tags):
            problems.append({"problem": "OUT_OF_SCOPE", "index": i})
            continue
        attr = AT.attribute(provider=PROVIDER, source=a["source"], author=a["author"], url=a["url"], published_at=created, first_seen_at=retrieved_at,
                            deny_hosts=deny)
        flags = VA.news_codes({"headline": a["headline"], "published_at": created, "retrieved_at": retrieved_at, "url": a["url"]}, deny)
        flags += VA.time_codes(created, retrieved_at, backfilled=backfilled)
        session, sdate = C.session_fields(created)
        try:
            out.append(Observation(kind=KIND, provider=PROVIDER, feed="none", adjustment="none", observed_at=created, published_at=created,
                                   retrieved_at=retrieved_at, available_at=retrieved_at, session=session, session_date=sdate, status="AVAILABLE",
                                   dataset_id=dataset_id, source_url=attr["canonical_url"], source_identifier=str(a["id"]), tier=attr["tier"],
                                   quality_flags=tuple(sorted(set(flags))), backfilled=backfilled,
                                   payload={"headline": AT.snippet(a["headline"], 500), "summary": AT.snippet(a["summary"]), "author": attr["author"],
                                            "source": attr["source"], "host": attr["host"], "deny_host": attr["deny_host"], "symbols": tags,
                                            "updated_at": updated.isoformat()}))
        except (ValueError, TypeError) as exc:                     # the envelope refuses it (e.g. a forbidden key) - reported, never stored
            problems.append({"problem": "REFUSED_BY_ENVELOPE", "index": i, "detail": str(exc)[:160]})
    return out, token, problems
