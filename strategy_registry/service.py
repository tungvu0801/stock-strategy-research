"""
strategy_registry/service.py — the read-only views of the registry: lifecycle derivation from persisted facts, the registry listing and one
strategy's detail. Pure functions over plain dicts (the API gathers the facts; the tests pass them in). Nothing here writes, orders or calls out.

Lifecycle derivation (documented, deterministic, never a human decision):
  * a placeholder keeps its declared status (RESEARCH)
  * the implemented strategy is declared PAPER_CANDIDATE (the Stage 6.1 forward paper validation exists for it) and reads PAPER_ACTIVE only while a
    persisted fact says so: at least one recorded validation session, or Paper Auto currently ON (GET /api/execution/status mode == PAPER_AUTO)
  * APPROVED / REJECTED are never derived: no decision record exists yet, so the Decision log says so
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from strategy_registry import (ACTIVE_STATUSES, DISCLAIMER, NOT_EVALUATED, PIPELINE, QUANTCONNECT_PIPELINE, SECTIONS, SOURCE_LABELS, SOURCES, STANDARD_VERSION, STATUS_LABELS,
                               STATUSES, VERSION)
from strategy_registry import registry as R
from strategy_registry.models import Section, StrategyDetail, StrategyEntry

QUANTCONNECT_NOTE = ("QuantConnect is a strategy SOURCE, not a broker and not an integration yet: LEAN research and backtests would produce normalized Strategy Standard v1 "
                     "signals for the Stock Agent Strategy Adapter, which hands them to Opportunity Ranking, Portfolio, Risk / Safety, Execution and the Alpaca PAPER broker. "
                     "No QuantConnect strategy is registered and nothing is wired.")


def runtime_facts(stream_fn: Optional[Callable[[], Any]]) -> Dict[str, Any]:
    """The persisted facts the lifecycle reads: the execution mode and the forward paper validation summary. Every miss is reported, never invented."""
    out: Dict[str, Any] = {"available": False, "mode": None, "validation": None, "errors": []}
    if stream_fn is None:
        out["errors"].append("no stream")
        return out
    try:
        s = stream_fn()
    except Exception as exc:  # noqa: BLE001 - display only
        out["errors"].append(f"stream: {type(exc).__name__}")
        return out
    try:
        out["mode"] = (s.execution.mode() or {}).get("mode")
    except Exception as exc:  # noqa: BLE001
        out["errors"].append(f"execution: {type(exc).__name__}")
    try:
        ov = s.forward_validation.overview() or {}
        sess, strat = ov.get("sessions") or {}, ov.get("strategy") or {}
        out["validation"] = {"sessions_completed": int(sess.get("completed") or 0), "sessions_target": sess.get("target"), "sessions_label": sess.get("label"),
                             "closed_trades": strat.get("closed_trades"), "edge_status": strat.get("edge_status"), "edge_label": strat.get("edge_label"),
                             "insufficient": strat.get("insufficient"), "insufficient_text": strat.get("insufficient_text"), "needs_attention": bool(ov.get("needs_attention")),
                             "attention": [a.get("code") if isinstance(a, dict) else a for a in (ov.get("attention") or [])], "frozen_controls_hash": ov.get("frozen_controls_hash")}
    except Exception as exc:  # noqa: BLE001
        out["errors"].append(f"forward_validation: {type(exc).__name__}")
    out["available"] = out["mode"] is not None or out["validation"] is not None
    return out


def derive_lifecycle(entry: StrategyEntry, facts: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    f = facts or {}
    v = f.get("validation") or {}
    basis: List[str] = []
    status = entry.status
    if entry.implemented and entry.status == "PAPER_CANDIDATE":
        sessions = int(v.get("sessions_completed") or 0)
        if sessions > 0:
            status, basis = "PAPER_ACTIVE", [f"forward paper validation has recorded {sessions} session(s) (target {v.get('sessions_target') or '—'})"]
        elif f.get("mode") == "PAPER_AUTO":
            status, basis = "PAPER_ACTIVE", ["Paper Auto is ON (GET /api/execution/status mode PAPER_AUTO); no validation session recorded yet"]
        elif f.get("available"):
            basis = ["no forward paper validation session recorded yet and Paper Auto is OFF"]
        else:
            basis = ["runtime facts unavailable: the declared status is shown"]
    elif not entry.implemented:
        basis = ["placeholder: not implemented"]
    return {"declared": entry.status, "status": status, "label": STATUS_LABELS[status], "basis": basis, "decided": False,
            "decision": "No approval or rejection has been recorded; APPROVED / REJECTED are never derived."}


def _row(entry: StrategyEntry, facts: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    lc = derive_lifecycle(entry, facts)
    return {**entry.model_dump(), "declared_status": lc["declared"], "status": lc["status"], "status_label": lc["label"], "lifecycle_basis": lc["basis"],
            "source_label": SOURCE_LABELS[entry.source], "active": bool(entry.implemented and lc["status"] in ACTIVE_STATUSES)}


def _validation_summary(entry: StrategyEntry, validations: Optional[Dict[str, dict]]) -> dict:
    """Stage 6.2: the validation fields of a registry row (the latest stored run, or NOT_RUN and why). Display only: never a lifecycle input."""
    from strategy_validation import service as SV
    return (validations or {}).get(entry.strategy_id) or SV.summary(None, entry.strategy_id, entry)


def registry_view(facts: Optional[Dict[str, Any]] = None, validations: Optional[Dict[str, dict]] = None) -> Dict[str, Any]:
    rows = [{**_row(e, facts), **_validation_summary(e, validations)} for e in R.entries()]
    sources = [{"source": s, "label": SOURCE_LABELS[s], "count": sum(1 for r in rows if r["source"] == s),
                "note": QUANTCONNECT_NOTE if s == "QUANTCONNECT" else "Deterministic Python rules inside this repository."} for s in SOURCES]
    return {"version": VERSION, "standard_version": STANDARD_VERSION, "strategies": rows, "sources": sources, "lifecycle": [{"status": s, "label": STATUS_LABELS[s]} for s in STATUSES],
            "active_statuses": list(ACTIVE_STATUSES), "pipeline": list(PIPELINE), "quantconnect": {"integrated": False, "registered": sum(1 for r in rows if r["source"] == "QUANTCONNECT"),
                                                                                                    "pipeline": list(QUANTCONNECT_PIPELINE), "note": QUANTCONNECT_NOTE},
            "facts": facts or {"available": False}, "counts": {"strategies": len(rows), "implemented": sum(1 for r in rows if r["implemented"]), "placeholders": sum(1 for r in rows if not r["implemented"])},
            "disclaimer": DISCLAIMER}


def placeholder_sections(entry: StrategyEntry) -> List[Section]:
    out = [Section(key="overview", title="Overview", state="DOCUMENTED", items=[entry.description, f"Lifecycle: {STATUS_LABELS[entry.status]} (declared; no evaluation has started)."],
                   sources=["strategy_registry/registry.py"])]
    out += [Section(key=k, title=t, state="NOT_EVALUATED", note=NOT_EVALUATED) for k, t in SECTIONS if k != "overview"]
    return out


def detail_view(strategy_id: str, facts: Optional[Dict[str, Any]] = None, validation: Optional[dict] = None) -> Optional[Dict[str, Any]]:
    entry = R.get(strategy_id)
    if entry is None:
        return None
    lc = derive_lifecycle(entry, facts)
    if entry.implemented and entry.strategy_id == R.DDR_ID:
        from strategy_registry import downside_recovery as DR
        sections, links = DR.sections(facts), DR.LINKS
    elif entry.implemented and entry.strategy_id == "VALUE_QUALITY":                # Stage 6.4: research strategy #2 (documented from its frozen rules)
        from strategy_registry import value_quality as VQS
        sections, links = VQS.sections(), VQS.LINKS
    elif entry.implemented and entry.strategy_id == "MOMENTUM":                     # Stage 6.5: research strategy #3 (documented from its frozen rules)
        from strategy_registry import momentum as MOS
        sections, links = MOS.sections(), MOS.LINKS
    elif entry.implemented and entry.strategy_id == "MEAN_REVERSION":               # Stage 6.6: research strategy #4 (documented from its frozen rules)
        from strategy_registry import mean_reversion as MRS
        sections, links = MRS.sections(), MRS.LINKS
    else:
        sections, links = placeholder_sections(entry), {}
    detail = StrategyDetail(entry=entry, lifecycle=lc, sections=sections, links=dict(links), disclaimer=DISCLAIMER)
    d = detail.model_dump()
    d["entry"] = _row(entry, facts)
    d["version"], d["standard_version"] = VERSION, STANDARD_VERSION
    if validation is None:                                                     # Stage 6.2: the Validation section (latest stored run, or "Not yet evaluated")
        from strategy_validation import service as SV
        validation = SV.detail(None, entry.strategy_id, entry)
    d["validation"] = validation
    d["entry"].update({k: validation.get(k) for k in ("validation_status", "validation_label", "not_run_reason", "latest_validation_run_id", "validation_verdict",
                                                      "train_trade_count", "oos_trade_count", "mechanical_audit_status", "last_validated_at", "validation_synthetic")})
    return d
