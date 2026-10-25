"""
api/routes/strategy_registry.py — Stage 6.1.2 STRATEGY LAB FOUNDATION (GET only; nothing here creates, changes, activates, tunes, orders or calls out).

GET /api/strategy-registry                  every registered strategy (declared + derived lifecycle status, source, horizon, implemented flag), the two sources
                                            (INTERNAL, QUANTCONNECT), the lifecycle enum, the unchanged execution pipeline and the facts the derivation read
GET /api/strategy-registry/standard         the Strategy Standard v1 schema: required / optional fields, the forbidden execution keys, enums, rules
GET /api/strategy-registry/{strategy_id}    one strategy's detail: the thirteen sections (DOCUMENTED from code and design documents, LIVE from persisted
                                            records, or "Not yet evaluated"), the lifecycle basis and the product-UI links

The runtime facts (execution mode, forward paper validation summary) are read through the existing Trader View stream services the setups ->
proposals -> execution path already shares; a missing service is reported in `facts.errors`, never invented. Route words: no path contains
"trade", "paper", "order", "broker" or "execute" (tests/test_regression_isolation.py and the earlier stages' route-word rules).
"""
import re

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from strategy_registry import service as SR
from strategy_registry.standard import SCHEMA

router = APIRouter(prefix="/api/strategy-registry", tags=["strategy-registry"])
_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_]{2,63}$")


def _facts() -> dict:
    try:
        from api.routes.trader_view import stream
    except Exception as exc:  # noqa: BLE001 - display only
        return {"available": False, "mode": None, "validation": None, "errors": [f"import: {type(exc).__name__}"]}
    return SR.runtime_facts(stream)


def _validation_store():
    """Stage 6.2: the stored validation runs (read only); None when the database cannot be opened (the rows then read NOT_RUN)."""
    try:
        from strategy_validation.store import StrategyValidationStore
        return StrategyValidationStore()
    except Exception:  # noqa: BLE001 - display only
        return None


@router.get("")
def get_registry() -> JSONResponse:
    from strategy_registry import registry as R
    from strategy_validation import service as SV
    store = _validation_store()
    return JSONResponse(SR.registry_view(_facts(), SV.registry_summaries(store, R.entries()) if store is not None else None))


@router.get("/standard")
def get_standard() -> JSONResponse:
    return JSONResponse(SCHEMA)


@router.get("/{strategy_id}")
def get_strategy(strategy_id: str) -> JSONResponse:
    if not _ID.match(strategy_id or ""):
        return JSONResponse({"status": "INVALID", "message": "strategy_id must be UPPER_SNAKE_CASE"}, status_code=400)
    from strategy_registry import registry as R
    from strategy_validation import service as SV
    entry, store = R.get(strategy_id), _validation_store()
    validation = SV.detail(store.latest(entry.strategy_id) if (entry and store) else None, strategy_id.upper(), entry) if entry else None
    d = SR.detail_view(strategy_id.upper(), _facts(), validation)
    if d is None:
        return JSONResponse({"status": "NOT_FOUND", "message": f"no strategy {strategy_id.upper()} in the registry"}, status_code=404)
    return JSONResponse(d)


# Stage 6.2: the validation views (GET only) live under this prefix: /{strategy_id}/validation and /validation/runs/{run_id}[/simulations | /audit]
from api.routes import strategy_validation as _validation_routes  # noqa: E402

router.include_router(_validation_routes.router)
