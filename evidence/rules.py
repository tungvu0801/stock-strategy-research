"""
evidence/rules.py — the deterministic rules of Stage 5.4 (`ev_v1`, DESIGN_54 §5). Pure functions over dicts; no I/O, no model.

tier_for / epistemic_for / category_for / direction_scan     provenance and labelling of one item (case-insensitive source names)
claim_key                                                    deterministic subject + claim identity used by the contradiction rule
cause_attribution                                            market-wide vs company-specific decline; NOT_SEPARABLE without a sector benchmark
mark_contradictions                                          opposite directions, same category, same subject / claim key, within 30 days
ladder                                                       six rungs; rung 4 needs validated XBRL or official filing text, never a bare computation
strength                                                     WEAK / MODERATE / STRONG from rung points — evidence strength, not a probability
gaps / next_events                                           templated sentences for NOT_AVAILABLE rungs, failed fetches and dated events
Rules read RULE text (the full extracted document, capped) while the stored item text is only a display snippet.
The model never changes any of these outputs; it may only describe them.
"""
from __future__ import annotations

import re
from datetime import date
from typing import Dict, Iterable, List, Optional, Tuple

import config
from evidence import (COMPANY_CONFIRMATION, COMPANY_SPECIFIC, CONFIRMATION_MISSING, DOWN, FACT, INDUSTRY_SUPPLY_CHAIN, INFERENCE,
                      MARKET_WIDE, MODERATE, NA, NEUTRAL, NOT_AVAILABLE, NOT_SEPARABLE, NOT_SUPPORTED, PARTIAL, RUNGS,
                      STRENGTH_DISCLAIMER, STRONG, SUPPORTED, UNCONFIRMED, UP, WEAK)
from evidence.net import host_matches

SEC_HOSTS = ("sec.gov",)
CONTRADICTION_DAYS = 30
DECLINE_THRESHOLD = -0.02
MARKET_SHARE_HIGH, MARKET_SHARE_LOW = 0.60, 0.30
STRONG_POINTS, MODERATE_POINTS = 9, 5
COMPUTED_KINDS = ("METRIC", "EVENT")
NOT_SEPARABLE_REASON = "sector benchmark unavailable; market-wide versus company-specific cause cannot be separated reliably"

_YEAR = r"(?:full[- ]year\s+|fiscal\s+(?:\d{4}\s+)?|annual\s+|\d{4}\s+)?"
_UP = [r"raise[sd]?\s+(?:its\s+|the\s+)?" + _YEAR + r"(?:guidance|outlook)",
       r"above\s+(?:the\s+)?(?:prior|previous|high\s+end)",
       r"record\s+(?:revenue|quarter|quarterly\s+revenue)",
       r"(?:revenue|sales|shipments|orders|bookings|demand|margins?)\s+(?:grew|increased|improved|expanded|rose)",
       r"better[- ]than[- ]expected",
       r"inventory\s+normali[sz]",
       r"pricing\s+(?:improv|increas)"]
_DOWN = [r"(?:lower(?:s|ed)?|cuts?|reduce[sd]?)\s+(?:its\s+|the\s+)?" + _YEAR + r"(?:guidance|outlook)",
         r"below\s+(?:the\s+)?(?:prior|previous|low\s+end)",
         r"(?:revenue|sales|shipments|orders|bookings|demand|margins?)\s+(?:fell|declined|decreased|contracted|weakened)",
         r"weaker[- ]than[- ]expected",
         r"impairment|restructuring|layoffs|customer\s+loss|supply\s+(?:constraint|shortage|disruption)"]
_NEUTRAL = [r"(?:reaffirm|maintain|reiterat)(?:e|s|ed|ing)?\s+(?:its\s+|the\s+)?(?:guidance|outlook)", r"in\s+line\s+with"]
UP_RX = [re.compile(p, re.I) for p in _UP]
DOWN_RX = [re.compile(p, re.I) for p in _DOWN]
NEUTRAL_RX = [re.compile(p, re.I) for p in _NEUTRAL]
GUIDANCE_RX = re.compile(r"\b(?:guidance|outlook)\b", re.I)
EARNINGS_RX = re.compile(r"\b(?:results|earnings|quarter(?:ly)?|fiscal\s+(?:first|second|third|fourth))\b", re.I)


# ---------------------------------------------------------------- provenance ----------------------------------------------

def is_guidance(text: str) -> bool:
    return bool(GUIDANCE_RX.search(text or ""))


def is_earnings(text: str) -> bool:
    return bool(EARNINGS_RX.search(text or ""))


def tier_for(*, host: str, subject_ticker: str, ticker: str, source_type: str, role: str = "", press_source: str = "",
             ir_domains: Optional[Dict[str, List[str]]] = None, related: Optional[Dict[str, List[str]]] = None) -> int:
    """1 official (target) · 2 official (declared supplier / customer) · 3 established press · 4 other public page · 5 social.
    Hosts and source names are compared case-insensitively."""
    ir_domains = ir_domains if ir_domains is not None else config.EVIDENCE_IR_DOMAINS
    host = (host or "").strip().lower()
    if source_type in ("MARKET_DATA", "EVENT"):
        return 1
    if host_matches(host, config.EVIDENCE_SOCIAL_HOSTS) or (role or "").lower() == "social":
        return 5
    related_tickers = {t.upper() for group in (related or {}).values() for t in group}
    if source_type == "SEC_FILING" or host_matches(host, SEC_HOSTS):
        return 1 if subject_ticker.upper() == ticker.upper() else 2
    if source_type == "IR_RELEASE":
        return 1
    if host and host_matches(host, ir_domains.get(ticker.upper(), [])):
        return 1
    if host and subject_ticker.upper() in related_tickers and host_matches(host, ir_domains.get(subject_ticker.upper(), [])):
        return 2
    if source_type == "ALPACA_NEWS":
        allow = {p.strip().lower() for p in config.EVIDENCE_PRESS_ALLOWLIST}
        return 3 if (press_source or "").strip().lower() in allow else 4
    return 4


def epistemic_for(*, tier: int, fetched_ok: bool, has_url: bool, source_type: str) -> str:
    if source_type in ("MARKET_DATA", "EVENT"):
        return FACT
    if tier >= 5 or not has_url or not fetched_ok:
        return UNCONFIRMED
    return FACT if tier <= 3 else UNCONFIRMED if source_type == "MANUAL_TEXT" else FACT


def category_for(*, role: str, source_type: str, subject_is_target: bool) -> str:
    if source_type in ("MARKET_DATA", "EVENT"):
        return MARKET_WIDE
    if (role or "").lower() in ("supplier", "customer", "industry") or not subject_is_target:
        return INDUSTRY_SUPPLY_CHAIN
    return COMPANY_SPECIFIC


def direction_scan(text: str) -> Tuple[str, str, Optional[int]]:
    """(direction, rule_match, offset of the first matched phrase). Evaluates the FULL rule text it is given.
    Both UP and DOWN present → NEUTRAL with both phrases listed (the document is self-contradictory on direction)."""
    text = text or ""
    ups = [m for rx in UP_RX for m in [rx.search(text)] if m]
    downs = [m for rx in DOWN_RX for m in [rx.search(text)] if m]
    neutral = [m for rx in NEUTRAL_RX for m in [rx.search(text)] if m]
    first = min((m.start() for m in ups + downs + neutral), default=None)
    if ups and downs:
        return NEUTRAL, "UP:" + " | ".join(m.group(0) for m in ups) + "; DOWN:" + " | ".join(m.group(0) for m in downs), first
    if ups:
        return UP, "UP:" + " | ".join(m.group(0) for m in ups), first
    if downs:
        return DOWN, "DOWN:" + " | ".join(m.group(0) for m in downs), first
    if neutral:
        return NEUTRAL, "NEUTRAL:" + " | ".join(m.group(0) for m in neutral), first
    return NA, "", None


def direction_for(text: str) -> Tuple[str, str]:
    d, m, _ = direction_scan(text)
    return d, m


def metric_direction(value: Optional[dict]) -> Tuple[str, str]:
    """Direction of a computed METRIC item from its own numbers (set by the adapter that computed them)."""
    v = value or {}
    if "revenue_up" in v:
        if v.get("margin_up") is None:
            return (NEUTRAL, "revenue up q/q; gross margin not comparable") if v["revenue_up"] else (DOWN, "revenue down q/q; gross margin not comparable")
        if v["revenue_up"] and v["margin_up"]:
            return UP, "revenue up q/q and gross margin not lower"
        if not v["revenue_up"] and not v["margin_up"]:
            return DOWN, "revenue down q/q and gross margin lower"
        return NEUTRAL, "one of revenue / gross margin improved"
    if v.get("signed") is not None:
        return (UP if v["signed"] > 0 else DOWN if v["signed"] < 0 else NEUTRAL), "sign of the computed value"
    return NA, ""


def claim_key(item: dict) -> str:
    """Deterministic identity of what an item is a claim about. Computed items: subject + the exact measure; sourced text: subject +
    category. Only items with comparable keys can contradict each other."""
    subject = str(item.get("subject_ticker") or "").upper()
    if item.get("kind") in COMPUTED_KINDS:
        return f"{subject}|{item.get('kind')}|{item.get('source_identifier') or ''}"
    return f"{subject}|{item.get('category')}"


# ---------------------------------------------------------------- cause attribution --------------------------------------

def _in_window(d: str, start: date, end: date) -> bool:
    try:
        x = date.fromisoformat(d[:10])
    except (ValueError, TypeError):
        return False
    return start <= x <= end


def cause_attribution(facts: dict, items: List[dict]) -> dict:
    """INFERENCE. Fails closed: without a sector benchmark the cause is NOT_SEPARABLE — a stock falling more than SPY is never, by
    itself, read as company-specific. With a sector benchmark: the sector-explained share of the decline window plus tier-1 negative
    company items inside it."""
    stock = facts.get("stock_ret")
    if stock is None:
        return {"verdict": "NOT_AVAILABLE", "reason": "no decline window could be computed (missing bars)", "inputs": {}, "item_ids": [], "epistemic": INFERENCE}
    inputs = {k: facts.get(k) for k in ("window_start", "window_end", "peak_close", "trough_close", "stock_ret", "spy_ret", "sector_ret", "sector_etf")}
    if stock >= DECLINE_THRESHOLD:
        return {"verdict": "NO_MATERIAL_DECLINE", "reason": f"peak-to-trough move {stock:+.1%} is above {DECLINE_THRESHOLD:+.0%}", "inputs": inputs, "item_ids": [],
                "epistemic": INFERENCE}
    ws, we = facts.get("window_start"), facts.get("window_end")
    try:
        start, end = date.fromisoformat(ws), date.fromisoformat(we)
    except (TypeError, ValueError):
        start, end = date.min, date.max
    company_down = [i["item_id"] for i in items if i["category"] in (COMPANY_SPECIFIC, COMPANY_CONFIRMATION) and i["source_tier"] == 1
                    and i["direction"] == DOWN and i["kind"] not in COMPUTED_KINDS and _in_window(i["evidence_date"], start, end)]
    sector = facts.get("sector_ret")
    if sector is None:
        reason = (f"{NOT_SEPARABLE_REASON}. Stock {stock:+.1%} over the decline window ({ws} to {we}); SPY {_pct(facts.get('spy_ret'))} is shown for context only; "
                  f"{len(company_down)} tier-1 negative company item(s) inside the window are listed, not scored")
        return {"verdict": NOT_SEPARABLE, "reason": reason, "inputs": {**inputs, "benchmark": None, "explained_share": None}, "item_ids": company_down,
                "epistemic": INFERENCE}
    share = max(0.0, min(1.0, sector / stock)) if (sector < 0 and stock < 0) else 0.0
    if share >= MARKET_SHARE_HIGH and not company_down:
        verdict = "MARKET_WIDE_DOMINANT"
    elif share < MARKET_SHARE_LOW or (company_down and share < MARKET_SHARE_HIGH):
        verdict = "COMPANY_SPECIFIC_DOMINANT"
    else:
        verdict = "MIXED"
    reason = (f"sector ETF {facts.get('sector_etf')} moved {sector:+.1%} while the stock moved {stock:+.1%} over the decline window ({ws} to {we}); explained share "
              f"{share:.0%}; {len(company_down)} tier-1 negative company-specific item(s) inside the window")
    return {"verdict": verdict, "reason": reason, "inputs": {**inputs, "benchmark": "sector ETF", "explained_share": round(share, 4)}, "item_ids": company_down,
            "epistemic": INFERENCE}


# ---------------------------------------------------------------- contradictions ------------------------------------------

def comparable(a: dict, b: dict) -> bool:
    """Two items can contradict only when they are about the same thing: same category, same subject, and — for computed metrics or
    events — the same claim key (the same measure of the same symbol). SNDK DOWN vs SPY UP is never a contradiction."""
    if a.get("category") != b.get("category"):
        return False
    ka, kb = a.get("claim_key") or claim_key(a), b.get("claim_key") or claim_key(b)
    if a.get("kind") in COMPUTED_KINDS or b.get("kind") in COMPUTED_KINDS:
        return ka == kb
    return str(a.get("subject_ticker") or "").upper() == str(b.get("subject_ticker") or "").upper()


def mark_contradictions(items: List[dict]) -> int:
    """Opposite directions (UP vs DOWN), comparable items (see `comparable`), both tier <= 3, within 30 days → both flagged."""
    flagged = 0
    for i in items:
        i.setdefault("contradiction", 0)
        i.setdefault("contradicts_item_id", None)
        i.setdefault("claim_key", claim_key(i))
    pool = [i for i in items if i["direction"] in (UP, DOWN) and i["source_tier"] <= 3]
    for a in pool:
        for b in pool:
            if a is b or a["direction"] == b["direction"] or not comparable(a, b):
                continue
            try:
                da, db = date.fromisoformat(a["evidence_date"]), date.fromisoformat(b["evidence_date"])
            except ValueError:
                continue
            if abs((da - db).days) <= CONTRADICTION_DAYS and not a["contradiction"]:
                a["contradiction"], a["contradicts_item_id"] = 1, b["item_id"]
                flagged += 1
    return flagged


# ---------------------------------------------------------------- ladder ---------------------------------------------------

def _status(two: Iterable[Optional[bool]]) -> str:
    vals = list(two)
    if any(v is None for v in vals):
        return NOT_AVAILABLE if all(v is None for v in vals) else (PARTIAL if any(v for v in vals if v is not None) else NOT_SUPPORTED)
    if all(vals):
        return SUPPORTED
    if any(vals):
        return PARTIAL
    return NOT_SUPPORTED


def _rung(n: int, status: str, reason: str, inputs: dict, item_ids: List[str]) -> dict:
    return {"rung": n, "name": dict(RUNGS)[n], "status": status, "reason": reason, "inputs": inputs, "item_ids": item_ids}


def _degrade_if_contradicted(rung: dict, used: List[dict]) -> dict:
    if rung["status"] == SUPPORTED and any(i.get("contradiction") for i in used):
        rung["status"], rung["reason"] = PARTIAL, rung["reason"] + "; downgraded: a contradicting item sits among the inputs"
    return rung


def _rung4(facts: dict, items: List[dict]) -> dict:
    """Company fundamentals. SUPPORTED only when (A) validated, semantically compatible XBRL quarters improve on both revenue and gross
    margin and no official filing text says otherwise, or (B) official tier-1 earnings / guidance text of the target says UP and XBRL
    does not say DOWN. Ambiguous XBRL → at most PARTIAL. Text and XBRL disagreeing → PARTIAL with an explicit ambiguity note."""
    f = facts.get("fundamentals") or {}
    text = [i for i in items if i["category"] == COMPANY_CONFIRMATION and i["kind"] in ("EARNINGS", "GUIDANCE") and i["source_tier"] == 1 and i["direction"] in (UP, DOWN)]
    t_up, t_down = [i for i in text if i["direction"] == UP], [i for i in text if i["direction"] == DOWN]
    has_xbrl = "revenue_up" in f
    if not has_xbrl and not text:
        return _rung(4, NOT_AVAILABLE, "no quarterly company facts (no CIK, no fetch, or fewer than two comparable quarters) and no official results text", f, [])
    xbrl_dir = None
    if has_xbrl:
        if f["revenue_up"] and f.get("margin_up") is True:
            xbrl_dir = UP
        elif not f["revenue_up"] and f.get("margin_up") is False:
            xbrl_dir = DOWN
        else:
            xbrl_dir = NEUTRAL
    valid = f.get("validation") == "OK"
    reasons = list(f.get("reasons") or [])
    metric_ids = [i["item_id"] for i in items if i["kind"] == "METRIC" and i["category"] == COMPANY_CONFIRMATION]
    ids = metric_ids + [i["item_id"] for i in text]
    base = (f"XBRL {f.get('revenue_concept', 'n/a')}: revenue {f.get('revenue_latest')} vs prior {f.get('revenue_prior')} "
            f"({'up' if f.get('revenue_up') else 'not up'}); gross margin {_pct(f.get('gm_latest'))} vs {_pct(f.get('gm_prior'))} "
            f"({'not lower' if f.get('margin_up') else 'lower' if f.get('margin_up') is False else 'not comparable'}); validation {f.get('validation', 'n/a')}"
            + (f" ({'; '.join(reasons)})" if reasons else "")) if has_xbrl else "no XBRL quarters"
    txt = f"; official text UP {len(t_up)} / DOWN {len(t_down)}"
    inputs = {**f, "text_up": len(t_up), "text_down": len(t_down), "xbrl_direction": xbrl_dir, "ambiguity": False}
    disagree = (xbrl_dir == UP and t_down) or (xbrl_dir == DOWN and t_up)
    if disagree:
        inputs["ambiguity"] = True
        return _rung(4, PARTIAL, base + txt + "; AMBIGUITY: official filing text and XBRL point in opposite directions — not SUPPORTED", inputs, ids)
    if (valid and xbrl_dir == UP and not t_down) or (t_up and not t_down and xbrl_dir != DOWN):
        why = "A: validated XBRL quarters improved on revenue and gross margin" if (valid and xbrl_dir == UP) else "B: official filing text confirms improvement"
        return _rung(4, SUPPORTED, base + txt + f"; {why}", inputs, ids)
    if xbrl_dir == DOWN and not t_up:
        return _rung(4, NOT_SUPPORTED, base + txt, inputs, ids)
    if xbrl_dir in (UP, NEUTRAL) or t_up:
        if xbrl_dir == UP and not valid:
            inputs["ambiguity"] = True
            return _rung(4, PARTIAL, base + txt + "; XBRL concept mapping ambiguous — capped at PARTIAL", inputs, ids)
        return _rung(4, PARTIAL, base + txt, inputs, ids)
    return _rung(4, NOT_AVAILABLE, base + txt, inputs, ids)


def ladder(facts: dict, items: List[dict], related: Optional[Dict[str, List[str]]] = None) -> List[dict]:
    related = related or {}
    rel = {t.upper() for group in related.values() for t in group}
    rungs: List[dict] = []
    # 1 market backdrop
    s20, above50 = facts.get("spy_20"), facts.get("spy_above_ema50")
    r1 = _status([None if s20 is None else s20 > 0, above50])
    rungs.append(_rung(1, r1, f"SPY 20-session return {_pct(s20)}; SPY above 50-day EMA: {_yn(above50)}", {"spy_20": s20, "spy_above_ema50": above50}, []))
    # 2 industry fundamentals
    ind = [i for i in items if i["category"] == INDUSTRY_SUPPLY_CHAIN and i["source_tier"] <= 3 and i["direction"] in (UP, DOWN)]
    ups, downs = [i for i in ind if i["direction"] == UP], [i for i in ind if i["direction"] == DOWN]
    e20 = facts.get("etf_20")
    if e20 is None and not ind:
        r2 = NOT_AVAILABLE
    else:
        etf_ok = None if e20 is None else e20 > 0
        items_ok = (True if ups and not downs else False if (downs and not ups) or (not ups and not downs) else None)
        r2 = PARTIAL if (ups and downs) else _status([etf_ok, items_ok])
    rungs.append(_degrade_if_contradicted(_rung(2, r2, f"sector ETF {facts.get('sector_etf') or 'n/a'} 20-session return {_pct(e20)}; industry items UP {len(ups)} / DOWN {len(downs)}",
                                                {"etf_20": e20, "up": len(ups), "down": len(downs)}, [i["item_id"] for i in ind]), ind))
    # 3 supplier / customer confirmation
    sc = [i for i in items if i["category"] == INDUSTRY_SUPPLY_CHAIN and i["role"] in ("supplier", "customer") and i["direction"] in (UP, DOWN)]
    if not rel and not sc:
        r3, why = NOT_AVAILABLE, "no supplier or customer declared and no supplier / customer item"
    elif not sc:
        r3, why = NOT_AVAILABLE, "no supplier / customer item in the window"
    else:
        t2_up = [i for i in sc if i["direction"] == UP and i["source_tier"] <= 2]
        any_up, any_down = [i for i in sc if i["direction"] == UP], [i for i in sc if i["direction"] == DOWN]
        if t2_up and not any_down:
            r3, why = SUPPORTED, f"{len(t2_up)} official (tier <= 2) supplier / customer item(s) UP, none DOWN"
        elif any_up and any_down:
            r3, why = PARTIAL, f"UP {len(any_up)} and DOWN {len(any_down)} supplier / customer items"
        elif any_up:
            r3, why = PARTIAL, f"UP items only from tier 3-4 sources ({len(any_up)})"
        else:
            r3, why = NOT_SUPPORTED, f"only DOWN supplier / customer items ({len(any_down)})"
    rungs.append(_degrade_if_contradicted(_rung(3, r3, why, {"declared": sorted(rel)}, [i["item_id"] for i in sc]), sc))
    # 4 company fundamentals
    rungs.append(_rung4(facts, items))
    # 5 company guidance
    g = [i for i in items if i["kind"] == "GUIDANCE" and i["source_tier"] == 1 and i["category"] == COMPANY_CONFIRMATION]
    g_up, g_down, g_neutral = [i for i in g if i["direction"] == UP], [i for i in g if i["direction"] == DOWN], [i for i in g if i["direction"] == NEUTRAL]
    if not g:
        r5, why = NOT_AVAILABLE, "no tier-1 guidance item in the window"
    elif g_up and not g_down:
        r5, why = SUPPORTED, f"{len(g_up)} tier-1 guidance item(s) UP, none DOWN"
    elif g_up and g_down:
        r5, why = PARTIAL, "guidance items both UP and DOWN"
    elif g_down:
        r5, why = NOT_SUPPORTED, f"only DOWN guidance items ({len(g_down)})"
    else:
        r5, why = PARTIAL, f"guidance reaffirmed / neutral only ({len(g_neutral)})"
    rungs.append(_degrade_if_contradicted(_rung(5, r5, why, {"up": len(g_up), "down": len(g_down), "neutral": len(g_neutral)}, [i["item_id"] for i in g]), g))
    # 6 market / price confirmation
    above20, rs = facts.get("price_above_ema20"), facts.get("rel_strength_20")
    r6 = _status([above20, None if rs is None else rs > 0])
    rungs.append(_rung(6, r6, f"close above EMA20: {_yn(above20)}; 20-session relative strength vs sector {_pct(rs)}", {"price_above_ema20": above20, "rel_strength_20": rs}, []))
    return rungs


def _pct(x) -> str:
    return "n/a" if x is None else f"{x:+.1%}"


def _yn(x) -> str:
    return "n/a" if x is None else ("yes" if x else "no")


# ---------------------------------------------------------------- strength -------------------------------------------------

def strength(rungs: List[dict]) -> dict:
    pts = {SUPPORTED: 2, PARTIAL: 1, NOT_SUPPORTED: 0, NOT_AVAILABLE: 0}
    points = sum(pts[r["status"]] for r in rungs)
    by = {r["rung"]: r["status"] for r in rungs}
    confirmed = by.get(4) == SUPPORTED or by.get(5) == SUPPORTED
    grade = STRONG if (points >= STRONG_POINTS and confirmed) else MODERATE if points >= MODERATE_POINTS else WEAK
    sentences = []
    early_ok = all(by.get(n) != NOT_SUPPORTED for n in (1, 2, 3)) and any(by.get(n) in (SUPPORTED, PARTIAL) for n in (1, 2, 3))
    if early_ok and not confirmed:
        sentences.append(CONFIRMATION_MISSING)
    if points >= STRONG_POINTS and not confirmed:
        sentences.append("Points would reach STRONG but neither company fundamentals nor company guidance is SUPPORTED, so the grade is capped at MODERATE.")
    text = (f"{grade}: {points} of 12 points (SUPPORTED = 2, PARTIAL = 1); company confirmation {'present' if confirmed else 'absent'}. "
            + " ".join(sentences) + (" " if sentences else "") + STRENGTH_DISCLAIMER)
    return {"grade": grade, "points": points, "confirmed": confirmed, "sentences": sentences, "text": text}


# ---------------------------------------------------------------- gaps and next events -------------------------------------

_FILL = {1: "SPY bars covering the window (market backdrop cannot be judged)",
         2: "sector ETF bars or at least one industry / supply-chain item from a tier <= 3 source",
         3: "an official report from a declared supplier or customer (declare related tickers and supply their IR or SEC material)",
         4: "two consecutive, semantically compatible quarters of company facts from SEC XBRL, or official results text of the company",
         5: "a tier-1 company statement mentioning guidance or outlook inside the window",
         6: "price bars for the ticker and its sector ETF (price confirmation cannot be judged)"}


def gaps(rungs: List[dict], fetches: List[dict]) -> List[dict]:
    out = [{"kind": "rung", "rung": r["rung"], "text": f"Rung {r['rung']} ({r['name']}) is NOT_AVAILABLE: needs {_FILL[r['rung']]}."} for r in rungs if r["status"] == NOT_AVAILABLE]
    for r in rungs:
        if (r.get("inputs") or {}).get("ambiguity"):
            out.append({"kind": "ambiguity", "rung": r["rung"], "text": f"Rung {r['rung']} ({r['name']}) is capped at PARTIAL: {r['reason'].split(';')[-1].strip()}."})
    for f in fetches:
        if f["status"] != "OK":
            out.append({"kind": "fetch", "text": f"{f['adapter']} could not retrieve {f['url']}: {f['status']}{' (' + f['error'] + ')' if f.get('error') else ''}."})
    return out


def next_events(facts: dict) -> List[dict]:
    out = []
    e = facts.get("next_earnings")
    if e:
        out.append({"event": "next earnings report", "date": e.get("date"), "source": e.get("source"), "title": e.get("title"),
                    "would": "strengthen the thesis if revenue, gross margin and guidance improve against the stored quarter; weaken it if they do not"})
    else:
        out.append({"event": "next earnings report", "date": None, "source": facts.get("earnings_reason") or "earnings calendar unavailable", "title": None,
                    "would": "the first official confirmation or disconfirmation of company fundamentals"})
    f = facts.get("next_fomc")
    if f:
        out.append({"event": "next FOMC decision", "date": f.get("date"), "source": f.get("source"), "title": f.get("title"),
                    "would": "move the market backdrop rung either way; it says nothing about the company"})
    return out
