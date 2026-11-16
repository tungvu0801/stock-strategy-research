"""
historical_data/validate.py - contract section 5 (+ v2 A9): the dataset checks and the machine-readable acceptance record.

BLOCK (any one makes the dataset unusable): DUPLICATE_KEY; NON_CALENDAR_SESSION; CALENDAR_NOT_COVERED; KNOWN_AT_MISMATCH (a bar's known_at is
not close_utc(session)); FACTOR_NOT_FINITE_OR_NONPOSITIVE; FACTOR_CHANGE_WITHOUT_EVENT (events COMPLETE and a factor moves more than 1e-5
relative between consecutive bars with no matching event); TICKER_REUSE_COLLISION (two instruments share a symbol at one session);
SPLIT_SERIES_MISSING (a declared split series absent, wholly or at a session); MANIFEST_HASH_MISMATCH; CALENDAR_CROSSCHECK_DISAGREEMENT;
TZDATA_UNAVAILABLE.
WARN: DAY_JUMP_UNCLASSIFIED (a raw close-to-close move strictly over 25 % without a matching provider event or VERIFIED table entry);
GAP_2_OR_MORE_SESSIONS; POSSIBLE_TICKER_REUSE (a gap over 20 sessions; the table's ticker-reuse rule only annotates it); SPIN_OFF_NOT_ADJUSTED;
VOLUME_FEED_NOT_CONSOLIDATED; PRICE_SOURCE_NOT_CONSOLIDATED; INFERRED_SPLIT (events not COMPLETE and split_factor moves more than 1e-5);
SYMBOL_DROPPED_BY_ADAPTER (MD-A addition, QA L-5: a source symbol that yields no instrument). A duplicate event_id is a DUPLICATE_KEY;
membership-change and scheduled-event sessions are calendar-checked like bar and event sessions (L-7). The record carries adapter_notes.
The verdict is BLOCK if any BLOCK reason exists, else WARN if any WARN reason, else CLEAN. Nothing is repaired, booked or written.
"""
from __future__ import annotations

import json
import math
from typing import Dict, List, Optional

import historical_data as HD
from historical_data import calendar as CAL
from historical_data import canonical as CN
from historical_data import expected_events as EE
from historical_data.schema import DISTRIBUTION_EVENTS, SHARE_EVENTS
from historical_data.status import is_missing

VALIDATOR_VERSION = "historical_data_validate_v1"
RECORD_VERSION = "historical_data_acceptance_record_v1"
_SEV = {c: HD.BLOCK for c in HD.BLOCK_CODES} | {c: HD.WARN for c in HD.WARN_CODES}
JUMP_EVENT_TYPES = SHARE_EVENTS + ("SPECIAL_DIVIDEND", "SPIN_OFF", "MERGER")


class UnsupportedContractVersion(ValueError):
    pass


def _reason(code, instrument_id=None, session=None, detail=""):
    return {"code": code, "severity": _SEV[code], "instrument_id": instrument_id, "session": session, "detail": detail}


def _rel_change(a: float, b: float) -> float:
    return abs(b / a - 1.0)


def _session_problem(s: str) -> Optional[str]:
    try:
        CAL.check_session(s)
    except CAL.CalendarNotCovered:
        return HD.CALENDAR_NOT_COVERED
    except (CAL.NotASession, ValueError):
        return HD.NON_CALENDAR_SESSION
    return None


def _runs(sessions: List[str]) -> List[List[str]]:
    """Consecutive calendar sessions grouped into runs."""
    out: List[List[str]] = []
    for s in sorted(sessions):
        if out and CAL.next_session(out[-1][-1]) == s:
            out[-1].append(s)
        else:
            out.append([s])
    return out


def validate(provider, *, table=EE.TABLE, calendar_crosscheck: Optional[dict] = None) -> dict:
    """The acceptance record of one dataset (section 5). `calendar_crosscheck` is injectable for tests; by default the real cross-check runs."""
    m = provider.manifest()
    if m.contract_version not in HD.SUPPORTED_CONTRACT_VERSIONS:
        raise UnsupportedContractVersion(f"{m.contract_version} is not supported ({HD.SUPPORTED_CONTRACT_VERSIONS})")
    reasons: List[dict] = []
    classified: List[dict] = []
    listed: Dict[str, dict] = {}

    # ---- the calendar: tzdata and the cross-check (section 2)
    tz_ok, tz_version, tz_detail = CAL.tzdata_status()
    if not tz_ok:
        reasons.append(_reason(HD.TZDATA_UNAVAILABLE, detail=tz_detail))
    cc = calendar_crosscheck if calendar_crosscheck is not None else json.loads(CAL.crosscheck_cached())
    if cc.get("status") != "AGREE":
        reasons.append(_reason(HD.CALENDAR_CROSSCHECK_DISAGREEMENT, detail=json.dumps(cc.get("disagreements", [])[:5], sort_keys=True)[:600]))

    # ---- the manifest's content hash (6.3)
    try:
        recomputed = CN.content_sha256(provider.canonical_document())
    except (ValueError, TypeError) as exc:                                     # e.g. a non-finite number smuggled past the schema
        recomputed = None
        reasons.append(_reason(HD.MANIFEST_HASH_MISMATCH, detail=f"the canonical form cannot be computed: {type(exc).__name__}"))
    if recomputed is not None and recomputed != m.content_sha256:
        reasons.append(_reason(HD.MANIFEST_HASH_MISMATCH, detail=f"manifest {m.content_sha256} != recomputed {recomputed}"))

    instruments = provider.instruments()
    events = provider.events()
    complete = m.events_completeness == "COMPLETE"
    split_declared = "SPLIT_ADJUSTED" in m.price_views_available
    feeds, sources = {m.volume_feed}, {m.price_source}
    n_bars = 0

    # ---- event sessions on the calendar; an event_id is a key (L-7)
    ids: Dict[str, int] = {}
    for e in events:
        ids[e.event_id] = ids.get(e.event_id, 0) + 1
        for s in (e.effective_session,) + tuple(x for x in (e.record_session, e.pay_session, e.last_trade_session) if isinstance(x, str)):
            p = _session_problem(s)
            if p:
                reasons.append(_reason(p, e.instrument_id, s, f"event {e.event_id} ({e.type})"))
    for eid, k in sorted(ids.items()):
        if k > 1:
            reasons.append(_reason(HD.DUPLICATE_KEY, None, None, f"event_id {eid}: {k} events"))

    # ---- membership-change and scheduled-event sessions on the calendar (L-7)
    for c in (provider.membership_changes() if hasattr(provider, "membership_changes") else []):
        p = _session_problem(c.session)
        if p:
            reasons.append(_reason(p, c.instrument_id, c.session, f"membership {c.universe_id} {c.action}"))
    sched = provider.all_scheduled_events() if hasattr(provider, "all_scheduled_events") else []
    for s_ev in ([] if is_missing(sched) else sched):
        p = _session_problem(s_ev.event_session)
        if p:
            reasons.append(_reason(p, s_ev.instrument_id, s_ev.event_session, f"scheduled event {s_ev.event_id}"))

    # ---- what the adapter could not turn into an instrument (L-5): never silent
    dropped = provider.dropped_symbols() if hasattr(provider, "dropped_symbols") else []
    for sym in dropped:
        reasons.append(_reason(HD.SYMBOL_DROPPED_BY_ADAPTER, None, None, f"{sym}: in the source, but no instrument (no valid bar)"))
    notes = provider.adapter_notes() if hasattr(provider, "adapter_notes") else ()
    adapter_notes = {"skipped_responses": sum(1 for n in notes if "skipped_response" in n),
                     "skipped_response_symbols": sorted({n["symbol"] for n in notes if "skipped_response" in n and "symbol" in n}),
                     "dropped_symbols": list(dropped),
                     "removed_rows": len(getattr(provider, "removed_rows", list)())}

    for inst in instruments:
        iid = inst.instrument_id
        bars = provider.bars(iid)
        n_bars += len(bars)
        ev = [e for e in events if e.instrument_id == iid]
        seen: Dict[str, int] = {}
        for b in bars:
            seen[b.session] = seen.get(b.session, 0) + 1
            feeds.add(b.volume_feed)
            sources.add(b.price_source)
            p = _session_problem(b.session)
            if p:
                reasons.append(_reason(p, iid, b.session, "bar"))
            elif tz_ok and b.known_at != CAL.close_utc(b.session):
                reasons.append(_reason(HD.KNOWN_AT_MISMATCH, iid, b.session, f"known_at {b.known_at.isoformat()} != close_utc "
                                                                            f"{CAL.close_utc(b.session).isoformat()}"))
            for f in ("split_factor", "total_return_factor"):
                v = getattr(b, f)
                if not is_missing(v) and (isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0):
                    reasons.append(_reason(HD.FACTOR_NOT_FINITE_OR_NONPOSITIVE, iid, b.session, f))
        for s, k in sorted(seen.items()):
            if k > 1:
                reasons.append(_reason(HD.DUPLICATE_KEY, iid, s, f"{k} bars"))

        # declared split series (BLOCK when absent, wholly or at a session: D-4)
        if split_declared and bars:
            holes = [b.session for b in bars if is_missing(b.split_factor)]
            if len(holes) == len(bars):
                reasons.append(_reason(HD.SPLIT_SERIES_MISSING, iid, None, "no split factor at any session"))
            else:
                for s in holes:
                    reasons.append(_reason(HD.SPLIT_SERIES_MISSING, iid, s, "no split factor at this session"))

        # consecutive bars: factor changes and day jumps
        uniq = [b for i, b in enumerate(bars) if i == 0 or b.session != bars[i - 1].session]
        for prev, cur in zip(uniq, uniq[1:]):
            window = (prev.session, cur.session)
            def match(types):
                return [e for e in ev if e.type in types and window[0] < e.effective_session <= window[1]]
            sp, sc = prev.split_factor, cur.split_factor
            if not is_missing(sp) and not is_missing(sc) and sp > 0 and sc > 0 and _rel_change(sp, sc) > HD.FACTOR_REL_TOLERANCE:
                if complete and not match(SHARE_EVENTS):
                    reasons.append(_reason(HD.FACTOR_CHANGE_WITHOUT_EVENT, iid, cur.session, f"split_factor {sp!r} -> {sc!r}"))
                elif not complete:
                    reasons.append(_reason(HD.INFERRED_SPLIT, iid, cur.session, f"split_factor {sp!r} -> {sc!r} (never booked)"))
            tp, tc = prev.total_return_factor, cur.total_return_factor
            if complete and not is_missing(tp) and not is_missing(tc) and tp > 0 and tc > 0 and _rel_change(tp, tc) > HD.FACTOR_REL_TOLERANCE \
                    and not match(SHARE_EVENTS + DISTRIBUTION_EVENTS):
                reasons.append(_reason(HD.FACTOR_CHANGE_WITHOUT_EVENT, iid, cur.session, f"total_return_factor {tp!r} -> {tc!r}"))
            move = cur.raw_close / prev.raw_close - 1.0
            if abs(move) > HD.DAY_JUMP_THRESHOLD:
                sym = inst.symbol_at(cur.session)
                by_event = match(JUMP_EVENT_TYPES)
                table_hits = EE.entries(sym, types=EE.JUMP_TYPES, session=cur.session, table=table) if sym else []
                verified = [x for x in table_hits if EE.classifying(x)]
                for x in table_hits:
                    if not EE.classifying(x):
                        listed[x.entry_id] = {**x.to_json(), "matched_jump": {"instrument_id": iid, "session": cur.session}}
                if by_event or verified:
                    classified.append({"instrument_id": iid, "symbol": sym, "session": cur.session, "move": move,
                                       "event_ids": [e.event_id for e in by_event], "table_entry_ids": [x.entry_id for x in verified]})
                else:
                    reasons.append(_reason(HD.DAY_JUMP_UNCLASSIFIED, iid, cur.session, f"{sym} raw close {prev.raw_close!r} -> {cur.raw_close!r} "
                                                                                     f"({move:+.4%}) since {prev.session}"))

        # gaps and possible ticker reuse
        gap_sessions = [g.session for g in provider.gaps(iid)]
        for run in _runs(gap_sessions) if gap_sessions else []:
            if len(run) >= HD.GAP_WARN_SESSIONS:
                reasons.append(_reason(HD.GAP_2_OR_MORE_SESSIONS, iid, run[0], f"{len(run)} sessions {run[0]}..{run[-1]}"))
            if len(run) > HD.TICKER_REUSE_GAP_SESSIONS:
                sym = inst.symbol_at(run[0])
                rule = EE.entries(sym, types=("TICKER_REUSE_RULE",), table=table) if sym else []
                note = f"; table rule {rule[0].entry_id}: {rule[0].note}" if rule else ""
                reasons.append(_reason(HD.POSSIBLE_TICKER_REUSE, iid, run[0], f"{sym}: {len(run)} sessions without a bar{note}"[:600]))

        # spin-off indicated but not adjusted (A9)
        first, last = (bars[0].session, bars[-1].session) if bars else (None, None)
        indicated = [(e.effective_session, f"event {e.event_id}") for e in ev if e.type == "SPIN_OFF"]
        if bars:
            for s in sorted({sp.symbol for sp in inst.symbol_history}):
                for x in EE.entries(s, types=("SPIN_OFF",), table=table):
                    if x.session and first <= x.session <= last and inst.symbol_at(x.session) == s:
                        if EE.classifying(x):
                            indicated.append((x.session, f"table {x.entry_id}"))
                        else:
                            listed[x.entry_id] = {**x.to_json(), "matched_spin_off_window": {"instrument_id": iid}}
        by_session = {b.session: b for b in uniq}
        for s, why in sorted(set(indicated)):
            cur = by_session.get(s)
            prev = next((b for b in reversed(uniq) if b.session < s), None)
            adjusted = (cur is not None and prev is not None and not is_missing(cur.total_return_factor) and not is_missing(prev.total_return_factor)
                        and _rel_change(prev.total_return_factor, cur.total_return_factor) > HD.FACTOR_REL_TOLERANCE)
            if not adjusted:
                reasons.append(_reason(HD.SPIN_OFF_NOT_ADJUSTED, iid, s, f"{why}: total_return_factor unavailable or unchanged"))

    # ---- ticker-reuse collision: two instruments sharing a symbol at one session (A9 / section 5)
    lo = m.start_session if isinstance(m.start_session, str) else "0000-00-00"
    hi = m.end_session if isinstance(m.end_session, str) else "9999-99-99"
    spans = []
    for inst in instruments:
        for sp in inst.symbol_history:
            if inst.symbol_history_status == "UNKNOWN":
                spans.append((sp.symbol, lo, hi, inst.instrument_id))                 # A8: the single entry applies to every session
            else:
                spans.append((sp.symbol, sp.valid_from_session, sp.valid_to_session or hi, inst.instrument_id))
    for i, a in enumerate(spans):
        for b in spans[i + 1:]:
            if a[0] == b[0] and a[3] != b[3] and a[1] <= b[2] and b[1] <= a[2]:
                reasons.append(_reason(HD.TICKER_REUSE_COLLISION, None, max(a[1], b[1]), f"{a[0]}: {a[3]} and {b[3]}"))

    # ---- feeds (WARN)
    if any(f != "CONSOLIDATED" for f in feeds):
        reasons.append(_reason(HD.VOLUME_FEED_NOT_CONSOLIDATED, detail=",".join(sorted(feeds))))
    if any(s != "CONSOLIDATED" for s in sources):
        reasons.append(_reason(HD.PRICE_SOURCE_NOT_CONSOLIDATED, detail=",".join(sorted(sources))))

    order = {HD.BLOCK: 0, HD.WARN: 1}
    reasons.sort(key=lambda r: (order[r["severity"]], r["code"], r["instrument_id"] or "", r["session"] or "", r["detail"]))
    verdict = HD.BLOCK if any(r["severity"] == HD.BLOCK for r in reasons) else HD.WARN if reasons else HD.CLEAN
    record = {
        "record_version": RECORD_VERSION, "validator_version": VALIDATOR_VERSION, "package_version": HD.VERSION,
        "contract_version": m.contract_version, "contract_sha256": m.contract_sha256, "contract_base_sha256": m.contract_base_sha256,
        "dataset_id": m.dataset_id, "kind": m.kind, "adapter": m.adapter, "adapter_version": m.adapter_version,
        "content_sha256": m.content_sha256, "recomputed_content_sha256": recomputed,
        "licence_personal_research_use": m.licence.personal_research_use,
        "expected_events_table": {"version": EE.TABLE_VERSION, "entries": len(table), "classifying_status": EE.CLASSIFYING_STATUS},
        "calendar": {"version": CAL.CALENDAR_VERSION, "tzdata_available": tz_ok, "tzdata_version": tz_version, "crosscheck": cc.get("status"),
                     "crosscheck_checked": cc.get("checked")},
        "verdict": verdict, "reasons": reasons,
        "reason_counts": {c: sum(1 for r in reasons if r["code"] == c) for c in sorted({r["code"] for r in reasons})},
        "classified_jumps": sorted(classified, key=lambda c: (c["instrument_id"], c["session"])),
        "expected_events_listed": [listed[k] for k in sorted(listed)],
        "counts": {"instruments": len(instruments), "bars": n_bars, "events": len(events), "removed_rows": len(getattr(provider, "removed_rows", list)())},
        "adapter_notes": adapter_notes,
    }
    record["record_sha256"] = CN.sha256_bytes(CN.dumps(record))
    return record


def record_bytes(record: dict) -> bytes:
    """The record's canonical bytes (section 6.3 rules), as handed to MD-7."""
    return CN.dumps(record)
