"""
tools/evaluator_capacity.py - Stage 6.10C OFFLINE capacity measurement of the setup evaluator (DESIGN_610C_DECISION_FLOW.md section 9).

It runs the REAL scheduled evaluation path - SetupService.evaluate_all -> the PPE and Market Demand providers -> the RealMarketData caches ->
the setup engine -> SetupStore -> the ProposalService listener and its safety evaluation - for N symbols against a FAKE market-data client
injected through the existing seam RealMarketData(client_factory=..., now_fn=...), so the real caching and batching decide how many requests
a cycle makes. Per N and injected request latency it reports the cold (first) and warm (median of the others) cycle seconds, ms per symbol,
requests per cycle by kind and requests per minute, beside request_budget(), the pure request-budget model.

  universe  the 10 live symbols (watchlist.txt on 2026-10-27, in file order = the evaluator's order), then Core universe v1, then synthetic
            tickers SYNAA, SYNAB, ... that no sector map knows (SPY benchmark, like CLS and SHOP)
  clock     simulated, from 11:00 New York time on 2026-10-27: cycle i starts where the 60 s scheduler would start it (back to back after an
            overrun) and runs in real time inside a cycle, so the 3 s quote, 60 s bar and 900 s daily-bar caches age as they do live. The
            proposal and safety services' clocks and the session hint are bound to it, so the result does not depend on the real time of day
  latency   every fake request sleeps the injected latency (per response page): 130 ms reproduces the live 10-symbol cycle span

Offline by construction:
  * main() sets STOCK_AGENT_ENV=test, blanks every credential and turns the background services off BEFORE config is imported; every
    measure() run points the database singleton at its own temporary database before any store exists and restores it afterwards
  * while measuring, socket creation, DNS and connects raise (attempts are recorded), api.server cannot be imported, and the paper-broker,
    paper-read and evidence network wires are tripwires (a hit fails the run); no market-data client is ever built
  * nothing is written outside the work directory (start it with python -B so no bytecode is cached either)

Usage: python -B -m tools.evaluator_capacity --symbols 10,50,100,200 --latency-ms 0,130 --cycles 3 --out capacity.json [--markdown capacity.md]
"""
from __future__ import annotations

import argparse
import bisect
import contextlib
import json
import logging
import math
import os
import random
import shutil
import socket
import sqlite3
import statistics
import sys
import tempfile
import time
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence

UTC = timezone.utc
TOOL_VERSION = "evaluator_capacity_v1"
LIVE_SYMBOLS = ("NVDA", "AMD", "MU", "SNDK", "AVGO", "MRVL", "LLY", "ORCL", "SHOP", "CLS")   # watchlist.txt on 2026-10-27, file order
SYNTHETIC_PREFIX = "SYN"                                        # SYNAA ... SYNZZ: unknown to every sector map (SPY benchmark)
MAX_SYMBOLS = 500
SIM_DAY = date(2026, 10, 27)                                    # a Tuesday, EDT: the regular session is 13:30-20:00 UTC
SIM_START = datetime(2026, 10, 27, 15, 0, tzinfo=UTC)           # 11:00 New York time
SESSION_OPEN_UTC = (13, 30)                                     # 09:30 EDT (every simulated session falls in EDT)
DAILY_HISTORY = 320                                             # synthetic daily bars per symbol (covers the 420-day PPE lookback)
INTERVAL_S = 60.0                                               # SETUP_EVAL_INTERVAL_SECONDS (live default)
MIN_GAP_S = 1.0                                                 # setups/scheduler.py: wait = max(1 s, next_at - now)
BURST_EVERY_S = 900.0                                           # PPE_BARS_CACHE_SECONDS: the daily-bar batches refresh every 15 minutes
CALIBRATED_LATENCY_MS = 130.0                                   # reproduces the live 10-symbol cycle span (median 4 s) offline

# request_budget() defaults: requests per symbol per evaluator cycle in steady state, from this tool's warm cycles
PER_SYMBOL_REQUESTS = {"own_snapshot": 1.0, "benchmark_snapshot": 0.5, "minute_bars": 0.5, "proposal_daily_bars_60d": 1.0}
CPU_MS_PER_SYMBOL = 40.0                                        # CPU + SQLite per symbol at 0 ms latency (machine dependent: 33-99 ms seen)
BURST_PER_SYMBOL = 1.0                                          # 420-day + 120-day daily-bar batches per symbol every 15 minutes
TAB_REQUESTS_PER_MIN = 27.0                                     # one open Trader View tab: ~20 active quotes + 6 watchlist snapshots + 1
POSITION_REQUESTS_PER_MIN = 2.0                                 # the executor: one snapshot per open paper position every 30 s

LIVE_REFERENCE = {"date": "2026-10-27", "symbols": 10, "evaluator_cycles": 390,
                  "evaluator_cycle_span_s": {"median": 4, "p95": 8, "max": 19},
                  "executor_cycle_ms": {"median": 594, "runs": 533, "candidates": 0},
                  "source": ("persisted data only: max(ppe_computed_at, demand_computed_at) - evaluated_at of each 10-symbol setup_evaluations "
                             "batch (1 s resolution; excludes the last symbol's proposal step), and execution_runs.duration_ms of the "
                             "PAPER_AUTO cycles")}

CREDENTIAL_ENV = ("ALPACA_API_KEY", "ALPACA_SECRET_KEY", "ALPACA_PAPER_API_KEY", "ALPACA_PAPER_SECRET_KEY", "ANTHROPIC_API_KEY",
                  "ANTHROPIC_WORKSPACE_ID", "FRED_API_KEY", "ROBINHOOD_USERNAME", "ROBINHOOD_PASSWORD", "SEC_USER_AGENT")
CREDENTIAL_MARKERS = ("API_KEY", "SECRET", "TOKEN", "PASSWORD", "PASSWD", "CREDENTIAL")
SERVICES_OFF = {"STOCK_AGENT_ENV": "test", "SETUP_EVALUATOR_ENABLED": "false", "EXECUTION_SCHEDULER_ENABLED": "false",
                "ACCOUNT_REFRESH_ENABLED": "false", "CASH_FLOW_ENABLED": "false", "FORWARD_VALIDATION_ENABLED": "false",
                "AUTORUN_SUPERVISOR_ENABLED": "false", "EVIDENCE_NETWORK_ENABLED": "false", "EVENT_WARMUP_ON_STARTUP": "false",
                "ROBINHOOD_PORTFOLIO_ENABLED": "false", "PORTFOLIO_AWARENESS_ENABLED": "false", "PORTFOLIO_POLICY_ENABLED": "false"}
BLOCKED_IMPORTS = ("api.server",)
WIRE_MODULES = ("execution.broker", "paper.alpaca_readonly", "evidence.net")             # imported and tripwired for every run
LOADED_WIRE_MODULES = ("paper.alpaca_order_reads", "paper.alpaca_order_writer")          # tripwired when something else loaded them


# ================================================================ the pure request-budget model ==============================================
def request_budget(n_symbols: int, *, latency_ms: float = CALIBRATED_LATENCY_MS, cpu_ms_per_symbol: float = CPU_MS_PER_SYMBOL,
                   per_symbol: Optional[Dict[str, float]] = None, burst_per_symbol: float = BURST_PER_SYMBOL, interval_s: float = INTERVAL_S,
                   min_gap_s: float = MIN_GAP_S, burst_every_s: float = BURST_EVERY_S, trader_view_tabs: int = 0,
                   tab_requests_per_min: float = TAB_REQUESTS_PER_MIN, open_positions: int = 0,
                   limit_per_min: Optional[float] = None) -> Dict[str, Any]:
    """The evaluator's expected market-data load for N symbols. Pure arithmetic (no I/O, no clock):
      requests / cycle  = N x sum(per_symbol)          own snapshot 1 + benchmark snapshot ~0.5 + minute bars ~0.5 + proposal 60-day bars 1
      cycle seconds     = N x (cpu + sum(per_symbol) x latency)            one thread, one symbol after another
      period seconds    = max(interval, cycle + 1 s)                       a longer cycle is followed by the next one 1 s later
      requests / minute = per cycle x 60 / period + the 15-minute daily-bar burst averaged per minute + Trader View tabs + open positions
    `limit_per_min` (a market-data plan's limit - verify the account's own) adds `over_limit`."""
    per = dict(PER_SYMBOL_REQUESTS if per_symbol is None else per_symbol)
    n = max(0, int(n_symbols))
    req_symbol = sum(per.values())
    per_cycle = n * req_symbol
    cycle_s = n * (cpu_ms_per_symbol + req_symbol * latency_ms) / 1000.0
    period_s = max(interval_s, cycle_s + min_gap_s)
    evaluator_per_min = per_cycle * 60.0 / period_s
    burst = n * burst_per_symbol
    burst_per_min = burst * 60.0 / burst_every_s
    tabs = trader_view_tabs * tab_requests_per_min
    positions = open_positions * POSITION_REQUESTS_PER_MIN
    total = evaluator_per_min + burst_per_min + tabs + positions
    return {"n": n, "latency_ms": latency_ms, "requests_per_symbol_per_cycle": round(req_symbol, 3), "requests_per_cycle": round(per_cycle, 1),
            "cycle_s": round(cycle_s, 2), "period_s": round(period_s, 2), "over_interval": cycle_s + min_gap_s > interval_s,
            "evaluations_per_symbol_per_min": round(60.0 / period_s, 3), "evaluator_requests_per_min": round(evaluator_per_min, 1),
            "burst_requests": round(burst, 1), "burst_every_s": burst_every_s, "burst_requests_per_min_avg": round(burst_per_min, 1),
            "trader_view_requests_per_min": round(tabs, 1), "position_requests_per_min": round(positions, 1),
            "total_requests_per_min": round(total, 1), "limit_per_min": limit_per_min,
            "over_limit": (total > limit_per_min) if limit_per_min is not None else None}


# ================================================================ universe, clock, fake market data ==========================================
def universe(n: int) -> List[str]:
    """N symbols: the live 10 in watchlist order, then Core universe v1, then synthetic tickers (SYNAA, SYNAB, ...)."""
    from dynamic_universe.core import CORE_V1
    if not 1 <= n <= MAX_SYMBOLS:
        raise ValueError(f"N must be 1..{MAX_SYMBOLS}")
    out = list(LIVE_SYMBOLS)
    out += [s for s, _ in CORE_V1 if s not in out]
    i = 0
    while len(out) < n:
        a, b = divmod(i, 26)
        out.append(f"{SYNTHETIC_PREFIX}{chr(65 + a)}{chr(65 + b)}")
        i += 1
    return out[:n]


class SimClock:
    """Simulated wall clock. Cycle i starts at max(start of cycle i-1 + interval, end of cycle i-1 + 1 s) - the scheduler's rule - and the
    clock runs in real time inside a cycle (and between cycles while the tool does its bookkeeping)."""

    def __init__(self, start: datetime = SIM_START, interval_s: float = INTERVAL_S, min_gap_s: float = MIN_GAP_S):
        self.base = start.timestamp()
        self.interval_s, self.min_gap_s = float(interval_s), float(min_gap_s)
        self._start: Optional[float] = None
        self._t0 = time.perf_counter()

    def begin(self) -> float:
        start = self.base if self._start is None else max(self._start + self.interval_s, self.time() + self.min_gap_s)
        self._start, self._t0 = start, time.perf_counter()
        return start

    def time(self) -> float:
        return (self.base if self._start is None else self._start) + (time.perf_counter() - self._t0)

    def dt(self) -> datetime:
        return datetime.fromtimestamp(self.time(), tz=UTC)

    def session_hint(self) -> str:
        """RealMarketData._session_hint's rule on the simulated clock (EDT: every simulated moment is in EDT)."""
        et = datetime.fromtimestamp(self.time() - 4 * 3600, tz=UTC)
        if et.weekday() >= 5:
            return "WEEKEND"
        m = et.hour * 60 + et.minute
        return "REGULAR" if 570 <= m < 960 else "PRE_MARKET" if 240 <= m < 570 else "AFTER_HOURS" if 960 <= m < 1200 else "CLOSED"


def _weekdays_back(end: date, n: int) -> List[date]:
    out, d = [], end
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= timedelta(days=1)
    return out[::-1]


class _Series:
    """One symbol's synthetic history, generated once, with the indexes that keep the fake's own cost per call negligible."""

    def __init__(self, symbol: str):
        rng = random.Random(f"{symbol}:daily")                # seeded by name: identical across runs and processes
        px, self.daily = 50 + rng.random() * 400, []
        for d in _weekdays_back(SIM_DAY - timedelta(days=1), DAILY_HISTORY):
            o = px
            px = max(1.0, px * (1 + rng.gauss(0.0005, 0.02)))
            hi, lo = max(o, px) * (1 + abs(rng.gauss(0, 0.006))), min(o, px) * (1 - abs(rng.gauss(0, 0.006)))
            self.daily.append(SimpleNamespace(timestamp=datetime(d.year, d.month, d.day, 4, 0, tzinfo=UTC), open=o, high=hi, low=lo, close=px,
                                              volume=float(rng.randint(500_000, 9_000_000))))
        self.daily_dates = [b.timestamp.date() for b in self.daily]
        self.prior = [b for d in _weekdays_back(SIM_DAY - timedelta(days=1), 4) for b in self._session(symbol, d)]
        self.prior_t = [b.timestamp.timestamp() for b in self.prior]
        self.today = self._session(symbol, SIM_DAY)
        self.today_t = [b.timestamp.timestamp() for b in self.today]
        self.cum_volume, self.cum_high, self.cum_low = [], [], []
        vol, hi, lo = 0.0, float("-inf"), float("inf")
        for b in self.today:                                  # running day aggregates for the snapshot's daily bar
            vol, hi, lo = vol + b.volume, max(hi, b.high), min(lo, b.low)
            self.cum_volume.append(vol)
            self.cum_high.append(hi)
            self.cum_low.append(lo)

    def _session(self, symbol: str, d: date) -> list:
        """390 regular-session minute bars with a U-shaped volume profile."""
        rng = random.Random(f"{symbol}:{d.isoformat()}")
        px, out = self.daily[-1].close * (1 + rng.gauss(0, 0.01)), []
        start = datetime(d.year, d.month, d.day, *SESSION_OPEN_UTC, tzinfo=UTC)
        for i in range(390):
            o = px
            px = max(1.0, px * (1 + rng.gauss(0, 0.0012)))
            u = 1.0 + 6.0 * (i / 389.0 - 0.5) ** 2
            out.append(SimpleNamespace(timestamp=start + timedelta(minutes=i), open=o, high=max(o, px) * 1.0004, low=min(o, px) * 0.9996, close=px,
                                       volume=float(int(20_000 * u * (0.7 + 0.6 * rng.random())))))
        return out

    def completed_today(self, now: float) -> int:
        return bisect.bisect_right(self.today_t, now - 60)     # bars whose minute has ended

    def minutes(self, now: float) -> list:
        """What a 5-calendar-day 1-minute request returns at `now`: completed bars only."""
        return self.prior[bisect.bisect_left(self.prior_t, now - 5 * 86400):] + self.today[:self.completed_today(now)]

    def daily_since(self, since: date) -> list:
        return self.daily[bisect.bisect_left(self.daily_dates, since):]


class FakeMarketDataClient:
    """Stands in for alpaca-py's StockHistoricalDataClient on the calls the evaluator path makes (get_stock_snapshot; get_stock_bars for 1-minute
    and daily bars), for ANY symbol, from seeded synthetic series in alpaca-py's shapes (attributes only). Every call is counted by kind (daily
    bars by requested lookback) with its estimated response pages, and sleeps the latency per page. Answers depend on the simulated clock only."""

    PAGE_BARS = 10_000                                          # alpaca-py pages multi-symbol bar responses at 10,000 bars

    def __init__(self, clock: SimClock, latency_s: float = 0.0):
        self.clock, self.latency_s = clock, float(latency_s)
        self.calls: Counter = Counter()
        self.pages: Counter = Counter()
        self.symbols: Counter = Counter()
        self._series: Dict[str, _Series] = {}

    def series(self, s: str) -> _Series:
        if s not in self._series:
            self._series[s] = _Series(s)
        return self._series[s]

    def prepare(self, symbols: Sequence[str]) -> None:
        """Generate the synthetic history of `symbols` plus every benchmark and peer the providers may ask for, before any cycle is timed."""
        import config
        from data.sector_map import SECTOR_MAP
        extra = [config.MARKET_PROXY_SYMBOL] + [i["etf"] for i in SECTOR_MAP.values()] + [p for i in SECTOR_MAP.values() for p in i["peers"]]
        for s in dict.fromkeys(list(symbols) + extra):
            self.series(s)

    @staticmethod
    def _list(x: Any) -> List[str]:
        return [x] if isinstance(x, str) else list(x)

    def _count(self, kind: str, symbols: int, bars: int) -> None:
        pages = max(1, math.ceil(bars / self.PAGE_BARS))
        self.calls[kind] += 1
        self.pages[kind] += pages
        self.symbols[kind] += symbols
        if self.latency_s:
            time.sleep(self.latency_s * pages)

    def get_stock_snapshot(self, req: Any) -> Dict[str, Any]:
        syms = self._list(req.symbol_or_symbols)
        self._count("snapshot", len(syms), 0)
        now = self.clock.time()
        t = datetime.fromtimestamp(now - 1, tz=UTC)
        out = {}
        for s in syms:
            x = self.series(s)
            k = x.completed_today(now)
            last, prev = (x.today[k - 1] if k else x.prior[-1]), x.daily[-1]
            day = (SimpleNamespace(open=x.today[0].open, high=x.cum_high[k - 1], low=x.cum_low[k - 1], close=last.close, volume=x.cum_volume[k - 1])
                   if k else None)
            out[s] = SimpleNamespace(symbol=s, latest_trade=SimpleNamespace(price=last.close, timestamp=t),
                                     latest_quote=SimpleNamespace(bid_price=last.close * 0.9998, ask_price=last.close * 1.0002, timestamp=t),
                                     minute_bar=last, daily_bar=day,
                                     previous_daily_bar=SimpleNamespace(open=prev.open, high=prev.high, low=prev.low, close=prev.close, volume=prev.volume))
        return out

    def get_stock_bars(self, req: Any) -> Any:
        syms = self._list(req.symbol_or_symbols)
        now = self.clock.time()
        if "Min" in str(req.timeframe):
            kind, data = "minute_bars", {s: self.series(s).minutes(now) for s in syms}
        else:
            days = round((req.end - req.start).total_seconds() / 86400) if req.start is not None and req.end is not None else 0
            since = SIM_DAY - timedelta(days=days)                 # the requested lookback, counted back from the simulated day
            kind, data = f"daily_bars_{days}d", {s: self.series(s).daily_since(since) for s in syms}
        self._count(kind, len(syms), sum(len(v) for v in data.values()))
        return SimpleNamespace(data=data)


# ================================================================ guards (each one restores what it replaced) ===============================
class NetworkBlocked(RuntimeError):
    pass


_MISSING = object()


@contextlib.contextmanager
def _patched(pairs: Sequence[tuple]) -> Iterator[None]:
    """setattr(obj, name, value) for each (obj, name, value); afterwards every attribute is exactly as before (removed if it was inherited)."""
    saved = [(obj, name, vars(obj).get(name, _MISSING)) for obj, name, _ in pairs]
    try:
        for obj, name, value in pairs:
            setattr(obj, name, value)
        yield
    finally:
        for obj, name, old in reversed(saved):
            if old is _MISSING:
                with contextlib.suppress(AttributeError):
                    delattr(obj, name)
            else:
                setattr(obj, name, old)


@contextlib.contextmanager
def network_blocked(attempts: List[str]) -> Iterator[List[str]]:
    """Socket creation, DNS lookups and connects raise NetworkBlocked; every attempt is appended to `attempts`. socket.has_ipv6 reads False
    meanwhile, so import-time IPv6 capability probes (urllib3's HAS_IPV6) skip their loopback test socket instead of tripping the guard."""
    def blocked(what: str) -> Callable[..., Any]:
        def _refuse(*a: Any, **_k: Any) -> Any:
            detail = next((repr(x)[:80] for x in a if isinstance(x, (str, tuple))), "")
            attempts.append(f"{what}({detail})")
            raise NetworkBlocked(f"network access blocked by tools.evaluator_capacity: {what}")
        return _refuse
    pairs = [(socket, n, blocked(n)) for n in ("getaddrinfo", "gethostbyname", "gethostbyname_ex", "gethostbyaddr", "create_connection",
                                                "create_server", "socketpair")]
    pairs += [(socket.socket, n, blocked(f"socket.{n}")) for n in ("__init__", "connect", "connect_ex")]
    pairs.append((socket, "has_ipv6", False))
    with _patched(pairs):
        yield attempts


class _ImportGate:
    """A meta-path finder refusing the named modules (and their sub-modules) while installed."""

    def __init__(self, names: Sequence[str], hits: List[str]):
        self.names, self.hits = tuple(names), hits

    def find_spec(self, fullname: str, path: Any = None, target: Any = None) -> None:
        if any(fullname == n or fullname.startswith(n + ".") for n in self.names):
            self.hits.append(fullname)
            raise ImportError(f"{fullname} must not be imported by tools.evaluator_capacity")
        return None


@contextlib.contextmanager
def imports_blocked(names: Sequence[str], hits: List[str]) -> Iterator[None]:
    gate = _ImportGate(names, hits)
    sys.meta_path.insert(0, gate)
    try:
        yield
    finally:
        with contextlib.suppress(ValueError):
            sys.meta_path.remove(gate)


@contextlib.contextmanager
def temporary_database(path: Path) -> Iterator[Any]:
    """The process-wide database singleton points at `path` (created and migrated here) and is restored afterwards."""
    import database.database as dbm
    saved = dbm._db_instance
    dbm._db_instance = dbm.ResearchDatabase(Path(path))
    try:
        yield dbm._db_instance
    finally:
        dbm._db_instance = saved


@contextlib.contextmanager
def wires_tripped(hits: List[str]) -> Iterator[None]:
    """The real network wires become tripwires: requesting one records the hit and raises."""
    import evidence.net  # noqa: F401 - loaded here so its wire is tripped
    import execution.broker  # noqa: F401
    import paper.alpaca_readonly  # noqa: F401
    modules = [sys.modules[m] for m in WIRE_MODULES] + [sys.modules[m] for m in LOADED_WIRE_MODULES if m in sys.modules]

    def trip(name: str) -> Callable[..., Any]:
        def _wire(*_a: Any, **_k: Any) -> Any:
            hits.append(name)
            raise RuntimeError(f"{name}.WIRE was requested while measuring (tools.evaluator_capacity)")
        return _wire
    with _patched([(m, "WIRE", trip(m.__name__)) for m in modules if hasattr(m, "WIRE")]):
        yield


@contextlib.contextmanager
def sqlite_counted(counter: Counter) -> Iterator[None]:
    real = sqlite3.connect

    def counting(*a: Any, **k: Any) -> Any:
        counter["connect"] += 1
        return real(*a, **k)
    with _patched([(sqlite3, "connect", counting)]):
        yield


class _WarningCounter(logging.Handler):
    def __init__(self, counter: Counter, samples: List[str]):
        super().__init__(level=logging.WARNING)
        self.counter, self.samples = counter, samples

    def emit(self, record: logging.LogRecord) -> None:
        self.counter[record.name] += 1
        if len(self.samples) < 10:
            with contextlib.suppress(Exception):
                self.samples.append(f"{record.levelname} {record.name}: {record.getMessage()}"[:200])


@contextlib.contextmanager
def warnings_counted(counter: Counter, samples: List[str]) -> Iterator[None]:
    """WARNING+ log records during the run are counted (a healthy run logs none) instead of printed."""
    handler = _WarningCounter(counter, samples)
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        yield
    finally:
        root.removeHandler(handler)


# ================================================================ the measurement ===========================================================
class _Timers:
    def __init__(self) -> None:
        self.s: Dict[str, float] = defaultdict(float)

    def wrap(self, obj: Any, name: str, label: str) -> None:
        fn = getattr(obj, name)

        def timed(*a: Any, **k: Any) -> Any:
            t0 = time.perf_counter()
            try:
                return fn(*a, **k)
            finally:
                self.s[label] += time.perf_counter() - t0
        setattr(obj, name, timed)


def _median(xs: List[float]) -> Optional[float]:
    return statistics.median(xs) if xs else None


def measure(n_symbols: int, *, latency_ms: float = 0.0, cycles: int = 3, work_dir: Any, interval_s: float = INTERVAL_S) -> Dict[str, Any]:
    """One offline measurement: N symbols, `cycles` scheduled cycles (the first is cold), `latency_ms` per fake request page. Writes only a
    temporary database in a new folder under `work_dir`. {summary, cycles, guards, ok}; ok is False when the run reached a guard."""
    if cycles < 1:
        raise ValueError("cycles must be >= 1")
    symbols = universe(int(n_symbols))
    n = len(symbols)
    run_dir = Path(tempfile.mkdtemp(prefix=f"n{n}_lat{int(latency_ms)}ms_", dir=str(work_dir)))
    db_file = run_dir / "capacity.db"
    attempts: List[str] = []
    wire_hits: List[str] = []
    import_hits: List[str] = []
    sql: Counter = Counter()
    warns: Counter = Counter()
    warn_samples: List[str] = []
    api_before = {m for m in sys.modules if m == "api.server" or m.startswith("api.routes")}
    rows: List[Dict[str, Any]] = []
    per_symbol: Dict[str, List[float]] = defaultdict(list)
    with (network_blocked(attempts), imports_blocked(BLOCKED_IMPORTS, import_hits), temporary_database(db_file), sqlite_counted(sql),
          warnings_counted(warns, warn_samples)):
        import config
        from setups.service import SetupService
        from setups.store import SetupStore
        from trader_view.adapters import NullPositionAdapter
        from trader_view.market_real import RealMarketData
        from trader_view.research import EvidenceResearchAdapter
        from trader_view.stream import TerminalStream
        with wires_tripped(wire_hits):
            clock = SimClock(SIM_START, interval_s)
            client = FakeMarketDataClient(clock, latency_ms / 1000.0)
            client.prepare(symbols)
            market = RealMarketData(client_factory=lambda: client, feed="iex", now_fn=clock.time)   # quotes 3 s, minute bars 60 s: the live caches
            market._session_hint = clock.session_hint            # the hint's clock (it reads the wall clock and has no seam)
            holder: Dict[str, Any] = {}
            svc = SetupService(stream_fn=lambda: holder["stream"], store=SetupStore(db_file), clock=clock.dt)
            st = TerminalStream(market, NullPositionAdapter(), EvidenceResearchAdapter(), watchlist_fn=lambda: list(symbols), run_loop=False,
                                now_fn=clock.time, setup_service=svc)
            holder["stream"] = st
            st.proposals.clock = clock.dt                          # the services' own clock seams, bound to the simulated clock
            st.safety.clock = clock.dt
            timers = _Timers()
            for obj, name, label in ((st.ppe, "result", "ppe"), (st.demand, "result", "demand"), (st.research, "latest", "evidence_read"),
                                     (st.proposals, "evaluate", "proposal_incl_safety"), (st.safety, "evaluate", "safety"),
                                     (svc.store, "record", "setup_record")):
                timers.wrap(obj, name, label)
            evaluate_symbol = svc.evaluate_symbol

            def timed_symbol(s: str, *a: Any, **k: Any) -> Any:
                t0 = time.perf_counter()
                try:
                    return evaluate_symbol(s, *a, **k)
                finally:
                    per_symbol[s].append(time.perf_counter() - t0)
            svc.evaluate_symbol = timed_symbol
            for c in range(cycles):
                start = clock.begin()
                calls0, pages0, sql0, comp0 = Counter(client.calls), Counter(client.pages), sql["connect"], dict(timers.s)
                t0 = time.perf_counter()
                out = svc.evaluate_all(clock.dt(), "SCHEDULED")
                wall = time.perf_counter() - t0
                req = client.calls - calls0
                rows.append({"cycle": c, "sim_start": datetime.fromtimestamp(start, tz=UTC).isoformat(timespec="seconds"), "wall_s": round(wall, 3),
                             "per_symbol_ms": round(1000.0 * wall / n, 1), "evaluated": len(out["results"]), "errors": len(out["errors"]),
                             "requests": dict(sorted(req.items())), "requests_total": sum(req.values()),
                             "http_pages_estimate": sum((client.pages - pages0).values()), "sqlite_connects": sql["connect"] - sql0,
                             "components_s": {k: round(v - comp0.get(k, 0.0), 3) for k, v in sorted(timers.s.items())},
                             "decisions": dict(Counter(v.split(":")[0] for v in out["results"].values()))})
        burst_kinds = (f"daily_bars_{int(config.PPE_BARS_LOOKBACK_DAYS)}d", f"daily_bars_{int(config.DEMAND_DAILY_LOOKBACK_DAYS)}d")
    api_new = sorted({m for m in sys.modules if m == "api.server" or m.startswith("api.routes")} - api_before)
    guards = {"network_attempts": attempts, "wire_hits": wire_hits, "blocked_imports": import_hits, "api_modules_imported": api_new,
              "api_server_imported": "api.server" in api_new, "database": "temporary (removed with the work directory unless kept)",
              "market_data_client": "FakeMarketDataClient (no Alpaca client built)"}
    ok = not attempts and not wire_hits and not import_hits and "api.server" not in api_new
    return {"summary": _summary(n, latency_ms, interval_s, rows, burst_kinds, per_symbol, warns, warn_samples), "cycles": rows,
            "guards": guards, "ok": ok, "db_file": str(db_file)}


def _summary(n: int, latency_ms: float, interval_s: float, rows: List[Dict[str, Any]], burst_kinds: tuple, per_symbol: Dict[str, List[float]],
             warns: Counter, warn_samples: List[str]) -> Dict[str, Any]:
    cold, warm = rows[0], rows[1:]
    wall = _median([r["wall_s"] for r in warm])
    req = _median([r["requests_total"] for r in warm])
    kinds = sorted({k for r in warm for k in r["requests"]})
    period = max(interval_s, wall + MIN_GAP_S) if wall is not None else None
    burst = sum(cold["requests"].get(k, 0) for k in burst_kinds)
    rpm = round(req * 60.0 / period, 1) if req is not None else None
    busy = sum(r["wall_s"] for r in warm)
    out = {"n": n, "latency_ms": latency_ms, "cycles": len(rows),
           "cold": {"wall_s": cold["wall_s"], "per_symbol_ms": cold["per_symbol_ms"], "requests_total": cold["requests_total"],
                    "requests": cold["requests"], "http_pages_estimate": cold["http_pages_estimate"]},
           "warm": None, "period_s": round(period, 2) if period is not None else None,
           "back_to_back": (wall + MIN_GAP_S > interval_s) if wall is not None else None,
           "evaluations_per_symbol_per_min": round(60.0 / period, 3) if period else None,
           "requests_per_min_warm": rpm,
           "burst": {"kinds": list(burst_kinds), "requests": burst, "every_s": BURST_EVERY_S, "per_min_avg": round(burst * 60.0 / BURST_EVERY_S, 1)},
           "requests_per_min_avg_with_burst": round(rpm + burst * 60.0 / BURST_EVERY_S, 1) if rpm is not None else None,
           "requests_per_min_cold_cycle": round(cold["requests_total"] * 60.0 / max(interval_s, cold["wall_s"] + MIN_GAP_S), 1),
           "errors_total": sum(r["errors"] for r in rows), "decisions": dict(sum((Counter(r["decisions"]) for r in rows), Counter())),
           "log_warnings": {"total": sum(warns.values()), "by_logger": dict(warns), "samples": warn_samples},
           "healthy": not any(r["errors"] for r in rows) and not warns,          # every evaluation ran and nothing logged a warning
           "slowest_symbols_ms": [[round(1000.0 * statistics.median(v), 1), s] for s, v in
                                  sorted(per_symbol.items(), key=lambda kv: -statistics.median(kv[1]))[:5]]}
    if warm:
        out["warm"] = {"cycles": len(warm), "wall_s_median": round(wall, 3), "wall_s_min": min(r["wall_s"] for r in warm),
                       "wall_s_max": max(r["wall_s"] for r in warm), "per_symbol_ms_median": round(1000.0 * wall / n, 1),
                       "requests_total_median": req, "requests_by_kind_mean": {k: round(statistics.mean(r["requests"].get(k, 0) for r in warm), 2) for k in kinds},
                       "requests_per_symbol": round(statistics.mean(r["requests_total"] for r in warm) / n, 2),
                       "sqlite_connects_per_symbol": round(statistics.mean(r["sqlite_connects"] for r in warm) / n, 1),
                       "component_share": {k: round(sum(r["components_s"].get(k, 0.0) for r in warm) / max(1e-9, busy), 3)
                                           for k in sorted({k for r in warm for k in r["components_s"]})}}
    return out


# ================================================================ command line ==============================================================
def prepare_process() -> Dict[str, Any]:
    """Before config is imported (command line only): the test environment, every credential blank, the background services off, no bytecode."""
    if "config" in sys.modules:
        raise RuntimeError("config is already imported: run the tool as its own process (python -B -m tools.evaluator_capacity)")
    sys.dont_write_bytecode = True
    names = set(CREDENTIAL_ENV) | {k for k in os.environ if any(m in k.upper() for m in CREDENTIAL_MARKERS)}
    for k in names:
        os.environ[k] = ""                                     # set and empty: load_dotenv never overrides an existing variable
    os.environ.update(SERVICES_OFF)
    return {"credentials_blanked": sorted(names), "environment": dict(SERVICES_OFF)}


def _ints(text: str) -> List[int]:
    return [int(x) for x in text.split(",") if x.strip()]


def _floats(text: str) -> List[float]:
    return [float(x) for x in text.split(",") if x.strip()]


def _fmt(v: Any, spec: str = "g") -> str:
    return "-" if v is None else format(v, spec)


def render_markdown(report: Dict[str, Any]) -> str:
    lines = [f"# Setup evaluator capacity (offline, {report['generated_at']})", "",
             "| N | latency ms | cold cycle s | warm cycle s (median) | ms/symbol (warm) | requests/cycle warm (by kind) | period s | requests/min warm"
             " | + burst avg/min | cold-cycle requests |", "|---|---|---|---|---|---|---|---|---|---|"]
    for run in report["runs"]:
        s, w = run["summary"], run["summary"]["warm"] or {}
        kinds = ", ".join(f"{k} {v:g}" for k, v in (w.get("requests_by_kind_mean") or {}).items())
        lines.append(f"| {s['n']} | {s['latency_ms']:g} | {s['cold']['wall_s']:.2f} | {_fmt(w.get('wall_s_median'), '.2f')} | "
                     f"{_fmt(w.get('per_symbol_ms_median'), '.1f')} | {_fmt(w.get('requests_total_median'))} ({kinds}) | {_fmt(s['period_s'])} | "
                     f"{_fmt(s['requests_per_min_warm'])} | {_fmt(s['requests_per_min_avg_with_burst'])} | {s['cold']['requests_total']} |")
    g = report["guards"]
    lines += ["", f"Guards: network attempts {len(g['network_attempts'])}, wire hits {len(g['wire_hits'])}, blocked imports {len(g['blocked_imports'])}, "
                  f"api.server imported {g['api_server_imported']}, all runs ok {g['all_runs_ok']}, all runs healthy {g['all_runs_healthy']}.",
              f"Live reference ({LIVE_REFERENCE['date']}, persisted): 10-symbol evaluator cycle span median {LIVE_REFERENCE['evaluator_cycle_span_s']['median']} s, "
              f"p95 {LIVE_REFERENCE['evaluator_cycle_span_s']['p95']} s, max {LIVE_REFERENCE['evaluator_cycle_span_s']['max']} s; executor cycles median "
              f"{LIVE_REFERENCE['executor_cycle_ms']['median']} ms."]
    return "\n".join(lines) + "\n"


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="python -B -m tools.evaluator_capacity", description=__doc__.strip().splitlines()[0])
    ap.add_argument("--symbols", default="10,50,100,200", help="universe sizes, comma separated")
    ap.add_argument("--latency-ms", default="0,130", help="injected latency per request, comma separated")
    ap.add_argument("--cycles", type=int, default=3, help="scheduled cycles per run (the first is cold)")
    ap.add_argument("--interval-s", type=float, default=INTERVAL_S, help="scheduler interval (SETUP_EVAL_INTERVAL_SECONDS)")
    ap.add_argument("--out", required=True, help="JSON report path")
    ap.add_argument("--markdown", default=None, help="optional Markdown summary path")
    ap.add_argument("--work-dir", default=None, help="folder for the temporary databases (default: a new temporary folder, removed)")
    ap.add_argument("--keep-work-dir", action="store_true", help="keep the default temporary folder")
    ap.add_argument("--no-warmup", action="store_true", help="skip the discarded 2-symbol run that absorbs one-time import / first-call costs")
    args = ap.parse_args(argv)
    sizes, latencies = _ints(args.symbols), _floats(args.latency_ms)
    env = prepare_process()
    own_work = args.work_dir is None
    work = Path(tempfile.mkdtemp(prefix="evaluator-capacity-")) if own_work else Path(args.work_dir)
    work.mkdir(parents=True, exist_ok=True)
    attempts: List[str] = []
    report: Dict[str, Any] = {"tool": TOOL_VERSION, "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
                              "python": sys.version.split()[0], "parameters": {"symbols": sizes, "latency_ms": latencies, "cycles": args.cycles,
                                                                               "interval_s": args.interval_s, "sim_start": SIM_START.isoformat()},
                              "process": env, "runs": [], "live_reference": LIVE_REFERENCE}
    try:
        with network_blocked(attempts):
            import config
            if config.has_credentials():
                raise RuntimeError("Alpaca credentials are visible to config: refusing to measure")
            if not args.no_warmup:                             # discarded: keeps one-time costs out of the first measured cold cycle
                warmup_run = measure(2, latency_ms=0.0, cycles=1, work_dir=work, interval_s=args.interval_s)
                report["warmup"] = {"n": 2, "ok": warmup_run["ok"], "guards": warmup_run["guards"]}
            for lat in latencies:
                for n in sizes:
                    run = measure(n, latency_ms=lat, cycles=args.cycles, work_dir=work, interval_s=args.interval_s)
                    run.pop("db_file", None)
                    report["runs"].append(run)
                    s, w = run["summary"], run["summary"]["warm"] or {}
                    print(f"N={n:>3} latency={lat:g} ms: cold {s['cold']['wall_s']:.2f} s, warm {w.get('wall_s_median')} s, "
                          f"{w.get('requests_total_median')} requests/cycle, {s['requests_per_min_warm']} requests/min, ok={run['ok']}, "
                          f"healthy={s['healthy']}", flush=True)
    finally:
        if own_work and not args.keep_work_dir:
            shutil.rmtree(work, ignore_errors=True)
    report["model"] = [request_budget(n, latency_ms=lat) for lat in latencies for n in sizes]
    guard_lists = [r["guards"] for r in report["runs"]] + ([report["warmup"]["guards"]] if "warmup" in report else [])
    report["guards"] = {"network_attempts": attempts + [a for g in guard_lists for a in g["network_attempts"]],
                        "wire_hits": [h for g in guard_lists for h in g["wire_hits"]],
                        "blocked_imports": [h for g in guard_lists for h in g["blocked_imports"]],
                        "api_server_imported": "api.server" in sys.modules,
                        "all_runs_ok": all(r["ok"] for r in report["runs"]) and report.get("warmup", {}).get("ok", True),
                        "all_runs_healthy": all(r["summary"]["healthy"] for r in report["runs"]),
                        "work_dir": "removed" if own_work and not args.keep_work_dir else str(work)}
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=1), encoding="utf-8")
    if args.markdown:
        Path(args.markdown).write_text(render_markdown(report), encoding="utf-8")
    print(f"wrote {out}" + (f" and {args.markdown}" if args.markdown else ""))
    ok = report["guards"]["all_runs_ok"] and not report["guards"]["network_attempts"] and not report["guards"]["api_server_imported"]
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
