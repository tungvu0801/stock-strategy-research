"""
execution/exposure_gate.py - Stage 6.9.1 UNIFIED EXTERNAL EXPOSURE GATE (DESIGN_691_UNIFIED_EXPOSURE_GATE.md).

ONE deterministic entry control, UNIFIED_EXTERNAL_EXPOSURE, for NEW Alpaca PAPER entries only. execution.engine.entry_controls appends it when
UNIFIED_EXTERNAL_EXPOSURE_GATE_ENABLED=true, and blocking() / the entry decision treat it like every other control: there is no second decision
path. Its inputs are the Stage 6.9 UnifiedPortfolioState as a plain dict (read once per Paper Auto scan by the executor, through its stream) and
the planned entry the other controls already use (the entry quantity x the proposal's reference price). The other accounts of the unified
portfolio (Robinhood, read only) are a risk INPUT only: the result is ALLOW (PASS / WARN) or BLOCK for the proposed PAPER entry - never an order,
an exit, a resize or any other action on any account, and never a smaller quantity.

Pure: no I/O, no clock, no broker, no model, no write; nothing from unified_portfolio, portfolio or rh_gateway is imported, and no account is
named in code (an account's name comes from the snapshot's own source label). Money and quantities are Decimal; fractional shares are kept.

  A duplicate symbol   an account that is not the execution broker holds the symbol: quantity != 0, ownership USER_MANAGED / EXTERNAL /
                       NEEDS_REVIEW (a non-paper row labelled BOT_MANAGED is treated as external, never as the bot's) -> BLOCK
                       EXTERNAL_SYMBOL_ALREADY_HELD. Alpaca PAPER holdings stay the job of the unchanged BROKER_POSITION control.
  B concentration      projected = (known symbol value + planned notional) / (known invested value + planned notional) x 100: gross (absolute
                       market values) over every KNOWN account's positions, each position counted once. An exposure share, never an equity ratio
                       (PAPER and REAL equity are never added). BLOCK SYMBOL_CONCENTRATION_ABOVE_LIMIT when projected > the limit (strictly).
  C sector             not evaluated: the curated sector map leaves names UNCLASSIFIED, so nothing blocks on a sector.
  D data state         COMPLETE: A and B. STALE_SOURCE: A and B on the last known values, marked STALE. PARTIAL / UNAVAILABLE: a missing source
                       is never zero holdings - A blocks on any KNOWN holding, B is not evaluated; UNIFIED_STALE_SOURCE_POLICY decides the rest:
                         BLOCK_DUPLICATE_ONLY_IF_KNOWN (default)  allow with a WARN that names what is unknown;
                         BLOCK_IF_UNKNOWN                         block every new entry until the data is known (fail closed, explicit opt-in).
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Tuple

from execution.models import ControlEvaluation

CONTROL_ID = "UNIFIED_EXTERNAL_EXPOSURE"
VERSION, PREVIOUS_VERSION, STAGE = "entry_controls_v2", "entry_controls_v1", "6.9.1"
ENTRY_CONTROLS_V1 = ("EXECUTION_MODE", "PAPER_ENVIRONMENT", "SAFETY_GATE", "PROPOSAL_VALIDITY", "SESSION_POLICY", "BROKER_TRADABILITY", "DUPLICATE_ENTRY",
                     "BROKER_POSITION", "BROKER_ACCOUNT", "MAX_OPEN_POSITIONS", "BROKER_RECONCILIATION")   # execution.engine.entry_controls before Stage 6.9.1, in order
ENTRY_CONTROLS_V2 = ENTRY_CONTROLS_V1 + (CONTROL_ID,)
POLICIES = ("BLOCK_DUPLICATE_ONLY_IF_KNOWN", "BLOCK_IF_UNKNOWN")
COMPARISON = "BLOCK if projected_symbol_exposure_pct > max_symbol_exposure_pct (strictly greater; equal is allowed)"
DENOMINATOR = ("projected known invested value = the gross market value of every position in every KNOWN account (PAPER and REAL, each position once) "
               "+ the planned notional; never account equity")

HELD, CONCENTRATION, VALUE_UNREADABLE = "EXTERNAL_SYMBOL_ALREADY_HELD", "SYMBOL_CONCENTRATION_ABOVE_LIMIT", "EXTERNAL_VALUE_UNREADABLE"
NOTIONAL_INVALID, CONFIG_INVALID = "PLANNED_NOTIONAL_INVALID", "GATE_CONFIG_INVALID"
UNKNOWN, STALE_OK, WITHIN = "EXTERNAL_DATA_UNKNOWN", "ALLOWED_ON_STALE_DATA", "WITHIN_LIMITS"
NO_PLANNED_ENTRY = "NO_PLANNED_ENTRY"                                                # the display pre-check only (planned_required=False)
REASON_CODES = (HELD, CONCENTRATION, VALUE_UNREADABLE, NOTIONAL_INVALID, CONFIG_INVALID, UNKNOWN, STALE_OK, WITHIN, NO_PLANNED_ENTRY)

_STATES = ("COMPLETE", "STALE_SOURCE", "PARTIAL", "UNAVAILABLE")
_KNOWN = ("LOADED", "STALE")
_OWN = {"USER_MANAGED": "user managed", "EXTERNAL": "external", "NEEDS_REVIEW": "needs review", "BOT_MANAGED": "labelled bot managed, treated as external"}
_Q = Decimal("0.01")


@dataclass(frozen=True)
class GateSettings:
    enabled: bool = False
    block_duplicate_symbol: bool = True
    max_symbol_exposure_pct: str = "20"                           # Decimal text: no binary float ever reaches the comparison
    stale_source_policy: str = POLICIES[0]

    def limit(self) -> Optional[Decimal]:
        d = _dec(self.max_symbol_exposure_pct)
        return d if d is not None and Decimal(0) < d <= Decimal(100) else None

    def problems(self) -> List[str]:
        out = [] if self.limit() is not None else [f"UNIFIED_MAX_SYMBOL_EXPOSURE_PCT={self.max_symbol_exposure_pct!r} is not a number in (0, 100]"]
        return out + ([] if self.stale_source_policy in POLICIES else [f"UNIFIED_STALE_SOURCE_POLICY={self.stale_source_policy!r} is not one of {', '.join(POLICIES)}"])


def from_config(cfg: Any) -> GateSettings:
    """The configured settings (the caller passes the config module; read at every scan, so nothing is cached)."""
    return GateSettings(enabled=bool(getattr(cfg, "UNIFIED_EXTERNAL_EXPOSURE_GATE_ENABLED", False)),
                        block_duplicate_symbol=bool(getattr(cfg, "UNIFIED_BLOCK_DUPLICATE_SYMBOL", True)),
                        max_symbol_exposure_pct=str(getattr(cfg, "UNIFIED_MAX_SYMBOL_EXPOSURE_PCT", "20")).strip(),
                        stale_source_policy=str(getattr(cfg, "UNIFIED_STALE_SOURCE_POLICY", POLICIES[0]) or "").strip().upper())


# ---------------------------------------------------------------- the versioned contract (forward-validation metadata) ----------------------
def contract(s: GateSettings) -> Dict[str, Any]:
    """The entry-controls contract a PAPER entry is decided under. The Stage 6.1 frozen strategy controls (forward_validation.frozen_controls) are a
    separate, unchanged record; this one says which ENTRY controls ran and, for the gate, exactly how."""
    return {"version": VERSION, "previous_version": PREVIOUS_VERSION, "stage": STAGE, "entry_controls": list(ENTRY_CONTROLS_V2 if s.enabled else ENTRY_CONTROLS_V1),
            "added_control": CONTROL_ID, "unified_external_exposure": {
                "enabled": s.enabled, "block_duplicate_symbol": s.block_duplicate_symbol, "max_symbol_exposure_pct": s.max_symbol_exposure_pct, "comparison": COMPARISON,
                "denominator": DENOMINATOR, "stale_source_policy": s.stale_source_policy,
                "duplicate_scope": "accounts other than the execution broker; ownership USER_MANAGED / EXTERNAL / NEEDS_REVIEW; quantity != 0",
                "sector_rule": "not evaluated (no reliable sector source)", "decision": "ALLOW or BLOCK only; never a resize"}}


def contract_hash(s: GateSettings) -> str:
    return hashlib.sha256(json.dumps(contract(s), sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def summary(s: GateSettings) -> Dict[str, Any]:
    """The gate as the APIs show it (no account data)."""
    return {"control_id": CONTROL_ID, "version": VERSION, "previous_version": PREVIOUS_VERSION, "stage": STAGE, "enabled": s.enabled, "enforced": s.enabled,
            "contract_hash": contract_hash(s), "entry_controls": list(ENTRY_CONTROLS_V2 if s.enabled else ENTRY_CONTROLS_V1), "settings": {
                "block_duplicate_symbol": s.block_duplicate_symbol, "max_symbol_exposure_pct": s.max_symbol_exposure_pct, "stale_source_policy": s.stale_source_policy,
                "comparison": COMPARISON}, "problems": s.problems(),
            "note": ("Enforced: the executor blocks a new Alpaca PAPER entry when the gate says BLOCK." if s.enabled else
                     "Off: advisory only - the executor's entry controls are the entry_controls_v1 set; nothing is blocked by this gate.")}


def intent_record(s: GateSettings, ev: Optional[ControlEvaluation]) -> Dict[str, Any]:
    """What an entry intent records about the entry controls that allowed it (Stage 6.9.1 onward; an older intent has no such record)."""
    i = (ev.input if ev is not None else None) or {}
    return {"version": VERSION, "contract_hash": contract_hash(s), "unified_external_exposure": {
        "enabled": s.enabled, "enforced": s.enabled, "result": ev.result if ev is not None else "NOT_EVALUATED", "reason_code": i.get("reason_code"),
        "unified_state": i.get("unified_state"), "snapshot_at": i.get("snapshot_at"), "projected_pct": (i.get("concentration") or {}).get("projected_pct"),
        "threshold_pct": s.max_symbol_exposure_pct, "policy": s.stale_source_policy}}


# ---------------------------------------------------------------- numbers ---------------------------------------------------------------------
def _dec(v) -> Optional[Decimal]:
    if v is None or isinstance(v, bool):
        return None
    try:
        d = Decimal(str(v).strip())
    except (InvalidOperation, ValueError):
        return None
    return d if d.is_finite() else None


def _qty(d: Optional[Decimal]) -> Optional[str]:
    return None if d is None else "0" if d == 0 else format(d.normalize(), "f")


def _money(d: Optional[Decimal]) -> Optional[str]:
    return None if d is None else format(d.quantize(_Q), "f")


def _usd(d: Optional[Decimal]) -> str:
    return "value unknown" if d is None else f"${d.quantize(_Q):,}"


def _pct(d: Optional[Decimal]) -> Optional[str]:
    return None if d is None else format(d.quantize(_Q), "f")


# ---------------------------------------------------------------- the snapshot ----------------------------------------------------------------
def _sources(u: dict) -> List[Dict[str, Any]]:
    return [{"source": str(s.get("source") or ""), "account_type": str(s.get("account_type") or ""), "state": str(s.get("state") or ""),
             "refreshed_at": s.get("refreshed_at"), "name": str(s.get("label") or "").split("·")[0].strip() or str(s.get("source") or "external account").replace("_", " ").title()}
            for s in (u.get("sources") or []) if isinstance(s, dict)]


def _positions(u: dict, sym: str) -> Tuple[List[Dict[str, Any]], int, List[str]]:
    """One entry per (source, symbol); a row with quantity 0 is no position. A position repeated with identical values is counted once; repeated
    with conflicting values it is resolved conservatively (the candidate symbol at its largest value, any other symbol at its smallest - both
    raise the projected share). A row without a readable symbol is counted as unreadable, never dropped silently."""
    groups: Dict[Tuple[str, str], List[dict]] = {}
    unreadable, notes = 0, []
    for r in u.get("rows") or []:
        if not isinstance(r, dict) or not str(r.get("symbol") or "").strip():
            unreadable += 1 if not isinstance(r, dict) or r.get("account_type") != "PAPER" else 0
            continue
        q = _dec(r.get("quantity"))
        if q is not None and q == 0:
            continue
        mv = _dec(r.get("market_value"))
        row = {"symbol": str(r["symbol"]).strip().upper(), "source": str(r.get("source") or ""), "account_type": str(r.get("account_type") or ""),
               "ownership": str(r.get("ownership") or ""), "qty": q, "value": abs(mv) if mv is not None else None}
        groups.setdefault((row["source"], row["symbol"]), []).append(row)
    out = []
    for (src, s), rows in sorted(groups.items()):
        if len(rows) == 1 or len({(r["qty"], r["value"]) for r in rows}) == 1:
            if len(rows) > 1:
                notes.append(f"{s}: {len(rows)} identical {src} rows counted once")
            out.append(rows[0])
            continue
        vals = [r["value"] for r in rows]
        pick = None if None in vals else (max(vals) if s == sym else min(vals))
        qs = [r["qty"] for r in rows if r["qty"] is not None]
        out.append({**rows[0], "value": pick, "qty": max(qs, key=abs) if qs else None})
        notes.append(f"{s}: {len(rows)} conflicting {src} rows resolved conservatively")
    return out, unreadable, notes


# ---------------------------------------------------------------- the control --------------------------------------------------------------------
def evaluate(symbol: str, qty: Optional[int], reference_price: Optional[Decimal], unified: Optional[dict], s: GateSettings, *,
             planned_required: bool = True) -> ControlEvaluation:
    """UNIFIED_EXTERNAL_EXPOSURE for a proposed PAPER entry of `qty` shares at `reference_price` (the planned notional). PASS / WARN allow,
    BLOCK blocks. `planned_required=False` (the display pre-check only) leaves B unevaluated instead of blocking when no valid entry is planned."""
    sym = str(symbol or "").strip().upper()
    base: Dict[str, Any] = {"symbol": sym, "control_version": VERSION, "enabled": s.enabled, "block_duplicate_symbol": s.block_duplicate_symbol,
                            "threshold_pct": s.max_symbol_exposure_pct, "comparison": COMPARISON, "policy": s.stale_source_policy}
    if s.problems():
        return _out("BLOCK", CONFIG_INVALID, f"Unified exposure gate configuration invalid ({'; '.join(s.problems())}): no entry until it is fixed.", base)
    limit = s.limit()
    u = unified if isinstance(unified, dict) else {}
    state = u.get("state") if u.get("state") in _STATES else "UNAVAILABLE"
    srcs = _sources(u)
    ext_src = [x for x in srcs if x["account_type"] != "PAPER"]
    paper_src = [x for x in srcs if x["account_type"] == "PAPER"]
    ext_known = state != "UNAVAILABLE" and bool(ext_src) and all(x["state"] in _KNOWN for x in ext_src)
    all_known = state in ("COMPLETE", "STALE_SOURCE") and ext_known and bool(paper_src) and all(x["state"] in _KNOWN for x in paper_src)
    stale = [x for x in srcs if x["state"] == "STALE"]
    names = {x["source"]: x["name"] for x in srcs}
    ext_name = ext_src[0]["name"] if ext_src else "external account"
    positions, unreadable, notes = _positions(u, sym) if state != "UNAVAILABLE" else ([], 0, [])
    mine = [p for p in positions if p["symbol"] == sym]
    held = [p for p in mine if p["account_type"] != "PAPER"]
    planned = None if not qty or int(qty) < 1 or reference_price is None or reference_price <= 0 else Decimal(int(qty)) * reference_price
    base.update(unified_state=state, error=u.get("error"), sources=[{k: x[k] for k in ("source", "account_type", "state", "refreshed_at")} for x in srcs],
                snapshot_at=(ext_src[0]["refreshed_at"] if ext_src else None), stale=bool(stale), notes=notes, unreadable_external_rows=unreadable,
                planned_qty=int(qty) if qty else 0, reference_price=_money(reference_price), planned_notional=_money(planned),
                holdings=[{"source": p["source"], "account_type": p["account_type"], "ownership": p["ownership"], "quantity": _qty(p["qty"]), "market_value": _money(p["value"])} for p in mine],
                external_holdings=[{"source": p["source"], "account_type": p["account_type"], "ownership": p["ownership"], "quantity": _qty(p["qty"]), "market_value": _money(p["value"])} for p in held])
    stale_note = f" Based on STALE data read at {stale[0]['refreshed_at']}." if stale else ""

    # A - duplicate symbol: a KNOWN holding blocks whatever the data state; "no holding" needs the external accounts known and readable
    if not s.block_duplicate_symbol:
        dup = {"evaluated": False, "result": "OFF", "text": "duplicate-symbol rule off (UNIFIED_BLOCK_DUPLICATE_SYMBOL=false)"}
    elif held:
        h = held[0]
        kind = "real" if h["account_type"] == "REAL" else "SYNTHETIC fixture (not a real account)" if h["account_type"] == "SYNTHETIC" else h["account_type"].lower() or "external"
        own = "user-managed" if h["ownership"] == "USER_MANAGED" else _OWN.get(h["ownership"], h["ownership"].lower() or "unclassified")
        parts = "; ".join(f"{_qty(p['qty']) or 'an unknown number of'} {sym} shares already held in {names.get(p['source'], p['source'])} {p['account_type']} "
                          f"({_OWN.get(p['ownership'], p['ownership'].lower() or 'unclassified')}), {_usd(p['value'])}" for p in held)
        dup = {"evaluated": True, "result": "BLOCK", "text": f"{names.get(h['source'], h['source'])} already holds {sym} as a {own} {kind} position. Blocked: {parts}."}
    elif ext_known and not unreadable:
        dup = {"evaluated": True, "result": "PASS", "text": f"no {ext_name} holding in {sym}"}
    else:
        if ext_known:
            why = f"{unreadable} {ext_name} row(s) unreadable"
        elif state != "UNAVAILABLE":
            why = ", ".join(x["state"] if len(ext_src) == 1 else x["name"] + " " + x["state"] for x in ext_src) or "no external account in the snapshot"
        else:
            why = "unified portfolio unavailable" + (" (" + str(u.get("error")) + ")" if u.get("error") else "")
        dup = {"evaluated": False, "result": "UNKNOWN", "text": f"{ext_name} holdings unknown ({why}), not zero: cannot confirm {sym} is not held"}

    # B - concentration of the symbol over the known invested value, planned notional included (gross; KNOWN accounts only)
    conc: Dict[str, Any] = {"evaluated": False, "projected_pct": None, "threshold_pct": s.max_symbol_exposure_pct, "basis": "STALE" if stale else state}
    if planned is None:
        conc.update(result="BLOCK" if planned_required else "NOT_EVALUATED",
                    text="no valid planned entry (quantity x reference price): concentration cannot be evaluated" + ("" if planned_required else " until a reviewable proposal exists"))
    elif not all_known:
        missing = [x["name"] + " " + x["state"] for x in srcs if x["state"] not in _KNOWN] or ["unified state " + state]
        conc.update(result="UNKNOWN", text=f"concentration not evaluated: {', '.join(missing)} - unknown, not zero")
    elif any(p["value"] is None for p in mine):
        conc.update(result="BLOCK", text=f"a known {sym} position has no readable market value: its concentration cannot be bounded")
    else:
        sv = sum((p["value"] for p in mine), Decimal(0))
        total = sum((p["value"] for p in positions if p["value"] is not None), Decimal(0))
        projected = (sv + planned) / (total + planned) * 100
        unvalued = sum(1 for p in positions if p["value"] is None)
        conc.update(evaluated=True, symbol_value=_money(sv), invested_value=_money(total), planned_notional=_money(planned), projected_invested_value=_money(total + planned),
                    projected_pct=_pct(projected), projected_pct_exact=str(projected), unvalued_positions_excluded=unvalued,
                    result="BLOCK" if projected > limit else "PASS",
                    text=(f"projected {sym} exposure {_pct(projected)}% of the known invested value "
                          f"({_usd(sv)} known + {_usd(planned)} planned) / ({_usd(total)} known + {_usd(planned)} planned) "
                          f"{'is above' if projected > limit else 'is within'} the {s.max_symbol_exposure_pct}% limit"
                          + (f"; {unvalued} unvalued position(s) excluded (the known total is a lower bound)" if unvalued else "")))
    base.update(rules={"duplicate_symbol": dup, "concentration": conc, "sector": {"evaluated": False, "result": "NOT_EVALUATED", "text": "no reliable sector source; never blocks"}})
    base["concentration"] = conc

    if dup["result"] == "BLOCK":
        return _out("BLOCK", HELD, dup["text"] + (f" Also {conc['text']}." if conc.get("result") == "BLOCK" and conc.get("evaluated") else "")
                    + f" No PAPER entry; the {ext_name} holding is not touched." + stale_note, base)
    if conc.get("result") == "BLOCK":
        code = CONCENTRATION if conc.get("evaluated") else NOTIONAL_INVALID if planned is None else VALUE_UNREADABLE
        return _out("BLOCK", code, f"{_cap(conc['text'])}. No trade and no resize." + stale_note, base)
    unknown = [r["text"] for r in (dup, conc) if r.get("result") == "UNKNOWN"]
    if unknown:
        if s.stale_source_policy == "BLOCK_IF_UNKNOWN":
            return _out("BLOCK", UNKNOWN, f"{_cap('; '.join(unknown))}. Blocked by policy BLOCK_IF_UNKNOWN until the data is known." + stale_note, base)
        done = [r["text"] for r in (dup, conc) if r.get("result") == "PASS"]
        return _out("WARN", UNKNOWN, f"{_cap('; '.join(unknown + done))}. Allowed by policy BLOCK_DUPLICATE_ONLY_IF_KNOWN (only a known holding blocks)." + stale_note, base)
    text = _cap("; ".join(r["text"] for r in (dup, conc) if r.get("result") in ("PASS", "OFF", "NOT_EVALUATED")))
    if conc.get("result") == "NOT_EVALUATED":                                      # the display pre-check without a planned entry: never reached by the executor
        return _out("WARN", NO_PLANNED_ENTRY, f"{text}.{stale_note}", base)
    if stale:
        return _out("WARN", STALE_OK, f"{text}.{stale_note} Allowed on the last known values.", base)
    return _out("PASS", WITHIN, f"{text}.", base)


def _cap(t: str) -> str:
    return t[:1].upper() + t[1:]


def _out(result: str, code: str, reason: str, inp: Dict[str, Any]) -> ControlEvaluation:
    return ControlEvaluation(control_id=CONTROL_ID, result=result, reason=reason, input={**inp, "result": result, "reason_code": code})


def record(ev: ControlEvaluation) -> Dict[str, Any]:
    """The per-cycle record of a gate evaluation (the persisted run summary; {} for any other control): symbol, reason code, the known external
    holding with its source, ownership and market value, the projected concentration and threshold, the unified state and snapshot time.
    No account label or number."""
    if ev.control_id != CONTROL_ID:
        return {}
    i = ev.input or {}
    c = i.get("concentration") or {}
    keys = ("symbol", "reason_code", "unified_state", "snapshot_at", "sources", "external_holdings", "threshold_pct", "comparison", "policy", "enabled", "stale", "planned_notional")
    return {"code": i.get("reason_code"), "result": ev.result, "detail": {**{k: i.get(k) for k in keys}, "projected_pct": c.get("projected_pct"),
                                                                         "concentration_evaluated": bool(c.get("evaluated")), "symbol_value": c.get("symbol_value"),
                                                                         "invested_value": c.get("invested_value")}}


def precheck_view(ev: ControlEvaluation, s: GateSettings, proposal_status: Optional[str] = None) -> Dict[str, Any]:
    """The display pre-check of one symbol (GET /api/unified-portfolio): what the control decides now, and whether the executor enforces it."""
    i = ev.input or {}
    return {"control_id": CONTROL_ID, "symbol": i.get("symbol"), "result": ev.result, "reason_code": i.get("reason_code"), "text": ev.reason, "enforced": s.enabled,
            "would_block": ev.result == "BLOCK", "proposal_status": proposal_status, "unified_state": i.get("unified_state"), "snapshot_at": i.get("snapshot_at"),
            "projected_pct": (i.get("concentration") or {}).get("projected_pct"), "threshold_pct": s.max_symbol_exposure_pct, "external_holdings": i.get("external_holdings") or [],
            "label": ("BLOCKED" if s.enabled else "WOULD BLOCK · advisory") if ev.result == "BLOCK" else _LABELS.get(i.get("reason_code"), "ALLOWED")
                     + ("" if s.enabled else " · advisory")}


_LABELS = {UNKNOWN: "ALLOWED · data unknown", STALE_OK: "ALLOWED · stale data", NO_PLANNED_ENTRY: "DUPLICATE CHECK ONLY · no planned entry", WITHIN: "ALLOWED"}
