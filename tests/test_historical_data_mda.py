"""
MD-A - the historical data contract package (historical_data/; contract v2 = HISTORICAL_DATA_CONTRACT_v1.md rev 2 + the v2 amendment).
SYNTHETIC data only: no network, no database, every output under tmp_path. Z01 checks the contract files committed under
historical_data/contract/ (it runs in CI and has no skip path); Z03 is local only (the planning copies live outside the repository).

  C01-C07 the authoritative calendar: DST, early closes, closures and coverage, next session, the market_intel cross-check, tzdata
  S01-S04 P4 status objects, strict types, the licence block, the instrument rules
  K01-K03 the canonical form and content_sha256 (P7, 6.3)
  L01-L06 look-ahead from the provider side: (a) future perturbation, (b) truncation, (c) event timing, (d) fill timing, (i) snapshot
          invariance; A6 UNAVAILABLE anchor, A11 view manifest, clipping, the licence and integrity gates, A1 sessions
  V00-V14 section 5 validation and the acceptance record / criteria
  T01     the expected-events table (A9: only VERIFIED classifies; the five contradictions UNRESOLVED)
  A01-A05 the MS-1 raw-archive adapter (reads only the archive, never a database) and the sip_daily_v1 adapter
  X01-X03 isolation; Z01-Z03 the contract versions and shas (A12)
"""
from __future__ import annotations

import ast
import builtins
import copy
import gc
import gzip
import hashlib
import importlib.metadata
import json
import sqlite3
import types
from dataclasses import replace
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

import pytest

import historical_data as HD
import historical_data_fixtures as F
from historical_data import acceptance as AC
from historical_data import calendar as CAL
from historical_data import canonical as CN
from historical_data import expected_events as EE
from historical_data import validate as VA
from historical_data.adapters import synthetic as SY
from historical_data.adapters.ms1_evidence import Ms1EvidenceProvider, SourceFormatError, SourceIntegrityError
from historical_data.adapters.sip_daily_v1 import SipDailyV1Provider
from historical_data.provider import ExecutionTimingError, HistoricalDataProvider, IntegrityOnly, LicenceRefused
from historical_data.schema import (VIEW_MANIFEST_FIELDS, BarInvalid, CorporateEvent, Gap, Instrument, Licence, SymbolSpan, licence_unknown)
from historical_data.status import EARNINGS_DATA_UNAVAILABLE, NO_BAR, Missing, unavailable, unknown

ROOT = Path(__file__).resolve().parents[1]
CONTRACT_DIR = ROOT / "historical_data" / "contract"                                      # the committed copies (MDA-FR-6): checked in CI
PLANNING_DIR = ROOT.parent / "project-planning" / "md"                                    # the approved originals: outside the repository (local only)
CONTRACT_FILES = ("HISTORICAL_DATA_CONTRACT_v1.md", "HISTORICAL_DATA_CONTRACT_v2_AMENDMENT.md")
UTC = timezone.utc
S = F.S


def Z(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


@pytest.fixture
def fresh_calendar():
    CAL.reset_caches()
    yield
    CAL.reset_caches()


def clean(**kw):
    a = F.series("AAA", S[:30], F.smooth(30))
    b = F.series("BBB", S[:30], F.smooth(30, 40.0))
    return F.provider(a, b, **kw)


def reasons(rec, code):
    return [r for r in rec["reasons"] if r["code"] == code]


def reachable_provider(root, limit=300_000) -> bool:
    seen, stack = set(), [root]
    while stack and len(seen) < limit:
        o = stack.pop()
        if id(o) in seen:
            continue
        seen.add(id(o))
        if isinstance(o, HistoricalDataProvider):
            return True
        if isinstance(o, (type, types.ModuleType, types.FunctionType, types.BuiltinFunctionType, str, bytes, int, float)):
            continue
        stack.extend(gc.get_referents(o))
    return False


# ============================================================================================================ the calendar (C)
def test_C01_session_times_are_dst_correct_new_york_hours_in_utc():
    assert CAL.session_times("2026-03-06") == (Z("2026-03-06T14:30:00Z"), Z("2026-03-06T21:00:00Z"))      # EST
    assert CAL.session_times("2026-03-09") == (Z("2026-03-09T13:30:00Z"), Z("2026-03-09T20:00:00Z"))      # EDT
    assert CAL.session_times("2016-11-04") == (Z("2016-11-04T13:30:00Z"), Z("2016-11-04T20:00:00Z"))
    assert CAL.session_times("2016-11-07") == (Z("2016-11-07T14:30:00Z"), Z("2016-11-07T21:00:00Z"))
    naive = datetime(2026, 3, 9, 9, 30, tzinfo=timezone(timedelta(hours=-5))).astimezone(UTC)            # control: a fixed UTC-5 conversion
    assert naive != CAL.open_utc("2026-03-09")
    assert all(t.tzinfo == UTC for t in CAL.session_times("2026-03-09"))


def test_C02_early_closes_end_at_13_new_york():
    assert CAL.close_utc("2026-11-27") == Z("2026-11-27T18:00:00Z") and CAL.close_utc("2016-11-25") == Z("2016-11-25T18:00:00Z")
    assert CAL.close_utc("2025-07-03") == Z("2025-07-03T17:00:00Z") and CAL.close_utc("2024-12-24") == Z("2024-12-24T18:00:00Z")
    from zoneinfo import ZoneInfo
    ny = ZoneInfo(CAL.ZONE)
    assert len(CAL.EARLY_CLOSES) == 23
    for s in sorted(CAL.EARLY_CLOSES):
        assert CAL.close_utc(s).astimezone(ny).time() == time(13, 0) and CAL.open_utc(s).astimezone(ny).time() == time(9, 30), s
    assert CAL.close_utc("2026-11-25") == Z("2026-11-25T21:00:00Z")                                     # control: an ordinary day


def test_C03_closures_weekends_and_dates_outside_the_coverage_are_refused():
    for s in ("2016-01-01", "2018-12-05", "2025-01-09", "2026-01-03", "2026-07-03"):
        with pytest.raises(CAL.NotASession):
            CAL.session_times(s)
        assert CAL.is_session(s) is False
    for s in ("2015-12-31", "2027-01-04"):
        with pytest.raises(CAL.CalendarNotCovered):
            CAL.session_times(s)
        with pytest.raises(CAL.CalendarNotCovered):
            CAL.is_session(s)
    assert CAL.SESSIONS[0] == "2016-01-04" and CAL.SESSIONS[-1] == "2026-12-31" and CAL.COVERAGE == ("2016-01-01", "2026-12-31")
    with pytest.raises(CAL.CalendarNotCovered):
        CAL.next_session("2026-12-31")
    with pytest.raises(CAL.CalendarNotCovered):
        CAL.sessions("2026-12-01", "2027-01-10")
    assert CAL.CalendarNotCovered.reason == "CALENDAR_NOT_COVERED"


def test_C04_next_session_is_the_next_calendar_session_and_the_counts_match():
    from market_intel import calendar_history as HIST
    assert CAL.next_session("2026-07-02") == "2026-07-06" and CAL.next_session("2025-01-08") == "2025-01-10"
    assert CAL.next_session("2026-02-13") == "2026-02-17" and CAL.previous_session("2026-04-06") == "2026-04-02"
    assert len(CAL.sessions("2025-01-01", "2025-12-31")) == 250 == HIST.SESSIONS_PER_YEAR[2025]
    assert CAL.sessions("2026-04-02", "2026-04-06") == ["2026-04-02", "2026-04-06"]
    with pytest.raises(ValueError):
        CAL.sessions("2026-04-06", "2026-04-02")
    assert CAL.last_session_closed_by(CAL.close_utc("2026-03-09")) == "2026-03-09"
    assert CAL.last_session_closed_by(CAL.close_utc("2026-03-09") - F.US) == "2026-03-06"


def test_C05_the_calendar_agrees_with_market_intel_for_2025_2026_and_a_disagreement_blocks():
    from market_intel import calendar as MIC
    r = CAL.crosscheck_market_intel()
    assert r == {"status": "AGREE", "range": ["2025-01-01", "2026-12-31"], "checked": 730, "disagreement_count": 0, "disagreements": []}

    class Fake:
        MARKET_DAY, NOT_COVERED = MIC.MARKET_DAY, MIC.NOT_COVERED
        market_day_status = staticmethod(MIC.market_day_status)

        @staticmethod
        def regular_minutes(d):
            return 390 if d.isoformat() == "2026-11-27" else MIC.regular_minutes(d)
    bad = CAL.crosscheck_market_intel(Fake)
    assert bad["status"] == "DISAGREE" and bad["disagreements"] == [{"date": "2026-11-27", "problem": "REGULAR_MINUTES", "historical_data": 210,
                                                                     "market_intel": 390}]
    p = clean()
    assert "CALENDAR_CROSSCHECK_DISAGREEMENT" in F.codes(VA.validate(p, calendar_crosscheck=bad), HD.BLOCK)
    assert "CALENDAR_CROSSCHECK_DISAGREEMENT" not in F.codes(VA.validate(p))


def test_C06_without_the_tzdata_package_times_are_refused_and_validation_blocks(monkeypatch, fresh_calendar):
    p = clean()
    assert CAL.tzdata_status()[0] is True
    monkeypatch.setattr(CAL, "_find_tzdata", lambda: None)
    CAL.reset_caches()
    assert CAL.tzdata_status()[0] is False
    with pytest.raises(CAL.TzdataUnavailable):
        CAL.session_times("2026-03-09")
    rec = VA.validate(p, calendar_crosscheck={"status": "AGREE", "checked": 730})
    assert F.codes(rec) == ["TZDATA_UNAVAILABLE"] and rec["verdict"] == "BLOCK" and rec["calendar"]["tzdata_available"] is False


def test_C07_tzdata_is_pinned_in_requirements_at_the_installed_version():
    pins = [line for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines() if line.startswith("tzdata")]
    assert len(pins) == 1 and pins[0].split("#")[0].strip() == "tzdata==2026.4" == f"tzdata=={importlib.metadata.version('tzdata')}"
    assert CAL.tzdata_status() == (True, "2026.4", "ok")


# ============================================================================================================ schema (S)
def test_S01_missing_values_are_status_objects_never_zero_false_or_empty():
    assert Missing("UNKNOWN", "NOT_IN_SOURCE").to_json() == {"status": "UNKNOWN", "reason": "NOT_IN_SOURCE"}
    for args in (("NONE", "X"), ("UNKNOWN", "lower"), ("UNKNOWN", ""), ("UNAVAILABLE", 0)):
        with pytest.raises(ValueError):
            Missing(*args)
    assert NO_BAR.to_json() == {"status": "UNAVAILABLE", "reason": "NO_BAR"}
    assert EARNINGS_DATA_UNAVAILABLE.to_json() == {"status": "UNAVAILABLE", "reason": "EARNINGS_DATA_UNAVAILABLE"}


def test_S02_strict_numbers_and_the_section_3_2_bar_rules():
    i = SY.instrument("AAA", S[0])
    for bad in (True, 0, -1.0, float("nan"), float("inf"), "10"):
        with pytest.raises(BarInvalid):
            SY.bar(i.instrument_id, S[0], 10.0, 10.5, 9.5, bad, 100.0)
    for o, h, lo, c, v in ((10.0, 11.0, 10.5, 10.0, 1.0), (10.0, 10.2, 9.0, 10.5, 1.0), (10.0, 11.0, 9.0, 10.0, -1.0), (10.0, 11.0, 9.0, 10.0, True)):
        with pytest.raises(BarInvalid):
            SY.bar(i.instrument_id, S[0], o, h, lo, c, v)
    with pytest.raises(ValueError):
        SY.bar(i.instrument_id, S[0], 10.0, 11.0, 9.0, 10.0, 1.0, split_factor=float("nan"))
    with pytest.raises(ValueError):
        SY.bar(i.instrument_id, S[0], 10.0, 11.0, 9.0, 10.0, 1.0, total_return_factor=0.0)
    with pytest.raises(TypeError):
        SY.bar(i.instrument_id, S[0], 10.0, 11.0, 9.0, 10.0, 1.0, split_factor=True)
    m = unavailable("RAW_OHL_NOT_ARCHIVED")
    b = SY.bar(i.instrument_id, S[0], m, m, m, 10.0, 1.0)                                           # open / high / low may be UNAVAILABLE (P3)
    assert b.raw_open == m and b.raw_close == 10.0 and isinstance(b.raw_volume, float)


def test_S03_the_licence_block_adapters_emit_unknown_and_confirmed_needs_the_owner_file():
    lu = licence_unknown()
    assert lu.to_json() == {"licence_ref": {"status": "UNKNOWN", "reason": "NO_LICENCE_FILE"}, "personal_research_use": "UNKNOWN", "redistribution": "NONE"}
    with pytest.raises(ValueError):
        Licence("owner-licence", "CONFIRMED")
    with pytest.raises(ValueError):
        Licence(unknown("X"), "UNKNOWN", licence_file_sha256="a" * 64)
    with pytest.raises(ValueError):
        Licence(unknown("X"), "UNKNOWN", redistribution="ALLOWED")
    ok = Licence("owner-licence", "CONFIRMED", licence_file_sha256="ab" * 32, confirmed_by="owner", confirmed_on="2026-11-16")
    assert ok.to_json()["licence_file_sha256"] == "ab" * 32
    for f in sorted((ROOT / "historical_data" / "adapters").glob("*.py")):
        src = f.read_text(encoding="utf-8")
        assert '"CONFIRMED"' not in src and "'CONFIRMED'" not in src, f.name                     # A10: no adapter can emit CONFIRMED


def test_S04_an_unknown_symbol_history_is_its_single_entry_for_every_session():
    with pytest.raises(ValueError):
        Instrument("X:1", (SymbolSpan("AAA", S[0]), SymbolSpan("BBB", S[5])), "UNKNOWN", "ETF", unknown("NOT_IN_SOURCE"))
    i = SY.instrument("AAA", S[5])
    assert i.symbol_at(S[0]) == i.symbol_at(S[40]) == "AAA" and i.sector == unknown("NOT_IN_SOURCE") and i.issuer_id == unknown("NOT_IN_SOURCE")
    k = SY.instrument("NEW", S[0], history=[SymbolSpan("OLD", S[0], S[9]), SymbolSpan("NEW", S[10])], status="KNOWN")
    assert (k.symbol_at(S[9]), k.symbol_at(S[10])) == ("OLD", "NEW")


# ============================================================================================================ the canonical form (K)
def test_K01_the_same_input_gives_byte_identical_output_in_any_row_order():
    a, b = F.build(F.spec()), F.build(F.spec())
    assert CN.dumps(a.canonical_document()) == CN.dumps(b.canonical_document()) and a.manifest().content_sha256 == b.manifest().content_sha256
    i1, b1 = F.series("AAA", S[:6], F.smooth(6))
    i2, b2 = F.series("BBB", S[:6], F.smooth(6, 40.0))
    p1 = F.provider((i1, b1), (i2, b2))
    p2 = SY.SyntheticProvider(instruments=[i2, i1], bars=list(reversed(b1 + b2)))
    assert p1.manifest().content_sha256 == p2.manifest().content_sha256
    last = b1[-1]
    p3 = F.provider((i1, b1[:-1] + [replace(last, raw_close=last.raw_close + 0.01, raw_high=(last.raw_close + 0.01) * 1.01)]), (i2, b2))
    assert p3.manifest().content_sha256 != p1.manifest().content_sha256


def test_K02_the_canonical_document_has_exactly_the_contract_form():
    inst, bars = F.series("AAA", S[:3], [0.30000000000000004, 0.31, 0.32])
    p = F.provider((inst, bars))
    doc = p.canonical_document()
    assert set(doc) == {"manifest", "instruments", "bars", "gaps", "events", "membership", "scheduled_events"}
    assert doc["scheduled_events"] == {"status": "UNAVAILABLE", "reason": "EARNINGS_DATA_UNAVAILABLE"}
    text = CN.dumps(doc).decode("utf-8")
    assert '"raw_close":0.30000000000000004' in text and text == json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    assert doc["bars"][0]["known_at"] == "2026-02-02T21:00:00Z" and all(b["known_at"].endswith("Z") for b in doc["bars"])
    assert doc["bars"][0]["source_ref"] == {"status": "UNAVAILABLE", "reason": "SYNTHETIC_ROW"}
    assert (doc["manifest"]["contract_version"], doc["manifest"]["contract_sha256"], doc["manifest"]["contract_base_sha256"]) == (
        "historical_data_contract_v2", HD.CONTRACT_SHA256, HD.CONTRACT_BASE_SHA256)
    with pytest.raises(ValueError):
        CN.dumps({"x": float("nan")})


def test_K03_content_sha256_covers_the_document_without_itself():
    p = F.build(F.spec())
    doc = p.canonical_document()
    assert "content_sha256" not in doc["manifest"] and p.manifest().content_sha256 == CN.sha256_bytes(CN.dumps(doc))
    assert p.manifest().to_json()["content_sha256"] == p.manifest().content_sha256


# ============================================================================================================ look-ahead, provider side (L)
TIMES = ([CAL.close_utc(S[k]) for k in (5, 20, 29, 30, 40, 45, 61)] + [CAL.close_utc(S[k]) - F.US for k in (20, 30, 41)]
         + [CAL.open_utc(S[40]), CAL.open_utc(S[25])])


def test_L01a_future_perturbation_changes_nothing_visible_at_or_before_t():
    sp = F.spec()
    full = F.build(sp)
    leaks_seen = 0
    for t in TIMES:
        pert_spec = F.perturb(sp, t)
        pert = F.build(pert_spec)
        assert F.differences(F.dump(full.as_of(t)), F.dump(pert.as_of(t)), rel=1e-12) == [], t
        if pert_spec != sp:                                                    # control 1: a stand-in with no known_at filter sees it
            assert F.differences(F.leaky_dump(full), F.leaky_dump(pert), rel=1e-12) != [], t
            leaks_seen += 1
    assert leaks_seen >= 8
    t = CAL.close_utc(S[30])                                                   # control 2: a change known at t IS visible
    sp2 = copy.deepcopy(sp)
    sp2["closes"]["BBB"][S[29]] *= 1.0625
    assert F.differences(F.dump(full.as_of(t)), F.dump(F.build(sp2).as_of(t)), rel=1e-12) != []


def test_L01b_decisions_up_to_t_are_identical_on_data_cut_at_t():
    sp = F.spec()
    full = F.build(sp)
    for t in TIMES:
        cutp = F.build(F.cut(sp, t), as_of=CAL.last_session_closed_by(t))
        assert F.differences(F.dump(full.as_of(t)), F.dump(cutp.as_of(t)), rel=1e-12) == [], t
    t = CAL.close_utc(S[30])                                                   # control: data cut one session too early differs
    early = F.build(F.cut(sp, CAL.close_utc(S[29])), as_of=S[29])
    assert F.differences(F.dump(full.as_of(t)), F.dump(early.as_of(t)), rel=1e-12) != []


def test_L01c_events_membership_and_scheduled_events_are_invisible_before_known_at():
    sp = F.spec()
    full = F.build(sp)
    aaa, bbb, ccc = F.iid(sp, "AAA"), F.iid(sp, "BBB"), F.iid(sp, "CCC")

    def ids(v):
        return [e.event_id for e in v.events()]
    k20 = CAL.close_utc(S[20])
    assert "E-SPLIT-AAA" not in ids(full.as_of(k20 - F.US)) and "E-SPLIT-AAA" in ids(full.as_of(k20))
    o40 = CAL.open_utc(S[40])                                                  # known_at UNKNOWN: from the effective session's open
    assert "E-DIV-BBB" not in ids(full.as_of(o40 - F.US)) and "E-DIV-BBB" in ids(full.as_of(o40))
    assert {e.event_id for e in full.events()} == {"E-SPLIT-AAA", "E-DIV-BBB"}                 # control: the full history has both
    k45 = CAL.close_utc(S[45])
    assert bbb in full.as_of(k45 - F.US).membership(F.U, S[50]).instrument_ids
    assert bbb not in full.as_of(k45).membership(F.U, S[50]).instrument_ids
    k25 = CAL.close_utc(S[25])
    v = full.as_of(k25 - F.US)
    assert ccc not in [i.instrument_id for i in v.instruments()] and ccc not in v.membership(F.U, S[25]).instrument_ids and v.bars(ccc) == []
    v = full.as_of(k25)
    assert ccc in [i.instrument_id for i in v.instruments()] and ccc in v.membership(F.U, S[25]).instrument_ids
    k15 = CAL.close_utc(S[15])
    assert full.as_of(k15 - F.US).scheduled_events(aaa) == [] and [s.event_id for s in full.as_of(k15).scheduled_events(aaa)] == ["SCH-AAA"]
    ms = full.as_of(k45).membership(F.U, S[46])
    assert ms.membership_basis == "POINT_IN_TIME" and dict(ms.change_known_at)[aaa] == sp["changes"][0]["known_at"]


def test_L01d_fills_only_at_the_next_calendar_sessions_open_and_the_view_has_no_channel():
    sp = F.spec()
    full = F.build(sp)
    aaa, bbb = F.iid(sp, "AAA"), F.iid(sp, "BBB")
    assert full.execution_open(aaa, S[6], decision_session=S[5]) == full.bars(aaa, S[6], S[6])[0].raw_open
    for wrong in (S[5], S[7]):                                                 # never the signal bar, never later
        with pytest.raises(ExecutionTimingError):
            full.execution_open(aaa, wrong, decision_session=S[5])
    assert full.execution_open(aaa, S[10], decision_session=S[9]) is NO_BAR    # a NO_BAR gap -> NO_FILL_GAP
    assert full.execution_open(bbb, S[12], decision_session=S[11]) is NO_BAR   # a removed invalid bar
    assert S[9] == "2026-02-13" and S[10] == "2026-02-17"                     # over a weekend and Washington's Birthday
    assert full.execution_open(bbb, "2026-02-17", decision_session="2026-02-13") == full.bars(bbb, "2026-02-17", "2026-02-17")[0].raw_open
    assert full.execution_open(aaa, "2026-04-06", decision_session="2026-04-02") == full.bars(aaa, "2026-04-06", "2026-04-06")[0].raw_open  # Good Friday
    with pytest.raises(ExecutionTimingError):
        full.execution_open(bbb, "2026-02-16", decision_session="2026-02-13")  # the holiday is not the next session
    with pytest.raises(CAL.NotASession):
        full.execution_open(bbb, "2026-02-17", decision_session="2026-02-14")
    with pytest.raises(CAL.CalendarNotCovered):
        full.execution_open(aaa, "2027-01-04", decision_session="2026-12-31")
    with pytest.raises(TypeError):
        full.execution_open(aaa, S[6], S[5])                                    # decision_session is keyword-only (A2)
    v = full.as_of(CAL.close_utc(S[5]))
    assert not hasattr(v, "execution_open") and not hasattr(v, "__dict__") and v.bars(aaa)[-1].session == S[5]
    assert not reachable_provider(v)
    assert hasattr(full, "execution_open") and reachable_provider([v, full])   # control: the walk finds a provider when one is reachable


def test_L01i_scaling_the_snapshot_factors_changes_nothing_in_the_view():
    sp = F.spec()
    aaa = F.iid(sp, "AAA")
    t = CAL.close_utc(S[45])
    v = F.build(sp).as_of(t)
    base = F.dump(v)
    for c in (2.0, 0.5):                                                       # powers of two: exactly (A6)
        assert F.differences(base, F.dump(F.build(sp, scale={"AAA": c, "BBB": c, "CCC": c}).as_of(t)), rel=0.0) == [], c
    for c in (0.37, 1e6 + 1):                                                  # otherwise within 1e-12 relative (A6)
        assert F.differences(base, F.dump(F.build(sp, scale={"AAA": c, "BBB": c, "CCC": c}).as_of(t)), rel=1e-12) == [], c
    for i in v.instruments():
        last = v.bars(i.instrument_id)[-1]
        assert last.split_factor == 1.0 and last.total_return_factor == 1.0
    assert v.bars(aaa, S[29], S[29])[0].split_factor == 2.0 and v.bars(aaa, S[30], S[30])[0].split_factor == 1.0
    assert F.build(sp, scale={"AAA": 2.0}).bars(aaa)[0].split_factor == 2 * F.build(sp).bars(aaa)[0].split_factor   # control: full history changes


def test_L02_an_unavailable_last_visible_factor_makes_the_views_factors_unavailable():
    inst, bars = F.series("AAA", S[:5], F.smooth(5), sf=[2.0, 2.0, 1.0, 1.0, unavailable("SPLIT_RATIO_MISSING")])
    p = F.provider((inst, bars), price_views=("RAW_CLOSE", "RAW_OHL", "TOTAL_RETURN"))
    assert all(b.split_factor == unavailable("LAST_VISIBLE_FACTOR_UNAVAILABLE") for b in p.as_of(CAL.close_utc(S[4])).bars(inst.instrument_id))
    assert [b.split_factor for b in p.as_of(CAL.close_utc(S[3])).bars(inst.instrument_id)] == [2.0, 2.0, 1.0, 1.0]
    assert [b.total_return_factor for b in p.as_of(CAL.close_utc(S[4])).bars(inst.instrument_id)] == [1.0] * 5


def test_L03_the_views_manifest_is_the_a11_allow_list():
    v = F.build(F.spec()).as_of(CAL.close_utc(S[30]))
    m = v.manifest()
    assert sorted(m) == sorted(VIEW_MANIFEST_FIELDS) and len(m) == 13
    for k in ("end_session", "instrument_count", "content_sha256", "dataset_id", "adjustment_as_of", "retrieved_at", "source_files", "data_exposure",
              "contract_base_sha256"):
        assert k not in m, k
    assert m["start_session"] == S[0] and m["licence"]["personal_research_use"] == "UNKNOWN"


def test_L04_symbol_history_is_clipped_to_the_last_closed_session():
    inst = SY.instrument("NEW", S[0], history=[SymbolSpan("OLD", S[0], S[9]), SymbolSpan("NEW", S[10])], status="KNOWN")
    bars = [SY.bar(inst.instrument_id, d, c, c * 1.01, c * 0.99, c, 1000.0) for d, c in zip(S[:20], F.smooth(20))]
    p = SY.SyntheticProvider(instruments=[inst], bars=bars)
    assert p.as_of(CAL.close_utc(S[5])).instruments()[0].symbol_history == (SymbolSpan("OLD", S[0], None),)
    assert p.as_of(CAL.close_utc(S[15])).instruments()[0].symbol_history == (SymbolSpan("OLD", S[0], S[9]), SymbolSpan("NEW", S[10], None))


def test_L05_the_licence_and_integrity_gates_refuse_the_view_and_the_channel():
    sp = F.spec()
    aaa, t = F.iid(sp, "AAA"), CAL.close_utc(S[30])
    hist = F.build(sp, kind="HISTORICAL")
    with pytest.raises(LicenceRefused):
        hist.as_of(t)
    with pytest.raises(LicenceRefused):
        hist.execution_open(aaa, S[6], decision_session=S[5])
    conf = Licence("owner-licence", "CONFIRMED", licence_file_sha256="ab" * 32, confirmed_by="owner", confirmed_on="2026-11-16")
    for kind, lic in (("HISTORICAL", conf), ("SYNTHETIC", conf), ("HISTORICAL", Licence(unknown("NO_LICENCE_FILE"), "NOT_PERMITTED"))):
        with pytest.raises(ValueError, match="no dataset can be CONFIRMED"):
            F.build(sp, kind=kind, licence=lic)                                                     # QA L-1: nothing makes a dataset CONFIRMED in v2 (A10)
    stand_in = F.build(sp, kind="HISTORICAL")
    stand_in._manifest = replace(stand_in._manifest, licence=conf)                                 # a test-only stand-in, never reachable from code
    assert stand_in.as_of(t).decision_time == t                                                    # control: the gate that refuses `hist` is the licence
    io = F.build(sp, price_views=("RAW_CLOSE", "SPLIT_ADJUSTED", "TOTAL_RETURN"))
    with pytest.raises(IntegrityOnly):
        io.as_of(t)
    with pytest.raises(IntegrityOnly):
        io.execution_open(aaa, S[6], decision_session=S[5])
    with pytest.raises(TypeError):
        F.build(sp).as_of(datetime(2026, 3, 1))


def test_L06_sessions_and_next_session_come_from_the_calendar_a1():
    full = F.build(F.spec())
    v = full.as_of(CAL.close_utc(S[30]))
    assert full.sessions(S[0], S[4]) == v.sessions(S[0], S[4]) == list(S[:5]) and full.next_session("2026-04-02") == v.next_session("2026-04-02") == "2026-04-06"
    assert full.session_times(S[3]) == v.session_times(S[3]) == CAL.session_times(S[3])


# ============================================================================================================ validation (V)
def test_V00_a_clean_dataset_is_clean_and_the_record_is_deterministic():
    p = clean()
    rec = VA.validate(p)
    assert rec["verdict"] == "CLEAN" and rec["reasons"] == [] and rec["calendar"]["crosscheck"] == "AGREE"
    assert rec == VA.validate(p) and rec["content_sha256"] == rec["recomputed_content_sha256"] == p.manifest().content_sha256
    body = dict(rec)
    sha = body.pop("record_sha256")
    assert sha == CN.sha256_bytes(CN.dumps(body)) and json.loads(VA.record_bytes(rec)) == rec
    assert (rec["contract_version"], rec["contract_sha256"], rec["contract_base_sha256"]) == (HD.CONTRACT_VERSION, HD.CONTRACT_SHA256, HD.CONTRACT_BASE_SHA256)


def test_V01_a_duplicate_key_blocks():
    i, bars = F.series("AAA", S[:5], F.smooth(5))
    rec = VA.validate(F.provider((i, bars + [bars[2]])))
    assert [(r["code"], r["session"]) for r in rec["reasons"] if r["severity"] == "BLOCK"] == [("DUPLICATE_KEY", S[2])]


def test_V02_a_non_calendar_or_uncovered_session_blocks():
    inst = SY.instrument("AAA", "2026-01-02")
    rows = [SY.bar(inst.instrument_id, "2026-01-02", 10.0, 10.1, 9.9, 10.0, 1.0),
            SY.bar(inst.instrument_id, "2026-01-03", 10.0, 10.1, 9.9, 10.0, 1.0, known_at=Z("2026-01-03T21:00:00Z")),
            SY.bar(inst.instrument_id, "2015-12-31", 10.0, 10.1, 9.9, 10.0, 1.0, known_at=Z("2015-12-31T21:00:00Z"))]
    rec = VA.validate(SY.SyntheticProvider(instruments=[inst], bars=rows))
    assert {(r["code"], r["session"]) for r in rec["reasons"] if r["severity"] == "BLOCK"} == {("NON_CALENDAR_SESSION", "2026-01-03"),
                                                                                                ("CALENDAR_NOT_COVERED", "2015-12-31")}


def test_V03_known_at_must_be_close_utc_including_early_closes():
    i, bars = F.series("AAA", ["2026-11-25", "2026-11-27", "2026-11-30"], [10.0, 10.1, 10.2])
    assert bars[1].known_at == Z("2026-11-27T18:00:00Z")
    assert VA.validate(F.provider((i, bars)))["verdict"] == "CLEAN"
    bad = [bars[0], replace(bars[1], known_at=Z("2026-11-27T21:00:00Z")), replace(bars[2], known_at=bars[2].known_at + timedelta(hours=1))]
    assert [(r["code"], r["session"]) for r in VA.validate(F.provider((i, bad)))["reasons"]] == [("KNOWN_AT_MISMATCH", "2026-11-27"),
                                                                                                ("KNOWN_AT_MISMATCH", "2026-11-30")]


def test_V04_a_non_finite_or_non_positive_factor_smuggled_past_the_schema_blocks():
    p = clean()
    object.__setattr__(p.bars(p.instruments()[0].instrument_id)[3], "split_factor", -1.0)
    assert {"FACTOR_NOT_FINITE_OR_NONPOSITIVE", "MANIFEST_HASH_MISMATCH"} <= set(F.codes(VA.validate(p), "BLOCK"))
    q = clean()
    object.__setattr__(q.bars(q.instruments()[0].instrument_id)[3], "total_return_factor", float("nan"))
    rec = VA.validate(q)
    assert {"FACTOR_NOT_FINITE_OR_NONPOSITIVE", "MANIFEST_HASH_MISMATCH"} <= set(F.codes(rec, "BLOCK")) and rec["recomputed_content_sha256"] is None


def test_V05_factor_changes_block_under_complete_events_and_warn_as_inferred_splits_otherwise():
    sess = S[:6]
    sfs = [2.0, 2.0, 2.0, 1.0, 1.0, 1.0]
    i, bars = F.series("AAA", sess, [100.0, 101.0, 102.0, 51.5, 52.0, 52.5], sf=sfs, trf=sfs)
    none = VA.validate(F.provider((i, bars)))
    assert [(r["code"], r["session"]) for r in none["reasons"]] == [("DAY_JUMP_UNCLASSIFIED", sess[3]), ("INFERRED_SPLIT", sess[3])]
    comp = VA.validate(F.provider((i, bars), events_completeness="COMPLETE"))
    assert [r["detail"].split(" ")[0] for r in reasons(comp, "FACTOR_CHANGE_WITHOUT_EVENT")] == ["split_factor", "total_return_factor"]
    ev = CorporateEvent("E1", i.instrument_id, "SPLIT", sess[3], CAL.close_utc(sess[0]), ratio=2.0)
    ok = VA.validate(F.provider((i, bars), events_completeness="COMPLETE", events=[ev]))
    assert ok["verdict"] == "CLEAN" and ok["classified_jumps"][0]["event_ids"] == ["E1"]
    i2, b2 = F.series("BBB", S[:3], F.smooth(3), sf=[1.000009, 1.0, 1.0])
    assert VA.validate(F.provider((i2, b2), events_completeness="COMPLETE"))["verdict"] == "CLEAN"
    i3, b3 = F.series("CCC", S[:3], F.smooth(3), sf=[1.000011, 1.0, 1.0])
    assert F.codes(VA.validate(F.provider((i3, b3), events_completeness="COMPLETE"))) == ["FACTOR_CHANGE_WITHOUT_EVENT"]
    i4, b4 = F.series("DDD", S[:3], F.smooth(3), trf=[1.02, 1.0, 1.0])
    assert F.codes(VA.validate(F.provider((i4, b4), events_completeness="COMPLETE"))) == ["FACTOR_CHANGE_WITHOUT_EVENT"]
    div = CorporateEvent("D1", i4.instrument_id, "CASH_DIVIDEND", S[1], CAL.close_utc(S[0]), cash_amount=2.0)
    assert VA.validate(F.provider((i4, b4), events_completeness="COMPLETE", events=[div]))["verdict"] == "CLEAN"


def test_V06_ticker_reuse_blocks_only_when_two_instruments_share_a_symbol_at_one_session():
    a = F.series("SNDK", S[:5], F.smooth(5), prefix="P1")
    b = F.series("SNDK", S[10:15], F.smooth(5), prefix="P2")
    assert F.codes(VA.validate(F.provider(a, b)), "BLOCK") == ["TICKER_REUSE_COLLISION"]          # A8: UNKNOWN histories span every session
    known = lambda f, t: {"history": [SymbolSpan("XYZ", f, t)], "status": "KNOWN"}  # noqa: E731
    c = F.series("XYZ", S[:5], F.smooth(5), prefix="P1", inst_kw=known(S[0], S[4]))
    d = F.series("XYZ", S[10:15], F.smooth(5), prefix="P2", inst_kw=known(S[10], None))
    assert "TICKER_REUSE_COLLISION" not in F.codes(VA.validate(F.provider(c, d)))
    e = F.series("XYZ", S[3:8], F.smooth(5), prefix="P3", inst_kw=known(S[3], None))
    assert F.codes(VA.validate(F.provider(c, e)), "BLOCK") == ["TICKER_REUSE_COLLISION"]


def _with_gap(sym, missing, n=40):
    keep = [s for k, s in enumerate(S[:n]) if k not in set(missing)]
    return F.series(sym, keep, F.smooth(len(keep)))


def test_V07_gaps_warn_from_two_sessions_and_possible_ticker_reuse_over_twenty():
    one = _with_gap("AAA", [5])
    p = F.provider(one)
    assert p.gaps(one[0].instrument_id) == [Gap(one[0].instrument_id, S[5], "NO_BAR")] and VA.validate(p)["verdict"] == "CLEAN"
    assert F.codes(VA.validate(F.provider(_with_gap("AAA", [5, 6])))) == ["GAP_2_OR_MORE_SESSIONS"]
    assert F.codes(VA.validate(F.provider(_with_gap("AAA", range(5, 25))))) == ["GAP_2_OR_MORE_SESSIONS"]                     # 20 sessions
    assert F.codes(VA.validate(F.provider(_with_gap("AAA", range(5, 26))))) == ["GAP_2_OR_MORE_SESSIONS", "POSSIBLE_TICKER_REUSE"]  # 21
    rec = VA.validate(F.provider(_with_gap("SNDK", range(5, 26))))
    r = reasons(rec, "POSSIBLE_TICKER_REUSE")
    assert rec["verdict"] == "WARN" and len(r) == 1 and r[0]["severity"] == "WARN" and "EE-044" in r[0]["detail"]   # annotated, never upgraded


def _jump(sym, at, before=100.0, after=40.0, n=5):
    k = CAL.session_index(at)
    sess = list(CAL.SESSIONS[k - n:k + n])
    return F.series(sym, sess, [before] * n + [after] * n), at


def test_V08_day_jumps_over_25_percent_warn_unless_a_provider_event_or_a_verified_entry_matches():
    for after, expect in ((125.0, []), (125.001, ["DAY_JUMP_UNCLASSIFIED"]), (70.0, ["DAY_JUMP_UNCLASSIFIED"]), (75.0, [])):
        (pair, _at) = _jump("AAA", S[20], after=after)
        assert F.codes(VA.validate(F.provider(pair))) == expect, after
    (pair, at) = _jump("NVDA", "2024-06-10", before=1200.0, after=120.0)                           # EE-013 VERIFIED
    rec = VA.validate(F.provider(pair))
    assert rec["verdict"] == "CLEAN" and [(c["session"], c["table_entry_ids"]) for c in rec["classified_jumps"]] == [("2024-06-10", ["EE-013"])]
    (pair, at) = _jump("GE", "2021-08-02", before=10.0, after=80.0)                                 # EE-005 VERIFIED reverse split
    assert VA.validate(F.provider(pair))["classified_jumps"][0]["table_entry_ids"] == ["EE-005"]
    for sym, at, listed in (("HON", "2026-06-29", ["EE-019", "EE-033"]), ("T", "2022-04-11", ["EE-028"]), ("ISRG", "2021-10-05", ["EE-006"])):
        (pair, _) = _jump(sym, at)
        rec = VA.validate(F.provider(pair))
        assert [(r["code"], r["session"]) for r in rec["reasons"]] == [("DAY_JUMP_UNCLASSIFIED", at)], sym
        assert rec["classified_jumps"] == [] and [x["entry_id"] for x in rec["expected_events_listed"]] == listed, sym


def test_V09_a_spin_off_indicated_but_not_adjusted_warns():
    sess = CAL.sessions("2021-05-24", "2021-06-11")
    k = sess.index("2021-06-03")
    flat = F.series("MRK", sess, F.smooth(len(sess)))                                              # EE-026 VERIFIED
    assert [(r["code"], r["session"]) for r in VA.validate(F.provider(flat))["reasons"]] == [("SPIN_OFF_NOT_ADJUSTED", "2021-06-03")]
    adj = F.series("MRK", sess, F.smooth(len(sess)), trf=[1.05] * k + [1.0] * (len(sess) - k))
    assert VA.validate(F.provider(adj))["verdict"] == "CLEAN"
    i, _b = flat
    so = CorporateEvent("SO1", i.instrument_id, "SPIN_OFF", sess[k], CAL.close_utc(sess[0]), child_instrument_id="SYNTH:CHILD:2021-06-03", child_ratio=0.1)
    ev_flat = F.series("ZZZ", sess, F.smooth(len(sess)))
    so_z = replace(so, instrument_id=ev_flat[0].instrument_id)
    assert F.codes(VA.validate(F.provider(ev_flat, events=[so_z], events_completeness="COMPLETE"))) == ["SPIN_OFF_NOT_ADJUSTED"]
    ev_adj = F.series("ZZZ", sess, F.smooth(len(sess)), trf=[1.05] * k + [1.0] * (len(sess) - k))
    assert VA.validate(F.provider(ev_adj, events=[so_z], events_completeness="COMPLETE"))["verdict"] == "CLEAN"
    for sym, start, end, eid in (("T", "2022-04-01", "2022-04-22", "EE-028"), ("HON", "2026-06-17", "2026-07-10", "EE-033")):
        s2 = CAL.sessions(start, end)
        rec = VA.validate(F.provider(F.series(sym, s2, F.smooth(len(s2)))))
        assert "SPIN_OFF_NOT_ADJUSTED" not in F.codes(rec) and eid in [x["entry_id"] for x in rec["expected_events_listed"]], sym


def test_V10_a_single_venue_feed_warns():
    rec = VA.validate(clean(volume_feed="SINGLE_VENUE:IEX", price_source="SINGLE_VENUE:IEX"))
    assert F.codes(rec) == ["PRICE_SOURCE_NOT_CONSOLIDATED", "VOLUME_FEED_NOT_CONSOLIDATED"] and rec["verdict"] == "WARN"


def test_V11_a_declared_split_series_that_is_missing_blocks():
    m = unavailable("SPLIT_RATIO_MISSING")
    i, bars = F.series("AAA", S[:5], F.smooth(5), sf=[m] * 5)
    assert [(r["code"], r["session"]) for r in VA.validate(F.provider((i, bars)))["reasons"]] == [("SPLIT_SERIES_MISSING", None)]
    i, bars = F.series("AAA", S[:5], F.smooth(5), sf=[1.0, 1.0, m, 1.0, 1.0])
    assert [(r["code"], r["session"]) for r in VA.validate(F.provider((i, bars)))["reasons"]] == [("SPLIT_SERIES_MISSING", S[2])]
    i, bars = F.series("AAA", S[:5], F.smooth(5), sf=[m] * 5)
    assert VA.validate(F.provider((i, bars), price_views=("RAW_CLOSE", "RAW_OHL", "TOTAL_RETURN")))["verdict"] == "CLEAN"   # not declared


def test_V12_a_manifest_or_row_that_no_longer_matches_its_hash_blocks():
    p = clean()
    p._manifest = replace(p._manifest, instrument_count=99)
    assert F.codes(VA.validate(p)) == ["MANIFEST_HASH_MISMATCH"]
    q = clean()
    object.__setattr__(q.bars(q.instruments()[0].instrument_id)[0], "raw_volume", 7.0)
    assert F.codes(VA.validate(q)) == ["MANIFEST_HASH_MISMATCH"]


def test_V13_the_verdict_is_the_most_severe_reason_and_an_unsupported_version_is_refused():
    warn_and_block = F.provider(_with_gap("AAA", [5, 6]), F.series("SNDK", S[:5], F.smooth(5), prefix="P1"), F.series("SNDK", S[3:8], F.smooth(5), prefix="P2"))
    rec = VA.validate(warn_and_block)
    assert rec["verdict"] == "BLOCK" and [r["severity"] for r in rec["reasons"]] == sorted((r["severity"] for r in rec["reasons"]), key=["BLOCK", "WARN"].index)
    assert rec["reason_counts"] == {"GAP_2_OR_MORE_SESSIONS": 1, "TICKER_REUSE_COLLISION": 1}
    p = clean()
    p._manifest = replace(p._manifest, contract_version="historical_data_contract_v1")
    with pytest.raises(VA.UnsupportedContractVersion):
        VA.validate(p)


def test_V14_the_acceptance_criteria_as_code_and_as_a_document():
    p = clean()
    rec = VA.validate(p)
    d = AC.decide(rec, p.manifest(), instruments=p.instruments())
    assert d["usable_for"] == "EVALUATION" and [c["flag"] for c in d["verdict_caps"]] == ["SURVIVORSHIP_BIAS_PRESENT", "CORPORATE_ACTION_UNACCOUNTED"]
    assert d["flags"] == ["EARNINGS_DATA_UNAVAILABLE", "SYMBOL_HISTORY_UNKNOWN", "STATIC_CLASSIFICATION_MAP_NEEDED"]
    h = clean(kind="HISTORICAL")
    assert AC.decide(VA.validate(h), h.manifest())["reasons"] == ["LICENCE_NOT_CONFIRMED"]
    assert AC.decide(VA.validate(h), h.manifest())["usable_for"] == "INTEGRITY_ONLY"
    b = F.provider(F.series("SNDK", S[:3], F.smooth(3), prefix="P1"), F.series("SNDK", S[5:8], F.smooth(3), prefix="P2"))
    assert AC.decide(VA.validate(b), b.manifest())["usable_for"] == "NONE"
    n = clean(licence=Licence(unknown("NO_LICENCE_FILE"), "NOT_PERMITTED"))
    assert AC.decide(VA.validate(n), n.manifest())["reasons"] == ["LICENCE_NOT_PERMITTED"]
    r = clean(price_views=("RAW_CLOSE", "TOTAL_RETURN"))
    dr = AC.decide(VA.validate(r), r.manifest())
    assert dr["usable_for"] == "INTEGRITY_ONLY" and dr["reasons"] == ["RAW_OHL_UNAVAILABLE"] and "SPLIT_UNADJUSTED" in [c["flag"] for c in dr["verdict_caps"]]
    assert AC.decide(rec, clean(dataset_id="synthetic:other").manifest())["usable_for"] == "NONE"
    doc = (ROOT / "historical_data" / "ACCEPTANCE_CRITERIA.md").read_text(encoding="utf-8")
    for code in HD.BLOCK_CODES + HD.WARN_CODES + ("INTEGRITY_ONLY", "EVALUATION", "LICENCE_NOT_CONFIRMED", "NOT_PERMITTED", "SPLIT_UNADJUSTED",
                                                  "CORPORATE_ACTION_UNACCOUNTED", "SURVIVORSHIP_BIAS_PRESENT", "VERIFIED", "historical_data_contract_v2"):
        assert code in doc, code


# ============================================================================================================ the expected-events table (T)
def test_T01_only_verified_entries_classify_and_the_five_contradictions_are_unresolved():
    by_id = {e.entry_id: e for e in EE.TABLE}
    assert len(by_id) == len(EE.TABLE) and set(EE.UNRESOLVED_CONTRADICTIONS) == {"HON", "XOM", "BKNG", "KLAC", "PFE"}
    unresolved = {i for ids in EE.UNRESOLVED_CONTRADICTIONS.values() for i in ids}
    assert {e.entry_id for e in EE.TABLE if e.status == "UNRESOLVED"} == unresolved
    for sym, ids in EE.UNRESOLVED_CONTRADICTIONS.items():
        for i in ids:
            assert by_id[i].symbol == sym and "OWNER DECISION" in by_id[i].note, i
    checked = 0
    for e in EE.TABLE:                                                         # property: every entry, through the real validator
        assert EE.classifying(e) == (e.status == "VERIFIED"), e.entry_id
        if e.type not in EE.JUMP_TYPES or not e.session or not CAL.covered(e.session) or not CAL.is_session(e.session):
            continue
        (pair, _) = _jump(e.symbol, e.session)
        rec = VA.validate(F.provider(pair))
        hit = [c for c in rec["classified_jumps"] if e.entry_id in c["table_entry_ids"]]
        assert bool(hit) == (e.status == "VERIFIED"), e.entry_id
        assert bool(reasons(rec, "DAY_JUMP_UNCLASSIFIED")) == (e.status != "VERIFIED"), e.entry_id
        checked += 1
    assert checked >= 30
    assert not hasattr(EE, "events") and all(e.status != "VERIFIED" for e in EE.TABLE if e.symbol in ("BKNG", "KLAC", "XOM") and e.session and e.session >= "2026-01-01")


# ============================================================================================================ the adapters (A)
SESS_MS1 = CAL.sessions("2026-01-02", "2026-01-15")


def _ms1_bars(base):
    return [F.ms1_bar(s, base + k, base + k + 1, base + k - 1, base + k + 0.5) for k, s in enumerate(SESS_MS1)]


def test_A01_the_ms1_adapter_maps_the_raw_archive_per_section_4_1(tmp_path):
    raw = F.write_ms1_archive(tmp_path / "ms1-7", {"AAA": _ms1_bars(50.0), "BBB": _ms1_bars(80.0), "SPY": _ms1_bars(400.0)})
    p = Ms1EvidenceProvider(raw)
    m = p.manifest()
    assert (m.kind, m.licence.personal_research_use, m.volume_feed, m.price_source) == ("HISTORICAL", "UNKNOWN", "SINGLE_VENUE:IEX", "SINGLE_VENUE:IEX")
    assert (m.price_views_available, m.events_completeness, m.membership_basis) == (("RAW_CLOSE", "RAW_OHL"), "NONE", "FIXED_CURRENT_UNIVERSE")
    assert m.adjustment_as_of == unavailable("NO_ADJUSTED_FACTORS") and m.retrieved_at == Z("2026-11-15T20:42:10.347658Z")
    assert m.dataset_id.startswith("ms1-evidence:") and len(m.source_files) == 4 and m.source_files[0].path == "raw/manifest.jsonl"
    assert [i.instrument_id for i in p.instruments()] == ["ALPACA:AAA:2026-01-02", "ALPACA:BBB:2026-01-02", "ALPACA:SPY:2026-01-02"]
    b = p.bars("ALPACA:AAA:2026-01-02")[0]
    line = json.loads((raw / "manifest.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert (b.raw_open, b.raw_high, b.raw_low, b.raw_close, b.known_at) == (50.0, 51.0, 49.0, 50.5, CAL.close_utc("2026-01-02"))
    gz_sha = hashlib.sha256((raw / line["path"]).read_bytes()).hexdigest()
    assert b.split_factor == b.total_return_factor == unavailable("NOT_IN_SOURCE") and b.source_ref.index == 0
    assert (b.source_ref.body_sha256, b.source_ref.file_sha256) == (line["sha256"], gz_sha) and gz_sha != line["sha256"]   # QA L-4: both shas
    assert (m.source_files[1].path, m.source_files[1].body_sha256, m.source_files[1].file_sha256) == (f"raw/{line['path']}", line["sha256"], gz_sha)
    assert m.source_files[0].file_sha256 == m.source_files[0].body_sha256 == hashlib.sha256((raw / "manifest.jsonl").read_bytes()).hexdigest()
    assert p.scheduled_events("ALPACA:AAA:2026-01-02") is EARNINGS_DATA_UNAVAILABLE and p.events() == []
    assert p.membership("MS1_EVIDENCE_UNIVERSE", SESS_MS1[3]).flags == ("SURVIVORSHIP_BIAS_PRESENT",)
    with pytest.raises(LicenceRefused):
        p.as_of(CAL.close_utc(SESS_MS1[-1]))
    with pytest.raises(LicenceRefused):
        p.execution_open("ALPACA:AAA:2026-01-02", SESS_MS1[1], decision_session=SESS_MS1[0])
    rec = VA.validate(p)
    assert rec["verdict"] == "WARN" and F.codes(rec) == ["PRICE_SOURCE_NOT_CONSOLIDATED", "VOLUME_FEED_NOT_CONSOLIDATED"]
    d = AC.decide(rec, m, instruments=p.instruments())
    assert d["usable_for"] == "INTEGRITY_ONLY" and d["reasons"] == ["LICENCE_NOT_CONFIRMED"] and "SPLIT_UNADJUSTED" in [c["flag"] for c in d["verdict_caps"]]


def test_A02_the_ms1_adapter_refuses_a_tampered_or_unexpected_archive_and_removes_invalid_rows(tmp_path):
    raw = F.write_ms1_archive(tmp_path / "a", {"AAA": _ms1_bars(50.0)})
    gz = next((raw / "2026-11-15").glob("*.json.gz"))
    gz.write_bytes(gzip.compress(b'{"bars": [], "symbol": "AAA", "next_page_token": null}', mtime=0))
    with pytest.raises(SourceIntegrityError):
        Ms1EvidenceProvider(raw)
    raw = F.write_ms1_archive(tmp_path / "b", {"AAA": _ms1_bars(50.0)})
    next((raw / "2026-11-15").glob("*.json.gz")).unlink()
    with pytest.raises(SourceIntegrityError):
        Ms1EvidenceProvider(raw)
    bad_stamp = _ms1_bars(50.0)
    bad_stamp[2] = {**bad_stamp[2], "t": "2026-01-06T14:30:00Z"}
    with pytest.raises(SourceFormatError):
        Ms1EvidenceProvider(F.write_ms1_archive(tmp_path / "c", {"AAA": bad_stamp}))
    with pytest.raises(SourceFormatError):
        Ms1EvidenceProvider(F.write_ms1_archive(tmp_path / "d", {"AAA": {"bars": _ms1_bars(50.0), "symbol": "BBB", "next_page_token": None}}))
    for old, new in (('"path": "2026-11-15/', '"path": "../'), ('/bars"', '/quotes"')):
        raw = F.write_ms1_archive(tmp_path / f"e{len(old)}", {"AAA": _ms1_bars(50.0)})
        mf = raw / "manifest.jsonl"
        mf.write_text(mf.read_text(encoding="utf-8").replace(old, new), encoding="utf-8")
        with pytest.raises(SourceFormatError):
            Ms1EvidenceProvider(raw)
    p = Ms1EvidenceProvider(F.write_ms1_archive(tmp_path / "f", {"AAA": _ms1_bars(50.0), "CCC": {"message": "too many requests"}}))
    assert [i.instrument_id for i in p.instruments()] == ["ALPACA:AAA:2026-01-02"]
    assert [(n.get("why"), n.get("symbol")) for n in p.adapter_notes()] == [("ERROR_ENVELOPE", "CCC")]
    rec = VA.validate(p)                                                                          # QA L-5: a lost symbol is never silent
    assert [(r["code"], r["severity"]) for r in reasons(rec, "SYMBOL_DROPPED_BY_ADAPTER")] == [("SYMBOL_DROPPED_BY_ADAPTER", "WARN")] and "CCC" in reasons(rec, "SYMBOL_DROPPED_BY_ADAPTER")[0]["detail"]
    assert rec["adapter_notes"] == {"skipped_responses": 1, "skipped_response_symbols": ["CCC"], "dropped_symbols": ["CCC"], "removed_rows": 0}
    all_bad = [{**x, "l": x["c"] + 5} for x in _ms1_bars(70.0)]
    q = Ms1EvidenceProvider(F.write_ms1_archive(tmp_path / "f2", {"AAA": _ms1_bars(50.0), "DDD": all_bad}))
    assert q.dropped_symbols() == ["DDD"] and VA.validate(q)["adapter_notes"]["dropped_symbols"] == ["DDD"]
    assert VA.validate(Ms1EvidenceProvider(F.write_ms1_archive(tmp_path / "f3", {"AAA": _ms1_bars(50.0)})))["adapter_notes"]["dropped_symbols"] == []  # control
    invalid = _ms1_bars(50.0)
    invalid[3] = {**invalid[3], "l": invalid[3]["c"] + 5}
    p = Ms1EvidenceProvider(F.write_ms1_archive(tmp_path / "g", {"AAA": invalid}))
    assert p.gaps("ALPACA:AAA:2026-01-02") == [Gap("ALPACA:AAA:2026-01-02", SESS_MS1[3], "REMOVED_INVALID")]
    assert p.removed_rows() == [("ALPACA:AAA:2026-01-02", SESS_MS1[3])]


def test_A03_the_ms1_adapter_reads_only_the_raw_archive_never_a_database_and_writes_nothing(tmp_path, monkeypatch):
    root = tmp_path / "ms1-9"
    raw = F.write_ms1_archive(root, {"AAA": _ms1_bars(50.0), "BBB": _ms1_bars(80.0)})
    (root / "market_intel.db").write_bytes(b"decoy - not a database")
    (root / "out").mkdir()
    (root / "out" / "summary.json").write_text("{}", encoding="utf-8")
    before, listing = F.tree_snapshot(root), sorted(p.relative_to(root).as_posix() for p in root.rglob("*"))
    opened, real_open = [], builtins.open

    def spy(file, mode="r", *a, **k):
        opened.append((str(file), mode))
        return real_open(file, mode, *a, **k)

    def no_db(*a, **k):
        raise AssertionError("a database was opened")
    monkeypatch.setattr(sqlite3, "connect", no_db)
    monkeypatch.setattr(builtins, "open", spy)
    p = Ms1EvidenceProvider(raw)
    rec = VA.validate(p)
    monkeypatch.undo()
    mine = [(f, m) for f, m in opened if Path(f).resolve().is_relative_to(root.resolve())]
    assert len(mine) == 3 and all(m == "rb" and Path(f).resolve().is_relative_to(raw.resolve()) for f, m in mine), mine
    assert not [f for f, _ in opened if f.lower().endswith(F.DB_SUFFIXES)]
    assert F.tree_snapshot(root) == before and sorted(p.relative_to(root).as_posix() for p in root.rglob("*")) == listing
    assert rec["verdict"] == "WARN"


def _sip_out(tmp_path, split=("2016-03-01", 2.0)):
    import sip_eval_fixtures as SF
    from research_data import sip_bundle as SB
    universe, end = ["ABBV", "SPY", "AAPL"], "2016-03-31"
    plan = SB.request_plan("2016-01-04", end, universe=universe)
    sess = CAL.sessions("2016-01-04", end)
    bars = SF.synthetic_bars(universe, sess, split={"AAPL": split} if split else None)
    out = tmp_path / "sip"
    SB.fetch(plan, out=out, approval="OPERATOR-APPROVAL:test-only-reference", plan_sha256=SB.sha256_of(plan), transport=SF.fake_provider(bars, SF.sec_tickers(universe)),
             alpaca_key=SF.KEY, alpaca_secret=SF.SECRET, sec_user_agent=SF.UA, sleep=lambda s: None)
    SB.build(out, kind="SYNTHETIC")
    return out, bars, sess


def test_A04_the_sip_adapter_maps_the_bundle_and_its_raw_responses(tmp_path):
    out, bars, sess = _sip_out(tmp_path)
    p = SipDailyV1Provider(out)
    m = p.manifest()
    assert (m.kind, m.volume_feed, m.price_source, m.events_completeness, m.membership_basis) == ("SYNTHETIC", "CONSOLIDATED", "CONSOLIDATED", "NONE",
                                                                                                    "FIXED_CURRENT_UNIVERSE")
    assert m.price_views_available == ("RAW_CLOSE", "RAW_OHL", "SPLIT_ADJUSTED", "TOTAL_RETURN") and m.licence.personal_research_use == "UNKNOWN"
    assert {"sip_bundle.json", "manifest.json"} <= {s.path for s in m.source_files} and any(s.path.startswith("raw/bars_raw_") for s in m.source_files)
    aapl = "ALPACA:AAPL:2016-01-04"
    pb, rb = p.bars(aapl), bars["raw"]["AAPL"]
    assert [(b.raw_open, b.raw_high, b.raw_low, b.raw_close) for b in pb] == [(float(x["o"]), float(x["h"]), float(x["l"]), float(x["c"])) for x in rb]
    k = sess.index("2016-03-01")
    assert abs(pb[k - 1].split_factor - 2.0) < 1e-4 and pb[k].split_factor == 1.0 and abs(pb[k - 1].total_return_factor - 2.0) < 1e-3
    rec = VA.validate(p)
    assert rec["verdict"] == "WARN" and [(r["code"], r["instrument_id"], r["session"]) for r in rec["reasons"]] == [
        ("DAY_JUMP_UNCLASSIFIED", aapl, "2016-03-01"), ("INFERRED_SPLIT", aapl, "2016-03-01")]
    v = p.as_of(CAL.close_utc("2016-03-31"))
    assert v.bars(aapl)[0].split_factor == pb[0].split_factor and v.bars(aapl)[-1].split_factor == 1.0
    assert p.execution_open(aapl, "2016-03-02", decision_session="2016-03-01") == float(rb[k + 1]["o"])


def test_A05_the_sip_adapter_is_integrity_only_without_raw_responses_and_refuses_tampering(tmp_path):
    out, _bars, _sess = _sip_out(tmp_path / "a")
    doc = json.loads((out / "sip_bundle.json").read_text(encoding="utf-8"))
    doc["aux"]["series"]["split_ratio"]["ABBV"] = {}
    (out / "sip_bundle.json").write_text(json.dumps(doc, sort_keys=True), encoding="utf-8")
    rec = VA.validate(SipDailyV1Provider(out))
    assert [(r["code"], r["instrument_id"]) for r in rec["reasons"] if r["severity"] == "BLOCK"] == [("SPLIT_SERIES_MISSING", "ALPACA:ABBV:2016-01-04")]
    out, _bars, _sess = _sip_out(tmp_path / "b")
    for f in (out / "raw").glob("bars_raw_*.json.gz"):
        f.unlink()
    p = SipDailyV1Provider(out)
    assert "RAW_OHL" not in p.manifest().price_views_available and p.bars("ALPACA:ABBV:2016-01-04")[0].raw_open == unavailable("RAW_OHL_NOT_ARCHIVED")
    with pytest.raises(IntegrityOnly):
        p.as_of(CAL.close_utc("2016-03-31"))
    d = AC.decide(VA.validate(p), p.manifest())
    assert d["usable_for"] == "INTEGRITY_ONLY" and d["reasons"] == ["RAW_OHL_UNAVAILABLE"]
    out, _bars, _sess = _sip_out(tmp_path / "c")
    (out / "manifest.json").write_bytes((out / "manifest.json").read_bytes() + b" ")
    with pytest.raises(SourceIntegrityError):
        SipDailyV1Provider(out)
    out, _bars, _sess = _sip_out(tmp_path / "d")
    f = next((out / "raw").glob("bars_raw_*.json.gz"))
    f.write_bytes(gzip.compress(b'{"bars": {}, "next_page_token": null}', mtime=0))
    with pytest.raises(SourceIntegrityError):
        SipDailyV1Provider(out)


def _rewrite_raw_manifest(out, edit):
    """Edit the archived manifest.json and re-point the bundle at it, so ONLY the edited fact differs (the bundle's own sha check passes)."""
    man = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    edit(man)
    data = json.dumps(man, indent=1, sort_keys=True).encode("utf-8")
    (out / "manifest.json").write_bytes(data)
    b = json.loads((out / "sip_bundle.json").read_text(encoding="utf-8"))
    b["provenance"]["raw_manifest_sha256"] = hashlib.sha256(data).hexdigest()
    (out / "sip_bundle.json").write_text(json.dumps(b, sort_keys=True), encoding="utf-8")


def test_A06_the_sip_adapter_refuses_an_incomplete_fetch_and_a_synthetic_bundle_from_the_network(tmp_path):
    out, _b, _s = _sip_out(tmp_path / "a")
    _rewrite_raw_manifest(out, lambda m: None)                                                      # control: the rewrite alone is accepted
    assert SipDailyV1Provider(out).manifest().kind == "SYNTHETIC"
    _rewrite_raw_manifest(out, lambda m: m.update(complete=False, stopped="bars_raw_00 page 1: HTTP 429"))
    with pytest.raises(SourceIntegrityError, match="not complete"):
        SipDailyV1Provider(out)                                                                     # QA L-2
    out, _b, _s = _sip_out(tmp_path / "b")
    _rewrite_raw_manifest(out, lambda m: m.update(network=True))
    with pytest.raises(SourceIntegrityError, match="network"):
        SipDailyV1Provider(out)                                                                     # QA L-2: a network fetch is never SYNTHETIC


def test_A07_raw_file_names_from_a_manifest_are_strict_and_stay_inside_the_archive_folder(tmp_path):
    for bad in ("..\\evil.json.gz", "../evil.json.gz", "C:\\evil.json.gz", "/etc/evil.json.gz", "bars_raw_00_p000.json.gz/../../x"):
        out, _b, _s = _sip_out(tmp_path / f"s{abs(hash(bad)) % 10 ** 8}")

        def edit(m, bad=bad):
            m["responses"][1]["file"] = bad
        _rewrite_raw_manifest(out, edit)
        with pytest.raises(SourceFormatError):
            SipDailyV1Provider(out)                                                                 # QA L-3: refused before any read
    raw = F.write_ms1_archive(tmp_path / "m", {"AAA": _ms1_bars(50.0)})
    mf = raw / "manifest.jsonl"
    line = json.loads(mf.read_text(encoding="utf-8").splitlines()[0])
    for bad in ("..\\2026-11-15/" + line["path"].split("/")[1], "C:/2026-11-15/" + line["path"].split("/")[1], "/2026-11-15/" + line["path"].split("/")[1]):
        mf.write_text(json.dumps({**line, "path": bad}, sort_keys=True) + "\n", encoding="utf-8")
        with pytest.raises(SourceFormatError):
            Ms1EvidenceProvider(raw)
    from historical_data.adapters.ms1_evidence import inside
    for bad in ("..\\x", "a/../../x", "C:\\x", "/x", ""):
        with pytest.raises(SourceFormatError):
            inside(raw, bad)
    assert inside(raw, line["path"]) == (raw / line["path"]).resolve()                                # control: a plain name inside the folder


def test_A08_event_fields_are_values_or_p4_objects_and_merger_fields_are_validated():
    i = "SYNTH:AAA:2026-02-02"
    k = CAL.close_utc(S[0])
    split = CorporateEvent("E1", i, "SPLIT", S[3], k, ratio=2.0)
    assert all(v is not None for v in split.to_json().values()) and split.to_json()["cash_amount"] == {"status": "UNKNOWN", "reason": "NOT_PROVIDED"}
    for bad in (dict(type="SPLIT"), dict(type="SPLIT", ratio=2.0, cash_amount=1.0), dict(type="CASH_DIVIDEND", cash_amount=1.0, ratio=2.0),
                dict(type="MERGER"), dict(type="MERGER", stock_ratio=0.5), dict(type="MERGER", cash_amount=-1.0),
                dict(type="MERGER", cash_amount=10.0, child_ratio=0.1), dict(type="SPLIT", ratio=None), dict(type="SPIN_OFF", child_ratio=0.1),
                dict(type="DELISTING", last_trade_session=S[2], last_raw_price=5.0, delisting_return=-1.5)):
        kw = dict(bad)
        typ = kw.pop("type")
        with pytest.raises((ValueError, TypeError)):
            CorporateEvent("E2", i, typ, S[3], k, **kw)
    stock = CorporateEvent("M1", i, "MERGER", S[3], k, acquirer_instrument_id="SYNTH:BIG:2016-01-04", stock_ratio=0.5)
    cash = CorporateEvent("M2", i, "MERGER", S[3], k, cash_amount=42.0)
    both = CorporateEvent("M3", i, "MERGER", S[3], k, acquirer_instrument_id="SYNTH:BIG:2016-01-04", stock_ratio=0.25, cash_amount=10.0)
    assert (stock.stock_ratio, cash.cash_amount, both.cash_amount) == (0.5, 42.0, 10.0) and cash.acquirer_instrument_id == unknown("NOT_PROVIDED")
    d = CorporateEvent("D1", i, "DELISTING", S[3], k, last_trade_session=S[2], last_raw_price=5.0)
    assert d.delisting_return == unknown("NOT_PROVIDED") and d.to_json()["delisting_return"] == {"status": "UNKNOWN", "reason": "NOT_PROVIDED"}


def test_A09_a_duplicate_event_id_blocks_and_membership_and_scheduled_sessions_are_calendar_checked():
    inst, bars = F.series("AAA", S[:6], F.smooth(6))
    k = CAL.close_utc(S[0])
    e1 = CorporateEvent("E1", inst.instrument_id, "CASH_DIVIDEND", S[2], k, cash_amount=0.1)
    e2 = CorporateEvent("E1", inst.instrument_id, "CASH_DIVIDEND", S[4], k, cash_amount=0.1)
    rec = VA.validate(F.provider((inst, bars), events=[e1, e2]))
    assert [(r["code"], r["detail"]) for r in rec["reasons"] if r["severity"] == "BLOCK"] == [("DUPLICATE_KEY", "event_id E1: 2 events")]
    assert VA.validate(F.provider((inst, bars), events=[e1]))["verdict"] == "CLEAN"                   # control
    from historical_data.schema import MembershipChange, ScheduledEvent
    changes = [MembershipChange("U", inst.instrument_id, S[0], "ADD", k), MembershipChange("U", inst.instrument_id, "2026-02-07", "REMOVE", k)]
    sched = [ScheduledEvent("S1", inst.instrument_id, "2027-01-04", "BEFORE_OPEN", k), ScheduledEvent("S2", inst.instrument_id, S[5], "AFTER_CLOSE", k)]
    rec = VA.validate(F.provider((inst, bars), membership=changes, membership_basis="POINT_IN_TIME", scheduled_events=sched))
    assert {(r["code"], r["session"]) for r in rec["reasons"]} == {("NON_CALENDAR_SESSION", "2026-02-07"), ("CALENDAR_NOT_COVERED", "2027-01-04")}
    good = [MembershipChange("U", inst.instrument_id, S[0], "ADD", k)]
    assert VA.validate(F.provider((inst, bars), membership=good, membership_basis="POINT_IN_TIME", scheduled_events=sched[1:]))["verdict"] == "CLEAN"


# ============================================================================================================ isolation (X)
STDLIB_OK = {"__future__", "abc", "bisect", "copy", "dataclasses", "datetime", "functools", "gzip", "hashlib", "importlib", "importlib.metadata",
             "importlib.util", "json", "math", "pathlib", "re", "typing", "zoneinfo"}


def test_X01_the_package_imports_no_network_database_trading_or_market_scan_module_and_writes_nothing():
    for f in sorted((ROOT / "historical_data").rglob("*.py")):
        tree = ast.parse(f.read_text(encoding="utf-8"))
        mods = {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)} | {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        for m in mods:
            assert m in STDLIB_OK or m == "historical_data" or m.startswith("historical_data.") or m in ("research_data", "market_intel"), (f.name, m)
        for n in ast.walk(tree):
            if isinstance(n, ast.ImportFrom) and n.module == "research_data":
                assert [a.name for a in n.names] == ["nyse_calendar"], f.name
            if isinstance(n, ast.ImportFrom) and n.module == "market_intel":
                assert f.name == "calendar.py" and [a.name for a in n.names] == ["calendar"], f.name
        calls = {(c.func.attr if isinstance(c.func, ast.Attribute) else c.func.id) for c in ast.walk(tree) if isinstance(c, ast.Call)
                 and isinstance(c.func, (ast.Attribute, ast.Name))}
        assert not calls & {"write_text", "write_bytes", "mkdir", "unlink", "rmdir", "rename", "touch", "connect", "urlopen", "system", "Popen",
                            "run", "remove", "makedirs"}, (f.name, calls)
        for c in ast.walk(tree):
            if isinstance(c, ast.Call) and isinstance(c.func, ast.Name) and c.func.id == "open":
                assert len(c.args) == 2 and isinstance(c.args[1], ast.Constant) and c.args[1].value == "rb", f.name


def test_X03_only_calendar_py_names_the_stage_71_and_78b1_packages_the_isolation_guards_admit_that_one_file():
    for f in sorted((ROOT / "historical_data").rglob("*.py")):
        src = f.read_text(encoding="utf-8")
        named = [w for w in ("market_intel", "research_data", "market_scan", "strategy_validation", "swing_eval", "swing_pullback") if w in src]
        assert named == ([] if f.name != "calendar.py" or f.parent.name != "historical_data" else ["market_intel", "research_data"]), (f.name, named)


def test_X02_nothing_outside_the_package_and_the_tests_imports_it():
    for p in ROOT.rglob("*.py"):
        rel = p.relative_to(ROOT).as_posix()
        if rel.startswith(("historical_data/", "tests/", ".venv/", "scratchpad/", ".claude/")) or "/site-packages/" in rel:
            continue
        assert "historical_data" not in p.read_text(encoding="utf-8", errors="replace"), rel


# ============================================================================================================ the contract (Z)
def test_Z01_the_contract_files_match_the_pinned_shas_before_use():
    # MDA-FR-6: the committed copies, so this runs in CI. There is no skip path: a missing file fails.
    raw = {name: (CONTRACT_DIR / name).read_bytes() for name in CONTRACT_FILES}
    for name, data in raw.items():                                                          # checkout normalisation would change the shas
        assert b"\r" not in data and not data.startswith(b"\xef\xbb\xbf"), name
    base = hashlib.sha256(raw["HISTORICAL_DATA_CONTRACT_v1.md"]).hexdigest()
    amend = hashlib.sha256(raw["HISTORICAL_DATA_CONTRACT_v2_AMENDMENT.md"]).hexdigest()
    assert base == HD.CONTRACT_BASE_SHA256 == "453676fd44554ac9f66ce2d168318893ea18d93916ea2eef3ffa0f8c4de40fd7"
    assert amend == HD.CONTRACT_SHA256 == "7850580ba22195e92ee03f71e75d2d7eb3719176343e90f485ce47c3ac876ed8"
    assert base in raw["HISTORICAL_DATA_CONTRACT_v2_AMENDMENT.md"].decode("utf-8")           # A12: v2 pins its base by sha
    recorded = (CONTRACT_DIR / "CONTRACT_SHA256.txt").read_text(encoding="utf-8")
    assert f"{base}  HISTORICAL_DATA_CONTRACT_v1.md" in recorded and f"{amend}  HISTORICAL_DATA_CONTRACT_v2_AMENDMENT.md" in recorded


def test_Z02_every_manifest_and_adapter_names_contract_v2():
    from historical_data.adapters import ms1_evidence, sip_daily_v1
    assert HD.CONTRACT_VERSION == "historical_data_contract_v2" and HD.SUPPORTED_CONTRACT_VERSIONS == ("historical_data_contract_v2",)
    for mod in (SY, ms1_evidence, sip_daily_v1):
        assert mod.SUPPORTED_CONTRACT_VERSIONS == HD.SUPPORTED_CONTRACT_VERSIONS
    m = clean().manifest()
    assert (m.contract_version, m.contract_sha256, m.contract_base_sha256) == (HD.CONTRACT_VERSION, HD.CONTRACT_SHA256, HD.CONTRACT_BASE_SHA256)


@pytest.mark.skipif(not (PLANNING_DIR / "HISTORICAL_DATA_CONTRACT_v1.md").is_file(), reason="local only: the approved planning copies live outside the repository")
def test_Z03_the_committed_contract_copies_equal_the_approved_planning_originals():
    for name in CONTRACT_FILES:                                                              # byte for byte
        assert (CONTRACT_DIR / name).read_bytes() == (PLANNING_DIR / name).read_bytes(), name
    def lines(p):                                                                           # the planning record has a BOM and CRLF; the committed one is normalised
        return [s for s in p.read_text(encoding="utf-8-sig").replace("\r\n", "\n").split("\n") if s]
    assert lines(CONTRACT_DIR / "CONTRACT_SHA256.txt") == lines(PLANNING_DIR / "CONTRACT_SHA256.txt")
    assert not (CONTRACT_DIR / "CONTRACT_SHA256.txt").read_bytes().startswith(b"\xef\xbb\xbf") and b"\r" not in (CONTRACT_DIR / "CONTRACT_SHA256.txt").read_bytes()
