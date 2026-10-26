"""
execution/account_positions.py - the Alpaca PAPER account's positions, as the read-only account refresh already fetched them, each marked
bot-managed or external (DESIGN_68_ACCOUNT_POSITIONS_BOT_ACTIVITY.md section 2).

  classify(broker_positions, store)   one row per broker position: BOT | BOT_ENTRY | NEEDS_REVIEW | EXTERNAL, by the SAME rule the executor's
                                      reconciliation uses (ExecutionService._reconcile; tests/test_bot_activity_68.py proves the parity)
  view(refresher, store)              the account-refresh snapshot classified, with its source and freshness:
                                      LOADED (rows, possibly none) | NOT_LOADED (no snapshot yet: nothing is known) | DISABLED (no paper broker configured)

Zero broker calls: this reads the account-refresh snapshot and the executor's own tables only. It never changes the execution mode, a position
or an order, and it is display only: the executor's entry controls keep using the broker positions it reads itself in each Paper Auto cycle.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

MANAGEMENT = {
    "BOT": ("Bot-managed", "Opened by the bot from a broker-confirmed fill; the bot monitors it and decides its exit."),
    "BOT_ENTRY": ("Bot entry in progress", "The bot has an open entry order in this symbol."),
    "NEEDS_REVIEW": ("Not managed · needs a look", "The bot's own trade in this symbol is closed, yet the account still holds shares. The bot will not act on it."),
    "EXTERNAL": ("External · not managed by the bot", "Not opened by the bot. The bot never trades or exits it; it counts toward the open-positions limit "
                                                      "and blocks a new bot entry in the same symbol."),
}
SOURCES = {"ALPACA_PAPER": ("Alpaca PAPER account", "Alpaca PAPER account · read-only account refresh"),
           "SIMULATED_PAPER": ("Simulated paper account", "Simulated paper broker · not an Alpaca account")}   # the snapshot names its own source; never relabelled
FIELDS = ("qty", "side", "avg_entry_price", "current_price", "market_value", "unrealized_pl", "unrealized_plpc", "cost_basis")


def classify(broker_positions: List[dict], store) -> List[Dict[str, Any]]:
    """Each broker position marked by the executor's reconciliation rule: a symbol with an open internal position or an open ENTRY intent is
    the bot's; a symbol whose internal position is CLOSED is INTERNAL_FLAT_BROKER_POSITION (NEEDS_REVIEW); anything else is EXTERNAL."""
    internal_open = {p.symbol for p in store.positions(open_only=True)}
    entering = {i.symbol for i in store.open_intents() if i.intent_type == "ENTRY"}
    closed = {p.symbol for p in store.positions(open_only=False, limit=500) if p.state == "CLOSED"}
    rows = []
    for b in broker_positions:
        sym = str(b.get("symbol") or "").upper()
        kind = "BOT" if sym in internal_open else "BOT_ENTRY" if sym in entering else "NEEDS_REVIEW" if sym in closed else "EXTERNAL"
        label, text = MANAGEMENT[kind]
        rows.append({"symbol": sym, **{k: b.get(k) for k in FIELDS}, "management": kind, "management_label": label, "management_text": text,
                     "managed": kind in ("BOT", "BOT_ENTRY")})
    rows.sort(key=lambda r: r["symbol"])
    return rows


def _loop_running() -> Optional[bool]:
    try:
        from account_refresh.scheduler import status as loop_status
        return bool(loop_status().get("running"))
    except Exception:  # noqa: BLE001 - display only
        return None


def view(refresher, store, loop_running: Optional[bool] = None) -> Dict[str, Any]:
    """The account-refresh snapshot, classified. `positions` is None unless a snapshot exists: NOT_LOADED never reads as "no position"."""
    loop_running = _loop_running() if loop_running is None else loop_running
    base: Dict[str, Any] = {"source": None, "account_label": SOURCES["ALPACA_PAPER"][0], "source_label": SOURCES["ALPACA_PAPER"][1], "host": None, "refreshed_at": None,
                            "freshness": None, "positions": None, "counts": None, "loop_running": loop_running, "broker_calls": 0}
    named = lambda src: dict(zip(("account_label", "source_label"), SOURCES.get(src, (str(src), str(src)))))  # noqa: E731
    if refresher is None or not refresher.enabled():
        return {**base, "state": "DISABLED", "message": "No Alpaca PAPER broker is configured, so no account positions are loaded."}
    fr = refresher.freshness()
    fresh = {k: fr.get(k) for k in ("status", "fresh", "label", "short_label", "reason", "age_seconds", "refreshed_at", "last_success_at", "consecutive_failures")}
    snap = refresher.store.snapshot()
    if snap is None:
        why = ("The read-only account refresh runs automatically; the positions appear after its first successful read." if loop_running
               else "The account refresh loop is not running (ACCOUNT_REFRESH_ENABLED=false or not started), so nothing has been read.")
        return {**base, **named(fr.get("source")), "state": "NOT_LOADED", "freshness": fresh, "source": fr.get("source"), "message": f"Not loaded yet. {why}"}
    rows = classify(snap.get("positions") or [], store)
    counts = {"total": len(rows), "bot": sum(r["management"] == "BOT" for r in rows), "bot_entry": sum(r["management"] == "BOT_ENTRY" for r in rows),
              "external": sum(r["management"] == "EXTERNAL" for r in rows), "needs_review": sum(r["management"] == "NEEDS_REVIEW" for r in rows)}
    counts["unmanaged"] = counts["external"] + counts["needs_review"]
    names = named(snap.get("source"))
    msg = f"No positions in the {names['account_label']}." if not rows else f"{len(rows)} position(s) in the {names['account_label']}."
    if not fr.get("fresh"):
        msg += f" {fr.get('label') or 'Account data not fresh'}: these values are from {snap['refreshed_at']} and may be out of date."
    return {**base, **names, "state": "LOADED", "source": snap.get("source"), "host": snap.get("host") if snap.get("source") == "ALPACA_PAPER" else None,
            "refreshed_at": snap.get("refreshed_at"), "freshness": fresh,
            "positions": rows, "counts": counts, "message": msg}
