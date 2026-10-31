"""
evidence_refresh/journal.py - the durable record of refresh attempts that left NO packet behind, and of the run circuit (Stage 5.4.2).

A FAILED build is durable by itself: the engine stores a FAILED packet and policy.symbol_state backs off from it. An exception raised
before any packet is stored (the adapter factory, the engine's post-processing, a locked or full database during the insert) leaves
nothing in the store, so without this record the next run - 15 minutes later, or right after a restart - would try the same symbol
again. Each such attempt is recorded here and counts exactly like a FAILED packet: the same retry wait, the same per-symbol daily cap
and the same daily build budget (policy.plan). A run stopped by consecutive errors or by a refusing source opens a circuit: no build at
all until it closes (retry_hours later).

This is operational state, never evidence: a small JSON file in the git-ignored runtime/ directory (where account_refresh.fault and
cash_flow.fault live). The evidence tables are untouched (append-only, no schema change). Only the lease holder writes it, atomically (temp file + os.replace). Reading a missing file is an empty journal and
creates nothing. An unreadable file reads as empty and is reported; the in-memory copy still bounds retries for the life of the
process, and the next write replaces the file.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional

log = logging.getLogger(__name__)

JOURNAL_VERSION = "evidence_refresh_journal_v1"
JOURNAL_FILE = "evidence_refresh_journal.json"
KEEP_HOURS = 48                    # longer than every window that reads it (24 h cap, retry wait, circuit)
MAX_ENTRIES = 500


def _utc(d: datetime) -> datetime:
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _iso(d: datetime) -> str:
    return _utc(d).astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_at(s) -> Optional[datetime]:
    try:
        return _utc(datetime.fromisoformat(str(s or "").replace("Z", "+00:00")))
    except ValueError:
        return None


def default_path() -> Path:
    """runtime/evidence_refresh_journal.json under the app root (git-ignored), like the other operational state files."""
    import config
    return Path(config.PROJECT_ROOT) / "runtime" / JOURNAL_FILE


class AttemptJournal:
    """path_fn() -> the journal file (None: memory only). Reading never creates the file; only record_error / open_circuit write."""

    def __init__(self, path_fn: Optional[Callable[[], Optional[Path]]] = None, *, in_memory: bool = False):
        # default: default_path(), looked up at use time; in_memory=True: no file at all
        self._path_fn = (lambda: None) if in_memory else (path_fn or (lambda: default_path()))
        self._lock = threading.Lock()
        self._mem: Dict[str, dict] = {}
        self._circuit: Optional[dict] = None
        self.read_error: Optional[str] = None
        self.write_error: Optional[str] = None

    @staticmethod
    def memory() -> "AttemptJournal":
        return AttemptJournal(in_memory=True)

    def path(self) -> Optional[Path]:
        try:
            p = self._path_fn()
        except Exception as exc:  # noqa: BLE001 - no path: memory only, reported
            self.read_error = f"path {type(exc).__name__}"
            return None
        return Path(p) if p else None

    # ---------------------------------------------------------------------------------------------------- read
    def _read_file(self) -> dict:
        p = self.path()
        if p is None or not p.exists():
            return {"errors": [], "circuit": None}
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            if not isinstance(d, dict) or d.get("version") != JOURNAL_VERSION or not isinstance(d.get("errors"), list):
                raise ValueError("unexpected journal content")
            self.read_error = None
            errs = [e for e in d["errors"] if isinstance(e, dict) and isinstance(e.get("symbol"), str) and parse_at(e.get("at"))]
            c = d.get("circuit")
            return {"errors": errs, "circuit": c if isinstance(c, dict) and parse_at(c.get("until")) else None}
        except Exception as exc:  # noqa: BLE001 - unreadable: empty, reported; memory still bounds this process
            self.read_error = type(exc).__name__
            log.warning("evidence refresh journal unreadable (%s); using the in-memory copy", type(exc).__name__)
            return {"errors": [], "circuit": None}

    @staticmethod
    def _key(e: dict) -> str:
        return f"{e.get('symbol')}|{e.get('at')}|{e.get('error')}"

    def _merged(self, now: datetime) -> dict:
        f = self._read_file()
        errs = {self._key(e): e for e in f["errors"]}
        errs.update(self._mem)
        floor = _utc(now) - timedelta(hours=KEEP_HOURS)
        kept = sorted((e for e in errs.values() if parse_at(e["at"]) > floor), key=lambda e: (e["at"], e["symbol"]))[-MAX_ENTRIES:]
        circuits = [c for c in (f["circuit"], self._circuit) if c]
        circuit = max(circuits, key=lambda c: c["until"]) if circuits else None
        if circuit and parse_at(circuit["until"]) <= floor:
            circuit = None
        return {"errors": kept, "circuit": circuit}

    def state(self, now: datetime) -> dict:
        """{"errors": [{"symbol", "at", "error", "stage"}...] within KEEP_HOURS, "circuit": {"at", "until", "reason"} or None}."""
        with self._lock:
            return self._merged(now)

    def errors_by_symbol(self, now: datetime) -> Dict[str, List[dict]]:
        out: Dict[str, List[dict]] = {}
        for e in self.state(now)["errors"]:
            out.setdefault(e["symbol"].upper(), []).append(e)
        return out

    # ---------------------------------------------------------------------------------------------------- write (lease holder only)
    def _write(self, now: datetime) -> bool:
        p = self.path()
        if p is None:
            return False
        d = self._merged(now)
        body = {"version": JOURNAL_VERSION, "updated_at": _iso(now), "errors": d["errors"], "circuit": d["circuit"],
                "note": "Stage 5.4.2 evidence refresh: attempts that stored no packet, and the run circuit. Operational state, not evidence."}
        tmp = p.with_name(p.name + ".tmp")
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(body, sort_keys=True, indent=1), encoding="utf-8")
            os.replace(tmp, p)
            self.write_error = None
            return True
        except Exception as exc:  # noqa: BLE001 - never crash a run; the in-memory copy still bounds this process
            self.write_error = type(exc).__name__
            log.warning("evidence refresh journal not written (%s); retries stay bounded in memory", type(exc).__name__)
            return False

    def record_error(self, symbol: str, at: datetime, error: str, stage: str = "build") -> bool:
        e = {"symbol": symbol.upper(), "at": _iso(at), "error": str(error)[:120], "stage": stage}
        with self._lock:
            self._mem[self._key(e)] = e
            return self._write(at)

    def open_circuit(self, at: datetime, until: datetime, reason: str) -> bool:
        with self._lock:
            self._circuit = {"at": _iso(at), "until": _iso(until), "reason": reason}
            return self._write(at)

    def describe(self, now: datetime) -> dict:
        s = self.state(now)
        p = self.path()
        return {"path": str(p) if p else None, "errors_kept": len(s["errors"]), "circuit": s["circuit"], "read_error": self.read_error,
                "write_error": self.write_error}
