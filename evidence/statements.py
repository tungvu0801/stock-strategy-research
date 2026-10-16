"""
evidence/statements.py — context-aware, sentence-level classification of sourced text (DESIGN_54 §14). Pure; no I/O.

A direction is never assigned from a keyword found somewhere in a long document. The extracted text is segmented into bounded
statements (sentences / short blocks); every rule must match inside ONE statement with a subject and a predicate, and the
matched statement, phrase, rule id and offset are kept. Three independent kinds of direction are produced:

  GUIDANCE   forward-looking statements (expect / outlook / guidance / forecast ... + a metric or business condition, or a numeric range).
             GUIDANCE_PRESENT is separate from GUIDANCE_DIRECTION: "expects revenue of $2.0B to $2.2B" is guidance with direction NA;
             "raised its outlook" is UP; "lowered its outlook" / "expects demand to weaken" is DOWN; "reaffirmed its outlook" is NEUTRAL.
  OPERATING  realised business developments with subject + predicate ("orders increased 20%", "gross margin declined", "demand weakened");
             each carries the measure it is about, so rung 4 can compare it with the XBRL metric. Bare nouns ("restructuring charges",
             "impairment", a table row, a heading) never produce a direction.
  PRICE      headline price moves ("shares fall 6%", "stock jumps"); classified separately and never usable as company fundamentals.

Index / landing pages (news listings, IR home pages, events pages) are discovery context only: no guidance, no direction, no
page-derived date.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

from evidence import DOWN, NA, NEUTRAL, UP

MAX_STATEMENT_CHARS = 700            # longer blocks are boilerplate / tables, never a statement
MAX_STORED_STATEMENTS = 4
MAX_STORED_STATEMENT_CHARS = 300

_YEAR = r"(?:full[- ]year\s+|fiscal\s+(?:\d{4}\s+)?|annual\s+|\d{4}\s+|(?:first|second|third|fourth|next|current)[- ]quarter\s+|q[1-4]\s+)?"
_GUIDE = r"(?:guidance|outlook|forecast|forecasts)"
_FWD = r"\b(?:expect|expects|expected|expecting|forecast|forecasts|forecasted|outlook|guidance|anticipate|anticipates|anticipated|project|projects|projected|projections?|estimate|estimates|estimated|target|targets|targeting)\b"
_METRIC = (r"\b(?:revenues?|net\s+sales|sales|gross\s+margins?|margins?|operating\s+(?:income|expenses?|margin)|eps|earnings\s+per\s+share|net\s+income|"
           r"demand|shipments|bit\s+(?:growth|supply)|free\s+cash\s+flow|capex|capital\s+expenditures|pricing|asps?|orders|bookings|"
           r"(?:next|first|second|third|fourth|fiscal)\s+quarter|fiscal\s+(?:year|20\d{2})|full[- ]year)\b")
_RANGE = r"(?:\$\s?\d[\d,]*(?:\.\d+)?\s*(?:billion|million|bn|mn|b|m)?\s*(?:to|-|–|—)\s*\$?\s?\d[\d,]*(?:\.\d+)?(?:\s*(?:billion|million|bn|mn|b|m)\b)?|\d+(?:\.\d+)?\s*%\s*(?:to|-|–|—)\s*\d+(?:\.\d+)?\s*%)"
_SAFE_HARBOR = re.compile(r"forward[- ]looking\s+statements|safe\s+harbor|private\s+securities\s+litigation|risk\s+factors|undue\s+reliance", re.I)
_MEAS = r"(?P<measure>revenues?|net\s+sales|sales|shipments|orders|bookings|demand|gross\s+margins?|margins?|pricing|asps?|bit\s+shipments|volume)"
_UPV = r"(?:grew|increased|improved|expanded|rose|strengthened|accelerated|recovered|normali[sz]ed|climbed|doubled)"
_DNV = r"(?:fell|declined|decreased|contracted|weakened|deteriorated|softened|slowed|dropped|shrank|halved)"

RULES: Dict[str, Tuple[str, str, str]] = {        # id -> (kind, direction, pattern)
    "guidance_raised":  ("GUIDANCE", UP,      r"\b(?:raised?|raises|raising|lifted|lifts|increased?)\b[^.;\n]{0,30}?\b(?:its\s+|the\s+|our\s+)?" + _YEAR + _GUIDE + r"\b"),
    "guidance_raised2": ("GUIDANCE", UP,      r"\b" + _GUIDE + r"\b[^.;\n]{0,40}?\b(?:was|were|has\s+been|is|are)?\s*(?:raised|increased|lifted)\b"),
    "guidance_lowered": ("GUIDANCE", DOWN,    r"\b(?:lower(?:s|ed|ing)?|cuts?|cutting|reduce[sd]?|reducing|trimmed|trims|withdrew|withdraws|withdrawn|suspended|suspends)\b[^.;\n]{0,30}?\b(?:its\s+|the\s+|our\s+)?" + _YEAR + _GUIDE + r"\b"),
    "guidance_lowered2": ("GUIDANCE", DOWN,   r"\b" + _GUIDE + r"\b[^.;\n]{0,40}?\b(?:was|were|has\s+been|is|are)?\s*(?:lowered|cut|reduced|trimmed|withdrawn|suspended)\b"),
    "guidance_reaffirmed": ("GUIDANCE", NEUTRAL, r"\b(?:reaffirm|maintain|reiterat|confirm)\w*\s+(?:its\s+|the\s+|our\s+)?" + _YEAR + _GUIDE + r"\b"),
    # the metric must be the OBJECT of the expectation: at most three words between the forward verb and the metric, the predicate right after it
    "expect_up":        ("GUIDANCE", UP,      _FWD + r"\s+(?:that\s+)?(?:[a-z-]+\s+){0,3}?" + _METRIC + r"\s+(?:to\s+|will\s+|should\s+|would\s+|continues?\s+to\s+|is\s+expected\s+to\s+)?(?:improve|increase|grow|strengthen|recover|expand|rise|accelerate)\w*\b"),
    "expect_down":      ("GUIDANCE", DOWN,    _FWD + r"\s+(?:that\s+)?(?:[a-z-]+\s+){0,3}?" + _METRIC + r"\s+(?:to\s+|will\s+|should\s+|would\s+|continues?\s+to\s+|is\s+expected\s+to\s+)?(?:decline|decrease|weaken|soften|fall|contract|deteriorate|slow|moderate)\w*\b"),
    "expect_growth":    ("GUIDANCE", UP,      _FWD + r"\s+(?:that\s+)?(?:[a-z-]+\s+){0,3}?(?:" + _METRIC + r"\s+(?:growth|improvement|strength|recovery)\b|(?:growth|improvement|strength|recovery)\s+(?:in|of)\s+(?:[a-z-]+\s+){0,2}?" + _METRIC + r")"),
    "expect_decline":   ("GUIDANCE", DOWN,    _FWD + r"\s+(?:that\s+)?(?:[a-z-]+\s+){0,3}?(?:" + _METRIC + r"\s+(?:decline|weakness|softness|headwinds?)\b|(?:decline|weakness|softness|headwinds?)\s+(?:in|of)\s+(?:[a-z-]+\s+){0,2}?" + _METRIC + r")"),
    "op_metric_up":     ("OPERATING", UP,     _MEAS + r"\b[^.;\n]{0,40}?\b(?:" + _UPV + r")\b"),
    "op_metric_down":   ("OPERATING", DOWN,   _MEAS + r"\b[^.;\n]{0,40}?\b(?:" + _DNV + r")\b"),
    "op_record":        ("OPERATING", UP,     r"\brecord\s+(?:quarterly\s+)?(?P<measure>revenues?|sales|shipments|quarter)\b"),
    "op_beat":          ("OPERATING", UP,     r"\b(?:better|stronger)[- ]than[- ]expected\b[^.;\n]{0,40}?(?P<measure>revenues?|sales|demand|margins?|results|orders|shipments)?"),
    "op_miss":          ("OPERATING", DOWN,   r"\b(?:weaker|worse|softer)[- ]than[- ]expected\b[^.;\n]{0,40}?(?P<measure>revenues?|sales|demand|margins?|results|orders|shipments)?"),
    "op_weak":          ("OPERATING", DOWN,   _MEAS + r"\s+(?:was|were|is|are|remained?|stayed)\s+(?:weak|soft|sluggish|muted|subdued)\b"),
    "op_strong":        ("OPERATING", UP,     _MEAS + r"\s+(?:was|were|is|are|remained?|stayed)\s+(?:strong|robust|solid|healthy)\b"),
    "op_inventory":     ("OPERATING", UP,     r"\binventor(?:y|ies)\s+(?:levels?\s+)?(?:normali[sz]ed|normali[sz]ation|has\s+normali[sz]ed|have\s+normali[sz]ed)\b"),
    "op_inline":        ("OPERATING", NEUTRAL, r"\bin\s+line\s+with\s+(?:our\s+|the\s+)?(?:expectations|guidance|outlook|prior\s+guidance)\b"),
    "price_down":       ("PRICE", DOWN,       r"\b(?:shares?|stock)\s+(?:is\s+|are\s+)?(?:fall(?:s|ing)?|fell|drop(?:s|ped|ping)?|slid(?:e|es|ing)?|tumbl(?:e|es|ed|ing)|plung(?:e|es|ed|ing)|s(?:i|a)nk(?:s|ing)?|slump(?:s|ed|ing)?|declin(?:e|es|ed|ing)|down)\b"),
    "price_up":         ("PRICE", UP,         r"\b(?:shares?|stock)\s+(?:is\s+|are\s+)?(?:ris(?:e|es|ing)|rose|jump(?:s|ed|ing)?|rall(?:y|ies|ied|ying)|gain(?:s|ed|ing)?|surg(?:e|es|ed|ing)|soar(?:s|ed|ing)?|climb(?:s|ed|ing)?|up)\b"),
}
_COMPILED = {rid: (kind, d, re.compile(pat, re.I)) for rid, (kind, d, pat) in RULES.items()}
_FWD_RX, _METRIC_RX, _RANGE_RX = re.compile(_FWD, re.I), re.compile(_METRIC, re.I), re.compile(_RANGE, re.I)
_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z\"“(•\-–])|\n+")
_INDEX_PATH = re.compile(r"(?:/(?:news|news-releases|press-releases|news-events(?:/[a-z-]+)?|events|ir|investors?|investor-relations|media)$|/(?:news|ir|index|default)\.html?$)", re.I)
_DATE_LINE = re.compile(r"\b(?:20\d{2}-\d{2}-\d{2}|(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},\s+20\d{2}|\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*\s+20\d{2})\b")
MEASURE_GROUPS = {"revenue": ("revenue", "revenues", "net sales", "sales"), "gross_margin": ("gross margin", "gross margins", "margin", "margins")}


# ---------------------------------------------------------------- segmentation ------------------------------------------------

def segment(text: str) -> List[Tuple[int, str]]:
    """(offset, statement) for every bounded statement; blocks longer than MAX_STATEMENT_CHARS are split on sentence ends."""
    out: List[Tuple[int, str]] = []
    pos = 0
    for part in _SPLIT.split(text or ""):
        if part is None:
            continue
        start = text.find(part, pos) if part else pos
        if start < 0:
            start = pos
        pos = start + len(part)
        s = part.strip()
        if s:
            out.append((start + (len(part) - len(part.lstrip())), s))
    return out


def _measure_group(m: re.Match) -> Optional[str]:
    try:
        raw = (m.group("measure") or "").lower()
    except IndexError:
        raw = ""
    raw = re.sub(r"\s+", " ", raw)
    for group, names in MEASURE_GROUPS.items():
        if raw in names:
            return group
    return raw or None


def is_index_page(url: Optional[str], text: str, title: str = "") -> bool:
    """A news / IR / events listing or landing page: known path shapes, or many short dated lines without a real paragraph."""
    path = ""
    if url:
        try:
            path = re.sub(r"^[a-z]+://[^/]+", "", url).split("?")[0].rstrip("/") or "/"
        except Exception:  # noqa: BLE001
            path = ""
        p = path.rstrip("/")
        if p in ("", "/"):
            return True
        if _INDEX_PATH.search(p):
            return True
    lines = [ln for ln in (text or "").split("\n") if ln.strip()]
    if len(lines) >= 12:
        dated = sum(1 for ln in lines if _DATE_LINE.search(ln) and len(ln) < 160)
        longest = max((len(ln) for ln in lines), default=0)
        if dated >= 5 and longest < 400:
            return True
    return bool(re.search(r"\b(?:news\s+releases?|press\s+releases?|ir\s+news|investor\s+relations)\b\s*\|?\s*$", title or "", re.I)) and len(lines) >= 12


# ---------------------------------------------------------------- classification ---------------------------------------------

def guidance_present(statement: str) -> bool:
    """Forward-looking term + a metric / business condition in the same bounded statement, or a numeric range with a forward term.
    Safe-harbor boilerplate never counts."""
    if len(statement) > MAX_STATEMENT_CHARS or _SAFE_HARBOR.search(statement):
        return False
    if not _FWD_RX.search(statement):
        return False
    return bool(_METRIC_RX.search(statement) or _RANGE_RX.search(statement))


def classify_statement(statement: str, offset: int) -> List[dict]:
    """Every rule matched inside ONE statement (subject + predicate), with the local evidence kept."""
    hits: List[dict] = []
    if len(statement) > MAX_STATEMENT_CHARS:
        return hits
    boiler = bool(_SAFE_HARBOR.search(statement))
    for rid, (kind, d, rx) in _COMPILED.items():
        m = rx.search(statement)
        if not m or (boiler and kind == "GUIDANCE"):
            continue
        hits.append({"rule": rid, "kind": kind, "direction": d, "measure": _measure_group(m) if kind == "OPERATING" else None,
                     "phrase": m.group(0)[:120], "statement": statement[:MAX_STORED_STATEMENT_CHARS], "offset": offset + m.start()})
    return hits


def _aggregate(dirs: List[str]) -> str:
    s = {d for d in dirs if d in (UP, DOWN)}
    if s == {UP}:
        return UP
    if s == {DOWN}:
        return DOWN
    if s == {UP, DOWN}:
        return NEUTRAL
    return NEUTRAL if any(d == NEUTRAL for d in dirs) else NA


def analyze(text: str, *, headline: bool = False, index_page: bool = False) -> dict:
    """Structured, deterministic analysis of one document's rule text."""
    out = {"guidance_present": False, "guidance_direction": NA, "operating_direction": NA, "price_direction": NA, "direction": NA, "direction_kind": "NA",
           "statements": [], "rule_match": "", "rule_offset": None, "index_page": bool(index_page), "n_statements": 0}
    if index_page:
        out["rule_match"] = "index/landing page: discovery context only, no direction"
        return out
    segments = segment(text)
    out["n_statements"] = len(segments)
    guidance: List[dict] = []
    operating: List[dict] = []
    price: List[dict] = []
    for off, st in segments:
        hits = classify_statement(st, off)
        g_hits = [h for h in hits if h["kind"] == "GUIDANCE"]    # every GUIDANCE rule embeds its own context (raised / lowered / reaffirmed guidance, expects <metric> to ...)
        if g_hits:
            guidance.extend(g_hits)
        elif guidance_present(st):                               # forward guidance exists but says nothing about direction versus expectations
            guidance.append({"rule": "guidance_present", "kind": "GUIDANCE", "direction": NA, "measure": None,
                             "phrase": (_RANGE_RX.search(st) or _METRIC_RX.search(st)).group(0)[:120], "statement": st[:MAX_STORED_STATEMENT_CHARS], "offset": off})
        operating.extend(h for h in hits if h["kind"] == "OPERATING")
        if headline:
            price.extend(h for h in hits if h["kind"] == "PRICE")
    out["guidance_present"] = bool(guidance)
    out["guidance_direction"] = _aggregate([g["direction"] for g in guidance]) if guidance else NA
    out["operating_direction"] = _aggregate([o["direction"] for o in operating]) if operating else NA
    out["price_direction"] = _aggregate([p["direction"] for p in price]) if price else NA
    if guidance and out["guidance_direction"] != NA:
        out["direction"], out["direction_kind"] = out["guidance_direction"], "GUIDANCE"
    elif operating and out["operating_direction"] != NA:
        out["direction"], out["direction_kind"] = out["operating_direction"], "OPERATING"
    elif guidance:
        out["direction"], out["direction_kind"] = NA, "GUIDANCE"
    elif price and out["price_direction"] != NA:
        out["direction"], out["direction_kind"] = out["price_direction"], "PRICE"
    chosen = (guidance + operating + price)
    chosen.sort(key=lambda h: h["offset"])
    out["statements"] = chosen[:MAX_STORED_STATEMENTS]
    out["rule_match"] = "; ".join(f"{h['rule']}:{h['direction']}:{h['phrase']}" for h in chosen[:6])[:500]
    out["rule_offset"] = chosen[0]["offset"] if chosen else None
    return out


def operating_direction_for(analysis: dict, measures: Tuple[str, ...]) -> str:
    """Direction of the OPERATING statements about the given measure groups only (used by rung 4 comparability)."""
    dirs = [s["direction"] for s in analysis.get("statements", []) if s.get("kind") == "OPERATING" and s.get("measure") in measures]
    return _aggregate(dirs) if dirs else NA
