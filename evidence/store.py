"""
evidence/store.py — append-only persistence for Stage 5.4 evidence packets (DESIGN_54 §7).

A packet, its items and its fetch attempts are written in ONE transaction; the tables carry no-update / no-delete triggers, so a
completed packet can never change. Readers open the database read-only.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import List, Optional

from database.research_packet_migrations import TABLES, run_research_packet_migrations
from fit import readonly as RO

PACKET_COLS = ("packet_id", "packet_hash", "ticker", "as_of_date", "created_at", "lookback_days", "engine_version", "rules_version", "status",
               "failure_code", "failure_detail", "request_json", "cause_json", "ladder_json", "strength", "strength_points", "strength_text",
               "missing_json", "next_events_json", "n_items", "n_fetches", "n_failed_fetches", "sec_requests", "runtime_s", "universe_note")
ITEM_COLS = ("item_id", "packet_id", "position", "ticker", "subject_ticker", "source_type", "source_name", "source_url", "source_identifier",
             "published_at", "retrieved_at", "evidence_date", "category", "kind", "text", "value_json", "epistemic", "direction", "rule_match",
             "ladder_rung", "ladder_status", "source_tier", "contradiction", "contradicts_item_id", "derived_from_json", "source_hash", "fetch_id",
             "role", "date_source")
FETCH_COLS = ("fetch_id", "packet_id", "position", "adapter", "purpose", "url", "host", "status", "http_status", "bytes", "content_hash",
              "started_at", "duration_ms", "error")
EXPL_COLS = ("explanation_id", "packet_id", "packet_hash", "model", "prompt_version", "input_hash", "status", "text", "guard_failures_json",
             "input_tokens", "output_tokens", "estimated_cost_usd", "created_at")
_JSON_COLS = {"request_json", "cause_json", "ladder_json", "missing_json", "next_events_json", "value_json", "derived_from_json", "guard_failures_json"}


def new_id() -> str:
    return uuid.uuid4().hex


def canonical_json(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


class EvidenceStore:
    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else RO.db_path()

    # ---------------------------------------------------------------- reads (read-only connection) ----------------------
    def _ro(self):
        conn = sqlite3.connect(f"file:{self.path.as_posix()}?mode=ro", uri=True, timeout=10.0)
        conn.row_factory = sqlite3.Row
        return conn

    def exists(self) -> bool:
        if not self.path.exists():
            return False
        conn = self._ro()
        try:
            names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        finally:
            conn.close()
        return set(TABLES) <= names

    def read(self, sql: str, args=()) -> List[dict]:
        if not self.exists():
            return []
        conn = self._ro()
        try:
            return [self._decode(dict(r)) for r in conn.execute(sql, args).fetchall()]
        finally:
            conn.close()

    @staticmethod
    def _decode(r: dict) -> dict:
        for k in list(r):
            if k in _JSON_COLS and isinstance(r[k], str):
                try:
                    r[k[:-5]] = json.loads(r[k])
                except ValueError:
                    r[k[:-5]] = None
                del r[k]
        return r

    def packets(self, limit: int = 50, ticker: Optional[str] = None) -> List[dict]:
        if ticker:
            return self.read("SELECT * FROM evidence_packets WHERE ticker = ? ORDER BY created_at DESC, packet_id LIMIT ?", (ticker.upper(), int(limit)))
        return self.read("SELECT * FROM evidence_packets ORDER BY created_at DESC, packet_id LIMIT ?", (int(limit),))

    def packet(self, packet_id: str) -> Optional[dict]:
        rows = self.read("SELECT * FROM evidence_packets WHERE packet_id = ?", (packet_id,))
        return rows[0] if rows else None

    def items(self, packet_id: str) -> List[dict]:
        return self.read("SELECT * FROM evidence_items WHERE packet_id = ? ORDER BY position", (packet_id,))

    def fetches(self, packet_id: str) -> List[dict]:
        return self.read("SELECT * FROM evidence_source_fetches WHERE packet_id = ? ORDER BY position", (packet_id,))

    def explanations(self, packet_id: str) -> List[dict]:
        return self.read("SELECT * FROM evidence_explanations WHERE packet_id = ? ORDER BY created_at", (packet_id,))

    # ---------------------------------------------------------------- writes (one transaction) -------------------------
    @contextmanager
    def write(self):
        conn = sqlite3.connect(str(self.path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA busy_timeout=10000;")
            conn.execute("PRAGMA foreign_keys=ON;")
            run_research_packet_migrations(conn)
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def _encode(row: dict, cols) -> tuple:
        out = []
        for c in cols:
            v = row.get(c)
            if c in _JSON_COLS:
                v = canonical_json(v if v is not None else ({} if c != "derived_from_json" else []))
            out.append(v)
        return tuple(out)

    def insert_packet(self, packet: dict, items: List[dict], fetches: List[dict]) -> str:
        """packet / items / fetches are the dicts produced by evidence.engine (ids already assigned). Returns the packet id."""
        with self.write() as c:
            c.execute(f"INSERT INTO evidence_packets ({', '.join(PACKET_COLS)}) VALUES ({', '.join('?' * len(PACKET_COLS))})", self._encode(packet, PACKET_COLS))
            for f in fetches:
                c.execute(f"INSERT INTO evidence_source_fetches ({', '.join(FETCH_COLS)}) VALUES ({', '.join('?' * len(FETCH_COLS))})", self._encode(f, FETCH_COLS))
            for it in items:
                c.execute(f"INSERT INTO evidence_items ({', '.join(ITEM_COLS)}) VALUES ({', '.join('?' * len(ITEM_COLS))})", self._encode(it, ITEM_COLS))
        return packet["packet_id"]

    def insert_explanation(self, row: dict) -> str:
        row = dict(row)
        row.setdefault("explanation_id", new_id())
        with self.write() as c:
            c.execute(f"INSERT INTO evidence_explanations ({', '.join(EXPL_COLS)}) VALUES ({', '.join('?' * len(EXPL_COLS))})", self._encode(row, EXPL_COLS))
        return row["explanation_id"]
