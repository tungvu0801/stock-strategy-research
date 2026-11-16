"""
MD-A E1 - LOCAL ONLY: the section 5 validation on the REAL MS-1 evidence archive (research-data/ms1-2/raw: manifest.jsonl + gzipped bodies),
through Ms1EvidenceProvider. Skipped where the archive is absent (CI).

It proves, in one run: the contract files match their pinned shas before use (A12); the adapter reads ONLY the raw archive (every file it
opens is under raw/, opened 'rb'; sqlite3.connect is made to fail, and nothing ending .db / -wal / -shm / -journal is opened); every raw byte
and every file's size and mtime under the MS-1 root are unchanged and no file or folder appears (no -wal / -shm, no __pycache__); the licence is
UNKNOWN, so the decision view and the execution channel refuse (no returns or signals); and the known >25 % jumps (BKNG, INTC, MRVL, NOW, SNDK,
SOXX) are reported as DAY_JUMP_UNCLASSIFIED - classifying them needs a corporate-action source; nothing is classified or changed.
"""
from __future__ import annotations

import builtins
import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

import historical_data as HD
import historical_data_fixtures as F
from historical_data import acceptance as AC
from historical_data import calendar as CAL
from historical_data import validate as VA
from historical_data.adapters.ms1_evidence import Ms1EvidenceProvider
from historical_data.provider import LicenceRefused

MS1_ROOT = Path(r"C:\Data Analyst Py\DA IDX\research-data\ms1-2")
RAW = MS1_ROOT / "raw"
CONTRACT_DIR = Path(__file__).resolve().parents[2] / "project-planning" / "md"
KNOWN_JUMPS = {"BKNG", "INTC", "MRVL", "NOW", "SNDK", "SOXX"}

pytestmark = pytest.mark.skipif(not (RAW / "manifest.jsonl").is_file(), reason="local only: the MS-1 evidence archive is not in this checkout (CI)")


def _listing(root: Path):
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*"))


def test_E01_the_real_ms1_archive_validates_read_only_and_reports_the_known_jumps_unclassified(monkeypatch, tmp_path):
    base = hashlib.sha256((CONTRACT_DIR / "HISTORICAL_DATA_CONTRACT_v1.md").read_bytes()).hexdigest()       # A12: before use
    amend = hashlib.sha256((CONTRACT_DIR / "HISTORICAL_DATA_CONTRACT_v2_AMENDMENT.md").read_bytes()).hexdigest()
    assert (base, amend) == (HD.CONTRACT_BASE_SHA256, HD.CONTRACT_SHA256)

    before, listing = F.tree_snapshot(MS1_ROOT, hash_under="raw"), _listing(MS1_ROOT)
    opened, real_open = [], builtins.open

    def spy(file, mode="r", *a, **k):
        opened.append((str(file), mode))
        return real_open(file, mode, *a, **k)

    def no_db(*a, **k):
        raise AssertionError("a database was opened")
    monkeypatch.setattr(sqlite3, "connect", no_db)
    monkeypatch.setattr(builtins, "open", spy)
    try:
        p = Ms1EvidenceProvider(RAW)
        rec = VA.validate(p)
        decision = AC.decide(rec, p.manifest(), instruments=p.instruments())
        first = p.instruments()[0].instrument_id
        last = p.manifest().end_session
        with pytest.raises(LicenceRefused):
            p.as_of(CAL.close_utc(last))
        with pytest.raises(LicenceRefused):
            p.execution_open(first, last, decision_session=CAL.previous_session(last))
    finally:
        monkeypatch.undo()

    # read only: the raw archive alone, bytes; nothing changed or added anywhere under the MS-1 root
    mine = [(f, m) for f, m in opened if Path(f).resolve().is_relative_to(MS1_ROOT.resolve())]
    assert all(m == "rb" and Path(f).resolve().is_relative_to(RAW.resolve()) for f, m in mine), [x for x in mine if not x[0].startswith(str(RAW))][:5]
    assert not [f for f, _ in opened if f.lower().endswith(F.DB_SUFFIXES)]
    n_entries = sum(1 for line in (RAW / "manifest.jsonl").read_text(encoding="utf-8").splitlines() if line.strip())
    assert len(mine) == 1 + n_entries
    assert F.tree_snapshot(MS1_ROOT, hash_under="raw") == before and _listing(MS1_ROOT) == listing

    # what the dataset is
    m = p.manifest()
    assert (m.kind, m.licence.personal_research_use, m.instrument_count, m.start_session) == ("HISTORICAL", "UNKNOWN", 94, "2026-01-02")
    assert m.volume_feed == m.price_source == "SINGLE_VENUE:IEX" and m.events_completeness == "NONE" and m.membership_basis == "FIXED_CURRENT_UNIVERSE"
    assert p.removed_rows() == [] and p.adapter_notes() == () and p.dropped_symbols() == []
    assert rec["adapter_notes"] == {"skipped_responses": 0, "skipped_response_symbols": [], "dropped_symbols": [], "removed_rows": 0}
    assert all(s.file_sha256 != s.body_sha256 for s in m.source_files if s.path.endswith(".json.gz"))   # QA L-4: both shas recorded
    assert len({len(p.bars(i.instrument_id)) for i in p.instruments()}) == 1                              # every symbol on every session (219/219)

    # the verdict: WARN, no BLOCK; the six known jumps UNCLASSIFIED, nothing classified
    jumps = [r for r in rec["reasons"] if r["code"] == "DAY_JUMP_UNCLASSIFIED"]
    jump_list = [[r["instrument_id"], r["session"], r["detail"]] for r in jumps]
    evidence = json.dumps({"ms1_day_jumps": jump_list, "record_sha256": rec["record_sha256"], "content_sha256": rec["content_sha256"]},
                          sort_keys=True, separators=(",", ":"))
    (tmp_path / "ms1_day_jumps.json").write_text(evidence + "\n", encoding="utf-8")   # one canonical JSON line, under tmp_path only
    print(evidence)
    assert rec["verdict"] == "WARN" and not [r for r in rec["reasons"] if r["severity"] == "BLOCK"], (rec["reasons"][:5], jump_list)
    assert {r["instrument_id"].split(":")[1] for r in jumps} == KNOWN_JUMPS, f"day jumps: {jump_list}"
    assert json.loads((tmp_path / "ms1_day_jumps.json").read_text(encoding="utf-8"))["ms1_day_jumps"] == jump_list and len(jump_list) >= 6
    assert rec["classified_jumps"] == []
    assert set(F.codes(rec)) == {"DAY_JUMP_UNCLASSIFIED", "VOLUME_FEED_NOT_CONSOLIDATED", "PRICE_SOURCE_NOT_CONSOLIDATED"}, rec["reason_counts"]
    assert rec["calendar"]["crosscheck"] == "AGREE" and rec["content_sha256"] == rec["recomputed_content_sha256"]
    assert decision["usable_for"] == "INTEGRITY_ONLY" and decision["reasons"] == ["LICENCE_NOT_CONFIRMED"]
