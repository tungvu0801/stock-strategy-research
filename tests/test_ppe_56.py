"""
Stage 5.6 — PPE ENGINE (Price Position & Excess). Engine tests (E01–E25 + bands), lookahead tests (L1–L5), evidence snapshot tests,
replay tool test, Trader View / UI tests (U1–U9) and safety tests (S1–S9). Synthetic bars only; no network; stored-evidence fakes.
"""
import ast
import hashlib
import json
import math
import random
import re
import sqlite3
import subprocess
import sys
import types
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import config  # noqa: E402
from api.routes import trader_view as RT  # noqa: E402
from ppe import CAPS, DISCLAIMER, WEIGHTS, confidence_label, stretch_label  # noqa: E402
from ppe.confidence import compute_confidence  # noqa: E402
from ppe.engine import compute_ppe  # noqa: E402
from ppe.evidence_context import latest_snapshot, snapshot_from_packet  # noqa: E402
from ppe.inputs import Benchmark, DailyBar, MarketInputs, point_in_time  # noqa: E402
from ppe.models import EvidenceSnapshot, PPEResult, StretchResult  # noqa: E402
from ppe.sectors import basket_bars, resolve_benchmark, sector_for  # noqa: E402
from ppe.stretch import compute_stretch  # noqa: E402
from trader_view.adapters import NullPositionAdapter, NullResearchAdapter, UnknownSymbol  # noqa: E402
from trader_view.hub import accepts_message  # noqa: E402
from trader_view.ppe_provider import PPEProvider, session_today  # noqa: E402
from trader_view.simulated import SimulatedMarket  # noqa: E402
from trader_view.stream import TerminalStream  # noqa: E402
from test_trader_view_55 import T0, WL, FakePositions, FakeResearch, burst, hello_of, marker  # noqa: E402

JS = (ROOT / "frontend" / "trader_view.js").read_text(encoding="utf-8")
CORE = (ROOT / "frontend" / "trader_core.js").read_text(encoding="utf-8")
PPE_FILES = sorted(p.relative_to(ROOT).as_posix() for p in (ROOT / "ppe").glob("*.py")) + ["trader_view/ppe_provider.py"]
END = date(2026, 9, 28)
WS_PATH = "/api/terminal/stream"


def ppe_source() -> str:
    return "".join((ROOT / f).read_text(encoding="utf-8") for f in PPE_FILES)


# ---------------------------------------------------------------- synthetic bars --------------------------------------------------

def sessions(n: int, end: date = END):
    out, d = [], end
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= timedelta(days=1)
    return list(reversed(out))


def walk(seed: int, n: int = 300, vol: float = 0.015, drift: float = 0.0003, start: float = 100.0, volume=1_000_000.0):
    rng = random.Random(seed)
    px, bars = start, []
    for d in sessions(n):
        o = px * (1 + rng.gauss(0, vol / 3))
        c = o * (1 + rng.gauss(drift, vol))
        h, lo = max(o, c) * (1 + abs(rng.gauss(0, vol / 2))), min(o, c) * (1 - abs(rng.gauss(0, vol / 2)))
        v = None if volume is None else float(int(volume * (1 + abs(rng.gauss(0, 0.4)))))
        bars.append(DailyBar(d, round(o, 4), round(h, 4), round(lo, 4), round(c, 4), v))
        px = c
    return bars


def calm(n: int = 300, start: float = 100.0, amp: float = 0.002, volume=1_000_000.0):
    """A deliberately unremarkable series: tiny alternating moves around a flat level, constant volume."""
    bars = []
    for i, d in enumerate(sessions(n)):
        c = start * (1 + amp * (1 if i % 2 else -1) * (0.5 + (i % 5) / 10))
        o = start * (1 + amp * (0.5 if i % 2 else -0.5))
        bars.append(DailyBar(d, round(o, 4), round(max(o, c) * 1.001, 4), round(min(o, c) * 0.999, 4), round(c, 4), volume))
    return bars


def shock(bars, pct: float, vol_mult: float = 1.0, days: int = 1):
    """Replace the last `days` sessions with a move of `pct` (compounded evenly) at `vol_mult` × the original volume."""
    out = list(bars)
    for k in range(days, 0, -1):
        prev = out[-k - 1].close
        step = (1 + pct) ** (1 / days)
        c = prev * step
        o = prev * (1 + (step - 1) * 0.6)
        b = out[-k]
        out[-k] = DailyBar(b.date, round(o, 4), round(max(o, c) * 1.002, 4), round(min(o, c) * 0.998, 4), round(c, 4), None if b.volume is None else b.volume * vol_mult)
    return out


def bench(seed: int = 99, **kw) -> Benchmark:
    return Benchmark("SOXX", "SECTOR_ETF", walk(seed, vol=0.012, **kw), label="SOXX (Semiconductors ETF)")


def inputs(bars, benchmark=None, **kw) -> MarketInputs:
    return MarketInputs(symbol="TEST", bars=bars, as_of=max(b.date for b in bars), benchmark=benchmark, mode="replay", **kw)


def calm_bench(seed: int = 99) -> Benchmark:
    """A quiet but non-degenerate benchmark: a periodic one has zero relative-return dispersion (ZERO_VARIANCE by design)."""
    return Benchmark("SOXX", "SECTOR_ETF", walk(seed, vol=0.006, drift=0.0, start=200.0), label="SOXX (Semiconductors ETF)")


def ppe(bars, ev=None, benchmark=None, **kw) -> PPEResult:
    return compute_ppe(inputs(bars, benchmark, **kw), ev, now_fn=lambda: 0.0)


def ev(**over) -> EvidenceSnapshot:
    base = dict(packet_id="a" * 32, packet_status="COMPLETED", created_at="2026-09-27T12:00:00+00:00", age_days=1, strength="MODERATE", cause="MIXED",
                explained_share=0.5, rungs={"1": "SUPPORTED", "2": "PARTIAL", "3": "NOT_AVAILABLE", "4": "PARTIAL", "5": "NOT_SUPPORTED", "6": "NOT_SUPPORTED"},
                n_items=12)
    base.update(over)
    return EvidenceSnapshot(**base)


def stretched(score: float) -> StretchResult:
    return StretchResult(status="AVAILABLE", score=score, raw_score=score, label=stretch_label(score), coverage_weight=1.0, direction="DOWN" if score < 0 else "UP")


# ================================================================== engine E01–E25 ==============================================

def test_E01_neutral_series_gives_near_zero_stretch():
    r = ppe(calm(), benchmark=calm_bench())
    assert r.stretch.status == "AVAILABLE" and abs(r.stretch.score) < 20 and r.stretch.label == "NORMAL" and r.stretch.coverage_weight == 1.0
    assert all(abs(c.score) < 60 for c in r.stretch.components.values()) and r.confidence.status == "EVIDENCE_NOT_AVAILABLE"


def test_E02_severe_downside_dislocation_is_extreme_negative():
    r = ppe(shock(calm(), -0.18, 4.0), benchmark=calm_bench())
    s = r.stretch
    assert s.coverage_weight == 1.0 and s.score <= -75 and s.label == "EXTREME_DOWNSIDE" and s.direction == "DOWN" and all(c.score < 0 for c in s.components.values())
    assert any("below EMA20" in f for f in s.key_factors) and len(s.key_factors) <= 3


def test_E03_severe_upside_dislocation_is_extreme_positive():
    r = ppe(shock(calm(), 0.18, 4.0), benchmark=calm_bench())
    s = r.stretch
    assert s.coverage_weight == 1.0 and s.score >= 75 and s.label == "EXTREME_UPSIDE" and s.direction == "UP" and all(c.score > 0 for c in s.components.values())


def test_E04_same_absolute_move_is_reasonably_symmetric():
    b = calm_bench()
    for pct in (0.04, 0.09, 0.18):
        up, down = ppe(shock(calm(), pct, 2.0), benchmark=b).stretch.score, ppe(shock(calm(), -pct, 2.0), benchmark=b).stretch.score
        assert up > 0 > down and abs(up + down) <= 12, (pct, up, down)


def test_E05_volume_changes_magnitude_only_in_the_direction_of_the_move():
    base = calm()
    quiet, loud = ppe(shock(base, -0.10, 1.0)).stretch, ppe(shock(base, -0.10, 4.0)).stretch
    assert quiet.components["volume_shock"].score == 0.0 and loud.components["volume_shock"].score == -100.0 and loud.score < quiet.score
    mid = ppe(shock(base, -0.10, 2.5)).stretch.components["volume_shock"]
    assert -100 < mid.score < 0 and mid.raw["ratio"] == pytest.approx(2.5, rel=0.05)
    flat_loud = list(base)
    last = flat_loud[-1]
    flat_loud[-1] = DailyBar(last.date, flat_loud[-2].close, flat_loud[-2].close * 1.0001, flat_loud[-2].close * 0.9999, flat_loud[-2].close, last.volume * 5)
    assert ppe(flat_loud).stretch.components["volume_shock"].score == 0.0                      # heavy volume on a flat session adds nothing
    up_loud = ppe(shock(base, 0.10, 4.0)).stretch.components["volume_shock"]
    assert up_loud.score == 100.0


def test_E06_sector_selloff_reduces_the_stock_specific_contribution():
    stock = shock(calm(), -0.15, 2.0)
    alone = ppe(stock, benchmark=calm_bench()).stretch
    together = ppe(stock, benchmark=Benchmark("SOXX", "SECTOR_ETF", shock(calm_bench().bars, -0.13, 2.0), label="SOXX")).stretch
    assert alone.components["relative_sector_move"].score <= -75
    assert together.components["relative_sector_move"].score > alone.components["relative_sector_move"].score + 40
    assert together.score > alone.score


def test_E07_stock_only_selloff_increases_relative_sector_downside():
    r = ppe(shock(calm(), -0.12, 1.0), benchmark=calm_bench()).stretch
    c = r.components["relative_sector_move"]
    assert c.status == "AVAILABLE" and c.score <= -75 and c.raw["rel5"] < -0.10 and "vs SOXX" in c.reason


def test_E08_missing_sector_mapping_is_handled_explicitly():
    assert sector_for("SNDK") == "Semiconductors" and sector_for("NVDA") == "Semiconductors" and sector_for("AAPL") == "Software" and sector_for("ZZZQ") is None
    assert resolve_benchmark("NVDA", lambda s: True)[:2] == (("SOXX",), "SECTOR_ETF")
    syms, kind, label = resolve_benchmark("NVDA", lambda s: s != "SOXX")
    assert kind == "PEER_BASKET" and "NVDA" not in syms and len(syms) >= 3 and "peer basket" in label
    assert resolve_benchmark("NVDA", lambda s: s in ("SPY", "AMD"))[:2] == (("SPY",), "MARKET")          # fewer than 3 peers: labelled market fallback
    assert "market fallback" in resolve_benchmark("ZZZQ", lambda s: s == "SPY")[2]
    assert resolve_benchmark("ZZZQ", lambda s: False) == ((), "NONE", "no deterministic benchmark")
    r = ppe(shock(calm(), -0.15, 2.0), benchmark=None).stretch
    c = r.components["relative_sector_move"]
    assert c.status == "NOT_AVAILABLE" and c.code == "NO_BENCHMARK" and r.coverage_weight == pytest.approx(0.85) and r.status == "AVAILABLE"
    # equal-weight peer basket: returns are the mean of member returns
    d1, d2, d3 = sessions(3)
    members = {"A": [DailyBar(d1, 10, 10, 10, 10), DailyBar(d2, 11, 11, 11, 11), DailyBar(d3, 11, 11, 11, 11)],
               "B": [DailyBar(d1, 20, 20, 20, 20), DailyBar(d2, 20, 20, 20, 20), DailyBar(d3, 22, 22, 22, 22)]}
    basket = basket_bars(members)
    assert [round(b.close, 4) for b in basket] == [100.0, 105.0, 110.25] and basket[0].volume is None


def test_E09_insufficient_history_is_handled():
    forty = ppe(calm(40)).stretch
    assert forty.status == "AVAILABLE" and forty.coverage_weight == pytest.approx(0.65)
    assert forty.components["abnormal_return"].code == "INSUFFICIENT_HISTORY" and forty.components["relative_sector_move"].code == "NO_BENCHMARK"
    ten = ppe(calm(10)).stretch
    assert ten.status == "INSUFFICIENT_DATA" and ten.score is None and ten.label is None and ten.coverage_weight < 0.6
    one = compute_stretch(MarketInputs(symbol="T", bars=calm(1), as_of=END))
    assert one.status == "NOT_AVAILABLE" and one.score is None
    none = compute_stretch(MarketInputs(symbol="T", bars=[], as_of=END))
    assert none.status == "NOT_AVAILABLE"


def test_E10_zero_atr_and_zero_variance_are_safe():
    flat = [DailyBar(d, 100.0, 100.0, 100.0, 100.0, 1e6) for d in sessions(300)]
    r = ppe(flat)
    s = r.stretch
    assert s.status == "INSUFFICIENT_DATA" and s.score is None
    assert s.components["price_dislocation"].code == "ZERO_ATR" and s.components["gap_extension"].code == "ZERO_ATR" and s.components["abnormal_return"].code == "ZERO_VARIANCE"
    json.dumps(r.model_dump(), allow_nan=False)
    flat_zero_vol = [DailyBar(d, 100.0, 100.0, 100.0, 100.0, 0.0) for d in sessions(300)]
    assert ppe(flat_zero_vol).stretch.components["volume_shock"].code == "ZERO_VOLUME"


def test_E11_missing_volume_is_handled():
    r = ppe(shock(calm(volume=None), -0.12, 1.0), benchmark=calm_bench()).stretch
    assert r.components["volume_shock"].status == "NOT_AVAILABLE" and r.components["volume_shock"].code == "NO_VOLUME"
    assert r.coverage_weight == pytest.approx(0.85) and r.status == "AVAILABLE" and r.score < -40


def test_E12_partial_coverage_renormalises_correctly():
    r = ppe(shock(calm(volume=None), -0.12, 1.0), benchmark=None).stretch       # C and D unavailable: 0.70 of the weight remains
    avail = {k: c.score for k, c in r.components.items() if c.status == "AVAILABLE"}
    assert set(avail) == {"price_dislocation", "abnormal_return", "gap_extension", "short_term_exhaustion"}
    expected = sum(WEIGHTS[k] * v for k, v in avail.items()) / sum(WEIGHTS[k] for k in avail)
    assert r.coverage_weight == pytest.approx(0.70) and r.raw_score == pytest.approx(expected, abs=1e-5) and r.score == round(expected, 1)


def test_E13_below_minimum_coverage_gives_null_stretch_and_no_confidence():
    r = ppe(calm(40, volume=None), ev())                                          # A, E, F only = 0.50 < 0.60
    assert r.stretch.status == "INSUFFICIENT_DATA" and r.stretch.score is None and r.stretch.coverage_weight == pytest.approx(0.5)
    assert r.confidence.status == "STRETCH_NOT_AVAILABLE" and r.confidence.score is None and r.status == "INSUFFICIENT_DATA"


def test_E14_no_evidence_gives_null_confidence():
    r = ppe(shock(calm(), -0.18, 4.0), None)
    assert r.confidence.status == "EVIDENCE_NOT_AVAILABLE" and r.confidence.score is None and r.confidence.label is None
    assert "Research runs only on explicit request" in r.confidence.reasons[0] and r.sources.evidence_packet_at is None


def test_E15_weak_evidence_caps_confidence():
    c = compute_confidence(stretched(-80), ev(strength="WEAK", cause="MARKET_WIDE_DOMINANT", explained_share=0.9, rungs={"4": "SUPPORTED", "5": "SUPPORTED", "6": "SUPPORTED"}))
    assert c.status == "AVAILABLE" and c.score <= CAPS["WEAK"] == 50 and any(x["cap"] == 50 for x in c.caps)
    strong = compute_confidence(stretched(-80), ev(strength="STRONG", cause="MARKET_WIDE_DOMINANT", explained_share=0.9, rungs={"4": "SUPPORTED", "5": "SUPPORTED", "6": "SUPPORTED"}))
    assert strong.score > c.score and strong.score >= 85 and strong.label == "VERY_HIGH"
    moderate = compute_confidence(stretched(-80), ev(strength="MODERATE", cause="MARKET_WIDE_DOMINANT", explained_share=0.9, rungs={"4": "SUPPORTED", "5": "SUPPORTED", "6": "SUPPORTED"}))
    assert moderate.score <= 84 and moderate.label == "HIGH"                       # VERY_HIGH needs STRONG evidence


def test_E16_company_specific_negative_evidence_keeps_confidence_low():
    c = compute_confidence(stretched(-85), ev(cause="COMPANY_SPECIFIC_DOMINANT", explained_share=0.1, company_down=2, strength="STRONG"))
    assert c.score <= 35 and c.label in ("VERY_LOW", "LOW") and any("COMPANY_SPECIFIC_DOMINANT" in x["reason"] for x in c.caps)
    only_items = compute_confidence(stretched(-85), ev(cause="MIXED", company_down=1, strength="STRONG"))
    assert only_items.score <= 40
    # the same company evidence pointing the OTHER way does not confirm a downside move
    contrary = compute_confidence(stretched(-85), ev(cause="MIXED", company_up=2, strength="STRONG"))
    assert contrary.score > only_items.score


def test_E17_negative_guidance_keeps_confidence_low():
    c = compute_confidence(stretched(-85), ev(cause="MARKET_WIDE_DOMINANT", explained_share=0.9, guidance_down=1, strength="STRONG"))
    assert c.score <= 30 and any("guidance" in x["reason"] for x in c.caps)
    up = compute_confidence(stretched(85), ev(cause="NO_MATERIAL_DECLINE", guidance_up=1, strength="STRONG"))   # upside move confirmed by upward guidance
    assert up.score <= 30


def test_E18_market_driven_cause_can_increase_confidence():
    low = compute_confidence(stretched(-80), ev(cause="NOT_SEPARABLE", explained_share=None))
    mkt = compute_confidence(stretched(-80), ev(cause="MARKET_WIDE_DOMINANT", explained_share=0.8))
    assert mkt.score > low.score and "MARKET_WIDE_DOMINANT" in " ".join(mkt.reasons) and mkt.contributions["sector_explained_share"] == 12
    # high stretch alone must not produce high confidence: the magnitude term is worth at most +5
    a, b = compute_confidence(stretched(-45), ev()), compute_confidence(stretched(-98), ev())
    assert abs(b.score - a.score) <= 5


def test_E19_stale_evidence_caps_confidence():
    fresh = compute_confidence(stretched(-80), ev(age_days=1, cause="MARKET_WIDE_DOMINANT", explained_share=0.8, strength="STRONG"))
    stale = compute_confidence(stretched(-80), ev(age_days=15, cause="MARKET_WIDE_DOMINANT", explained_share=0.8, strength="STRONG"))
    very = compute_confidence(stretched(-80), ev(age_days=45, cause="MARKET_WIDE_DOMINANT", explained_share=0.8, strength="STRONG"))
    assert fresh.score > stale.score >= very.score and stale.score <= 60 and very.score <= 45
    assert any("days old" in x["reason"] for x in very.caps)


def test_E20_contradictions_reduce_confidence():
    clean = compute_confidence(stretched(-80), ev(contradictions=0))
    some = compute_confidence(stretched(-80), ev(contradictions=3))
    many = compute_confidence(stretched(-80), ev(contradictions=9))
    assert clean.score - some.score == 15 and clean.score - many.score == 20 and many.contributions["contradictions"] == -20


def test_E21_stretch_scores_never_leave_the_bounds():
    rng = random.Random(7)
    for i in range(60):
        bars = walk(i, n=rng.choice([80, 150, 300]), vol=rng.choice([0.003, 0.02, 0.08]), drift=rng.uniform(-0.01, 0.01))
        if i % 3 == 0:
            bars = shock(bars, rng.uniform(-0.6, 1.5), rng.uniform(0, 12), days=rng.choice([1, 3, 7]))
        r = ppe(bars, benchmark=bench(i + 1000) if i % 2 else None)
        s = r.stretch
        if s.score is not None:
            assert -100 <= s.score <= 100 and -100 <= s.raw_score <= 100 and s.label == stretch_label(s.score)
        for c in s.components.values():
            assert c.score is None or -100 <= c.score <= 100


def test_E22_confidence_never_leaves_the_bounds():
    rng = random.Random(11)
    causes = ("MARKET_WIDE_DOMINANT", "COMPANY_SPECIFIC_DOMINANT", "MIXED", "NOT_SEPARABLE", "NO_MATERIAL_DECLINE", "NOT_AVAILABLE", None)
    statuses = ("SUPPORTED", "PARTIAL", "NOT_SUPPORTED", "NOT_AVAILABLE")
    for _ in range(300):
        e = ev(cause=rng.choice(causes), explained_share=rng.choice([None, 0.0, 0.5, 1.0, 1.7]), strength=rng.choice(("WEAK", "MODERATE", "STRONG", None)),
               age_days=rng.choice([0, 3, 4, 10, 11, 30, 31, 400]), rungs={str(k): rng.choice(statuses) for k in range(1, 7)},
               company_down=rng.randint(0, 5), company_up=rng.randint(0, 5), guidance_down=rng.randint(0, 2), guidance_up=rng.randint(0, 2),
               supply_down=rng.randint(0, 3), supply_up=rng.randint(0, 3), contradictions=rng.randint(0, 10), n_items=rng.randint(0, 40))
        c = compute_confidence(stretched(rng.choice([-100, -80, -41, -40, 0, 40, 41, 75, 100])), e)
        if c.status == "AVAILABLE":
            assert isinstance(c.score, int) and 0 <= c.score <= 100 and c.label == confidence_label(c.score)
        else:
            assert c.score is None


def test_E23_deterministic_repeatability():
    bars, b, e = shock(walk(3), -0.14, 3.0), bench(5), ev()
    a1, a2 = ppe(bars, e, b).model_dump(), ppe(bars, e, b).model_dump()
    assert a1 == a2
    shuffled = list(reversed(bars))                                              # input order never matters: the engine sorts by date
    assert ppe(shuffled, e, b).stretch.model_dump() == ppe(bars, e, b).stretch.model_dump()


def test_E24_no_nan_or_infinity_in_any_output():
    cases = [calm(), shock(calm(), -0.99, 50.0), shock(calm(), 9.0, 0.0), [DailyBar(d, 100.0, 100.0, 100.0, 100.0, 1e6) for d in sessions(300)],
             walk(5, vol=0.0001), walk(6, vol=0.3), calm(66), calm(volume=None)]
    for bars in cases:
        d = ppe(bars, ev(), bench(2)).model_dump()
        text = json.dumps(d, allow_nan=False)
        assert "NaN" not in text and "Infinity" not in text


def test_E25_serialisation_is_stable_and_has_the_contract_keys():
    r = ppe(shock(walk(3), -0.14, 3.0), ev(), bench(5))
    d = r.model_dump()
    assert set(d) == {"version", "symbol", "as_of", "status", "stretch", "confidence", "sources", "weights", "disclaimer", "note"}
    assert set(d["stretch"]) == {"status", "score", "raw_score", "label", "coverage_weight", "direction", "components", "key_factors", "note"}
    assert set(d["confidence"]) == {"status", "score", "label", "reasons", "contributions", "caps", "evidence"}
    assert set(d["stretch"]["components"]) == set(WEIGHTS) and d["disclaimer"] == DISCLAIMER and d["version"] == "ppe_v1"
    assert json.dumps(d, sort_keys=True) == json.dumps(ppe(shock(walk(3), -0.14, 3.0), ev(), bench(5)).model_dump(), sort_keys=True)
    assert PPEResult.model_validate(json.loads(json.dumps(d))) == r


def test_E26_bands_have_explicit_symmetric_boundaries():
    assert stretch_label(-100) == "EXTREME_DOWNSIDE" and stretch_label(-75.1) == "EXTREME_DOWNSIDE" and stretch_label(-75.0) == "DOWNSIDE_STRETCHED"
    assert stretch_label(-40.1) == "DOWNSIDE_STRETCHED" and stretch_label(-40.0) == "NORMAL" and stretch_label(0) == "NORMAL" and stretch_label(40.0) == "NORMAL"
    assert stretch_label(40.1) == "UPSIDE_STRETCHED" and stretch_label(75.0) == "UPSIDE_STRETCHED" and stretch_label(75.1) == "EXTREME_UPSIDE" and stretch_label(None) is None
    assert [confidence_label(x) for x in (0, 24, 25, 49, 50, 69, 70, 84, 85, 100)] == ["VERY_LOW", "VERY_LOW", "LOW", "LOW", "MODERATE", "MODERATE", "HIGH", "HIGH", "VERY_HIGH", "VERY_HIGH"]
    assert compute_confidence(stretched(-40), ev()).status == "NOT_APPLICABLE" and compute_confidence(stretched(-41), ev()).status == "AVAILABLE"
    assert sum(WEIGHTS.values()) == pytest.approx(1.0)


# ================================================================== lookahead L1–L5 =============================================

def test_L1_only_bars_at_or_before_T_are_used():
    bars = shock(walk(3), -0.14, 3.0)
    T = bars[-1].date
    future = bars + [DailyBar(T + timedelta(days=k), 1.0, 1.0, 1.0, 1.0, 1e9) for k in (1, 2, 3)]   # absurd future bars
    assert point_in_time(future, T) == bars
    a = compute_ppe(MarketInputs("TEST", bars, T, benchmark=bench(5), mode="replay"), None, now_fn=lambda: 0.0)
    b = compute_ppe(MarketInputs("TEST", future, T, benchmark=bench(5), mode="replay"), None, now_fn=lambda: 0.0)
    assert a.stretch.model_dump() == b.stretch.model_dump() and b.sources.bars_through == T.isoformat() and b.sources.sessions_used == len(bars)


def test_L2_future_bars_do_not_alter_the_score_at_T():
    from ppe.replay import replay_symbol
    bars = walk(8, n=200)
    rows_a = replay_symbol("TEST", bars, bench(9), packets_by_symbol=None)
    crashed = shock(bars, -0.5, 10.0, days=5)                                      # the last 5 sessions rewritten
    rows_b = replay_symbol("TEST", crashed, bench(9), packets_by_symbol=None)
    cut = bars[-6].date
    sa = {r.session: (r.stretch, r.components) for r in rows_a if date.fromisoformat(r.session) <= cut}
    sb = {r.session: (r.stretch, r.components) for r in rows_b if date.fromisoformat(r.session) <= cut}
    assert sa and sa == sb                                                          # every score at or before the cut is identical
    assert [r.stretch for r in rows_b][-1] < [r.stretch for r in rows_a][-1]           # only the rewritten sessions differ


def test_L3_future_evidence_packets_are_invisible_at_T():
    from ppe.replay import replay_symbol
    bars = shock(walk(8, n=200), -0.15, 3.0)
    T = bars[-1].date
    created_after = datetime(T.year, T.month, T.day, tzinfo=timezone.utc) + timedelta(days=2)
    created_before = datetime(T.year, T.month, T.day, tzinfo=timezone.utc) - timedelta(days=2)
    snap_after = ev(created_at=created_after.isoformat(), cause="MARKET_WIDE_DOMINANT", explained_share=0.9)
    snap_before = ev(created_at=created_before.isoformat(), cause="MARKET_WIDE_DOMINANT", explained_share=0.9)
    rows = replay_symbol("TEST", bars, None, start=T, end=T, packets_by_symbol={"TEST": [(created_after, snap_after)]})
    assert rows[-1].confidence_status == "EVIDENCE_NOT_AVAILABLE"
    rows = replay_symbol("TEST", bars, None, start=T, end=T, packets_by_symbol={"TEST": [(created_after, snap_after), (created_before, snap_before)]})
    assert rows[-1].confidence_status in ("AVAILABLE", "NOT_APPLICABLE") and rows[-1].confidence is not None
    # the engine itself: a snapshot is just data; latest_snapshot() is what filters by created_at (tested below on a fake store)


def test_L4_outcome_diagnostics_are_separate_from_the_score():
    from ppe.replay import replay_symbol
    bars = walk(4, n=160)
    rows = replay_symbol("TEST", bars, bench(1), packets_by_symbol=None)
    r = rows[-25]
    i = next(i for i, b in enumerate(bars) if b.date.isoformat() == r.session)
    direct = compute_ppe(MarketInputs("TEST", bars[:i + 1], bars[i].date, benchmark=Benchmark("SOXX", "SECTOR_ETF", [b for b in bench(1).bars if b.date <= bars[i].date], label="SOXX"), mode="replay"), None, now_fn=lambda: 0.0)
    assert r.stretch == direct.stretch.score                                        # the row's score is exactly the point-in-time computation
    assert r.fwd["1"] == pytest.approx(bars[i + 1].close / bars[i].close - 1, abs=1e-6) and r.fwd["20"] is not None and r.mfe_20 >= 0 >= r.mae_20
    moved = list(bars)
    for k in range(i + 1, len(moved)):
        b = moved[k]
        moved[k] = DailyBar(b.date, b.open * 0.5, b.high * 0.5, b.low * 0.5, b.close * 0.5, b.volume)
    r2 = [x for x in replay_symbol("TEST", moved, bench(1), packets_by_symbol=None) if x.session == r.session][0]
    assert r2.stretch == r.stretch and r2.fwd["1"] != r.fwd["1"]                     # future bars change the diagnostics, never the score


def test_L5_sector_comparison_uses_the_same_sessions():
    stock = shock(calm(), -0.12, 1.0)
    b = calm_bench()
    with_future = Benchmark("SOXX", "SECTOR_ETF", b.bars + [DailyBar(END + timedelta(days=3), 1, 1, 1, 1, 1)], label=b.label)
    assert ppe(stock, benchmark=b).stretch.model_dump() == ppe(stock, benchmark=with_future).stretch.model_dump()
    lagging = Benchmark("SOXX", "SECTOR_ETF", b.bars[:-2], label=b.label)           # no benchmark bar for the evaluated session at all
    c = ppe(stock, benchmark=lagging).stretch.components["relative_sector_move"]
    assert c.status == "NOT_AVAILABLE" and c.code == "NO_BENCHMARK"
    gap = Benchmark("SOXX", "SECTOR_ETF", b.bars[:-2] + b.bars[-1:], label=b.label)  # has the session but lacks the latest prior session
    c = ppe(stock, benchmark=gap).stretch.components["relative_sector_move"]
    assert c.status == "NOT_AVAILABLE" and c.code == "BENCHMARK_NOT_ALIGNED"


# ================================================================== evidence snapshot ===========================================

def _packet(created="2026-09-20T10:00:00+00:00", **over):
    p = {"packet_id": "f" * 32, "status": "COMPLETED", "created_at": created, "as_of_date": created[:10], "strength": "MODERATE", "n_items": 4,
         "cause": {"verdict": "MIXED", "inputs": {"explained_share": 0.42}},
         "ladder": [{"rung": n, "name": "x", "status": s} for n, s in ((1, "SUPPORTED"), (2, "PARTIAL"), (3, "NOT_AVAILABLE"), (4, "NOT_SUPPORTED"), (5, "PARTIAL"), (6, "NOT_SUPPORTED"))]}
    p.update(over)
    return p


ITEMS = [{"category": "COMPANY_SPECIFIC", "kind": "EARNINGS", "direction": "DOWN", "epistemic": "FACT", "source_tier": 1, "role": "company", "contradiction": 0},
         {"category": "COMPANY_CONFIRMATION", "kind": "GUIDANCE", "direction": "DOWN", "epistemic": "INFERENCE", "source_tier": 1, "role": "company", "contradiction": 1},
         {"category": "COMPANY_SPECIFIC", "kind": "HEADLINE", "direction": "DOWN", "epistemic": "UNCONFIRMED", "source_tier": 5, "role": "social", "contradiction": 0},
         {"category": "COMPANY_CONFIRMATION", "kind": "METRIC", "direction": "DOWN", "epistemic": "FACT", "source_tier": 1, "role": "market", "contradiction": 0},
         {"category": "INDUSTRY_SUPPLY_CHAIN", "kind": "FILING", "direction": "UP", "epistemic": "FACT", "source_tier": 1, "role": "supplier", "contradiction": 1},
         {"category": "MARKET_WIDE", "kind": "METRIC", "direction": "UP", "epistemic": "FACT", "source_tier": 1, "role": "market", "contradiction": 0}]


def test_evidence_snapshot_counts_only_supported_non_computed_items_by_direction():
    s = snapshot_from_packet(_packet(), ITEMS, date(2026, 9, 28))
    assert s.company_down == 2 and s.guidance_down == 1 and s.supply_up == 1 and s.company_up == 0 and s.contradictions == 2
    assert s.age_days == 8 and s.cause == "MIXED" and s.explained_share == 0.42 and s.rungs["5"] == "PARTIAL" and s.strength == "MODERATE" and s.n_items == 4
    assert snapshot_from_packet(_packet(cause={"verdict": "NOT_SEPARABLE", "inputs": {"explained_share": None}}), [], date(2026, 9, 28)).explained_share is None


def test_latest_snapshot_is_point_in_time_and_skips_failed_packets():
    class FakeStore:
        def __init__(self, packets, items):
            self._p, self._i = packets, items

        def exists(self):
            return True

        def packets(self, limit=50, ticker=None):
            return [p for p in self._p if p["ticker"] == ticker][:limit]

        def items(self, pid):
            return self._i.get(pid, [])
    p1 = _packet("2026-09-10T10:00:00+00:00", packet_id="1" * 32, ticker="SNDK", strength="WEAK")
    p2 = _packet("2026-09-20T10:00:00+00:00", packet_id="2" * 32, ticker="SNDK", strength="STRONG")
    p3 = _packet("2026-09-25T10:00:00+00:00", packet_id="3" * 32, ticker="SNDK", status="FAILED")
    store = FakeStore([p3, p2, p1], {"1" * 32: ITEMS[:1], "2" * 32: ITEMS})
    assert latest_snapshot("sndk", datetime(2026, 9, 28, tzinfo=timezone.utc), store).packet_id == "2" * 32          # FAILED is skipped
    assert latest_snapshot("SNDK", datetime(2026, 9, 15, tzinfo=timezone.utc), store).packet_id == "1" * 32          # later packets invisible
    assert latest_snapshot("SNDK", datetime(2026, 9, 1, tzinfo=timezone.utc), store) is None
    assert latest_snapshot("NVDA", datetime(2026, 9, 28, tzinfo=timezone.utc), store) is None

    class Empty:
        def exists(self):
            return False
    assert latest_snapshot("SNDK", datetime(2026, 9, 28, tzinfo=timezone.utc), Empty()) is None


def test_latest_snapshot_reads_a_real_stage54_store(tmp_path, monkeypatch):
    import evidence_fakes as F
    from evidence import engine as EN
    from evidence import net
    from evidence.store import EvidenceStore
    monkeypatch.setattr(config, "EVIDENCE_NETWORK_ENABLED", True)
    monkeypatch.setattr(net, "WIRE", F.FakeWire())
    monkeypatch.setattr(config, "SEC_USER_AGENT", F.SEC_UA)
    monkeypatch.setattr(config, "EVIDENCE_IR_DOMAINS", F.IR_DOMAINS)
    store = EvidenceStore(tmp_path / "ev.db")
    out = EN.build_packet(F.request(), adapters=F.adapters(), now_fn=F.now_fn, store=store)
    snap = latest_snapshot("SNDK", F.NOW + timedelta(days=2), store)
    assert snap is not None and snap.packet_id == out["packet"]["packet_id"] and snap.age_days == 2 and snap.strength in ("WEAK", "MODERATE", "STRONG")
    assert set(snap.rungs) == {"1", "2", "3", "4", "5", "6"} and snap.n_items == out["packet"]["n_items"] and snap.cause is not None
    assert latest_snapshot("SNDK", F.NOW - timedelta(seconds=1), store) is None                     # one second before creation: invisible
    assert len(store.packets(limit=10)) == 1                                                       # reading never writes


# ================================================================== replay tool =================================================

def _bar_store(path: Path, series):
    conn = sqlite3.connect(str(path))
    conn.executescript("""
    CREATE TABLE historical_bar_datasets (dataset_id TEXT PRIMARY KEY, symbol TEXT NOT NULL, source TEXT NOT NULL, feed TEXT NOT NULL, adjustment TEXT NOT NULL,
      timeframe TEXT NOT NULL, requested_start TEXT NOT NULL, requested_end TEXT NOT NULL, first_session TEXT, last_session TEXT, bar_count INTEGER NOT NULL,
      content_hash TEXT NOT NULL, fetched_at TEXT NOT NULL);
    CREATE TABLE historical_daily_bars (dataset_id TEXT NOT NULL, session_date TEXT NOT NULL, bar_timestamp TEXT NOT NULL, open REAL NOT NULL, high REAL NOT NULL,
      low REAL NOT NULL, close REAL NOT NULL, volume REAL NOT NULL, PRIMARY KEY (dataset_id, session_date));""")
    for i, (sym, bars) in enumerate(series.items()):
        ds = f"{i:032x}"
        conn.execute("INSERT INTO historical_bar_datasets VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (ds, sym, "test", "iex", "all", "1Day", bars[0].date.isoformat(), bars[-1].date.isoformat(), bars[0].date.isoformat(), bars[-1].date.isoformat(), len(bars), "0" * 64, "2026-09-29T00:00:00Z"))
        conn.executemany("INSERT INTO historical_daily_bars VALUES (?,?,?,?,?,?,?,?)",
                         [(ds, b.date.isoformat(), b.date.isoformat() + "T04:00:00Z", b.open, b.high, b.low, b.close, b.volume or 0.0) for b in bars])
    conn.commit()
    conn.close()


def test_R1_replay_tool_runs_point_in_time_on_a_stored_series(tmp_path):
    from ppe import replay as RP
    series = {"NVDA": shock(walk(21, n=160), -0.2, 4.0, days=3), "AMD": walk(22, n=160), "MU": walk(23, n=160), "AVGO": walk(24, n=160), "SPY": walk(25, n=160, vol=0.008)}
    db = tmp_path / "bars.db"
    _bar_store(db, series)
    assert RP.stored_symbols(str(db)) == ["AMD", "AVGO", "MU", "NVDA", "SPY"]
    summary = RP.run(str(db), tmp_path / "out", symbols=["NVDA", "AMD"], packets_by_symbol={})
    assert summary["observations"] == 2 * (160 - RP.FIRST_EVAL_SESSIONS) and summary["symbols"] == ["AMD", "NVDA"]
    assert summary["per_symbol"]["NVDA"]["benchmark_kind"] == "PEER_BASKET" and summary["per_symbol"]["NVDA"]["min_stretch"] <= -75
    assert set(summary["bands"]) == {"EXTREME_DOWNSIDE", "DOWNSIDE_STRETCHED", "NORMAL", "UPSIDE_STRETCHED", "EXTREME_UPSIDE"}
    assert "OUTCOME_DIAGNOSTICS" in summary and "Not used to compute or tune PPE" in summary["OUTCOME_DIAGNOSTICS"]["note"]
    assert summary["confidence"] == {"EVIDENCE_NOT_AVAILABLE": summary["observations"]}
    rows = [json.loads(l) for l in (tmp_path / "out" / "ppe_replay_rows.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(rows) == summary["observations"] and all(-100 <= r["stretch"] <= 100 for r in rows if r["stretch"] is not None)
    text = (tmp_path / "out" / "ppe_replay_summary.json").read_text(encoding="utf-8")
    assert "NaN" not in text and "Infinity" not in text and "weights" in summary


# ================================================================== Trader View U1–U9 ===========================================

def make_stream(**kw):
    market = kw.pop("market", None) or SimulatedMarket(seed=11, start_time=T0)
    return TerminalStream(market, kw.pop("positions", None) or FakePositions(), kw.pop("research", None) or FakeResearch(),
                          watchlist_fn=kw.pop("watchlist_fn", lambda: list(WL)), tick_seconds=1.0, allow_manual_tick=True, run_loop=False,
                          now_fn=lambda: T0, **kw)


@pytest.fixture
def ts():
    return make_stream()


@pytest.fixture
def api(ts, monkeypatch):
    monkeypatch.setitem(RT.STREAM, "instance", ts)
    from fastapi.testclient import TestClient
    from api.server import app
    return TestClient(app)


def test_U1_state_and_route_carry_the_ppe_result(api, ts):
    j = api.get("/api/terminal/state/NVDA?candles=0").json()["ppe"]
    assert j["status"] == "SIMULATED_DATA" and j["symbol"] == "NVDA" and j["disclaimer"] == DISCLAIMER and set(j["stretch"]["components"]) == set(WEIGHTS)
    assert j["sources"]["mode"] == "simulated" and j["sources"]["benchmark_kind"] == "SECTOR_ETF" and j["sources"]["benchmark"] == "SOXX"
    r = api.get("/api/terminal/ppe/nvda")
    assert r.status_code == 200 and r.json()["symbol"] == "NVDA" and r.json()["stretch"]["status"] == "AVAILABLE"
    assert api.get("/api/terminal/ppe/12345").status_code == 400

    class NoData(SimulatedMarket):
        def resolve(self, symbol):
            s = super().resolve(symbol)
            if s == "ZZZZ":
                raise UnknownSymbol("No market data for 'ZZZZ'.")
            return s
    ts.market = NoData(seed=1, start_time=T0)
    assert api.get("/api/terminal/ppe/zzzz").status_code == 404


def test_U2_ppe_follows_the_active_ticker_and_switches(api, ts):
    with api.websocket_connect(WS_PATH) as ws:
        hello_of(ws)
        a = burst(ws, "AAPL")
        state_a = next(m for m in a if m["type"] == "state")
        assert state_a["ppe"]["symbol"] == "AAPL" and state_a["ppe"]["status"] == "SIMULATED_DATA"
        b = burst(ws, "NVDA")
        state_b = next(m for m in b if m["type"] == "state")
        assert state_b["ppe"]["symbol"] == "NVDA" and state_b["ppe"]["stretch"]["score"] != state_a["ppe"]["stretch"]["score"]
        ws.send_json({"type": "refresh"})
        got = marker(ws)
        assert [m["type"] for m in got] == ["position", "evidence", "ppe", "demand", "setup"] and got[2]["symbol"] == "NVDA" and got[2]["disclaimer"] == DISCLAIMER   # "demand": Stage 5.7; "setup": Stage 5.8
        assert all(m.get("symbol") != "AAPL" for m in got)


def test_U3_stale_ticker_ppe_messages_are_ignored():
    assert not accepts_message({"type": "ppe", "symbol": "SNDK", "status": "AVAILABLE"}, "NVDA") and accepts_message({"type": "ppe", "symbol": "NVDA"}, "NVDA")
    assert not accepts_message({"type": "ppe"}, "NVDA")
    assert '"ppe"' in CORE.split("SYMBOL_BOUND = new Set(")[1].split(")")[0] and 'case "ppe": renderPPE(m); break;' in JS


def test_U4_missing_ppe_displays_not_available(ts):
    class Boom:
        def result(self, symbol):
            raise RuntimeError("bars down")

        def invalidate(self, symbol=None):
            pass
    s = make_stream(ppe_provider=Boom())
    p = s.state("NVDA", with_candles=False).ppe
    assert p.stretch.status == "NOT_AVAILABLE" and p.stretch.score is None and p.confidence.score is None and "PPE unavailable" in p.note
    assert "NOT AVAILABLE" in JS and 'if (!p || !p.stretch)' in JS


def test_U5_simulated_data_is_clearly_labelled(ts):
    p = ts.state("SNDK", with_candles=False).ppe
    assert p.status == "SIMULATED_DATA" and p.note.startswith("SIMULATED DATA") and "simulator" in p.sources.market_data and p.sources.mode == "simulated"
    assert 'sim ? `<span class="tv-tag warn">SIMULATED DATA</span>`' in JS


def test_U6_disclaimer_is_visible_everywhere(ts):
    p = ts.state("SNDK", with_candles=False).ppe
    assert p.disclaimer == DISCLAIMER == "PPE Confidence measures confidence in the overreaction classification, not the probability that the stock will rise."
    assert "${esc(p.disclaimer || \"\")}" in JS and DISCLAIMER in (ROOT / "DESIGN_56_PPE_ENGINE.md").read_text(encoding="utf-8")


def code_only(text: str) -> str:
    """Source without docstrings, comments and the two sanctioned disclaimer sentences."""
    from ppe import NOT_A_SIGNAL
    tree = ast.parse(text)
    lines = text.splitlines(keepends=True)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.body and isinstance(node.body[0], ast.Expr) \
                and isinstance(getattr(node.body[0], "value", None), ast.Constant) and isinstance(node.body[0].value.value, str):
            for ln in range(node.body[0].lineno - 1, node.body[0].end_lineno):
                lines[ln] = "\n"
        if isinstance(node, ast.Assign) and any(isinstance(x, ast.Name) and x.id in ("DISCLAIMER", "NOT_A_SIGNAL") for x in node.targets):
            for ln in range(node.lineno - 1, node.end_lineno):                          # the two sanctioned sentences (source literals may be split)
                lines[ln] = "\n"
    return re.sub(r"#.*", "", "".join(lines)).replace(DISCLAIMER, "").replace(NOT_A_SIGNAL, "")


def test_U7_no_buy_sell_recommendation_or_probability_language():
    src = "".join(code_only((ROOT / f).read_text(encoding="utf-8")) for f in PPE_FILES)
    assert not re.search(r"(?i)\b(buy|sell|recommend\w*)\b", src), "trading verbs in ppe sources"
    assert not re.search(r"(?i)expected return|reversal probability|probability of (a )?(rise|recovery|reversal)|will rise|find(s)? the bottom|bottom(s)? (is|are) in|price target", src)
    assert ppe_source().count("probability that the stock will rise") == 1              # only inside the disclaimer constant
    assert not re.search(r"(?i)overreaction.*buy|buy.*overreaction|recovery probability", JS)
    r = ppe(shock(calm(), -0.18, 4.0), ev()).model_dump()
    keys = set()

    def walk_keys(o):
        if isinstance(o, dict):
            for k, v in o.items():
                keys.add(k.lower())
                walk_keys(v)
        elif isinstance(o, list):
            for v in o:
                walk_keys(v)
    walk_keys(r)
    assert not any(k in keys for k in ("buy", "sell", "recommendation", "action", "expected_return", "probability", "target", "stop", "signal"))


def test_U8_prepare_buy_sell_remain_disabled_with_ppe_present(api, ts):
    st = ts.state("NVDA", with_candles=False)
    assert st.ppe.stretch.status == "AVAILABLE"
    acts = {a.id: a for a in st.actions}
    assert acts["prepare_buy"].enabled is False and acts["prepare_sell"].enabled is False and acts["prepare_buy"].status == "PREVIEW_NOT_CONNECTED"
    from api.server import app
    mine = {p: set(o) for p, o in app.openapi()["paths"].items() if p.startswith("/api/terminal")}
    assert mine["/api/terminal/ppe/{symbol}"] == {"get"} and all(o == {"get"} for o in mine.values())
    for method in ("POST", "PUT", "DELETE", "PATCH"):
        assert api.request(method, "/api/terminal/ppe/NVDA", json={"side": "buy"}).status_code == 405
    assert "ppe" not in JS.split("function renderActions")[1].split("function renderStatusLine")[0]   # actions never read PPE


def test_U9_ppe_cadence_is_bounded_and_never_per_quote_tick(api):
    prov = PPEProvider(SimulatedMarket(seed=11, start_time=T0), evidence_fn=lambda s, t: None, result_cache_s=0, now_fn=lambda: T0)
    s = make_stream(market=prov.market, ppe_provider=prov, ppe_poll_seconds=5)
    assert s.ppe_every == 5 and s.cadence()["ppe_s"] == 5.0
    from fastapi.testclient import TestClient
    from api.server import app
    import api.routes.trader_view as R
    R.STREAM["instance"] = s
    try:
        with TestClient(app).websocket_connect(WS_PATH) as ws:
            hello_of(ws)
            burst(ws, "NVDA")
            n0 = prov.computations
            assert n0 == 1                                                          # one computation on subscribe (inside state)
            for i in range(1, 11):
                ws.send_json({"type": "tick", "now": T0 + i})
            got = marker(ws)
            types = [m["type"] for m in got]
            assert types.count("quote") == 10 and types.count("ppe") == 2 and prov.computations == n0 + 2       # ticks 5 and 10 only
            assert all(m["symbol"] == "NVDA" for m in got if m["type"] == "ppe")
    finally:
        R.STREAM["instance"] = None
    cached = PPEProvider(SimulatedMarket(seed=11, start_time=T0), evidence_fn=lambda s, t: None, result_cache_s=60, now_fn=lambda: T0)
    cached.result("NVDA")
    cached.result("NVDA")
    assert cached.computations == 1                                                   # the result cache absorbs repeated reads
    cached.invalidate("NVDA")
    cached.result("NVDA")
    assert cached.computations == 2


# ================================================================== safety S1–S9 ================================================

FORBIDDEN = ("alpaca.trading", "paper", "robin_stocks", "robinhood_sync", "rh_gateway", "agents", "anthropic", "ai_explain", "evidence.engine", "evidence.explain",
             "evidence.net", "evidence.sources", "requests", "urllib", "subprocess", "portfolio", "research_flow", "rotation", "forward", "notifications", "insights")


def test_S1_ppe_imports_no_order_writer_broker_client_or_model():
    for rel in PPE_FILES:
        text = (ROOT / rel).read_text(encoding="utf-8")
        names = []
        for n in ast.walk(ast.parse(text)):
            if isinstance(n, ast.Import):
                names += [a.name for a in n.names]
            elif isinstance(n, ast.ImportFrom):
                names += [n.module or ""] + [f"{n.module}.{a.name}" if n.module else a.name for a in n.names]
        for name in names:
            assert not any(name == f or name.startswith(f + ".") for f in FORBIDDEN), (rel, name)
        assert not re.search(r"TradingClient|TradingStream|/v2/orders|alpaca_order|W\.submit|\.submit\(|place_order|cancel_order|messages\.create|get_provider|"
                             r"one_request|build_packet|insert_packet|insert_explanation|\beval\(|\bexec\(|__import__|os\.system", text), rel
    import ppe.confidence, ppe.engine, ppe.evidence_context, ppe.features, ppe.inputs, ppe.models, ppe.replay, ppe.sectors, ppe.stretch  # noqa: E401
    import trader_view.ppe_provider
    for mod in (ppe.confidence, ppe.engine, ppe.evidence_context, ppe.features, ppe.inputs, ppe.models, ppe.replay, ppe.sectors, ppe.stretch, trader_view.ppe_provider):
        for v in vars(mod).values():
            if isinstance(v, types.ModuleType):
                assert not any(v.__name__ == f or v.__name__.startswith(f + ".") for f in FORBIDDEN), (mod.__name__, v.__name__)


def test_S2_ppe_cannot_submit_orders_or_enable_execution(api, ts):
    from api.server import app
    for path, ops in app.openapi()["paths"].items():
        if path.startswith("/api/terminal"):
            assert set(ops) == {"get"}, path
    assert JS.count("ws.send(") == 1 and not re.search(r"""method\s*:\s*["'](POST|PUT|PATCH|DELETE)["']""", JS)
    r = api.get("/api/terminal/ppe/NVDA").json()
    assert "actions" not in r and "order" not in json.dumps(r).lower()


def test_S3_no_robinhood_or_alpaca_trading_path_from_ppe():
    src = ppe_source().lower()
    for tok in ("robinhood", "robin_stocks", "rh_gateway", "alpaca.trading", "tradingclient", "/v2/orders", "api.alpaca.markets", "paper-api"):
        assert tok not in src, tok
    assert "fetch_daily_bars" in (ROOT / "trader_view" / "market_real.py").read_text(encoding="utf-8")      # the only market read is the existing data client
    assert "get_data_client" not in ppe_source()                                                               # the engine itself never opens a client


def test_S4_no_real_model_call_while_computing_ppe(api, ts, monkeypatch):
    import anthropic

    def boom(self, *a, **k):
        raise AssertionError("a model client was constructed by PPE")
    monkeypatch.setattr(anthropic.Anthropic, "__init__", boom)
    assert api.get("/api/terminal/ppe/NVDA").status_code == 200 and api.get("/api/terminal/state/SNDK").status_code == 200
    with api.websocket_connect(WS_PATH) as ws:
        hello_of(ws)
        burst(ws, "SNDK")
        ws.send_json({"type": "refresh"})
        marker(ws)
    assert not re.search(r"anthropic|get_provider|\.analyze\(|LLMProvider|technical_agent", ppe_source())


def test_S5_ppe_never_triggers_stage54_research_or_writes(api, ts, monkeypatch):
    import conftest
    from evidence import engine as EN
    from evidence.store import EvidenceStore
    monkeypatch.setattr(EN, "build_packet", lambda *a, **k: (_ for _ in ()).throw(AssertionError("Stage 5.4 research was triggered by PPE")))
    monkeypatch.setattr(EvidenceStore, "insert_packet", lambda *a, **k: (_ for _ in ()).throw(AssertionError("PPE wrote an evidence packet")))
    before = hashlib.sha256(conftest.TEST_DB.read_bytes()).hexdigest() if conftest.TEST_DB.exists() else None
    real = TerminalStream(SimulatedMarket(seed=3, start_time=T0), NullPositionAdapter(), NullResearchAdapter(), watchlist_fn=lambda: [], run_loop=False, now_fn=lambda: T0)
    for sym in ("NVDA", "SNDK", "AAPL"):
        p = real.ppe_result(sym)                                                    # the production provider: reads the (isolated) evidence store
        assert p.confidence.status in ("EVIDENCE_NOT_AVAILABLE", "NOT_APPLICABLE", "AVAILABLE")
    assert api.get("/api/terminal/ppe/NVDA").status_code == 200
    after = hashlib.sha256(conftest.TEST_DB.read_bytes()).hexdigest() if conftest.TEST_DB.exists() else None
    assert before == after


def test_S6_no_buy_sell_or_probability_field_in_the_contract():
    schema = json.dumps(PPEResult.model_json_schema()).lower()
    for tok in ('"buy"', '"sell"', "recommend", "expected_return", "probability_", "prob_rise", '"target"', "stop_loss", "signal"):
        assert tok not in schema, tok


def test_S7_prepare_buttons_cannot_be_enabled_by_any_ppe_result(ts):
    from trader_view.models import default_actions
    for bars in (shock(calm(), -0.3, 6.0), shock(calm(), 0.3, 6.0), calm()):
        ppe(bars, ev(cause="MARKET_WIDE_DOMINANT", explained_share=1.0, strength="STRONG", rungs={"4": "SUPPORTED", "5": "SUPPORTED", "6": "SUPPORTED"}))
        assert all(a.enabled is False for a in default_actions() if a.id in ("prepare_buy", "prepare_sell", "add_alert"))
    st = ts.state("NVDA", with_candles=False)
    assert [a.enabled for a in st.actions if a.id.startswith("prepare")] == [False, False]


def test_S8_ppe_has_no_side_effects_on_config_or_data(ts):
    snapshot = {k: v for k, v in vars(config).items() if k.isupper() and isinstance(v, (int, float, str, tuple))}
    for sym in ("NVDA", "AAPL", "SNDK", "JPM"):
        ts.ppe_result(sym)
    assert {k: v for k, v in vars(config).items() if k.isupper() and isinstance(v, (int, float, str, tuple))} == snapshot
    assert not list((ROOT / "database").glob("*ppe*")) and not list((ROOT / "database").glob("*stretch*"))   # no table, no migration


def test_S9_real_provider_path_fails_closed_without_network(monkeypatch):
    """Real mode without a feed: the provider reports NOT_AVAILABLE, never invents bars, and the live quote path is never required."""
    from trader_view.market_real import RealMarketData
    real = RealMarketData(client_factory=lambda: (_ for _ in ()).throw(RuntimeError("no credentials")), now_fn=lambda: 0.0)
    prov = PPEProvider(real, evidence_fn=lambda s, t: None, result_cache_s=0, now_fn=lambda: T0)
    p = prov.result("NVDA")
    assert p.status == "NOT_AVAILABLE" and p.stretch.score is None and p.sources.mode == "real" and "no completed daily bars" in p.note
    assert session_today(T0) in (date(2025, 10, 8), date(2025, 10, 9))                 # ET session date for the fixed clock
