"""
forward_validation/run_admin.py - Wave 2 PR C (design 4.2, 4.3): the operator's forward-run command.

  python -m forward_validation.run_admin start --approved-by <name> --evidence-ref <dw2k id> --accept-blockers <ids> [--approval-ref <ref>]
      [--acknowledge-attention <codes>] [--acknowledge-invalid-days <dates> --reason <text> | --reason-file <path>]
      [--min-trades N --target-sessions N --target-events N]
  python -m forward_validation.run_admin end --reason <text>
  python -m forward_validation.run_admin status

A local command working from DATABASE ROWS ONLY: no stream, no broker, no network client, no HTTP route (T-C14). Every read is strict
(query_strict), so a schema error refuses - it never passes - and nothing is written unless every precondition holds. The run identity is the
SERVER's (its latest sample's stamps), never the CLI environment's; the equity is stored and never printed. Imports live inside the functions:
this module has no module-level statement but its main guard (design 14).
"""


def _drift_fields(cfg):
    """C-11 (F-16): the drift fields of a 6.10A config snapshot - all 8 flags, all 8 cadences, all 13 executor limits, universe.sha256 and
    .count, the 5 autorun keys, market_data_mode.configured and .resolved, calendar, every versions key, entry_controls.contract_hash and
    .gate_enabled and the broker label; not `app` (the database location) and not the constant session_policy text. A missing value reads None (drift)."""
    from forward_capture.cohort import CADENCES, EXECUTOR_LIMITS, FLAGS
    cfg = cfg if isinstance(cfg, dict) else {}
    out = {}
    for group, names in (("flags", FLAGS), ("cadences", CADENCES), ("executor_limits", EXECUTOR_LIMITS)):
        g = cfg.get(group) if isinstance(cfg.get(group), dict) else {}
        for n in names:
            out[f"{group}.{n}"] = g.get(n)
    uni = cfg.get("universe") if isinstance(cfg.get("universe"), dict) else {}
    out["universe.sha256"], out["universe.count"] = uni.get("sha256"), uni.get("count")
    auto = cfg.get("autorun") if isinstance(cfg.get("autorun"), dict) else {}
    for k in ("policy", "effective_policy", "scheduled_arm_enabled", "disarm_at_session_end", "arm_delay_minutes"):
        out[f"autorun.{k}"] = auto.get(k)
    md = cfg.get("market_data_mode") if isinstance(cfg.get("market_data_mode"), dict) else {}
    out["market_data_mode.configured"], out["market_data_mode.resolved"] = md.get("configured"), md.get("resolved")
    out["calendar"] = cfg.get("calendar")
    vers = cfg.get("versions") if isinstance(cfg.get("versions"), dict) else {}
    for k in sorted(vers):
        out[f"versions.{k}"] = vers[k]
    ec = cfg.get("entry_controls") if isinstance(cfg.get("entry_controls"), dict) else {}
    out["entry_controls.contract_hash"], out["entry_controls.gate_enabled"] = ec.get("contract_hash"), ec.get("gate_enabled")
    out["broker"] = cfg.get("broker")
    return out


def _capture_live(store, now, s_at, snap, lease_since=None):
    """C-06 (semantic 8: no capture flag is invented): the capture is live from EXISTING rows only, at any time of day. Returns the failing
    clauses as [{clause, text, first_evidence_at}]; [] = live. (a) the snapshot's flags.FORWARD_VALIDATION_ENABLED; (b) no setup_evaluations
    row above the capture watermark (forward_capture_state eval_watermark) evaluated before s.at - 600 s; (c) no DUPLICATE_INSTANCE,
    SINK_OVERFLOW or EVIDENCE_LOST fault in the last 600 s; (d) no unexpired stock_agent_instance lease whose owner differs from the
    snapshot's owner (with `lease_since`, the observer compares the latest snapshot at or after the lease's acquired_at, N-2). Strict reads:
    an error raises (never 'live')."""
    import json
    from datetime import timedelta
    from autorun import INSTANCE_LEASE
    from forward_validation.service import _iso, _parse
    out = []
    cfg = json.loads(snap["config_json"]) if isinstance(snap.get("config_json"), str) else (snap.get("config") or {})
    if (cfg.get("flags") or {}).get("FORWARD_VALIDATION_ENABLED") is not True:
        out.append({"clause": "a", "text": "the latest config snapshot does not show FORWARD_VALIDATION_ENABLED", "first_evidence_at": snap.get("at")})
    st = store.query_strict("SELECT value FROM forward_capture_state WHERE key = 'eval_watermark'")
    wm = int(json.loads(st[0]["value"]) or 0) if st and st[0]["value"] is not None else 0
    cut = _iso(_parse(s_at) - timedelta(seconds=600))
    old = store.query_strict("SELECT MIN(evaluated_at) AS m FROM setup_evaluations WHERE rowid > ? AND evaluated_at < ?", (wm, cut))
    if old and old[0]["m"]:
        out.append({"clause": "b", "text": f"capture backlog: a setup evaluation of {old[0]['m']} is above the capture watermark for more than 600 s",
                    "first_evidence_at": _iso(_parse(old[0]["m"]) + timedelta(seconds=600))})
    f = store.query_strict("SELECT MIN(at) AS a, group_concat(DISTINCT code) AS c FROM forward_capture_faults WHERE code IN ('DUPLICATE_INSTANCE', 'SINK_OVERFLOW', 'EVIDENCE_LOST') "
                           "AND at > ?", (_iso(now - timedelta(seconds=600)),))
    if f and f[0]["a"]:
        out.append({"clause": "c", "text": f"capture fault in the last 600 s: {f[0]['c']}", "first_evidence_at": f[0]["a"]})
    lease = store.query_strict("SELECT owner, acquired_at, expires_at FROM app_leases WHERE name = ? AND expires_at > ?", (INSTANCE_LEASE, _iso(now)))
    if lease:
        ref = snap
        if lease_since:
            rows = store.query_strict("SELECT owner, at FROM forward_capture_config_snapshots WHERE at >= ? ORDER BY at DESC, rowid DESC LIMIT 1", (lease[0]["acquired_at"],))
            ref = rows[0] if rows else None                                    # N-2: no snapshot of the new lease holder yet (deferred STARTUP): no mismatch yet
        if ref is not None and ref.get("owner") != lease[0]["owner"]:
            out.append({"clause": "d", "text": "the stock_agent_instance lease is held by another owner than the capturing process", "first_evidence_at": _iso(now)})
    return out


def _prior_days(store, now, start_date):
    """C-08 (semantic 4): (i) the earlier days that are not final - any OPEN row of an earlier date, today's OPEN row after the close, and any
    earlier market day with samples but no row; (ii) PI, the earlier dates whose row is FAILED or DEGRADED with EVIDENCE_UNREADABLE (v1 or v2);
    (iii) the pre-v2 days: final rows with classifier_version NULL that are not FAILED (informational only). The start day counts as an
    earlier day once its close has passed: it is then outside O(R), so it must be acknowledged like any other prior day (C-08, review MED-2)."""
    from forward_capture import health as FH
    rows = store.query_strict("SELECT trading_date, session_status, notes_code, classifier_version FROM forward_validation_sessions ORDER BY trading_date")
    w = FH.window(start_date)
    unfinal = [r["trading_date"] for r in rows if r["session_status"] == "OPEN" and (r["trading_date"] < start_date or (
        r["trading_date"] == start_date and w is not None and now > w[1]))]
    have = {r["trading_date"] for r in rows}
    today_prior = w is not None and now > w[1]                                    # after the start day's close: outside O(R) (C-16), so a prior day
    sampled = store.query_strict("SELECT DISTINCT trading_date FROM forward_validation_samples WHERE in_session = 1 AND trading_date <= ?", (start_date,))
    sampled = [r for r in sampled if r["trading_date"] < start_date or today_prior]
    unfinal += [r["trading_date"] for r in sampled if r["trading_date"] not in have and FH.window(r["trading_date"]) is not None]
    invalid, pre_v2 = [], []
    for r in rows:
        if r["trading_date"] > start_date or (r["trading_date"] == start_date and not today_prior) or r["session_status"] == "OPEN":
            continue
        notes = [c for c in (r["notes_code"] or "").split("|") if c]
        if r["session_status"] == "FAILED" or (r["session_status"] == "DEGRADED" and "EVIDENCE_UNREADABLE" in notes):
            invalid.append({"date": r["trading_date"], "session_status": r["session_status"], "notes": notes, "classifier_version": r["classifier_version"]})
        elif r["classifier_version"] is None:
            pre_v2.append(r["trading_date"])
    return {"unfinalized": sorted(set(unfinal)), "invalid": invalid, "pre_v2_days": pre_v2}


def _preconditions(store, now, opts):
    """C-01...C-12 (design 4.3): every start precondition from database rows (strict). Returns (errors, record): errors name each failing
    precondition; the record holds what a start writes."""
    import json
    import execution as X
    import forward_validation as FV
    from forward_capture import health as FH
    from forward_validation import CRITICAL_CODES, MIN_TRADES_FOR_EDGE, TARGET_EVENTS, TARGET_SESSIONS, frozen_hash
    from forward_validation import metrics as M
    from forward_validation.attention_signature import signature
    from forward_validation.service import _iso, _parse
    from setups.market_hours import session_date
    blockers = ("PF-18", "EX-13", "PF-08", "W2-N23", "W2-N03")                   # C-09: run_admin's open Paper Auto blocker ids (function-local)
    errors = []
    if store.active_run() is not None:
        errors.append("C-01: a forward run is already active")
    smp = store.query_strict("SELECT * FROM forward_validation_samples ORDER BY at DESC, rowid DESC LIMIT 1")
    s = smp[0] if smp else None
    snaps = store.query_strict("SELECT * FROM forward_capture_config_snapshots ORDER BY at DESC, rowid DESC LIMIT 1")
    snap = snaps[0] if snaps else None
    if s is None:
        errors.append("C-03: no observer sample")
        return errors, None
    if snap is None:
        errors.append("C-11: no 6.10A config snapshot")
        return errors, None
    if s.get("rr_intents") is None or s.get("worker_stale_after_s") is None or s.get("frozen_hash") is None:
        errors.append("C-02: the latest sample was not written by PR B and PR C code (rr_intents / worker_stale_after_s / frozen_hash missing)")
    age = (now - _parse(s["at"])).total_seconds()
    if age < 0:
        errors.append(f"C-03: the latest sample is in the future (age {age:.0f} s): the clock was set back")
    elif age > 600:
        errors.append(f"C-03: the latest sample is {age:.0f} s old (> 600 s): the server is not observing")
    newest = store.query_strict("SELECT MAX(m) AS m FROM (SELECT MAX(at) AS m FROM forward_validation_samples UNION ALL SELECT MAX(at) FROM execution_mode_events "
                                "UNION ALL SELECT MAX(at) FROM forward_capture_config_snapshots)")
    if newest and newest[0]["m"] and _iso(now) < newest[0]["m"]:
        errors.append(f"C-03: a recorded event ({newest[0]['m']}) is newer than the start time: no back-dating")
    if s.get("frozen_hash") is not None and frozen_hash() != s["frozen_hash"]:
        errors.append("C-03: CLI environment differs from the server (frozen_hash)")
    if s.get("account_status") != "FRESH" or s.get("equity") is None:
        errors.append("C-04: the latest sample's account data is not FRESH")
    pos = store.query_strict("SELECT count(*) AS n FROM paper_positions WHERE state != 'CLOSED'")[0]["n"]
    its = store.query_strict(f"SELECT count(*) AS n FROM execution_intents WHERE state IN ({', '.join('?' * len(X.INTENT_OPEN))})", tuple(X.INTENT_OPEN))[0]["n"]
    if pos or its:
        errors.append(f"C-05: {pos} open bot position(s) and {its} open intent(s): a run starts flat")
    for c in _capture_live(store, now, s["at"], snap):
        errors.append(f"C-06 ({c['clause']}): {c['text']}")
    cfg = json.loads(snap["config_json"] or "{}")
    flags, cad = cfg.get("flags") or {}, cfg.get("cadences") or {}
    for f in ("FORWARD_VALIDATION_ENABLED", "FORWARD_VALIDATION_EARLY_STOP", "EXECUTION_SCHEDULER_ENABLED", "SETUP_EVALUATOR_ENABLED"):
        if flags.get(f) is not True:
            errors.append(f"C-11: flags.{f} is not true in the latest config snapshot")
    if cad.get("FORWARD_VALIDATION_INTERVAL_SECONDS") != M.H:
        errors.append(f"C-11: cadences.FORWARD_VALIDATION_INTERVAL_SECONDS is {cad.get('FORWARD_VALIDATION_INTERVAL_SECONDS')}, not {M.H}")
    idle = cad.get("FORWARD_VALIDATION_IDLE_SECONDS")
    if not isinstance(idle, (int, float)) or isinstance(idle, bool) or idle > M.G:
        errors.append(f"C-11: cadences.FORWARD_VALIDATION_IDLE_SECONDS is {idle}, not <= {M.G}")
    drift = _drift_fields(cfg)
    missing = sorted(k for k, v in drift.items() if v is None)
    if missing:
        errors.append(f"C-11: drift field(s) null or missing at start: {', '.join(missing)}")
    def counted(r) -> bool:                                                         # Wave 2 PR C (L-2, Option B): W2-6's predicate (C-65), AST-identical to the service's
        if r["kind"] != "MULTIPLE_ORDERS":
            return True
        try:
            d = json.loads(r["detail_json"] or "{}")
        except ValueError:
            return True
        if not isinstance(d, dict) or type(d.get("bot_orders")) is not int or type(d.get("bot_exposed")) is not bool or d["bot_orders"] < 0:
            return True                                                             # a legacy row, or ownership the row does not establish: counted (L-b)
        return d["bot_orders"] >= 2 or d["bot_exposed"]
    manual = sorted(int(r["rid"]) for r in store.query_strict("SELECT rowid AS rid, kind, detail_json FROM execution_reconciliations WHERE kind = 'MULTIPLE_ORDERS'")
                    if not counted(r))                                              # strict: an unreadable ownership read refuses the start
    strict = store.strict_view()
    critical, recorded = {}, {}
    for c in sorted(CRITICAL_CODES):
        el = signature(strict, c)                                                   # C-07: database rows only, through a strict store (a schema error raises)
        if el:                                                                      # the whole sigma is RECORDED (run_ack: the early stop as before L-2) ...
            recorded[c] = sorted([list(e) for e in el], key=lambda e: json.dumps(e, default=str))
        named = frozenset(e for e in el if not (c == "DUPLICATE_BROKER_ORDER" and len(e) == 3 and e[0] == "REC" and e[1] == "execution_reconciliations"
                                                and e[2] in set(manual)))
        if named:                                                                   # ... but only W2-6-counted evidence must be NAMED, as attention() shows it
            critical[c] = sorted([list(e) for e in named], key=lambda e: json.dumps(e, default=str))
    ack = sorted(set(opts.get("acknowledge_attention") or []))
    if sorted(critical) != ack:
        errors.append(f"C-07: critical attention {sorted(critical)} must be named exactly by --acknowledge-attention (given {ack})")
    start_date = (session_date(now) or now.date()).isoformat()
    prior = _prior_days(store, now, start_date)
    if prior["unfinalized"]:
        errors.append(f"C-08 (i): an earlier market day is not final: {', '.join(prior['unfinalized'])}")
    pi = [d["date"] for d in prior["invalid"]]
    given = sorted(set(opts.get("acknowledge_invalid_days") or []))
    reason = (opts.get("reason") or "").strip()
    if pi and (given != pi or not reason):
        errors.append(f"C-08 (ii): the invalid earlier days {pi} must be acknowledged exactly by --acknowledge-invalid-days, with --reason "
                      f"(given {given}{', no reason' if not reason else ''})")
    if not pi and given:
        errors.append(f"C-08 (ii): --acknowledge-invalid-days names {given}, but no earlier day is invalid")
    acc = set(opts.get("accept_blockers") or [])
    if acc != set(blockers):
        errors.append(f"C-09: --accept-blockers must name exactly {list(blockers)} (missing {sorted(set(blockers) - acc)}, unknown {sorted(acc - set(blockers))})")
    if not (opts.get("evidence_ref") or "").strip():
        errors.append("C-12: --evidence-ref (the D-W2-k inspection id) is required")
    if not (opts.get("approved_by") or "").strip():
        errors.append("C-12: --approved-by is required")
    explicit = [opts.get(k) for k in ("min_trades", "target_sessions", "target_events")]
    if any(v is not None for v in explicit) and not all(v is not None and int(v) >= 1 for v in explicit):
        errors.append("C-10: --min-trades, --target-sessions and --target-events are given together, each >= 1")
    from datetime import timedelta
    first = None
    day0 = session_date(now) or now.date()
    for i in range(15):                                                              # the first market day whose window opens at or after the start
        day = (day0 + timedelta(days=i)).isoformat()
        w = FH.window(day)
        if w is not None and w[0] >= now:
            first = day
            break
    if first is None:
        errors.append("C-03: no market day opens within 15 days of the start")
    if errors:
        return errors, None
    source = "CLI_FLAGS" if explicit[0] is not None else "CODE_DEFAULTS"
    mins = [int(v) for v in explicit] if source == "CLI_FLAGS" else [MIN_TRADES_FOR_EDGE, TARGET_SESSIONS, TARGET_EVENTS]
    n = len(store.runs()) + 1
    record = {"run_id": f"FR-{n:04d}", "created_at": _iso(now), "started_at": _iso(now), "first_session_date": first, "frozen_hash": s["frozen_hash"],
              "frozen_controls_json": json.dumps(FV.frozen_controls(), sort_keys=True, default=str), "cohort_id": snap["cohort_id"], "config_hash": snap["config_hash"],
              "execution_version": X.VERSION, "classifier_version": M.CLASSIFIER_VERSION, "min_trades": mins[0], "target_sessions": mins[1], "target_events": mins[2],
              "minimums_source": source, "base_equity": str(s["equity"]), "approved_by": opts["approved_by"].strip(), "approval_ref": opts.get("approval_ref"),
              "evidence_ref": opts["evidence_ref"].strip(), "accepted_blockers_json": json.dumps(sorted(acc)),
              "acknowledged_attention_json": json.dumps(recorded, sort_keys=True, default=str), "server_config_json": json.dumps(drift, sort_keys=True, default=str),
              "prior_invalid_days_json": json.dumps(prior["invalid"], sort_keys=True), "_prior": prior, "_reason": reason, "_manual": manual}
    return errors, record


def start(store, now, opts):
    """C-01...C-12: write the run row, its STARTED event and (when PI is non-empty) one PRIOR_INVALID_ACKNOWLEDGED event, in one transaction,
    only when every precondition holds. The acknowledgement changes no session row, no 6.10A record and no all-time block (C-36)."""
    errors, rec = _preconditions(store, now, opts)
    if errors:
        return {"ok": False, "errors": errors}
    prior, reason, manual = rec.pop("_prior"), rec.pop("_reason"), rec.pop("_manual")
    at = rec["started_at"]
    events = [{"at": at, "kind": "STARTED", "detail": {"approved_by": rec["approved_by"], "approval_ref": rec["approval_ref"], "evidence_ref": rec["evidence_ref"],
                                                      "accepted_blockers": sorted(opts.get("accept_blockers") or []), "pre_v2_days": prior["pre_v2_days"],
                                                      "minimums_source": rec["minimums_source"], "duplicate_rows_not_counted": manual}}]   # L-2: recorded, not named
    if prior["invalid"]:
        events.append({"at": at, "kind": "PRIOR_INVALID_ACKNOWLEDGED", "detail": {"operator": rec["approved_by"], "dates": [d["date"] for d in prior["invalid"]],
                                                                                  "reason": reason, "pre_v2_days": prior["pre_v2_days"]}})
    run = store.insert_run(rec, events)
    return {"ok": True, "run": {k: v for k, v in run.items() if k != "base_equity"}}


def end(store, now, reason):
    """Ends the active run (ended_at set once, an ENDED event). A run never resumes; a new run is a new id."""
    run = store.active_run()
    if run is None:
        return {"ok": False, "errors": ["no active forward run"]}
    if not (reason or "").strip():
        return {"ok": False, "errors": ["--reason is required"]}
    out = store.end_run(run["run_id"], now, reason.strip())
    return {"ok": True, "run": {k: v for k, v in out.items() if k != "base_equity"}}


def status(store, now):
    """The active run (never its base equity) and its events, or 'no forward run'."""
    run = store.active_run()
    if run is None:
        last = store.runs()
        return {"ok": True, "status": "no forward run", "last_run": ({k: v for k, v in last[-1].items() if k != "base_equity"} if last else None)}
    return {"ok": True, "status": "active", "run": {k: v for k, v in run.items() if k != "base_equity"}, "events": store.run_events(run["run_id"])}


def main(argv=None):
    """The CLI. Exit 0 = done; 1 = refused (each failing precondition printed); 2 = usage or a read error. Prints JSON, never the equity."""
    import argparse
    import json
    import sys
    from datetime import datetime, timezone
    from forward_validation.store import ForwardValidationStore
    p = argparse.ArgumentParser(prog="python -m forward_validation.run_admin", description="Wave 2 PR C: the forward run (start / end / status)")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("start")
    s.add_argument("--approved-by", required=True)
    s.add_argument("--evidence-ref", default="")
    s.add_argument("--accept-blockers", default="")
    s.add_argument("--approval-ref", default=None)
    s.add_argument("--acknowledge-attention", default="")
    s.add_argument("--acknowledge-invalid-days", default="")
    s.add_argument("--reason", default="")
    s.add_argument("--reason-file", default=None)
    s.add_argument("--min-trades", type=int, default=None)
    s.add_argument("--target-sessions", type=int, default=None)
    s.add_argument("--target-events", type=int, default=None)
    e = sub.add_parser("end")
    e.add_argument("--reason", required=True)
    sub.add_parser("status")
    a = p.parse_args(argv)
    store = ForwardValidationStore()
    now = datetime.now(timezone.utc)
    split = lambda v: [x.strip() for x in (v or "").split(",") if x.strip()]  # noqa: E731
    try:
        if a.cmd == "start":
            reason = a.reason
            if a.reason_file:
                with open(a.reason_file, encoding="utf-8") as fh:
                    reason = fh.read()
            out = start(store, now, {"approved_by": a.approved_by, "evidence_ref": a.evidence_ref, "accept_blockers": split(a.accept_blockers), "approval_ref": a.approval_ref,
                                     "acknowledge_attention": split(a.acknowledge_attention), "acknowledge_invalid_days": split(a.acknowledge_invalid_days), "reason": reason,
                                     "min_trades": a.min_trades, "target_sessions": a.target_sessions, "target_events": a.target_events})
        elif a.cmd == "end":
            out = end(store, now, a.reason)
        else:
            out = status(store, now)
    except Exception as exc:  # noqa: BLE001 - a strict read error refuses; it never passes
        print(json.dumps({"ok": False, "errors": [f"read error: {type(exc).__name__}: {exc}"[:300]]}))
        return 2
    print(json.dumps(out, indent=1, sort_keys=True, default=str))
    sys.stdout.flush()
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
