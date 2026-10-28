"""
dynamic_universe/plan.py - the Stage 6.10C universe plan: settings, the read-only status and pipeline description, and the future selection rule.

  settings()        DYNAMIC_UNIVERSE_* normalised: mode OFF | SHADOW (ACTIVE and any unknown value read as OFF and are reported as a problem)
  status(live)      the inactive universe at a glance: the live watchlist, the core, the scanner (0 of 6 signals implemented) and the active set,
                    which is exactly the live watchlist (checked on every call, never assumed)
  plan(live, keep)  the pipeline core -> scanner -> active -> evaluator -> risk -> execution, stage by stage; SHADOW adds what select_active()
                    would choose now - a preview, never applied
  select_active()   the pure FUTURE rule: live watchlist + kept symbols (open setup episodes, held paper positions) + ranked core candidates,
                    bounded by the active maximum without ever dropping a live or kept symbol

Read on demand by the read-only decision flow (lazily) and by tests and tools; no live path imports this module. It starts no thread, makes
no request and writes nothing (a configured core file is only read).
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

from dynamic_universe import ACTIVE_MAX_DEFAULT, ACTIVE_MAX_LIMIT, LIVE_SOURCE, MODES, NOTE, OFF, SHADOW, STAGE, VERSION
from dynamic_universe import signals as SIG
from dynamic_universe.core import Core, load_core

STAGES = ("core", "scanner", "active", "evaluator", "risk", "execution")
ACTIVATION_REQUIREMENTS = (
    "Keep every open setup episode in scope (the evaluator's symbols_in_scope already adds them to the watchlist).",
    "Keep the symbol of every open Alpaca PAPER position in scope: the DEMAND_FAILURE and DISLOCATION_NORMALIZED exits need fresh setup "
    "evaluations of held symbols.",
    "Stay inside the market-data request budget: about 3 requests per symbol per evaluator cycle (DESIGN_610C section 9).",
    "Implement and test the scanner signals first: no candidate can enter the active set before then.",
)


def _text(v: Any, n: int = 40) -> str:
    return str(v).strip()[:n]


def settings(cfg: Any = None) -> Dict[str, Any]:
    """{mode, configured_mode, core_file, active_max, problems}. ACTIVE (a later stage) and any unknown mode read as OFF and are reported; an
    active maximum that is not an integer from 1 to ACTIVE_MAX_LIMIT reads as ACTIVE_MAX_DEFAULT and is reported."""
    if cfg is None:
        import config
        cfg = config
    raw = _text(getattr(cfg, "DYNAMIC_UNIVERSE_MODE", OFF) or "").upper()
    mode = raw if raw in MODES else OFF
    problems: List[str] = []
    if raw == "ACTIVE":
        problems.append("DYNAMIC_UNIVERSE_MODE=ACTIVE is not available in Stage 6.10C (activation is a later stage): OFF applies")
    elif raw and raw not in MODES:
        problems.append(f"DYNAMIC_UNIVERSE_MODE={raw!r} is not one of {', '.join(MODES)}: OFF applies")
    amax = getattr(cfg, "DYNAMIC_UNIVERSE_ACTIVE_MAX", ACTIVE_MAX_DEFAULT)
    if isinstance(amax, str) and amax.strip().isdecimal() and len(amax.strip()) <= 6:
        amax = int(amax.strip())
    if isinstance(amax, bool) or not isinstance(amax, int) or not 1 <= amax <= ACTIVE_MAX_LIMIT:
        problems.append(f"DYNAMIC_UNIVERSE_ACTIVE_MAX={_text(amax, 20)!r} is not an integer from 1 to {ACTIVE_MAX_LIMIT}: {ACTIVE_MAX_DEFAULT} applies")
        amax = ACTIVE_MAX_DEFAULT
    return {"mode": mode, "configured_mode": raw or OFF, "core_file": _text(getattr(cfg, "DYNAMIC_UNIVERSE_CORE_FILE", "") or "", 1024),
            "active_max": amax, "problems": problems}


def _symbols(symbols: Any) -> Tuple[Tuple[str, ...], List[str]]:
    """(valid upper-case symbols, de-duplicated in order; the rejected inputs). The API's ticker rule only: nothing is looked up."""
    from api.validation import validate_symbol
    if symbols is None:
        return (), []
    if isinstance(symbols, str):
        symbols = [symbols]
    out: List[str] = []
    rejected: List[str] = []
    for s in symbols:
        try:
            v = validate_symbol(str(s))
        except ValueError:
            rejected.append(_text(s, 20))
            continue
        if v not in out:
            out.append(v)
    return tuple(out), rejected


def _active_now(live: Tuple[str, ...]) -> Tuple[str, ...]:
    """Stage 6.10C's active universe: the live watchlist itself (no selection runs)."""
    return tuple(live)


def _require_active_is_live(active: Tuple[str, ...], live: Tuple[str, ...]) -> None:
    if tuple(active) != tuple(live):   # explicit (not `assert`): it must hold under python -O as well
        raise AssertionError("Stage 6.10C: the active universe must be exactly the live watchlist")


def scanner_status() -> Dict[str, Any]:
    done = SIG.implemented()
    return {"signals": SIG.describe(), "declared": len(SIG.SIGNALS), "implemented": len(done), "candidates": [],
            "note": (f"Interfaces only: {len(done)} of {len(SIG.SIGNALS)} signals implemented. No signal value is computed, no candidate is "
                     "produced, and the scanner reads no news or catalyst source.")}


def _view(live_symbols: Any) -> Tuple[Dict[str, Any], Tuple[str, ...], Core, Tuple[str, ...], List[str]]:
    """(settings, live, core, active, problems) of one call: the configuration, the core file and the live list are read once."""
    s = settings()
    live, rejected = _symbols(live_symbols)
    core = load_core(s["core_file"])
    active = _active_now(live)
    _require_active_is_live(active, live)
    problems = list(s["problems"]) + list(core.problems) + [f"live symbol {r!r} is not a valid ticker - ignored" for r in rejected]
    return s, live, core, active, problems


def _status(s: Dict[str, Any], live: Tuple[str, ...], core: Core, active: Tuple[str, ...], problems: List[str]) -> Dict[str, Any]:
    in_core = set(core.symbols)
    return {"version": VERSION, "stage": STAGE, "mode": s["mode"], "configured_mode": s["configured_mode"],
            "live": {"count": len(live), "symbols": list(live), "source": LIVE_SOURCE},
            "core": {**core.summary(), "live_in_core": sum(1 for x in live if x in in_core)},
            "scanner": scanner_status(),
            "active": {"count": len(active), "equals_live": tuple(active) == tuple(live), "max": s["active_max"], "symbols": list(active)},
            "problems": list(problems), "note": NOTE}


def status(live_symbols: Iterable[str]) -> Dict[str, Any]:
    """The inactive universe at a glance. `live_symbols` is the live watchlist as the evaluator reads it; the active set equals it."""
    return _status(*_view(live_symbols))


def plan(live_symbols: Iterable[str], keep_symbols: Iterable[str] = (), now: Optional[datetime] = None) -> Dict[str, Any]:
    """status() plus the pipeline stage by stage, the symbols any activation must keep, the selection rule and its activation requirements.
    `keep_symbols`: open setup episodes and open paper-position symbols. SHADOW adds the select_active() preview (never applied)."""
    import config
    s, live, core, active, problems = _view(live_symbols)
    keep, rejected = _symbols(keep_symbols)
    problems += [f"kept symbol {r!r} is not a valid ticker - ignored" for r in rejected]
    at = now if now is not None else datetime.now(timezone.utc)
    at = at if at.tzinfo is not None else at.replace(tzinfo=timezone.utc)
    interval = getattr(config, "SETUP_EVAL_INTERVAL_SECONDS", 60)
    session = " in the regular session" if getattr(config, "SETUP_MARKET_HOURS_ONLY", True) else ""
    done = len(SIG.implemented())
    stages = [
        {"id": "core", "label": "Core universe", "state": "DECLARED", "count": core.count,
         "text": f"{core.count} US stocks ({core.version}, sha256 {core.sha256[:12]}); read by no live path"},
        {"id": "scanner", "label": "Cheap scanner", "state": "INTERFACES_ONLY", "count": 0,
         "text": f"{len(SIG.SIGNALS)} signals declared, {done} implemented: no candidate is produced"},
        {"id": "active", "label": "Active universe", "state": "LIVE_WATCHLIST", "count": len(active),
         "text": f"exactly the live watchlist ({LIVE_SOURCE}, {len(active)} symbols), unchanged"},
        {"id": "evaluator", "label": "Setup evaluator", "state": "UNCHANGED", "count": None,
         "text": f"the existing setup evaluator: the live watchlist plus any open setup episode, every {interval:g} s{session}"},
        {"id": "risk", "label": "Risk engine", "state": "UNCHANGED", "count": None,
         "text": "the existing execution safety gate and entry controls, reached by READY setups only"},
        {"id": "execution", "label": "Execution engine", "state": "UNCHANGED", "count": None,
         "text": "the existing Alpaca PAPER executor: it reads open READY setup episodes, never the universe"},
    ]
    shadow = None
    if s["mode"] == SHADOW:
        chosen = select_active(core.symbols, (), live, keep, s["active_max"])
        shadow = {"selected": list(chosen), "count": len(chosen), "beyond_live": [x for x in chosen if x not in live], "applied": False,
                  "text": ("What the future rule would select now: the live watchlist, then the kept symbols, then ranked scanner candidates "
                           f"(none: {done} signals implemented). A preview only; nothing reads it.")}
    return {**_status(s, live, core, active, problems), "at": at.astimezone(timezone.utc).isoformat(timespec="seconds"), "stages": stages,
            "keep": {"count": len(keep), "symbols": list(keep), "outside_live": [k for k in keep if k not in live],
                     "text": "open setup episodes and open paper-position symbols: any later activation must keep them in scope"},
            "selection_rule": {"order": ["live watchlist", "open setup episodes and held paper positions", "ranked core candidates"],
                               "bound": "DYNAMIC_UNIVERSE_ACTIVE_MAX limits the candidates only: a live or kept symbol is never dropped",
                               "max": s["active_max"], "used_by_live_path": False},
            "shadow": shadow, "activation_requirements": list(ACTIVATION_REQUIREMENTS),
            "side_effects": {"requests": 0, "threads": 0, "writes": 0}}


def _ranked(candidates: Any) -> List[str]:
    """Candidate symbols best first: (symbol, score) pairs by descending score, ties by symbol, then plain symbols in their given order.
    A pair whose score is not a finite number, and any invalid symbol, is skipped."""
    from api.validation import validate_symbol
    if isinstance(candidates, dict):
        candidates = list(candidates.items())
    elif isinstance(candidates, str):
        candidates = [candidates]
    scored: List[Tuple[float, str]] = []
    plain: List[str] = []
    for c in candidates or ():
        if isinstance(c, str):
            sym, score = c, None
        elif isinstance(c, (tuple, list)) and len(c) == 2:
            sym, score = c
        else:
            continue
        try:
            sym = validate_symbol(str(sym))
        except ValueError:
            continue
        if score is None:
            plain.append(sym)
            continue
        if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score):
            continue
        scored.append((-float(score), sym))
    out: List[str] = []
    for sym in [x for _, x in sorted(scored)] + plain:
        if sym not in out:
            out.append(sym)
    return out


def select_active(core_symbols: Iterable[str], candidates: Iterable[Any], live_symbols: Iterable[str], keep_symbols: Iterable[str],
                  max_size: int) -> Tuple[str, ...]:
    """The FUTURE active-universe rule - pure and deterministic; called by no live path in Stage 6.10C:
      1. every live watchlist symbol, in order;
      2. every kept symbol (open setup episodes, held paper positions) not already selected, in order - even one outside the core;
      3. scanner candidates that are in the core, best first (see _ranked), while fewer than max_size symbols are selected.
    max_size bounds step 3 only: a live or kept symbol is never dropped, so the result can be longer than max_size."""
    if isinstance(max_size, bool) or not isinstance(max_size, int) or max_size < 0:
        raise ValueError("max_size must be a non-negative integer")
    core = set(_symbols(core_symbols)[0])
    out: List[str] = list(_symbols(live_symbols)[0])
    for sym in _symbols(keep_symbols)[0]:
        if sym not in out:
            out.append(sym)
    for sym in _ranked(candidates):
        if len(out) >= max_size:
            break
        if sym in core and sym not in out:
            out.append(sym)
    return tuple(out)
