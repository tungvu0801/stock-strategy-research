"""
autorun/view.py - the Bot Activity block of the Stage 6.10A auto-run policy (display only, read only): the policy (distinct from the CURRENT
mode and from the mode the last cycle ran in), the latest preflight, this process's startup outcome and lease, and today's arm / disarm record.
No write, no broker call, no evaluation: the supervisor's memory when it runs in this process, else the persisted preflight.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

import config
from autorun import DISCLAIMER, POLICY_LABELS, VERSION
from autorun.policy import settings_from
from autorun.session import classify
from autorun.store import AutoRunStore

UTC = timezone.utc
POLICY_TEXT = {"MANUAL_ARM": "The bot starts in Monitor only after every start or restart; a person arms Paper Auto.",
               "SCHEDULED_ARM": "Paper Auto may be armed once per regular session after every startup check passes (explicitly enabled)."}


def activity_view(db_path: Optional[Path] = None, now: Optional[datetime] = None) -> Dict[str, Any]:
    now = now or datetime.now(UTC)
    s = settings_from(config)
    store = AutoRunStore(db_path) if db_path else AutoRunStore()
    from autorun.scheduler import current_service, status as sup_status
    from autorun.service import view_state
    svc = current_service()
    pf = getattr(svc, "last_preflight", None) if svc is not None else None
    if pf is None:
        pf = store.last_preflight()
    sess = classify(now)
    from forward_capture.capture import trading_date
    td = trading_date(now.astimezone(UTC).isoformat(timespec="seconds"))
    day = store.day(td) if td else None
    events = store.events(limit=5)
    lease = ((getattr(svc, "last_facts", None) or {}).get("lease")) if svc is not None else None
    eff = s["effective_policy"]
    return {"version": VERSION, "policy": eff, "label": POLICY_LABELS[eff].split(" · ")[0], "label_long": POLICY_LABELS[eff], "text": POLICY_TEXT[eff],
            "configured_policy": s["policy"], "scheduled_arm_enabled": s["scheduled_arm_enabled"], "disarm_at_session_end": s["disarm_at_session_end"],
            "problems": s["problems"], "session": {"class": sess["session"], "trading_date": sess["trading_date"], "et_time": sess["et_time"], "calendar": sess["calendar"]},
            "preflight": ({"status": pf.get("status"), "at": pf.get("at"), "blocking": pf.get("blocking") or [c["id"] for c in pf.get("checks") or [] if c.get("status") == "FAIL"],
                           "checks": [{"id": c.get("id"), "status": c.get("status"), "text": c.get("text")} for c in pf.get("checks") or []]} if pf else None),
            "startup": view_state(), "supervisor": sup_status(), "lease": lease,
            "today": {k: (day or {}).get(k) for k in ("first_armed_at", "first_armed_by", "last_disarmed_at", "last_disarmed_by", "last_disarm_code", "scheduled_arm_at",
                                                      "scheduled_arm_result")},
            "events": [{"at": e["at"], "kind": e["kind"], "code": e.get("code"), "actor": e.get("actor")} for e in events], "disclaimer": DISCLAIMER}
