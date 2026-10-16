"""
evidence/engine.py — build ONE immutable evidence packet (DESIGN_54 §6).

    build_packet(EvidenceRequest(...)) -> {"packet": {...}, "items": [...], "fetches": [...]}

Order, each step bounded: internal facts → SEC (target, then declared related tickers) → IR → Alpaca headlines → manual URLs and
texts → deterministic rules (tier, label, category, direction, contradictions, cause attribution, ladder, strength, gaps, next
events) → hashes → one INSERT transaction. Any adapter failure is a stored fetch row, never an exception; an engine-level
problem produces a FAILED packet with a failure code. Items are sorted before ids and hashes are assigned, so the packet hash
does not depend on adapter order. Nothing here calls a model, a broker or Robinhood.
"""
from __future__ import annotations

import hashlib
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional

import config
from evidence import (COMPANY_CONFIRMATION, COMPANY_SPECIFIC, ENGINE_VERSION, INFERENCE, LIMITS, NA, ROLES, RULES_VERSION)
from evidence import rules as R
from evidence.net import host_of
from evidence.sources.alpaca_news import NewsAdapter
from evidence.sources.base import AdapterResult, SourceItem
from evidence.sources.company_ir import IrAdapter
from evidence.sources.internal import InternalAdapter
from evidence.sources.manual_url import ManualAdapter
from evidence.sources.sec_edgar import SecAdapter
from evidence.store import EvidenceStore, canonical_json, new_id

_TICKER = re.compile(r"^[A-Z][A-Z.]{0,9}$")
UNIVERSE_NOTE = ("Evidence packet from sourced items only (SEC EDGAR, the company's declared IR page, existing Alpaca headlines, user-supplied public URLs, "
                 "internal market / event data). Evidence strength is not a price probability. Research only; nothing here places or sizes an order.")


class EvidenceError(ValueError):
    pass


@dataclass
class EvidenceRequest:
    ticker: str
    as_of_date: Optional[date] = None
    lookback_days: int = config.EVIDENCE_LOOKBACK_DAYS
    related: Optional[Dict[str, List[str]]] = None          # {"suppliers": [...], "customers": [...]}; default config.EVIDENCE_RELATED[ticker]
    ir_urls: Optional[List[str]] = None                      # default config.EVIDENCE_IR_URLS[ticker]
    manual_urls: List[dict] = field(default_factory=list)    # [{url, role, related_ticker?, note?}]
    manual_texts: List[dict] = field(default_factory=list)   # [{text, role, related_ticker?, note?}]
    sources: Dict[str, bool] = field(default_factory=lambda: {"internal": True, "sec": True, "ir": True, "news": True, "manual": True})
    cik_overrides: Dict[str, str] = field(default_factory=dict)

    def normalised(self, today: date) -> "EvidenceRequest":
        t = (self.ticker or "").strip().upper()
        if not _TICKER.match(t):
            raise EvidenceError("INVALID_TICKER")
        as_of = self.as_of_date or today
        if as_of > today:
            raise EvidenceError("as_of_date is in the future")
        lb = int(self.lookback_days)
        if not LIMITS["lookback_min_days"] <= lb <= LIMITS["lookback_max_days"]:
            raise EvidenceError(f"lookback_days must be between {LIMITS['lookback_min_days']} and {LIMITS['lookback_max_days']}")
        related = self.related if self.related is not None else (config.EVIDENCE_RELATED.get(t) or {})
        related = {"suppliers": [str(x).upper() for x in (related.get("suppliers") or [])][:6], "customers": [str(x).upper() for x in (related.get("customers") or [])][:6]}
        ir_urls = list(self.ir_urls if self.ir_urls is not None else (config.EVIDENCE_IR_URLS.get(t) or []))[:LIMITS["ir_urls"]]
        if len(self.manual_urls) > LIMITS["manual_urls"] or len(self.manual_texts) > LIMITS["manual_urls"]:
            raise EvidenceError(f"at most {LIMITS['manual_urls']} manual URLs / texts")
        for spec in list(self.manual_urls) + list(self.manual_texts):
            if str(spec.get("role", "")).lower() not in ROLES:
                raise EvidenceError(f"manual item role must be one of {ROLES}")
        srcs = {k: bool(self.sources.get(k, True)) for k in ("internal", "sec", "ir", "news", "manual")}
        return EvidenceRequest(ticker=t, as_of_date=as_of, lookback_days=lb, related=related, ir_urls=ir_urls, manual_urls=list(self.manual_urls),
                               manual_texts=list(self.manual_texts), sources=srcs, cik_overrides={k.upper(): str(v) for k, v in (self.cik_overrides or {}).items()})

    def as_dict(self) -> dict:
        d = asdict(self)
        d["as_of_date"] = self.as_of_date.isoformat() if self.as_of_date else None
        return d


def default_adapters(now_fn: Callable[[], datetime]) -> Dict[str, object]:
    return {"internal": InternalAdapter(now_fn=now_fn), "sec": SecAdapter(now_fn=now_fn), "ir": IrAdapter(now_fn=now_fn),
            "news": NewsAdapter(now_fn=now_fn), "manual": ManualAdapter(now_fn=now_fn)}


# ---------------------------------------------------------------- item conversion ---------------------------------------------

def _item_dict(si: SourceItem, req: EvidenceRequest, fetch_ids: List[str], fetch_ok: List[bool]) -> dict:
    host = host_of(si.source_url) if si.source_url else (si.source_name if si.source_type == "IR_RELEASE" else "")
    tier = si.source_tier or R.tier_for(host=host, subject_ticker=si.subject_ticker, ticker=req.ticker, source_type=si.source_type, role=si.role,
                                        press_source=(si.extra or {}).get("press_source", ""), related=req.related)
    fetched_ok = (si.fetch_index is not None and si.fetch_index < len(fetch_ok) and fetch_ok[si.fetch_index]) or si.source_type in ("MARKET_DATA", "EVENT", "ALPACA_NEWS")
    epistemic = si.epistemic or R.epistemic_for(tier=tier, fetched_ok=bool(fetched_ok), has_url=bool(si.source_url) or si.source_type in ("MARKET_DATA", "EVENT", "SEC_FILING"),
                                                source_type=si.source_type)
    subject_is_target = si.subject_ticker.upper() == req.ticker
    category = si.category or R.category_for(role=si.role, source_type=si.source_type, subject_is_target=subject_is_target)
    if category == COMPANY_SPECIFIC and tier == 1 and si.kind in ("EARNINGS", "GUIDANCE") and subject_is_target:
        category = COMPANY_CONFIRMATION
    offset, kind, value = None, si.kind, dict(si.value or {})
    if si.kind == "METRIC":
        direction, match = R.metric_direction(si.value)
    elif si.kind == "EVENT" or epistemic == "UNCONFIRMED" and tier >= 5:
        direction, match = NA, ""
    else:                                                       # statement-level analysis of the FULL extracted document (capped), never the snippet
        from evidence import statements as ST
        an = ST.analyze((si.rule_text or si.text)[:LIMITS["rule_text_chars"]], headline=si.source_type == "ALPACA_NEWS",
                        index_page=(si.extra or {}).get("page_kind") == "index")
        direction, match, offset = an["direction"], an["rule_match"], an["rule_offset"]
        value.update({k: an[k] for k in ("guidance_present", "guidance_direction", "operating_direction", "price_direction", "direction_kind", "statements", "index_page")})
        if an["guidance_present"] and category in (COMPANY_SPECIFIC, COMPANY_CONFIRMATION) and si.kind in ("EARNINGS", "PAGE", "FILING", "HEADLINE"):
            kind = "GUIDANCE"
    if category == COMPANY_SPECIFIC and tier == 1 and kind in ("EARNINGS", "GUIDANCE") and subject_is_target:
        category = COMPANY_CONFIRMATION
    row = {"item_id": None, "packet_id": None, "position": 0, "ticker": req.ticker, "subject_ticker": si.subject_ticker.upper(), "source_type": si.source_type,
           "source_name": si.source_name, "source_url": si.source_url, "source_identifier": si.source_identifier, "published_at": si.published_at,
           "retrieved_at": si.retrieved_at, "evidence_date": si.evidence_date[:10], "category": category, "kind": kind, "text": si.text[:LIMITS["text_chars"] + 100],
           "value_json": value, "epistemic": epistemic, "direction": direction, "rule_match": (match or "")[:500], "rule_offset": offset, "ladder_rung": None,
           "ladder_status": None, "source_tier": int(tier), "contradiction": 0, "contradicts_item_id": None, "derived_from_json": [], "source_hash": si.source_hash,
           "fetch_id": fetch_ids[si.fetch_index] if si.fetch_index is not None and si.fetch_index < len(fetch_ids) else None, "role": si.role,
           "date_source": si.date_source}
    row["claim_key"] = R.claim_key(row)
    return row


def _hash_payload(req: EvidenceRequest, items: List[dict], fetches: List[dict], cause: dict, ladder: List[dict], strength: dict) -> str:
    skip = {"item_id", "packet_id", "fetch_id", "contradicts_item_id", "position"}
    rq = req.as_dict()
    rq["manual_urls"] = sorted(rq.get("manual_urls") or [], key=lambda m: str(m.get("url")))
    rq["manual_texts"] = sorted(rq.get("manual_texts") or [], key=lambda m: str(m.get("text")))
    body = {"request": rq, "rules_version": RULES_VERSION, "engine_version": ENGINE_VERSION,
            "items": [{k: v for k, v in i.items() if k not in skip} for i in items],
            "fetches": sorted(({k: f.get(k) for k in ("adapter", "purpose", "url", "status", "http_status", "content_hash")} for f in fetches),
                              key=lambda f: (f["adapter"], f["url"], f["purpose"])),
            "cause": {k: v for k, v in cause.items() if k != "item_ids"},
            "ladder": [{k: v for k, v in r.items() if k != "item_ids"} for r in ladder], "strength": strength}
    return hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- build --------------------------------------------------------

def build_packet(request: EvidenceRequest, *, adapters: Optional[Dict[str, object]] = None, now_fn: Optional[Callable[[], datetime]] = None,
                 store: Optional[EvidenceStore] = None, persist: bool = True) -> dict:
    now_fn = now_fn or (lambda: datetime.now(timezone.utc))
    now = now_fn()
    t0 = time.perf_counter()
    created = now.isoformat(timespec="seconds")
    try:
        req = request.normalised(now.date())
    except EvidenceError as exc:
        return _failed(request, created, "INVALID_TICKER" if str(exc) == "INVALID_TICKER" else "INTERNAL_ERROR", str(exc), t0, store, persist)
    ad = adapters or default_adapters(now_fn)
    results: List[AdapterResult] = []
    notes: List[str] = []
    facts: dict = {}

    def over_budget() -> bool:
        return (time.perf_counter() - t0) > LIMITS["build_s"]

    try:
        if req.sources["internal"]:
            r = ad["internal"].run(req.ticker, req.as_of_date, req.lookback_days)
            results.append(r)
            facts.update(r.facts or {})
            if "ticker bars" in (facts.get("missing") or []):
                return _failed(req, created, "NO_MARKET_DATA", f"no daily bars for {req.ticker} up to {req.as_of_date}", t0, store, persist, results)
        if req.sources["sec"] and not over_budget():
            r = ad["sec"].run(req.ticker, req.related, req.as_of_date, req.lookback_days, req.cik_overrides or None)
            results.append(r)
            if r.facts.get("fundamentals"):
                facts["fundamentals"] = r.facts["fundamentals"]
        if req.sources["ir"] and req.ir_urls and not over_budget():
            results.append(ad["ir"].run(req.ticker, req.ir_urls, req.as_of_date))
        if req.sources["news"] and not over_budget():
            results.append(ad["news"].run(req.ticker, req.as_of_date))
        if req.sources["manual"] and (req.manual_urls or req.manual_texts) and not over_budget():
            results.append(ad["manual"].run(req.ticker, req.manual_urls, req.manual_texts, req.as_of_date))
        if over_budget():
            notes.append(f"build time limit ({LIMITS['build_s']}s) reached; later sources were skipped")
    except Exception as exc:  # noqa: BLE001 - never let an adapter crash a build
        return _failed(req, created, "INTERNAL_ERROR", f"{type(exc).__name__}: {exc}"[:300], t0, store, persist, results)

    # fetch rows (ids now, positions after sorting items is unnecessary: fetch order is the adapter order, which is fixed)
    fetches: List[dict] = []
    per_adapter_ids: Dict[int, List[str]] = {}
    per_adapter_ok: Dict[int, List[bool]] = {}
    for ai, r in enumerate(results):
        ids, oks = [], []
        for fr in r.fetches:
            fid = new_id()
            row = fr.row()
            row.update({"fetch_id": fid, "packet_id": None, "position": len(fetches), "adapter": r.adapter, "error": row.get("error") or ""})
            row.pop("final_url", None)
            fetches.append(row)
            ids.append(fid)
            oks.append(fr.ok)
        per_adapter_ids[ai], per_adapter_ok[ai] = ids, oks
        notes.extend(f"{r.adapter}: {n}" for n in r.notes)

    items: List[dict] = []
    for ai, r in enumerate(results):
        for si in r.items:
            items.append(_item_dict(si, req, per_adapter_ids[ai], per_adapter_ok[ai]))
    items = items[:LIMITS["items"]]
    items.sort(key=lambda i: (i["category"], i["evidence_date"], i["source_url"] or "", i["text"]))
    for pos, i in enumerate(items):
        i["item_id"], i["position"] = new_id(), pos

    start = (req.as_of_date - timedelta(days=req.lookback_days)).isoformat()
    eligible = [i for i in items if start <= i["evidence_date"] <= req.as_of_date.isoformat() and i["epistemic"] != "UNCONFIRMED"]
    R.mark_contradictions(eligible)
    cause = R.cause_attribution(facts, eligible)
    ladder = R.ladder(facts, eligible, req.related)
    by_id = {i["item_id"]: i for i in items}
    for rung in ladder:
        for iid in rung["item_ids"]:
            by_id[iid]["ladder_rung"], by_id[iid]["ladder_status"] = rung["rung"], rung["status"]
    strength = R.strength(ladder)
    missing = R.gaps(ladder, fetches)
    nxt = R.next_events(facts)
    sec_requests = sum(r.requests for r in results if r.adapter == "sec_edgar")
    packet = {"packet_id": new_id(), "packet_hash": _hash_payload(req, items, fetches, cause, ladder, strength), "ticker": req.ticker,
              "as_of_date": req.as_of_date.isoformat(), "created_at": created, "lookback_days": req.lookback_days, "engine_version": ENGINE_VERSION,
              "rules_version": RULES_VERSION, "status": "COMPLETED", "failure_code": None, "failure_detail": None,
              "request_json": {**req.as_dict(), "notes": notes, "facts": _jsonable_facts(facts)}, "cause_json": cause, "ladder_json": ladder,
              "strength": strength["grade"], "strength_points": strength["points"], "strength_text": strength["text"], "missing_json": missing,
              "next_events_json": nxt, "n_items": len(items), "n_fetches": len(fetches), "n_failed_fetches": sum(1 for f in fetches if f["status"] != "OK"),
              "sec_requests": sec_requests, "runtime_s": f"{time.perf_counter() - t0:.3f}", "universe_note": UNIVERSE_NOTE}
    for i in items:
        i["packet_id"] = packet["packet_id"]
    for f in fetches:
        f["packet_id"] = packet["packet_id"]
    if persist:
        (store or EvidenceStore()).insert_packet(packet, items, fetches)
    return {"packet": packet, "items": items, "fetches": fetches}


def _jsonable_facts(facts: dict) -> dict:
    out = {}
    for k, v in facts.items():
        if isinstance(v, (int, float, str, bool)) or v is None:
            out[k] = v
        elif isinstance(v, dict):
            out[k] = {a: (b if isinstance(b, (int, float, str, bool)) or b is None else str(b)) for a, b in v.items()}
        elif isinstance(v, list):
            out[k] = [x if isinstance(x, (int, float, str, bool)) or x is None else str(x) for x in v]
        else:
            out[k] = str(v)
    return out


def _failed(req, created: str, code: str, detail: str, t0: float, store, persist: bool, results: Optional[List[AdapterResult]] = None) -> dict:
    fetches: List[dict] = []
    pid = new_id()
    for r in results or []:
        for fr in r.fetches:
            row = fr.row()
            row.update({"fetch_id": new_id(), "packet_id": pid, "position": len(fetches), "adapter": r.adapter, "error": row.get("error") or ""})
            row.pop("final_url", None)
            fetches.append(row)
    ticker = (getattr(req, "ticker", "") or "").strip().upper() or "UNKNOWN"
    as_of = getattr(req, "as_of_date", None)
    req_json = req.as_dict() if hasattr(req, "as_dict") else {"ticker": ticker}
    body = {"request": req_json, "failure_code": code, "failure_detail": detail, "created_at": created}
    lb = getattr(req, "lookback_days", config.EVIDENCE_LOOKBACK_DAYS)
    packet = {"packet_id": pid, "packet_hash": hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest(),
              "ticker": ticker if _TICKER.match(ticker) else "UNKNOWN", "as_of_date": (as_of or datetime.fromisoformat(created).date()).isoformat(), "created_at": created,
              "lookback_days": lb if LIMITS["lookback_min_days"] <= int(lb) <= LIMITS["lookback_max_days"] else config.EVIDENCE_LOOKBACK_DAYS,
              "engine_version": ENGINE_VERSION, "rules_version": RULES_VERSION, "status": "FAILED", "failure_code": code, "failure_detail": detail,
              "request_json": req_json, "cause_json": {"verdict": "NOT_AVAILABLE", "reason": detail}, "ladder_json": [], "strength": None, "strength_points": 0,
              "strength_text": f"No grade: {code}.", "missing_json": [{"kind": "failure", "text": detail}], "next_events_json": [], "n_items": 0,
              "n_fetches": len(fetches), "n_failed_fetches": sum(1 for f in fetches if f["status"] != "OK"),
              "sec_requests": sum(r.requests for r in (results or []) if r.adapter == "sec_edgar"), "runtime_s": f"{time.perf_counter() - t0:.3f}",
              "universe_note": UNIVERSE_NOTE}
    if persist:
        (store or EvidenceStore()).insert_packet(packet, [], fetches)
    return {"packet": packet, "items": [], "fetches": fetches}
