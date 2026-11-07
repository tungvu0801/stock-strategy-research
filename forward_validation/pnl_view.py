"""
forward_validation/pnl_view.py - Wave 2 PR C (design 12, J-01 / K-09; semantics 6 and 7): the READ-ONLY P/L decoration of the executor's read
routes. Every position, card, history row and CLOSED / EXIT_FILL position event of a payload gains

  pnl_status   VERIFIED | UNVERIFIED | UNKNOWN
  pnl_display  the value the UI may show as a number (a closed trade's VERIFIED realized P/L; an open position's unrealized P/L when its
               entry is priced), else None
  pnl_kind     realized | unrealized

It renames nothing: the raw fields stay as raw evidence, the executor's card and history builders are unchanged. decorate() NEVER raises: on
any error every row reads UNKNOWN with pnl_display None and the route still answers 200 (Q43-B). It finds the forward-validation service
through api.routes.trader_view.stream() and never opens a default-path store. No order, no broker call, no write. This module has no
module-level statement (design 14): its imports live inside the function.
"""


def decorate(payload):
    """The decorated payload (the same object, decorated in place). Closed rows use ForwardValidationService.verification() (PR A Rule F);
    an open row with an unpriced entry (None, "0.00" or <= 0) or no price is UNKNOWN (C-74, C-75)."""
    from decimal import Decimal, InvalidOperation
    rows = []                                                                    # (row, "closed" | "open" | "event")

    def collect(obj):
        if not isinstance(obj, dict):
            return
        if obj.get("kind") == "TRADE" and obj.get("position_id"):                 # a history row (TRADE)
            rows.append((obj, "closed"))
        elif "key" in obj and "symbol" in obj and obj.get("position_id"):        # a symbol card with a position
            rows.append((obj, "closed" if obj.get("key") == "CLOSED" else "open"))
        for k in ("cards", "recent", "history", "positions"):
            for r in obj.get(k) or []:
                if isinstance(r, dict) and k == "positions" and r.get("position_id") and "state" in r:
                    rows.append((r, "closed" if r.get("state") == "CLOSED" else "open"))
                else:
                    collect(r)
        for e in obj.get("events") or []:
            if isinstance(e, dict) and e.get("position_id") and e.get("kind") in ("CLOSED", "EXIT_FILL"):
                rows.append((e, "event"))
    try:
        collect(payload)
    except Exception:  # noqa: BLE001 - nothing collected is decorated: an undecorated row renders "P/L unknown"
        return payload

    def dec(x):
        try:
            return Decimal(str(x)) if x is not None and str(x).strip() != "" else None
        except (InvalidOperation, ValueError):
            return None
    try:
        from api.routes.trader_view import stream
        fv = stream().forward_validation
        if fv is None:
            raise RuntimeError("no forward-validation service")
        xstore = fv.execution.store
        cache = {}

        def closed(pid):
            if pid not in cache:
                p = xstore.position(pid)
                v = fv.verification(p) if p is not None else {"verified": False}
                cache[pid] = (p, bool(v.get("verified")) and p is not None and p.state == "CLOSED")
            return cache[pid]
        for row, kind in rows:
            if kind in ("closed", "event"):
                p, ok = closed(row["position_id"])
                row.update(pnl_kind="realized", pnl_status="VERIFIED" if ok else "UNVERIFIED", pnl_display=(p.realized_pnl if ok else None))
            else:
                entry = dec(row.get("entry") if "entry" in row else row.get("average_entry_price"))
                price = dec(row.get("current") if "current" in row else row.get("last_price"))
                qty = row.get("shares") if "shares" in row else row.get("quantity")
                if entry is None or entry <= 0 or price is None or qty is None:
                    row.update(pnl_kind="unrealized", pnl_status="UNKNOWN", pnl_display=None)
                else:
                    shown = row.get("pnl") if "pnl" in row and row.get("pnl") is not None else str(((price - entry) * int(qty)).quantize(Decimal("0.01")))
                    row.update(pnl_kind="unrealized", pnl_status="VERIFIED", pnl_display=shown)
    except Exception:  # noqa: BLE001 - Q43-B: never a 500; every row unknown
        for row, kind in rows:
            row.update(pnl_kind="realized" if kind in ("closed", "event") else "unrealized", pnl_status="UNKNOWN", pnl_display=None)
    for row, kind in rows:                                                      # a card's display line carries its P/L text: never a raw number for a row that is not
        try:                                                                    # VERIFIED (I12; the product UI's agent and focus cards render `line` as is)
            raw = row.get("pnl")
            if "key" in row and isinstance(row.get("line"), str) and row.get("pnl_status") != "VERIFIED" and raw not in (None, "") and f" · {raw}" in row["line"]:
                row["line"] = row["line"].replace(f" · {raw}", " · P/L unverified" if row.get("pnl_status") == "UNVERIFIED" else " · P/L unknown")
        except Exception:  # noqa: BLE001
            pass
    return payload
