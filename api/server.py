"""
api/server.py — FastAPI application entry point.

Run with (from the stock-agent/ directory, with the venv active):
    uvicorn api.server:app --host 127.0.0.1 --port 8000 --reload

Serves the JSON API under /api/* and the static dashboard frontend at /.
Missing Alpaca credentials do NOT prevent the server from starting — the
dashboard shell still loads, and individual endpoints return a clear 503
until .env is filled in. This module never imports Alpaca's trading
client, so nothing here can place an order.
"""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

import config
from api.routes import (agent, ai_explain, alerts, alpaca_paper, alpaca_paper_orders, backtests, daily_brief, daily_brief_delivery,
                        daily_review,
                        evidence_comparison, explanation_history, paper,
                        forward_automation, forward_tests,
                        insights, market, ops_status, portfolio, research, research_history, saved_scans, stock_decision, stocks,
                        strategies, strategy_fit, strategy_scanner, trade_review, watchlist)
from api.routes import portfolio_rotation
from api.routes import research_workflow
from api.routes import portfolio_backtest
from api.routes import portfolio_walkforward
from api.routes import model_campaign
from api.routes import rotation_diagnostics
from api.routes import signal_research
from api.routes import evidence
from api.routes import trader_view
from api.routes import setups
from api.routes import proposals
from api.routes import execution_safety  # Stage 5.9.1: persisted execution safety gate (state + review-only actions)
from api.routes import execution  # Stage 6.0: autonomous PAPER execution of READY setups / reviewable proposals behind the Stage 5.9.1 safety gate
from api.routes import account_refresh  # Stage 6.0.1: read-only account refresh status feeding proposals and the Stage 5.9.1 gate
from api.routes import cash_flow_status  # Stage 6.0.2: cash-flow status (GET only) - the deposits / withdrawals input of the Stage 5.9.1 gate
from api.routes import forward_validation  # Stage 6.1: forward paper validation (GET only) - the descriptive record of the setups -> proposals -> paper execution funnel and session health
from api.routes import strategy_registry  # Stage 6.1.2: Strategy Lab registry + Strategy Standard v1 (GET only) - describes the setups -> proposals -> safety -> execution strategy; no order path
from api.routes import unified_portfolio  # Stage 6.9: unified portfolio (GET only) - Alpaca PAPER + Robinhood READ-ONLY holdings, exposure; no write path
from api.routes import decision_flow  # Stage 6.10C: agent decision flow (GET only) - read-only cycles, READY checks and decision history from persisted rows; no write path
from database.database import get_db
from models.schemas import HealthResponse

logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")

@asynccontextmanager
async def _lifespan(_app):
    """Stage 2.7G.1: start warming the Stage 2.6 event calendars in a daemon thread. Returns immediately — startup
    never waits for FRED, a failure is only logged, and no Claude call is made."""
    if config.EVENT_WARMUP_ON_STARTUP:
        try:
            from insights.warmup import start_event_warmup
            start_event_warmup()
        except Exception as exc:  # noqa: BLE001 - warming is an optimisation; the server must start regardless
            logging.getLogger(__name__).warning("Event calendar warm-up could not start: %s", exc)
    # Stage 4.4: desktop notifications already ON (e.g. enabled in Stage 4.3) keep their per-user notification identity
    try:
        from notifications import delivery as _dlv
        from notifications import identity as _ident
        if _dlv.DeliveryStore().settings()["enabled"]:
            _ident.register()
    except Exception as exc:  # noqa: BLE001 - branding is optional; the server must start regardless
        logging.getLogger(__name__).warning("Notification identity not registered: %s", type(exc).__name__)
    # Stage 3.7: the opt-in automatic forward capture starts only if the user turned it on (default OFF)
    try:
        from forward.automation import start_if_enabled
        start_if_enabled()
    except Exception as exc:  # noqa: BLE001 - automation must never stop the server from starting
        logging.getLogger(__name__).warning("AUTO_CAPTURE could not start: %s", type(exc).__name__)
    try:                                                    # Stage 5.8: market-hours setup-state evaluator (opt-out; never in tests)
        from setups.scheduler import start_if_enabled as _setups_start
        _setups_start()
        from safety.service import startup_if_enabled as _safety_start  # Stage 5.9.1: apply the startup policy, evaluate the gate once
        _safety_start()  # Stage 5.9.1
        from account_refresh.scheduler import start_if_enabled as _acct_start  # Stage 6.0.1: read-only account refresh for proposals / the Stage 5.9.1 gate; never in tests
        _acct_start()  # Stage 6.0.1: first action = STARTUP refresh; until it succeeds proposals / the Stage 5.9.1 gate stay paused
        from autorun.service import startup_if_enabled as _autorun_startup  # Stage 6.10A: the single-instance lease, then Monitor only (MANUAL_ARM)
        _autorun_startup()  # Stage 6.10A: a start never keeps Paper Auto armed; raises (no executor, no observer: fail closed) when this process may not run them
        from execution.scheduler import start_if_enabled as _exec_start  # Stage 6.0: the paper executor consumes setups / proposals / the safety gate; never in tests
        _exec_start()  # Stage 6.0: setups -> proposals -> safety gate -> PAPER execution loop (idle in MONITOR_ONLY; PAPER host only)
        from forward_validation.scheduler import start_if_enabled as _fv_start  # Stage 6.1: forward validation observer (reads the setups / proposals / execution state; never in tests)
        _fv_start()  # Stage 6.1: records session health and the setups -> proposals -> execution funnel; its only action is the early stop (Paper Auto -> Monitor only)
        from autorun.service import mark_started as _autorun_started  # Stage 6.10A: the threads above are this process's own (no takeover on the first tick)
        _autorun_started()  # Stage 6.10A
    except Exception as exc:  # noqa: BLE001 - the evaluator must never stop the server from starting
        logging.getLogger(__name__).warning("Setup evaluator could not start: %s", type(exc).__name__)
    try:                                                    # Stage 6.10A: the auto-run supervisor (lease renewal, preflight, automatic disarm, opt-in scheduled arm; never in tests)
        from autorun.scheduler import start_if_enabled as _autorun_start
        _autorun_start()
    except Exception as exc:  # noqa: BLE001 - the supervisor must never stop the server from starting
        logging.getLogger(__name__).warning("Auto-run supervisor could not start: %s", type(exc).__name__)
    # >>> Evidence refresh, Stage 5.4.2: rebuilds stale Stage 5.4 packets outside the regular session (default OFF; never in tests; zero model calls)
    try:
        from evidence_refresh.scheduler import start_if_enabled as _evr_start
        _evr_start()
    except Exception as exc:  # noqa: BLE001 - the refresher must never stop the server from starting
        logging.getLogger(__name__).warning("Evidence refresh could not start: %s", type(exc).__name__)
    # <<< Evidence refresh, Stage 5.4.2
    yield
    # >>> Evidence refresh, Stage 5.4.2: stop the refresher first (it touches no trading state)
    try:
        from evidence_refresh.scheduler import shutdown as _evr_shutdown
        _evr_shutdown()
    except Exception as exc:  # noqa: BLE001 - shutting down regardless
        logging.getLogger(__name__).warning("Evidence refresh did not stop cleanly: %s", type(exc).__name__)
    # <<< Evidence refresh, Stage 5.4.2
    try:                                                    # Stage 6.10A: stop the auto-run supervisor first (it can disarm Paper Auto)
        from autorun.scheduler import shutdown as _autorun_shutdown
        _autorun_shutdown()
    except Exception as exc:  # noqa: BLE001 - shutting down regardless
        logging.getLogger(__name__).warning("Auto-run supervisor did not stop cleanly: %s", type(exc).__name__)
    try:                                                    # Stage 5.8: stop the setup evaluator thread
        from forward_validation.scheduler import shutdown as _fv_shutdown  # Stage 6.1: stop the forward validation observer first (a setups / proposals / execution reader)
        _fv_shutdown()  # Stage 6.1 (setups / proposals / execution reader)
        from execution.scheduler import shutdown as _exec_shutdown  # Stage 6.0: stop the paper executor thread before the setups evaluator
        _exec_shutdown()  # Stage 6.0 (setups / proposals consumer)
        from account_refresh.scheduler import shutdown as _acct_shutdown  # Stage 6.0.1: stop the account refresh thread (proposals / Stage 5.9.1 gate source)
        _acct_shutdown()  # Stage 6.0.1 (proposals / Stage 5.9.1 gate source)
        from setups.scheduler import shutdown as _setups_shutdown
        _setups_shutdown()
    except Exception as exc:  # noqa: BLE001 - shutting down regardless
        logging.getLogger(__name__).warning("Setup evaluator did not stop cleanly: %s", type(exc).__name__)
    try:                                                    # Stage 6.10A: release the instance lease last, after the executor and the observer stopped
        from autorun.scheduler import release_lease as _autorun_release
        _autorun_release()
    except Exception as exc:  # noqa: BLE001 - the lease expires by itself
        logging.getLogger(__name__).warning("Auto-run lease not released: %s", type(exc).__name__)
    try:
        from forward.automation import shutdown
        shutdown()
    except Exception as exc:  # noqa: BLE001 - shutting down regardless
        logging.getLogger(__name__).warning("AUTO_CAPTURE did not stop cleanly: %s", type(exc).__name__)
    try:                                                    # Stage 4.4: icon removed, window destroyed, loop stopped
        from notifications import windows as _nw
        _nw.shutdown()
    except Exception as exc:  # noqa: BLE001 - shutting down regardless
        logging.getLogger(__name__).warning("Desktop notification adapter did not stop cleanly: %s", type(exc).__name__)


app = FastAPI(
    lifespan=_lifespan,
    title="Stock Agent Dashboard API",
    description=(
        "Market monitoring, watchlist, and AI technical-analysis endpoints. "
        "Analysis and research only — this API never places, modifies, or "
        "cancels a trade."
    ),
    version="1.0.0",
)

# Initializes data/stock_agent.db and runs (idempotent) migrations once per
# process. Deliberately at import time, not a startup event, so the database
# is ready before the first request even under test clients that skip lifespan.
get_db()

app.include_router(market.router)
app.include_router(watchlist.router)
app.include_router(stocks.router)
app.include_router(research.router)
app.include_router(research_history.router)
app.include_router(alerts.router)
app.include_router(agent.router)
app.include_router(portfolio.router)  # Stage 2.7C read-only portfolio (feature-flagged, off by default)
app.include_router(insights.router)  # Stage 2.7E beginner market context (read-only)
app.include_router(trade_review.router)  # Stage 2.7E educational trader review (read-only)
app.include_router(daily_review.router)  # Stage 2.7G portfolio + watchlist decision report (read-only)
app.include_router(ops_status.router)  # Stage 2.7G.1 operational status (read-only, no external calls)
app.include_router(stock_decision.router)  # Stage 2.7G.3 one-stock decision after Analyze (read-only)
app.include_router(strategies.router)  # Stage 3.1 Strategy Lab: versioned strategy definitions (no backtest, no orders)
app.include_router(backtests.router)  # Stage 3.2 historical backtests of saved BACKTEST READY versions (no orders, no AI)
app.include_router(forward_tests.router)  # Stage 3.3 forward-test signal journal: manual captures only (no orders, no AI)
app.include_router(strategy_fit.router)  # Stage 3.4 Strategy Fit: current rule fit of saved versions (read-only, no AI)
app.include_router(evidence_comparison.router)  # Stage 3.5: stored historical vs forward evidence (read-only, no AI)
app.include_router(forward_automation.router)  # Stage 3.7: opt-in automatic after-close forward capture (capture only)
app.include_router(ai_explain.router)  # Stage 3.8: explicit, grounded AI explanation of Strategy Fit / Evidence (<=1 call)
app.include_router(explanation_history.router)  # Stage 3.9: optional, local, append-only AI explanation history
app.include_router(strategy_scanner.router)  # Stage 4.0: deterministic read-only Strategy Scanner (one version, one list)
app.include_router(saved_scans.router)  # Stage 4.1: saved scans (opt-in, local) + in-app RULES MET change alerts
app.include_router(saved_scans.alerts_router)
app.include_router(daily_brief.router)  # Stage 4.2: read-only Daily Brief of stored data (GET only)
app.include_router(daily_brief_delivery.router)  # Stage 4.3: opt-in local desktop delivery of the Daily Brief
app.include_router(paper.portfolio_router)  # Stage 4.5: LOCAL paper portfolio — simulated, no broker, no real order
app.include_router(paper.orders_router)
app.include_router(paper.fills_router)
app.include_router(alpaca_paper.router)  # Stage 4.6A: Alpaca PAPER account, READ ONLY (explicit refresh; no broker action)
app.include_router(alpaca_paper_orders.router)  # Stage 4.6B: manual PAPER orders — preview, then explicit click Confirm only
app.include_router(portfolio_rotation.router)  # Stage 4.7: deterministic portfolio rotation PROPOSALS (display only; no order path)
app.include_router(research_workflow.router)  # research workflow: shortlist-before-LLM, cached, budgeted; research only, no order path
app.include_router(portfolio_backtest.router)  # Stage 4.8: historical rotation backtest — research only, nothing is traded
app.include_router(portfolio_walkforward.router)  # Stage 4.9: walk-forward robustness — research only, nothing is traded or deployed
app.include_router(model_campaign.router)  # Stage 5.0: model evaluation campaign / leaderboard — research only, nothing is traded or activated
app.include_router(rotation_diagnostics.router)  # Stage 5.1: attribution & benchmark diagnostics — research only
app.include_router(signal_research.router)  # Stage 5.2: deterministic signal research — research only, nothing is traded or activated
app.include_router(evidence.router)  # Stage 5.4: multi-source evidence packets — research only; evidence strength is not a price probability
app.include_router(setups.router)  # Stage 5.8: setup state machine + alerts — read-only state, read-acknowledgement only; no order path
app.include_router(proposals.router)  # Stage 5.9: review proposals for READY setups — review artifact only, review-state POSTs only; no order path
app.include_router(execution_safety.router)  # Stage 5.9.1: execution safety gate — persisted kill switch / daily-loss halt / pause; safety state only, no order path
app.include_router(execution.router)  # Stage 6.0: autonomous PAPER execution loop (setups -> proposals -> safety gate -> paper order); mode POSTs only; PAPER host only
app.include_router(account_refresh.router)  # Stage 6.0.1: account refresh status (GET only) - the read-only source of proposals and the Stage 5.9.1 gate
app.include_router(cash_flow_status.router)  # Stage 6.0.2: cash-flow status (GET only) - adjusted daily P&L of the Stage 5.9.1 gate
app.include_router(forward_validation.router)  # Stage 6.1: forward paper validation (GET only) - session health, the setups -> proposals -> execution funnel, closed paper trades, reports, exports
app.include_router(strategy_registry.router)  # Stage 6.1.2: strategy registry + Strategy Standard v1 (GET only) - the setups -> proposals -> safety gate -> execution strategy described, never executed
app.include_router(unified_portfolio.router)  # Stage 6.9: unified portfolio (GET only) - Robinhood is read only and never an execution broker
app.include_router(decision_flow.router)  # Stage 6.10C: agent decision flow (GET only) - a read-only projection of persisted rows; no write path, no broker call
# >>> Evidence refresh, Stage 5.4.2
from api.routes import evidence_refresh as _evidence_refresh_routes  # noqa: E402 - evidence refresh status (GET only): mode, thread, plan; never builds
app.include_router(_evidence_refresh_routes.router)
# <<< Evidence refresh, Stage 5.4.2
app.include_router(trader_view.router)  # Stage 5.5: Trader View — read-only terminal (GET + one WebSocket); simulated or read-only market data; no order path


@app.get("/api/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(
        status="ok",
        alpaca_configured=config.has_credentials(),
        llm_configured=config.has_llm_credentials(),
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request, exc):  # noqa: ANN001 - FastAPI signature
    """Last-resort catch-all so an unexpected error never leaks internals (or a stack trace) to the client."""
    logging.getLogger(__name__).error("Unhandled error on %s: %s", request.url.path, exc)
    return JSONResponse(status_code=500, content={"detail": "Internal server error."})


# Static dashboard frontend (plain HTML/CSS/JS — see project plan for why,
# pending a Node.js/React upgrade). Mounted last so it doesn't shadow /api/*.
app.mount("/", StaticFiles(directory=str(config.PROJECT_ROOT / "frontend"), html=True), name="frontend")
