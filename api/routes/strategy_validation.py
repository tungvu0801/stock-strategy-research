"""
api/routes/strategy_validation.py — Stage 6.2 STRATEGY VALIDATION views (GET only; included under the Strategy Registry prefix by
api/routes/strategy_registry.py). Nothing here launches a backtest, changes a lifecycle, orders or calls out: research runs are made by the CLI
(python -m strategy_validation validate ...) and only read here.

GET /api/strategy-registry/{strategy_id}/validation           the Strategy Lab Validation section (latest run, or "Not yet evaluated" + why)
GET /api/strategy-registry/validation/runs/{run_id}           one stored run (config snapshot, split, friction, integrity, causality, metrics, verdict)
GET /api/strategy-registry/validation/runs/{run_id}/simulations   its hypothetical simulated trades (?arm=RULES|GATED|MODEL)
GET /api/strategy-registry/validation/runs/{run_id}/audit     the independent mechanical audit, the integrity check and the candidate set

Route words: no path contains "trade" (tests/test_regression_isolation.py), "order", "broker", "paper" or "execute", so the simulated trades are
served under /simulations. SYNTHETIC fixture runs are invisible outside a test process.
"""
import re

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

router = APIRouter(tags=["strategy-validation"])
_RUN = re.compile(r"^[0-9a-f]{32}$")
_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_]{2,63}$")


def _store():
    from strategy_validation.store import StrategyValidationStore
    return StrategyValidationStore()


def _missing(run_id: str) -> JSONResponse:
    return JSONResponse({"status": "NOT_FOUND", "message": f"no validation run {run_id}"}, status_code=404)


@router.get("/{strategy_id}/validation")
def get_strategy_validation(strategy_id: str) -> JSONResponse:
    from strategy_registry import registry as R
    from strategy_validation import service as SV
    if not _ID.match(strategy_id or ""):
        return JSONResponse({"status": "INVALID", "message": "strategy_id must be UPPER_SNAKE_CASE"}, status_code=400)
    entry = R.get(strategy_id)
    if entry is None:
        return JSONResponse({"status": "NOT_FOUND", "message": f"no strategy {strategy_id.upper()} in the registry"}, status_code=404)
    return JSONResponse({"strategy_id": entry.strategy_id, "declared_status": entry.status, **SV.detail(_store().latest(entry.strategy_id), entry.strategy_id, entry)})


@router.get("/validation/runs/{run_id}")
def get_validation_run(run_id: str) -> JSONResponse:
    if not _RUN.match(run_id or ""):
        return JSONResponse({"status": "INVALID", "message": "run_id must be a 32-character hex id"}, status_code=400)
    run = _store().run(run_id)
    if run is None:
        return _missing(run_id)
    return JSONResponse(run)                                                     # the simulated trades are served by /simulations


@router.get("/validation/runs/{run_id}/simulations")
def get_validation_simulations(run_id: str, arm: str = Query(default="")) -> JSONResponse:
    if not _RUN.match(run_id or "") or (arm and arm.upper() not in ("RULES", "GATED", "MODEL")):
        return JSONResponse({"status": "INVALID", "message": "run_id must be a 32-character hex id; arm RULES | GATED | MODEL"}, status_code=400)
    store = _store()
    if store.run(run_id) is None:
        return _missing(run_id)
    rows = store.trades(run_id, arm.upper() or None)
    return JSONResponse({"run_id": run_id, "simulations": rows, "count": len(rows), "note": "hypothetical simulated trades of a research backtest; nothing was ever ordered"})


@router.get("/validation/runs/{run_id}/audit")
def get_validation_audit(run_id: str) -> JSONResponse:
    if not _RUN.match(run_id or ""):
        return JSONResponse({"status": "INVALID", "message": "run_id must be a 32-character hex id"}, status_code=400)
    store = _store()
    run = store.run(run_id)
    if run is None:
        return _missing(run_id)
    return JSONResponse({"run_id": run_id, "audit": run.get("audit"), "integrity": run.get("integrity"), "causality": run.get("causality"),
                         "candidates": store.candidates(run_id), "candidates_skipped": run.get("candidates_skipped")})
