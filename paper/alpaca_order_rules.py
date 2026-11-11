"""
paper/alpaca_order_rules.py — Stage 4.6B PURE rules for manual Alpaca PAPER orders (DESIGN_46B_FINAL). No I/O, no network,
no database, no clock of its own: every input is passed in. Deterministic.

  * client_order_id: "sa46b-" + uuid4 hex, created ONCE per preview and reused forever (retries included)
  * the exact canonical POST body (sorted keys, no spaces, qty as a string) and its sha256; the preview hash
  * validation rules V1–V17, and V18–V20 of the Emergency Stop order gate (each returns a fixed code and text; never AI)
  * the classification of the single POST's outcome into O1–O9 (conservative: only a failure before any byte left is
    "not sent"; only an allow-listed, definitive, non-duplicate rejection may become BROKER_REJECTED)
  * the Alpaca status → state map and the exact consistency check of a found order (never heuristic matching)
  * fixed, sanitised rejection texts (raw broker text is never passed on)
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Optional

# ---- limits and timing (DESIGN_46B_FINAL §6.4 / §7 I8) --------------------------------------------------------------------------
MAX_QTY = 10_000
MAX_NOTIONAL = Decimal("100000.00")
DAILY_LIMIT = 20
BUY_BUFFER = Decimal("1.05")
PREVIEW_TTL_S = 120
NEAR_CLOSE_S = 300
MAX_ATTEMPTS = 3
AUTO_LOOKUP_DELAY_S = 2.0
NOT_FOUND_SPAN_S = 30
STALE_CONFIRMING_S = 60
STALE_SUBMITTING_S = 45
STATUS_SPACING_S = 3
STATUS_MAX = 20
COID_PREFIX = "sa46b-"
COID_RE = re.compile(r"^sa46b-[0-9a-f]{32}$")
SYMBOL_RE = re.compile(r"^[A-Z]{1,5}(\.[A-Z]{1,2})?$")
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
MONEY, PRICE = Decimal("0.01"), Decimal("0.0001")

# ---- states (DESIGN_46B_FINAL §4) --------------------------------------------------------------------------------------------------
PREVIEWED, SUPERSEDED, EXPIRED = "PREVIEWED", "SUPERSEDED", "EXPIRED"
CONFIRMING, CONFIRM_REJECTED = "CONFIRMING", "CONFIRM_REJECTED"
SUBMISSION_PENDING, SUBMIT_NOT_SENT, RR = "SUBMISSION_PENDING", "SUBMIT_NOT_SENT", "RECONCILIATION_REQUIRED"
SUBMITTED, BROKER_ACCEPTED, PARTIALLY_FILLED = "SUBMITTED", "BROKER_ACCEPTED", "PARTIALLY_FILLED"
FILLED, CANCELED, BROKER_REJECTED, ABANDONED = "FILLED", "CANCELED", "BROKER_REJECTED", "ABANDONED"
TERMINAL = frozenset({SUPERSEDED, EXPIRED, CONFIRM_REJECTED, FILLED, CANCELED, BROKER_REJECTED, ABANDONED})
UNRESOLVED = frozenset({CONFIRMING, SUBMISSION_PENDING, SUBMIT_NOT_SENT, RR})
LIVE = frozenset({SUBMITTED, BROKER_ACCEPTED, PARTIALLY_FILLED})
BROKER_STATES = LIVE | {FILLED, CANCELED, BROKER_REJECTED, RR}
_AFTER_SUBMIT = BROKER_STATES | {SUBMIT_NOT_SENT}
ALLOWED = {PREVIEWED: {SUPERSEDED, EXPIRED, CONFIRMING},
           CONFIRMING: {CONFIRM_REJECTED, SUBMISSION_PENDING},
           SUBMISSION_PENDING: set(_AFTER_SUBMIT),
           SUBMIT_NOT_SENT: set(BROKER_STATES) | {SUBMISSION_PENDING, ABANDONED},
           RR: set(BROKER_STATES) | {SUBMISSION_PENDING, ABANDONED},
           SUBMITTED: set(BROKER_STATES), BROKER_ACCEPTED: set(BROKER_STATES), PARTIALLY_FILLED: set(BROKER_STATES)}
LABELS = {PREVIEWED: "Preview", SUPERSEDED: "Superseded", EXPIRED: "Expired", CONFIRMING: "Confirming",
          CONFIRM_REJECTED: "Not sent — confirmation rejected", SUBMISSION_PENDING: "Sending",
          SUBMIT_NOT_SENT: "Not sent", RR: "Reconciliation required", SUBMITTED: "Submitted", BROKER_ACCEPTED: "Accepted",
          PARTIALLY_FILLED: "Partially filled", FILLED: "Filled", CANCELED: "Canceled", BROKER_REJECTED: "Rejected by Alpaca",
          ABANDONED: "Abandoned"}


def transition_ok(frm: str, to: str) -> bool:
    """Allowed moves of §4; a same-state update (lookup counters, broker fields) only for unresolved / live states."""
    return to == frm and frm in (RR, SUBMIT_NOT_SENT, SUBMITTED, BROKER_ACCEPTED, PARTIALLY_FILLED) or to in ALLOWED.get(frm, set())


# ---- rule texts (fixed; no AI) -------------------------------------------------------------------------------------------------------
RULE_TEXT = {
    "NOT_CONFIGURED": "Alpaca Paper is not configured (ALPACA_PAPER_API_KEY and ALPACA_PAPER_SECRET_KEY in .env).",
    "NOT_LINKED": "Link your Alpaca paper account first.",
    "NOT_ENABLED": "Manual paper orders are OFF. Turn them on first.",
    "INVALID_SYMBOL": "Enter a valid US stock ticker (1–5 letters, optional class suffix such as BRK.B).",
    "ASSET_NOT_SUPPORTED": "Alpaca does not list this symbol as an active, tradable US equity outside OTC.",
    "INVALID_SIDE": "Side must be BUY or SELL (long only).",
    "INVALID_QUANTITY": "Shares must be a whole number from 1 to 10,000.",
    "ACCOUNT_NOT_ACTIVE": "Alpaca reports this paper account as not ACTIVE, blocked or suspended.",
    "ACCOUNT_NOT_LINKED": "The configured Alpaca paper account is not the linked account. Relink explicitly to use it.",
    "NO_POSITION": "There is no long position in this symbol in the Alpaca paper account.",
    "INSUFFICIENT_QTY_AVAILABLE": "The sell exceeds the shares available to sell in the Alpaca paper account.",
    "REFERENCE_PRICE_UNAVAILABLE": "No previous-close reference price is available, so the Stock Agent cash guard cannot be checked.",
    "CASH_GUARD": "The Stock Agent cash guard (previous close + 5%) does not fit in the Alpaca paper cash.",
    "UNRESOLVED_IN_SYMBOL": "Another manual paper order in this symbol has an unresolved outcome. Resolve it first.",
    "DAILY_LIMIT": "20 manual paper orders have already been confirmed today (New York).",
    "NOTIONAL_LIMIT": "The estimated notional is above $100,000 per order.",
    "NO_SHORTING_REQUIRED": "Turn on 'no shorting' in your Alpaca paper dashboard; Stock Agent never changes it.",
    "MARKET_CLOSED": "The market is closed — confirmation is available during regular hours.",
    "NEAR_CLOSE": "Confirmation is disabled within 5 minutes of the market close.",
    # ---- Emergency Stop order gate (EMERGENCY_STOP_ORDER_GATE_DESIGN.md revision 3.5, sections 3.2, 3.5, 3.6, 4): each under 200 characters
    "EMERGENCY_STOP_ACTIVE": "Emergency Stop is active. This order was not sent.",
    "EMERGENCY_STOP_STATE_UNKNOWN": ("The Emergency Stop state could not be read, so manual orders are refused. Nothing was sent. Use Alpaca's website if "
                                     "you must act now."),
    "STOP_BLOCKS_BUY": "Emergency Stop blocks manual BUY orders, including buy-to-cover. Nothing was sent. Alpaca's website stays available.",
    "STOP_BOT_OWNED": ("The bot manages this symbol, so the app refuses this SELL during Emergency Stop. A position marked Needs attention gets "
                       "no automatic exit. Alpaca's website stays available; see the runbook first."),
    "STOP_OWNERSHIP_UNKNOWN": "Could not check whether the bot manages this symbol, so the order was refused. Nothing was sent.",
    "STOP_READS_STALE": "The broker reads for this order are too old. Nothing was sent. Preview it again.",
    "STOP_UNKNOWN_SIDE": "During Emergency Stop only a SELL can be checked; this order's side is not one. Nothing was sent.",
    "STOP_SELL_NO_POSITION": "There is no position to reduce, so this SELL would open a short during Emergency Stop. Nothing was sent.",
    "STOP_POSITION_NOT_LONG": "The broker does not report this position as long, so the SELL cannot be checked during Emergency Stop. Nothing was sent.",
    "STOP_POSITION_INCONSISTENT": "The broker's position figures are missing or inconsistent, so the SELL was refused during Emergency Stop. Nothing was sent.",
    "STOP_SELL_EXCEEDS_AVAILABLE": "The SELL is larger than the shares available to sell, so it was refused during Emergency Stop. Nothing was sent.",
    "STOP_NO_SHORTING_UNCONFIRMED": "'No shorting' is not confirmed on the paper account, so the SELL was refused during Emergency Stop. Nothing was sent.",
    "STOP_UNRESOLVED_IN_SYMBOL": "Another manual order in this symbol has an unresolved outcome, so the SELL was refused during Emergency Stop. Nothing was sent.",
    "STOP_OPEN_ORDERS_UNREADABLE": "The open orders in this symbol could not be read completely, so the SELL was refused during Emergency Stop. Nothing was sent.",
    "STOP_OPEN_ORDER_CONFLICT": ("An open order in this symbol is not a plain SELL (or its size is unclear), so the SELL was refused during Emergency Stop. "
                                 "Nothing was sent."),
    "STOP_SELLS_EXCEED_POSITION": "Open SELL orders plus this SELL exceed the position, so it was refused during Emergency Stop. Nothing was sent.",
    "PAUSE_DOES_NOT_BLOCK_MANUAL": "A Pause is active. It stops automatic entries. It does not block this manual paper order.",
    "DAILY_HALT_DOES_NOT_BLOCK_MANUAL": "The daily-loss halt is active. It stops automatic entries. It does not block this manual paper order.",
    "BOT_OWNED_SELL_WARNING": ("The bot manages this symbol. Selling it leaves the bot's record wrong: the position needs attention, a remainder gets "
                               "no automatic exit, and entries and Paper Auto stay refused until recovery runs."),
}
RULE_TEXT["DAILY_LIMIT"] += " This limit also applies during Emergency Stop. Alpaca's website stays available."   # C-5: kept and disclosed (design section 6)

# ---- Emergency Stop order gate: the latch and the manual-order classification (design revision 3.5, sections 3 and 4) ---------------
STOP_OFF, STOP_ON, STOP_UNKNOWN = "OFF", "ON", "UNKNOWN"              # OrderStore.stop_state(): the persisted latch, never the gate's evaluation
RISK_REDUCING, RISK_INCREASING, AMBIGUOUS = "RISK_REDUCING", "RISK_INCREASING", "AMBIGUOUS"
STOP_READS_MAX_AGE_S = 10                                             # a stop-time SELL's classification is at most this old at the arm (3.3)
OPEN_ORDERS_LIMIT = 200                                               # GET /v2/orders?status=open&symbols=SYM&limit=200&direction=desc
STOP_EXIT_ALLOWED, ARMED_BEFORE_STOP = "STOP_EXIT_ALLOWED", "ARMED_BEFORE_STOP"   # STATUS_CHANGE event codes (no new audit kind)
ARMED_BEFORE_STOP_LABEL = "armed before the stop"
CASH_GUARD_TEXT = ("Stock Agent cash guard: previous close + 5% must fit in your Alpaca paper cash — a conservative estimate, "
                   "not the fill price; Alpaca decides buying power")
REFERENCE_NOTE = "previous close ({}) — not a quote, not the fill price"

# ---- rejection categories (allow-listed; message substrings, lower-case) — DESIGN_46B_FINAL §7 O4–O7 ---------------------------
DUPLICATE_MARKERS = ("client_order_id", "unique", "duplicate")
CATEGORIES = (
    ("INSUFFICIENT_BUYING_POWER", ("insufficient buying power",)),
    ("INSUFFICIENT_QTY", ("insufficient qty", "insufficient quantity", "not enough shares", "qty available")),
    ("WASH_TRADE", ("wash trade",)),
    ("PDT_PROTECTION", ("pattern day", "day trading protection")),
    ("SHORT_NOT_ALLOWED", ("not allowed to short", "short sale not allowed", "shorting is not allowed", "cannot be sold short")),
    ("ASSET_NOT_TRADABLE", ("not tradable", "is not active", "asset not found", "could not find asset")),
    ("ACCOUNT_RESTRICTED", ("account is restricted", "account restricted", "account is blocked", "trading is blocked")),
    ("INVALID_QUANTITY", ("qty must be", "invalid qty", "qty is required", "quantity must")),
    ("MARKET_CLOSED", ("market is closed", "market closed")),
)
DEFINITIVE_422 = frozenset({"INVALID_QUANTITY", "ASSET_NOT_TRADABLE", "WASH_TRADE", "PDT_PROTECTION", "SHORT_NOT_ALLOWED",
                            "INSUFFICIENT_QTY", "MARKET_CLOSED"})
DEFINITIVE_403 = frozenset({"INSUFFICIENT_BUYING_POWER", "INSUFFICIENT_QTY", "ACCOUNT_RESTRICTED", "PDT_PROTECTION", "WASH_TRADE"})
CATEGORY_TEXT = {
    "INSUFFICIENT_BUYING_POWER": "Alpaca rejected the order: insufficient buying power (Alpaca decides buying power).",
    "INSUFFICIENT_QTY": "Alpaca rejected the order: not enough shares available.",
    "WASH_TRADE": "Alpaca rejected the order: potential wash trade.",
    "PDT_PROTECTION": "Alpaca rejected the order: pattern day trading protection.",
    "SHORT_NOT_ALLOWED": "Alpaca rejected the order: short selling is not allowed.",
    "ASSET_NOT_TRADABLE": "Alpaca rejected the order: the asset is not tradable.",
    "ACCOUNT_RESTRICTED": "Alpaca rejected the order: the account is restricted.",
    "INVALID_QUANTITY": "Alpaca rejected the order: invalid quantity.",
    "MARKET_CLOSED": "Alpaca rejected the order: the market is closed.",
}
OUTCOME_TEXT = {
    "NOT_SENT": "The order was not sent (no connection was made). You can retry with the same client order id.",
    "LOCAL_REFUSAL": "The order was not sent (refused before sending).",
    "UNREADABLE_200": "Alpaca answered, but the answer could not be read. Check the order status before anything else.",
    "INCONSISTENT_200": "Alpaca's answer did not match this order exactly. Check the order status before anything else.",
    "DUPLICATE_NOT_FOUND": "Alpaca reported this client order id as already used, but the exact lookup did not find it yet.",
    "UNCERTAIN_TIMEOUT": "No answer from Alpaca in time — the order may or may not exist. Check the order status.",
    "UNCERTAIN_CONNECTION": "The connection broke after sending — the order may or may not exist. Check the order status.",
    "MISMATCH": "An order with this client order id exists but does not match exactly — it is not linked.",
    "BROKER_REPLACED": "Alpaca reports the order as replaced outside Stock Agent — not followed automatically.",
    "UNKNOWN_STATUS": "Alpaca reported an unknown order status.",
    "INTERRUPTED": "The confirmation was interrupted before anything was sent.",
    "INTERRUPTED_SUBMISSION": "The app stopped while sending — the order may or may not exist. Check the order status.",
}


def uncertain_text(code: str) -> str:
    if code.startswith("UNCERTAIN_HTTP_"):
        return f"Alpaca answered HTTP {code.rsplit('_', 1)[1]} — the order may or may not exist. Check the order status."
    return OUTCOME_TEXT.get(code) or CATEGORY_TEXT.get(code) or RULE_TEXT.get(code) or "See the order status."


# ---- identifiers, payload, hashes ----------------------------------------------------------------------------------------------------
def new_client_order_id() -> str:
    return COID_PREFIX + uuid.uuid4().hex


def canonical_payload(client_order_id: str, symbol: str, side: str, qty: int) -> bytes:
    """The exact POST /v2/orders body: MARKET, DAY, whole shares, no extended hours, no order class, no price."""
    body = {"client_order_id": client_order_id, "qty": str(int(qty)), "side": side.lower(), "symbol": symbol,
            "time_in_force": "day", "type": "market"}
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha256_hex(data) -> str:
    return hashlib.sha256(data if isinstance(data, bytes) else str(data).encode("utf-8")).hexdigest()


def canonical_json(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def preview_hash(preview: dict) -> str:
    return sha256_hex(canonical_json(preview).encode("utf-8"))


def fingerprint(account_id) -> str:
    return sha256_hex(str(account_id))


def mask(account_number) -> str:
    s = str(account_number or "")
    return "••••" + (s[-4:] if len(s) > 4 else "")


def dec(v) -> Optional[Decimal]:
    if v is None or v == "":
        return None
    try:
        d = Decimal(str(v))
    except (InvalidOperation, ValueError):
        raise ValueError("not a number") from None
    if not d.is_finite():
        raise ValueError("not a finite number")
    return d


def money(d: Decimal) -> str:
    return str(d.quantize(MONEY))


def qty_str(d: Optional[Decimal]) -> Optional[str]:
    if d is None:
        return None
    return str(int(d)) if d == d.to_integral_value() else format(d.normalize(), "f")


def parse_time(v) -> datetime:
    return datetime.fromisoformat(str(v).replace("Z", "+00:00"))


# ---- validation rules ---------------------------------------------------------------------------------------------------------------
def result(rule: str, ok: bool, code: Optional[str] = None, info: bool = False) -> dict:
    return {"rule": rule, "ok": bool(ok), "code": None if ok else code, "text": None if ok else RULE_TEXT.get(code, code),
            "info": info}


def body_errors(symbol, side, quantity) -> Optional[str]:
    """V3 (format), V4, V5 — refusals before any request."""
    if not isinstance(symbol, str) or not SYMBOL_RE.match(symbol):
        return "INVALID_SYMBOL"
    if side not in ("BUY", "SELL"):
        return "INVALID_SIDE"
    if not isinstance(quantity, int) or isinstance(quantity, bool) or not 1 <= quantity <= MAX_QTY:
        return "INVALID_QUANTITY"
    return None


def v3_asset(asset: Optional[dict]) -> dict:
    ok = bool(asset) and asset.get("class") == "us_equity" and asset.get("status") == "active" and \
        asset.get("tradable") is True and str(asset.get("exchange") or "").upper() != "OTC"
    return result("V3", ok, "ASSET_NOT_SUPPORTED")


def v8_account(account: dict, bound_fp: Optional[str]) -> dict:
    if bound_fp is not None and account.get("fingerprint") != bound_fp:
        return result("V8", False, "ACCOUNT_NOT_LINKED")
    ok = account.get("status") == "ACTIVE" and account.get("currency") == "USD" and not account.get("trading_blocked") \
        and not account.get("account_blocked") and not account.get("trade_suspended_by_user")
    return result("V8", ok, "ACCOUNT_NOT_ACTIVE")


def v9_sell(position: Optional[dict], qty: int, other_unresolved_sell_qty: int) -> dict:
    if not position or position.get("side") != "long" or (dec(position.get("qty")) or 0) <= 0:
        return result("V9", False, "NO_POSITION")
    available = dec(position.get("qty_available")) or Decimal(0)
    return result("V9", Decimal(qty) <= available - Decimal(other_unresolved_sell_qty), "INSUFFICIENT_QTY_AVAILABLE")


def guard_amount(qty: int, reference: Decimal) -> Decimal:
    return (Decimal(qty) * reference * BUY_BUFFER).quantize(MONEY)


def v10_buy(reference: Optional[Decimal], qty: int, cash: Optional[Decimal], other_unresolved_buy: Decimal) -> dict:
    if reference is None:
        return result("V10", False, "REFERENCE_PRICE_UNAVAILABLE")
    return result("V10", cash is not None and guard_amount(qty, reference) + other_unresolved_buy <= cash, "CASH_GUARD")


def v11_unresolved(n_other_unresolved_in_symbol: int) -> dict:
    return result("V11", n_other_unresolved_in_symbol == 0, "UNRESOLVED_IN_SYMBOL")


def v12a_daily(confirmed_today: int) -> dict:
    return result("V12a", confirmed_today < DAILY_LIMIT, "DAILY_LIMIT")


def v12b_notional(notional: Optional[Decimal]) -> dict:
    return result("V12b", notional is None or notional <= MAX_NOTIONAL, "NOTIONAL_LIMIT")


def v15_no_shorting(config: dict) -> dict:
    return result("V15", config.get("no_shorting") is True, "NO_SHORTING_REQUIRED")


def v16_open(clock: dict, info: bool = False) -> dict:
    return result("V16", clock.get("is_open") is True, "MARKET_CLOSED", info=info)


def seconds_to_close(clock: dict) -> Optional[float]:
    try:
        return (parse_time(clock["next_close"]) - parse_time(clock["timestamp"])).total_seconds()
    except (KeyError, TypeError, ValueError):
        return None


def v17_near_close(clock: dict, info: bool = False) -> dict:
    """Both times from the SAME Alpaca clock response (server time, never the local clock)."""
    s = seconds_to_close(clock)
    return result("V17", s is not None and s > NEAR_CLOSE_S, "NEAR_CLOSE", info=info)


def blocking(results) -> list:
    return [r for r in results if not r["ok"] and not r["info"]]


# ---- Emergency Stop order gate: classification of a manual order while the latch is ON (design revision 3.5, section 4) ------------
def _num(v) -> Optional[Decimal]:
    """A strict quantity: None when missing; raises ValueError when unparsable (a read with an unparsable field fails closed upstream)."""
    return None if v is None else dec(v)


def classify_manual_order(side, qty, bot_owned, position, open_orders, others_unresolved_in_symbol, no_shorting) -> tuple:
    """(class, code). Runs only while the latch is ON; only RISK_REDUCING passes. The first matching row wins; rows 1 to 4 need no broker read.
    `position` is the STRICT position read (None = 404, no position; missing fields stay missing); `open_orders` is the open-orders read of
    this symbol (None = unreadable); `others_unresolved_in_symbol` counts OTHER own intents with an unresolved outcome in this symbol."""
    if side == "BUY":                                                                         # 1: buy-to-cover included (C-3)
        return RISK_INCREASING, "STOP_BLOCKS_BUY"
    if side != "SELL":                                                                        # 2
        return AMBIGUOUS, "STOP_UNKNOWN_SIDE"
    if bot_owned is None:                                                                     # 3
        return AMBIGUOUS, "STOP_OWNERSHIP_UNKNOWN"
    if bot_owned is not False:                                                                # 4 (C-8)
        return AMBIGUOUS, "STOP_BOT_OWNED"
    if position is None:                                                                      # 5: no position, the SELL would open a short
        return RISK_INCREASING, "STOP_SELL_NO_POSITION"
    if position.get("side") != "long":                                                        # 6: missing or not exactly "long"
        return AMBIGUOUS, "STOP_POSITION_NOT_LONG"
    try:
        held, available = _num(position.get("qty")), _num(position.get("qty_available"))
    except ValueError:
        return AMBIGUOUS, "STOP_POSITION_INCONSISTENT"
    if held is None or available is None or held <= 0 or available < 0 or available > held:  # 7
        return AMBIGUOUS, "STOP_POSITION_INCONSISTENT"
    if Decimal(qty) > available:                                                              # 8
        return RISK_INCREASING, "STOP_SELL_EXCEEDS_AVAILABLE"
    if no_shorting is not True:                                                               # 9
        return AMBIGUOUS, "STOP_NO_SHORTING_UNCONFIRMED"
    if others_unresolved_in_symbol:                                                           # 10: another own intent may exist at the broker
        return AMBIGUOUS, "STOP_UNRESOLVED_IN_SYMBOL"
    if not isinstance(open_orders, list) or len(open_orders) >= OPEN_ORDERS_LIMIT or \
            any(not isinstance(o, dict) or o.get("symbol") != position.get("symbol") for o in open_orders):   # 11
        return AMBIGUOUS, "STOP_OPEN_ORDERS_UNREADABLE"
    resting = Decimal(0)
    for o in open_orders:                                                                     # 12: only plain SELLs with known sizes
        try:
            oq, of = _num(o.get("qty")), _num(o.get("filled_qty"))
        except ValueError:
            return AMBIGUOUS, "STOP_OPEN_ORDER_CONFLICT"
        if o.get("side") != "sell" or o.get("order_class") not in ("", "simple") or oq is None or of is None or of < 0 or of > oq:
            return AMBIGUOUS, "STOP_OPEN_ORDER_CONFLICT"
        resting += oq - of
    if resting + Decimal(qty) > held:                                                         # 13: compared with qty, not qty_available
        return AMBIGUOUS, "STOP_SELLS_EXCEED_POSITION"
    return RISK_REDUCING, None                                                                # 14


def v18_stop(klass: str, code: Optional[str]) -> dict:
    """V18: the Emergency Stop classification of this SELL (a refusal is ok false, so its text prints with no frontend change)."""
    return result("V18", klass == RISK_REDUCING, code or "EMERGENCY_STOP_ACTIVE")


def v19_disclosure(flags: Optional[dict], trading_date: str) -> Optional[dict]:
    """V19 (C-1): a Pause or today's daily-loss halt does not block a manual order. Informational: prints, never blocks. None adds no line."""
    if not flags:
        return None
    if flags.get("pause_active") == 1:
        return result("V19", False, "PAUSE_DOES_NOT_BLOCK_MANUAL", info=True)
    if flags.get("daily_halt_active") == 1 and flags.get("daily_halt_trading_date") == trading_date:
        return result("V19", False, "DAILY_HALT_DOES_NOT_BLOCK_MANUAL", info=True)
    return None


def v20_bot_owned_warning() -> dict:
    """V20 (R-5): with the stop off, a SELL of a bot-owned symbol prints this warning; it does not block (PF-18 stays accepted)."""
    return result("V20", False, "BOT_OWNED_SELL_WARNING", info=True)


# ---- the broker's answer ------------------------------------------------------------------------------------------------------------
def parse_order(body) -> Optional[dict]:
    """A parsed Alpaca order (the fields 4.6B uses) or None if the answer is not an order."""
    try:
        o = json.loads(body.decode("utf-8") if isinstance(body, bytes) else body)
    except (ValueError, UnicodeDecodeError, AttributeError):
        return None
    if not isinstance(o, dict) or not isinstance(o.get("id"), str) or not UUID_RE.match(o["id"]):
        return None
    try:
        return {"id": o["id"], "client_order_id": o.get("client_order_id"), "symbol": o.get("symbol"), "side": o.get("side"),
                "qty": qty_str(dec(o.get("qty"))), "type": o.get("type") or o.get("order_type"),
                "time_in_force": o.get("time_in_force"), "order_class": o.get("order_class") or "",
                "extended_hours": bool(o.get("extended_hours")), "status": o.get("status"),
                "filled_qty": qty_str(dec(o.get("filled_qty"))), "filled_avg_price": o.get("filled_avg_price") and str(dec(o["filled_avg_price"]))}
    except ValueError:
        return None


def consistent(order: dict, intent: dict) -> bool:
    """Exact identity of a found order with the stored intent — never a heuristic match."""
    return (order.get("client_order_id") == intent["client_order_id"] and order.get("symbol") == intent["symbol"]
            and order.get("side") == intent["side"] and order.get("qty") == str(intent["qty"])
            and order.get("type") == "market" and order.get("time_in_force") == "day"
            and order.get("order_class") in ("", "simple") and order.get("extended_hours") is False)


def parse_error(body) -> Optional[dict]:
    try:
        e = json.loads(body.decode("utf-8") if isinstance(body, bytes) else body)
    except (ValueError, UnicodeDecodeError, AttributeError):
        return None
    if not isinstance(e, dict) or not isinstance(e.get("message"), str):
        return None
    code = e.get("code")
    return {"code": code if isinstance(code, int) else None, "message": e["message"][:500].lower()}


def category_of(message: str) -> Optional[str]:
    return next((cat for cat, needles in CATEGORIES if any(n in message for n in needles)), None)


def classify_post(res: dict, intent: dict) -> dict:
    """DESIGN_46B_FINAL §7. `res` is the writer's result: {"kind": "response", "status", "body"} or {"kind": "not_sent" |
    "uncertain", "reason"}. Returns the outcome class, the automatic lookup policy and the codes to record."""
    out = {"klass": None, "lookup": None, "code": None, "http_status": None, "broker_error_code": None, "order": None}
    if res["kind"] == "not_sent":
        return {**out, "klass": "O3", "code": res.get("reason") or "NOT_SENT"}
    if res["kind"] != "response":
        return {**out, "klass": "O8", "lookup": "later", "code": "UNCERTAIN_" + (res.get("reason") or "CONNECTION")}
    status, body = res["status"], res.get("body") or b""
    out["http_status"] = status
    if status == 200:
        order = parse_order(body)
        if order is not None and consistent(order, intent):
            return {**out, "klass": "O1", "order": order}
        return {**out, "klass": "O2", "lookup": "later", "code": "UNREADABLE_200" if order is None else "INCONSISTENT_200"}
    if status in (403, 422):
        err = parse_error(body)
        if err is not None:
            out["broker_error_code"] = err["code"]
            if any(m in err["message"] for m in DUPLICATE_MARKERS):
                return {**out, "klass": "O4", "lookup": "now", "code": "DUPLICATE_NOT_FOUND"}
            cat = category_of(err["message"])
            if status == 422 and cat in DEFINITIVE_422:
                return {**out, "klass": "O5", "lookup": "now", "code": cat}
            if status == 403 and cat in DEFINITIVE_403:
                return {**out, "klass": "O6", "lookup": "now", "code": cat}
        return {**out, "klass": "O7", "lookup": "later", "code": f"UNCERTAIN_HTTP_{status}"}
    return {**out, "klass": "O8", "lookup": "later", "code": f"UNCERTAIN_HTTP_{status}"}


# ---- Alpaca status → state (DESIGN_46B_FINAL §8) -------------------------------------------------------------------------------------
STATUS_MAP = {"pending_new": SUBMITTED, "accepted": SUBMITTED, "accepted_for_bidding": SUBMITTED, "pending_review": SUBMITTED,
              "new": BROKER_ACCEPTED, "held": BROKER_ACCEPTED, "calculated": BROKER_ACCEPTED, "done_for_day": BROKER_ACCEPTED,
              "pending_cancel": BROKER_ACCEPTED, "suspended": BROKER_ACCEPTED, "stopped": BROKER_ACCEPTED,
              "partially_filled": PARTIALLY_FILLED, "filled": FILLED, "canceled": CANCELED, "expired": CANCELED,
              "rejected": BROKER_REJECTED}


def map_status(status) -> tuple:
    if status in STATUS_MAP:
        return STATUS_MAP[status], None
    if status in ("replaced", "pending_replace"):
        return RR, "BROKER_REPLACED"
    return RR, "UNKNOWN_STATUS"
