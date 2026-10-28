"""
api/routes/decision_flow.py - Stage 6.10C AGENT DECISION FLOW (GET only; DESIGN_610C_DECISION_FLOW.md section 6). A read-only projection of the rows
the setup evaluator, the risk gate, Paper Auto and the executor already persist: nothing here evaluates, changes a mode, writes or calls a broker.

GET /api/decision-flow                                   the latest evaluation cycle: summary, one row per ticker, Closest to READY, universe status
GET /api/decision-flow/history?date=YYYY-MM-DD&limit=N   one New York trading date: day counts and the cycle list (newest first)
GET /api/decision-flow/cycle?at=ISO                      one cycle reconstructed: the scheduler batch at or before `at`
GET /api/decision-flow/explain?symbol&date&time&tz       why a ticker did (not) trade at a time (tz ET, PT or UTC)
GET /api/decision-flow/universe                          the inactive dynamic-universe plan

Route words: no path may contain a broker-action word (tests/test_regression_isolation.py). Malformed input is 400 with a short message; a
database that cannot be read is 503. Every payload carries broker_calls 0.
"""
import re
import sqlite3
from datetime import date as Date

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

from api.validation import validate_symbol
from decision_flow import view as V

router = APIRouter(prefix="/api/decision-flow", tags=["decision-flow"])
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIME = re.compile(r"^\d{2}:\d{2}(:\d{2})?$")
TZS = ("ET", "PT", "UTC")
OUT_OF_RANGE = "the time is out of range"


def _bad(message: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"status": "INVALID", "message": message}, status_code=status)


def _unreadable() -> JSONResponse:
    return JSONResponse({"status": "UNAVAILABLE", "message": "the database could not be read; nothing was changed", "broker_calls": 0}, status_code=503)


def _date_ok(text: str) -> bool:
    if not _DATE.match(text):
        return False
    try:
        d = Date.fromisoformat(text)
    except ValueError:
        return False
    return V.YEARS[0] <= d.year <= V.YEARS[1]                                  # year 1 or 9999 would overflow the time arithmetic


@router.get("")
def get_latest() -> JSONResponse:
    try:
        return JSONResponse(V.latest())
    except sqlite3.Error:
        return _unreadable()


@router.get("/history")
def get_history(date: str = Query(default=""), limit: str = Query(default=str(V.HISTORY_LIMIT))) -> JSONResponse:
    if date and not _date_ok(date):
        return _bad("date must be YYYY-MM-DD")
    try:
        n = int(limit)
    except ValueError:
        return _bad(f"limit must be an integer 1..{V.HISTORY_MAX}")
    if not 1 <= n <= V.HISTORY_MAX:
        return _bad(f"limit must be 1..{V.HISTORY_MAX}")
    try:
        return JSONResponse(V.history(date or None, n))
    except ValueError as exc:
        return _bad(str(exc))
    except OverflowError:
        return _bad(OUT_OF_RANGE)
    except sqlite3.Error:
        return _unreadable()


@router.get("/cycle")
def get_cycle(at: str = Query(default="")) -> JSONResponse:
    at = re.sub(r" (\d{2}:?\d{2})$", r"+\1", (at or "").strip())          # a '+00:00' offset sent unencoded arrives as ' 00:00'
    if not at:
        return _bad("at is required (an ISO 8601 time, e.g. 2026-10-27T19:59:03Z)")
    if len(at) > 40:
        return _bad("at must be an ISO 8601 time, e.g. 2026-10-27T19:59:03Z")
    try:
        return JSONResponse(V.cycle(at))
    except ValueError as exc:
        return _bad(str(exc))
    except OverflowError:
        return _bad(OUT_OF_RANGE)
    except sqlite3.Error:
        return _unreadable()


@router.get("/explain")
def get_explain(symbol: str = Query(default=""), date: str = Query(default=""), time: str = Query(default=""), tz: str = Query(default="ET")) -> JSONResponse:
    try:
        sym = validate_symbol(symbol)
    except ValueError:
        return _bad("symbol must be a ticker symbol (letters, '.' or '-', at most 10)")
    if date and not _date_ok(date):
        return _bad("date must be YYYY-MM-DD")
    if not _TIME.match(time or ""):
        return _bad("time must be HH:MM or HH:MM:SS")
    zone = (tz or "").strip().upper()
    if zone not in TZS:
        return _bad("tz must be ET, PT or UTC")
    try:
        return JSONResponse(V.explain(sym, date or None, time, zone))
    except ValueError as exc:
        return _bad(str(exc))
    except OverflowError:
        return _bad(OUT_OF_RANGE)
    except sqlite3.Error:
        return _unreadable()


@router.get("/universe")
def get_universe() -> JSONResponse:
    return JSONResponse(V.universe())
