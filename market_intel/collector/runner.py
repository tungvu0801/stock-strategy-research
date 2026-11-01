"""
market_intel/collector/runner.py - Stage 7.3: one bounded collection run (DESIGN_73 section 3). Offline: the only transport it accepts
is a FakeTransport.

Collector(...).collect(plan, scope) - refusals first, and nothing is sent, created or claimed when one applies (status REFUSED):
PLAN_NOT_REGISTERED, BLOCKED_PENDING_APPROVAL, SCOPE_INVALID, NOT_CONFIGURED (credential presence only - an injected predicate that
defaults to "nothing configured"; a value is never read), REAL_NETWORK_NOT_APPROVED, LIVE_INGESTION_NOT_APPROVED (any sink but a FIXTURE
dataset), HISTORICAL_WINDOW_NEEDS_APPROVAL, CLOCK_BEHIND_WATERMARK, CIRCUIT_OPEN / CIRCUIT_TRIAL_RUNNING, BUDGET_EXHAUSTED / RATE_LIMITED /
ACCESS_DENIED_THIS_CYCLE, LEASE_HELD / PROVIDER_CONCURRENCY, SHUTDOWN.

Then, in this order: the lease (and a half-open trial claim) in one state transaction; a FIXTURE dataset in the 7.1 store and the run
record; the pages; the seal; the anchor; the run end WITH its watermark (only for a COMPLETED or EMPTY seal) and the breaker transition in
one state transaction; the lease release. Per attempt: the budget is spent and persisted (mi_budget_events), the response archived (the
7.1 RawArchive; a body above the cap is not kept), recorded as a 7.1 fetch row with its error class and provenance, parsed by the 7.2
adapter (never raises), its row problems recorded as REJECTED members and its rows ingested in quarantine mode.

What a run never does: advance a watermark before every page was fetched, validated and ingested; rewrite a provider timestamp or widen the
store's 2 s chronology tolerance (a row from the future is REJECTED and the run is PARTIAL - decision A); accept a Stage 7.1 bar
BackfillApproval (no parameter takes one - decision C); read a credential's value.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Callable, Dict, Optional, Tuple

from market_intel import calendar as CAL
from market_intel import ingest as IG
from market_intel import store as ST
from market_intel import validate as VA
from market_intel.budget import BLOCKED_BACKOFF, RATE_LIMITED, ProviderBudget, default_budgets
from market_intel.collector import health as HE
from market_intel.collector import plans as PL
from market_intel.collector import policy as PO
from market_intel.collector import transport as TR
from market_intel.collector.state import COMPLETE, CollectorState
from market_intel.models import FetchRecord
from market_intel.sources import alpaca_news, alpaca_snapshots, earnings_av, fred, halts
from market_intel.sources.common import PAGE_PROBLEMS, ny_date

COLLECTOR_VERSION = "market_intel_collector_v1"
SINK_JOB_KIND = "FIXTURE"                     # live collection needs a schema v2 job kind (DESIGN_73 section 5)
LEASE_TTL_S = PO.TRIAL_STALE_S                # 1800 s: longer than any bounded run
MAX_RUN_S = 900.0                             # a run stops at the next page boundary after this (RUN_TIME_LIMIT)
MAX_BUDGET_WAIT_S = 60.0                      # the longest a run waits for its own budget before it stops
FRED_PAGE_ROWS = 10_000
CHRONOLOGY_REASONS = ("OBSERVED_AFTER_RETRIEVAL", "PUBLISHED_AFTER_RETRIEVAL", "BAR_NOT_COMPLETE_AT_RETRIEVAL")
STOP_CODES = ("SHUTDOWN", "RUN_TIME_LIMIT", "PAGE_LIMIT", "ROW_LIMIT", "PAGINATION_LOOP", "DUPLICATE_PAGE", "ACCESS_DENIED", "INVALID_RESPONSE",
              "TOO_LARGE", "HTTP_ERROR", "RETRIES_EXHAUSTED", "ARCHIVE_ERROR")       # also BUDGET_<reason>, ERROR:<type>, INTERRUPTED:<type>
COUNTS = ("pages", "requests", "ok_fetches", "failed_fetches", "unrecovered", "inserted", "duplicates", "revised", "absences", "rejected", "excluded",
          "problems", "rows_delivered")


def plan_sha256(plan: PL.CollectionPlan) -> str:
    return hashlib.sha256(json.dumps(asdict(plan), sort_keys=True, default=str).encode("utf-8")).hexdigest()


def nothing_configured(name: str) -> bool:
    return False


def _utc(d: datetime) -> datetime:
    if d.tzinfo is None or d.tzinfo.utcoffset(d) is None:
        raise ValueError("times must be timezone-aware")
    return d.astimezone(timezone.utc)


def server_date(headers) -> Optional[datetime]:
    raw = next((v for k, v in headers.items() if str(k).lower() == "date"), None) if hasattr(headers, "items") else None
    if raw is None:
        return None
    try:
        d = parsedate_to_datetime(str(raw)[:64])
    except (TypeError, ValueError, IndexError):
        return None
    return d.astimezone(timezone.utc) if d is not None and d.tzinfo is not None else None


@dataclass
class _Run:
    plan: PL.CollectionPlan
    scope: PL.Scope
    run_id: str
    dataset_id: str
    started: datetime
    window: Optional[Tuple[datetime, datetime]]
    budget: ProviderBudget
    configured: bool
    stats: Dict[str, int] = field(default_factory=lambda: {k: 0 for k in COUNTS})
    clock_skew: bool = False
    last_error: str = ""


class Collector:
    def __init__(self, *, store: ST.MarketIntelStore, archive, state: CollectorState, transport, clock: Callable[[], datetime],
                 sleep: Callable[[float], None], credentials: Callable[[str], bool] = nothing_configured,
                 budgets: Optional[Dict[str, ProviderBudget]] = None, worker: str = "collector-1"):
        self.store, self.archive, self.state, self.transport = store, archive, state, transport
        self.clock, self.sleep, self.credentials, self.worker = clock, sleep, credentials, str(worker)[:60]
        self.budgets = budgets if budgets is not None else default_budgets(clock)
        self._stop = False

    # ------------------------------------------------------------------------------------------------ control
    def request_stop(self) -> None:
        """Stop at the next page boundary (or before the next retry): the run is sealed PARTIAL, its watermark unchanged."""
        self._stop = True

    @property
    def stopping(self) -> bool:
        return self._stop

    def new_cycle(self) -> None:
        """A new collection cycle (the scheduler: each New York day): an ACCESS_DENIED provider may be tried again."""
        for b in self.budgets.values():
            b.new_cycle()

    def _now(self) -> datetime:
        return _utc(self.clock())

    # ------------------------------------------------------------------------------------------------ budgets
    def _sync(self, b: ProviderBudget, now: datetime) -> None:
        """A daily cap's count, re-read from the store's persisted ALLOW events before every run: a restart, or another collector sharing the
        store, never resets it or races past it (the provider lease serializes the runs that spend it)."""
        if b.daily_cap is None:
            return
        b.status()                                                       # rolls the budget's New York day
        b.day_count = max(b.day_count, self.store.budget_used(b.name, CAL.session(now)["session_date"]))

    def budget_refusal(self, b: ProviderBudget, now: datetime) -> Optional[str]:
        """Why a run of this budget may not start now (None: it may). Spends nothing."""
        self._sync(b, now)
        if b.bucket.per_minute <= 0 or b.daily_cap == 0:
            return "BUDGET_EXHAUSTED"
        if b.blocked_cycle:
            return "ACCESS_DENIED_THIS_CYCLE"
        if b.blocked_until is not None and now < b.blocked_until:
            return "RATE_LIMITED"
        if b.daily_cap is not None and b.day_count + 1 > b.daily_cap:
            return "BUDGET_EXHAUSTED"
        if b.bucket.wait_s(1) != 0:
            return "RATE_LIMITED"
        return None

    def _spend(self, r: _Run) -> Optional[str]:
        """One request's budget, persisted; waits (at most MAX_BUDGET_WAIT_S in all) for a token or a back-off to pass. Returns the refusal."""
        waited = 0.0
        while True:
            ok, why = r.budget.allow()
            self.store.record_budget_event(r.budget.name, "ALLOW" if ok else "REFUSE", reason=why, per_minute=r.budget.bucket.per_minute,
                                           used_today=r.budget.day_count, dataset_id=r.dataset_id)
            if ok:
                return None
            now = self._now()
            wait = r.budget.bucket.wait_s() if why == RATE_LIMITED else (
                (r.budget.blocked_until - now).total_seconds() if why == BLOCKED_BACKOFF and r.budget.blocked_until else None)
            if wait is None or waited + wait > MAX_BUDGET_WAIT_S or self._stop:
                return why
            self.sleep(wait)
            waited += wait

    # ------------------------------------------------------------------------------------------------ windows, requests, parsing
    def window(self, plan: PL.CollectionPlan, scope: PL.Scope, now: datetime, start: Optional[datetime]):
        """(window or None, gap or None, refusal or None). A windowed run covers [watermark - overlap, now], never earlier than now - lookback;
        a longer outage leaves a GAP [watermark, now - lookback] that only a historical approval could fill (it is recorded, never guessed)."""
        wm = self.state.watermark(plan.source, scope.key())
        if wm is not None and wm > now:
            return None, None, "CLOCK_BEHIND_WATERMARK"                 # our clock is behind what we already covered: never run
        if not plan.windowed:
            return None, None, ("SCOPE_INVALID" if start is not None else None)
        floor = now - timedelta(seconds=plan.lookback_max_s)
        if start is not None:
            start = _utc(start)
            if start < floor:
                return None, None, "HISTORICAL_WINDOW_NEEDS_APPROVAL"
            return ((start, now), None, None) if start < now else (None, None, "SCOPE_INVALID")
        if wm is None:
            return (floor, now), None, None
        lo = wm - timedelta(seconds=plan.overlap_s)
        return ((floor, now), (wm, floor), None) if lo < floor else ((lo, now), None, None)

    @staticmethod
    def build_request(r: _Run, token: Optional[str]) -> dict:
        s, sc = r.plan.source, r.scope
        if s == "alpaca_news":
            return alpaca_news.request(sc.symbols, r.window[0], r.window[1], page_token=token)
        if s == "alpaca_snapshots":
            return alpaca_snapshots.request(sc.symbols, sc.feed)
        if s == "halts":
            return halts.request()
        if s == "fred":
            today = ny_date(r.started)
            return fred.request(sc.series, observation_start=today - timedelta(days=r.plan.observation_days), observation_end=today,
                                offset=int(token or 0), limit=FRED_PAGE_ROWS, configured=r.configured)
        if s == "earnings_av":
            return earnings_av.request(horizon="3month", configured=r.configured)
        raise PermissionError(f"{s}: no request is built for this plan")

    @staticmethod
    def parse(r: _Run, body: bytes, retrieved: datetime, token: Optional[str]):
        s, sc = r.plan.source, r.scope
        if s == "alpaca_news":
            return alpaca_news.parse_page(body, symbols=sc.symbols, retrieved_at=retrieved, start=r.window[0], end=r.window[1], dataset_id=r.dataset_id)
        if s == "alpaca_snapshots":
            return alpaca_snapshots.parse(body, symbols=sc.symbols, feed=sc.feed, retrieved_at=retrieved, dataset_id=r.dataset_id)
        if s == "halts":
            return halts.parse(body, retrieved_at=retrieved, dataset_id=r.dataset_id)
        if s == "fred":
            return fred.parse(body, series_id=sc.series, retrieved_at=retrieved, dataset_id=r.dataset_id, expected_offset=int(token or 0))
        return earnings_av.parse(body, retrieved_at=retrieved, symbols=sc.symbols or None, dataset_id=r.dataset_id)

    @staticmethod
    def dataset_spec(plan: PL.CollectionPlan, scope: PL.Scope, run_id: str, window) -> ST.DatasetSpec:
        symbols = sorted(scope.symbols) if plan.source in ("alpaca_snapshots", "earnings_av") else []
        return ST.DatasetSpec(job_kind=SINK_JOB_KIND, kind=plan.kind, provider=plan.provider,
                              feed=scope.feed if plan.source == "alpaca_snapshots" else "none", adjustment="none", symbols=symbols,
                              request={"collector": COLLECTOR_VERSION, "source": plan.source, "scope": scope.key(), "run_id": run_id,
                                       "plan_sha256": plan_sha256(plan), "window": [ST.ts(w) for w in window] if window else None})

    # ------------------------------------------------------------------------------------------------ one run
    def _refuse(self, plan, scope, code: str, detail: str = "") -> dict:
        return {"source": getattr(plan, "source", None), "scope": scope.key() if isinstance(scope, PL.Scope) else None, "status": "REFUSED",
                "refused": code, "detail": detail[:300], "run_id": None, "dataset_id": None, "requests": 0}

    def collect(self, plan: PL.CollectionPlan, scope: PL.Scope, *, window_start: Optional[datetime] = None, sink_job_kind: str = SINK_JOB_KIND) -> dict:
        if not PL.registered(plan):
            return self._refuse(plan, scope, "PLAN_NOT_REGISTERED")
        if plan.blocked_reason:
            return self._refuse(plan, scope, "BLOCKED_PENDING_APPROVAL", plan.blocked_reason)
        bad = PL.scope_problem(plan, scope)
        if bad:
            return self._refuse(plan, scope, "SCOPE_INVALID", bad)
        configured = plan.credential is None or self.credentials(plan.credential) is True
        if not configured:
            return self._refuse(plan, scope, "NOT_CONFIGURED", f"{plan.credential} is not configured")
        if not TR.is_fake(self.transport):
            return self._refuse(plan, scope, TR.REAL_NETWORK_NOT_APPROVED, "Stage 7.3 accepts only the fake transport")
        if sink_job_kind != SINK_JOB_KIND:
            return self._refuse(plan, scope, "LIVE_INGESTION_NOT_APPROVED", "live collection needs a schema v2 job kind (DESIGN_73 section 5)")
        if self._stop:
            return self._refuse(plan, scope, "SHUTDOWN")
        now = self._now()
        window, gap, why = self.window(plan, scope, now, window_start)
        if why:
            return self._refuse(plan, scope, why)
        gate = PO.gate(self.state.breaker(plan.source), now)
        if gate in (PO.CIRCUIT_OPEN, PO.TRIAL_RUNNING):
            return self._refuse(plan, scope, gate)
        budget = self.budgets[plan.budget_name]
        why = self.budget_refusal(budget, now)
        if why:
            return self._refuse(plan, scope, why)
        lease, why, trial = self.state.begin(source=plan.source, scope_key=scope.key(), provider=plan.provider, worker=self.worker, now=now,
                                             ttl_s=LEASE_TTL_S, concurrency=plan.concurrency)
        if lease is None:
            return self._refuse(plan, scope, why)
        why = self.budget_refusal(budget, now) if budget.daily_cap is not None else None
        if why:                                                              # re-read under the provider lease: exact across collectors
            self.state.abandon(lease, plan.source, now, trial=trial)
            return self._refuse(plan, scope, why)
        run_id = uuid.uuid4().hex
        try:
            did = self.store.create_dataset(self.dataset_spec(plan, scope, run_id, window))
            self.state.start_run(run_id=run_id, source=plan.source, scope_key=scope.key(), scope=asdict(scope), worker=self.worker, lease_id=lease,
                                 plan_sha256=plan_sha256(plan), started_at=now, window=window, gap=gap, dataset_id=did, trial=trial)
        except BaseException:
            self.state.abandon(lease, plan.source, now, trial=trial)
            raise
        r = _Run(plan, scope, run_id, did, now, window, budget, configured)
        try:
            return self._finish(r, lease, trial, gap, *self._guarded_pages(r))
        finally:
            self.state.release_lease(lease, self._now())

    def _guarded_pages(self, r: _Run):
        try:
            return self._pages(r), None
        except Exception as exc:                                             # noqa: BLE001 - recorded as the stop reason, never left RUNNING
            r.last_error = f"ERROR:{type(exc).__name__}"
            return r.last_error, None
        except BaseException as exc:                                         # KeyboardInterrupt / SystemExit: sealed first, then re-raised
            return f"INTERRUPTED:{type(exc).__name__}", exc

    @staticmethod
    def _final(stats: dict, stop: Optional[str]) -> str:
        if stats["ok_fetches"] == 0:
            return "FAILED"
        if stop or stats["unrecovered"] or stats["rejected"] or stats["problems"]:
            return "PARTIAL"
        return "COMPLETED" if stats["inserted"] + stats["revised"] + stats["duplicates"] else "EMPTY"

    def _finish(self, r: _Run, lease: str, trial: bool, gap, stop: Optional[str], interrupt: Optional[BaseException]) -> dict:
        final = self._final(r.stats, stop)
        detail = json.dumps({"collector": COLLECTOR_VERSION, "source": r.plan.source, "stop": stop, **r.stats})
        failed = r.stats["unrecovered"] > 0
        tried = r.stats["requests"] > 0                                      # a run that asked nothing proves nothing about the provider
        try:
            try:
                sealed = self.store.finish_dataset(r.dataset_id, final, detail=detail)
            except ST.SealedError:
                raise
            except ST.StoreError:                                            # the store's own rule disagrees: never COMPLETED / EMPTY by default
                final = "PARTIAL" if r.stats["ok_fetches"] else "FAILED"
                sealed = self.store.finish_dataset(r.dataset_id, final, detail=detail)
        except Exception as exc:                                             # noqa: BLE001 - the seal could not be written: say so, never claim it
            self.state.end_run(r.run_id, status="PENDING_FINALIZATION", now=self._now(), stop=stop, failed=failed, clock_skew=r.clock_skew,
                               last_error=f"SEAL:{type(exc).__name__}", content_sha256=None, counts=r.stats)
            raise IG.FinalizationError(r.dataset_id, "PENDING_FINALIZATION", type(exc).__name__) from (interrupt or exc)
        finalization = "ANCHORED"
        try:
            self.archive.anchor(r.dataset_id, sealed["content_sha256"], records_sha256=sealed["records_sha256"], metadata_sha256=sealed["metadata_sha256"],
                                status=final, sealed_at=self._now())
        except Exception:                                                    # noqa: BLE001 - sealed, not yet anchored: recover() anchors it
            finalization = "PENDING_ANCHOR"
        end = self._now()
        watermark = (r.window[1] if r.window else r.started) if final in COMPLETE else None
        current = self.state.watermark(r.plan.source, r.scope.key())
        if watermark is not None and current is not None and watermark < current:
            watermark, r.last_error = None, "WATERMARK_BEHIND_CURRENT"        # another worker's clock ran ahead: never moved back
        self.state.end_run(r.run_id, status=final, now=end, stop=stop, failed=failed, clock_skew=r.clock_skew, last_error=r.last_error or (stop or ""),
                           content_sha256=sealed["content_sha256"], counts=r.stats, watermark=watermark, breaker_failed=failed if tried else None)
        if trial and not tried:
            self.state.hand_back_trial(r.plan.source, end)
        if interrupt is not None:
            raise interrupt
        return {"source": r.plan.source, "scope": r.scope.key(), "status": final, "refused": None, "detail": "", "run_id": r.run_id,
                "dataset_id": r.dataset_id, "stop": stop, "window": [ST.ts(w) for w in r.window] if r.window else None,
                "gap": [ST.ts(g) for g in gap] if gap else None, "watermark": ST.ts(watermark) if watermark else None, "trial": trial,
                "clock_skew": r.clock_skew, "last_error": r.last_error, "finalization": finalization, "content_sha256": sealed["content_sha256"], **r.stats}

    def _pages(self, r: _Run) -> Optional[str]:
        """The page loop. Returns the stop reason (None: the last page said there is no more)."""
        token: Optional[str] = None
        seen = set()
        last_digest = None
        for _ in range(r.plan.max_pages):
            if self._stop:
                return "SHUTDOWN"
            if (self._now() - r.started).total_seconds() > MAX_RUN_S:
                return "RUN_TIME_LIMIT"
            if token is not None:
                seen.add(token)
            outcome = self._request(r, token)
            if isinstance(outcome, str):
                return outcome
            body, obs, nxt, problems, fid = outcome
            r.stats["pages"] += 1
            digest = hashlib.sha256(body).hexdigest()
            if digest == last_digest:
                return "DUPLICATE_PAGE"                                       # the provider ignored the token: recorded, not ingested again
            if r.stats["rows_delivered"] + len(obs) + len(problems) > r.plan.max_rows_per_run:
                return "ROW_LIMIT"                                            # archived and recorded, not ingested
            r.stats["rows_delivered"] += len(obs) + len(problems)
            r.stats["problems"] += self.store.record_problems(r.dataset_id, problems, fetch_id=fid)
            res = self.store.ingest(r.dataset_id, obs, fetch_id=fid, on_reject="quarantine")
            for k in ("inserted", "duplicates", "revised", "absences", "rejected", "excluded"):
                r.stats[k] += getattr(res, k)
            if any(x["reason"] in CHRONOLOGY_REASONS for x in res.rejections):
                r.clock_skew = True
            if not nxt:
                return None
            if nxt in seen:
                return "PAGINATION_LOOP"
            token, last_digest = nxt, digest
        return "PAGE_LIMIT"

    def _record(self, r: _Run, req: dict, *, started, retrieved=None, status: str, error: str = "", http_status=None, body: Optional[bytes] = None,
                sha=None, raw_path=None, retry_after=None, fid=None) -> str:
        return self.store.record_fetch(FetchRecord(provider=r.plan.provider, endpoint=req["endpoint"], started_at=started, retrieved_at=retrieved,
                                                   status=status, http_status=http_status, error=error[:300], bytes=len(body or b""), content_hash=sha,
                                                   retry_after_s=retry_after, latency_ms=None if retrieved is None else
                                                   max(0, int((retrieved - started).total_seconds() * 1000))),
                                       dataset_id=r.dataset_id, params=req["params"], raw_path=raw_path, fetch_id=fid)

    def _request(self, r: _Run, token: Optional[str]):
        """One page, with retries: (body, observations, next token, problems, fetch id) - or the stop reason."""
        req = self.build_request(r, token)
        key = ST.canonical({"endpoint": req["endpoint"], "params": req["params"]})
        attempt = 0
        while True:
            attempt += 1
            why = self._spend(r)
            if why:
                if attempt > 1:
                    r.stats["unrecovered"] += 1                              # a failed attempt the budget no longer lets us retry
                r.last_error = f"BUDGET_{why}"
                return f"BUDGET_{why}"
            r.stats["requests"] += 1
            started = self._now()
            try:
                outcome = self.transport.send(req, timeout_s=TR.REQUEST_TIMEOUT_S, max_bytes=r.plan.max_bytes)
            except Exception as exc:                                         # noqa: BLE001 - a transport failure is a fetch row, not a crash
                outcome = exc
            retrieved = self._now()
            status, error, retry = TR.classify(outcome, max_bytes=r.plan.max_bytes)
            fid = uuid.uuid4().hex
            if isinstance(outcome, BaseException) or not TR.contract_ok(outcome):
                self._record(r, req, started=started, retrieved=None if isinstance(outcome, BaseException) else retrieved, status=status, error=error,
                             fid=fid)
                self.store.record_budget_event(r.budget.name, "RESPONSE", reason=status, dataset_id=r.dataset_id)
            else:
                body = bytes(outcome.body)
                wait_s = IG.retry_after_s(outcome.headers, retrieved)
                r.budget.record(outcome.status, retry_after_s=wait_s, access_denied=status == "ACCESS_DENIED")
                self.store.record_budget_event(r.budget.name, "RESPONSE", http_status=outcome.status, retry_after_s=wait_s, dataset_id=r.dataset_id)
                if VA.clock_skew(server_date(outcome.headers), retrieved):
                    r.clock_skew = True
                if status == "TOO_LARGE":                                    # never kept: storage stays bounded
                    self._record(r, req, started=started, retrieved=retrieved, status=status, error=error, http_status=outcome.status, body=body, fid=fid)
                else:
                    try:
                        entry = self.archive.write(fid, body, provider=r.plan.provider, endpoint=req["endpoint"], retrieved_at=retrieved)
                    except OSError as exc:                                   # no raw evidence: nothing from this response is stored
                        self._record(r, req, started=started, retrieved=retrieved, status="ERROR", error=f"ARCHIVE:{type(exc).__name__}",
                                     http_status=outcome.status, fid=fid)
                        r.stats["failed_fetches"] += 1
                        r.stats["unrecovered"] += 1
                        r.last_error = "ARCHIVE_ERROR"
                        return "ARCHIVE_ERROR"
                    obs, nxt, problems = [], None, []
                    if status == "OK":
                        obs, nxt, problems = self.parse(r, body, retrieved, token)
                        page = next((p["problem"] for p in problems if p["problem"] in PAGE_PROBLEMS or p["problem"].startswith("PARSER_ERROR")), None)
                        if page:
                            status, error = "INVALID_RESPONSE", page
                    self._record(r, req, started=started, retrieved=retrieved, status=status, error=error, http_status=outcome.status, body=body,
                                 sha=entry["sha256"], raw_path=entry["path"], retry_after=wait_s, fid=fid)
                    if status == "OK":
                        r.stats["ok_fetches"] += 1
                        return body, obs, nxt, problems, fid
                    if status == "INVALID_RESPONSE":
                        r.stats["problems"] += self.store.record_problems(r.dataset_id, problems, fetch_id=fid)
            r.stats["failed_fetches"] += 1
            if retry and attempt < PO.MAX_ATTEMPTS and not self._stop:
                wait = PO.backoff_s(attempt, key)
                if status == "RATE_LIMITED" and r.budget.blocked_until is not None:
                    wait = max(wait, (r.budget.blocked_until - self._now()).total_seconds())
                self.sleep(wait)
                continue                                                     # the same request again: recovered only if it then succeeds
            r.stats["unrecovered"] += 1
            r.last_error = error or status
            if self._stop and retry:
                return "SHUTDOWN"
            return {"ACCESS_DENIED": "ACCESS_DENIED", "INVALID_RESPONSE": "INVALID_RESPONSE", "TOO_LARGE": "TOO_LARGE"}.get(
                status, "RETRIES_EXHAUSTED" if retry else "HTTP_ERROR")

    # ------------------------------------------------------------------------------------------------ recovery and health
    def recover(self, *, stale_after: timedelta = timedelta(seconds=LEASE_TTL_S)) -> dict:
        """After a crash or a restart: a run with no end whose lease is no longer live is INTERRUPTED (its watermark never moved); the store
        datasets such runs left open are sealed and anchored by the Stage 7.1 recover_pending (which never touches recent activity)."""
        now = self._now()
        out = {"interrupted": [], "left_running": []}
        for run in self.state.open_runs():
            if self.state.lease_active(run["lease_id"], now):
                out["left_running"].append(run["run_id"])
                continue
            self.state.end_run(run["run_id"], status="INTERRUPTED", now=now, stop="RECOVERED_INTERRUPTED_RUN", failed=False, clock_skew=False,
                               last_error="", content_sha256=None, counts={})
            out["interrupted"].append(run["run_id"])
        out["store"] = IG.recover_pending(self.store, self.archive, clock=self.clock, stale_after=stale_after)
        return out

    def health(self, plan: PL.CollectionPlan, scope: PL.Scope) -> dict:
        b = self.budgets.get(plan.budget_name)
        now = self._now()
        return HE.source_health(plan, scope, state=self.state, now=now, credentials=self.credentials,
                                budget_refusal=None if b is None else self.budget_refusal(b, now))
