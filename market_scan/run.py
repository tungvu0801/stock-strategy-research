"""
market_scan/run.py - Stage 7.5: one offline scan as of an explicit evaluation time T (DESIGN_75_MARKET_SCAN.md sections 2, 3, 7, 8).

One call reads one store, as of T, and returns one JSON document (the operator chose a document over tables at Stage 7.5: nothing is
written anywhere):
* run      - what D.1's mi_scan_runs row holds: the inputs hash, the universe hash and the data states (the store's bytes and schema,
             the Stage 7.4 verdict and its checks), plus the run status (COMPLETED / BLOCKED / INSUFFICIENT_DATA);
* results  - what D.1's mi_scan_results rows hold, one per Tier A symbol: every feature with its state and inputs, the flags, the
             evidence classes and, per horizon, the eligibility with every reason;
* sequence - INTRADAY: the gap list (ranked); SWING: symbol order (unranked) - never named "order", a Strategy Standard forbidden key;
* the pre-registration, the deferred and blocked parts, the limitations and the disclaimer; scan_sha256 over all of it.
The same store bytes, T, feed, universe and pre-registration give the same document and hash, wherever the store file lies (paths are
kept apart, under `invocation`, outside the hash).

Read-only, through the Stage 7.4 protocol itself (quality._snapshot / _open_store / _ro, reused unchanged): every input database is copied
byte for byte into a private temporary directory and refused while a writer may be active; every read - the 7.4 report's, the 7.1 /
7.3.1 store interfaces' and this module's own SELECTs - runs on that copy, which is deleted afterwards. Quiet or offline stores only:
the copy is not an atomic snapshot of a store being written (DESIGN_74 section 2.1). The raw archive is read with plain file reads.

Point in time: every value is the store's KNOWN revision at T (observed, complete, available and ingested by T); fetch records count
when retrieved and ingested by T; a news-mention run counts when its as_of and its computed_at are both at or before T. Nothing
recorded after T reaches any feature, flag or filter.

    python -m market_scan.run --store <db> --as-of <ISO time> [--state <collector.db>] [--archive-root <raw dir>] [--feed iex|sip]
                              [--watchlist <file>] [--core-file <file>] [--format text|json]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tempfile
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

from api.validation import validate_symbol
from data.sector_map import SECTOR_MAP
from dynamic_universe import core as DUC
from market_intel import AVAILABLE, INSUFFICIENT_DATA as MI_INSUFFICIENT, NOT_APPLICABLE as MI_NOT_APPLICABLE, PRE, REG, UNAVAILABLE
from market_intel import calendar as CAL
from market_intel import freshness as FR
from market_intel import quality as Q
from market_intel import store as ST
from market_intel import validate as VA
from market_intel.news import persist as NP
from market_intel.sources import alpaca_news as AN
from market_intel.sources import earnings_av as EA
from market_intel.sources import halts as HA
from market_scan import (ATR_SESSIONS, BAR_ADJUSTMENT, BLOCKED, BLOCKED_BY_DEFERRED_7_3_WIRING, CATALYST_WINDOW_S, COMPLETED, CORE, CURVE_SERIES,
                         DEFAULT_FEED, DEFERRED, DISCLAIMER, DOLLAR_VOLUME_SESSIONS, FEEDS, HALT_RESUMPTION_S, HORIZONS, INDEX_PROXIES, INDEX_PROXY,
                         INSUFFICIENT_DATA,
                         INTRADAY, MARKET_PROXY, MIN_PRICE, NOT_PRE_REGISTERED, PRE_REGISTRATION, REGIME_TREND_SESSIONS, RVOL_PRIOR_SESSIONS,
                         SECTOR_ETF, SEC_BLOCKED, SPREAD_SNAPSHOTS, STAGE, SWING, VERSION, WATCHLIST)
from market_scan import features as F
from market_scan import filters as FI
from market_scan import flags as FG

NY = ZoneInfo("America/New_York")
ROLE_ORDER = (CORE, WATCHLIST, SECTOR_ETF, INDEX_PROXY)
EXIT_CODES = {"PASS": 0, "WARN": 1, BLOCKED: 2, INSUFFICIENT_DATA: 3}
USAGE_ERROR = 4

LIMITATIONS = (
    "No production observation has been scanned: every test store is a fixture or an offline-verification store.",
    "Quiet or offline stores only: a store being written is refused when the signs of a writer are visible, and the copy is not an atomic "
    "snapshot (DESIGN_74 section 2.1).",
    "The 7.0 quote policy has no premarket window: before 09:30 every quote reads NOT_APPLICABLE, so a PRE gap list shows its values but "
    "no candidate is eligible until a premarket quote policy is pre-registered.",
    "A missing minute bar cannot be told apart from a minute without prints from the store alone: such a session is MISSING, never zero.",
    "Bars are raw (unadjusted) and read from one feed; dollar volume reads SIP when 20 sessions exist, else IEX labelled IEX_ONLY.",
    "The universe is today's Tier A (the core, the watchlist, 12 sector ETFs, SPY / QQQ / IWM): a scan of a past T is survivorship-biased.",
    "The regime's macro flags are display only (no release schedule makes them usable under the 7.0 MACRO policy); the VIX percentile is "
    "not pre-registered.",
    "Earnings proximity reads expected dates only; SEC confirmations, like every SEC acceptance time, stay blocked.",
    "From 18:00 to the daily bar's completion (20:00; 17:00 on an early close) no swing candidate can be eligible: 7.0 expects today's bar "
    "from 18:00, the 7.1 store holds it only once complete (DAILY_BAR_DUE_BEFORE_STORABLE).",
    "ATR(14) and what uses it (ATR units, unusual volatility) are withheld for 14 sessions after any close-to-close move beyond 25 %: on "
    "raw bars a split cannot be told from a real move without a corporate-action source.",
    "Unverified offline: whether the provider rolls a snapshot's daily and previous-day bars before the open; if it does not, a "
    "premarket gap reads PREV_CLOSE_NOT_PREVIOUS_SESSION (fail closed).",
)


# ------------------------------------------------------------------------------------------------ hashing
def canonical(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def sha256(obj: Any) -> str:
    return hashlib.sha256(canonical(obj)).hexdigest()


def scan_sha256(doc: Dict[str, Any]) -> str:
    """The hash of a scan document: everything but the hash itself and the invocation paths."""
    return sha256({k: v for k, v in doc.items() if k not in ("scan_sha256", "invocation")})


# ------------------------------------------------------------------------------------------------ the universe (Tier A, D.1)
def read_watchlist(path: Optional[Any] = None) -> Tuple[List[str], List[str]]:
    """The live watchlist exactly as scanner.watchlist.load_watchlist reads it (one per line, '#' comments, upper-cased, the first
    occurrence kept; tested equal), without importing that module, which imports the market-data client. A plain read."""
    if path is None:
        import config
        path = config.WATCHLIST_FILE
    p = Path(path)
    if not p.exists():
        return [], [f"watchlist file {p.name} not found: an empty watchlist"]
    out: List[str] = []
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip().upper()
        if line and not line.startswith("#") and line not in out:
            out.append(line)
    return out, []


def universe(*, core: Optional[DUC.Core] = None, watchlist: Optional[Sequence[str]] = None, watchlist_file: Optional[Any] = None) -> Dict[str, Any]:
    """Tier A: dynamic_universe.core.load_core() (read only) + the watchlist + the 12 data/sector_map.py ETFs + SPY / QQQ / IWM. Sector
    tags are the core's (Unmapped: none). The dynamic universe stays OFF: nothing here selects, activates or writes."""
    c = core if core is not None else DUC.load_core()
    if watchlist is not None:
        wl, problems = list(watchlist), []
    else:
        wl, problems = read_watchlist(watchlist_file)
    problems = list(c.problems) + problems
    members: Dict[str, Dict[str, Any]] = {}
    core_sector = {s: sec for s, sec in c.entries}

    def add(sym: str, role: str, sector: Optional[str]) -> None:
        try:
            s = validate_symbol(str(sym))
        except ValueError:
            problems.append(f"{str(sym)[:20]!r} is not a valid ticker symbol - dropped")
            return
        m = members.setdefault(s, {"symbol": s, "roles": [], "sector": None})
        if role not in m["roles"]:
            m["roles"].append(role)
        if m["sector"] is None and sector in SECTOR_MAP:                 # Unmapped (or none) is not a sector: never guessed
            m["sector"] = sector

    for s, sec in c.entries:
        add(s, CORE, sec)
    for s in wl:
        add(s, WATCHLIST, core_sector.get(str(s).strip().upper()))
    for info in SECTOR_MAP.values():
        add(info["etf"], SECTOR_ETF, None)
    for s in INDEX_PROXIES:
        add(s, INDEX_PROXY, None)
    rows = []
    for s in sorted(members):
        m = members[s]
        rows.append({"symbol": s, "roles": [r for r in ROLE_ORDER if r in m["roles"]], "sector": m["sector"],
                     "sector_etf": SECTOR_MAP[m["sector"]]["etf"] if m["sector"] else None})
    return {"tier": "A", "symbols": rows, "count": len(rows), "sha256": sha256([[r["symbol"], r["roles"], r["sector"]] for r in rows]),
            "core": c.summary(), "watchlist": {"count": len({str(s).strip().upper() for s in wl}), "symbols": sorted({str(s).strip().upper() for s in wl})},
            "problems": problems,
            "sector_etfs": {sec: info["etf"] for sec, info in SECTOR_MAP.items()}}


# ------------------------------------------------------------------------------------------------ reading, point in time
def ny(d: date, hh: int = 0, mm: int = 0) -> datetime:
    return datetime.combine(d, time(hh, mm), tzinfo=NY).astimezone(timezone.utc)


def _back(last: Optional[date], n: int) -> Tuple[List[date], bool]:
    """The `n` market days ending at `last`, oldest first; False when the calendar does not cover them all."""
    out, x = [], last
    while x is not None and len(out) < n:
        out.append(x)
        if len(out) < n:
            x = CAL.previous_market_day(x)
    return list(reversed(out)), len(out) == n


class _Reader:
    """Every read of one scan: the 7.1 / 7.3.1 store interfaces and a few SELECTs, all on the private copy, all as of T."""

    def __init__(self, store, conn, T: datetime, feed: str, schema_version: int):
        self.store, self.conn, self.T, self.feed, self.v2 = store, conn, T, feed, schema_version >= 2
        self.at = ST.ts(T)
        self._news: Optional[Dict[str, dict]] = None
        self.jobs = {r["dataset_id"]: r["job_kind"] for r in conn.execute("SELECT dataset_id, job_kind FROM mi_datasets")}

    def evidence(self, dataset_id: Optional[str]) -> str:
        return Q.EVIDENCE.get(self.jobs.get(dataset_id), Q.UNATTRIBUTED)

    def brief(self, r: Optional[dict]) -> Optional[dict]:
        if r is None:
            return None
        keys = ("obs_id", "natural_key", "revision", "dataset_id", "provider", "feed", "adjustment", "observed_at", "event_end", "retrieved_at",
                "available_at", "ingested_at", "last_delivered_at", "record_type", "status")
        return {**{k: r.get(k) for k in keys if k in r}, "quality_flags": list(r.get("quality_flags") or []), "evidence": self.evidence(r.get("dataset_id"))}

    def digest(self, rows: Sequence[dict]) -> dict:
        ids = sorted(r["obs_id"] for r in rows)
        return {"count": len(rows), "obs_sha256": hashlib.sha256(",".join(ids).encode("utf-8")).hexdigest(),
                "datasets": sorted({r["dataset_id"] for r in rows}), "evidence": sorted({self.evidence(r["dataset_id"]) for r in rows}),
                "first_observed_at": min((r["observed_at"] for r in rows), default=None), "last_observed_at": max((r["observed_at"] for r in rows), default=None)}

    def quote(self, sym: str) -> dict:
        ls = self.store.latest_status("QUOTE_SNAPSHOT", at=self.T, symbol=sym, feed=self.feed)
        u = ls["latest_usable"] if ls["usable_is_latest"] else None
        qa = ST.parse_ts(u["observed_at"]) if u else None
        return {"latest_record": self.brief(ls["latest_record"]), "usable_is_latest": ls["usable_is_latest"],
                "reason": "NEVER_OBSERVED" if ls["latest_record"] is None else ("OK" if ls["usable_is_latest"] else ls["reason"]),
                "payload": u["payload"] if u else None, "day": FR.assess("QUOTE_DAY", qa, self.T), "swing": FR.assess("QUOTE_SWING", qa, self.T)}

    def quotes_today(self, sym: str, today: Optional[date]) -> List[dict]:
        if today is None:
            return []
        rows = self.store.history("QUOTE_SNAPSHOT", symbol=sym, feed=self.feed, start=ny(today, 9, 30), end=self.T, as_of=self.T, usable_only=True)
        return [r for r in rows if r["session"] == REG and r["session_date"] == today.isoformat()]

    def bars(self, kind: str, sym: str, feed: str, start: datetime) -> List[dict]:
        rows = self.store.history(kind, symbol=sym, feed=feed, adjustment=BAR_ADJUSTMENT, start=start, end=self.T, as_of=self.T, usable_only=True)
        return [{**r["payload"], "start": ST.parse_ts(r["observed_at"]), "session_date": date.fromisoformat(r["session_date"]), "session": r["session"],
                 "_row": r} for r in rows]

    def daily(self, sym: str, feed: str, last: Optional[date], n: int) -> Tuple[List[dict], List[str], List[dict]]:
        """The usable raw daily bars of the `n` market days ending at `last`: (present bars oldest first, missing sessions, rows)."""
        days, covered = _back(last, n)
        if not days:
            return [], ["CALENDAR_NOT_COVERED"], []
        by = {b["session_date"]: b for b in self.bars("BAR_1DAY", sym, feed, ny(days[0]))}
        present = [by[d] for d in days if d in by]
        missing = [d.isoformat() for d in days if d not in by] + ([] if covered else ["CALENDAR_NOT_COVERED"])
        return present, missing, [b["_row"] for b in present]

    def last_daily(self, sym: str) -> dict:
        """The 7.0 daily-bar state of the latest usable raw bar - only while it is the latest record: a newer absence or unusable bar makes
        it UNAVAILABLE, exactly as the store's latest_status says."""
        ls = self.store.latest_status("BAR_1DAY", at=self.T, symbol=sym, feed=self.feed, adjustment=BAR_ADJUSTMENT)
        f = ls["freshness"]                                            # assess_daily_bar of the latest usable bar (NEVER_OBSERVED without one)
        if ls["latest_record"] is None or ls["usable_is_latest"]:
            return f
        return {**f, "status": UNAVAILABLE, "usable": False, "freshness": None, "reason": ls["reason"]}

    def halts(self, sym: str) -> List[dict]:
        """Every halt of `sym` known at T (whenever it began): an unresolved halt from an earlier session is still open today."""
        return self.store.history("HALT", symbol=sym, end=self.T, as_of=self.T, usable_only=True)

    def last_ok_fetch(self, provider: str, endpoint: Optional[str] = None) -> Optional[datetime]:
        """The latest successful fetch of `provider` (and `endpoint`) retrieved and ingested by T: when the feed was last seen working. A
        time that does not parse is never a time (7.4's own reader); the next one is taken."""
        sql = (f"SELECT retrieved_at FROM mi_fetches WHERE provider = :p AND status IN ('OK', 'NOT_MODIFIED') AND {Q._le('retrieved_at')} "
               f"AND {Q._le('ingested_at')}" + (" AND endpoint = :e" if endpoint else "") + " ORDER BY retrieved_at DESC")
        for (v,) in self.conn.execute(sql, {"p": provider, "e": endpoint, "T": self.at}):
            t = Q._t(v)
            if t is not None:
                return t
        return None

    def mention_runs(self) -> int:
        if not self.v2:
            return 0
        return self.conn.execute(f"SELECT COUNT(*) FROM mi_news_runs WHERE run_kind = 'MENTIONS' AND algorithm = :a AND {Q._le('as_of')} AND "
                                 f"{Q._le('computed_at')}", {"a": NP.MENTION_ALGORITHM, "T": self.at}).fetchone()[0]

    def _articles(self) -> Dict[str, dict]:
        """Per article published within the 18 h before T: the MENTIONS run known at T (as_of and computed_at <= T) that read its LATEST
        revision known at T - the highest revision read, then the latest as_of / computed_at / run id. An article whose latest known revision
        no run has read yet is withheld (`behind`): a correction is never outweighed by the run that read the text before it."""
        if self._news is None:
            lo = ST.ts(self.T - timedelta(seconds=CATALYST_WINDOW_S))
            best: Dict[str, dict] = {}
            for r in self.conn.execute(
                    "SELECT i.natural_key, i.revision, i.obs_id, r.run_id, r.as_of, r.computed_at, o.published_at, o.dataset_id FROM mi_news_run_inputs i "
                    "JOIN mi_news_runs r ON r.run_id = i.run_id JOIN mi_observations o ON o.obs_id = i.obs_id "
                    f"WHERE r.run_kind = 'MENTIONS' AND r.algorithm = :a AND {Q._le('r.as_of')} AND {Q._le('r.computed_at')} "
                    f"AND {Q._le('o.published_at')} AND o.published_at >= :lo", {"a": NP.MENTION_ALGORITHM, "T": self.at, "lo": lo}):
                key = (int(r["revision"]), r["as_of"], r["computed_at"], r["run_id"])
                if r["natural_key"] not in best or key > best[r["natural_key"]]["key"]:
                    best[r["natural_key"]] = {**dict(r), "key": key}
            latest = {r[0]: int(r[1]) for r in self.conn.execute(
                f"SELECT o.natural_key, MAX(o.revision) FROM mi_observations o WHERE o.kind = 'NEWS' AND {Q._KNOWN.format(a='o')} GROUP BY o.natural_key",
                {"T": self.at})}
            self._news = {k: {**v, "behind": latest.get(k, v["key"][0]) > v["key"][0]} for k, v in best.items()}
        return self._news

    def mentions(self, sym: str) -> Optional[List[dict]]:
        """Catalyst-eligible mentions of `sym`, each from its article's run (_articles), in articles published within the 18 h before T. None on
        a v1 store (no mention tables)."""
        if not self.v2:
            return None
        arts = self._articles()
        out = []
        for m in self.conn.execute("SELECT natural_key, run_id, method, confidence, role FROM mi_news_mentions WHERE symbol = :s AND catalyst_eligible = 1 "
                                   "ORDER BY natural_key, run_id", {"s": sym}):
            a = arts.get(m["natural_key"])
            if a is not None and a["run_id"] == m["run_id"] and not a["behind"]:
                out.append({"natural_key": m["natural_key"], "published_at": a["published_at"], "method": m["method"], "confidence": m["confidence"],
                            "role": m["role"], "run_id": m["run_id"], "revision": a["key"][0], "obs_id": a["obs_id"], "evidence": self.evidence(a["dataset_id"])})
        return sorted(out, key=lambda x: (x["published_at"], x["natural_key"]))

    def macro(self) -> Optional[dict]:
        return self.store.latest_usable("MACRO", at=self.T, series_id=CURVE_SERIES)

    def earnings_index(self) -> Tuple[Optional[str], Dict[str, List[dict]]]:
        """(the latest calendar snapshot date known at T, its usable rows per symbol). One calendar call lists every symbol, so a symbol the
        latest snapshot no longer lists has no expected date, whatever an older snapshot said."""
        rows = self.store.history("EARNINGS", as_of=self.T, usable_only=True)
        dated = []
        for r in rows:
            try:
                dated.append((date.fromisoformat(str(r["payload"].get("snapshot_date"))).isoformat(), r))
            except ValueError:
                continue                                                # a snapshot date that does not parse is never the latest
        latest = max((d for d, _ in dated), default=None)
        by: Dict[str, List[dict]] = {}
        for d, r in dated:
            if d == latest:
                by.setdefault(r["symbol"], []).append(r)
        return latest, by


# ------------------------------------------------------------------------------------------------ one symbol
def _context(T: datetime) -> Dict[str, Any]:
    s = CAL.session(T)
    today = s["session_date"] if s["market_day"] else None
    return {"session": s["session"], "today": today, "previous": CAL.previous_market_day(today) if today else None,
            "last_completed": CAL.last_completed_session(T), "calendar": s["calendar"], "covered": s["covered"], "et_minute": s["et_minute"]}


def _rvol(rd: _Reader, sym: str, ctx: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Dict[str, Any]], dict]:
    """(relative_volume_tod, the premarket features, the minute-bar inputs). Regular session only for the relative volume: t = the end of
    today's latest delivered regular-session minute bar (graded by the 7.0 BAR_1MIN policy), m = the minutes from 09:30 to t; today and each
    of the 10 prior sessions must have a bar for every one of those minutes (an early close: up to its close), else MISSING - a missing
    minute, on IEX possibly a minute without prints, is never read as zero volume (C.13)."""
    today = ctx["today"]
    if today is None:
        na = F.feature(status=MI_NOT_APPLICABLE, codes=["CALENDAR_NOT_COVERED" if not ctx["covered"] else "NOT_A_MARKET_DAY"], unit="x")
        return na, F.premarket([], feed=rd.feed), {"count": 0}
    prior, covered = _back(CAL.previous_market_day(today), RVOL_PRIOR_SESSIONS)
    start = ny(prior[0], 9, 30) if prior else ny(today, 4)
    bars = rd.bars("BAR_1MIN", sym, rd.feed, min(start, ny(today, 4)))
    by: Dict[date, List[dict]] = {}
    for b in bars:
        by.setdefault(b["session_date"], []).append(b)
    open_at = ny(today, 9, 30)
    pre = [b for b in by.get(today, []) if b["session"] == PRE]
    pm = F.premarket(pre, feed=rd.feed, minutes_elapsed=max(0, int((min(rd.T, open_at) - ny(today, 4)).total_seconds() // 60)))
    inputs = rd.digest([b["_row"] for b in bars])
    if ctx["session"] != REG:
        return F.feature(status=MI_NOT_APPLICABLE, codes=["OUTSIDE_REGULAR_SESSION"], unit="x"), pm, inputs
    reg = lambda d: [b for b in by.get(d, []) if b["session"] == REG]  # noqa: E731
    if not reg(today):
        return F.feature(codes=["NO_BARS_TODAY"], unit="x"), pm, inputs
    t_end = max(b["start"] for b in reg(today)) + timedelta(minutes=1)
    minutes = min(int((t_end - open_at).total_seconds() // 60), CAL.regular_minutes(today) or 0)
    cum_today = F.cumulative_volume(reg(today), open_at=open_at, minutes=minutes)
    cums = [F.cumulative_volume(reg(d), open_at=ny(d, 9, 30), minutes=min(minutes, CAL.regular_minutes(d) or 0)) for d in prior]
    missing = [d.isoformat() for d, v in zip(prior, cums) if v is None] + ([] if covered else ["CALENDAR_NOT_COVERED"])
    f = F.relative_volume_tod(cum_today, cums, minutes=minutes, missing=missing, today_code="MINUTES_MISSING")   # fewer than 10: the calendar ends
    return F.graded(f, FR.assess("BAR_1MIN", t_end, rd.T)), pm, inputs


def _symbol(rd: _Reader, member: Dict[str, Any], ctx: Dict[str, Any], feeds: Dict[str, dict], earnings: Tuple[Optional[str], Dict[str, List[dict]]]
            ) -> Dict[str, Any]:
    sym, today, prev, last = member["symbol"], ctx["today"], ctx["previous"], ctx["last_completed"]
    q = rd.quote(sym)
    snap = q["payload"]
    day = q["day"]
    fs: Dict[str, Any] = {}
    fs["gap_pct"] = F.graded(F.gap_pct(snap, session=ctx["session"], today=today, previous=prev), day)
    fs["price_intraday"] = F.graded(F.last_trade(snap, today=today, sessions=(PRE, REG)), day)
    atr_bars, atr_missing, atr_rows = rd.daily(sym, rd.feed, last, ATR_SESSIONS + 1)
    fs["atr_14"] = F.atr(atr_bars, missing=atr_missing)
    fs["intraday_move"] = F.graded(F.intraday_move(F.last_trade(snap, today=today, sessions=(REG,)), snap, fs["atr_14"], today=today), day)
    fs["unusual_volatility"] = F.graded(F.unusual_volatility(snap, fs["atr_14"], today=today), day)
    fs["day_change"] = F.graded(F.day_change(snap, today=today, previous=prev), day)
    fs["relative_volume_tod"], pm, minute_inputs = _rvol(rd, sym, ctx)
    fs.update(pm)
    sip, sip_missing, sip_rows = rd.daily(sym, "sip", last, DOLLAR_VOLUME_SESSIONS)
    if not sip_missing:
        fs["dollar_volume_20d"], dv_rows = F.dollar_volume(sip, label=["SIP"]), sip_rows
    else:
        iex, iex_missing, dv_rows = rd.daily(sym, "iex", last, DOLLAR_VOLUME_SESSIONS)
        fs["dollar_volume_20d"] = F.dollar_volume(iex, label=["IEX_ONLY"], missing=iex_missing)
    spread_rows = rd.quotes_today(sym, today)
    fs["spread_bps"] = F.graded(F.spread_bps([r["payload"] for r in spread_rows], feed=rd.feed), day)
    close = atr_bars[-1] if atr_bars and atr_bars[-1]["session_date"] == last else None
    fs["price_swing"] = (F.feature(F.num(close["close"]), AVAILABLE, unit="USD", session=last.isoformat()) if close and (F.num(close["close"]) or 0) > 0
                         else F.feature(codes=["LAST_SESSION_BAR_MISSING"], unit="USD"))
    fs["sector_strength"] = F.feature(status=MI_NOT_APPLICABLE, codes=["NO_SECTOR"], unit="pp")      # completed once every symbol is read
    halts = rd.halts(sym)
    snapshot, by_symbol = earnings
    fl = {"halt": FG.halt_flag(halts, feeds["halt"], now=rd.T),
          "catalyst": FG.catalyst_flag(rd.mentions(sym), news_poll=feeds["news"], runs_known=feeds["mention_runs"]),
          "earnings_proximity": FG.earnings_proximity(by_symbol.get(sym), feeds["earnings_calendar"], today=today or last, snapshot=snapshot,
                                                      evidence=lambda r: rd.evidence(r["dataset_id"]))}
    inputs = {"quote": q["latest_record"], "daily_bars": rd.digest(atr_rows), "dollar_volume_bars": rd.digest(dv_rows), "minute_bars": minute_inputs,
              "spread_snapshots": rd.digest(spread_rows), "halts": [h["obs_id"] for h in halts]}
    ev = {e for k in ("daily_bars", "dollar_volume_bars", "minute_bars", "spread_snapshots") for e in inputs[k].get("evidence", [])}
    ev |= {inputs["quote"]["evidence"]} if inputs["quote"] else set()
    ev |= {a["evidence"] for a in fl["catalyst"].get("articles", [])} | {rd.evidence(h["dataset_id"]) for h in halts}
    ev |= {fl["earnings_proximity"]["evidence"]} if fl["earnings_proximity"].get("evidence") else set()
    return {"symbol": sym, "roles": member["roles"], "sector": member["sector"], "sector_etf": member["sector_etf"], "features": fs, "flags": fl,
            "quote": {"present": q["latest_record"] is not None, "usable_is_latest": q["usable_is_latest"], "reason": q["reason"], "day": q["day"],
                      "swing": q["swing"]}, "daily": rd.last_daily(sym), "inputs": inputs,
            "evidence": sorted(ev), "problems": []}


def _placeholder(member: Dict[str, Any], code: str, exc: Optional[Exception] = None) -> Dict[str, Any]:
    """A symbol nothing could be computed for - its inputs do not read as written (SYMBOL_INPUT_UNREADABLE), or the store was not read at
    all (INPUT_NOT_READ): every feature empty, every flag unknown, ineligible everywhere. Every symbol still has its result."""
    na = F.feature(codes=[code])
    fs = {k: na for k in ("gap_pct", "price_intraday", "atr_14", "intraday_move", "unusual_volatility", "day_change", "relative_volume_tod",
                          "premarket_volume", "premarket_range", "dollar_volume_20d", "spread_bps", "price_swing", "sector_strength")}
    unknown = FG.flag("UNKNOWN", [code])
    never = {"status": "UNAVAILABLE", "usable": False, "reason": code}
    return {"symbol": member["symbol"], "roles": member["roles"], "sector": member["sector"], "sector_etf": member["sector_etf"], "features": fs,
            "flags": {"halt": unknown, "catalyst": FG.flag("NOT_ESTABLISHED", [code]), "earnings_proximity": unknown},
            "quote": {"present": False, "usable_is_latest": False, "reason": code, "day": never, "swing": never}, "daily": never,
            "inputs": {"quote": None, **({"error": type(exc).__name__} if exc is not None else {})}, "evidence": [],
            "problems": [code] if code == "SYMBOL_INPUT_UNREADABLE" else []}


# ------------------------------------------------------------------------------------------------ the scan
def _archive_digest(root: Optional[Path]) -> Optional[dict]:
    if root is None:
        return None
    out = {"present": root.is_dir()}
    for name in ("anchors.jsonl", "manifest.jsonl"):
        p = root / name
        out[name] = hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None
    return out


def _quality_summary(rep: dict) -> dict:
    v = rep["verdict"]
    return {"report": rep["report"], "verdict": v["result"], "evidence_scope": v["evidence_scope"], "rule": v["rule"],
            "checks": [{"check": c["check"], "result": c["result"], "codes": c["codes"], "required": c["required"]} for c in rep["checks"]],
            "store_sha256": rep["inputs"]["store"].get("sha256"), "not_proof_of_live_readiness": True}


def run_scan(store, *, as_of: datetime, state=None, archive_root=None, feed: str = DEFAULT_FEED, core: Optional[DUC.Core] = None,
             watchlist: Optional[Sequence[str]] = None, watchlist_file=None, tmp_root=None) -> Dict[str, Any]:
    """The Stage 7.5 scan as of `as_of` (module docstring). Writes nothing; returns the JSON-ready document."""
    T = Q._aware(as_of, "as_of")
    if feed not in FEEDS:
        raise ValueError(f"feed must be one of {', '.join(FEEDS)}")
    uni = universe(core=core, watchlist=watchlist, watchlist_file=watchlist_file)
    store_path, state_path = Path(store), (Path(state) if state is not None else None)
    archive = Path(archive_root) if archive_root is not None else None
    ctx = _context(T)
    with tempfile.TemporaryDirectory(prefix="mi_scan_", dir=str(tmp_root) if tmp_root else None) as tmp:
        snap = Q._snapshot(store_path, Path(tmp) / "store.db")
        st_snap = Q._snapshot(state_path, Path(tmp) / "collector.db") if state_path is not None else None
        doc = _scan(T, feed, uni, ctx, snap, st_snap, archive, tmp)
    doc["scan_sha256"] = scan_sha256(doc)
    doc["invocation"] = {"store": str(store_path), "state": None if state_path is None else str(state_path), "archive_root": None if archive is None
                         else str(archive)}
    return doc


def _scan(T, feed, uni, ctx, snap, st_snap, archive, tmp) -> Dict[str, Any]:
    pub = lambda s: None if s is None else {k: v for k, v in s.items() if not k.startswith("_") and k != "path"}  # noqa: E731
    states = {"store": pub(snap), "collector_state": pub(st_snap), "archive": _archive_digest(archive)}
    inputs_sha = sha256({"scan": VERSION, "pre_registration": PRE_REGISTRATION, "as_of": ST.ts(T), "feed": feed, "universe": uni["sha256"],
                         "store": snap.get("sha256"), "collector_state": None if st_snap is None else st_snap.get("sha256"), "archive": states["archive"]})
    run = {"status": None, "codes": [], "inputs_sha256": inputs_sha, "universe_sha256": uni["sha256"], "data_states": states, "quality": None, "gate": None}
    results: Dict[str, Dict[str, Any]] = {}
    context: Dict[str, Any] = {"regime": None, "feeds": None}
    refused = [(n, s) for n, s in (("STORE", snap), ("STATE", st_snap)) if s is not None and s.get("refused")]
    missing = [n for n, s in (("STORE", snap), ("STATE", st_snap)) if s is not None and not s.get("present")]
    if refused:
        run.update(status=BLOCKED, codes=[f"{n}_{s['refused']}" for n, s in refused])
    elif missing:
        run.update(status=INSUFFICIENT_DATA, codes=[f"{n}_NOT_CREATED" for n in missing])
    else:
        rep = Q.build_report(snap["_copy"], as_of=T, state=None if st_snap is None else st_snap["_copy"], archive_root=archive, tmp_root=tmp)
        run["quality"] = _quality_summary(rep)
        if run["quality"]["store_sha256"] != snap["sha256"]:
            raise AssertionError("the 7.4 report read other bytes than the scan")         # a private copy of a private copy: never
        gate = FI.quality_gate(rep["verdict"]["result"], warnings=rep["verdict"]["warnings"])
        run["gate"] = gate
        store, info = Q._open_store(snap["_copy"], T)
        if store is None:
            run.update(status=BLOCKED if rep["verdict"]["result"] == FI.BLOCK else INSUFFICIENT_DATA, codes=[f"STORE_{info['state']}"])
            run["gate"] = {**gate, "actionable": False, "codes": sorted(set(gate["codes"]) | {f"STORE_{info['state']}"})}
        else:
            conn = Q._ro(snap["_copy"])
            try:
                results, context = _read_all(_Reader(store, conn, T, feed, info["schema_version"]), uni, ctx)
                unreadable = sorted(s for s, r in results.items() if r["problems"])
                run.update(status=COMPLETED if gate["actionable"] else (BLOCKED if gate["verdict"] == FI.BLOCK else INSUFFICIENT_DATA),
                           codes=sorted(set(gate["codes"]) | ({"SYMBOL_INPUT_UNREADABLE"} if unreadable else set())))
            except Exception as exc:                                     # run-level data that does not read as written: nothing is vouched for
                results, context = {}, {"regime": None, "feeds": None, "error": type(exc).__name__}
                run["gate"] = {**gate, "actionable": False, "codes": sorted(set(gate["codes"]) | {"SCAN_INPUT_UNREADABLE"})}
                run.update(status=BLOCKED, codes=sorted(set(gate["codes"]) | {"SCAN_INPUT_UNREADABLE"}))
            finally:
                conn.close()
    if not results:                                                      # nothing read: every symbol still has its (ineligible) result
        results = {m["symbol"]: _placeholder(m, "INPUT_NOT_READ") for m in uni["symbols"]}
    window = (ctx["today"] is not None and ctx["session"] in (PRE, REG),
              "CALENDAR_NOT_COVERED" if not ctx["covered"] else ("NOT_A_MARKET_DAY" if ctx["today"] is None else f"OUTSIDE_DAY_WINDOW_{ctx['session']}"))
    gate = run["gate"] or {"actionable": False, "codes": list(run["codes"]), "verdict": None}      # no 7.4 report ran: an input was refused
    for r in results.values():
        r["horizons"] = {INTRADAY: FI.intraday(r, gate, window=window), SWING: FI.swing(r, gate)}
    sequence = {h: FI.sequence(results, h) for h in HORIZONS}
    due = (ctx["today"] is not None and ctx["et_minute"] is not None
           and CAL.DAILY_BAR_CUTOFF_MINUTE <= ctx["et_minute"] < (CAL.post_market_end_minute(ctx["today"]) or 0))
    summary = {h: {"applies": window[0] if h == INTRADAY else True, "reason": None if h == SWING or window[0] else window[1],
                   "eligible": sum(r["horizons"][h]["eligible"] for r in results.values()),
                   "ranked": sum(r["horizons"][h]["rank"] is not None for r in results.values()), "symbols": len(results),
                   # 7.0 expects today's daily bar from 18:00; the 7.1 store holds it only from its completion (20:00, 17:00 on an early close)
                   "codes": ["DAILY_BAR_DUE_BEFORE_STORABLE"] if h == SWING and due else []} for h in HORIZONS}
    return {"scan": VERSION, "stage": STAGE, "pre_registration": PRE_REGISTRATION, "as_of": ST.ts(T), "feed": feed,
            "session": {"session": ctx["session"], "session_date": None if ctx["today"] is None else ctx["today"].isoformat(),
                        "previous_session": None if ctx["previous"] is None else ctx["previous"].isoformat(),
                        "last_completed_session": None if ctx["last_completed"] is None else ctx["last_completed"].isoformat(), "calendar": ctx["calendar"]},
            "run": run, "universe": uni, "context": context, "horizons": summary, "sequence": sequence,
            "results": [results[s] for s in sorted(results)], "pre_registered": pre_registered(),
            "deferred": {DEFERRED: list(BLOCKED_BY_DEFERRED_7_3_WIRING)}, "not_pre_registered": list(NOT_PRE_REGISTERED),
            "blocked": {SEC_BLOCKED: "8-K filings and earnings releases (SEC acceptance times): the catalyst flag is never FALSE, earnings are "
                                     "expected dates only"},
            "limitations": list(LIMITATIONS), "disclaimer": DISCLAIMER}


def _read_all(rd: _Reader, uni: Dict[str, Any], ctx: Dict[str, Any]) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Any]]:
    feeds = {"halt": FR.assess("HALT_FEED", rd.last_ok_fetch(HA.PROVIDER), rd.T),
             "news": FR.assess("NEWS_POLL", rd.last_ok_fetch(AN.PROVIDER, AN.ENDPOINT), rd.T),
             "earnings_calendar": FR.assess_earnings_calendar(rd.last_ok_fetch(EA.PROVIDER), rd.T), "mention_runs": rd.mention_runs()}
    earnings = rd.earnings_index()
    results: Dict[str, Dict[str, Any]] = {}
    for m in uni["symbols"]:
        try:
            results[m["symbol"]] = _symbol(rd, m, ctx, feeds, earnings)
        except Exception as exc:                                                     # data that does not read as written: fail closed
            results[m["symbol"]] = _placeholder(m, "SYMBOL_INPUT_UNREADABLE", exc)
    spy = results.get(MARKET_PROXY)
    for r in results.values():
        etf = results.get(r["sector_etf"]) if r["sector_etf"] else None
        if r["sector"] and not r["problems"]:
            r["features"]["sector_strength"] = F.sector_strength(r["sector"], r["sector_etf"], etf["features"]["day_change"] if etf else None,
                                                                 spy["features"]["day_change"] if spy else None)
    try:
        bars, missing, _ = rd.daily(MARKET_PROXY, rd.feed, ctx["last_completed"], REGIME_TREND_SESSIONS)
        run_ = []
        for b in reversed(bars):                                  # the contiguous run of present sessions ending at the last completed one
            if run_ and CAL.previous_market_day(run_[-1]["session_date"]) != b["session_date"]:
                break
            run_.append(b)
        reg = FG.regime(list(reversed(run_)), rd.last_daily(MARKET_PROXY), last_session=ctx["last_completed"], macro=rd.macro(), now=rd.T)
        reg["inputs"] = rd.digest([b["_row"] for b in run_])
    except Exception as exc:                                                         # fail closed: the regime is unknown
        reg = {"spy_trend": "UNKNOWN", "spy_vol": "UNKNOWN", "codes": ["REGIME_INPUT_UNREADABLE"], "status": MI_INSUFFICIENT, "error": type(exc).__name__}
    return results, {"regime": reg, "feeds": feeds}


def pre_registered() -> Dict[str, Any]:
    """Every pre-registered number and rule the scan applies (DESIGN_75 section 4), reported in every document."""
    return {"version": PRE_REGISTRATION, "min_price": MIN_PRICE, "rvol_prior_sessions": RVOL_PRIOR_SESSIONS, "atr_sessions": ATR_SESSIONS,
            "dollar_volume_sessions": DOLLAR_VOLUME_SESSIONS, "spread_snapshots": SPREAD_SNAPSHOTS, "halt_resumption_s": HALT_RESUMPTION_S,
            "catalyst_window_s": CATALYST_WINDOW_S, "regime_trend_sessions": REGIME_TREND_SESSIONS, "curve_series": CURVE_SERIES,
            "bar_adjustment": BAR_ADJUSTMENT, "freshness_policy_version": FR.POLICY_VERSION, "day_jump_limit": VA.DAY_JUMP,
            "quote_policy_intraday": "QUOTE_DAY", "halt_policy": "HALT_FEED", "news_policy": "NEWS_POLL", "earnings_policy": "EARNINGS_CALENDAR",
            "sequence": {INTRADAY: "gap_pct descending, ties by symbol, eligible symbols with a usable gap only", SWING: "symbol order, unranked"},
            "gate": "Stage 7.4 verdict: BLOCK and INSUFFICIENT_DATA leave nothing eligible; WARN proceeds with its codes; PASS proceeds"}


# ------------------------------------------------------------------------------------------------ output
def dumps(doc: Dict[str, Any]) -> str:
    return json.dumps(doc, sort_keys=True, indent=1, ensure_ascii=False, allow_nan=False) + "\n"


def render_summary(doc: Dict[str, Any]) -> str:
    run = doc["run"]
    q = run["quality"] or {}
    lines = ["Shared market scanner (Stage 7.5; offline, read-only; research candidates only)",
             f"As of {doc['as_of']}   session {doc['session']['session']} {doc['session']['session_date'] or ''}   feed {doc['feed']}",
             f"RUN: {run['status']}  {' '.join(run['codes'])}",
             f"QUALITY: {q.get('verdict')}  EVIDENCE: {q.get('evidence_scope')} - {Q.SCOPE_TEXT.get(q.get('evidence_scope'), '')}",
             f"Universe: Tier A, {doc['universe']['count']} symbols (sha256 {doc['universe']['sha256'][:12]})"]
    for h in HORIZONS:
        s = doc["horizons"][h]
        lines.append(f"{h:<9} {'applies' if s['applies'] else 'not applicable (' + str(s['reason']) + ')'}: {s['eligible']} eligible of {s['symbols']}"
                     + (f", {s['ranked']} ranked" if h == INTRADAY else ""))
    by = {r["symbol"]: r for r in doc["results"]}
    for s in doc["sequence"][INTRADAY]:
        r = by[s]["horizons"][INTRADAY]
        if r["rank"] is None:
            break
        lines.append(f"  #{r['rank']:<3} {s:<6} gap {by[s]['features']['gap_pct']['value']:+.2f} %")
    lines += [doc["disclaimer"], f"scan_sha256: {doc['scan_sha256']}"]
    return "\n".join(lines) + "\n"


def main(argv: Optional[List[str]] = None, out=None) -> int:
    out = out or sys.stdout
    ap = argparse.ArgumentParser(prog="python -m market_scan.run", description="Stage 7.5 shared market scanner (offline, read-only, research only)",
                                 epilog="Quiet or offline stores only. Research candidates are not recommendations, strategy signals or orders.")
    ap.add_argument("--store", required=True)
    ap.add_argument("--as-of", required=True)
    ap.add_argument("--state")
    ap.add_argument("--archive-root")
    ap.add_argument("--feed", choices=FEEDS, default=DEFAULT_FEED)
    ap.add_argument("--watchlist")
    ap.add_argument("--core-file")
    ap.add_argument("--format", choices=("text", "json"), default="text")
    try:
        a = ap.parse_args(argv)
        as_of = Q._aware(Q._iso(a.as_of), "as_of")
    except SystemExit as exc:                                            # --help exits 0; a refused invocation exits 4
        return 0 if exc.code == 0 else USAGE_ERROR
    except ValueError as exc:
        out.write(f"refused: {exc}\n")
        return USAGE_ERROR
    try:
        doc = run_scan(a.store, as_of=as_of, state=a.state, archive_root=a.archive_root, feed=a.feed,
                       core=DUC.load_core(a.core_file) if a.core_file else None, watchlist_file=a.watchlist)
    except Exception as exc:                                             # nothing could be vouched for: as a BLOCK
        out.write(f"error: the scan could not be run ({type(exc).__name__}); nothing is vouched for\n")
        return EXIT_CODES[BLOCKED]
    out.write(dumps(doc) if a.format == "json" else render_summary(doc))
    st = doc["run"]["status"]
    return EXIT_CODES[doc["run"]["quality"]["verdict"]] if st == COMPLETED else EXIT_CODES[st]


if __name__ == "__main__":
    raise SystemExit(main())
