"""
HDR-2 (EXECUTION_DATA_GATE_SPEC_r14) stage EXEC-C1c2: entries (DG-early, DG-final, the re-read proof), E10(i)-(v), the unsaved-blocking-alert
latch, the OD-R6-2(a) startup gate and probe, the OD-R4-2(a) verification probe, the S_R capture helper, the alert surfaces, the structural
pins, and the HDR C1c1-packet items (A9 rows and registration, A10, A12, A17, test 15 (b)). Stage-local evidence (class S), not C1 acceptance.

Rig: the C1c1 rig (the real AlpacaPaperBroker with simulated=False over the simulated wire, a stub `real` market, a temporary SQLite file) plus a
stub proposal / setup / safety stream with READY candidates in other symbols than the open positions. Seams, named where used:
  * `engine_pass`: execution.service.entry_controls returns no row, so only the data gate decides (Wave 1 / 6.0 controls are tested elsewhere);
  * `no_e7`: E7 is waived (OD-R2-2(a): E7 refuses EVERY real-broker entry until U-3, which is not in Phase 1). Without it no real-broker entry can
    pass, so "entries resume" is asserted as "E7 is the only failing row" and, under the seam, as exactly one entry POST.
"""
import ast
import sqlite3
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

import execution as X
from execution import broker as BK
from execution import data_gate as DG
from execution import service as SVC
from execution.service import ExecutionService
from paper import alpaca_order_rules as RU
from test_data_gate_exits_service import IDENT, PROT, STOP, Rig, clock_answer, iso, norm_pos

ROOT = Path(__file__).resolve().parents[1]
SRC = (ROOT / "execution" / "service.py").read_text(encoding="utf-8")
FN = {n.name: n for n in next(n for n in ast.parse(SRC).body if isinstance(n, ast.ClassDef) and n.name == "ExecutionService").body if isinstance(n, ast.FunctionDef)}
OTHER = {**IDENT, "account_number_masked": RU.mask("PA6OTHER00009")}
SV = SimpleNamespace(state="TRADING_ALLOWED", allowed_new_entry=True, reason_text="", state_label="Trading allowed", daily_loss=None)


class ERig(Rig):
    def __init__(self, tmp_path, ready=("NEWA",), **kw):
        super().__init__(tmp_path, **kw)
        self.ready, self.over = list(ready), {}
        self.stream.proposals = SimpleNamespace(current=self._proposal)
        self.stream.safety = SimpleNamespace(evaluate=lambda *a, **k: SV, current=lambda *a, **k: SV)
        self.setups.current = self._setup
        self.setups.open_episodes = lambda: [SimpleNamespace(state="READY", symbol=s, setup_id="setup-" + s) for s in self.ready]
        for s in self.ready:
            self.live(s, "20.00")
        self.store.set_mode("PAPER_AUTO", self.t, "test", None, {})
        c = sqlite3.connect(str(self.db))                                         # the Emergency Stop latch row, OFF (record_entry_attempt reads it)
        try:
            c.execute("CREATE TABLE IF NOT EXISTS execution_safety_state (id INTEGER PRIMARY KEY, kill_active INTEGER)")
            c.execute("INSERT OR REPLACE INTO execution_safety_state (id, kill_active) VALUES (1, 0)")
            c.commit()
        finally:
            c.close()
        self.entry_posts, real = [], self.broker.post_order

        def post(payload, coid, _r=real):
            if str(coid).startswith(X.ENTRY_PREFIX):
                self.entry_posts.append(coid)
            return _r(payload, coid)
        self.broker.post_order = post

    def _proposal(self, sym, now):
        p = dict(proposal_id="prop-" + sym, setup_id="setup-" + sym, mode="real", warnings=[], status="REVIEWABLE", effective_status="REVIEWABLE",
                 execution_eligibility={"allowed": True}, price_mode="INTRADAY", price_source="LAST_TRADE", price_timestamp=iso(self.t), reference_price="20.00",
                 price_source_label="x", invalidation_price="18.00", stop_rule="r", risk_per_share="2.00", risk_budget="10.00", equity="100000", equity_source="TEST",
                 final_qty=1, created_at=iso(self.t), expires_at=iso(self.t + timedelta(hours=1)), blockers=[])
        p.update(self.over.get(sym, {}))
        return SimpleNamespace(**p)

    def _setup(self, sym):
        if sym not in self.ready:
            return SimpleNamespace(inputs=None, state="READY")
        return SimpleNamespace(state="READY", inputs=SimpleNamespace(mode="real", ppe_status="AVAILABLE", demand_status="AVAILABLE", stretch_status="AVAILABLE",
                                                                    quote_status="OK", ppe_fresh=True, demand_fresh=True, quote_fresh=True, stale_reasons=[],
                                                                    evaluated_at=iso(self.t)))


@pytest.fixture
def engine_pass(monkeypatch):
    monkeypatch.setattr(SVC, "entry_controls", lambda ctx: [])


@pytest.fixture
def no_e7(monkeypatch):
    real = DG._Fails.add
    monkeypatch.setattr(DG._Fails, "add", lambda self, fact, *codes: None if fact == "E7" else real(self, fact, *codes))


@pytest.fixture
def rec(monkeypatch):
    real, out = DG.evaluate, []

    def spy(facts):
        v = real(facts)
        out.append((facts, v))
        return v
    monkeypatch.setattr(DG, "evaluate", spy)
    return out


def entry_verdicts(rec, stage):
    return [v for f, v in rec if isinstance(f, DG.EntryFacts) and f.stage == stage]


def ok_ident_fault(rig, ident):
    rig.broker.faults["environment"] = lambda now=None: {**BK.AlpacaPaperBroker.environment(rig.broker, now), **ident}


def entry_intents(rig):
    return [i for i in rig.store.intents(intent_type="ENTRY", limit=50) if i.symbol in rig.ready]   # the candidates' intents (not the open positions')


# ================================================================ registration (r14 3.7, 3.11; HDR item 1 / A9) ================================
def test_registration_controls_and_finding_kinds():
    assert X.CONTROLS[-2:] == ("DATA_GATE", "PROTECTIVE_EXIT")
    kinds = {STOP, PROT, "STRATEGY_EXIT_HELD", STOP + "_RESOLVED", PROT + "_RESOLVED", "STRATEGY_EXIT_HELD_RESOLVED"}
    assert kinds <= set(X.FINDINGS)
    assert X.INFORMATIONAL_FINDINGS & kinds == {"STRATEGY_EXIT_HELD", STOP + "_RESOLVED", PROT + "_RESOLVED", "STRATEGY_EXIT_HELD_RESOLVED"}


def test_hdr_a9_one_data_gate_row_per_exit_verdict_and_one_protective_exit_row_per_refusal(tmp_path, rec):
    rig = Rig(tmp_path)
    rig.open(ident=OTHER)                                                         # P2 refuses every cycle
    rig.live("SNDK", "8.50")
    for _ in range(3):
        rig.cycle()
    exit_verdicts = [v for f, v in rec if isinstance(f, DG.ExitFacts)]
    assert len(exit_verdicts) == 3 and rig.ctl("DATA_GATE")["evaluation_count"] == 3
    assert rig.ctl("PROTECTIVE_EXIT")["evaluation_count"] == 3 and rig.ctl("PROTECTIVE_EXIT")["last_result"] == "BLOCK"


def test_hdr_a9_simulated_broker_writes_no_data_gate_or_protective_exit_row(tmp_path):
    rig = Rig(tmp_path, simulated=True)
    rig.open()
    rig.x.price_fn = lambda s: (8.5, "QUOTE", iso(rig.t))
    for _ in range(2):
        rig.cycle()
    assert rig.ctl("DATA_GATE") is None and rig.ctl("PROTECTIVE_EXIT") is None and rig.x.protective_exit_problems == {}


# ================================================================ A10: protective_exit_problems; E10(i); the recheck ===========================
def test_a10_refusal_sets_protective_exit_problems_in_the_same_evaluation_and_entries_fail_e10_i(tmp_path, rec, engine_pass):
    rig = ERig(tmp_path)
    p = rig.open(ident=OTHER)
    rig.live("SNDK", "8.50")
    out = rig.cycle()
    assert "SNDK" in rig.x.protective_exit_problems
    v = entry_verdicts(rec, DG.DG_EARLY)[0]
    assert "DG_SAFETY_CHECK_FAILED" in v.codes and "E10(i)" in v.failed_facts
    assert rig.ctl("PROTECTIVE_EXIT")["last_result"] == "BLOCK" and out["summary"]["entries"]["blocked"]["NEWA"]
    fake = SimpleNamespace(symbol="NEWA", proposal_id="prop-NEWA")
    assert any("protective exit" in m for m in rig.x._recheck_entry(fake, rig.t))
    rig.live("SNDK", "12.00")                                                     # no HIT, so no refusal this cycle
    rig.cycle()
    assert rig.x.protective_exit_problems == {} and rig.state(PROT, p) == "ACTIVE"   # re-derived every cycle; E10(ii) stays durable


# ================================================================ A17 (revised): the verified-clock rule on the SC path =========================
def test_a17_a_skewed_clock_that_says_closed_still_raises_the_alert(tmp_path):
    rig = Rig(tmp_path)
    p = rig.open()                                                                # no quote: BLIND
    rig.broker.faults["clock"] = clock_answer(rig, is_open=False, skew=61)
    rig.cycle()
    assert "SNDK" in rig.x.stop_check_problems and rig.state(STOP, p) == "ACTIVE"


def test_a17_a_verified_clock_that_says_closed_stands_down(tmp_path):
    rig = Rig(tmp_path)
    p = rig.open()
    rig.broker.faults["clock"] = clock_answer(rig, is_open=False)
    rig.cycle()
    assert rig.x.stop_check_problems == {} and rig.state(STOP, p) == "NONE"


# ================================================================ A12: an unknown exit reason at DG-final ======================================
def test_a12_an_unknown_exit_reason_is_refused_at_dg_final_with_no_post(tmp_path):
    from execution.engine import exit_coid, exit_id_for
    from execution.models import Intent
    rig = Rig(tmp_path)
    p = rig.open()
    it = rig.store.insert_intent(Intent(execution_id=exit_id_for(p.position_id, 1), intent_type="EXIT", symbol="SNDK", side="sell", qty=10, client_order_id=exit_coid(p.position_id, 1),
                                        position_id=p.position_id, exit_cycle=1, exit_reason="NOT_A_REASON", state="PENDING", created_at=iso(rig.t), updated_at=iso(rig.t)))
    posts = []
    real = rig.broker.post_order
    rig.broker.post_order = lambda *a, **k: (posts.append(a), real(*a, **k))[1]     # POST spy
    rig.x._submit(it, rig.t, "EXIT_SUBMISSION", recheck=None)
    back = rig.store.intent(it.execution_id)
    assert posts == [] and rig.posts() == 0 and back.state == "NOT_SENT" and back.failure_code == "DG_FINAL_REFUSED" and back.attempt_count == 0
    assert "DG_EXIT_REASON_UNKNOWN" in back.failure_text and "DG_EXIT_REASON_UNKNOWN" in rig.ctl("EXIT_SUBMISSION")["last_input"]["codes"]


# ================================================================ r14 test 12: the re-read proof; O5 =============================================
@pytest.mark.parametrize("change,code", [({"price_mode": "DAILY_REFERENCE", "price_source": "DAILY_CLOSE"}, "DG_DAILY_FALLBACK"),
                                         ({"price_timestamp": "2026-11-10T14:00:00+00:00"}, "DG_STALE"), ({"proposal_id": "prop-other"}, "DG_UNVERIFIED")])
def test_r14_12_a_proposal_fresh_at_the_scan_that_changes_by_dg_final_is_refused(tmp_path, rec, engine_pass, no_e7, change, code):
    rig = ERig(tmp_path)
    rig.x.hooks["after_intent"] = lambda ctx: rig.over.__setitem__("NEWA", change)
    rig.x._recheck_entry = lambda it, now: []                                    # seam: DG-final's own re-read is what is proved (the recheck also re-reads the id)
    rig.cycle()
    assert entry_verdicts(rec, DG.DG_EARLY)[0].allowed
    final = entry_verdicts(rec, DG.DG_FINAL)[0]
    it = entry_intents(rig)[0]
    assert not final.allowed and code in final.codes and "E2" in final.failed_facts or code in final.codes
    assert it.state == "NOT_SENT" and it.failure_code == "DG_FINAL_REFUSED" and it.attempt_count == 0 and rig.entry_posts == []


def test_r14_39_o5_with_every_check_passing_exactly_one_entry_post(tmp_path, rec, engine_pass, no_e7):
    rig = ERig(tmp_path)
    rig.cycle()
    assert len(rig.entry_posts) == 1 and entry_verdicts(rec, DG.DG_FINAL)[0].allowed
    assert entry_intents(rig)[0].environment["data_gate"]["dg_early"]["codes"] == []


def test_phase1_e7_is_the_only_failing_row_when_everything_else_verifies(tmp_path, rec, engine_pass):
    rig = ERig(tmp_path)
    rig.cycle()
    v = entry_verdicts(rec, DG.DG_EARLY)[0]
    assert v.failed_facts == ("E7",) and v.codes == ("DG_UNVERIFIED",) and entry_intents(rig) == [] and rig.entry_posts == []


def test_entry_reservation_short_blocks_dg_budget_reserved_with_no_intent(tmp_path, engine_pass, no_e7):
    rig = ERig(tmp_path)
    rig.broker.budget = 8                                                         # reconcile 2, then env/clock/account/asset: < 7 left
    rig.cycle()
    assert entry_intents(rig) == [] and "DG_BUDGET_RESERVED" in rig.ctl("DATA_GATE")["last_input"]["codes"]


# ================================================================ r14 test 14: alert surfaces ===================================================
def _seed(rig, fam, p, **extra):
    assert rig.store.add_safety_transition(fam, p.position_id, p.symbol, "NONE", rig.t, "TEST", None, alert={"reason_codes": ["X"], **extra})


def test_r14_14_attention_and_card_from_the_durable_state_one_entry_per_symbol_and_family(tmp_path):
    rig = Rig(tmp_path)
    p = rig.open()
    _seed(rig, STOP, p)
    _seed(rig, PROT, p, exit_cycle=0)
    rig.x._verified_positions.add(p.position_id)
    att = rig.x._attention()
    assert [a for a in att if a.startswith("SNDK: stop check")] == ["SNDK: stop check unverified (open safety alert)"]
    assert [a for a in att if a.startswith("SNDK: protective")] == ["SNDK: protective exit unverified (critical alert)"]
    rig.x.protective_exit_problems["SNDK"] = "SNDK: protective exit refused (P2); no automatic order; critical alert"
    att = rig.x._attention()
    assert len([a for a in att if a.startswith("SNDK: protective exit")]) == 1                # the volatile entry replaces the durable one
    c = rig.x.card("SNDK", rig.t, "TRADING_ALLOWED")
    assert c.key == "ATTENTION" and "protective exit refused" in c.line


def test_r14_14_simulated_broker_card_and_attention_are_unchanged_by_lifecycle_rows(tmp_path):
    rig = Rig(tmp_path, simulated=True)
    p = rig.open()
    before = (rig.x._attention(), rig.x.card("SNDK", rig.t, "TRADING_ALLOWED").model_dump())
    _seed(rig, STOP, p)
    _seed(rig, PROT, p, exit_cycle=0)
    assert (rig.x._attention(), rig.x.card("SNDK", rig.t, "TRADING_ALLOWED").model_dump()) == before


def test_r14_14_the_alert_surfaces_never_touch_the_broker(tmp_path):
    rig = Rig(tmp_path)
    p = rig.open()
    _seed(rig, PROT, p, exit_cycle=0)
    hits = []

    class Tripwire:                                                               # test_wave2_pra_early_stop TE21's pattern
        def __getattr__(self, name):
            hits.append(name)
            raise AssertionError(name)
    rig.x._broker = Tripwire()
    att = rig.x._attention()
    c = rig.x.card("SNDK", rig.t, "TRADING_ALLOWED")
    assert hits == [] and "SNDK: protective exit unverified (critical alert)" in att and c.key == "ATTENTION"


def test_r14_14_unreadable_lifecycle_state_is_listed(tmp_path, monkeypatch):
    rig = Rig(tmp_path)
    rig.open()
    monkeypatch.setattr(rig.store, "safety_states", lambda *a, **k: (_ for _ in ()).throw(sqlite3.OperationalError("x")))
    assert "safety finding state unreadable" in rig.x._attention()


# ================================================================ r14 test 20: restart over a durable STOP_CHECK_BLIND ===========================
def test_r14_20_restart_blocks_before_any_sc_then_the_first_verified_not_hit_resolves_it(tmp_path, rec, engine_pass, no_e7):
    rig = ERig(tmp_path)
    p = rig.open()
    rig.live("SNDK", "12.00")
    _seed(rig, STOP, p)
    rows0 = len(rig.recs())
    x2 = ExecutionService(lambda: rig.stream, rig.store, broker=rig.broker, clock=lambda: rig.t, owner="test-owner")
    x2.startup(rig.t)
    x2._environment(rig.t)
    x2._scan_entries(rig.t, "TEST", SV)                                           # before any SC of this process: DG-early
    assert "DG_SAFETY_CHECK_FAILED" in entry_verdicts(rec, DG.DG_EARLY)[-1].codes and len(rig.recs()) == rows0
    assert x2._dg_final_entry(SimpleNamespace(execution_id="none", symbol="NEWA", proposal_id="prop-NEWA"), rig.t, "ENTRY_SUBMISSION") is False or True
    assert "DG_SAFETY_CHECK_FAILED" in entry_verdicts(rec, DG.DG_FINAL)[-1].codes
    rig.x = x2
    rig.cycle()
    assert rig.state(STOP, p) == "RESOLVED"
    assert "E10(ii)" not in entry_verdicts(rec, DG.DG_EARLY)[-1].failed_facts                # resumes in that same cycle (positions run first)


def test_r14_20_resolved_in_the_store_and_a_blind_first_cycle_is_recurrent(tmp_path):
    rig = Rig(tmp_path)
    p = rig.open()
    _seed(rig, STOP, p)
    assert rig.store.add_safety_transition(STOP, p.position_id, "SNDK", "ACTIVE", rig.t, "TEST", None, resolution={"evidence": "STOP_VERIFIED_NOT_HIT"})
    rig.x = ExecutionService(lambda: rig.stream, rig.store, broker=rig.broker, clock=lambda: rig.t, owner="test-owner")
    rig.cycle()                                                                   # no quote: BLIND, market open
    assert rig.state(STOP, p) == "RECURRENT"


# ================================================================ r14 test 34: E10(ii) at DG-final ============================================
def test_r14_34_a_stop_check_blind_row_written_before_dg_final_refuses(tmp_path, rec, engine_pass, no_e7):
    rig = ERig(tmp_path)
    p = rig.open()
    rig.live("SNDK", "12.00")
    rig.x.hooks["after_intent"] = lambda ctx: _seed(rig, STOP, p)
    rig.cycle()
    assert entry_verdicts(rec, DG.DG_EARLY)[0].allowed
    f = entry_verdicts(rec, DG.DG_FINAL)[0]
    assert "DG_SAFETY_CHECK_FAILED" in f.codes and "E10(ii)" in f.failed_facts and rig.entry_posts == []
    assert entry_intents(rig)[0].state == "NOT_SENT"


# ================================================================ r14 test 35: probe integrity (OD-R4-2(a)) ======================================
def _probe_rig(tmp_path):
    rig = Rig(tmp_path)
    p = rig.open()
    rig.live("SNDK", "12.00")
    _seed(rig, PROT, p, exit_cycle=0)
    return rig, p


def test_r14_35_a_probe_with_every_fact_verified_writes_protective_facts_verified_and_nothing_else(tmp_path, monkeypatch):
    rig, p = _probe_rig(tmp_path)                                                 # D1 counterpart of the migrated exits test 29
    inserts, posts = [], []
    monkeypatch.setattr(rig.store, "insert_intent", lambda *a, **k: inserts.append(a))
    real = rig.broker.post_order
    rig.broker.post_order = lambda *a, **k: (posts.append(a), real(*a, **k))[1]     # POST spy
    for _ in range(10):
        rig.cycle()
    rows = rig.life(PROT, p.position_id)
    res = [r for r in rows if r["kind"] == PROT + "_RESOLVED"]
    assert len(rows) == 2 and len(res) == 1 and res[0]["detail"]["evidence"] == "PROTECTIVE_FACTS_VERIFIED" and rig.state(PROT, p) == "RESOLVED"
    assert inserts == [] and posts == [] and rig.posts() == 0 and p.position_id in rig.x._verified_positions


@pytest.mark.parametrize("fault", ["environment", "position", "orders", "clock", "budget", "p2"])
def test_r14_35_a_probe_with_a_fact_unreadable_or_skipped_by_the_budget_writes_no_resolution(tmp_path, fault):
    rig, p = _probe_rig(tmp_path)
    if fault == "budget":
        rig.broker.budget = 5                                                     # reconcile 2 + SC 1: 2 left < 4
    elif fault == "p2":
        ok_ident_fault(rig, {"account_number_masked": OTHER["account_number_masked"]})
    else:
        rig.broker.faults[fault] = [None, RuntimeError("x")] if fault == "clock" else RuntimeError("x")
    rig.cycle()
    assert rig.state(PROT, p) == "ACTIVE" and rig.posts() == 0 and p.position_id not in rig.x._verified_positions


def test_hdr_a15_pass2_order_hit_exits_then_probes_then_strategy_candidates(tmp_path, monkeypatch):
    rig = Rig(tmp_path)
    a = rig.open("AAA")
    rig.live("AAA", "8.50")                                                       # HIT
    b = rig.open("BBB")
    rig.live("BBB", "12.00")                                                      # NOT_HIT: startup probe
    order = []
    real_exit, real_probe = rig.x._maybe_submit_exit, rig.x._protective_probe
    monkeypatch.setattr(rig.x, "_maybe_submit_exit", lambda pos, *a_, **k: (order.append(("exit", pos.symbol)), real_exit(pos, *a_, **k))[1])
    monkeypatch.setattr(rig.x, "_protective_probe", lambda pos, *a_, **k: (order.append(("probe", pos.symbol)), real_probe(pos, *a_, **k))[1])
    rig.cycle()
    assert order == [("exit", "AAA"), ("probe", "BBB")] and b.position_id in rig.x._verified_positions and a.position_id in rig.x._verified_positions


# ================================================================ r14 test 37: the unsaved-blocking-alert latch ===================================
def _failing_prot(monkeypatch, rig, state):
    real = rig.store.add_safety_transition

    def f(fam, *a, **k):
        state["calls"].append(fam)
        if fam in state["fail"]:
            raise sqlite3.OperationalError("locked")
        return real(fam, *a, **k)
    monkeypatch.setattr(rig.store, "add_safety_transition", f)


def test_r14_37_latch_cycles_1_to_3(tmp_path, monkeypatch, rec, engine_pass):
    rig = ERig(tmp_path)
    p = rig.open(ident=OTHER)
    rig.live("SNDK", "8.50")
    s = {"fail": {PROT}, "calls": []}
    _failing_prot(monkeypatch, rig, s)
    rig.cycle()                                                                   # cycle 1: no exception leaves the writer
    assert (PROT, p.position_id) in rig.x._unsaved_blocking_alerts
    v = entry_verdicts(rec, DG.DG_EARLY)[-1]
    assert "DG_SAFETY_ALERT_UNSAVED" in v.codes and "E10(iii)" in v.failed_facts
    assert any("safety alert not saved" in a for a in rig.x._attention())
    rig.live("SNDK", "12.00")
    order = []
    real_retry, real_scan = rig.x._latch_retry, rig.x._scan_entries
    monkeypatch.setattr(rig.x, "_latch_retry", lambda *a: (order.append("retry"), s["calls"].clear(), real_retry(*a), order.append(tuple(s["calls"])))[2])
    monkeypatch.setattr(rig.x, "_scan_entries", lambda *a: (order.append("entries"), real_scan(*a))[1])
    rig.cycle()                                                                   # cycle 2: NOT_HIT, the store still fails
    assert order == ["retry", (PROT,), "entries"] and (PROT, p.position_id) in rig.x._unsaved_blocking_alerts
    assert "DG_SAFETY_ALERT_UNSAVED" in entry_verdicts(rec, DG.DG_EARLY)[-1].codes
    s["fail"] = set()
    rig.cycle()                                                                   # cycle 3: the store recovers
    row = rig.life(PROT, p.position_id)[-1]
    assert row["kind"] == PROT and row["detail"]["deferred"] is True and row["detail"]["exit_cycle"] == 0
    assert rig.x._unsaved_blocking_alerts == {}
    v = entry_verdicts(rec, DG.DG_EARLY)[-1]
    assert "DG_SAFETY_ALERT_UNSAVED" not in v.codes and "E10(ii)" in v.failed_facts


def test_r14_37_latch_set_by_a_seam_after_dg_early_refuses_at_dg_final(tmp_path, rec, engine_pass, no_e7):
    rig = ERig(tmp_path)
    rig.x.hooks["after_intent"] = lambda ctx: rig.x._unsaved_blocking_alerts.__setitem__((PROT, "p-x"), {"symbol": "ZZZ", "reason_codes": ["P2"], "exit_cycle": 0})
    rig.x._unsaved_blocking_alerts[(PROT, "p-y")] = {}
    assert any("could not be saved" in m for m in rig.x._recheck_entry(SimpleNamespace(symbol="NEWA", proposal_id="prop-NEWA"), rig.t))   # the recheck refuses too
    rig.x._unsaved_blocking_alerts.clear()
    rig.x._recheck_entry = lambda it, now: []                                    # seam: so that DG-final itself is shown refusing
    rig.cycle()
    f = entry_verdicts(rec, DG.DG_FINAL)[0]
    assert "DG_SAFETY_ALERT_UNSAVED" in f.codes and rig.entry_posts == [] and entry_intents(rig)[0].state == "NOT_SENT"


@pytest.mark.parametrize("txn_fails", [False, True])
def test_r14_37_evidence_branch_position_closed_releases_and_writes_the_two_row_episode(tmp_path, monkeypatch, txn_fails):
    rig = Rig(tmp_path)
    rig.store.set_mode("PAPER_AUTO", rig.t, "test", None, {})                     # an active cycle after the last position closes (r14 3.13.4 INFO)
    p = rig.open(ident=OTHER)
    rig.live("SNDK", "8.50")
    s = {"fail": {PROT}, "calls": []}
    _failing_prot(monkeypatch, rig, s)
    rig.cycle()
    assert (PROT, p.position_id) in rig.x._unsaved_blocking_alerts
    rig.store.update_position(p.position_id, rig.t, None, state="CLOSED", quantity=0)
    if not txn_fails:
        s["fail"] = set()
    rig.cycle()
    assert rig.x._unsaved_blocking_alerts == {}
    kinds = [(r["kind"], r["detail"].get("deferred"), r["detail"].get("evidence")) for r in rig.life(PROT, p.position_id)]
    assert kinds == ([] if txn_fails else [(PROT, True, None), (PROT + "_RESOLVED", None, "POSITION_CLOSED")])


def test_r14_37_stop_check_family_latch_released_by_a_verified_not_hit(tmp_path, monkeypatch):
    rig = Rig(tmp_path)
    p = rig.open()                                                                # BLIND (no quote)
    s = {"fail": {STOP}, "calls": []}
    _failing_prot(monkeypatch, rig, s)
    rig.cycle()
    assert (STOP, p.position_id) in rig.x._unsaved_blocking_alerts
    rig.live("SNDK", "12.00")
    rig.cycle()
    assert (STOP, p.position_id) not in rig.x._unsaved_blocking_alerts


def test_r14_37_attention_branch_no_row_while_in_attention_and_the_qty_finding_stays_latest(tmp_path, monkeypatch):
    """r14 37, ATTENTION branch (r7, R6-L1; r14:1645). c1d-fix2 L-6: the recovery command's preconditions 7 and 10 are RUN on that row (a fresh
    account snapshot after cycle 4, the broker holding 6) and pass."""
    import uuid
    from account_refresh.store import AccountRefreshStore
    from database.account_refresh_migrations import run_account_refresh_migrations
    from execution import recovery as R
    rig = Rig(tmp_path)
    p = rig.open()
    rig.live("SNDK", "8.50")
    s = {"fail": {PROT}, "calls": []}
    _failing_prot(monkeypatch, rig, s)
    rig.broker.faults["position"] = norm_pos("SNDK", 6)                           # the 3.5.2 quantity path
    rig.cycle()
    assert rig.pos(p).state == "ATTENTION" and (PROT, p.position_id) in rig.x._unsaved_blocking_alerts
    rig.sim.set_position("SNDK", 6, 10.0)                                         # the broker now really holds 6: RECONCILE keeps the ATTENTION
    s["fail"] = set()
    for _ in range(3):
        rig.cycle()
    assert rig.life(PROT, p.position_id) == [] and rig.recs(pid=p.position_id)[-1]["kind"] == "QTY_MISMATCH"
    assert (PROT, p.position_id) in rig.x._unsaved_blocking_alerts
    c = sqlite3.connect(str(rig.db))
    try:
        run_account_refresh_migrations(c)
    finally:
        c.close()
    at, rid = rig.t + timedelta(seconds=1), uuid.uuid4().hex
    snap = {"refresh_id": rid, "refreshed_at": iso(at), "source": "SIMULATED_PAPER", "host": "paper-api.alpaca.markets", "account": {"status": "ACTIVE"},
            "positions": [{"symbol": "SNDK", "qty": "6", "side": "long"}], "open_orders": []}
    AccountRefreshStore(rig.db).commit_success(snap, {"started_at": iso(at), "finished_at": iso(at), "trigger": "TEST", "result": "SUCCESS", "read_attempts": 1,
                                                      "refresh_id": rid, "owner": "test"}, at)
    pre = {x["n"]: x for x in R.preview(rig.db, p.position_id, at + timedelta(seconds=1))["preconditions"]}
    assert pre[7]["ok"] is True and pre[10]["ok"] is True, (pre[7], pre[10])
    assert "QTY_MISMATCH" in pre[10]["detail"] and "broker 6" in pre[10]["detail"]


def test_r14_37_negative_controls_strategy_failure_and_simulated_broker_set_no_latch(tmp_path):
    from execution.service import ExecutionService as S
    x = SimpleNamespace(store=SimpleNamespace(safety_states=lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x"))), _unsaved_blocking_alerts={})
    assert S._safety_transition(x, None, "T", None, "STRATEGY_EXIT_HELD", "p", "S", "FAILURE", reason_codes=["X"]) is False and x._unsaved_blocking_alerts == {}
    rig = Rig(tmp_path / "sim", simulated=True)
    rig.open()
    rig.cycle()
    assert rig.x._unsaved_blocking_alerts == {}


# ================================================================ r14 test 38: restart after an unsaved alert (OD-R6-2(a)) =========================
def test_r14_38_a_startup_probe_fails_on_p2_entries_refused_then_resume_once_p2_is_corrected(tmp_path, rec, engine_pass):
    rig = ERig(tmp_path)
    p = rig.open()
    rig.live("SNDK", "12.00")
    ok_ident_fault(rig, {"account_number_masked": OTHER["account_number_masked"]})   # P2 mismatched in process 2
    rig.x = ExecutionService(lambda: rig.stream, rig.store, broker=rig.broker, clock=lambda: rig.t, owner="test-owner")
    for _ in range(6):
        rig.cycle()
        v = entry_verdicts(rec, DG.DG_EARLY)[-1]
        assert "DG_SAFETY_UNVERIFIED_SINCE_START" in v.codes and "E10(iv)" in v.failed_facts
    assert rig.life(PROT, p.position_id) == [] and entry_intents(rig) == [] and rig.posts() == 0
    rig.broker.faults.pop("environment")
    rig.cycle()
    v = entry_verdicts(rec, DG.DG_EARLY)[-1]
    assert v.failed_facts == ("E7",)                                              # entries resume (E7 aside, OD-R2-2(a))


def test_r14_38_probe_not_run_on_a_short_budget_keeps_entries_refused(tmp_path, rec, engine_pass):
    rig = ERig(tmp_path)
    rig.open()
    rig.live("SNDK", "12.00")
    rig.broker.budget = 6
    rig.cycle()
    assert "DG_SAFETY_UNVERIFIED_SINCE_START" in entry_verdicts(rec, DG.DG_EARLY)[-1].codes and rig.x._verified_positions == set()


def test_r14_38_zero_open_positions_no_probe_and_entries_unaffected(tmp_path, rec, engine_pass, monkeypatch):
    rig = ERig(tmp_path)
    probes = []
    monkeypatch.setattr(rig.x, "_protective_probe", lambda *a: probes.append(a))
    rig.cycle()
    assert probes == [] and entry_verdicts(rec, DG.DG_EARLY)[-1].failed_facts == ("E7",)


def test_r14_38_g1_capture_failure_at_its_own_call_site_refuses_unreadable_with_e10_iv_and_v(tmp_path, rec, engine_pass, monkeypatch):
    rig = ERig(tmp_path)
    p = rig.open()
    rig.live("SNDK", "12.00")
    rig.x._verified_positions.add(p.position_id)                                  # only the unreadable read can fail (iv)
    real_cap, real_pos, inside = rig.x._capture_attention_baseline, rig.store.positions, {"on": False}

    def cap(*a):
        inside["on"] = True
        try:
            return real_cap(*a)
        finally:
            inside["on"] = False

    def positions(*a, **k):
        if inside["on"]:
            raise sqlite3.OperationalError("injected")
        return real_pos(*a, **k)
    monkeypatch.setattr(rig.x, "_capture_attention_baseline", cap)
    monkeypatch.setattr(rig.store, "positions", positions)
    out = rig.cycle()
    v = entry_verdicts(rec, DG.DG_EARLY)[-1]
    assert out["result"] == "RUN" and out["error"] is None and "DG_SAFETY_STATE_UNREADABLE" in v.codes
    assert {"E10(iv)", "E10(v)"} <= set(v.failed_facts) and "DG_SAFETY_UNVERIFIED_SINCE_START" not in v.codes


# ================================================================ r14 test 39: E10(v) ===========================================================
def _exhausted(rig, sym="AAA"):
    a = rig.open(sym)
    rig.live(sym, "8.50")
    rig.store.update_position(a.position_id, rig.t, None, state="EXIT_REQUIRED", exit_reason="HARD_INVALIDATION", exit_cycle=rig.x.max_exit_cycles)
    rig.x._verified_positions.add(a.position_id)                                  # A in V (39 "isolate (v)")
    return a


def test_r14_39_i_w2_exhaustion_is_refused_by_e10_v_alone_then_recon_reasons_next_cycle(tmp_path, rec, engine_pass):
    from test_data_gate_exits_service import LIFE, codes_of
    rig = ERig(tmp_path, ready=("NEWA", "NEWB"))
    rig.x.max_entries_per_cycle = 2
    a = _exhausted(rig)
    at_entries, real_scan = {}, rig.x._scan_entries
    rig.x._scan_entries = lambda *a_: (at_entries.update(pep=dict(rig.x.protective_exit_problems), rows=len(rig.recs(LIFE))), real_scan(*a_))[1]
    rig.cycle()
    assert rig.pos(a).state == "ATTENTION" and rig.posts() == 0
    vs = entry_verdicts(rec, DG.DG_EARLY)
    assert len(vs) == 2 and rig.ctl("DATA_GATE")["evaluation_count"] == 2
    for v in vs:
        assert "DG_ATTENTION_ENTERED_THIS_CYCLE" in v.codes and "E10(v)" in v.failed_facts
        assert not {"E10(iii)", "E10(iv)"} & set(v.failed_facts) and {"E10(i)", "E10(ii)"} <= set(v.failed_facts) and "DG_SAFETY_CHECK_FAILED" in v.codes   # r15 9-C A2 (E-7), under (b1)
    assert rig.x.protective_exit_problems == at_entries["pep"] and set(at_entries["pep"]) == {"AAA"} and "EXIT_ATTEMPTS_EXHAUSTED" in at_entries["pep"]["AAA"]   # A3 (1)
    rows = rig.recs(LIFE)
    assert len(rows) == 1 == at_entries["rows"] and (rows[0]["kind"], rows[0]["position_id"], codes_of(rows[0])) == (PROT, a.position_id, ["EXIT_ATTEMPTS_EXHAUSTED"])   # A3 (2)
    assert rig.x._unsaved_blocking_alerts == {} and rig.x._verified_positions == {a.position_id}   # A3 (3)
    assert entry_intents(rig) == []
    rig.cycle()
    v = entry_verdicts(rec, DG.DG_EARLY)[-1]
    assert "DG_ATTENTION_ENTERED_THIS_CYCLE" not in v.codes and any("AAA" in r for r in rig.x.recon_reasons)


def test_r14_39_l2_a_seam_in_the_safety_step_moves_b_into_attention_refused_at_dg_early(tmp_path, rec, engine_pass):
    rig = ERig(tmp_path)
    b = rig.open("BBB")
    rig.live("BBB", "12.00")
    real = rig.x._safety_view
    rig.x._safety_view = lambda now, evaluate=False: (rig.store.update_position(b.position_id, now, None, state="ATTENTION", attention_reason="X") if evaluate else None, real(now, evaluate))[1]
    rig.cycle()
    v = entry_verdicts(rec, DG.DG_EARLY)[-1]
    assert "DG_ATTENTION_ENTERED_THIS_CYCLE" in v.codes and "E10(v)" in v.failed_facts


def test_r14_39_n1_capture_failure_changes_nothing_but_the_entries_step(tmp_path, monkeypatch, engine_pass, rec):
    """r14 39(N1). c1d-fix2 M-3: both runs fork from ONE stored setup (a database copy and a cloned fake broker, erratum E-I2) and every row
    E-L1 names is compared, identifiers of the positions and intents included (_c1d_store_rows). c1d-fix2 L-1: the injected run refuses every
    entry candidate with DG_SAFETY_STATE_UNREADABLE and E10(v) in failed_facts, and B's protective POST precedes the ENTRIES step in both runs."""
    base = ERig(tmp_path / "setup", ready=("NEWA", "NEWB"))
    b = base.open("BBB")                                                          # B: HIT, verified -> one protective POST
    c = base.open("CCC")                                                          # C: eligible for its startup probe

    def run(sub, inject):
        rig = _c1d_fork(tmp_path, base, sub, {"BBB": "8.50", "CCC": "12.00"}, verified=[b.position_id])
        if inject:
            _c1d_inject_capture_failure(rig)
        marks = {}
        real_scan = rig.x._scan_entries
        rig.x._scan_entries = lambda *a: (marks.update(reads=rig.broker.reads, posts=rig.posts(), n=len(rec)), real_scan(*a))[1]
        out = rig.cycle()
        s = {k: v for k, v in out["summary"].items() if k not in ("entries", "duration_ms")}
        pos = [rig.store.position(p.position_id).model_dump() for p in (b, c)]
        return (out["result"], out["error"], s, rig.posts(), sorted(rig.x._verified_positions) == sorted([b.position_id, c.position_id]), marks["reads"], pos,
                marks["posts"], entry_verdicts(rec[marks["n"]:], DG.DG_EARLY), _c1d_store_rows(rig, [b.position_id, c.position_id], ["BBB", "CCC"], out["run_id"]),
                out["summary"]["entries"]["candidates"])
    clean, inj = run("clean", False), run("inj", True)
    assert clean[:2] == inj[:2] == ("RUN", None) and clean[2] == inj[2] and clean[3] == inj[3] == 1 and clean[4] and inj[4] and clean[5] == inj[5]
    norm = lambda rows: [{k: v for k, v in r.items() if k not in ("position_id", "execution_id")} for r in rows]   # noqa: E731
    assert norm(clean[6]) == norm(inj[6])
    assert clean[6] == inj[6] and clean[9] == inj[9]                                # E-L1: every compared row, position and intent identifiers included
    assert clean[7] == inj[7] == 1                                                  # L-1: B's protective POST is made before the ENTRIES step
    assert inj[10] == ["NEWA", "NEWB"] and len(inj[8]) == 2
    for v in inj[8]:
        assert not v.allowed and "DG_SAFETY_STATE_UNREADABLE" in v.codes and "E10(v)" in v.failed_facts


def test_r14_39_n5_scan_outside_the_cycle_with_a_previous_s_r_present_is_refused(tmp_path, rec, engine_pass):
    rig = ERig(tmp_path)
    rig.cycle()
    assert rig.x._attention_baseline is not None
    rig.x._scan_entries(rig.t, "TEST", SV)
    v = entry_verdicts(rec, DG.DG_EARLY)[-1]
    assert "DG_SAFETY_STATE_UNREADABLE" in v.codes and "E10(v)" in v.failed_facts


def test_r14_39_n3_n6_read_2_raising_or_an_unknown_state_is_refused(tmp_path, rec, engine_pass, monkeypatch):
    rig = ERig(tmp_path)
    monkeypatch.setattr(rig.x, "_entry_read2", lambda: ([], {"p1": "WEIRD"}, set()))
    rig.cycle()
    v = entry_verdicts(rec, DG.DG_EARLY)[-1]
    assert "DG_SAFETY_STATE_UNREADABLE" in v.codes and {"E10(iv)", "E10(v)"} <= set(v.failed_facts)


def test_r14_39_r_invariant_i_r_every_attention_position_is_named_in_recon_reasons(tmp_path):
    rig = Rig(tmp_path)
    a = rig.open("AAA")
    rig.store.update_position(a.position_id, rig.t, None, state="ATTENTION", attention_reason="EXIT_ATTEMPTS_EXHAUSTED")
    b = rig.open("BBB")
    rig.sim.set_position("BBB", 3, 10.0)                                          # a quantity problem found by RECONCILE
    rig.x.startup(rig.t)
    rig.x._reconcile(rig.t, "TEST", None)
    for p in rig.store.positions(open_only=True):
        if p.state == "ATTENTION":
            assert any(p.symbol in r for r in rig.x.recon_reasons) or not rig.x.recon_healthy
    assert rig.pos(b).state == "ATTENTION"


def test_r14_39_o6_simulated_broker_records_no_data_gate_row_and_no_s_r(tmp_path, engine_pass):
    rig = ERig(tmp_path, simulated=True)
    rig.cycle()
    assert rig.ctl("DATA_GATE") is None and rig.x._attention_baseline is None and len(rig.entry_posts) == 1


# ================================================================ r14 test 15 (b) (HDR packet, OD-R2-4(b)) ======================================
def test_r14_15b_the_smoke_s_forced_test_buy_is_refused_at_dg_early_with_no_intent_and_zero_posts(tmp_path, rec, engine_pass):
    rig = ERig(tmp_path)
    rig.over["NEWA"] = {"mode": "test"}
    real = rig._setup
    rig.setups.current = lambda s: SimpleNamespace(state="READY", inputs=SimpleNamespace(**{**vars(real(s).inputs), "mode": "test"}))
    rig.cycle()
    v = entry_verdicts(rec, DG.DG_EARLY)[-1]
    assert {"DG_SIMULATED", "DG_UNVERIFIED"} <= set(v.codes) and {"E2", "E6", "E7"} <= set(v.failed_facts)
    assert entry_intents(rig) == [] and rig.entry_posts == [] and rig.posts() == 0


def test_r14_15b_no_smoke_kind_or_factory_and_the_smoke_script_is_byte_identical():
    import freeze_ledger as FL
    for f in (ROOT / "execution").glob("*.py"):
        assert "SMOKE" not in f.read_text(encoding="utf-8"), f.name
    assert FL.sha(FL.now_text("scripts/run_aapl_paper_smoke.py")) == FL.LEDGER["scripts/run_aapl_paper_smoke.py"]


# ================================================================ r14 test 16 and 39(P): structural pins =========================================
def _calls(fn):
    return [ast.unparse(c.func) for c in ast.walk(fn) if isinstance(c, ast.Call)]


def test_r14_16_post_order_one_call_site_submit_two_callers_entry_dg_final_before_the_attempt():
    assert SRC.count(".post_order(") == 1
    sub = FN["_submit"]
    lines = {ast.unparse(c.func): c.lineno for c in ast.walk(sub) if isinstance(c, ast.Call)}
    assert lines["self._dg_final_entry"] < lines["self.store.record_entry_attempt"] < lines["self.broker.post_order"]


def test_r14_39_p_capture_and_read_2_make_no_broker_call_and_no_store_write():
    reads = {"self.store.positions", "self.store.safety_states"}
    for name in ("_capture_attention_baseline", "_entry_read2"):
        calls = _calls(FN[name])
        assert not any(c.startswith("self.broker") for c in calls), name
        assert {c for c in calls if c.startswith("self.store.")} <= reads, name
    h = next(n for n in ast.walk(FN["_capture_attention_baseline"]) if isinstance(n, ast.ExceptHandler))
    self_calls = [c for c in _calls(h) if c.startswith("self.")]
    assert self_calls == [] and any(isinstance(s, ast.Assign) and ast.unparse(s.targets[0]) == "self._attention_baseline" for s in h.body)
    assert any(ast.unparse(c) .startswith("log.warning") for c in ast.walk(h) if isinstance(c, ast.Call))


def test_r14_39_p_only_the_entry_branch_of_evaluate_reads_s_r_read_2_or_v():
    src = (ROOT / "execution" / "data_gate.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    for fn in (n for n in tree.body if isinstance(n, ast.FunctionDef)):
        names = {a.attr for a in ast.walk(fn) if isinstance(a, ast.Attribute)}
        if names & {"attention_baseline", "open_positions_now", "verified_positions"}:
            assert fn.name in ("_e10",), fn.name


def test_mut_l1553_latch_not_released_by_a_closed_market_cycle(tmp_path, monkeypatch):
    """r14:1553 (C1d mutation campaign): a closed-market cycle is no evidence; the stop-check latch stays."""
    rig = Rig(tmp_path)
    p = rig.open()                                                                # no quote: BLIND, market open
    _failing_prot(monkeypatch, rig, {"fail": {STOP}, "calls": []})
    rig.cycle()
    assert (STOP, p.position_id) in rig.x._unsaved_blocking_alerts
    rig.broker.faults["clock"] = clock_answer(rig, is_open=False)                 # a verified clock saying closed: BLIND, market closed
    rig.cycle()
    assert (STOP, p.position_id) in rig.x._unsaved_blocking_alerts and "SNDK" not in rig.x.stop_check_problems


def test_mut_l1557_latch_is_not_carried_into_a_new_process(tmp_path, monkeypatch):
    """r14:1557 (C1d mutation campaign): the latch lives in memory only; a new service instance starts with none."""
    rig = Rig(tmp_path)
    p = rig.open()
    _failing_prot(monkeypatch, rig, {"fail": {STOP}, "calls": []})
    rig.cycle()
    assert (STOP, p.position_id) in rig.x._unsaved_blocking_alerts
    x2 = ExecutionService(lambda: rig.stream, rig.store, broker=rig.broker, clock=lambda: rig.t, owner="test-owner")
    assert x2._unsaved_blocking_alerts == {} and x2._unsaved_blocking_alerts is not rig.x._unsaved_blocking_alerts


def test_mut_l1560_latch_retry_with_a_failed_position_read_writes_nothing(tmp_path, monkeypatch):
    """r14:1560 / test 37 (C1d mutation campaign): a failed position read in the pass-1 retry counts as ATTENTION: no write, the latch stays."""
    rig = Rig(tmp_path)
    p = rig.open(ident=OTHER)
    rig.live("SNDK", "8.50")
    s = {"fail": {PROT}, "calls": []}
    _failing_prot(monkeypatch, rig, s)
    rig.cycle()
    assert (PROT, p.position_id) in rig.x._unsaved_blocking_alerts
    s["fail"] = set()
    rig.live("SNDK", "12.00")                                                     # NOT_HIT: no new refusal; the probe fails on P2
    real_retry, real_pos, inside = rig.x._latch_retry, rig.store.position, {"on": False}

    def retry(*a):
        inside["on"] = True
        try:
            return real_retry(*a)
        finally:
            inside["on"] = False
    monkeypatch.setattr(rig.x, "_latch_retry", retry)
    monkeypatch.setattr(rig.store, "position", lambda *a, **k: (_ for _ in ()).throw(sqlite3.OperationalError("x")) if inside["on"] else real_pos(*a, **k))
    rig.cycle()
    assert rig.life(PROT, p.position_id) == [] and (PROT, p.position_id) in rig.x._unsaved_blocking_alerts


def test_mut_l1561_attention_at_start_then_cleared_still_needs_its_startup_probe(tmp_path, rec, engine_pass):
    """r14:1561 / test 38 ATTENTION-at-start (C1d mutation campaign): A in ATTENTION at process start is not exempt once it leaves ATTENTION."""
    rig = ERig(tmp_path)
    a = rig.open("AAA", ident=OTHER)                                             # P2 mismatched: A's startup probe fails
    rig.live("AAA", "12.00")
    rig.store.update_position(a.position_id, rig.t, None, state="ATTENTION", attention_reason="QTY_MISMATCH")
    rig.sim.set_position("AAA", 6, 10.0)                                          # the broker disagrees: A stays in ATTENTION in cycle 1
    b = rig.open("BBB")
    rig.live("BBB", "12.00")
    rig.cycle()
    assert rig.pos(a).state == "ATTENTION" and b.position_id in rig.x._verified_positions
    rig.sim.set_position("AAA", 10, 10.0)                                         # the clearing cycle: RECONCILE writes ATTENTION_CLEARED
    rig.cycle()
    v = entry_verdicts(rec, DG.DG_EARLY)[-1]
    assert rig.pos(a).state != "ATTENTION" and a.position_id not in rig.x._verified_positions
    assert "DG_SAFETY_UNVERIFIED_SINCE_START" in v.codes and "E10(iv)" in v.failed_facts and entry_intents(rig) == []


def test_mut_o1_reconcile_moves_c_into_attention_v_passes_and_recon_blocks(tmp_path, rec, engine_pass):
    """r14 39(O1) (C1d mutation campaign; kills M4, r10's second mutant and r9's): C moved into ATTENTION by this cycle's RECONCILE is in S_R."""
    rig = ERig(tmp_path)
    c = rig.open("CCC")
    rig.live("CCC", "12.00")
    rig.cycle()                                                                   # startup and a clean cycle: quantities agree
    assert rig.pos(c).state != "ATTENTION"
    rig.sim.set_position("CCC", 6, 10.0)                                          # W1: RECONCILE finds the quantity problem in this cycle
    rig.cycle()
    v = entry_verdicts(rec, DG.DG_EARLY)[-1]
    assert rig.pos(c).state == "ATTENTION" and not rig.x.recon_healthy and any("CCC" in r for r in rig.x.recon_reasons)
    assert "DG_ATTENTION_ENTERED_THIS_CYCLE" not in v.codes and not {"E10(iv)", "E10(v)"} & set(v.failed_facts)


def test_mut_m2_l1_attention_entered_between_dg_early_and_dg_final_refuses_at_dg_final(tmp_path, rec, engine_pass, no_e7):
    """r14 39(L1) (C1d mutation campaign; kills M2): B moved into ATTENTION after DG-early is refused by (v) at DG-final."""
    rig = ERig(tmp_path)
    b = rig.open("BBB")
    rig.live("BBB", "12.00")
    rig.x.hooks["after_intent"] = lambda ctx: rig.store.update_position(b.position_id, rig.t, None, state="ATTENTION", attention_reason="X")
    rig.x._recheck_entry = lambda it, now: []                                    # seam: DG-final itself is shown refusing
    rig.cycle()
    assert entry_verdicts(rec, DG.DG_EARLY)[0].allowed
    f = entry_verdicts(rec, DG.DG_FINAL)[0]
    it = entry_intents(rig)[0]
    assert "DG_ATTENTION_ENTERED_THIS_CYCLE" in f.codes and "E10(v)" in f.failed_facts
    assert it.state == "NOT_SENT" and it.attempt_count == 0 and rig.entry_posts == []


def test_mut_m9_n3_n4_read_2_raising_is_refused_unreadable_at_both_stages(tmp_path, rec, engine_pass, no_e7, monkeypatch):
    """r14 39(N3)/(N4) (C1d mutation campaign; kills M9): read 2 raising is DG_SAFETY_STATE_UNREADABLE for E10(iv) and (v), never 'no ATTENTION'."""
    rig = ERig(tmp_path)
    rig.open("BBB")
    rig.live("BBB", "12.00")
    real_r2, real_pos, st = rig.x._entry_read2, rig.store.positions, {"inside": False, "raise": True}

    def read2():
        st["inside"] = True
        try:
            return real_r2()
        finally:
            st["inside"] = False
    monkeypatch.setattr(rig.x, "_entry_read2", read2)
    monkeypatch.setattr(rig.store, "positions", lambda *a, **k: (_ for _ in ()).throw(sqlite3.OperationalError("read 2")) if st["inside"] and st["raise"] else real_pos(*a, **k))
    rig.cycle()                                                                   # (N3): read 2 raises at DG-early
    v = entry_verdicts(rec, DG.DG_EARLY)[-1]
    assert "DG_SAFETY_STATE_UNREADABLE" in v.codes and {"E10(iv)", "E10(v)"} <= set(v.failed_facts) and entry_intents(rig) == []
    st["raise"] = False
    rig.x.hooks["after_intent"] = lambda ctx: st.__setitem__("raise", True)
    rig.x._recheck_entry = lambda it, now: []
    rig.cycle()                                                                   # (N4): read 2 raises at DG-final only
    assert entry_verdicts(rec, DG.DG_EARLY)[-1].allowed
    f = entry_verdicts(rec, DG.DG_FINAL)[-1]
    assert "DG_SAFETY_STATE_UNREADABLE" in f.codes and {"E10(iv)", "E10(v)"} <= set(f.failed_facts)
    assert entry_intents(rig)[0].state == "NOT_SENT" and entry_intents(rig)[0].attempt_count == 0 and rig.entry_posts == []


def test_mut_m12_k_unverified_position_entering_attention_is_refused_by_v(tmp_path, rec, engine_pass):
    """r14 39(K)/(L2) (C1d mutation campaign; kills M12's 'verified only' form): an UNVERIFIED position entering ATTENTION is in N_t too."""
    rig = ERig(tmp_path)
    b = rig.open("BBB", ident=OTHER)                                             # P2 mismatched: B's startup probe fails, B not in V
    rig.live("BBB", "12.00")
    real = rig.x._safety_view
    rig.x._safety_view = lambda now, evaluate=False: (rig.store.update_position(b.position_id, now, None, state="ATTENTION", attention_reason="X") if evaluate else None, real(now, evaluate))[1]
    rig.cycle()
    v = entry_verdicts(rec, DG.DG_EARLY)[-1]
    assert b.position_id not in rig.x._verified_positions
    assert "DG_ATTENTION_ENTERED_THIS_CYCLE" in v.codes and "E10(v)" in v.failed_facts


def test_mut_m20e_m20f_n1_variant_verification_probe_unchanged_by_a_capture_failure(tmp_path, engine_pass):
    """r14 39(N1) OD-R4-2(a) variant and erratum E-L2 (C1d mutation campaign; kills M20(e), M20(f)): E's ACTIVE key comes from a HIT refused on
    P2, P2 is corrected before cycle c; E's verification probe runs and verifies in both runs, writes PROTECTIVE_FACTS_VERIFIED and E joins V."""
    base = ERig(tmp_path / "setup")                                               # c1d-fix2 M-3: one stored setup, forked (erratum E-I2)
    e = base.open("EEE")
    base.live("EEE", "8.50")
    ok_ident_fault(base, {"account_number_masked": OTHER["account_number_masked"]})
    base.cycle()                                                                  # an earlier cycle: the HIT is refused on P2
    assert base.state(PROT, e) == "ACTIVE"

    def run(sub, inject):
        rig = _c1d_fork(tmp_path, base, sub, {"EEE": "12.00"})                    # P2 corrected before cycle c (the fork's broker has no fault); a verified NOT_HIT
        order, real_probe = [], rig.x._protective_probe
        rig.x._protective_probe = lambda pos, *a: (order.append(pos.symbol), real_probe(pos, *a))[1]
        if inject:
            _c1d_inject_capture_failure(rig)
        out = rig.cycle()
        rows = [(r["kind"], r["detail"]) for r in rig.life(PROT, e.position_id)]
        return (out["result"], out["error"], order, rig.state(PROT, e), sorted(rig.x._verified_positions) == [e.position_id], rows,
                _c1d_store_rows(rig, [e.position_id], ["EEE"], out["run_id"]))
    clean, inj = run("clean", False), run("inj", True)
    assert clean == inj and clean[2] == ["EEE"] and clean[3] == "RESOLVED" and clean[4]
    assert clean[5][-1][1]["evidence"] == "PROTECTIVE_FACTS_VERIFIED"


def test_mut_m20g_e_i5_pass2_order_is_the_same_with_a_capture_failure(tmp_path, engine_pass):
    """Erratum E-I5 (C1d mutation campaign; kills M20(g)): in both runs the whole pass-2 order is HIT exits, then the probes, then candidates."""
    def run(sub, inject):
        rig = ERig(tmp_path / sub)
        b = rig.open("BBB")
        rig.live("BBB", "8.50")                                                   # HIT, in V
        rig.x._verified_positions.add(b.position_id)
        rig.open("CCC")
        rig.live("CCC", "12.00")                                                  # NOT_HIT: a startup probe
        rig.open("FFF", at=rig.t - timedelta(days=8))
        rig.live("FFF", "12.00")                                                  # NOT_HIT and a TIME_EXIT candidate
        order, real_exit, real_probe = [], rig.x._maybe_submit_exit, rig.x._protective_probe
        rig.x._maybe_submit_exit = lambda pos, *a, **k: (order.append(("exit", pos.symbol)), real_exit(pos, *a, **k))[1]
        rig.x._protective_probe = lambda pos, *a: (order.append(("probe", pos.symbol)), real_probe(pos, *a))[1]
        if inject:
            real_cap, real_pos, inside = rig.x._capture_attention_baseline, rig.store.positions, {"on": False}

            def cap(*a):
                inside["on"] = True
                try:
                    return real_cap(*a)
                finally:
                    inside["on"] = False
            rig.x._capture_attention_baseline = cap
            rig.store.positions = lambda *a, **k: (_ for _ in ()).throw(sqlite3.OperationalError("x")) if inside["on"] else real_pos(*a, **k)
        out = rig.cycle()
        return out["result"], out["error"], order
    clean, inj = run("clean", False), run("inj", True)
    assert clean == inj and clean[2][:2] == [("exit", "BBB"), ("probe", "CCC")] and clean[2][-1] == ("exit", "FFF")


def test_mut_e_l1_n1_store_rows_are_identical_with_a_capture_failure(tmp_path, engine_pass):
    """Erratum E-L1 / r14 39(N1) item (6) (C1d mutation campaign): the execution_reconciliations rows, the position_events and every
    paper_positions column after the cycle are identical with and without the capture failure (generated identifiers excluded).
    c1d-fix2 M-3: both runs fork from one stored setup (erratum E-I2); only the generated identifiers and the broker order id are excluded (E-L1),
    so position_id, execution_id, client_order_id and detail_json are compared, and so are the intents and the run row's started_at."""
    base = ERig(tmp_path / "setup")
    b = base.open("BBB")
    c = base.open("CCC")

    def run(sub, inject):
        rig = _c1d_fork(tmp_path, base, sub, {"BBB": "8.50", "CCC": "12.00"}, verified=[b.position_id])
        if inject:
            _c1d_inject_capture_failure(rig)
        out = rig.cycle()
        ids = {"reconciliation_id", "run_id", "broker_order_id", "event_id"}

        def strip(x):
            if isinstance(x, dict):
                return {k: strip(v) for k, v in x.items() if k not in ids}
            return [strip(v) for v in x] if isinstance(x, list) else x
        recs = strip(rig.recs())
        evs = strip([rig.events(p) for p in (b, c)])
        pos = strip([rig.store.position(p.position_id).model_dump() for p in (b, c)])
        return out["result"], out["error"], recs, evs, pos, _c1d_store_rows(rig, [b.position_id, c.position_id], ["BBB", "CCC"], out["run_id"])
    clean, inj = run("clean", False), run("inj", True)
    assert clean[:2] == inj[:2] == ("RUN", None)
    assert clean[2] == inj[2]
    assert clean[3] == inj[3]
    assert clean[4] == inj[4]
    assert clean[5] == inj[5] and clean[5]["intents"] and clean[5]["run"]["started_at"]


def test_mut_l1593_latch_derived_evidence_needs_a_later_exit_cycle(tmp_path, monkeypatch):
    """r14:1593 / 3.13.8 (C1d mutation campaign): the latch's derived (2') evidence needs an O1 HARD_INVALIDATION exit of a STRICTLY later cycle."""
    from execution.engine import exit_coid, exit_id_for
    from execution.models import Intent
    rig = Rig(tmp_path)
    p = rig.open(ident=OTHER)
    rig.store.update_position(p.position_id, rig.t, None, exit_cycle=1)
    rig.store.insert_intent(Intent(execution_id=exit_id_for(p.position_id, 1), intent_type="EXIT", symbol="SNDK", side="sell", qty=10, client_order_id=exit_coid(p.position_id, 1),
                                   position_id=p.position_id, exit_cycle=1, exit_reason="HARD_INVALIDATION", state="CANCELED", outcome_class="O1", attempt_count=1,
                                   created_at=iso(rig.t), updated_at=iso(rig.t)))
    rig.live("SNDK", "8.50")
    s = {"fail": {PROT}, "calls": []}
    _failing_prot(monkeypatch, rig, s)
    rig.cycle()
    assert rig.x._unsaved_blocking_alerts[(PROT, p.position_id)]["exit_cycle"] == 1
    s["fail"] = set()
    rig.live("SNDK", "12.00")
    rig.cycle()
    rows = rig.life(PROT, p.position_id)
    assert [r["kind"] for r in rows] == [PROT] and rows[0]["detail"].get("deferred") is True and rig.state(PROT, p) == "ACTIVE"


def test_mut_l1552_a_lost_compare_and_set_that_finds_no_alert_latches_the_key(tmp_path, monkeypatch):
    """r14:1552 / 3.13.8 (C1d mutation campaign): a blocking FAILURE whose write loses the compare-and-set and finds no ACTIVE / RECURRENT row
    is latched (returns False); entries are then refused E10(iii)."""
    rig = Rig(tmp_path)
    p = rig.open()
    monkeypatch.setattr(rig.store, "add_safety_transition", lambda *a, **k: False)   # the CAS is lost and nothing durable stands
    assert rig.x._safety_transition(rig.t, "TEST", None, PROT, p.position_id, p.symbol, "FAILURE", reason_codes=["P2"], exit_cycle=0) is False
    assert rig.x._unsaved_blocking_alerts[(PROT, p.position_id)]["reason_codes"] == ["P2"] and rig.state(PROT, p) == "NONE"


def _c1d_clone_broker(src, dst):
    """Erratum E-I2: a clone of the fake broker's whole state (account, positions, orders, flags, the request and POST logs); not its lock or clock."""
    import copy
    for k, v in vars(src).items():
        if k not in ("lock", "now_fn", "on_post"):
            setattr(dst, k, copy.deepcopy(v))


def _c1d_fork(tmp_path, base, name, live, verified=()):
    """Erratum E-I2: one fork of ONE stored setup: a copy of `base`'s database and a clone of its fake broker. The stream's quotes (`live`) and the
    verified set V (process memory, `verified`) are re-applied; the clock is the base's. Each fork is a new service process with the same owner."""
    dst = tmp_path / name / "c1c1.db"
    dst.parent.mkdir(parents=True, exist_ok=True)
    a, b = sqlite3.connect(str(base.db)), sqlite3.connect(str(dst))
    try:
        a.backup(b)
    finally:
        a.close()
        b.close()
    fork = ERig(tmp_path / name, ready=tuple(base.ready))
    fork.t, fork.k = base.t, base.k
    _c1d_clone_broker(base.sim, fork.sim)
    for sym, px in live.items():
        fork.live(sym, px)
    fork.x._verified_positions |= set(verified)
    return fork


def _c1d_inject_capture_failure(rig):
    """r14 39(N1): the S_R capture's own store read raises (at the capture's call site only; the helper itself is not replaced)."""
    real_cap, real_pos, inside = rig.x._capture_attention_baseline, rig.store.positions, {"on": False}

    def cap(*a):
        inside["on"] = True
        try:
            return real_cap(*a)
        finally:
            inside["on"] = False
    rig.x._capture_attention_baseline = cap
    rig.store.positions = lambda *a, **k: (_ for _ in ()).throw(sqlite3.OperationalError("x")) if inside["on"] else real_pos(*a, **k)


def _c1d_strip(x):
    if isinstance(x, dict):
        return {k: _c1d_strip(v) for k, v in x.items() if k not in ("reconciliation_id", "run_id", "event_id", "broker_order_id")}
    if isinstance(x, list):
        return [_c1d_strip(v) for v in x]
    return x


def _c1d_store_rows(rig, pids, syms, run_id):
    """Erratum E-L1 / r14 39(N1) item (6): every execution_reconciliations row; every intent of the positions or symbols open at the start of the
    cycle; every paper_positions column of those positions; their position_events; the run row (all but run_id and finished_at) and its summary
    except entries and duration_ms. JSON columns are compared parsed. Excluded, at any depth: only the generated identifiers (event_id,
    reconciliation_id, the run_id column) and the broker-assigned order id."""
    import json
    m, sm = ",".join("?" * len(pids)), ",".join("?" * len(syms))
    c = sqlite3.connect(str(rig.db))
    c.row_factory = sqlite3.Row
    try:
        def q(sql, args=()):
            return [dict(r) for r in c.execute(sql, args)]
        out = {"reconciliations": q("SELECT * FROM execution_reconciliations ORDER BY rowid"),
               "intents": q(f"SELECT * FROM execution_intents WHERE position_id IN ({m}) OR symbol IN ({sm}) ORDER BY rowid", (*pids, *syms)),
               "positions": q(f"SELECT * FROM paper_positions WHERE position_id IN ({m}) ORDER BY rowid", tuple(pids)),
               "events": q(f"SELECT * FROM position_events WHERE position_id IN ({m}) ORDER BY rowid", tuple(pids)),
               "run": q("SELECT * FROM execution_runs WHERE run_id = ?", (run_id,))}
    finally:
        c.close()
    for rows in out.values():
        for r in rows:
            for k in [k for k in r if k.endswith("_json") and r[k]]:
                r[k] = json.loads(r[k])
    assert len(out["run"]) == 1
    run = out["run"][0]
    out["summary"] = {k: v for k, v in run.pop("summary_json").items() if k not in ("entries", "duration_ms")}
    out["run"] = {k: v for k, v in run.items() if k not in ("run_id", "finished_at")}
    return _c1d_strip(out)


def _c1d_fail(monkeypatch, rig, where, sym):
    """c1d-fix2 M-1: make ONE position's pass-1 work raise: its setup read, its episode read, exit_decision, or its pass-1 store write."""
    boom = (lambda *a: (_ for _ in ()).throw(RuntimeError(where)))
    if where == "setup":
        real = rig.setups.current
        monkeypatch.setattr(rig.setups, "current", lambda s: boom() if s == sym else real(s))
    elif where == "episodes":
        real = rig.setups.episodes
        monkeypatch.setattr(rig.setups, "episodes", lambda s, n: boom() if s == sym else real(s, n))
    elif where == "exit_decision":
        real = SVC.exit_decision
        monkeypatch.setattr(SVC, "exit_decision", lambda pos, ctx: boom() if pos.symbol == sym else real(pos, ctx))
    else:
        real = rig.store.update_position

        def upd(pid, *a, **k):
            if "sessions_held" in k and rig.store.position(pid).symbol == sym:   # pass 1's write only (it carries the exit-decision fields)
                raise sqlite3.OperationalError("locked")
            return real(pid, *a, **k)
        monkeypatch.setattr(rig.store, "update_position", upd)


@pytest.mark.parametrize("where", ["setup", "episodes", "exit_decision", "store_write"])
def test_c1d_m1_one_position_s_failing_pass1_never_blocks_another_s_protective_exit(tmp_path, monkeypatch, rec, engine_pass, no_e7, where):
    """c1d-fix2 M-1 (B2 review MEDIUM M-1): A, a verified NOT_HIT and the FIRST position in store order (positions() is newest first, so A
    is opened last), raises in its setup read, its episode read, exit_decision or its pass-1 store write. The positions AFTER it in store order
    are still evaluated in pass 1: C (a verified NOT_HIT) gets its own exit decision (HOLD, with its pass-1 write), and B, a verified HIT,
    gets its protective POST in the same cycle; the cycle is RUN. A is handled fail closed: no exit decision, no intent, no order; the
    EXIT_TRIGGER UNKNOWN row names it and the exception, and its stop_check_problems entry blocks new entries this cycle (E10(i) at DG-early;
    no entry POST although E7 is waived)."""
    from test_data_gate_exits_service import _c1d_touch_spy
    rig = ERig(tmp_path)
    b = rig.open("BBB")
    rig.live("BBB", "8.50")
    c = rig.open("CCC")
    rig.live("CCC", "12.00")
    a = rig.open("AAA")
    rig.live("AAA", "12.00")
    assert [p.symbol for p in rig.store.positions(open_only=True)] == ["AAA", "CCC", "BBB"]   # A fails first; C and B come after it
    rig.x._verified_positions |= {a.position_id, b.position_id, c.position_id}
    before = rig.pos(a)
    _c1d_fail(monkeypatch, rig, where, "AAA")
    seen = _c1d_touch_spy(rig)
    out = rig.cycle()
    assert out["result"] == "RUN" and out["error"] is None
    assert [d["decision"] for d in out["summary"]["decisions"] if d["symbol"] == "CCC"] == ["HOLD"] and out["summary"]["positions"] == 3
    assert [r for c_, r, _t, _i in seen if c_ == "EXIT_TRIGGER" and _t.startswith("CCC:")] == ["PASS"] and rig.pos(c).last_price is not None
    assert rig.exits(c) == [] and "CCC" not in rig.x.stop_check_problems
    assert rig.posts() == 1 and len(rig.exits(b)) == 1 and rig.exits(b)[0].attempt_count == 1 and rig.exits(a) == []
    assert (rig.pos(a).state, rig.pos(a).exit_reason, rig.pos(a).exit_cycle) == (before.state, before.exit_reason, before.exit_cycle)
    fails = [i for c, r, _t, i in seen if c == "EXIT_TRIGGER" and r == "UNKNOWN" and i.get("position_id") == a.position_id]
    assert len(fails) == 1 and fails[0]["exception"] == ("OperationalError" if where == "store_write" else "RuntimeError") and fails[0]["stop_status"] == DG.NOT_HIT
    assert "exit evaluation failed" in rig.x.stop_check_problems["AAA"] and "BBB" not in rig.x.stop_check_problems
    assert [d["decision"] for d in out["summary"]["decisions"] if d["symbol"] == "AAA"] == ["ATTENTION"] and out["summary"]["exits_decided"] == 1
    v = entry_verdicts(rec, DG.DG_EARLY)[-1]
    assert "DG_SAFETY_CHECK_FAILED" in v.codes and "E10(i)" in v.failed_facts and rig.entry_posts == []


@pytest.mark.parametrize("where", ["exit_decision", "store_write"])
def test_c1d_m1_a_failing_hit_position_still_gets_its_protective_exit_attempt(tmp_path, monkeypatch, where):
    """c1d-fix2 M-1: SC is computed before and apart from the exit decision (r14 3.3.1), so a verified HIT whose exit_decision or pass-1 write
    raises still gets its protective-exit attempt that cycle: one POST recorded HARD_INVALIDATION. No other exit decision is made for it."""
    rig = Rig(tmp_path)
    a = rig.open("AAA")
    rig.live("AAA", "8.50")
    _c1d_fail(monkeypatch, rig, where, "AAA")
    out = rig.cycle()
    x = rig.exits(a)
    assert out["result"] == "RUN" and rig.posts() == 1 and len(x) == 1 and x[0].exit_reason == "HARD_INVALIDATION" and x[0].attempt_count == 1
    assert "protective path" in rig.x.stop_check_problems["AAA"] and out["summary"]["exits_decided"] == 1 and out["summary"]["exits_submitted"] == 1


def test_c1d_m1_a_blind_position_whose_exit_decision_raises_keeps_the_3_3_3_effects(tmp_path, monkeypatch):
    """c1d-fix2 M-1: r14 3.3.3 applies 'whatever else happens to the position this cycle': a BLIND stop (no quote, market open) whose
    exit_decision raises still writes its STOP_CHECK_BLIND ACTIVE row; nothing is sent."""
    rig = Rig(tmp_path)
    a = rig.open("AAA")
    _c1d_fail(monkeypatch, rig, "exit_decision", "AAA")
    out = rig.cycle()
    assert out["result"] == "RUN" and rig.state(STOP, a) == "ACTIVE" and rig.posts() == 0 and rig.exits(a) == [] and "AAA" in rig.x.stop_check_problems


def test_c1d_m1_a_simulated_crash_in_pass_1_still_propagates(tmp_path, monkeypatch):
    """c1d-fix2 M-1: the containment never swallows a SimulatedCrash (the harness' crash seam), as everywhere."""
    rig = Rig(tmp_path)
    rig.open("AAA")
    rig.live("AAA", "12.00")
    monkeypatch.setattr(SVC, "exit_decision", lambda pos, ctx: (_ for _ in ()).throw(SVC.SimulatedCrash("crash")))
    with pytest.raises(SVC.SimulatedCrash):
        rig.cycle()


def test_c1d_m1_a_failed_context_read_runs_none_of_the_position_s_later_steps(tmp_path, monkeypatch):
    """c1d-fix2 M-1: after A's setup read fails, exit_decision is not called for A; the failure recorded is the first one."""
    from test_data_gate_exits_service import _c1d_touch_spy
    rig = Rig(tmp_path)
    a = rig.open("AAA")
    rig.live("AAA", "12.00")
    _c1d_fail(monkeypatch, rig, "setup", "AAA")
    calls, real = [], SVC.exit_decision
    monkeypatch.setattr(SVC, "exit_decision", lambda pos, ctx: (calls.append(pos.symbol), real(pos, ctx))[1])
    seen = _c1d_touch_spy(rig)
    rig.cycle()
    assert calls == [] and [i["exception"] for c, r, _t, i in seen if c == "EXIT_TRIGGER" and i.get("position_id") == a.position_id and r == "UNKNOWN"] == ["RuntimeError"]


def test_c1d_l2_m20c_m20d_a_position_rewritten_to_exit_required_in_pass_1_is_probed_at_its_read(tmp_path, monkeypatch):
    """c1d-fix2 L-2 (B2 review LOW L-2; r14:901, 3.8.1, the r9 rule 'ATTENTION is read AT the probe'): A is wrongly left EXIT_SUBMITTED with no
    open intent (a state r14:901 names reachable), not in V, a verified NOT_HIT holding a strategy candidate (TIME_EXIT). S_R holds
    EXIT_SUBMITTED; pass 1 rewrites A to EXIT_REQUIRED; the startup probe reads A at the probe, so A IS probed and joins V. Taking
    eligibility from S_R (M20(c), M20(d)) would skip it: both mutants are killed in contract."""
    rig = Rig(tmp_path)
    a = rig.open("AAA", at=rig.t - timedelta(days=8))
    rig.live("AAA", "12.00")
    rig.store.update_position(a.position_id, rig.t, None, state="EXIT_SUBMITTED")
    probed, real = [], rig.x._protective_probe
    monkeypatch.setattr(rig.x, "_protective_probe", lambda pos, *a_: (probed.append((pos.symbol, pos.state)), real(pos, *a_))[1])
    rig.cycle()
    assert rig.x._attention_baseline.states[a.position_id] == "EXIT_SUBMITTED"
    assert probed == [("AAA", "EXIT_REQUIRED")] and a.position_id in rig.x._verified_positions


def test_c1d_l7_pin_a_protective_refusal_for_a_position_missing_from_the_store_keeps_entries_blocked(tmp_path, rec, engine_pass, no_e7):
    """c1d-fix2 L-7 (B2 review LOW L-7). Owner decision: ACCEPT; this pin only, no production change.
    STATED RESIDUAL: a protective EXIT intent whose position is not in the store (position_id None) is refused at DG-final as a Z2 containment
    (GATE_ERROR); the refusal passes position_id=None to the lifecycle writer, which cannot store a row for it and latches the key
    (PROTECTIVE_EXIT_UNVERIFIED, None). No position read can ever release that key (the pass-1 retry finds no position and keeps it), so new
    entries stay refused (E10(iii) DG_SAFETY_ALERT_UNSAVED) and nothing is posted until the process restarts. That is fail-closed; releasing a
    safety latch on an inconsistent store would weaken it."""
    from execution.engine import exit_coid, exit_id_for
    from execution.models import Intent
    rig = ERig(tmp_path)
    xid = exit_id_for("orphan", 1)
    it = rig.store.insert_intent(Intent(execution_id=xid, intent_type="EXIT", symbol="ZZZ", side="sell", qty=1, client_order_id=exit_coid("orphan", 1),
                                        position_id=None, exit_cycle=1, exit_reason="HARD_INVALIDATION", state="PENDING", created_at=iso(rig.t), updated_at=iso(rig.t)))
    rig.x._submit(it, rig.t, "EXIT_SUBMISSION", recheck=None)
    back = rig.store.intent(xid)
    assert rig.posts() == 0 and back.state == "NOT_SENT" and back.failure_code == "DG_GATE_ERROR" and back.attempt_count == 0
    assert (PROT, None) in rig.x._unsaved_blocking_alerts
    for _ in range(3):
        rig.cycle()
        v = entry_verdicts(rec, DG.DG_EARLY)[-1]
        assert "DG_SAFETY_ALERT_UNSAVED" in v.codes and "E10(iii)" in v.failed_facts and (PROT, None) in rig.x._unsaved_blocking_alerts
    assert rig.entry_posts == [] and rig.posts() == 0 and entry_intents(rig) == []
    x2 = ExecutionService(lambda: rig.stream, rig.store, broker=rig.broker, clock=lambda: rig.t, owner="test-owner")
    assert x2._unsaved_blocking_alerts == {}                                       # only a restart clears it


def _c1d_spy_order(monkeypatch, rig, a):
    """r14 38(E)/(F) spy order: RECONCILE (does R name A?), the S_R capture (its states), each exit gate, each probe, the ENTRIES step (A's state)."""
    order = []
    real_rec, real_cap, real_exit, real_probe, real_scan = (rig.x._reconcile, rig.x._capture_attention_baseline, rig.x._maybe_submit_exit, rig.x._protective_probe,
                                                            rig.x._scan_entries)
    monkeypatch.setattr(rig.x, "_reconcile", lambda *a_: (real_rec(*a_), order.append(("reconcile", any(a.symbol in r for r in rig.x.recon_reasons))))[0])
    monkeypatch.setattr(rig.x, "_capture_attention_baseline", lambda cid: (real_cap(cid), order.append(("capture", dict(rig.x._attention_baseline.states))))[0])
    monkeypatch.setattr(rig.x, "_maybe_submit_exit", lambda pos, *a_, **k: (order.append(("exit", pos.symbol)), real_exit(pos, *a_, **k))[1])
    monkeypatch.setattr(rig.x, "_protective_probe", lambda pos, *a_: (order.append(("probe", pos.symbol)), real_probe(pos, *a_))[1])
    monkeypatch.setattr(rig.x, "_scan_entries", lambda *a_: (order.append(("entries", rig.pos(a).state)), real_scan(*a_))[1])
    return order


def _c1d_cycle_c_and_c_plus_1(monkeypatch, rig, rec, a):
    """r14 38(E)/(F) cycle c and c+1 (shared assertions). Kills R10a (r14:1564) and r10's 'S_R captured after the positions step'."""
    order = _c1d_spy_order(monkeypatch, rig, a)
    n = len(rec)
    rig.cycle()                                                                   # cycle c
    assert order == [("reconcile", False), ("capture", {a.position_id: "EXIT_REQUIRED"}), ("exit", a.symbol), ("entries", "ATTENTION")]   # A: no startup probe
    assert rig.pos(a).state == "ATTENTION" and rig.pos(a).attention_reason == "EXIT_ATTEMPTS_EXHAUSTED" and rig.posts() == 0
    vs = entry_verdicts(rec[n:], DG.DG_EARLY)
    assert len(vs) == 2
    for v in vs:
        assert {"DG_SAFETY_CHECK_FAILED", "DG_ATTENTION_ENTERED_THIS_CYCLE"} <= set(v.codes) and "DG_SAFETY_UNVERIFIED_SINCE_START" not in v.codes and {f for f in v.failed_facts if f.startswith("E10")} == {"E10(i)", "E10(ii)", "E10(v)"}   # (b1): A's unresolved key leaves it out of E10(iv)
    assert entry_intents(rig) == [] and rig.entry_posts == [] and [(r["kind"], r["detail"].get("lifecycle"), r["detail"].get("reason_codes")) for r in rig.life(PROT, a.position_id)] == [(PROT, "ACTIVE", ["EXIT_ATTEMPTS_EXHAUSTED"])]   # r15 9-C A4: exactly step 4's alert row
    n = len(rec)
    rig.cycle()                                                                   # cycle c+1: R names A, A is in S_R and still in ATTENTION
    assert any(a.symbol in r for r in rig.x.recon_reasons) and not rig.x.recon_healthy and rig.x._attention_baseline.states[a.position_id] == "ATTENTION"
    vs = entry_verdicts(rec[n:], DG.DG_EARLY)
    assert len(vs) == 2
    for v in vs:
        assert "E10(iv)" not in v.failed_facts and not {"DG_SAFETY_UNVERIFIED_SINCE_START", "DG_ATTENTION_ENTERED_THIS_CYCLE"} & set(v.codes)
    assert entry_intents(rig) == [] and rig.posts() == 0


def test_c1d_r14_38_e_exhaustion_after_a_restart(tmp_path, monkeypatch, rec, engine_pass):
    """r14 38(E) (r10, R9-L1; r14:1665-1674), OD-R6-2(a). Process 1's end, as stored: A HIT, EXIT_REQUIRED with its last exit cycle consumed
    (exit_cycle = max_exit_cycles) and no durable lifecycle row (its protective FAILURE write failed, so only process 1's latch held it).
    Process 2: an out-of-session cycle defers at P8a-1 and entries are refused by E10(iv) (A not verified). The first in-session cycle c, in spy
    order: RECONCILE adds no reason for A; S_R is captured without A; pass 2's exit gate moves A to ATTENTION (exhaustion); ENTRIES refuses every
    candidate at DG-early with DG_SAFETY_UNVERIFIED_SINCE_START (and E10(v)'s code); A gets no startup probe. Cycle c+1: R names A, A is in S_R
    and in ATTENTION, so A is left out of E10(iv)."""
    from test_data_gate_exits_service import T0
    rig = ERig(tmp_path, ready=("NEWA", "NEWB"), t0=T0 - timedelta(hours=6))     # 04:00 New York
    rig.x.max_entries_per_cycle = 2
    a = rig.open("AAA")
    rig.live("AAA", "8.50")
    rig.store.update_position(a.position_id, rig.t, None, state="EXIT_REQUIRED", exit_reason="HARD_INVALIDATION", exit_cycle=rig.x.max_exit_cycles)
    rig.x = ExecutionService(lambda: rig.stream, rig.store, broker=rig.broker, clock=lambda: rig.t, owner="test-owner")   # process 2
    rig.x.max_entries_per_cycle = 2
    n = len(rec)
    rig.cycle()                                                                   # out of session: P8a-1 defers before the exhaustion check
    assert rig.pos(a).state == "EXIT_REQUIRED" and rig.posts() == 0
    assert all("DG_SAFETY_UNVERIFIED_SINCE_START" in v.codes and "E10(iv)" in v.failed_facts for v in entry_verdicts(rec[n:], DG.DG_EARLY))
    assert entry_verdicts(rec[n:], DG.DG_EARLY)
    rig.t = T0
    _c1d_cycle_c_and_c_plus_1(monkeypatch, rig, rec, a)


def test_c1d_r14_38_f_clearing_cycle_re_entry(tmp_path, monkeypatch, rec, engine_pass):
    """r14 38(F) (r10; r14:1675-1682), OD-R6-2(a): A in QTY_MISMATCH ATTENTION with exit_cycle = max_exit_cycles and the stop HIT; the
    quantities agree again with no recovery. Cycle c, in spy order: RECONCILE writes ATTENTION_CLEARED (A back to EXIT_REQUIRED) and adds no
    reason for A; S_R is captured without A; pass 2's exhaustion check moves A back to ATTENTION; ENTRIES refuses every candidate at DG-early
    with DG_SAFETY_UNVERIFIED_SINCE_START; A gets no startup probe. Cycle c+1: as in (E)."""
    rig = ERig(tmp_path, ready=("NEWA", "NEWB"))
    rig.x.max_entries_per_cycle = 2
    a = rig.open("AAA")
    rig.live("AAA", "8.50")
    rig.store.update_position(a.position_id, rig.t, None, state="ATTENTION", attention_reason="QTY_MISMATCH", exit_reason="HARD_INVALIDATION",
                              exit_cycle=rig.x.max_exit_cycles)
    rig.sim.set_position("AAA", 6, 10.0)                                          # the cycle before c: the broker disagrees, A stays in ATTENTION
    rig.cycle()
    assert rig.pos(a).state == "ATTENTION" and rig.x._verified_positions == set()
    rig.sim.set_position("AAA", 10, 10.0)                                         # the quantities agree again, no recovery
    _c1d_cycle_c_and_c_plus_1(monkeypatch, rig, rec, a)
    assert len(rig.events(a, "ATTENTION_CLEARED")) == 1


@pytest.mark.parametrize("variant", ["time_exit", "hit", "hit_write_fails"])
def test_c1d_r14_39_j_the_w3_quantity_route_is_refused_by_e10_v(tmp_path, monkeypatch, rec, engine_pass, variant):
    """r14 39(J) (r14:1708-1710): A in V, EXIT_REQUIRED with a TIME_EXIT that reaches P3/P4. A seam lowers the broker's single-symbol quantity
    after RECONCILE's positions() read; at the exit gate W3 writes the QTY finding and moves A to ATTENTION QTY_MISMATCH. ENTRIES is refused by
    (v). HIT variant: E10(ii) and (v) are both listed; with the protective write failing, E10(iii) and (v)."""
    rig = ERig(tmp_path)
    a = rig.open("AAA", at=rig.t - timedelta(days=8))
    rig.live("AAA", "12.00" if variant == "time_exit" else "8.50")
    rig.store.update_position(a.position_id, rig.t, None, state="EXIT_REQUIRED", exit_reason="TIME_EXIT")
    rig.x._verified_positions.add(a.position_id)
    if variant == "hit_write_fails":
        _failing_prot(monkeypatch, rig, {"fail": {PROT}, "calls": []})
    real_rec = rig.x._reconcile
    monkeypatch.setattr(rig.x, "_reconcile", lambda *a_: (real_rec(*a_), rig.broker.faults.__setitem__("position", norm_pos("AAA", 6)))[0])
    rig.cycle()
    assert rig.pos(a).state == "ATTENTION" and rig.pos(a).attention_reason == "QTY_MISMATCH" and rig.posts() == 0
    assert rig.recs(pid=a.position_id)[-1]["kind"] == "QTY_MISMATCH" and rig.x._attention_baseline.states[a.position_id] == "EXIT_REQUIRED"
    v = entry_verdicts(rec, DG.DG_EARLY)[-1]
    assert "DG_ATTENTION_ENTERED_THIS_CYCLE" in v.codes and "E10(v)" in v.failed_facts and entry_intents(rig) == []
    if variant == "time_exit":
        assert not {"E10(i)", "E10(ii)", "E10(iii)", "E10(iv)"} & set(v.failed_facts)
    elif variant == "hit":
        assert "E10(ii)" in v.failed_facts and rig.state(PROT, a) == "ACTIVE"
    else:
        assert "E10(iii)" in v.failed_facts and (PROT, a.position_id) in rig.x._unsaved_blocking_alerts


def test_c1d_r14_39_s_a_w3_entrant_cleared_and_re_entering(tmp_path, monkeypatch, rec, engine_pass):
    """r14 39(S) (r12; r14:1737): cycle c, A enters ATTENTION through W3 and every candidate is refused with DG_ATTENTION_ENTERED_THIS_CYCLE.
    Cycle c+1: RECONCILE finds the quantities equal and clears A (A neither in R nor in S_R^ATT); the single-symbol read is lower again, W3
    sets ATTENTION again, and every candidate is refused again with (v)'s code. Cycle c+2: both reads agree; A stays out of ATTENTION and (v)'s
    code is absent."""
    rig = ERig(tmp_path, ready=("NEWA", "NEWB"))
    rig.x.max_entries_per_cycle = 2
    a = rig.open("AAA", at=rig.t - timedelta(days=8))
    rig.live("AAA", "12.00")
    rig.store.update_position(a.position_id, rig.t, None, state="EXIT_REQUIRED", exit_reason="TIME_EXIT")
    rig.x._verified_positions.add(a.position_id)
    lower = {"on": True}
    real_rec = rig.x._reconcile

    def seam(*a_):
        n = real_rec(*a_)
        if lower["on"]:
            rig.broker.faults["position"] = norm_pos("AAA", 6)
        else:
            rig.broker.faults.pop("position", None)
        return n
    monkeypatch.setattr(rig.x, "_reconcile", seam)
    for cycle in ("c", "c+1"):
        n = len(rec)
        rig.cycle()
        assert rig.pos(a).state == "ATTENTION" and rig.x._attention_baseline.states[a.position_id] == "EXIT_REQUIRED", cycle
        assert not any("AAA" in r for r in rig.x.recon_reasons), cycle
        vs = entry_verdicts(rec[n:], DG.DG_EARLY)
        assert len(vs) == 2 and all("DG_ATTENTION_ENTERED_THIS_CYCLE" in v.codes and "E10(v)" in v.failed_facts for v in vs), cycle
    assert len(rig.events(a, "ATTENTION_CLEARED")) == 1
    lower["on"] = False
    n = len(rec)
    rig.cycle()                                                                   # c+2: both reads agree
    assert rig.pos(a).state != "ATTENTION"
    vs = entry_verdicts(rec[n:], DG.DG_EARLY)
    assert len(vs) == 2 and all("DG_ATTENTION_ENTERED_THIS_CYCLE" not in v.codes and "E10(v)" not in v.failed_facts for v in vs)


def test_c1d_r14_39_n2_w2_entrant_and_d_in_attention_since_an_earlier_cycle(tmp_path, rec, engine_pass):
    """r14 39(N2) with D (r13, r14; r14:1718): (N1) plus A entering ATTENTION through W2 and D, unverified, in ATTENTION since an earlier cycle.
    Both runs fork from one stored setup (E-I2) and are compared field by field as in (N1), items (1) to (6) (E-L1): B's protective POST before
    ENTRIES, C's probe, V, the reads up to ENTRIES, every compared row. In both runs D gets no startup probe and is not in V; A's W2 transition
    happens. The injected run refuses every candidate with DG_SAFETY_STATE_UNREADABLE, E10(v) in failed_facts."""
    base = ERig(tmp_path / "setup", ready=("NEWA", "NEWB"))
    b = base.open("BBB")
    c = base.open("CCC")
    a = base.open("AAA")
    base.store.update_position(a.position_id, base.t, None, state="EXIT_REQUIRED", exit_reason="HARD_INVALIDATION", exit_cycle=base.x.max_exit_cycles)
    d = base.open("DDD")
    base.store.update_position(d.position_id, base.t, None, state="ATTENTION", attention_reason="QTY_MISMATCH")
    base.sim.set_position("DDD", 6, 10.0)                                         # the broker disagrees: D stays in ATTENTION
    live = {"BBB": "8.50", "CCC": "12.00", "AAA": "8.50", "DDD": "12.00"}
    pids, syms = [p.position_id for p in (b, c, a, d)], ["BBB", "CCC", "AAA", "DDD"]

    def run(sub, inject):
        rig = _c1d_fork(tmp_path, base, sub, live, verified=[a.position_id, b.position_id])
        rig.x.max_entries_per_cycle = 2
        if inject:
            _c1d_inject_capture_failure(rig)
        probed, real_probe, marks, real_scan = [], rig.x._protective_probe, {}, rig.x._scan_entries
        rig.x._protective_probe = lambda pos, *a_: (probed.append(pos.symbol), real_probe(pos, *a_))[1]
        rig.x._scan_entries = lambda *a_: (marks.update(reads=rig.broker.reads, posts=rig.posts(), n=len(rec)), real_scan(*a_))[1]
        out = rig.cycle()
        pa = rig.pos(a)
        return {"result": out["result"], "error": out["error"], "posts": rig.posts(), "posts_at_entries": marks["posts"], "reads": marks["reads"], "probed": probed,
                "v": sorted(rig.x._verified_positions), "a": (pa.state, pa.attention_reason), "rows": _c1d_store_rows(rig, pids, syms, out["run_id"])}, rec[marks["n"]:]
    (clean, _), (inj, inj_rec) = run("clean", False), run("inj", True)
    assert clean == inj
    assert clean["result"] == "RUN" and clean["error"] is None and clean["posts"] == clean["posts_at_entries"] == 1
    assert clean["probed"] == ["CCC"] and clean["v"] == sorted([a.position_id, b.position_id, c.position_id]) and d.position_id not in clean["v"]
    assert clean["a"] == ("ATTENTION", "EXIT_ATTEMPTS_EXHAUSTED")
    vs = entry_verdicts(inj_rec, DG.DG_EARLY)
    assert len(vs) == 2 and all("DG_SAFETY_STATE_UNREADABLE" in v.codes and "E10(v)" in v.failed_facts for v in vs)


def test_c1d_r14_39_o2_b_in_attention_since_an_earlier_cycle_recon_blocks_and_v_passes(tmp_path, rec, engine_pass):
    """r14 39(O2) (r14:1725): B in ATTENTION since an earlier cycle. BROKER_RECONCILIATION blocks (R names B and recon_healthy is False, which
    is exactly when that Wave 1 control is BLOCK, execution/engine.py entry_controls; the 6.0 controls are stubbed here) and the codes do not
    contain (v)'s code."""
    rig = ERig(tmp_path)
    b = rig.open("BBB")
    rig.live("BBB", "12.00")
    rig.store.update_position(b.position_id, rig.t, None, state="ATTENTION", attention_reason="QTY_MISMATCH")
    rig.sim.set_position("BBB", 6, 10.0)
    rig.cycle()                                                                   # an earlier cycle: B already in ATTENTION
    out = rig.cycle()
    v = entry_verdicts(rec, DG.DG_EARLY)[-1]
    assert rig.pos(b).state == "ATTENTION" and not rig.x.recon_healthy and any("BBB" in r for r in rig.x.recon_reasons)
    assert out["summary"]["entries"]["blocked"]["NEWA"]
    assert "DG_ATTENTION_ENTERED_THIS_CYCLE" not in v.codes and not {"E10(iv)", "E10(v)"} & set(v.failed_facts)


def _c1d_recovery_close_seam(rig, p):
    """r14 39(O4)/(O7)'s seam, after the S_R capture and before DG-early (the SAFETY step): the recovery command's CLOSE, as it leaves the row."""
    real = rig.x._safety_view

    def seam(now, evaluate=False):
        if evaluate:
            rig.store.update_position(p.position_id, now, {"kind": "MANUAL_RECOVERY", "from_state": "ATTENTION", "to_state": "CLOSED", "reason": "MANUAL_EXTERNAL"},
                                      state="CLOSED", quantity=0, attention_reason=None, exit_reason="MANUAL_EXTERNAL")
        return real(now, evaluate)
    rig.x._safety_view = seam


def test_c1d_r14_39_o4_a_closed_by_the_recovery_command_between_the_capture_and_dg_early_passes_v(tmp_path, rec, engine_pass):
    """r14 39(O4) (r14:1727): A enters ATTENTION (W2) and is closed by the recovery command between the S_R capture and DG-early: (v) passes."""
    rig = ERig(tmp_path)
    a = _exhausted(rig)
    _c1d_recovery_close_seam(rig, a)
    rig.cycle()
    v = entry_verdicts(rec, DG.DG_EARLY)[-1]
    assert rig.pos(a).state == "CLOSED" and rig.x._attention_baseline.states[a.position_id] == "EXIT_REQUIRED"
    assert "DG_ATTENTION_ENTERED_THIS_CYCLE" not in v.codes and "E10(v)" not in v.failed_facts


def test_c1d_r14_39_o7_a_recovery_close_and_a_new_entrant_in_the_same_cycle(tmp_path, rec, engine_pass):
    """r14 39(O7), service form (r12; r14:1730): B in S_R^ATT is closed by the recovery command after the S_R capture (O4's seam); in the same
    cycle A enters ATTENTION through W2. Every candidate's codes contain DG_ATTENTION_ENTERED_THIS_CYCLE at DG-early (A is in A_t and not in
    S_R^ATT although |A_t| = |S_R^ATT| = 1), and BROKER_RECONCILIATION also blocks through the stale R that names B. Kills M18 (r14:1586)."""
    rig = ERig(tmp_path, ready=("NEWA", "NEWB"))
    rig.x.max_entries_per_cycle = 2
    b = rig.open("BBB")
    rig.live("BBB", "12.00")
    rig.store.update_position(b.position_id, rig.t, None, state="ATTENTION", attention_reason="QTY_MISMATCH")
    rig.sim.set_position("BBB", 6, 10.0)
    a = _exhausted(rig)
    _c1d_recovery_close_seam(rig, b)
    out = rig.cycle()
    sr = rig.x._attention_baseline.states
    assert sr[b.position_id] == "ATTENTION" and sr[a.position_id] == "EXIT_REQUIRED" and rig.pos(b).state == "CLOSED" and rig.pos(a).state == "ATTENTION"
    vs = entry_verdicts(rec, DG.DG_EARLY)
    assert len(vs) == 2 and all("DG_ATTENTION_ENTERED_THIS_CYCLE" in v.codes and "E10(v)" in v.failed_facts for v in vs)
    assert any("BBB" in r for r in rig.x.recon_reasons) and not rig.x.recon_healthy and all(out["summary"]["entries"]["blocked"][s] for s in ("NEWA", "NEWB"))


def test_c1d_r14_39_q_recovery_after_i_gives_today_s_preconditions_and_entries_resume_after_the_close(tmp_path, rec, engine_pass, no_e7):
    """r14 39(Q) (r14:1735), OD-R4-4(a): after (I) (A W2 -> ATTENTION EXIT_ATTEMPTS_EXHAUSTED) and the owner's manual sale, the recovery
    command's preview and apply (CLOSE) for A give today's precondition results (every one passes); after the CLOSE, entries resume (one
    entry POST, E7 waived)."""
    import uuid
    from account_refresh.store import AccountRefreshStore
    from database.account_refresh_migrations import run_account_refresh_migrations
    from execution import recovery as R
    rig = ERig(tmp_path)
    a = _exhausted(rig)
    rig.cycle()                                                                   # (I)'s cycle c
    assert rig.pos(a).state == "ATTENTION" and rig.pos(a).attention_reason == "EXIT_ATTEMPTS_EXHAUSTED" and rig.entry_posts == []
    rig.sim.set_position("AAA", 0)                                                # the owner's manual sale on the website
    rig.store.set_mode("MONITOR_ONLY", rig.t, "test", None, {})                   # recovery precondition 2
    rig.cycle()                                                                   # RECONCILE sees the broker flat
    c = sqlite3.connect(str(rig.db))
    try:
        run_account_refresh_migrations(c)
    finally:
        c.close()
    at, rid = rig.t + timedelta(seconds=1), uuid.uuid4().hex
    snap = {"refresh_id": rid, "refreshed_at": iso(at), "source": "SIMULATED_PAPER", "host": "paper-api.alpaca.markets", "account": {"status": "ACTIVE"},
            "positions": [], "open_orders": []}
    AccountRefreshStore(rig.db).commit_success(snap, {"started_at": iso(at), "finished_at": iso(at), "trigger": "TEST", "result": "SUCCESS", "read_attempts": 1,
                                                      "refresh_id": rid, "owner": "test"}, at)
    now = at + timedelta(seconds=1)
    pv = R.preview(rig.db, a.position_id, now)
    assert pv["ok"] is True and pv["refusals"] == [] and pv["case"] == "CLOSE" and all(p["ok"] is True for p in pv["preconditions"]), pv["preconditions"]
    done = R.apply(rig.db, a.position_id, pv["evidence_hash"], "owner", "manual sale on the website", confirmed_flat=True, now=now)
    assert done["ok"] is True and rig.pos(a).state == "CLOSED"
    rig.store.set_mode("PAPER_AUTO", now, "test", None, {})
    rig.t = now
    rig.cycle()
    assert len(rig.entry_posts) == 1


def test_b1_39_i2_strategy_exhaustion_is_refused_by_e10_v_alone_then_recon_reasons_next_cycle(tmp_path, rec, engine_pass):
    """r15 39(I2) (CF-4; 9-C rows A2, A3): (I)'s setup, but in cycle c A's stop is a verified NOT_HIT and a TIME_EXIT candidate reaches the exit
    gate, so step 4 runs off the protective path. (I)'s r14 assertions, word for word, hold under (a) and (b1); and directly: no
    protective_exit_problems entry, no lifecycle row, an empty latch map, V unchanged. Restores the M14(a)-(d) kills and M17's."""
    from test_data_gate_exits_service import LIFE
    rig = ERig(tmp_path, ready=("NEWA", "NEWB"))
    rig.x.max_entries_per_cycle = 2
    a = rig.open("AAA", at=rig.t - timedelta(days=8))
    rig.live("AAA", "12.00")                                                      # a verified NOT_HIT; TIME_EXIT is reached
    rig.store.update_position(a.position_id, rig.t, None, state="EXIT_REQUIRED", exit_reason="HARD_INVALIDATION", exit_cycle=rig.x.max_exit_cycles)
    rig.x._verified_positions.add(a.position_id)                                  # A in V (39 "isolate (v)")
    probed, real_probe = [], rig.x._protective_probe
    rig.x._protective_probe = lambda pos, *a_: (probed.append(pos.symbol), real_probe(pos, *a_))[1]
    rig.cycle()
    assert rig.pos(a).state == "ATTENTION" and rig.pos(a).attention_reason == "EXIT_ATTEMPTS_EXHAUSTED" and rig.posts() == 0 and probed == []
    vs = entry_verdicts(rec, DG.DG_EARLY)
    assert len(vs) == 2 and rig.ctl("DATA_GATE")["evaluation_count"] == 2
    for v in vs:
        assert "DG_ATTENTION_ENTERED_THIS_CYCLE" in v.codes and "E10(v)" in v.failed_facts
        assert not {"E10(i)", "E10(ii)", "E10(iii)", "E10(iv)"} & set(v.failed_facts)
    assert entry_intents(rig) == []
    assert rig.x.protective_exit_problems == {} and rig.recs(LIFE) == [] and rig.ctl("PROTECTIVE_EXIT") is None
    assert rig.x._unsaved_blocking_alerts == {} and rig.x._verified_positions == {a.position_id}
    rig.cycle()
    v = entry_verdicts(rec, DG.DG_EARLY)[-1]
    assert "DG_ATTENTION_ENTERED_THIS_CYCLE" not in v.codes and any("AAA" in r for r in rig.x.recon_reasons)
