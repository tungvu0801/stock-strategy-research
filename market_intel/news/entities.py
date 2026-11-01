"""
market_intel/news/entities.py - Stage 7.2: deterministic ticker / entity matching (design C.10 "Ticker / entity matching"). Never a
model; every mention records its method and confidence.

| Method        | Rule                                                                         | Confidence |
| EXCHANGE_TAG  | "(NASDAQ: NVDA)", "(NYSE: ORCL)" in the headline or summary                  | 0.95       |
| PROVIDER_TAG  | the provider's own symbol tags                                               | 0.9        |
| CASHTAG       | "$NVDA"                                                                      | 0.8        |
| NAME_MATCH    | the SEC company_tickers.json title, normalised, word-bounded, or an alias    | 0.7        |
| UNVERIFIED    | a tag that is neither in the SEC map nor in the universe                     | 0.3        |

* A bare word is never a ticker. A COMMON-WORD ticker (ON, ALL, IT, A, NOW, CAT, ...) needs an exchange tag, a cashtag or a name
  match; a provider tag alone leaves it UNVERIFIED (0.3).
* Role: PRIMARY when the headline names the company (any method found in the headline), MENTION otherwise, ROUNDUP for every symbol of
  an article with more than ROUNDUP_OVER provider tags (excluded from catalyst flags by default).
* Catalyst eligibility (D.1): a PRIMARY mention with confidence >= 0.8 in an article of tier <= 3.
"""
from __future__ import annotations

import re
from typing import Dict, Iterable, List, Mapping, Optional

CONFIDENCE = {"EXCHANGE_TAG": 0.95, "PROVIDER_TAG": 0.9, "CASHTAG": 0.8, "NAME_MATCH": 0.7, "UNVERIFIED": 0.3}
ROUNDUP_OVER = 5
CATALYST_MIN_CONFIDENCE = 0.8
CATALYST_MAX_TIER = 3
COMMON_WORD_TICKERS = frozenset(("A", "AI", "ALL", "AM", "AN", "ANY", "ARE", "BE", "BIG", "CAN", "CAR", "CAT", "DAY", "FOR", "FUN", "GO", "HAS",
                                 "HE", "IT", "LOVE", "MAN", "NEW", "NOW", "ON", "ONE", "OPEN", "OUT", "PLAY", "REAL", "RUN", "SAFE", "SEE", "SO",
                                 "TRUE", "TV", "U", "WELL", "YOU"))
SUFFIXES = ("incorporated", "inc", "corporation", "corp", "company", "co", "holdings", "holding", "group", "ltd", "limited", "plc", "sa", "nv",
            "ag", "lp", "llc", "the", "class a", "class b", "com")
_EXCHANGE = re.compile(r"\((?:NASDAQ|NYSE(?:\s+American|\s+Arca)?|AMEX|NYSEAMERICAN|CBOE|OTC(?:QX|QB)?)\s*:\s*([A-Z][A-Z.]{0,6})\)", re.I)
_CASHTAG = re.compile(r"(?<![\w$])\$([A-Z][A-Z.]{0,6})\b")
_WORD = re.compile(r"[^a-z0-9& ]+")


def normalize_name(title: Optional[str]) -> str:
    """A company title for matching: lower case, punctuation dropped, legal suffixes (Inc., Corp., Holdings, Ltd, plc ...) removed."""
    s = " " + _WORD.sub(" ", str(title or "").lower()) + " "
    for suf in sorted(SUFFIXES, key=len, reverse=True):
        s = s.replace(f" {suf} ", " ")
    return re.sub(r"\s+", " ", s).strip()


def _name_in(text: str, name: str) -> bool:
    return bool(name) and len(name) >= 3 and re.search(rf"(?<![a-z0-9]){re.escape(name)}(?![a-z0-9])", _WORD.sub(" ", text.lower())) is not None


def mentions(*, headline: Optional[str], summary: Optional[str], provider_symbols: Iterable[str], sec_names: Mapping[str, str],
             universe: Iterable[str] = (), aliases: Optional[Mapping[str, Iterable[str]]] = None) -> List[dict]:
    """One row per symbol: {symbol, method, confidence, role, in_headline, methods}. sec_names: {ticker: SEC title}; universe: tickers
    the caller follows; aliases: {ticker: [extra names]}."""
    head, body = str(headline or ""), str(summary or "")
    tagged = sorted({str(s).upper() for s in provider_symbols or [] if str(s).strip()})
    known = {t.upper() for t in sec_names} | {t.upper() for t in universe}
    found: Dict[str, Dict[str, bool]] = {}                                   # symbol -> {method: seen in the headline?}

    def add(sym: str, method: str, in_head: bool) -> None:
        f = found.setdefault(sym.upper(), {})
        f[method] = f.get(method, False) or in_head
    for text, in_head in ((head, True), (body, False)):
        for m in _EXCHANGE.finditer(text):
            add(m.group(1), "EXCHANGE_TAG", in_head)
        for m in _CASHTAG.finditer(text):
            add(m.group(1), "CASHTAG", in_head)
    sec = {t.upper(): n for t, n in sec_names.items()}
    al = {t.upper(): list(v) for t, v in (aliases or {}).items()}
    for sym in sorted(set(tagged) | {u.upper() for u in universe}):          # names only for tagged and followed tickers (bounded)
        ns = {normalize_name(n) for n in [sec.get(sym, "")] + al.get(sym, [])}
        for text, in_head in ((head, True), (body, False)):
            if any(_name_in(text, n) for n in ns):
                add(sym, "NAME_MATCH", in_head)
    for sym in tagged:
        add(sym, "PROVIDER_TAG", False)
    roundup = len(tagged) > ROUNDUP_OVER
    out = []
    for sym, methods in sorted(found.items()):
        strong = dict(methods)
        if sym in COMMON_WORD_TICKERS or sym not in known:
            strong.pop("PROVIDER_TAG", None)                                 # a provider tag alone never confirms a common word or an unknown ticker
        if sym not in known and sym not in tagged:
            strong.pop("CASHTAG", None)                                      # a cashtag nobody tagged, of a ticker nobody knows
        method = max(strong, key=CONFIDENCE.get) if strong else "UNVERIFIED"
        in_head = any(methods.values())
        role = "ROUNDUP" if roundup else ("PRIMARY" if in_head else "MENTION")
        out.append({"symbol": sym, "method": method, "confidence": CONFIDENCE[method], "role": role, "in_headline": in_head,
                    "methods": sorted(methods)})
    return out


def catalyst_eligible(mention: dict, tier: Optional[int]) -> bool:
    return (mention.get("role") == "PRIMARY" and float(mention.get("confidence") or 0) >= CATALYST_MIN_CONFIDENCE
            and tier is not None and int(tier) <= CATALYST_MAX_TIER)
