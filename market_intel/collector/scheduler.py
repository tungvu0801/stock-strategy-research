"""
market_intel/collector/scheduler.py - Stage 7.3: deterministic scheduling on an injected clock (DESIGN_73 section 10). No thread.

A Scheduler is a plain object a caller drives: nothing here starts a thread, a timer, a job or a loop, and nothing constructs one.

* due(now): the (plan, scope) pairs whose cadence window is open at `now` and whose last run (by any worker sharing the state file)
  started at least one interval ago; a daily job is due once per New York day, from its time. A refused attempt is not a run.
* tick(): runs the due pairs SEQUENTIALLY in a fixed order (source, then scope), each under its lease. A repeated tick in the same
  interval runs nothing. A pair's exception is its result (the next pair still runs); a KeyboardInterrupt stops the tick.
* a new New York day starts a new collection cycle (an ACCESS_DENIED provider may be tried again).
* request_stop(): the current run ends at its next page boundary (SHUTDOWN, PARTIAL, watermark unchanged); no further pair starts.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable, List, Optional, Tuple
from zoneinfo import ZoneInfo

from market_intel import ingest as IG
from market_intel.collector import plans as PL
from market_intel.store import parse_ts

NY = ZoneInfo("America/New_York")


def _ny(t: datetime) -> datetime:
    return t.astimezone(NY)


class Scheduler:
    def __init__(self, collector, pairs: Iterable[Tuple[PL.CollectionPlan, PL.Scope]]):
        pairs = list(pairs)
        keys = [(p.source, s.key()) for p, s in pairs]
        if len(set(keys)) != len(keys):
            raise ValueError("a (source, scope) pair is scheduled once")
        self.collector = collector
        self.pairs = sorted(pairs, key=lambda x: (x[0].source, x[1].key()))
        self._stop = False
        self._day = None

    def request_stop(self) -> None:
        self._stop = True
        self.collector.request_stop()

    def due(self, now: Optional[datetime] = None) -> List[Tuple[PL.CollectionPlan, PL.Scope]]:
        now = (now or self.collector.clock()).astimezone(timezone.utc)
        out = []
        for plan, scope in self.pairs:
            cad = PL.active_cadence(plan, now)
            if cad is None:
                continue
            last = self.collector.state.last_run(plan.source, scope.key())
            started = None if last is None else parse_ts(last["started_at"])
            if started is not None and started > now:
                continue                                                  # our clock is behind the last run: never run again "earlier"
            if cad.window == PL.DAILY:
                local = _ny(now)
                if local.hour * 60 + local.minute < cad.at_minute or (started is not None and _ny(started).date() == local.date()):
                    continue
            elif started is not None and (now - started).total_seconds() < cad.interval_s:
                continue
            out.append((plan, scope))
        return out

    def tick(self) -> List[dict]:
        now = self.collector.clock().astimezone(timezone.utc)
        day = _ny(now).date()
        if self._day is not None and day != self._day:
            self.collector.new_cycle()
        self._day = day
        results = []
        for plan, scope in self.due(now):
            if self._stop or self.collector.stopping:
                break
            try:
                results.append(self.collector.collect(plan, scope))
            except IG.FinalizationError as exc:
                results.append({"source": plan.source, "scope": scope.key(), "status": "PENDING_FINALIZATION", "error": str(exc)[:300]})
            except Exception as exc:                                     # noqa: BLE001 - one pair's failure is its result, never the tick's
                results.append({"source": plan.source, "scope": scope.key(), "status": "ERROR", "error": f"{type(exc).__name__}: {str(exc)[:200]}"})
        return results

    def recover(self) -> dict:
        return self.collector.recover()
