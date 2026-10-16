"""
api/routes/evidence.py — Stage 5.4 MULTI-SOURCE EVIDENCE RESEARCH (evidence/). RESEARCH ONLY.

GET  /api/evidence/config                 conventions, rules version, declared related tickers / IR URLs, deny hosts, network flag (0 requests)
POST /api/evidence/packets                {ticker, as_of_date?, lookback_days?, related?, ir_urls?, manual_urls?, manual_texts?, sources?, cik_overrides?}:
                                          ONE bounded packet build — SEC / IR / manual-URL reads through evidence.net only, existing Alpaca
                                          data, 0 broker / Robinhood / model requests; persisted append-only
GET  /api/evidence/packets · /packets/{id} · /packets/{id}/items · /packets/{id}/fetches · /packets/{id}/explanations
POST /api/evidence/packets/{id}/explain   dry run (0 requests) unless {"confirm": true}: then exactly ONE model request over the stored packet

Nothing here previews, prepares or sends an order, sizes a position, calls Robinhood, or carries a draft for any form; no broker
import; no path contains "order", "trade", "backtest", "paper", "execute", "broker", "handoff", "deploy" or "activate". Strict bodies.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Dict, List, Optional

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

import config
from evidence import CATEGORIES, CONVENTIONS, ENGINE_VERSION, EPISTEMIC, LIMITS, PROMPT_VERSION, ROLES, RULES_VERSION, RUNGS, RUNG_STATUSES, STRENGTHS
from evidence import engine as EN
from evidence import explain as EX
from evidence.store import EvidenceStore
from fit import readonly as RO

router = APIRouter(prefix="/api/evidence", tags=["evidence"])
LABEL = "EVIDENCE RESEARCH — RESEARCH ONLY — EVIDENCE STRENGTH IS NOT A PRICE PROBABILITY"
_ID = r"^[0-9a-f]{32}$"
NOW_FN = None
BUILD: dict = {"adapters": None}            # tests inject fake adapters here
EXPLAIN: dict = {"provider_factory": None}  # tests inject a fake provider here


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ManualUrl(Strict):
    url: str = Field(min_length=12, max_length=1024)
    role: str = Field(pattern="^(" + "|".join(ROLES) + ")$")
    related_ticker: Optional[str] = Field(default=None, pattern=r"^[A-Za-z][A-Za-z.]{0,9}$")
    note: str = Field(default="", max_length=300)


class ManualText(Strict):
    text: str = Field(min_length=1, max_length=4000)
    role: str = Field(pattern="^(" + "|".join(ROLES) + ")$")
    related_ticker: Optional[str] = Field(default=None, pattern=r"^[A-Za-z][A-Za-z.]{0,9}$")
    note: str = Field(default="", max_length=300)


class BuildBody(Strict):
    ticker: str = Field(pattern=r"^[A-Za-z][A-Za-z.]{0,9}$")
    as_of_date: Optional[date] = None
    lookback_days: int = Field(default=config.EVIDENCE_LOOKBACK_DAYS, ge=LIMITS["lookback_min_days"], le=LIMITS["lookback_max_days"])
    related: Optional[Dict[str, List[str]]] = None
    ir_urls: Optional[List[str]] = Field(default=None, max_length=LIMITS["ir_urls"])
    manual_urls: List[ManualUrl] = Field(default_factory=list, max_length=LIMITS["manual_urls"])
    manual_texts: List[ManualText] = Field(default_factory=list, max_length=LIMITS["manual_urls"])
    sources: Optional[Dict[str, bool]] = None
    cik_overrides: Optional[Dict[str, str]] = None


class ExplainBody(Strict):
    confirm: bool = False


def _err(code: str, message: str, status: int = 422) -> JSONResponse:
    return JSONResponse({"status": code, "message": message}, status_code=status)


def _now() -> datetime:
    return (NOW_FN or (lambda: datetime.now(timezone.utc)))()


def _store() -> EvidenceStore:
    return EvidenceStore(RO.db_path())


@router.get("/config")
def get_config():
    return {"label": LABEL, "engine_version": ENGINE_VERSION, "rules_version": RULES_VERSION, "prompt_version": PROMPT_VERSION, "conventions": CONVENTIONS,
            "categories": CATEGORIES, "epistemic": EPISTEMIC, "rung_statuses": RUNG_STATUSES, "strengths": STRENGTHS, "rungs": [{"rung": n, "name": nm} for n, nm in RUNGS],
            "roles": ROLES, "limits": LIMITS, "network_enabled": bool(config.EVIDENCE_NETWORK_ENABLED), "sec_user_agent_configured": bool(config.SEC_USER_AGENT),
            "deny_hosts": list(config.EVIDENCE_DENY_HOSTS), "sec_hosts": ["www.sec.gov", "data.sec.gov"], "related": config.EVIDENCE_RELATED,
            "ir_urls": config.EVIDENCE_IR_URLS, "ir_domains": config.EVIDENCE_IR_DOMAINS, "model_configured": bool(config.has_llm_credentials()),
            "model": config.ANTHROPIC_MODEL if config.has_llm_credentials() else None, "requests_made": 0}


@router.post("/packets")
def build(body: BuildBody):
    req = EN.EvidenceRequest(ticker=body.ticker, as_of_date=body.as_of_date, lookback_days=body.lookback_days, related=body.related, ir_urls=body.ir_urls,
                             manual_urls=[m.model_dump() for m in body.manual_urls], manual_texts=[m.model_dump() for m in body.manual_texts],
                             sources=body.sources or {"internal": True, "sec": True, "ir": True, "news": True, "manual": True}, cik_overrides=body.cik_overrides or {})
    try:
        req.normalised(_now().date())
    except EN.EvidenceError as exc:
        return _err("INVALID_REQUEST", str(exc))
    out = EN.build_packet(req, adapters=BUILD["adapters"], now_fn=_now, store=_store())
    p = out["packet"]
    hosts = sorted({f["host"] for f in out["fetches"] if f["host"] not in ("alpaca",)})
    return JSONResponse({"status": p["status"], "packet_id": p["packet_id"], "packet_hash": p["packet_hash"], "failure_code": p["failure_code"],
                         "failure_detail": p["failure_detail"], "strength": p["strength"], "cause": p["cause_json"].get("verdict"), "n_items": p["n_items"],
                         "n_fetches": p["n_fetches"], "n_failed_fetches": p["n_failed_fetches"], "sec_requests": p["sec_requests"], "hosts_contacted": hosts,
                         "model_requests": 0, "label": LABEL}, status_code=201 if p["status"] == "COMPLETED" else 200)


@router.get("/packets")
def packets(limit: int = 50, ticker: Optional[str] = None):
    rows = _store().packets(limit=max(1, min(int(limit), 200)), ticker=ticker)
    return {"packets": [{k: v for k, v in r.items() if k not in ("request", "ladder", "missing", "next_events")} for r in rows], "label": LABEL}


@router.get("/packets/{packet_id}")
def packet(packet_id: str):
    if not __import__("re").match(_ID, packet_id):
        return _err("INVALID_ID", "packet id must be 32 hex characters")
    s = _store()
    p = s.packet(packet_id)
    if p is None:
        return _err("NOT_FOUND", "unknown packet", 404)
    return {"packet": p, "items": s.items(packet_id), "fetches": s.fetches(packet_id), "explanations": s.explanations(packet_id), "label": LABEL}


@router.get("/packets/{packet_id}/items")
def items(packet_id: str):
    return {"items": _store().items(packet_id)}


@router.get("/packets/{packet_id}/fetches")
def fetches(packet_id: str):
    return {"fetches": _store().fetches(packet_id)}


@router.get("/packets/{packet_id}/explanations")
def explanations(packet_id: str):
    return {"explanations": _store().explanations(packet_id)}


@router.post("/packets/{packet_id}/explain")
def explain(packet_id: str, body: ExplainBody):
    s = _store()
    if s.packet(packet_id) is None:
        return _err("NOT_FOUND", "unknown packet", 404)
    try:
        res = EX.explain(packet_id, confirm=body.confirm, store=s, provider_factory=EXPLAIN["provider_factory"], now_fn=_now)
    except KeyError:
        return _err("NOT_FOUND", "unknown packet", 404)
    res = dict(res)
    res["label"] = LABEL
    res["requests_made"] = res.get("requests", 0) if body.confirm else 0
    return res
