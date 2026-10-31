"""
api/routes/ops_status.py — compact operational status (Stage 2.7G.1). Read-only; no external calls, no Claude.

GET /api/ops/status   macro-event calendar state (warm-up / cache), market-data freshness from the existing
                      in-memory cache, and AI usage vs the hourly/daily limits (same counting rule as the gating).

It never reports a healthy state it cannot verify: "Ready" needs cached data, market freshness needs a cached
scan, and Robinhood/research status are taken by the page from the report it already loaded (no extra
gateway call). If the macro calendar is not cached and no warm-up is running, a background warm-up is started
(rate-limited, 0 Claude calls).
"""
from datetime import datetime, timezone

from fastapi import APIRouter
from fastapi.responses import JSONResponse

import config

router = APIRouter(prefix="/api/ops", tags=["ops"])


@router.get("/status")
def get_ops_status() -> JSONResponse:
    from agents.usage_tracker import usage_tracker
    from insights import service
    from insights.daily_review import ai_usage_counts
    from insights.plain import market_freshness
    from insights.warmup import macro_status, start_event_warmup
    now = datetime.now(timezone.utc)
    macro = macro_status()
    if macro["state"] == "NOT_LOADED" and config.EVENT_WARMUP_ON_STARTUP and start_event_warmup():
        macro = macro_status()
    entry = service._cache.get("inputs")             # read-only peek at the existing market-scan cache
    if entry is None:
        market = {"label": "NOT_LOADED", "text": "Not loaded yet"}
    else:
        f = market_freshness(entry[1].fetched_at.isoformat(), now)
        market = {"label": f["label"], "text": f["label"].title(), "updated": f["updated"]}
    counts = ai_usage_counts(usage_tracker)
    return JSONResponse({
        "macro_events": macro, "market_data": market,
        "ai": {"calls_last_hour": counts["last_hour"], "hourly_limit": config.AI_MAX_CALLS_PER_HOUR,
               "calls_today": counts["today"], "daily_limit": config.AI_MAX_CALLS_PER_DAY},
        "checked_at": now.isoformat(timespec="seconds"),
    })


# >>> Stage 6.10D engine health visibility (DESIGN_610D_ENGINE_HEALTH.md). GET only, read only: the commit the running code was started from,
# read from .git without running git (a plain repo, a worktree whose .git is a 'gitdir:' file, a symbolic or detached HEAD, loose or packed refs),
# the process facts (pid, start time, interpreter, working directory) and the runner's PID file when it names this process. Nothing here touches
# the executor, the safety gate, Paper Auto, a broker or Robinhood, and nothing is cached across restarts: a tree checked out under a running
# process is reported as 'tree_now' next to the 'deployed' commit so an operator can see that a restart is needed.
import json
import os
import re
import sys
from pathlib import Path
from typing import Optional

ENGINE_VERSION = "engine_health_v1"
_HEX40 = re.compile(r"^[0-9a-f]{40}$")
_PROCESS_STARTED_AT = datetime.now(timezone.utc).isoformat(timespec="seconds")       # module import: the process start, to the second


def _read(path: Path) -> Optional[str]:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None


def git_head(root: Path) -> dict:
    """The commit the tree at `root` is checked out at: {"sha", "ref", "source"}; sha is None when it cannot be read (no .git, unreadable, or a
    ref no loose or packed entry resolves). source: plain | worktree | no_git | unreadable | ref_unresolved."""
    dot = Path(root) / ".git"
    if dot.is_dir():
        gitdir, source = dot, "plain"
    elif dot.is_file():
        m = re.match(r"gitdir:\s*(.+)", _read(dot) or "")
        if not m:
            return {"sha": None, "ref": None, "source": "unreadable"}
        gitdir = Path(m.group(1).strip())
        gitdir = gitdir if gitdir.is_absolute() else (Path(root) / gitdir).resolve()
        source = "worktree"
    else:
        return {"sha": None, "ref": None, "source": "no_git"}
    common = gitdir
    cd = _read(gitdir / "commondir")
    if cd:
        common = Path(cd) if Path(cd).is_absolute() else (gitdir / cd).resolve()
    head = _read(gitdir / "HEAD")
    if head is None:
        return {"sha": None, "ref": None, "source": "unreadable"}
    if head.startswith("ref:"):
        ref = head[4:].strip()
        sha = _read(common / ref) or _read(gitdir / ref)
        if not sha:
            for line in (_read(common / "packed-refs") or "").splitlines():
                parts = line.split()
                if len(parts) == 2 and parts[1] == ref:
                    sha = parts[0]
                    break
        ok = bool(sha) and bool(_HEX40.match(sha))
        return {"sha": sha if ok else None, "ref": ref, "source": source if ok else "ref_unresolved"}
    return {"sha": head if _HEX40.match(head) else None, "ref": None, "source": source if _HEX40.match(head) else "unreadable"}


def runner_pid_info(runtime_dir: Path) -> Optional[dict]:
    """scripts/run_stock_agent.py's PID file when it names THIS process (instance id, start time, host, port); None for a preview, a test or a
    foreign runner, so nothing is ever claimed on another process's behalf."""
    try:
        info = json.loads((Path(runtime_dir) / "stock_agent.pid").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(info, dict) or info.get("pid") != os.getpid():
        return None
    return {k: info.get(k) for k in ("instance_id", "started_at", "host", "port", "app_root")}


_DEPLOYED = git_head(Path.cwd())                                                    # the tree the running code was imported from


def engine_descriptor(root: Optional[Path] = None) -> dict:
    root = Path(root or Path.cwd()).resolve()
    tree_now = git_head(root)
    return {
        "version": ENGINE_VERSION, "read_only": True,
        "deployed": {**_DEPLOYED, "app_root": str(root)},
        "tree_now": tree_now,
        "tree_matches_running": bool(_DEPLOYED.get("sha")) and tree_now.get("sha") == _DEPLOYED.get("sha"),
        "process": {"pid": os.getpid(), "started_at": _PROCESS_STARTED_AT, "python": sys.executable, "cwd": str(Path.cwd())},
        "runner": runner_pid_info(root / "runtime"),
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


@router.get("/engine")
def get_engine() -> JSONResponse:
    return JSONResponse(engine_descriptor())
# <<< Stage 6.10D engine health visibility
