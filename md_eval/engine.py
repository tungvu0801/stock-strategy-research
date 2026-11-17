"""md_eval/engine.py - the day loop. For every calendar session d of the segment (provider.sessions, A1), in this order (P3a):
  1. the corporate-action step: bookings of the events whose booking session is d (splits, dividends' entitlement, spin-offs, mergers,
     delistings - a delisting on its effective_session, or on the session after last_trade_session when that is earlier), then the dividend
     credits due on d; with events_completeness != COMPLETE every held instrument is flagged CORPORATE_ACTION_UNACCOUNTED;
  2. fills at the open: the orders decided at the previous close, exits first, then entries in rank order, each through the execution
     channel provider.execution_open(instrument_id, d, decision_session=...) - the ONLY call site, armed only in this phase (A2);
     NO_BAR -> NO_FILL_GAP (an exit is decided again at d's close);
  3. marks at the close (raw close; on a gap the last raw close, STALE_MARK, A5), the cash rate, equity;
  4. decisions after the close, from provider.as_of(close_utc(d)) only, through a DecisionContext (no provider, no execution channel).
Bookings and marks read full history (allowed: P3a, §4); decisions never do. Nothing is fetched, nothing touches a database (offline())."""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from md_eval import (BOOK, CONTRACT_BASE_OF, CORPORATE_ACTION_UNACCOUNTED, DISCLAIMER, ENGINE_VERSION, FIXED_CURRENT_UNIVERSE, NO_FILL_GAP,
                     POSSIBLE_DELISTING, SIZING_FROM_STALE_EQUITY, STALE_MARK, STATIC_CLASSIFICATION_MAP, SURVIVORSHIP_BIAS_PRESENT)
from md_eval import book as BK
from md_eval import prereg as PR
from md_eval.cash_rate import ZERO
from md_eval.contract import NO_BAR, CalendarNotCovered, ExecutionChannelViolation, check_version, is_missing, licence_gate
from md_eval.costs import CostModel, median_dollar_volume
from md_eval.isolation import offline
from md_eval.ledger import Ledger
from md_eval.signals import bar_at
from md_eval.view import BookSnapshot, DecisionContext, PositionSnapshot

DELISTING_MODES = ("PRIMARY", "LAST_PRICE")


class RunRefused(RuntimeError):
    pass


def canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def sha256_of(obj) -> str:
    return hashlib.sha256(canonical(obj).encode("utf-8")).hexdigest()


def _num(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


@dataclass(frozen=True)
class RunConfig:
    start_session: str
    end_session: str
    initial_cash: float = 100_000.0
    embargo_sessions: int = 0
    cost_model: Any = None
    cash_rate: Any = ZERO
    delisting_mode: str = "PRIMARY"
    static_map: Optional[dict] = None                 # instrument_id -> {"sector": ..., "issuer_id": ...} (A4)
    prereg_sha256: Optional[str] = None
    segment_label: Optional[str] = None               # TRAIN | VALIDATION | TEST
    book_overrides: Optional[dict] = None             # always refused: the book is FROZEN


@dataclass
class _Order:
    kind: str
    instrument_id: str
    decision_session: str
    reason: str
    bucket: dict
    notional: float = 0.0
    sector: str = "UNKNOWN"
    stop_raw: Optional[float] = None
    target_raw: Optional[float] = None
    max_hold: Optional[int] = None
    void_reason: Optional[str] = None            # set at the corporate-action step when the order can no longer fill as decided (MED-1)


def result_sha256(result: dict) -> str:
    """The sha256 of a run's evidence-bearing content (identity sha, daily rows, decisions, fills, flags, final equity): recorded in the holdout
    ledger with the run, recomputed by the verdict, so evidence edited after the run is refused."""
    return sha256_of({k: result[k] for k in ("identity_sha256", "rows", "decisions", "fills", "flags", "final_equity", "benchmark_returns")})


def config_settings(cfg: RunConfig, label) -> dict:
    """The run settings a configuration WILL produce, in the shape prereg.settings_mismatch reads (the same fields the identity records)."""
    costs = (cfg.cost_model or CostModel()).identity()
    cash = cfg.cash_rate.identity()
    return {"buckets": costs["buckets"], "fees": costs["fees"], "label": costs["label"], "embargo_sessions": cfg.embargo_sessions,
            "cash_rate": {k: cash[k] for k in ("mode", "annual", "sha256")}, "delisting_mode": cfg.delisting_mode, "initial_cash": cfg.initial_cash,
            "static_map_sha256": sha256_of(cfg.static_map) if cfg.static_map else None, "book": dict(BOOK), "segment": [cfg.start_session, cfg.end_session],
            "segment_label": label}


def _test_guard(cfg: RunConfig, reg: dict, source_identity: dict, source_role: str, cost_multiplier: float) -> None:
    """LOW-R3: on TEST, before any data is read and before the attempt is recorded: only the pre-registered strategy (1x or 2x) and control (1x),
    identified by id, version and the frozen pre-registration hash, with exactly the pre-registered settings; anything else is refused and the
    single TEST attempt is not spent. (The benchmark series is computed by the run itself.)"""
    ps, pc, sid = reg["strategy"], reg["control"], dict(source_identity or {})
    is_strat = (sid.get("name"), sid.get("version"), sid.get("preregistration_hash")) == (ps["id"], ps["version"], ps["preregistration_hash"])
    is_ctrl = (sid.get("name"), sid.get("version"), sid.get("preregistration_hash")) == (pc["id"], pc["version"], ps["preregistration_hash"])
    if source_role != "STRATEGY" or not (is_strat or is_ctrl):
        raise RunRefused("only the pre-registered strategy and control (id, version, frozen hash) run on the TEST segment")
    if float(cost_multiplier) not in ((1.0, 2.0) if is_strat else (1.0,)):
        raise RunRefused(f"cost level {cost_multiplier}x is not a pre-registered TEST run for this source")
    bad = PR.settings_mismatch(config_settings(cfg, "TEST"), reg)
    if bad:
        raise RunRefused(f"the TEST run's settings differ from the pre-registration {sorted(bad)}: refused before any data is read; the attempt is not spent")


def segment_guard(m, cfg: RunConfig, registry, holdout, source_name: str = "", cost_multiplier: float = 1.0, source_identity=None,
                  source_role: str = "STRATEGY"):
    """(the segment label, the check label). A HISTORICAL run, and any run given a segment label, needs its pre-registration registered; its
    [start_session, end_session] must lie inside exactly one declared segment and carry that segment's label (a mislabelled range is
    refused); any overlap with the TEST segment needs TEST_OPENED for this pre-registration and dataset; a VALIDATION or TEST run needs the
    holdout ledger (it is recorded there). A SYNTHETIC run with neither a segment label nor a pre-registration is exempt and labelled so in
    its identity; one that names a pre-registration is checked like a HISTORICAL run. MED-3 (fail closed): a checked run whose range overlaps
    the TEST range of ANY OTHER registered pre-registration that has no TEST_OPENED yet is refused (OVERLAPS_UNOPENED_TEST_OF_OTHER_PREREG)."""
    if m.kind != "HISTORICAL" and cfg.segment_label is None and not cfg.prereg_sha256:
        return None, "SYNTHETIC_EXEMPT_UNSEGMENTED", None
    if registry is None or not cfg.prereg_sha256:
        raise RunRefused("a HISTORICAL or segment-labelled run needs its pre-registration registered in the trial registry before it runs")
    entry = registry.trial_entry(cfg.prereg_sha256)
    if entry is None:
        raise RunRefused("the pre-registration is not registered in the trial registry")
    reg = entry["payload"]
    if reg["dataset_sha256"] not in ("TO_BE_ACCEPTED", m.content_sha256):
        raise RunRefused("the pre-registration names another dataset")
    for other in registry.entries():                                      # MED-3: fail closed on ANOTHER pre-registration's unopened TEST range
        op = other["payload"]
        if other["kind"] != "TRIAL_REGISTERED" or op["prereg_sha256"] == cfg.prereg_sha256 or "TEST" not in op["segments"]:
            continue
        a, b = op["segments"]["TEST"]
        if cfg.start_session <= b and a <= cfg.end_session and (holdout is None or not holdout.test_opened_for(op["prereg_sha256"])):
            raise RunRefused(f"OVERLAPS_UNOPENED_TEST_OF_OTHER_PREREG: {cfg.start_session}..{cfg.end_session} overlaps the unopened TEST range {a}..{b} "
                             f"of {op['hypothesis_id']} ({op['prereg_sha256'][:12]})")
    segs = reg["segments"]
    inside = [label for label, (a, b) in segs.items() if a <= cfg.start_session and cfg.end_session <= b]
    if len(inside) != 1:
        raise RunRefused(f"the run {cfg.start_session}..{cfg.end_session} lies inside no single declared segment")
    label = inside[0]
    if cfg.segment_label != label:
        raise RunRefused(f"mislabelled run: its range lies in {label}, it is labelled {cfg.segment_label!r}")
    test = segs.get("TEST")
    if test and cfg.start_session <= test[1] and test[0] <= cfg.end_session:
        if holdout is None or holdout.test_open_entry(cfg.prereg_sha256, m.content_sha256) is None:
            raise RunRefused("a run overlapping the TEST segment needs TEST_OPENED for this pre-registration and dataset")
    if label in ("VALIDATION", "TEST") and (holdout is None or holdout.prereg_entry(cfg.prereg_sha256) is None):
        raise RunRefused("a VALIDATION or TEST run is recorded in the holdout ledger, after its PREREG_RECORDED")
    if label == "TEST":
        _test_guard(cfg, reg, source_identity, source_role, cost_multiplier)
    if label == "TEST" and holdout.test_run_exists(cfg.prereg_sha256, m.content_sha256, source_name, cost_multiplier):
        raise RunRefused("this source at this cost level already ran on the TEST segment: one TEST attempt (MED-3)")
    return label, "REGISTERED_SEGMENT", reg


def run(provider, source, cfg: RunConfig, *, registry=None, holdout=None) -> dict:
    BK.check_frozen(cfg.book_overrides)
    if cfg.delisting_mode not in DELISTING_MODES:
        raise ValueError(f"delisting_mode must be one of {DELISTING_MODES}")
    if source.role not in ("STRATEGY", "BENCHMARK"):
        raise ValueError(f"unknown source role {source.role!r}")
    m = provider.manifest()
    check_version(m)
    licence = licence_gate(m)                                                   # before any data is read
    source_identity = source.identity()
    source_name = str(source_identity.get("name", ""))
    multiplier = float((cfg.cost_model or CostModel()).multiplier)
    label, check, reg = segment_guard(m, cfg, registry, holdout, source_name, multiplier, source_identity, source.role)   # before any data is read
    bench = (reg or {}).get("benchmark_instrument")
    if bench is not None and bench not in {i.instrument_id for i in provider.instruments()}:
        raise RunRefused(f"the pre-registered benchmark instrument {bench!r} is not in the dataset")
    heads = {"registry_head_sha256": registry.head() if registry is not None else None,
             "holdout_head_sha256": holdout.head() if holdout is not None else None}   # LOW-1: a later truncation is detectable
    with offline() as attempts:
        out = _Run(provider, source, cfg, m, licence, check, heads, bench).execute()
    if attempts:
        raise RunRefused(f"network or database access attempted: {attempts}")
    out["result_sha256"] = result_sha256(out)
    bsha = (out["identity"]["benchmark"] or {}).get("series_sha256")
    if label == "VALIDATION":
        holdout.record_validation_run(cfg.prereg_sha256, m.content_sha256, out["identity_sha256"], out["result_sha256"], bsha, source_name, multiplier)
    elif label == "TEST":
        holdout.record_test_run(cfg.prereg_sha256, m.content_sha256, out["identity_sha256"], out["result_sha256"], bsha, source_name, multiplier)
    return out


class _Run:
    def __init__(self, p, source, cfg: RunConfig, m, licence: str, segment_check: str = "SYNTHETIC_EXEMPT_UNSEGMENTED", heads: Optional[dict] = None,
                 benchmark_instrument: Optional[str] = None):
        self.p, self.src, self.cfg, self.m, self.licence = p, source, cfg, m, licence
        self.benchmark_instrument = benchmark_instrument
        self.segment_check, self.heads = segment_check, dict(heads or {"registry_head_sha256": None, "holdout_head_sha256": None})
        self.costs = cfg.cost_model or CostModel()
        self.L = Ledger(cash=float(cfg.initial_cash))
        self.flags = set()
        self.pending: List[_Order] = []
        self.decisions: List[dict] = []
        self.fills: List[dict] = []
        self.not_taken: List[dict] = []
        self.rows: List[dict] = []
        self.unaccounted = set()
        self.static_used = False
        self._phase = "IDLE"

    # ---- helpers -----------------------------------------------------------------------------------------------------------------------
    def sector_of(self, iid: str) -> str:
        ins = self.instr.get(iid)
        if ins is not None and not is_missing(ins.sector) and ins.sector:
            return str(ins.sector)
        entry = (self.cfg.static_map or {}).get(iid) or {}
        if entry.get("sector"):
            self.static_used = True
            return str(entry["sector"])
        return "UNKNOWN"

    def _close(self, iid: str, d: str):
        rows = self.p.bars(iid, d, d)
        return rows[0].raw_close if rows and _num(rows[0].raw_close) else None

    def _first_close_from(self, iid: str, d: str):
        for b in self.p.bars(iid, d, self.m.end_session):
            if _num(b.raw_close):
                return b.raw_close, b.session
        return None, None

    def _open(self, o: _Order, d: str):
        """The engine side of the execution channel: only in the fill phase, only for the session after the decision session."""
        if self._phase != "FILL" or d != self.p.next_session(o.decision_session):
            raise ExecutionChannelViolation(f"execution channel used outside the fill step ({self._phase}, {o.instrument_id}, {d})")
        return self.p.execution_open(o.instrument_id, d, decision_session=o.decision_session)

    def _not_taken(self, d: str, iid: str, reason: str, phase: str, decision_session: str):
        rec = {"session": d, "instrument_id": iid, "reason": reason, "phase": phase, "decision_session": decision_session}
        self.not_taken.append(rec)
        if phase == "DECISION":
            self.decisions.append({"session": d, "action": "NOT_TAKEN", "instrument_id": iid, "reason": reason})

    def _bucket(self, view, iid: str, d: str):
        b, fl = self.costs.bucket(median_dollar_volume(view.bars(iid, self.view_start, d)))
        self.flags.update(fl)
        return b

    # ---- the loop ------------------------------------------------------------------------------------------------------------------------
    def execute(self) -> dict:
        p, m, cfg = self.p, self.m, self.cfg
        self.sessions = list(p.sessions(cfg.start_session, cfg.end_session))
        if not self.sessions:
            raise RunRefused("the segment holds no calendar session")
        self.view_start = m.start_session
        self.instr = {i.instrument_id: i for i in p.instruments()}
        events = p.events(None, self.sessions[0], max(m.end_session, self.sessions[-1]))      # bookings only
        self.schedule: Dict[str, list] = {}
        for e in events:
            s = e.effective_session
            if e.type == "DELISTING" and isinstance(e.last_trade_session, str):
                try:
                    nxt = p.next_session(e.last_trade_session)
                except CalendarNotCovered:
                    nxt = None
                if nxt is not None and nxt < s:
                    s = nxt
            self.schedule.setdefault(s, []).append(e)
        self.delisted = {e.instrument_id for e in events if e.type == "DELISTING"}
        last = len(self.sessions) - 1
        for i, d in enumerate(self.sessions):
            self._corporate_actions(d)
            self._phase = "FILL"
            self._fills(d)
            self._phase = "MARK"
            self._marks(d)
            self._phase = "DECIDE"
            if i < last:
                self._decide(d, i, last)
            self._phase = "IDLE"
        return self._finish()

    def _corporate_actions(self, d: str):
        L = self.L
        # LOW-2: cash dividends first, so a same-session split, spin-off or merger never changes the entitled count (the shares at the start of
        # the step = at the previous close); then every other event in contract order (effective_session, event_id)
        for e in sorted(self.schedule.get(d, ()), key=lambda e: (0 if e.type in ("CASH_DIVIDEND", "SPECIAL_DIVIDEND") else 1, e.effective_session, e.event_id)):
            t = e.type
            if t in ("SPLIT", "REVERSE_SPLIT", "STOCK_DIVIDEND"):
                L.split(e, d)
                self._rescale_pending_entries(e, d)
            elif t in ("CASH_DIVIDEND", "SPECIAL_DIVIDEND"):
                L.dividend_entitle(e, d)
            elif t == "SPIN_OFF":
                if e.instrument_id in L.positions:
                    child_close, rule = (self._close(e.child_instrument_id, d), "RAW_CLOSES_ON_EFFECTIVE_SESSION") if e.child_instrument_id else (None, None)
                    if child_close is None and e.child_instrument_id:
                        child_close, at = self._first_close_from(e.child_instrument_id, d)
                        rule = f"CHILD_FIRST_RAW_CLOSE_AT_OR_AFTER:{at}" if at else None
                    L.spin_off(e, d, self._close(e.instrument_id, d), child_close, rule, self.sector_of,
                               child_mark=self._close(e.child_instrument_id, d) if e.child_instrument_id else None)
            elif t == "MERGER":
                if e.instrument_id in L.positions:
                    acq = e.acquirer_instrument_id
                    price, rule = (self._close(acq, d), "PRO_RATA_BY_VALUE_ON_EFFECTIVE_SESSION") if acq else (None, None)
                    if price is None and acq:
                        price, at = self._first_close_from(acq, d)
                        rule = f"PRO_RATA_BY_VALUE_ACQUIRER_FIRST_RAW_CLOSE_AT_OR_AFTER:{at}" if at else None
                    L.merger(e, d, price, rule, self.sector_of, acquirer_mark=self._close(acq, d) if acq else None)
            elif t == "DELISTING":
                L.delist(e, d, self.cfg.delisting_mode)
        L.credit_due(d)
        if self.m.events_completeness != "COMPLETE":
            for iid in sorted(L.positions):
                if iid not in self.unaccounted:
                    self.unaccounted.add(iid)
                    L.log.append({"session": d, "kind": "FLAG", "flag": CORPORATE_ACTION_UNACCOUNTED, "instrument_id": iid,
                                  "why": f"events_completeness={self.m.events_completeness}"})
                L.flags.add(CORPORATE_ACTION_UNACCOUNTED)

    def _rescale_pending_entries(self, e, d: str):
        """MED-1 (A3): a split, reverse split or stock dividend booked on the fill session also converts the stop and target of every PENDING
        entry order on that instrument (decided at the previous close in pre-split units) by the ratio, before the open's fills - as the ledger
        does for a held position. With an unknown ratio the levels cannot be converted: the entry is refused at the fill
        (SPLIT_RATIO_UNKNOWN_LEVELS_UNCONVERTED) and CORPORATE_ACTION_UNACCOUNTED is flagged, never filled with pre-split levels."""
        for o in self.pending:
            if o.kind != "ENTRY" or o.instrument_id != e.instrument_id or o.void_reason:
                continue
            if not (_num(e.ratio) and e.ratio > 0):
                o.void_reason = "SPLIT_RATIO_UNKNOWN_LEVELS_UNCONVERTED"
                self.L._flag(CORPORATE_ACTION_UNACCOUNTED, d, e.instrument_id, event_id=e.event_id, why="RATIO_UNKNOWN_PENDING_ENTRY")
                continue
            before = (o.stop_raw, o.target_raw)
            o.stop_raw = o.stop_raw / e.ratio if o.stop_raw is not None else None
            o.target_raw = o.target_raw / e.ratio if o.target_raw is not None else None
            self.L.log.append({"session": d, "kind": "PENDING_LEVELS_RESCALED", "instrument_id": e.instrument_id, "event_id": e.event_id,
                               "ratio": e.ratio, "before": list(before), "after": [o.stop_raw, o.target_raw]})

    def _fills(self, d: str):
        L, orders, self.pending = self.L, self.pending, []
        for o in sorted((o for o in orders if o.kind == "EXIT"), key=lambda o: o.instrument_id):
            if o.instrument_id not in L.positions:
                self._not_taken(d, o.instrument_id, "POSITION_GONE", "FILL", o.decision_session)
                continue
            px = self._open(o, d)
            if px is NO_BAR:
                self.flags.add(NO_FILL_GAP)
                self._not_taken(d, o.instrument_id, "NO_FILL_GAP", "FILL", o.decision_session)
                continue
            pos = L.positions[o.instrument_id]
            cost, parts, fl = self.costs.cost("SELL", pos.shares, px, o.bucket, d)
            self.flags.update(fl)
            shares = pos.shares
            L.sell(o.instrument_id, px, cost, d, reason=o.reason)
            self.fills.append({"session": d, "decision_session": o.decision_session, "kind": "EXIT", "instrument_id": o.instrument_id,
                               "shares": shares, "price": px, "cost": cost, "costs": parts, "reason": o.reason})
        for o in (o for o in orders if o.kind == "ENTRY"):
            if o.void_reason:
                self._not_taken(d, o.instrument_id, o.void_reason, "FILL", o.decision_session)
                continue
            if self.src.role == "STRATEGY":
                why = BK.fill_limits_ok({k: v.sector for k, v in L.positions.items()}, o.sector)
                if o.instrument_id in L.positions:
                    why = "ALREADY_HELD"
                if why:
                    self._not_taken(d, o.instrument_id, why, "FILL", o.decision_session)
                    continue
            elif o.instrument_id in L.positions:
                self._not_taken(d, o.instrument_id, "ALREADY_HELD", "FILL", o.decision_session)
                continue
            px = self._open(o, d)
            if px is NO_BAR:
                self.flags.add(NO_FILL_GAP)
                self._not_taken(d, o.instrument_id, "NO_FILL_GAP", "FILL", o.decision_session)
                continue
            shares = int(math.floor(o.notional / px))
            if shares < 1:
                self._not_taken(d, o.instrument_id, "SIZE_BELOW_ONE_SHARE", "FILL", o.decision_session)
                continue
            cost, parts, fl = self.costs.cost("BUY", shares, px, o.bucket, d)
            self.flags.update(fl)
            if shares * px + cost > L.cash:
                self._not_taken(d, o.instrument_id, "CASH", "FILL", o.decision_session)
                continue
            L.buy(o.instrument_id, shares, px, cost, d, sector=o.sector, stop_raw=o.stop_raw, target_raw=o.target_raw, max_hold=o.max_hold,
                  role=self.src.role)
            self.fills.append({"session": d, "decision_session": o.decision_session, "kind": "ENTRY", "instrument_id": o.instrument_id,
                               "shares": shares, "price": px, "cost": cost, "costs": parts})

    def _marks(self, d: str):
        L = self.L
        stale = False
        for iid in sorted(L.positions):
            pos = L.positions[iid]
            c = self._close(iid, d)
            if c is not None:
                pos.mark, pos.mark_session = c, d
            else:
                stale = True
                self.flags.add(STALE_MARK)
                L.log.append({"session": d, "kind": "STALE_MARK", "instrument_id": iid, "mark": pos.mark, "mark_session": pos.mark_session})
            pos.held_sessions += 1
        r = float(self.cfg.cash_rate.daily(d))
        L.cash += L.cash * r
        mv = L.market_value()
        eq = L.cash + mv
        self.rows.append({"session": d, "cash": L.cash, "market_value": mv, "equity": eq, "gross_long": mv,
                          "exposure": (mv / eq) if eq > 0 else 0.0, "stale": stale, "cash_return": r, "positions": len(L.positions)})

    def _decide(self, d: str, i: int, last: int):
        L, p = self.L, self.p
        view = p.as_of(p.session_times(d)[1])
        row = self.rows[-1]
        snap = BookSnapshot(session=d, cash=L.cash, equity=row["equity"], stale_equity=row["stale"],
                            positions=tuple(PositionSnapshot(k, v.shares, v.sector, v.held_sessions, v.stop_raw, v.target_raw, v.max_hold)
                                            for k, v in sorted(L.positions.items())))
        ctx = DecisionContext(view, d, snap)
        orders, exiting = [], set()
        src_exits = set(self.src.exits(ctx))
        for iid in sorted(L.positions):
            pos = L.positions[iid]
            reason = None
            if pos.received:
                reason = "RECEIVED_SHARES"
            elif iid in src_exits:
                reason = "SOURCE_EXIT"
            else:
                b = bar_at(view, iid, d)
                c = b.raw_close if b is not None and _num(b.raw_close) else None
                if c is not None and pos.stop_raw is not None and c <= pos.stop_raw:
                    reason = "STOP"
                elif c is not None and pos.target_raw is not None and c >= pos.target_raw:
                    reason = "TARGET"
                elif pos.max_hold is not None and pos.held_sessions >= pos.max_hold:
                    reason = "TIME"
            if reason:
                exiting.add(iid)
                orders.append(_Order("EXIT", iid, d, reason, self._bucket(view, iid, d)))
                self.decisions.append({"session": d, "action": "EXIT", "instrument_id": iid, "reason": reason})
        ranked = BK.rank(self.src.entries(ctx))
        held = {k: v.sector for k, v in L.positions.items()}
        if self.src.role == "STRATEGY":
            accepted, refused = BK.decide_strategy_entries(ranked, i=i, last_index=last, embargo=self.cfg.embargo_sessions, held=held,
                                                           exiting=exiting, equity=row["equity"], sector_of=self.sector_of)
            for s, why in refused:
                self._not_taken(d, s.instrument_id, why, "DECISION", d)
            for s, notional, sec in accepted:
                orders.append(_Order("ENTRY", s.instrument_id, d, "SIGNAL", self._bucket(view, s.instrument_id, d), notional=notional, sector=sec,
                                     stop_raw=s.stop_raw, target_raw=s.target_raw, max_hold=s.max_hold))
                self.decisions.append({"session": d, "action": "ENTRY", "instrument_id": s.instrument_id, "reason": "SIGNAL", "notional": notional,
                                       "stop_raw": s.stop_raw, "target_raw": s.target_raw})
        else:
            accepted = []
            for s in ranked:
                if s.instrument_id in held:
                    self._not_taken(d, s.instrument_id, "ALREADY_HELD", "DECISION", d)
                    continue
                bucket = self._bucket(view, s.instrument_id, d)
                notional = BK.benchmark_notional(s, row["equity"], self.costs.friction_rate(bucket))
                accepted.append(s)
                orders.append(_Order("ENTRY", s.instrument_id, d, "BENCHMARK", bucket, notional=notional, sector=self.sector_of(s.instrument_id)))
                self.decisions.append({"session": d, "action": "ENTRY", "instrument_id": s.instrument_id, "reason": "BENCHMARK", "notional": notional})
        if accepted and row["stale"]:
            self.flags.add(SIZING_FROM_STALE_EQUITY)
        self.pending = orders

    def _finish(self) -> dict:
        L, m, cfg = self.L, self.m, self.cfg
        for iid in sorted(L.positions):
            rows = self.p.bars(iid, self.sessions[0], m.end_session)
            last_bar = rows[-1].session if rows else None
            if (last_bar is None or last_bar < m.end_session) and iid not in self.delisted:
                self.flags.add(POSSIBLE_DELISTING)
                L.log.append({"session": self.sessions[-1], "kind": "FLAG", "flag": POSSIBLE_DELISTING, "instrument_id": iid, "last_bar": last_bar})
        flags = set(self.flags) | set(L.flags) | set(self.src.flags()) | set(cfg.cash_rate.flags())
        if m.membership_basis == "FIXED_CURRENT_UNIVERSE":
            flags |= {FIXED_CURRENT_UNIVERSE, SURVIVORSHIP_BIAS_PRESENT}
        if self.static_used:
            flags.add(STATIC_CLASSIFICATION_MAP)
        bench_returns, bench_id = None, None
        if self.benchmark_instrument is not None:            # the pre-registered benchmark, from THIS provider and THESE sessions (reporting, P3)
            from md_eval import benchmarks as BMK              # licence-gated; UNAVAILABLE without its view; never touches the ledger
            bench_returns, bench_id = BMK.bound_series(self.p, self.benchmark_instrument, [r["session"] for r in self.rows])
        identity = {"engine_version": ENGINE_VERSION, "contract_version": m.contract_version, "contract_sha256": m.contract_sha256,
                    "contract_base": list(CONTRACT_BASE_OF.get(m.contract_version, ())), "dataset_id": m.dataset_id, "content_sha256": m.content_sha256,
                    "kind": m.kind, "licence": self.licence, "source": self.src.identity(), "role": self.src.role,
                    "book": dict(BOOK) if self.src.role == "STRATEGY" else "BENCHMARK_WEIGHTS", "costs": self.costs.identity(),
                    "cash_rate": cfg.cash_rate.identity(), "segment": [cfg.start_session, cfg.end_session], "segment_label": cfg.segment_label,
                    "embargo_sessions": cfg.embargo_sessions, "initial_cash": cfg.initial_cash, "delisting_mode": cfg.delisting_mode,
                    "static_map_sha256": sha256_of(cfg.static_map) if cfg.static_map else None, "prereg_sha256": cfg.prereg_sha256,
                    "ratio_canonicalisation": None, "segment_check": self.segment_check, **self.heads, "benchmark": bench_id}
        return {"identity": identity, "identity_sha256": sha256_of(identity), "rows": self.rows, "decisions": self.decisions, "fills": self.fills,
                "not_taken": self.not_taken, "ledger_log": L.log, "flags": sorted(flags), "final_equity": self.rows[-1]["equity"],
                "initial_cash": cfg.initial_cash, "traded_notional": L.traded_notional, "total_costs": L.total_costs,
                "dividends_credited": L.dividends_credited, "receivables": list(L.receivables),
                "open_positions": [{"instrument_id": k, "shares": v.shares, "cost_basis": v.cost_basis, "mark": v.mark, "mark_session": v.mark_session,
                                    "stop_raw": v.stop_raw, "target_raw": v.target_raw} for k, v in sorted(L.positions.items())],
                "benchmark_returns": bench_returns, "disclaimer": DISCLAIMER}
