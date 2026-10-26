"""
unified_portfolio/state.py - UnifiedPortfolioState: one deterministic read model over every account (pure; no I/O, no broker, no write).

  rows        one row per account position, never merged: Alpaca PAPER (BOT_MANAGED / EXTERNAL / NEEDS_REVIEW, from the Stage 6.8 classification)
              and Robinhood REAL (USER_MANAGED, always). The same symbol in two accounts stays two rows.
  exposure    computed over the KNOWN sources only (LOADED or STALE): by symbol (quantity and market value summed across accounts, each account
              kept), by source, by ownership, PAPER vs REAL, by sector (curated map only; unclassified value reported, never guessed), largest,
              top-5 concentration. A source without data is listed as missing - never counted as zero.
  equity      PAPER (Alpaca) and REAL (Robinhood) kept apart and never summed: different accounts, different money.
  state       COMPLETE (every source loaded and fresh) | STALE_SOURCE (a source is stale; its values are included and disclosed) |
              PARTIAL (a source has no data; exposure covers the others, disclosed) | UNAVAILABLE (no source has data)
  warnings    informational only (stale / missing source, duplicate symbol, concentration, external exposure in a READY ticker, unvalued rows)
  policy      BLOCK_NEW_BOT_ENTRY_IF_EXTERNAL_EXPOSURE, ADVISORY in Stage 6.9: computed and shown, never enforced (the executor is unchanged)
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Dict, Iterable, List, Optional

import config
from unified_portfolio import (ACCOUNT_TYPE, AUTH_REQUIRED, BOT_MANAGED, COMPLETE, CURRENT_ENTRY_POLICY, DUPLICATE_ENTRY_POLICY, ERROR, EXTERNAL, LOADED,
                               SYNTHETIC,
                               NEEDS_REVIEW, NOT_CONFIGURED, NOT_LOADED, OWNERSHIP_LABELS, PAPER, PARTIAL, REAL, SOURCE_LABELS, STALE, STALE_SOURCE,
                               UNAVAILABLE, USER_MANAGED, VERSION)

_ALPACA_OWNERSHIP = {"BOT": BOT_MANAGED, "BOT_ENTRY": BOT_MANAGED, "EXTERNAL": EXTERNAL, "NEEDS_REVIEW": NEEDS_REVIEW}
_Q = Decimal("0.01")


def _d(v) -> Optional[Decimal]:
    try:
        return Decimal(str(v)) if v is not None and str(v) != "" else None
    except (InvalidOperation, ValueError):
        return None


def _money(v: Optional[Decimal]) -> Optional[str]:
    return None if v is None else format(v.quantize(_Q), "f")


def _qty(v: Optional[Decimal]) -> Optional[str]:
    return None if v is None else format(v.normalize(), "f") if v != 0 else "0"


def _pct(n: Optional[Decimal], d: Optional[Decimal]) -> Optional[str]:
    return None if n is None or d is None or d == 0 else format((n / d * 100).quantize(_Q), "f")


def alpaca_source(av: Optional[dict]) -> Dict[str, Any]:
    """The Alpaca PAPER account (execution.account_positions.view) as a source: LOADED only when its account data is fresh."""
    av = av or {"state": "DISABLED"}
    src = av.get("source") or "ALPACA_PAPER"
    fr = av.get("freshness") or {}
    if av.get("state") == "DISABLED":
        state, msg = NOT_CONFIGURED, av.get("message") or "No Alpaca PAPER broker is configured."
    elif av.get("state") != "LOADED":
        state, msg = NOT_LOADED, av.get("message") or "Alpaca PAPER positions not loaded yet."
    else:
        state = LOADED if fr.get("status") == "FRESH" else STALE
        msg = av.get("message") if state == LOADED else f"{fr.get('label') or 'Account data not fresh'}: values from {av.get('refreshed_at')}."
    rows = None
    if av.get("positions") is not None:
        rows = [{"source": src, "account_type": ACCOUNT_TYPE.get(src, PAPER), "account_label": SOURCE_LABELS.get(src, src), "symbol": p["symbol"],
                 "instrument_id": None, "quantity": p.get("qty"), "average_entry_price": p.get("avg_entry_price"), "current_price": p.get("current_price"),
                 "market_value": p.get("market_value"), "unrealized_pl": p.get("unrealized_pl"),
                 "ownership": _ALPACA_OWNERSHIP.get(p.get("management"), NEEDS_REVIEW), "managed": bool(p.get("managed")),
                 "ownership_label": OWNERSHIP_LABELS[_ALPACA_OWNERSHIP.get(p.get("management"), NEEDS_REVIEW)] + (" (entry in progress)" if p.get("management") == "BOT_ENTRY" else ""),
                 "refreshed_at": av.get("refreshed_at"), "freshness": state, "notes": p.get("management_text")} for p in av["positions"]]
    return {"source": src, "account_type": ACCOUNT_TYPE.get(src, PAPER), "label": SOURCE_LABELS.get(src, src), "state": state, "message": msg,
            "refreshed_at": av.get("refreshed_at"), "age_s": fr.get("age_seconds"), "positions": rows}


def robinhood_source(rv: Optional[dict]) -> Dict[str, Any]:
    """The Robinhood source (RobinhoodSource.view) with its rows normalized: USER_MANAGED, REAL, never managed."""
    rv = rv or {"state": NOT_CONFIGURED, "source": "ROBINHOOD", "positions": None}
    src = rv.get("source") or "ROBINHOOD"
    kind = ACCOUNT_TYPE.get(src, REAL)
    rows = None
    if rv.get("state") in (LOADED, STALE) and rv.get("positions") is not None:
        rows = [{"source": src, "account_type": kind, "account_label": rv.get("account_label") or SOURCE_LABELS.get(src, src), "symbol": p["symbol"],
                 "instrument_id": p.get("instrument_id"), "quantity": p.get("quantity"), "average_entry_price": p.get("average_entry_price"),
                 "current_price": p.get("current_price"), "market_value": p.get("market_value"), "unrealized_pl": p.get("unrealized_pl"),
                 "ownership": USER_MANAGED, "ownership_label": OWNERSHIP_LABELS[USER_MANAGED], "managed": False, "refreshed_at": rv.get("refreshed_at"),
                 "freshness": rv["state"], "notes": ("SYNTHETIC fixture row - not a real account." if kind == SYNTHETIC else
                                                     "Real holding in your Robinhood account; the bot never trades, exits or resizes it."),
                 "sector": p.get("sector")} for p in rv["positions"]]
    return {"source": src, "account_type": kind, "label": rv.get("label") or SOURCE_LABELS.get(src, src), "state": rv.get("state") or NOT_CONFIGURED,
            "message": rv.get("message"), "refreshed_at": rv.get("refreshed_at"), "age_s": rv.get("age_s"), "positions": rows,
            "equity": rv.get("equity"), "cash": rv.get("cash")}


def duplicate_entry_policy(symbol: str, rows: Iterable[dict], robinhood_state: str, mode: Optional[str] = None) -> Dict[str, Any]:
    """BLOCK_NEW_BOT_ENTRY_IF_EXTERNAL_EXPOSURE, ADVISORY: would a new bot entry in `symbol` add PAPER exposure on top of a REAL holding?
    Never enforced here (enforced = False): the executor's entry controls are unchanged in Stage 6.9."""
    mode = (mode or config.ROBINHOOD_DUPLICATE_ENTRY_POLICY or "ADVISORY").upper()
    held = [r for r in rows if r["symbol"] == symbol and r["ownership"] == USER_MANAGED]
    if mode == "OFF":
        would, why = False, "policy off (ROBINHOOD_DUPLICATE_ENTRY_POLICY=OFF)"
    elif held:
        would, why = True, f"Robinhood already holds {held[0]['quantity']} {symbol} (user managed)"
    elif robinhood_state not in (LOADED, STALE):
        would, why = None, f"unknown: Robinhood holdings are not loaded ({robinhood_state})"
    else:
        would, why = False, f"no Robinhood holding in {symbol}"
    return {"policy": DUPLICATE_ENTRY_POLICY, "mode": mode, "enforced": False, "would_block": would, "reason": why, "current_policy": CURRENT_ENTRY_POLICY}


def build(alpaca_view: Optional[dict], robinhood_view: Optional[dict], *, alpaca_account: Optional[dict] = None, sector_fn: Optional[Callable[[str], Optional[str]]] = None,
          ready_symbols: Iterable[str] = (), warn_pct: Optional[float] = None) -> Dict[str, Any]:
    warn_pct = float(warn_pct if warn_pct is not None else config.UNIFIED_CONCENTRATION_WARN_PCT)
    sources = [alpaca_source(alpaca_view), robinhood_source(robinhood_view)]
    known = [s for s in sources if s["positions"] is not None]
    missing = [s for s in sources if s["positions"] is None]
    stale = [s for s in known if s["state"] == STALE]
    rows = [r for s in known for r in s["positions"]]
    state = UNAVAILABLE if not known else PARTIAL if missing else STALE_SOURCE if stale else COMPLETE

    valued = [(r, _d(r["market_value"])) for r in rows]
    unvalued = [r for r, mv in valued if mv is None]
    total = sum((abs(mv) for _, mv in valued if mv is not None), Decimal("0"))
    by_symbol: Dict[str, dict] = {}
    for r, mv in valued:
        e = by_symbol.setdefault(r["symbol"], {"symbol": r["symbol"], "quantity": Decimal("0"), "gross_quantity": Decimal("0"), "market_value": Decimal("0"),
                                               "gross": Decimal("0"), "valued": True, "accounts": []})
        q = _d(r["quantity"])
        e["quantity"] += q or Decimal("0")
        e["gross_quantity"] += abs(q or Decimal("0"))
        if mv is None:
            e["valued"] = False
        else:
            e["market_value"] += mv
            e["gross"] += abs(mv)
        e["accounts"].append({"source": r["source"], "account_type": r["account_type"], "ownership": r["ownership"], "quantity": r["quantity"], "market_value": r["market_value"]})
    symbols = []
    for e in by_symbol.values():
        sector = None
        if sector_fn is not None:
            try:
                sector = sector_fn(e["symbol"])
            except Exception:  # noqa: BLE001 - a missing sector is unclassified, never guessed
                sector = None
        srcs = sorted({a["source"] for a in e["accounts"]})
        symbols.append({"symbol": e["symbol"], "quantity": _qty(e["quantity"]), "gross_quantity": _qty(e["gross_quantity"]), "market_value": _money(e["market_value"]),
                        "gross_market_value": _money(e["gross"]), "fully_valued": e["valued"], "weight_pct": _pct(e["gross"], total),
                        "sector": sector or "UNCLASSIFIED", "accounts": e["accounts"], "sources": srcs,
                        "duplicate": len(srcs) > 1, "ownerships": sorted({a["ownership"] for a in e["accounts"]})})
    symbols.sort(key=lambda s: (-(_d(s["gross_market_value"]) or Decimal("0")), s["symbol"]))

    def total_by(key: str) -> Dict[str, str]:
        acc: Dict[str, Decimal] = {}
        for r, mv in valued:
            if mv is not None:
                acc[r[key]] = acc.get(r[key], Decimal("0")) + mv
        return {k: _money(v) for k, v in sorted(acc.items())}

    sectors: Dict[str, Decimal] = {}
    unclassified = Decimal("0")
    for s in symbols:
        mv = _d(s["gross_market_value"]) or Decimal("0")
        if s["sector"] == "UNCLASSIFIED":
            unclassified += mv
        else:
            sectors[s["sector"]] = sectors.get(s["sector"], Decimal("0")) + mv
    top5 = sum(((_d(s["gross_market_value"]) or Decimal("0")) for s in symbols[:5]), Decimal("0"))
    exposure = {"basis": "ALL_SOURCES" if not missing else "KNOWN_SOURCES_ONLY", "known_sources": [s["source"] for s in known],
                "missing_sources": [s["source"] for s in missing], "stale_sources": [s["source"] for s in stale],
                "total_market_value": _money(total), "gross_exposure": _money(total), "measure": "GROSS (sum of absolute position values; PAPER + REAL)", "by_symbol": symbols, "by_source": total_by("source"),
                "by_ownership": total_by("ownership"), "by_account_type": total_by("account_type"),
                "by_sector": [{"sector": k, "market_value": _money(v), "weight_pct": _pct(v, total)} for k, v in sorted(sectors.items(), key=lambda kv: -kv[1])],
                "unclassified_market_value": _money(unclassified), "largest": ({"symbol": symbols[0]["symbol"], "weight_pct": symbols[0]["weight_pct"]} if symbols else None),
                "top5_pct": _pct(top5, total), "unvalued": [{"source": r["source"], "symbol": r["symbol"]} for r in unvalued]}

    rh = sources[1]
    acct = alpaca_account or {}
    equity = {"paper": {"source": sources[0]["source"], "label": sources[0]["label"], "equity": acct.get("equity"), "cash": acct.get("cash"),
                        "as_of": sources[0]["refreshed_at"]},
              "real_external": ({"source": rh["source"], "label": rh["label"], "equity": rh.get("equity"), "cash": rh.get("cash"), "as_of": rh["refreshed_at"]}
                                if rh["positions"] is not None else None),
              "note": "Paper and real money are separate accounts and are never added together. Paper trades are never sized from Robinhood cash."}

    warnings: List[dict] = []
    for s in missing:
        warnings.append({"code": "SOURCE_MISSING", "level": "warn" if s["state"] in (AUTH_REQUIRED, ERROR) else "info", "source": s["source"],
                         "text": f"{s['label']}: {s['state'].replace('_', ' ').lower()} - not included; its holdings are unknown, not zero."})
    for s in stale:
        warnings.append({"code": "SOURCE_STALE", "level": "warn", "source": s["source"], "text": f"{s['label']}: STALE - {s['message']}"})
    for s in symbols:
        if s["duplicate"]:
            warnings.append({"code": "DUPLICATE_SYMBOL", "level": "info", "symbol": s["symbol"],
                             "text": f"{s['symbol']} is held in {len(s['sources'])} accounts ({', '.join(s['sources'])}): {s['gross_quantity']} shares of exposure in total; ownership stays per account."})
        w = _d(s["weight_pct"])
        if w is not None and w >= Decimal(str(warn_pct)):
            warnings.append({"code": "CONCENTRATION", "level": "warn", "symbol": s["symbol"],
                             "text": f"{s['symbol']} is {s['weight_pct']}% of the known invested value (warning at {warn_pct:g}%)."})
    policies = {}
    for sym in sorted(set(ready_symbols)):
        p = duplicate_entry_policy(sym, rows, rh["state"])
        policies[sym] = p
        if p["would_block"]:
            warnings.append({"code": "EXTERNAL_EXPOSURE", "level": "warn", "symbol": sym,
                             "text": f"{sym} is READY for a bot PAPER entry and {p['reason']}. Advisory only: the executor does not use Robinhood holdings in Stage 6.9."})
    if unvalued:
        warnings.append({"code": "UNVALUED", "level": "info", "text": f"{len(unvalued)} position(s) without a usable price are excluded from the values (never guessed)."})

    disclosure = [f"{s['label']}: {s['state'].replace('_', ' ').lower()} - not included (unknown, not zero)." for s in missing] + \
                 [f"{s['label']}: STALE - included with values from {s['refreshed_at']}." for s in stale]
    return {"version": VERSION, "state": state, "sources": [{k: v for k, v in s.items() if k != "positions"} for s in sources], "rows": rows,
            "exposure": exposure, "equity": equity, "warnings": warnings, "duplicate_entry_policy": policies, "current_entry_policy": CURRENT_ENTRY_POLICY,
            "disclosure": disclosure, "write_capable": False,
            "note": "Read-only. Robinhood is not an execution broker: nothing here places, changes, cancels or closes anything. Alpaca PAPER is the only execution broker."}
