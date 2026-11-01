"""
Stage 6.10C - the agent Decision Flow (decision_flow/, api/routes/decision_flow.py; DESIGN_610C_DECISION_FLOW.md sections 3-7): a read-only
projection of the rows the setup evaluator, the risk gate, Paper Auto and the executor already persist.

D  the latest cycle: every evaluated ticker explained from its stored row and the engine's own predicates (WAIT / WATCHING / SKIPPED / BLOCKED /
   READY), Not available where the engine computes nothing, the decision map, the Closest to READY ordering
H  the decision history of one trading date (cycles, off-cycle evaluations, day counts)
X  explain a ticker at a time (ET / PT / UTC) and reconstruct a past cycle with its executor window
R  the read paths never write, the routes are GET only and validate their input, no secret or host reaches a payload, and the bisection keeps
   the views fast on a large evaluation table
F  freeze (6.10C changes exactly its declared files; no execution, strategy or policy change)
"""
import hashlib
import json
import re
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import decision_flow as DF  # noqa: E402
import freeze_ledger as FL  # noqa: E402
from decision_flow import conditions as C  # noqa: E402
from decision_flow import view as V  # noqa: E402
from decision_flow.store import DecisionFlowStore  # noqa: E402
from test_cash_flow_602 import cash_config, make_rig, started  # noqa: E402,F401 - cash_config: the autouse config fixture of the shared rig
from test_setups_58 import READY_IN, WATCH_IN, fx  # noqa: E402

UTC = timezone.utc
DAY = "2026-10-21"                                                    # the rig's session: Wednesday, 11:00 New York (15:00Z) at the start
NONE_IN = dict(stretch=-3.0, confidence=50, demand=70)               # no active setup: only the stretch check fails
SYMS = ["NVDA", "AMD", "MU", "SNDK", "AVGO", "MRVL", "LLY", "ORCL", "SHOP", "CLS"]


# ================================================================ the rig ==================================================================
def rig(tmp_path, monkeypatch, name="s610c.db"):
    r = make_rig(tmp_path, monkeypatch, db_name=name)
    r.fv = r.s.forward_validation
    r.fv.clock = r.clock
    r.df = DecisionFlowStore(r.db)
    return r


def batch(r, **per_symbol):
    """One scheduler batch: several symbols evaluated at one clock time with trigger SCHEDULED, the shape the market-hours loop writes."""
    return {sym: r.setups.evaluate_symbol(sym, trigger="SCHEDULED", inputs=fx(sym, **over)) for sym, over in per_symbol.items()}


def ready_batch(r, symbols=("SNDK",), others=("NVDA",)):
    """WATCH then READY for `symbols` (one minute apart, as the loop would), with `others` evaluated to no setup in the same batches."""
    batch(r, **{s: WATCH_IN for s in symbols}, **{o: NONE_IN for o in others})
    r.clock.advance(60)
    batch(r, **{s: READY_IN for s in symbols}, **{o: NONE_IN for o in others})
    pvs = {}
    for s in symbols:
        pv = r.s.proposals.current(s, r.clock.now)
        assert pv.effective_status == "REVIEWABLE", (s, pv.effective_status, pv.blockers)
        r.sim.set_price(s, float(pv.reference_price))
        pvs[s] = pv
    return pvs


def cyc(r, advance=30):
    r.clock.advance(advance)
    return r.x.run_cycle(r.clock.now, "SCHEDULED")


def obs(r):
    return r.fv.observe(r.clock.now, "TEST")


def latest(r, now=None):
    return V.latest(now=now or r.clock.now, store=r.df, stream=r.s)


def history(r, date=DAY, limit=20):
    return V.history(date, limit=limit, now=r.clock.now, store=r.df)


def row(payload, sym):
    return next(x for x in payload["tickers"] if x["symbol"] == sym)


def check(ticker, cid):
    return next(k for k in ticker["checks"] if k["id"] == cid)


def db_state(db):
    """The schema and every row of every table (hashed), read only: the proof that a read path wrote nothing."""
    con = sqlite3.connect(f"file:{Path(db).as_posix()}?mode=ro", uri=True)
    try:
        schema = con.execute("SELECT type, name, sql FROM sqlite_master ORDER BY type, name").fetchall()
        rows = {}
        for (t,) in con.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name").fetchall():
            data = con.execute(f'SELECT * FROM "{t}"').fetchall()
            rows[t] = (len(data), hashlib.sha256(json.dumps(sorted(str(x) for x in data)).encode("utf-8")).hexdigest())
        return schema, rows
    finally:
        con.close()


# ================================================================ D the latest cycle =======================================================
def test_D01_the_latest_cycle_explains_every_evaluated_ticker_from_its_stored_row_and_agrees_with_the_engine(tmp_path, monkeypatch):
    r = rig(tmp_path, monkeypatch)
    started(r)
    batch(r, NVDA=NONE_IN, AMD=dict(stretch=-20.0, confidence=50, demand=50), SNDK=WATCH_IN)
    p = latest(r)
    c = p["cycle"]
    assert c["cycle_id"] == "SE-20261021T150000Z" and c["at"] == "2026-10-21T15:00:00+00:00" and c["at_et"] == "2026-10-21 11:00:00 ET"
    assert c["market"]["session"] == "REGULAR" and c["tickers_scanned"] == 3 and c["candidate_setups"] == 1 and c["stale"] is False
    assert c["counts"] == {"READY": 0, "SKIPPED": 0, "BLOCKED": 0, "WATCHING": 1, "WAIT": 2}
    assert c["paper_auto"]["on"] is False and c["risk"]["state"] == "TRADING_ALLOWED" and c["executor"]["runs"] == 0 and c["order_intents"] == 0
    assert c["outcome"]["code"] == "NO_TRADE" and c["executor"]["idle_note"]
    assert [x["symbol"] for x in p["tickers"]] == ["NVDA", "AMD", "SNDK"] and all(x["consistent"] is True for x in p["tickers"])
    n = row(p, "NVDA")
    assert n["decision"] == "WAIT" and n["setup"]["state"] == "NONE" and n["setup"]["state_label"] == "NO ACTIVE SETUP" and n["origin"] == "SCHEDULER_CYCLE"
    assert [k["id"] for k in n["checks"]] == ["STRETCH", "PPE_COVERAGE", "CONFIDENCE", "DEMAND_DATA", "DEMAND", "EVIDENCE"]
    assert n["failed"] == ["Downside stretch (PPE)"] and len(n["passed"]) == 5 and n["distance"]["unmet"] == 1 and n["distance"]["stretch_gap"] == 37.0
    assert check(n, "STRETCH")["threshold_text"] == "<= -40" and check(n, "DEMAND")["passed"] is True and check(n, "DEMAND")["gap"] == 0
    assert n["score"]["value"] is None and "Not available" in n["score"]["text"] and n["price"]["value"] is None
    assert n["risk"]["evaluated"] is False and "Not evaluated" in n["risk"]["text"] and n["risk"]["controls"] == []
    assert "no active setup" in n["no_trade_reason"].lower() and n["setup"]["name"] == "Downside Dislocation Recovery"
    s = row(p, "SNDK")
    assert s["decision"] == "WATCHING" and s["setup"]["state"] == "WATCH" and s["distance"]["unmet"] == 2 and set(s["failed"]) == {"PPE confidence", "Market Demand"}
    assert check(s, "CONFIDENCE")["gap"] == 6 and check(s, "DEMAND")["gap"] == 19 and s["distance"]["stretch_gap"] == 0
    assert [x["symbol"] for x in p["closest"]] == ["SNDK", "NVDA", "AMD"] and p["closest"][0]["rank"] == 1 and c["closest"]["symbol"] == "SNDK"
    assert p["broker_calls"] == 0 and p["read_only"] is True and p["off_cycle_evaluations"] == 0
    assert p["universe"]["available"] is True and p["universe"]["active"]["equals_live"] is True and p["universe"]["live"]["symbols"] == ["NVDA", "AMD", "SNDK"]
    assert "WATCH only" in json.dumps(p["legend"]) and DF.DISCLAIMER == p["disclaimer"]


def test_D02_a_ready_setup_while_paper_auto_is_off_is_skipped_and_says_so(tmp_path, monkeypatch):
    r = rig(tmp_path, monkeypatch)
    started(r)
    obs(r)
    ready_batch(r)
    cyc(r)                                                                             # Monitor only with nothing open: IDLE, nothing persisted
    p = latest(r)
    s = row(p, "SNDK")
    assert s["decision"] == "SKIPPED" and s["decision_detail"] == DF.PAPER_AUTO_OFF and s["setup"]["state"] == "READY"
    assert "Paper Auto" in s["no_trade_reason"] and s["distance"]["unmet"] == 0 and s["failed"] == [] and len(s["passed"]) == 6
    assert s["risk"]["evaluated"] is False and r.x.store.intents() == []
    c = p["cycle"]
    assert c["counts"]["SKIPPED"] == 1 and c["paper_auto"]["on"] is False and c["paper_auto"]["mode"] == "MONITOR_ONLY" and c["outcome"]["code"] == "NO_TRADE"
    assert c["executor"]["runs"] == 0 and "IDLE" in (c["executor"]["idle_note"] or "")
    d = history(r)["day"]
    assert d["ready_evaluations"] == 1 and d["ready_transitions"] == 1 and d["watch_transitions"] == 1 and d["skipped_paper_auto_off"]["count"] == 1


def test_D03_a_blocking_entry_control_reads_as_blocked_with_the_control_that_blocked_and_every_control_result(tmp_path, monkeypatch):
    r = rig(tmp_path, monkeypatch)
    started(r)
    obs(r)
    ready_batch(r)
    r.x.enable_auto(actor="test", now=r.clock.now)
    r.s.safety.emergency_stop(reason="test", now=r.clock.now)
    cyc(r)
    assert r.x.store.intents() == []
    obs(r)                                                                             # the observer records the executor's per-control results
    p = latest(r)
    s = row(p, "SNDK")
    assert s["decision"] == "BLOCKED" and s["decision_detail"] == DF.ENTRY_CONTROLS and s["risk"]["evaluated"] is True
    assert "SAFETY_GATE" in s["risk"]["blocked_by"] and "SAFETY_GATE" in s["no_trade_reason"] or "Risk gate" in s["no_trade_reason"]
    results = {x["id"]: x["result"] for x in s["risk"]["controls"]}
    assert results.get("SAFETY_GATE") == "BLOCK" and results.get("EXECUTION_MODE") == "PASS" and len(results) >= 10   # every control, not only the blocking ones
    c = p["cycle"]
    assert c["counts"]["BLOCKED"] == 1 and c["executor"]["runs"] == 1 and c["order_intents"] == 0 and c["paper_auto"]["on"] is True
    d = history(r)["day"]
    assert d["blocked"]["count"] == 1 and d["blocked"]["by_control"].get("SAFETY_GATE") == 1 and d["executor"]["with_candidates"] == 1


def test_D04_an_entry_intent_reads_as_ready_with_an_order_intent_and_the_entry_limit_as_skipped(tmp_path, monkeypatch):
    r = rig(tmp_path, monkeypatch)
    started(r)
    obs(r)
    ready_batch(r, symbols=("SNDK", "AMD"), others=("NVDA",))
    r.x.enable_auto(actor="test", now=r.clock.now)
    cyc(r)
    obs(r)
    its = r.x.store.intents()
    assert len(its) == 1                                                               # EXECUTION_MAX_ENTRIES_PER_CYCLE = 1
    sent = its[0].symbol
    other = "AMD" if sent == "SNDK" else "SNDK"
    p = latest(r)
    a, b = row(p, sent), row(p, other)
    assert a["decision"] == "READY" and a["decision_detail"] == DF.ORDER_INTENT and a["risk"]["evaluated"] is True and a["risk"]["blocked_by"] == []
    assert b["decision"] == "SKIPPED" and b["decision_detail"] == DF.ENTRIES_PER_CYCLE and "limit" in b["no_trade_reason"].lower()
    c = p["cycle"]
    assert c["counts"] == {"READY": 1, "SKIPPED": 1, "BLOCKED": 0, "WATCHING": 0, "WAIT": 1} and c["order_intents"] == 1 and c["orders_submitted"] == 1
    assert c["outcome"]["code"] == "ORDER_INTENT" and sent in c["outcome"]["text"]
    d = history(r)["day"]
    assert d["order_intents"] == 1 and d["skipped_entry_limit"] == 1 and d["executor"]["with_candidates"] == 1 and d["ready_evaluations"] == 2


def test_D05_a_paused_or_failed_evaluation_is_wait_with_no_check_claimed(tmp_path, monkeypatch):
    import setups.service as SS
    r = rig(tmp_path, monkeypatch)
    started(r)
    batch(r, NVDA=NONE_IN, AMD=dict(ppe_status="NOT_AVAILABLE", stretch_status="NOT_AVAILABLE", stretch=None))
    monkeypatch.setattr(SS, "evaluate", lambda prev, i: (_ for _ in ()).throw(RuntimeError("engine exploded")))
    with pytest.raises(RuntimeError):
        r.setups.evaluate_symbol("SNDK", trigger="SCHEDULED", inputs=fx("SNDK", **NONE_IN))   # recorded as ERROR, then re-raised to the caller
    p = latest(r)
    assert [x["symbol"] for x in p["tickers"]] == ["NVDA", "AMD", "SNDK"] and p["cycle"]["counts"]["WAIT"] == 3
    a = row(p, "AMD")
    assert a["decision"] == "WAIT" and a["decision_detail"] == DF.PAUSED and all(k["passed"] is None for k in a["checks"]) and a["distance"]["unmet"] is None
    assert "paused" in a["no_trade_reason"].lower() and a["setup"]["evaluation"] == "PAUSED" and a["failed"] == [] and a["passed"] == []
    e = row(p, "SNDK")
    assert e["decision"] == "WAIT" and e["decision_detail"] == DF.EVALUATION_ERROR and all(k["passed"] is None for k in e["checks"])
    assert e["setup"]["evaluation"] == "ERROR" and "RuntimeError" in e["no_trade_reason"] and "exploded" not in json.dumps(e)   # the type, never the message
    assert [x["symbol"] for x in p["closest"]] == ["NVDA", "AMD", "SNDK"]              # an unevaluated ticker never ranks ahead of an evaluated one


def test_D06_an_unavailable_confidence_is_not_available_never_below_40_and_shares_one_root_cause_with_missing_evidence(tmp_path, monkeypatch):
    r = rig(tmp_path, monkeypatch)
    started(r)
    live = dict(stretch=-3.6, confidence=None, confidence_status="EVIDENCE_NOT_AVAILABLE", demand=55, evidence_available=False, evidence_packet_id=None,
                evidence_created_at=None, evidence_age_days=None, evidence_cause=None, evidence_strength=None, company_confirm=None)   # every live row of 2026-10-27
    batch(r, NVDA=live, AMD=dict(live, stretch=11.4, demand=71))
    p = latest(r)
    n = row(p, "NVDA")
    conf, ev = check(n, "CONFIDENCE"), check(n, "EVIDENCE")
    assert conf["passed"] is False and conf["value"] is None and conf["value_text"].startswith("Not available") and "evidence packet" in conf["value_text"]
    assert conf["root_cause"] == ev["root_cause"] == DF.NO_EVIDENCE_PACKET and conf["gap"] is None and ev["passed"] is False
    assert "below 40" not in json.dumps(n) and n["consistent"] is True
    assert n["distance"] | {"unmet": 4, "unmet_root_causes": 3, "stretch_gap": 36.4, "demand_gap": 5, "confidence_gap": None} == n["distance"]
    a = row(p, "AMD")
    assert a["distance"]["unmet"] == 3 and a["distance"]["unmet_root_causes"] == 2 and a["distance"]["demand_gap"] == 0
    assert [x["symbol"] for x in p["closest"]] == ["AMD", "NVDA"] and p["closest"][0]["unmet_root_causes"] == 2


def test_D07_the_decision_map_is_total_and_the_executor_result_wins_for_a_ready_setup():
    d = lambda *a, **k: C.decide(*a, **k)["decision"]  # noqa: E731
    assert d("ERROR", "NONE", True) == "WAIT" and d("PAUSED", "READY", True) == "WAIT" and d("HOLD", "NONE", True) == "WAIT"
    assert d("HOLD", "INVALIDATED", True) == "WAIT" and d("HOLD", "WATCH", True) == "WATCHING" and d("TRANSITION", "WATCH", False) == "WATCHING"
    assert C.decide("HOLD", "READY", False) == {"decision": "SKIPPED", "decision_detail": DF.PAPER_AUTO_OFF, "decision_label": "SKIPPED · Paper Auto off"}
    assert C.decide("HOLD", "READY", True)["decision_detail"] == DF.AWAITING_EXECUTOR
    assert d("HOLD", "READY", True, {"result": "ENTRY_INTENT"}) == "READY" and d("HOLD", "READY", False, {"result": "ENTRY_INTENT"}) == "READY"   # what happened wins
    assert d("HOLD", "READY", True, {"result": "BLOCKED", "blocked_by": ["SAFETY_GATE"]}) == "BLOCKED"
    assert C.decide("HOLD", "READY", True, {"result": "BLOCKED", "blocked_by": ["DUPLICATE_ENTRY"]})["decision_detail"] == DF.DUPLICATE_ENTRY
    assert C.decide("HOLD", "READY", True, {"result": "SKIPPED"})["decision_detail"] == DF.ENTRIES_PER_CYCLE
    assert DF.DECISIONS == ("READY", "SKIPPED", "BLOCKED", "WATCHING", "WAIT")
    assert {DF.DECISION_BADGES[x] for x in DF.DECISIONS} == {"ok", "info", "bad", "warn", "dim"} and all(DF.DECISION_TEXT[x] for x in DF.DECISIONS)
    assert C.closest_key("WATCH", 3, 10.0, 5, "ZZ") < C.closest_key("NONE", 0, 0.0, 0, "AA")                     # state first
    assert C.closest_key("NONE", 1, 5.0, None, "B") < C.closest_key("NONE", 2, 0.0, 0, "A")                      # then fewest unmet
    assert C.closest_key("NONE", 1, 5.0, 0, "B") < C.closest_key("NONE", 1, 5.0, None, "A") < C.closest_key("NONE", 1, 5.0, None, "C")   # gaps, then symbol
    assert C.score() == {"value": None, "text": DF.SCORE_TEXT}


def test_D08_a_stream_row_landing_in_the_batch_second_is_counted_once(tmp_path, monkeypatch):
    r = rig(tmp_path, monkeypatch)
    started(r)
    batch(r, NVDA=NONE_IN, AMD=NONE_IN)
    r.setups.evaluate_symbol("AMD", trigger="SCHEDULED", inputs=fx("AMD", stretch=-20.0, confidence=50, demand=70))   # the Trader View stream, same second
    p = latest(r)
    assert p["cycle"]["tickers_scanned"] == 2 and [x["symbol"] for x in p["tickers"]] == ["NVDA", "AMD"] and p["off_cycle_evaluations"] == 1
    assert row(p, "AMD")["values"]["stretch"] == -3.0                                 # the batch row (the lower rowid), never the later stream row


# ================================================================ H history ================================================================
def test_H01_history_counts_the_cycles_and_off_cycle_evaluations_of_one_trading_date(tmp_path, monkeypatch):
    r = rig(tmp_path, monkeypatch)
    started(r)
    for _ in range(4):
        batch(r, NVDA=NONE_IN, AMD=NONE_IN)
        r.clock.advance(60)
    r.setups.evaluate_symbol("NVDA", trigger="SCHEDULED", inputs=fx("NVDA", **NONE_IN))   # a Trader View stream row: one symbol alone
    r.clock.advance(20)
    r.setups.evaluate_symbol("AMD", trigger="SUBSCRIBE", inputs=fx("AMD", **NONE_IN))
    r.clock.advance(20)
    r.setups.evaluate_symbol("AMD", trigger="TEST", inputs=fx("AMD", **NONE_IN))          # never evidence
    h = history(r, limit=2)
    d = h["day"]
    assert d["date"] == DAY and d["timezone"] == "America/New_York" and d["cycles"] == 4 and d["evaluations"] == 10 and d["off_cycle_evaluations"] == 2
    assert d["tickers"] == ["NVDA", "AMD"] and d["ready_transitions"] == 0 and d["ready_evaluations"] == 0 and d["blocked"]["count"] == 0
    assert d["closest"]["symbol"] == "AMD" and d["closest"]["unmet"] == 1 and d["paper_auto"] == {"spans": [], "minutes": 0}   # equal rows: the symbol decides
    assert d["executor"]["runs"] == 0 and d["order_intents"] == 0 and d["risk_states"]
    assert len(h["cycles"]) == 2 and h["has_more"] is True and h["cycles"][0]["at"] > h["cycles"][1]["at"] and h["latest_date"] == DAY
    assert [c["counts"]["WAIT"] for c in h["cycles"]] == [2, 2] and h["cycles"][0]["cycle_id"] == "SE-20261021T150300Z"
    c0 = h["cycles"][0]                                                                # a section 6.2 summary, the shape the ACTIVITY table reads
    assert c0["tickers_scanned"] == 2 and c0["candidate_setups"] == 0 and c0["market"]["session"] == "REGULAR" and c0["market"]["label"]
    assert c0["paper_auto"]["mode"] == "MONITOR_ONLY" and c0["paper_auto"]["on"] is False and c0["paper_auto"]["label"]
    assert c0["risk"]["state"] == "TRADING_ALLOWED" and c0["risk"]["allowed_new_entry"] is True and c0["risk"]["evaluated_at"] <= c0["at"]
    assert c0["executor"] == {"runs": 0, "results": {"RUN": 0, "ERROR": 0}} and c0["outcome"]["code"] == "NO_TRADE" and c0["outcome"]["text"]
    full = history(r, limit=50)
    assert len(full["cycles"]) == 4 and full["has_more"] is False and full["cycles"][-1]["cycle_id"] == "SE-20261021T150000Z"
    assert history(r, "2026-10-20")["day"]["cycles"] == 0 and history(r, None)["day"]["date"] == DAY
    for bad in (("2026-13-01", 5), ("2026-10-2", 5), (DAY, 0), (DAY, 401)):
        with pytest.raises(ValueError):
            V.history(bad[0], limit=bad[1], now=r.clock.now, store=r.df)


# ================================================================ X explain and reconstruct ================================================
def test_X01_explain_answers_why_a_ticker_did_not_trade_at_a_time_in_et_pt_or_utc(tmp_path, monkeypatch):
    r = rig(tmp_path, monkeypatch)
    started(r)
    obs(r)
    ready_batch(r)                                                                     # 11:00 WATCH, 11:01 READY, Monitor only
    e = V.explain("SNDK", DAY, "11:02", "ET", now=r.clock.now, store=r.df)
    assert e["found"] is True and e["query"]["at"] == "2026-10-21T15:02:00+00:00" and e["query"]["at_et"] == "2026-10-21 11:02:00 ET"
    assert e["evaluation"]["evaluated_at"] == "2026-10-21T15:01:00+00:00" and e["evaluation"]["decision"] == "SKIPPED"
    assert e["context"]["paper_auto"]["on"] is False and e["context"]["market"]["session"] == "REGULAR" and e["context"]["risk"]["state"] == "TRADING_ALLOWED"
    assert "SNDK" in e["answer"] and "Paper Auto" in e["answer"] and e["broker_calls"] == 0
    assert e["asked"] == {"symbol": "SNDK", "date": DAY, "time": "11:02", "tz": "ET", "utc": "2026-10-21T15:02:00+00:00", "et": "2026-10-21 11:02:00 ET"}   # section 6's names
    assert e["context"]["session"]["session"] == "REGULAR" and e["context"]["mode"] == "MONITOR_ONLY" and e["context"]["executor"]["text"] in e["answer"]
    for t, z in (("08:02", "PT"), ("15:02", "UTC"), ("11:02:00", "ET")):
        x = V.explain("sndk", DAY, t, z, now=r.clock.now, store=r.df)
        assert x["query"]["at"] == e["query"]["at"] and x["evaluation"]["evaluated_at"] == e["evaluation"]["evaluated_at"], (t, z)
    none = V.explain("SNDK", DAY, "10:30", "ET", now=r.clock.now, store=r.df)
    assert none["found"] is False and none["evaluation"] is None and none["answer"]
    late = V.explain("SNDK", DAY, "11:30", "ET", now=r.clock.now, store=r.df)          # 29 min after the last evaluation: beyond the 10-minute window
    assert late["found"] is False and late["nearest_before"] and late["nearest_before"]["evaluated_at"] == "2026-10-21T15:01:00+00:00"
    assert V.explain("NVDA", DAY, "11:02", "ET", now=r.clock.now, store=r.df)["evaluation"]["decision"] == "WAIT"
    assert V.explain("SNDK", None, "11:02", "ET", now=r.clock.now, store=r.df)["query"]["date"] == DAY   # no date: the date of `now` in New York


def test_X02_a_past_cycle_is_reconstructed_with_the_executor_runs_of_its_window(tmp_path, monkeypatch):
    r = rig(tmp_path, monkeypatch)
    started(r)
    obs(r)
    ready_batch(r)
    r.x.enable_auto(actor="test", now=r.clock.now)
    cyc(r)                                                                             # 15:01:30
    cyc(r)                                                                             # 15:02:00
    obs(r)
    r.clock.advance(60)
    batch(r, NVDA=NONE_IN, SNDK=READY_IN)                                              # 15:03:00: the next cycle closes the window
    c = V.cycle("2026-10-21T15:02:30+00:00", now=r.clock.now, store=r.df)
    assert c["cycle"]["cycle_id"] == "SE-20261021T150100Z" and c["cycle"]["next_at"] == "2026-10-21T15:03:00+00:00" and c["cycle"]["executor"]["runs"] == 2
    assert c["cycle"]["executor"]["window"] == {"from": "2026-10-21T15:01:00+00:00", "to": "2026-10-21T15:03:00+00:00"}
    assert c["requested_at"] == "2026-10-21T15:02:30+00:00" and row(c, "SNDK")["decision"] == "READY" and row(c, "SNDK")["decision_detail"] == DF.ORDER_INTENT
    assert row(c, "SNDK")["price"]["value"] is None and "does not record prices" in row(c, "SNDK")["price"]["text"]
    assert c["cycle"]["order_intents"] == 1 and c["cycle"]["paper_auto"]["on"] is True
    assert V.cycle("2026-10-21T15:02:30Z", now=r.clock.now, store=r.df)["cycle"]["cycle_id"] == "SE-20261021T150100Z"
    before = V.cycle("2026-10-21T14:00:00+00:00", now=r.clock.now, store=r.df)
    assert before["cycle"]["at"] is None and before["tickers"] == [] and before["cycle"]["outcome"]["code"] == "NO_CYCLE"
    with pytest.raises(ValueError):
        V.cycle("not a time", now=r.clock.now, store=r.df)


# ================================================================ V the review's cases ======================================================
def test_V01_a_paused_or_failed_ready_evaluation_shows_what_the_executor_did_and_a_held_ready_names_its_entry_checks():
    # a PAUSED / ERROR row keeps its stored READY state and the executor still reads that open episode: what it did wins over the pause
    assert C.decide("PAUSED", "READY", True, {"result": "ENTRY_INTENT"})["decision_detail"] == DF.ORDER_INTENT
    assert C.decide("ERROR", "READY", True, {"result": "BLOCKED", "blocked_by": ["SAFETY_GATE"]})["decision"] == "BLOCKED"
    assert C.decide("PAUSED", "READY", True, {"result": "SKIPPED"})["decision_detail"] == DF.ENTRIES_PER_CYCLE
    assert C.decide("PAUSED", "READY", True)["decision_detail"] == DF.PAUSED and C.decide("ERROR", "READY", False)["decision_detail"] == DF.EVALUATION_ERROR
    assert C.decide("PAUSED", "WATCH", True, {"result": "ENTRY_INTENT"})["decision_detail"] == DF.PAUSED   # only a READY episode is the executor's
    # READY held by the engine's hysteresis: the unmet checks are the WATCH -> READY entry checks, and the exit thresholds are stated
    i = fx("SNDK", **dict(READY_IN, stretch=-30.0, demand=50))
    cs, codes = C.checks(i)
    held, plain = C.distance(cs, held=True)["text"], C.distance(cs)["text"]
    assert held.startswith("READY held: 2 of 6 entry checks (WATCH to READY) not met now") and held.endswith(C.HOLD_TEXT) and "READY checks unmet" not in held
    assert plain.startswith("2 of 6 READY checks unmet") and "-20" in C.HOLD_TEXT and "45" in C.HOLD_TEXT
    raw = {"symbol": "SNDK", "evaluated_at": "2026-10-21T15:05:00+00:00", "decision": "HOLD", "inputs_json": json.dumps(i.model_dump(mode="json")),
           "freshness_json": json.dumps({"blockers": codes})}
    assert C.explain({**raw, "resulting_state": "READY"})["distance"]["text"] == held and C.explain({**raw, "resulting_state": "WATCH"})["distance"]["text"] == plain


def test_V02_out_of_range_dates_are_400_never_500_and_a_cycle_before_any_batch_reads_the_context_of_its_own_time(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from api.server import app
    r = rig(tmp_path, monkeypatch)
    started(r)
    obs(r)
    ready_batch(r)
    r.x.enable_auto(actor="test", now=r.clock.now)                                     # Paper Auto on from 15:01Z: after the requested 14:00Z
    cyc(r)
    before = V.cycle("2026-10-21T14:00:00+00:00", now=r.clock.now, store=r.df)["cycle"]
    assert before["at"] is None and before["outcome"]["code"] == "NO_CYCLE" and "at or before the requested time" in before["outcome"]["text"]
    assert before["paper_auto"]["on"] is False and before["risk"]["evaluated_at"] is None   # 14:00Z, not now (Paper Auto on, a gate read since)
    assert before["market"]["trading_date"] == DAY
    client = TestClient(app)
    for bad in ("/api/decision-flow/history?date=9999-12-31", "/api/decision-flow/history?date=0001-01-01",
                "/api/decision-flow/explain?symbol=MU&date=9999-12-31&time=23:59&tz=ET", "/api/decision-flow/explain?symbol=MU&date=0001-01-01&time=00:00&tz=UTC",
                "/api/decision-flow/cycle?at=0001-01-01T00:00:00%2B05:00", "/api/decision-flow/cycle?at=9999-12-31T23:59:59-05:00"):
        res = client.get(bad)
        assert res.status_code == 400, (bad, res.status_code, res.text[:200])
    for bad in (lambda: V.history("9999-12-31", now=r.clock.now, store=r.df), lambda: V.cycle("9999-12-31T23:59:59-05:00", now=r.clock.now, store=r.df),
                lambda: V.explain("MU", "0001-01-01", "00:00", "UTC", now=r.clock.now, store=r.df)):
        with pytest.raises(ValueError):
            bad()


def test_V03_history_counts_one_decision_per_ticker_per_cycle_marks_a_running_span_open_and_excludes_test_transitions(tmp_path, monkeypatch):
    r = rig(tmp_path, monkeypatch)
    started(r)
    obs(r)
    ready_batch(r)
    r.x.enable_auto(actor="test", now=r.clock.now)
    r.s.safety.emergency_stop(reason="test", now=r.clock.now)
    cyc(r)
    cyc(r)                                                                             # two executor runs in one cycle window, both blocked
    d = history(r)["day"]
    assert d["executor"]["runs"] == 2 and d["blocked"]["count"] == 1 and d["blocked"]["by_control"].get("SAFETY_GATE") == 1   # one per ticker per cycle
    assert d["blocked"]["basis"] == V.BLOCKED_BASIS and d["blocked"]["labels"]["SAFETY_GATE"]
    span = d["paper_auto"]["spans"][-1]
    assert span["open"] is True and span["to"] == r.clock.now.astimezone(UTC).isoformat(timespec="seconds")   # still armed: open, ending at the read time
    assert V._spans([("2026-10-21T15:00:00+00:00", "PAPER_AUTO"), ("2026-10-21T15:10:00+00:00", "MONITOR_ONLY")], "2026-10-21T04:00:00+00:00",
                    "2026-10-21T16:00:00+00:00", open_at="2026-10-21T16:00:00+00:00")[0] == [
        {"from": "2026-10-21T15:00:00+00:00", "to": "2026-10-21T15:10:00+00:00", "open": False, "from_et": "2026-10-21 11:00:00 ET", "to_et": "2026-10-21 11:10:00 ET"}]
    ready_before = d["ready_transitions"]
    r.setups.evaluate_symbol("AMD", trigger="TEST", inputs=fx("AMD", **WATCH_IN))      # a TEST transition: never evidence
    assert history(r)["day"]["watch_transitions"] == d["watch_transitions"] and history(r)["day"]["ready_transitions"] == ready_before


def test_V04_the_explain_answer_says_each_fact_once_a_duplicate_batch_row_is_still_the_cycle_ticker_and_no_read_creates_a_database(tmp_path, monkeypatch):
    from database import database as dbm
    from decision_flow import store as ST
    r = rig(tmp_path, monkeypatch)
    started(r)
    batch(r, NVDA=NONE_IN, AMD=NONE_IN)
    r.setups.evaluate_symbol("AMD", trigger="SCHEDULED", inputs=fx("AMD", **NONE_IN))   # the same symbol again in the batch second (the stream)
    e = V.explain("AMD", DAY, "11:00:30", "ET", now=r.clock.now, store=r.df)
    reason = e["evaluation"]["setup"]["reason_text"]
    assert e["evaluation"]["origin"] == "SCHEDULER_CYCLE" and e["evaluation"]["cycle_id"] == "SE-20261021T150000Z"   # the higher rowid, still the cycle's ticker
    assert e["answer"].count(reason) == 1 and e["answer"].count(e["evaluation"]["distance"]["text"]) == 1 and e["answer"].endswith("No trade: no active setup.")
    assert "Market session: Regular session." in e["answer"]
    closed = V.explain("AMD", DAY, "22:30", "ET", now=r.clock.now, store=r.df)
    assert "The market was closed." in closed["answer"] and "Market: Market" not in closed["answer"]
    nvda = row(latest(r), "NVDA")
    assert nvda["setup"]["reason_reworded"] is False and nvda["decision_label"] == "WAIT · no active setup"
    monkeypatch.setattr(dbm, "_db_instance", None)
    target = tmp_path / "nowhere" / "stock_agent.db"
    import config
    monkeypatch.setattr(config, "RESEARCH_DB_PATH", target)
    assert ST.default_path() == target and not target.parent.exists()                 # resolved, never created, opened or migrated
    assert V.history(DAY, now=r.clock.now, store=DecisionFlowStore())["day"]["cycles"] == 0 and not target.parent.exists()


# ================================================================ R read only, GET only, private, fast =====================================
def test_R01_every_read_path_leaves_the_database_untouched_serves_get_only_validates_its_input_and_leaks_no_secret(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    import api.routes.trader_view as RT
    from api.server import app
    r = rig(tmp_path, monkeypatch)
    started(r)
    obs(r)
    ready_batch(r)
    r.x.enable_auto(actor="test", now=r.clock.now)
    cyc(r)
    obs(r)
    before = db_state(r.db)
    payloads = [latest(r), history(r), V.cycle("2026-10-21T15:05:00+00:00", now=r.clock.now, store=r.df),
                V.explain("SNDK", DAY, "11:02", "ET", now=r.clock.now, store=r.df), V.universe(now=r.clock.now, stream=r.s)]
    monkeypatch.setitem(RT.STREAM, "instance", r.s)
    client = TestClient(app)
    for path in ("/api/decision-flow", f"/api/decision-flow/history?date={DAY}&limit=3", "/api/decision-flow/cycle?at=2026-10-21T15:02:00Z",
                 f"/api/decision-flow/explain?symbol=SNDK&date={DAY}&time=11:02&tz=ET", "/api/decision-flow/universe"):
        res = client.get(path)
        assert res.status_code == 200, (path, res.text[:300])
        payloads.append(res.json())
        bare = path.split("?")[0]
        assert client.post(bare).status_code == 405 and client.delete(bare).status_code == 405 and client.put(bare).status_code == 405
    for bad in ("/api/decision-flow/history?date=2026-13-40", "/api/decision-flow/history?date=2026-10-2", "/api/decision-flow/history?limit=0",
                "/api/decision-flow/history?limit=401", "/api/decision-flow/history?limit=x", "/api/decision-flow/cycle", "/api/decision-flow/cycle?at=nope",
                "/api/decision-flow/explain?symbol=SNDK&time=25:00", "/api/decision-flow/explain?symbol=SNDK&time=11:02&tz=CET",
                "/api/decision-flow/explain?symbol=bad%20sym&time=11:02", "/api/decision-flow/explain?symbol=SNDK"):
        assert client.get(bad).status_code == 400, bad
    paths = {p: set(ops) for p, ops in app.openapi()["paths"].items() if p.startswith("/api/decision-flow")}
    assert paths == {p: {"get"} for p in ("/api/decision-flow", "/api/decision-flow/history", "/api/decision-flow/cycle", "/api/decision-flow/explain",
                                          "/api/decision-flow/universe")}
    assert db_state(r.db) == before
    blob = json.dumps(payloads, default=str)
    assert not re.search(r"paper-api|alpaca\.markets|Bearer |account_number|APCA-API|environment_json|(?<![0-9a-fA-F])\d{9,}(?![0-9a-fA-F])", blob)
    assert all(p.get("broker_calls") == 0 for p in payloads if "broker_calls" in p) and payloads[5]["cycle"]["cycle_id"] == payloads[0]["cycle"]["cycle_id"]
    assert payloads[5]["universe"]["live"]["symbols"] == ["NVDA", "AMD", "SNDK"]        # the existing stream's watchlist, read without building one
    names, pairs, strings = set(), set(), []
    for rel in ("decision_flow/__init__.py", "decision_flow/conditions.py", "decision_flow/store.py", "decision_flow/view.py", "api/routes/decision_flow.py"):
        n, p, s = used_names((ROOT / rel).read_text(encoding="utf-8"))
        names |= n
        pairs |= p
        strings += s
    banned = {"post_order", "insert_intent", "update_intent", "enable_auto", "disable_auto", "set_mode", "executescript", "quote", "quotes", "requests", "urllib",
              "TradingClient", "robin_stocks", "subprocess", "run_cycle", "evaluate_symbol", "refresh"}
    assert not names & banned and "safety.current" not in pairs                        # the code (strings and comments aside) reads: it never evaluates or writes
    sql = [s for s in strings if re.search(r"\bSELECT\b|\bFROM\b", s)]
    assert not [s for s in strings if re.match(r"\s*(INSERT|UPDATE|DELETE|CREATE|DROP|ALTER|REPLACE)\s", s)] and not [s for s in sql if "environment_json" in s]
    assert any("mode=ro" in s for s in strings)


def used_names(src):
    """What the code actually uses: imported modules, names, attributes, 'name.attr' pairs - and its string constants (SQL lives there)."""
    import ast
    names, pairs, strings = set(), set(), []
    for n in ast.walk(ast.parse(src)):
        if isinstance(n, ast.Import):
            names.update(a.name for a in n.names)
        elif isinstance(n, ast.ImportFrom):
            names.add(n.module or "")
            names.update(a.name for a in n.names)
        elif isinstance(n, ast.Name):
            names.add(n.id)
        elif isinstance(n, ast.Attribute):
            names.add(n.attr)
            if isinstance(n.value, ast.Name):
                pairs.add(f"{n.value.id}.{n.attr}")
        elif isinstance(n, ast.Constant) and isinstance(n.value, str):
            strings.append(n.value)
    return names, pairs, strings


def test_R02_the_latest_cycle_a_day_of_history_and_an_explanation_stay_fast_on_a_large_evaluation_table(tmp_path, monkeypatch):
    r = rig(tmp_path, monkeypatch)
    session = r.clock.now
    r.clock.now = datetime(2026, 10, 13, 15, 0, tzinfo=UTC)                           # one evaluation a week earlier creates the table (rowid = time order)
    r.setups.evaluate_symbol("NVDA", trigger="SCHEDULED", inputs=fx("NVDA", **NONE_IN))
    r.clock.now = session
    started(r)
    base_in = fx("NVDA", **NONE_IN).model_dump()
    t0 = datetime(2026, 10, 14, 13, 30, tzinfo=UTC)
    rows = []
    for day in range(5):                                                               # 5 earlier days x 390 minutes x 10 symbols
        for m in range(390):
            at = (t0 + timedelta(days=day, minutes=m)).isoformat(timespec="seconds")
            for s in SYMS:
                rows.append((hashlib.sha256(f"{at}|{s}".encode("utf-8")).hexdigest()[:32], at, s, "DOWNSIDE_DISLOCATION_RECOVERY", None, "NONE", "NONE", "HOLD",
                             "NO_DOWNSIDE_STRETCH", "No downside stretch.", json.dumps({**base_in, "symbol": s, "evaluated_at": at}),
                             json.dumps({"blockers": ["NO_DOWNSIDE_STRETCH"], "paused": False}), "", "test", "SCHEDULED", "perf"))
    con = sqlite3.connect(str(r.db))
    con.executemany("INSERT INTO setup_evaluations (evaluation_id, evaluated_at, symbol, setup_type, setup_id, prior_state, resulting_state, decision, reason_code, "
                    "reason_text, inputs_json, freshness_json, source, mode, trigger, owner) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    con.commit()
    con.close()
    batch(r, NVDA=NONE_IN, AMD=NONE_IN)                                                # the rig's own batch, after 19,500 older rows
    t = time.perf_counter()
    p = latest(r)
    dt_latest = time.perf_counter() - t
    assert p["cycle"]["cycle_id"] == "SE-20261021T150000Z" and p["cycle"]["tickers_scanned"] == 2
    t = time.perf_counter()
    h = V.history("2026-10-16", limit=20, now=r.clock.now, store=r.df)
    dt_hist = time.perf_counter() - t
    assert h["day"]["cycles"] == 390 and h["day"]["evaluations"] == 3900 and len(h["cycles"]) == 20 and h["has_more"] is True
    t = time.perf_counter()
    e = V.explain("MU", "2026-10-16", "10:32", "ET", now=r.clock.now, store=r.df)
    dt_ex = time.perf_counter() - t
    assert e["found"] is True and e["evaluation"]["evaluated_at"] == "2026-10-16T14:32:00+00:00"
    assert dt_latest < 2.0 and dt_hist < 3.0 and dt_ex < 1.0, (dt_latest, dt_hist, dt_ex)   # rowid bisection: never a scan of the whole table


def test_R03_when_today_has_no_cycle_the_most_recent_one_is_shown_as_stale_and_an_empty_database_reads_as_no_cycle(tmp_path, monkeypatch):
    r = rig(tmp_path, monkeypatch)
    started(r)
    batch(r, NVDA=NONE_IN, AMD=NONE_IN)
    p = latest(r, now=r.clock.now + timedelta(days=1))
    assert p["cycle"]["stale"] is True and p["cycle"]["cycle_id"] == "SE-20261021T150000Z" and p["cycle"]["age_s"] == 86400
    empty = V.latest(now=r.clock.now, store=DecisionFlowStore(tmp_path / "missing.db"), stream=r.s)
    assert empty["cycle"]["at"] is None and empty["tickers"] == [] and empty["closest"] == [] and empty["cycle"]["outcome"]["code"] == "NO_CYCLE"
    assert V.history(DAY, limit=5, now=r.clock.now, store=DecisionFlowStore(tmp_path / "missing.db"))["day"]["cycles"] == 0


# ---- freeze (F01-F06; added after the bookkeeping) ----
REPLAY_610C = {
    'tests/test_bot_activity_68.py': [
        ('    new = {p for p in changed if p not in in_base}\n    assert new == FL.NEW_68 | FL.NEW_69 | FL.NEW_691 | FL.NEW_610A, {"unexpected_new": sorted(new - FL.NEW_68 - FL.NEW_69 - FL.NEW_691 - FL.NEW_610A),\n                                                                     "missing_new": sorted((FL.NEW_68 | FL.NEW_69 | FL.NEW_691 | FL.NEW_610A) - new)}   # + Stage 6.9 / 6.9.1 / 6.10A\n',
         '    new = {p for p in changed if p not in in_base}\n    assert new == FL.NEW_68 | FL.NEW_69 | FL.NEW_691 | FL.NEW_610A | FL.NEW_610C, {"unexpected_new": sorted(new - FL.NEW_68 - FL.NEW_69 - FL.NEW_691 - FL.NEW_610A - FL.NEW_610C),\n                                                                     "missing_new": sorted((FL.NEW_68 | FL.NEW_69 | FL.NEW_691 | FL.NEW_610A | FL.NEW_610C) - new)}   # + Stage 6.9 / 6.9.1 / 6.10A / 6.10C\n'),
    ],
    'tests/test_robinhood_portfolio_69.py': [
        ('    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    later = FL.NEW_691 | FL.EDITED_691 | FL.NEW_610A | FL.EDITED_610A                                 # Stage 6.9.1 (FL.SEMANTIC_691) and Stage 6.10A (FL.SEMANTIC_610A),\n',
         '    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    later = FL.NEW_691 | FL.EDITED_691 | FL.NEW_610A | FL.EDITED_610A | FL.NEW_610C | FL.EDITED_610C   # Stage 6.9.1 (FL.SEMANTIC_691), Stage 6.10A (FL.SEMANTIC_610A), Stage 6.10C,\n'),
    ],
    'tests/test_unified_exposure_gate_691.py': [
        ('    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    later = FL.NEW_610A | FL.EDITED_610A                                                       # Stage 6.10A, pinned by tests/test_forward_paper_610a.py\n',
         '    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    later = FL.NEW_610A | FL.EDITED_610A | FL.NEW_610C | FL.EDITED_610C                       # Stage 6.10A / 6.10C, each pinned by its own F tests\n'),
    ],
    'tests/test_forward_paper_610a.py': [
        ('\n',
         '\ndef post(rel):\n    """main after PR #34 (Stage 6.10A merged): what Stage 6.10A left, whatever a later stage changed since."""\n    out = subprocess.run(["git", "show", f"{FL.BASE_610C}:{rel}"], cwd=ROOT, capture_output=True).stdout\n    assert out, f"{rel} is not in {FL.BASE_610C} (fetch-depth 0 is required)"\n    return out.decode("utf-8").replace("\\r\\n", "\\n")\n\n\n'),
        ('    assert FL.BASE_610A == "27926f627c5cb83a6e1b3b6a9b646ce455cfbe52" and subprocess.run(["git", "cat-file", "-e", f"{FL.BASE_610A}^{{commit}}"], cwd=ROOT).returncode == 0\n',
         '    assert FL.BASE_610A == "27926f627c5cb83a6e1b3b6a9b646ce455cfbe52" and subprocess.run(["git", "cat-file", "-e", f"{FL.BASE_610A}^{{commit}}"], cwd=ROOT).returncode == 0\n    stage610a = set(_git("diff", "--name-only", FL.BASE_610A, FL.BASE_610C).split())          # Stage 6.10A itself, between two fixed commits (main before / after PR #34)\n    in_base = set(_git("ls-tree", "-r", "--name-only", FL.BASE_610A).split())\n    new, edited = {p for p in stage610a if p not in in_base}, {p for p in stage610a if p in in_base}\n    assert new == FL.NEW_610A, {"unexpected_new": sorted(new - FL.NEW_610A), "missing_new": sorted(FL.NEW_610A - new)}\n    assert edited == FL.EDITED_610A, {"unexpected": sorted(edited - FL.EDITED_610A), "missing": sorted(FL.EDITED_610A - edited)}\n    assert {p for p in stage610a if p.startswith("execution/")} == {"execution/service.py", "execution/activity.py"}          # never engine / broker / store / scheduler\n    assert {p for p in stage610a if p.startswith("database/")} == {"database/autorun_migrations.py", "database/forward_capture_migrations.py"}\n    assert {p for p in stage610a if p.startswith("scripts/")} == {"scripts/install_stock_agent_task.ps1"}\n'),
        ('    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    in_base = set(_git("ls-tree", "-r", "--name-only", FL.BASE_610A).split())\n    new, edited = {p for p in changed if p not in in_base}, {p for p in changed if p in in_base}\n    assert new == FL.NEW_610A, {"unexpected_new": sorted(new - FL.NEW_610A), "missing_new": sorted(FL.NEW_610A - new)}\n    assert edited == FL.EDITED_610A, {"unexpected": sorted(edited - FL.EDITED_610A), "missing": sorted(FL.EDITED_610A - edited)}\n    assert {p for p in changed if p.startswith("execution/")} == {"execution/service.py", "execution/activity.py"}          # never engine / broker / store / scheduler\n    assert {p for p in changed if p.startswith("database/")} == {"database/autorun_migrations.py", "database/forward_capture_migrations.py"}\n    assert {p for p in changed if p.startswith("scripts/")} == {"scripts/install_stock_agent_task.ps1"}\n',
         '    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    later = FL.NEW_610C | FL.EDITED_610C                                                       # Stage 6.10C, pinned by its own F tests\n    assert changed == FL.NEW_610A | FL.EDITED_610A | later, {"unexpected": sorted(changed - FL.NEW_610A - FL.EDITED_610A - later),\n                                                              "missing": sorted((FL.NEW_610A | FL.EDITED_610A | later) - changed)}\n'),
        ('              "cash_flow", "paper", "portfolio", "rh_gateway", "trader_view", "unified_portfolio", "forward", "ppe", "demand", "evidence"):\n        assert not [p for p in changed if p.startswith(d + "/")], d                          # no strategy, risk, broker, market-data or Robinhood file\n',
         '              "cash_flow", "paper", "portfolio", "rh_gateway", "trader_view", "unified_portfolio", "forward", "ppe", "demand", "evidence"):\n        assert not [p for p in stage610a if p.startswith(d + "/")], d                        # no strategy, risk, broker, market-data or Robinhood file\n        assert not [p for p in changed - later if p.startswith(d + "/")], d                  # ... and none since but Stage 6.10C\'s declared files\n'),
        ('def test_F02_the_order_decision_logic_is_unchanged_and_the_executor_only_gained_failure_swallowing_markers():\n    assert now_text("execution/engine.py") == at_base("execution/engine.py")             # entry controls, exits, sizing: byte-identical\n    svc, base = now_text("execution/service.py"), at_base("execution/service.py")\n',
         'def test_F02_the_order_decision_logic_is_unchanged_and_the_executor_only_gained_failure_swallowing_markers():\n    assert post("execution/engine.py") == at_base("execution/engine.py")             # entry controls, exits, sizing: byte-identical\n    svc, base = post("execution/service.py"), at_base("execution/service.py")\n'),
        ('    assert X.MODES == ("MONITOR_ONLY", "PAPER_AUTO")\n    cfg_base, cfg_now = at_base("config.py"), now_text("config.py")\n',
         '    assert X.MODES == ("MONITOR_ONLY", "PAPER_AUTO")\n    cfg_base, cfg_now = at_base("config.py"), post("config.py")\n'),
        ('    assert [l.split(":")[0] for l in cfg_now[len(cfg_base):].splitlines() if l and not l.startswith("#")] == CONFIG_610A\n    srv_base, srv_now = at_base("api/server.py").splitlines(), now_text("api/server.py").splitlines()\n',
         '    assert [l.split(":")[0] for l in cfg_now[len(cfg_base):].splitlines() if l and not l.startswith("#")] == CONFIG_610A\n    srv_base, srv_now = at_base("api/server.py").splitlines(), post("api/server.py").splitlines()\n'),
        ('    assert [l for l in srv_base if l not in srv_now] == [] and [l for l in srv_now if l not in srv_base] == SERVER_LINES_610A\n    srv = now_text("api/server.py")\n',
         '    assert [l for l in srv_base if l not in srv_now] == [] and [l for l in srv_now if l not in srv_base] == SERVER_LINES_610A\n    srv = post("api/server.py")\n'),
        ('    assert life.index("yield") < life.index("_autorun_shutdown()") < life.index("_fv_shutdown()")   # the supervisor stops first\n',
         '    assert life.index("yield") < life.index("_autorun_shutdown()") < life.index("_fv_shutdown()")   # the supervisor stops first\n    for rel in ("execution/engine.py", "execution/service.py", "config.py", "api/server.py"):\n        assert not [l for l in post(rel).splitlines() if l not in set(now_text(rel).splitlines())], rel   # a later stage keeps every line Stage 6.10A left\n'),
        ('    for rel, edits in REPLAY_610A.items():\n        text = now_text(rel)\n',
         '    for rel, edits in REPLAY_610A.items():\n        text = post(rel)\n'),
        ('        assert text == at_base(rel), rel\n    fl, base = now_text("tests/freeze_ledger.py"), at_base("tests/freeze_ledger.py")\n',
         '        assert text == at_base(rel), rel\n    fl, base = post("tests/freeze_ledger.py"), at_base("tests/freeze_ledger.py")\n'),
        ('    old = ast.literal_eval(led(base)[len("LEDGER = "):])\n    assert set(FL.LEDGER) == set(old) | NEW_LEDGER_610A\n    assert {k for k in old if old[k] != FL.LEDGER[k]} == (FL.EDITED_610A & set(old)) - {"tests/freeze_ledger.py"}   # every re-hash is a declared Stage 6.10A edit\n',
         '    old = ast.literal_eval(led(base)[len("LEDGER = "):])\n    mine = ast.literal_eval(led(fl)[len("LEDGER = "):])                                   # the ledger Stage 6.10A left (main after PR #34)\n    assert set(mine) == set(old) | NEW_LEDGER_610A\n    assert {k for k in old if old[k] != mine[k]} == (FL.EDITED_610A & set(old)) - {"tests/freeze_ledger.py"}   # every re-hash is a declared Stage 6.10A edit\n'),
    ],
}
SERVER_LINES_610C = [
    'from api.routes import decision_flow  # Stage 6.10C: agent decision flow (GET only) - read-only cycles, READY checks and decision history from persisted rows; no write path',
    'app.include_router(decision_flow.router)  # Stage 6.10C: agent decision flow (GET only) - a read-only projection of persisted rows; no write path, no broker call',
]
CONFIG_610C = ['DYNAMIC_UNIVERSE_MODE', 'DYNAMIC_UNIVERSE_CORE_FILE', 'DYNAMIC_UNIVERSE_ACTIVE_MAX']
FROZEN_V1 = "b5d4a56ef5bba61f8250b3cd25182cc1655738adf0605a77671fd029883f8530"
CONTRACT_HASH_OFF = "2552bd6639f691b828624250665519cc90c4a00da7f70893dbb635f7551006fa"
CONTRACT_HASH_ON = "d2819342f6c8ccb1cd4a0ce2bfb5d1b0664f2453770c3e6736f7be420a17aed4"
STAGE_DIRS_NOT_TOUCHED = ("execution", "setups", "proposals", "safety", "trader_view", "forward_capture", "forward_validation", "autorun", "account_refresh",
                          "cash_flow", "paper", "portfolio", "unified_portfolio", "ppe", "demand", "evidence", "strategy_validation", "strategy_registry", "momentum",
                          "value_quality", "mean_reversion", "breakout", "scanner", "database", "scripts", "rh_gateway", "forward")


def _git(*a):
    return subprocess.run(["git", *a], cwd=ROOT, capture_output=True, text=True).stdout


def at_base(rel):
    out = subprocess.run(["git", "show", f"{FL.BASE_610C}:{rel}"], cwd=ROOT, capture_output=True).stdout
    assert out, f"{rel} is not in {FL.BASE_610C} (fetch-depth 0 is required)"
    return out.decode("utf-8").replace("\r\n", "\n")


def now_text(rel):
    return (ROOT / rel).read_text(encoding="utf-8").replace("\r\n", "\n")


def post(rel):
    """main after PR #35 (Stage 6.10C merged): what Stage 6.10C left, whatever a later stage changed since."""
    out = subprocess.run(["git", "show", f"{FL.BASE_541}:{rel}"], cwd=ROOT, capture_output=True).stdout
    assert out, f"{rel} is not in {FL.BASE_541} (fetch-depth 0 is required)"
    return out.decode("utf-8").replace("\r\n", "\n")


def test_F01_stage_610c_changes_exactly_its_declared_files_and_no_execution_strategy_risk_or_policy_file():
    assert FL.BASE_610C == "fed5b3cd3d602eccd71e473046ce58ad2f7819f4" and subprocess.run(["git", "cat-file", "-e", f"{FL.BASE_610C}^{{commit}}"], cwd=ROOT).returncode == 0
    stage610c = set(_git("diff", "--name-only", FL.BASE_610C, FL.BASE_541).split())          # Stage 6.10C itself, between two fixed commits (main before / after PR #35)
    in_base = set(_git("ls-tree", "-r", "--name-only", FL.BASE_610C).split())
    new, edited = {p for p in stage610c if p not in in_base}, {p for p in stage610c if p in in_base}
    assert new == FL.NEW_610C, {"unexpected_new": sorted(new - FL.NEW_610C), "missing_new": sorted(FL.NEW_610C - new)}
    assert edited == FL.EDITED_610C, {"unexpected": sorted(edited - FL.EDITED_610C), "missing": sorted(FL.EDITED_610C - edited)}
    changed = set(_git("diff", "--name-only", FL.BASE_610C).split()) | set(_git("ls-files", "--others", "--exclude-standard").split())
    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}
    later = FL.NEW_541 | FL.EDITED_541 | FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72                     # Stage 5.4.1 / the 6.1.1 CSS fix, each pinned by its own F tests
    assert changed == FL.NEW_610C | FL.EDITED_610C | later, {"unexpected": sorted(changed - FL.NEW_610C - FL.EDITED_610C - later),
                                                              "missing": sorted((FL.NEW_610C | FL.EDITED_610C | later) - changed)}
    for d in STAGE_DIRS_NOT_TOUCHED:
        assert not [p for p in stage610c if p.startswith(d + "/")], d                        # no execution, strategy, risk, broker, market-data or policy file
        assert not [p for p in changed - later if p.startswith(d + "/")], d                  # ... and none since but Stage 5.4.1's / the 6.1.1 CSS fix's declared files
    assert not stage610c & {"frontend/index.html", "tests/conftest.py", ".github/workflows/tests.yml", "README.md", "watchlist.txt", "DESIGN_610A_FORWARD_PAPER_AUTORUN.md"}
    assert FL.SEMANTIC_610C == {} and FL.INSTRUMENTATION_610C == {}                     # observability only: nothing changes what the executor does
    unregistrable = FL.EDITED_610C & (FL.NEW_68 | FL.NEW_69 | FL.NEW_691 | FL.NEW_610A | {"tests/freeze_ledger.py"})
    assert FL.EDITED_610C - unregistrable <= set(FL.LEDGER) and unregistrable == set(REPLAY_610C) | {"tests/freeze_ledger.py"}


def test_F02_execution_setups_auto_run_and_the_live_coverage_are_byte_identical_and_only_the_router_and_three_inert_flags_were_added():
    for d in ("execution", "setups", "autorun", "trader_view", "forward_capture", "safety", "proposals"):
        for p in sorted((ROOT / d).glob("*.py")):
            rel = p.relative_to(ROOT).as_posix()
            assert post(rel) == at_base(rel), rel                                       # the executor, the strategy, startup demotion and MANUAL_ARM: untouched
            assert now_text(rel) == post(rel) or rel in FL.SEMANTIC_541, rel               # since then: only Stage 5.4.1's declared correction
    for rel in ("scanner/watchlist.py", "watchlist.txt", "data/sector_map.py"):
        assert now_text(rel) == at_base(rel), rel                                       # live ticker coverage: watchlist.txt as before
    import execution as X
    assert X.MODES == ("MONITOR_ONLY", "PAPER_AUTO")
    cfg_base, cfg_now = at_base("config.py"), post("config.py")
    assert cfg_now.startswith(cfg_base.rstrip("\n"))
    assert [l.split(":")[0] for l in cfg_now[len(cfg_base):].splitlines() if l and not l.startswith("#")] == CONFIG_610C
    srv_base, srv_now = at_base("api/server.py").splitlines(), post("api/server.py").splitlines()
    assert [l for l in srv_base if l not in srv_now] == [] and [l for l in srv_now if l not in srv_base] == SERVER_LINES_610C
    life = post("api/server.py")
    life = life[life.index("async def _lifespan"):life.index("app = FastAPI(")]
    assert "decision_flow" not in life and "dynamic_universe" not in life                  # nothing new starts with the app: no thread, no loop


def test_F03_the_frozen_and_entry_contract_hashes_and_the_default_policy_hold_and_nothing_imports_the_universe_eagerly_in_a_clean_process():
    out = subprocess.run([sys.executable, "-c", "import sys, config, forward_validation as FV; from execution import exposure_gate as G; from autorun.policy import settings_from; "
                          "print(FV.frozen_hash()); print(G.contract_hash(G.from_config(config))); print(G.contract_hash(G.GateSettings(enabled=True))); "
                          "s = settings_from(config); print(s['effective_policy'], s['scheduled_arm_enabled'], s['disarm_at_session_end'], config.PAPER_AUTO_RUN_POLICY); "
                          "import decision_flow.view, api.routes.decision_flow; print('dynamic_universe' in sys.modules, config.DYNAMIC_UNIVERSE_MODE)"],
                         cwd=ROOT, capture_output=True, text=True)
    assert out.stdout.split() == [FROZEN_V1, CONTRACT_HASH_OFF, CONTRACT_HASH_ON, "MANUAL_ARM", "False", "True", "MANUAL_ARM", "False", "OFF"], out.stderr[-800:]


def test_F04_every_edit_no_ledger_entry_can_hold_is_pinned_exactly():
    import ast
    for rel, edits in REPLAY_610C.items():
        text = post(rel)
        for old, new in edits:
            assert text.count(new) == 1, (rel, new[:100])
            text = text.replace(new, old)
        assert text == at_base(rel), rel
    fl, base = post("tests/freeze_ledger.py"), at_base("tests/freeze_ledger.py")
    para = fl[fl.index("\nStage 6.10C (agent decision flow"):fl.index('"""', fl.index("\nStage 6.10C (agent decision flow"))]
    rebuilt = fl.replace(para, "", 1).replace(" + NEW_610A + NEW_610C plus this ledger.", " + NEW_610A plus this ledger.", 1)
    decl = rebuilt[rebuilt.index("BASE_610C = "):rebuilt.index("LEDGER = {")]
    rebuilt = rebuilt.replace(decl, "", 1)
    led = lambda t: t[t.index("LEDGER = {"):t.index("}\n", t.index("LEDGER = {")) + 2]  # noqa: E731
    rebuilt = rebuilt.replace(led(rebuilt), led(base), 1)
    assert rebuilt == base                                                                # the declarations, the paragraph and the ledger - nothing else
    old = ast.literal_eval(led(base)[len("LEDGER = "):])
    mine = ast.literal_eval(led(fl)[len("LEDGER = "):])                                   # the ledger Stage 6.10C left (main after PR #35)
    assert set(mine) == set(old)                                                          # no new key: every edited registered file was already registered
    assert {k for k in old if old[k] != mine[k]} == (FL.EDITED_610C & set(old))           # every re-hash is a declared Stage 6.10C edit
    for rel in FL.LEDGER:
        assert FL.view(rel, FL.now_text(rel)) == FL.base_text(rel), rel


def test_F05_the_text_bans_of_every_new_and_edited_file_hold():
    ui = now_text("frontend/product_ui.js")
    assert not re.search(r"(?i)retry|regime|claude|anthropic|openai|data-action|Not available in this version", ui)
    assert ui.count("runAutoAction(") == 3 and ui.count("clickLegacy(") == 5 and len(re.findall(r"\bfetch\(", ui)) == 1   # getJson is the one fetch call site
    assert not re.search(r"fetch\([^)]*(autorun|forward-capture|cohort|preflight)", ui) and 'method: "POST"' not in ui
    assert not re.search(r"WebSocket\(|EventSource\(", ui)
    names = set()
    for rel in sorted(p for p in FL.NEW_610C if p.endswith(".py") and not p.startswith(("tests/", "tools/"))):
        names |= used_names(now_text(rel))[0]
    assert not names & {"post_order", "insert_intent", "update_intent", "update_position", "enable_auto", "disable_auto", "set_mode", "requests", "urllib", "subprocess",
                        "TradingClient", "robin_stocks", "socket", "run_cycle", "evaluate_symbol", "evaluate_all", "add_to_watchlist", "remove_from_watchlist"}
    text = "\n".join(now_text(p) for p in sorted(FL.NEW_610C) if p.endswith(".py") and not p.startswith("tests/"))
    assert not re.search(r"(?i)\b(recommend\w*|profitable|guaranteed|buy now|sell now|you should)\b", text)
    cap = now_text("tools/evaluator_capacity.py")
    assert "import api.server" not in cap and "from api.server" not in cap and "getaddrinfo" in cap   # offline only: the network is blocked inside the tool


def test_F06_the_design_document_states_the_semantics_the_read_paths_the_universe_and_the_freeze():
    doc = now_text("DESIGN_610C_DECISION_FLOW.md")
    for s in (FROZEN_V1, CONTRACT_HASH_OFF, CONTRACT_HASH_ON, "SEMANTIC_610C = {}", "INSTRUMENTATION_610C = {}", "BASE_610C", "MANUAL_ARM", "MONITOR_ONLY", "mode=ro",
              "Not available", "no composite score", "Closest to READY", "DYNAMIC_UNIVERSE_MODE", "79 liquid US stocks", "implemented: False", "select_active()",
              "watchlist.txt", "GET /api/decision-flow", "1920×1080", "1280×720", "requests/min"):
        assert s in doc, s


def test_S01_robinhood_stays_read_only_and_the_decision_flow_adds_no_write_route():
    from api.server import app
    paths = app.openapi()["paths"]
    for p, ops in paths.items():
        if "robinhood" in p or p.startswith("/api/unified-portfolio") or p.startswith("/api/decision-flow"):
            assert set(ops) == {"get"}, (p, set(ops))
    before = {p for p in at_base("api/server.py").splitlines() if "include_router" in p}
    after = {p for p in post("api/server.py").splitlines() if "include_router" in p}
    assert len(after - before) == 1 and "decision_flow.router" in next(iter(after - before)) and before <= after
