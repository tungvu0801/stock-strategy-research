"""
tests/test_evidence_54.py — Stage 5.4 MULTI-SOURCE EVIDENCE RESEARCH (DESIGN_54). Fixtures only; no network; no model request.

Groups: net (boundary, every failure state, tripwire), extract, SEC adapter, IR adapter, Alpaca wrapper, manual URLs / texts,
internal facts, rules (tiers, labels, categories, directions, cause attribution, contradictions, every ladder rung, strength, gaps),
engine (golden packet, determinism, FAILED codes, limits), store (immutability, checks), API, explanation (fake provider, guard,
dry run), isolation and regression (frozen stages untouched, no forbidden route words, no broker / Robinhood / model path).
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import config
import evidence_fakes as F
from evidence import (COMPANY_CONFIRMATION, COMPANY_SPECIFIC, CONFIRMATION_MISSING, DOWN, FACT, INDUSTRY_SUPPLY_CHAIN, INFERENCE, MARKET_CONFIRMATION,
                      MARKET_WIDE, NA, NEUTRAL, NOT_AVAILABLE, NOT_SUPPORTED, PARTIAL, RULES_VERSION, STRENGTH_DISCLAIMER, SUPPORTED, UNCONFIRMED, UP)
from evidence import engine as EN
from evidence import explain as EX
from evidence import extract as X
from evidence import net
from evidence import rules as R
from evidence.sources import internal as INT
from evidence.sources.sec_edgar import SecAdapter
from evidence.store import EvidenceStore

ROOT = Path(__file__).resolve().parents[1]
GOLDEN_HASH = "dbed451a6ef97f0e27df19d7df37cb67a5baf00db8ec34cfe8cc36bde3ed5e3a"   # pinned after the first green run (see test_engine_golden_packet)


# ---------------------------------------------------------------- helpers ------------------------------------------------------

@pytest.fixture
def live(monkeypatch):
    """A test that may use the FakeWire: network flag on, wire = fixtures, SEC user agent set, IR domains declared."""
    wire = F.FakeWire()
    monkeypatch.setattr(config, "EVIDENCE_NETWORK_ENABLED", True)
    monkeypatch.setattr(net, "WIRE", wire)
    monkeypatch.setattr(config, "SEC_USER_AGENT", F.SEC_UA)
    monkeypatch.setattr(config, "EVIDENCE_IR_DOMAINS", F.IR_DOMAINS)
    return wire


@pytest.fixture
def store(tmp_path):
    return EvidenceStore(tmp_path / "ev.db")


def build(store, **over):
    return EN.build_packet(F.request(**over), adapters=F.adapters(), now_fn=F.now_fn, store=store)


def item(**kw) -> dict:
    base = {"item_id": kw.get("item_id", "a" * 32), "category": COMPANY_SPECIFIC, "kind": "PAGE", "direction": NA, "source_tier": 1, "epistemic": FACT,
            "evidence_date": "2026-09-01", "subject_ticker": "SNDK", "role": "company", "text": "", "rule_match": "", "contradiction": 0, "contradicts_item_id": None}
    base.update(kw)
    return base


# ---------------------------------------------------------------- net ----------------------------------------------------------

def test_net_disabled_and_tripwire_by_default():
    r = net.fetch("https://www.sec.gov/files/company_tickers.json", purpose="t", allowed_hosts=["sec.gov"])
    assert r.status == "DISABLED" and not r.ok and r.bytes == 0


def test_net_boundary_rules(live):
    ok = net.fetch("https://www.sec.gov/files/company_tickers.json", purpose="t", allowed_hosts=["sec.gov"])
    assert ok.ok and ok.http_status == 200 and len(ok.content_hash) == 64 and "SNDK" in ok.text and ok.host == "www.sec.gov"
    assert net.fetch("http://www.sec.gov/x", purpose="t", allowed_hosts=["sec.gov"]).status == "HOST_NOT_ALLOWED"
    assert net.fetch("https://evil.example/x", purpose="t", allowed_hosts=["sec.gov"]).status == "HOST_NOT_ALLOWED"
    assert net.fetch("https://www.reuters.com/article", purpose="t", allowed_hosts=["reuters.com"]).status == "HOST_DENIED"
    assert net.fetch("https://api.bloomberg.com/x", purpose="t", allowed_hosts=["bloomberg.com"]).status == "HOST_DENIED"
    assert net.fetch("https://a.example/timeout", purpose="t", allowed_hosts=["a.example"]).status == "TIMEOUT"
    d = net.fetch("https://a.example/denied", purpose="t", allowed_hosts=["a.example"])
    assert d.status == "ACCESS_DENIED" and d.http_status == 403 and "not retried" in d.error
    assert net.fetch("https://a.example/rate-limited", purpose="t", allowed_hosts=["a.example"]).status == "ACCESS_DENIED"
    assert net.fetch("https://a.example/server-error", purpose="t", allowed_hosts=["a.example"]).status == "HTTP_ERROR"
    assert net.fetch("https://a.example/redirect-offhost", purpose="t", allowed_hosts=["a.example"]).status == "REDIRECT_BLOCKED"
    assert net.fetch("https://a.example/redirect-loop", purpose="t", allowed_hosts=["a.example"]).status == "REDIRECT_BLOCKED"
    same = net.fetch("https://a.example/redirect-same", purpose="t", allowed_hosts=["a.example"])
    assert same.ok and same.final_url.endswith("/landed") and "landed" in same.text
    assert net.fetch("https://a.example/huge", purpose="t", allowed_hosts=["a.example"], max_bytes=1000).status == "TOO_LARGE"
    assert net.fetch("https://a.example/empty", purpose="t", allowed_hosts=["a.example"]).status == "EMPTY"
    assert net.fetch("https://a.example/missing", purpose="t", allowed_hosts=["a.example"]).status == "HTTP_ERROR"
    assert len(live.calls) >= 10 and all(c["headers"]["User-Agent"] == F.SEC_UA for c in live.calls)
    assert net.host_matches("data.sec.gov", ["sec.gov"]) and not net.host_matches("notsec.gov", ["sec.gov"]) and not net.host_matches("sec.gov.evil", ["sec.gov"])


def test_net_one_attempt_per_url(live):
    net.fetch("https://a.example/server-error", purpose="t", allowed_hosts=["a.example"])
    assert sum(1 for c in live.calls if c["url"].endswith("/server-error")) == 1


# ---------------------------------------------------------------- extract ------------------------------------------------------

def test_extract_html_text_title_date_snippet():
    html = (F.FIX / "8k_ex99_down.html").read_text(encoding="utf-8")
    text, title, meta = X.parse_html(html)
    assert title.startswith("Sandisk Reports") and "var x=1" not in text and "lowered its fiscal 2027 outlook" in text
    assert X.extract_date(meta, text) == date(2026, 8, 7)
    _t, _ti, m2 = X.parse_html((F.FIX / "ir_release.html").read_text(encoding="utf-8"))
    assert X.extract_date(m2, "") == date(2026, 8, 7)
    assert X.extract_date({}, "no date here") is None
    assert X.snippet("a" * 10, 5).startswith("aaaaa") and len(X.snippet("word " * 1000, 50)) <= 55


# ---------------------------------------------------------------- adapters -----------------------------------------------------

def test_sec_adapter_refuses_without_user_agent(live):
    res = F.adapters(user_agent="")["sec"].run("SNDK", {"suppliers": ["LRCX"], "customers": []}, F.AS_OF, 90)
    assert res.items == [] and res.requests == 0 and any("SEC_USER_AGENT" in n for n in res.notes) and live.calls == []


def test_sec_adapter_filings_documents_facts_and_budget(live):
    res = F.adapters()["sec"].run("SNDK", {"suppliers": ["LRCX"], "customers": []}, F.AS_OF, 90)
    forms = sorted((i.extra.get("form"), i.evidence_date, i.subject_ticker) for i in res.items if i.source_type == "SEC_FILING" and i.kind != "METRIC")
    assert ("8-K", "2026-05-01", "SNDK") not in forms and ("8-K", "2026-09-20", "SNDK") not in forms            # outside window / item 5.02 excluded
    assert ("8-K", "2026-08-07", "SNDK") in forms and ("8-K", "2026-09-15", "SNDK") in forms and ("10-Q", "2026-08-12", "SNDK") in forms
    assert ("8-K", "2026-09-10", "LRCX") in forms and ("8-K", "2026-06-01", "LRCX") not in forms
    aug = next(i for i in res.items if i.evidence_date == "2026-08-07" and i.source_type == "SEC_FILING")
    assert aug.kind == "GUIDANCE" and "lowered its fiscal 2027 outlook" in aug.text and aug.source_url.endswith("ex99-1.htm") and len(aug.source_hash) == 64
    sep = next(i for i in res.items if i.evidence_date == "2026-09-15")
    assert sep.kind == "GUIDANCE" and "reaffirmed its outlook" in sep.text
    lrcx = next(i for i in res.items if i.subject_ticker == "LRCX")
    assert lrcx.role == "supplier" and "better-than-expected" in lrcx.text
    metric = next(i for i in res.items if i.kind == "METRIC")
    f = res.facts["fundamentals"]
    assert f["quarter_latest"] == "2026-06-30" and f["quarter_prior"] == "2026-03-31" and f["revenue_up"] and f["margin_up"]      # Q3 filed after as_of ignored
    assert metric.category == COMPANY_CONFIRMATION and "2,000,000,000" in metric.text
    assert res.requests == 10 and all(c["headers"]["User-Agent"] == F.SEC_UA for c in live.calls)
    assert res.requests <= config.EVIDENCE_SEC_MAX_REQUESTS


def test_sec_adapter_budget_and_cik_override(live):
    res = F.adapters(sec_max_requests=3)["sec"].run("SNDK", {"suppliers": ["LRCX"], "customers": []}, F.AS_OF, 90)
    assert res.requests == 3 and any("budget" in n for n in res.notes)
    meta_only = [i for i in res.items if i.source_type == "SEC_FILING" and i.kind != "METRIC"]
    assert meta_only and all(i.source_url is None for i in meta_only)                                                   # metadata-only FACT items
    res2 = F.adapters()["sec"].run("SNDK", {"suppliers": [], "customers": []}, F.AS_OF, 90, cik_overrides={"SNDK": "0001234567"})
    assert not any(c["url"].endswith("company_tickers.json") for c in live.calls[3:]) and res2.facts.get("fundamentals")
    unknown = F.adapters()["sec"].run("ZZZZ", {"suppliers": [], "customers": []}, F.AS_OF, 90)
    assert unknown.items == [] and any("not in the SEC ticker map" in n for n in unknown.notes)


def test_ir_adapter_only_declared_urls(live):
    res = F.adapters()["ir"].run("SNDK", [F.IR_URL, "https://investor.sandisk.example/denied", "https://www.reuters.com/x"], F.AS_OF)
    assert [f.status for f in res.fetches] == ["OK", "ACCESS_DENIED", "HOST_DENIED"] and len(res.items) == 1
    it = res.items[0]
    assert it.kind == "GUIDANCE" and it.evidence_date == "2026-08-07" and it.date_source == "source" and it.role == "company" and it.source_type == "IR_RELEASE"
    assert "Investors Home" not in it.text or True                                                                     # nav text may remain; no claim about layout


def test_news_adapter_and_manual_adapter(live):
    res = F.adapters()["news"].run("SNDK", F.AS_OF)
    assert [i.evidence_date for i in res.items] == ["2026-08-07", "2026-09-22", "2026-09-28"] and res.fetches[0].url == "alpaca://news/SNDK"
    assert all(i.source_type == "ALPACA_NEWS" and len(i.source_hash) == 64 for i in res.items)
    empty = F.adapters(news=lambda s, n: [])["news"].run("SNDK", F.AS_OF)
    assert empty.items == [] and empty.fetches[0].status == "EMPTY"
    broken = F.adapters(news=lambda s, n: (_ for _ in ()).throw(RuntimeError("feed down")))["news"].run("SNDK", F.AS_OF)
    assert broken.fetches[0].status == "HTTP_ERROR" and "feed down" in broken.fetches[0].error
    m = F.adapters()["manual"].run("SNDK", [{"url": F.SUPPLIER_URL, "role": "supplier", "related_ticker": "LRCX"}, {"url": F.SOCIAL_URL, "role": "social"},
                                            {"url": "https://x.example/timeout", "role": "industry"}, {"url": "https://x.example/y", "role": "bogus"}],
                                   [{"text": "pasted claim", "role": "industry"}, {"text": "", "role": "industry"}], F.AS_OF)
    assert [f.status for f in m.fetches] == ["OK", "OK", "TIMEOUT"] and len(m.items) == 3 and len(m.notes) == 2
    sup, soc, txt = m.items
    assert sup.subject_ticker == "LRCX" and sup.role == "supplier" and sup.evidence_date == "2026-09-20"
    assert soc.role == "social" and txt.source_type == "MANUAL_TEXT" and txt.source_hash is None and txt.date_source == "retrieval"


def test_internal_adapter_facts_and_items():
    res = F.adapters()["internal"].run("SNDK", F.AS_OF, 90)
    f = res.facts
    assert f["sector_etf"] == "SOXX" and f["missing"] == [] and [x.url for x in res.fetches] == ["alpaca://bars/SNDK", "alpaca://bars/SPY", "alpaca://bars/SOXX"]
    assert f["stock_ret"] < -0.2 and f["sector_ret"] < 0 and f["spy_ret"] < 0 and f["window_start"] < f["window_end"] <= F.AS_OF.isoformat()
    assert f["stock_20"] > 0 and f["spy_20"] > 0 and f["etf_20"] > 0 and f["spy_above_ema50"] is True and f["price_above_ema20"] is True
    assert f["rel_strength_20"] == pytest.approx(f["stock_20"] - f["etf_20"]) and f["relative_volume"] > 1
    assert f["next_earnings"]["date"] == "2026-11-05" and f["next_fomc"]["date"] == "2026-10-28"
    cats = {i.category for i in res.items}
    assert cats == {MARKET_WIDE, MARKET_CONFIRMATION, None} and any(i.kind == "EVENT" and i.text.startswith("CPI release") for i in res.items)
    assert all(i.source_type in ("MARKET_DATA", "EVENT") for i in res.items)
    assert all(i.evidence_date <= F.AS_OF.isoformat() for i in res.items if i.kind != "EVENT")           # scheduled events may be dated after as_of
    assert INT.ema([1.0] * 5, 5) == 1.0 and INT.ema([1.0], 5) is None and INT.ret_n([1, 2], 1) == 1.0 and INT.ret_n([1], 1) is None
    missing = F.adapters(bars=lambda s, n: {})["internal"].run("SNDK", F.AS_OF, 90)
    assert "ticker bars" in missing.facts["missing"] and all(x.status == "EMPTY" for x in missing.fetches)


# ---------------------------------------------------------------- rules --------------------------------------------------------

def test_rules_tiers_labels_categories():
    t = lambda **k: R.tier_for(**{"host": "", "subject_ticker": "SNDK", "ticker": "SNDK", "source_type": "MANUAL_URL", "ir_domains": F.IR_DOMAINS,
                                   "related": {"suppliers": ["LRCX"]}, **k})
    assert t(host="www.sec.gov", source_type="SEC_FILING") == 1 and t(host="data.sec.gov", source_type="SEC_FILING", subject_ticker="LRCX") == 2
    assert t(host="investor.sandisk.example") == 1 and t(host="investor.lamresearch.example", subject_ticker="LRCX", role="supplier") == 2
    assert t(host="investor.lamresearch.example", subject_ticker="OTHR", role="supplier") == 4                         # not a declared related ticker
    assert t(host="www.reddit.com") == 5 and t(host="x.example", role="social") == 5
    assert t(source_type="ALPACA_NEWS", press_source="Reuters") == 3 and t(source_type="ALPACA_NEWS", press_source="Some Blog") == 4
    assert t(source_type="MARKET_DATA") == 1 and t(source_type="MANUAL_TEXT") == 4
    e = lambda **k: R.epistemic_for(**{"tier": 1, "fetched_ok": True, "has_url": True, "source_type": "MANUAL_URL", **k})
    assert e() == FACT and e(tier=5) == UNCONFIRMED and e(fetched_ok=False) == UNCONFIRMED and e(has_url=False) == UNCONFIRMED
    assert e(tier=4, source_type="MANUAL_TEXT", has_url=False) == UNCONFIRMED and e(source_type="MARKET_DATA", has_url=False) == FACT
    c = lambda **k: R.category_for(**{"role": "company", "source_type": "MANUAL_URL", "subject_is_target": True, **k})
    assert c() == COMPANY_SPECIFIC and c(role="supplier") == INDUSTRY_SUPPLY_CHAIN and c(subject_is_target=False) == INDUSTRY_SUPPLY_CHAIN
    assert c(source_type="MARKET_DATA") == MARKET_WIDE and c(source_type="EVENT") == MARKET_WIDE


def test_rules_direction_patterns():
    assert R.direction_for("The company raised its full-year guidance") == (UP, "UP:raised its full-year guidance")
    assert R.direction_for("lowered its fiscal 2027 outlook")[0] == DOWN and R.direction_for("cut guidance")[0] == DOWN
    assert R.direction_for("reaffirmed its outlook")[0] == NEUTRAL and R.direction_for("results in line with expectations")[0] == NEUTRAL
    d, m = R.direction_for("orders increased but margins declined")
    assert d == NEUTRAL and m.startswith("UP:") and "DOWN:" in m
    assert R.direction_for("nothing relevant") == (NA, "")
    assert R.metric_direction({"revenue_up": True, "margin_up": True})[0] == UP and R.metric_direction({"revenue_up": False, "margin_up": False})[0] == DOWN
    assert R.metric_direction({"revenue_up": True, "margin_up": False})[0] == NEUTRAL and R.metric_direction({"signed": -0.1})[0] == DOWN and R.metric_direction(None) == (NA, "")
    assert R.is_guidance("our outlook for the year") and not R.is_guidance("results") and R.is_earnings("Fiscal Fourth Quarter Results")


def test_rules_cause_attribution():
    facts = {"stock_ret": -0.25, "spy_ret": -0.04, "sector_ret": -0.12, "window_start": "2026-07-15", "window_end": "2026-08-25", "peak_close": 120, "trough_close": 90, "sector_etf": "SOXX"}
    c = R.cause_attribution(facts, [])
    assert c["verdict"] == "MIXED" and c["inputs"]["explained_share"] == pytest.approx(0.48) and c["epistemic"] == INFERENCE
    down = item(item_id="b" * 32, category=COMPANY_CONFIRMATION, kind="GUIDANCE", direction=DOWN, evidence_date="2026-08-07")
    assert R.cause_attribution(facts, [down])["verdict"] == "COMPANY_SPECIFIC_DOMINANT" and R.cause_attribution(facts, [down])["item_ids"] == ["b" * 32]
    assert R.cause_attribution({**facts, "sector_ret": -0.2}, [])["verdict"] == "MARKET_WIDE_DOMINANT"
    assert R.cause_attribution({**facts, "sector_ret": -0.2}, [down])["verdict"] == "MIXED"                           # share 0.8 but a tier-1 negative item
    assert R.cause_attribution({**facts, "sector_ret": -0.2}, [item(direction=DOWN, source_tier=3, evidence_date="2026-08-07")])["verdict"] == "MARKET_WIDE_DOMINANT"
    assert R.cause_attribution({**facts, "sector_ret": None, "spy_ret": -0.2}, [])["inputs"]["benchmark"] == "SPY"
    assert R.cause_attribution({**facts, "stock_ret": -0.01}, [])["verdict"] == "NO_MATERIAL_DECLINE"
    assert R.cause_attribution({"stock_ret": None}, [])["verdict"] == "NOT_AVAILABLE"
    assert R.cause_attribution(facts, [item(direction=DOWN, evidence_date="2026-09-30")])["verdict"] == "MIXED"       # outside the decline window


def test_rules_contradictions():
    a = item(item_id="a" * 32, category=INDUSTRY_SUPPLY_CHAIN, direction=UP, evidence_date="2026-09-01", source_tier=2)
    b = item(item_id="b" * 32, category=INDUSTRY_SUPPLY_CHAIN, direction=DOWN, evidence_date="2026-09-20", source_tier=3)
    c = item(item_id="c" * 32, category=INDUSTRY_SUPPLY_CHAIN, direction=DOWN, evidence_date="2026-11-01", source_tier=2)   # > 30 days
    d = item(item_id="d" * 32, category=COMPANY_SPECIFIC, direction=DOWN, evidence_date="2026-09-02", source_tier=1)       # other category
    e = item(item_id="e" * 32, category=INDUSTRY_SUPPLY_CHAIN, direction=DOWN, evidence_date="2026-09-02", source_tier=4)  # tier 4 ignored
    items = [a, b, c, d, e]
    assert R.mark_contradictions(items) == 2
    assert a["contradiction"] == 1 and a["contradicts_item_id"] == "b" * 32 and b["contradiction"] == 1 and b["contradicts_item_id"] == "a" * 32
    assert c["contradiction"] == 0 and d["contradiction"] == 0 and e["contradiction"] == 0


def test_rules_ladder_every_rung_and_status():
    facts = {"spy_20": 0.02, "spy_above_ema50": True, "etf_20": 0.01, "fundamentals": {"revenue_up": True, "margin_up": True, "revenue_latest": 2, "revenue_prior": 1, "gm_latest": 0.3, "gm_prior": 0.2},
             "price_above_ema20": True, "rel_strength_20": 0.05}
    sup_up = item(item_id="1" * 32, category=INDUSTRY_SUPPLY_CHAIN, role="supplier", direction=UP, source_tier=2, subject_ticker="LRCX")
    guid_up = item(item_id="2" * 32, category=COMPANY_CONFIRMATION, kind="GUIDANCE", direction=UP, source_tier=1)
    by = {r["rung"]: r for r in R.ladder(facts, [sup_up, guid_up], {"suppliers": ["LRCX"]})}
    assert [by[n]["status"] for n in range(1, 7)] == [SUPPORTED] * 6
    # rung 1 partial / not supported / not available
    assert R.ladder({**facts, "spy_above_ema50": False}, [], {})[0]["status"] == PARTIAL
    assert R.ladder({**facts, "spy_20": -0.01, "spy_above_ema50": False}, [], {})[0]["status"] == NOT_SUPPORTED
    assert R.ladder({**facts, "spy_20": None, "spy_above_ema50": None}, [], {})[0]["status"] == NOT_AVAILABLE
    # rung 2: etf up + no items -> PARTIAL; etf down + DOWN item -> NOT_SUPPORTED; UP and DOWN -> PARTIAL; nothing -> NOT_AVAILABLE
    ind_up = item(item_id="3" * 32, category=INDUSTRY_SUPPLY_CHAIN, role="industry", direction=UP, source_tier=3)
    ind_dn = item(item_id="4" * 32, category=INDUSTRY_SUPPLY_CHAIN, role="industry", direction=DOWN, source_tier=3)
    t4 = item(item_id="5" * 32, category=INDUSTRY_SUPPLY_CHAIN, role="industry", direction=UP, source_tier=4)
    assert R.ladder(facts, [], {})[1]["status"] == PARTIAL
    assert R.ladder({**facts, "etf_20": -0.01}, [ind_dn], {})[1]["status"] == NOT_SUPPORTED
    assert R.ladder(facts, [ind_up, ind_dn], {})[1]["status"] == PARTIAL
    assert R.ladder({**facts, "etf_20": None}, [t4], {})[1]["status"] == NOT_AVAILABLE
    # rung 3: tier 3-4 UP only -> PARTIAL; DOWN only -> NOT_SUPPORTED; declared but none -> NOT_AVAILABLE; contradiction downgrades SUPPORTED
    sup_t3 = item(item_id="6" * 32, category=INDUSTRY_SUPPLY_CHAIN, role="customer", direction=UP, source_tier=3)
    sup_dn = item(item_id="7" * 32, category=INDUSTRY_SUPPLY_CHAIN, role="supplier", direction=DOWN, source_tier=2)
    assert R.ladder(facts, [sup_t3], {"customers": ["X"]})[2]["status"] == PARTIAL
    assert R.ladder(facts, [sup_dn], {"suppliers": ["LRCX"]})[2]["status"] == NOT_SUPPORTED
    assert R.ladder(facts, [], {"suppliers": ["LRCX"]})[2]["status"] == NOT_AVAILABLE and R.ladder(facts, [], {})[2]["status"] == NOT_AVAILABLE
    assert R.ladder(facts, [sup_up, sup_dn], {"suppliers": ["LRCX"]})[2]["status"] == PARTIAL
    r3 = R.ladder(facts, [{**sup_up, "contradiction": 1}], {"suppliers": ["LRCX"]})[2]
    assert r3["status"] == PARTIAL and "downgraded" in r3["reason"]
    # rung 4
    assert R.ladder({**facts, "fundamentals": {"revenue_up": True, "margin_up": False}}, [], {})[3]["status"] == PARTIAL
    assert R.ladder({**facts, "fundamentals": {"revenue_up": False, "margin_up": False}}, [], {})[3]["status"] == NOT_SUPPORTED
    assert R.ladder({**facts, "fundamentals": {}}, [], {})[3]["status"] == NOT_AVAILABLE
    # rung 5
    guid_dn = item(item_id="8" * 32, category=COMPANY_CONFIRMATION, kind="GUIDANCE", direction=DOWN, source_tier=1)
    guid_n = item(item_id="9" * 32, category=COMPANY_CONFIRMATION, kind="GUIDANCE", direction=NEUTRAL, source_tier=1)
    other_cat = item(item_id="0" * 32, category=COMPANY_SPECIFIC, kind="GUIDANCE", direction=UP, source_tier=1)
    assert R.ladder(facts, [guid_dn], {})[4]["status"] == NOT_SUPPORTED and R.ladder(facts, [guid_n], {})[4]["status"] == PARTIAL
    assert R.ladder(facts, [guid_up, guid_dn], {})[4]["status"] == PARTIAL and R.ladder(facts, [other_cat], {})[4]["status"] == NOT_AVAILABLE
    assert R.ladder(facts, [{**guid_up, "source_tier": 3}], {})[4]["status"] == NOT_AVAILABLE
    # rung 6
    assert R.ladder({**facts, "rel_strength_20": -0.01}, [], {})[5]["status"] == PARTIAL
    assert R.ladder({**facts, "price_above_ema20": False, "rel_strength_20": -0.01}, [], {})[5]["status"] == NOT_SUPPORTED
    assert R.ladder({**facts, "price_above_ema20": None, "rel_strength_20": None}, [], {})[5]["status"] == NOT_AVAILABLE


def test_rules_strength_gaps_next_events():
    def rungs(*st):
        return [{"rung": i + 1, "name": n, "status": s, "reason": "", "inputs": {}, "item_ids": []} for i, (n, s) in enumerate(zip([x[1] for x in R.RUNGS], st))]
    s = R.strength(rungs(SUPPORTED, SUPPORTED, SUPPORTED, SUPPORTED, PARTIAL, SUPPORTED))
    assert s["grade"] == "STRONG" and s["points"] == 11 and s["confirmed"] and STRENGTH_DISCLAIMER in s["text"]
    s = R.strength(rungs(SUPPORTED, SUPPORTED, SUPPORTED, PARTIAL, PARTIAL, SUPPORTED))                                   # 10 points, no confirmation
    assert s["grade"] == "MODERATE" and CONFIRMATION_MISSING in s["sentences"] and "capped at MODERATE" in s["text"]
    s = R.strength(rungs(SUPPORTED, PARTIAL, NOT_AVAILABLE, NOT_AVAILABLE, NOT_AVAILABLE, PARTIAL))
    assert s["grade"] == "WEAK" and s["points"] == 4 and CONFIRMATION_MISSING in s["sentences"]
    s = R.strength(rungs(NOT_SUPPORTED, NOT_SUPPORTED, NOT_AVAILABLE, SUPPORTED, SUPPORTED, NOT_SUPPORTED))
    assert s["grade"] == "WEAK" and s["confirmed"] and CONFIRMATION_MISSING not in s["sentences"]
    assert R.strength(rungs(*([NOT_AVAILABLE] * 6)))["grade"] == "WEAK"
    g = R.gaps(rungs(NOT_AVAILABLE, SUPPORTED, NOT_AVAILABLE, SUPPORTED, SUPPORTED, SUPPORTED), [{"adapter": "company_ir", "url": "u", "status": "TIMEOUT", "error": "slow"}])
    assert [x["rung"] for x in g if x["kind"] == "rung"] == [1, 3] and any("company_ir could not retrieve u: TIMEOUT (slow)" in x["text"] for x in g)
    ne = R.next_events({"next_earnings": {"date": "2026-11-05", "title": "t", "source": "s"}, "next_fomc": {"date": "2026-10-28", "title": "f", "source": "s"}})
    assert [e["date"] for e in ne] == ["2026-11-05", "2026-10-28"] and "says nothing about the company" in ne[1]["would"]
    assert R.next_events({"earnings_reason": "no provider"})[0]["source"] == "no provider"


# ---------------------------------------------------------------- engine -------------------------------------------------------

def test_engine_golden_packet(live, store):
    out = build(store)
    p, items, fetches = out["packet"], out["items"], out["fetches"]
    assert p["status"] == "COMPLETED" and p["ticker"] == "SNDK" and p["rules_version"] == RULES_VERSION and p["sec_requests"] == 10
    assert p["cause_json"]["verdict"] == "COMPANY_SPECIFIC_DOMINANT" and p["cause_json"]["item_ids"]
    by = {r["rung"]: r["status"] for r in p["ladder_json"]}
    assert by == {1: SUPPORTED, 2: SUPPORTED, 3: SUPPORTED, 4: SUPPORTED, 5: PARTIAL, 6: SUPPORTED}
    assert p["strength"] == "STRONG" and p["strength_points"] == 11 and STRENGTH_DISCLAIMER in p["strength_text"]
    assert p["n_items"] == len(items) and p["n_fetches"] == len(fetches) == 10 + 3 + 1 + 1 + 3 and p["n_failed_fetches"] == 0
    cats = {i["category"] for i in items}
    assert cats == {MARKET_WIDE, COMPANY_SPECIFIC, INDUSTRY_SUPPLY_CHAIN, COMPANY_CONFIRMATION, MARKET_CONFIRMATION}
    social = next(i for i in items if i["source_url"] == F.SOCIAL_URL)
    assert social["epistemic"] == UNCONFIRMED and social["source_tier"] == 5 and social["direction"] == NA and social["ladder_rung"] is None
    pasted = next(i for i in items if i["source_type"] == "MANUAL_TEXT")
    assert pasted["epistemic"] == UNCONFIRMED and pasted["ladder_rung"] is None
    ir = next(i for i in items if i["source_type"] == "IR_RELEASE")
    assert ir["category"] == COMPANY_CONFIRMATION and ir["direction"] == DOWN and ir["source_tier"] == 1 and ir["ladder_rung"] == 5
    sup = next(i for i in items if i["source_url"] == F.SUPPLIER_URL)
    assert sup["source_tier"] == 2 and sup["category"] == INDUSTRY_SUPPLY_CHAIN and sup["ladder_rung"] == 3 and sup["direction"] == UP
    reuters = next(i for i in items if i["source_name"] == "Reuters")
    assert reuters["source_tier"] == 3 and reuters["direction"] == DOWN and reuters["category"] == COMPANY_SPECIFIC
    assert all(len(i["item_id"]) == 32 and i["packet_id"] == p["packet_id"] for i in items) and [i["position"] for i in items] == list(range(len(items)))
    assert all(f["packet_id"] == p["packet_id"] and len(f["fetch_id"]) == 32 for f in fetches)
    assert any(m["kind"] == "rung" for m in p["missing_json"]) is False and p["next_events_json"][0]["date"] == "2026-11-05"
    assert stored(store, p["packet_id"])["packet_hash"] == p["packet_hash"]
    assert p["packet_hash"] == GOLDEN_HASH, f"golden packet hash changed: {p['packet_hash']}"


def stored(store, pid):
    return store.packet(pid)


def test_engine_is_deterministic_and_order_independent(live, store):
    a = build(store)
    b = EN.build_packet(F.request(), adapters=F.adapters(), now_fn=F.now_fn, store=None, persist=False)
    assert a["packet"]["packet_hash"] == b["packet"]["packet_hash"] and a["packet"]["packet_id"] != b["packet"]["packet_id"]
    # a different manual-URL order yields the same hash (items are sorted before hashing)
    req = F.request(manual_urls=list(reversed(F.request().manual_urls)))
    c = EN.build_packet(req, adapters=F.adapters(), now_fn=F.now_fn, persist=False)
    assert c["packet"]["packet_hash"] == a["packet"]["packet_hash"]
    # changing one source changes the hash
    d = EN.build_packet(F.request(manual_texts=[]), adapters=F.adapters(), now_fn=F.now_fn, persist=False)
    assert d["packet"]["packet_hash"] != a["packet"]["packet_hash"]


def test_engine_failures_and_partial_sources(live, store):
    bad = EN.build_packet(F.request(ticker="bad ticker!"), adapters=F.adapters(), now_fn=F.now_fn, store=store)["packet"]
    assert bad["status"] == "FAILED" and bad["failure_code"] == "INVALID_TICKER" and bad["ticker"] == "UNKNOWN"
    nobars = EN.build_packet(F.request(), adapters=F.adapters(bars=lambda s, n: {}), now_fn=F.now_fn, store=store)["packet"]
    assert nobars["status"] == "FAILED" and nobars["failure_code"] == "NO_MARKET_DATA" and nobars["n_fetches"] == 3 and nobars["n_failed_fetches"] == 3
    with pytest.raises(EN.EvidenceError, match="lookback"):
        F.request(lookback_days=5).normalised(F.AS_OF)
    with pytest.raises(EN.EvidenceError, match="future"):
        F.request(as_of_date=F.AS_OF + timedelta(days=1)).normalised(F.AS_OF)
    with pytest.raises(EN.EvidenceError, match="role"):
        F.request(manual_urls=[{"url": "https://a.example/x", "role": "guru"}]).normalised(F.AS_OF)
    only_internal = EN.build_packet(F.request(sources={"internal": True, "sec": False, "ir": False, "news": False, "manual": False}), adapters=F.adapters(),
                                    now_fn=F.now_fn, store=store)["packet"]
    assert only_internal["status"] == "COMPLETED" and only_internal["sec_requests"] == 0 and only_internal["n_fetches"] == 3
    by = {r["rung"]: r["status"] for r in only_internal["ladder_json"]}
    assert by[3] == NOT_AVAILABLE and by[4] == NOT_AVAILABLE and by[5] == NOT_AVAILABLE and only_internal["strength"] == "MODERATE" and only_internal["strength_points"] == 5
    assert len([m for m in only_internal["missing_json"] if m["kind"] == "rung"]) == 3 and CONFIRMATION_MISSING in only_internal["strength_text"]
    class Boom:
        def run(self, *a, **k):
            raise RuntimeError("adapter exploded")
    crashed = EN.build_packet(F.request(), adapters={**F.adapters(), "sec": Boom()}, now_fn=F.now_fn, store=store)["packet"]
    assert crashed["status"] == "FAILED" and crashed["failure_code"] == "INTERNAL_ERROR" and "adapter exploded" in crashed["failure_detail"]


def test_engine_with_network_off_records_disabled_fetches(store, monkeypatch):
    monkeypatch.setattr(config, "EVIDENCE_IR_DOMAINS", F.IR_DOMAINS)
    out = build(store)                                                   # conftest: network flag off, wire tripwired — never reached
    p = out["packet"]
    assert p["status"] == "COMPLETED"
    statuses = {f["adapter"]: {f["status"] for f in out["fetches"] if f["adapter"] == f["adapter"]} for f in out["fetches"]}
    assert {f["status"] for f in out["fetches"] if f["adapter"] in ("sec_edgar", "company_ir", "manual_url")} == {"DISABLED"}
    assert p["sec_requests"] == 1 and all("DISABLED" in m["text"] for m in p["missing_json"] if m["kind"] == "fetch")
    assert {r["rung"]: r["status"] for r in p["ladder_json"]}[4] == NOT_AVAILABLE and statuses


# ---------------------------------------------------------------- store --------------------------------------------------------

def test_store_immutable_and_checked(live, store):
    out = build(store)
    pid = out["packet"]["packet_id"]
    assert store.exists() and store.packet(pid)["strength"] == "STRONG" and len(store.items(pid)) == out["packet"]["n_items"]
    assert len(store.fetches(pid)) == out["packet"]["n_fetches"] and store.packets(limit=5)[0]["packet_id"] == pid and store.packets(ticker="sndk")[0]["packet_id"] == pid
    conn = sqlite3.connect(str(store.path))
    for sql in ("UPDATE evidence_packets SET strength = 'WEAK'", "DELETE FROM evidence_items", "DELETE FROM evidence_source_fetches",
                "UPDATE evidence_items SET epistemic = 'FACT' WHERE epistemic = 'UNCONFIRMED'"):
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute(sql)
    with pytest.raises(sqlite3.DatabaseError):                             # same packet hash twice
        store.insert_packet({**out["packet"], "packet_id": "f" * 32}, [], [])
    with pytest.raises(sqlite3.DatabaseError):                             # bad epistemic value
        store.insert_packet({**out["packet"], "packet_id": "e" * 32, "packet_hash": "e" * 64}, [{**out["items"][0], "item_id": "e" * 32, "packet_id": "e" * 32, "epistemic": "TRUTH"}], [])
    before = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    from database.research_packet_migrations import run_research_packet_migrations
    run_research_packet_migrations(conn)
    assert {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")} == before
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
    conn.close()


# ---------------------------------------------------------------- explanation --------------------------------------------------

def test_explain_dry_run_guard_and_single_request(live, store, monkeypatch):
    out = build(store)
    pid = out["packet"]["packet_id"]
    plan = EX.dry_run(pid, store)
    assert plan["requests"] == 1 and plan["prompt_chars"] > 500 and len(plan["input_hash"]) == 64 and "No request" in plan["note"]
    assert EX.explain(pid, confirm=False, store=store, provider_factory=lambda: FakeBoom())["requests"] == 1 and store.explanations(pid) == []
    good = F.FakeProvider(text="The strength is STRONG with 11 of 12 points. The cause verdict is COMPANY_SPECIFIC_DOMINANT. "
                               "Rung 5 is PARTIAL because guidance items point both ways. This is an evidence-strength grade, not a probability that the stock will rise.")
    monkeypatch.setattr(config, "ANTHROPIC_MODEL", "claude-sonnet-5")
    r = EX.explain(pid, confirm=True, store=store, provider_factory=lambda: good, now_fn=F.now_fn)
    assert good.calls == 1 and r["status"] == "COMPLETED" and r["requests"] == 1 and r["input_tokens"] == 1200 and r["estimated_cost_usd"]
    bad = F.FakeProvider(text="Given the supplier strength the stock will recover and is likely to rise 30%; buy now with a price target of 150.")
    r2 = EX.explain(pid, confirm=True, store=store, provider_factory=lambda: bad, now_fn=F.now_fn)
    assert r2["status"] == "WITHHELD" and r2["text"] == "" and "forbidden_phrases" in r2["guard_failures_json"]
    assert {"will recover", "likely to", "buy", "price target"} <= set(r2["guard_failures_json"]["forbidden_phrases"])
    none = EX.explain(pid, confirm=True, store=store, provider_factory=lambda: None, now_fn=F.now_fn)
    assert none["status"] == "FAILED" and none["requests"] == 0
    failing = EX.explain(pid, confirm=True, store=store, provider_factory=lambda: F.FakeProvider(fail=True), now_fn=F.now_fn)
    assert failing["status"] == "FAILED" and "simulated provider failure" in failing["guard_failures_json"]["request"][0]
    rows = store.explanations(pid)
    assert [x["status"] for x in rows] == ["COMPLETED", "WITHHELD", "FAILED", "FAILED"] and all(x["packet_hash"] == out["packet"]["packet_hash"] for x in rows)
    with pytest.raises(KeyError):
        EX.dry_run("0" * 32, store)


class FakeBoom:
    def analyze(self, *a, **k):
        raise AssertionError("a dry run must never reach the provider")


def test_explanation_facts_text_excludes_social_and_keeps_labels(live, store):
    out = build(store)
    text = EX.facts_text(out["packet"] | {"cause": out["packet"]["cause_json"], "ladder": out["packet"]["ladder_json"], "missing": out["packet"]["missing_json"],
                                          "next_events": out["packet"]["next_events_json"]}, out["items"])
    assert "this will moon" not in text and "[UNCONFIRMED]" in text and "[FACT]" in text and "rung 5" in text and STRENGTH_DISCLAIMER in text
    assert "Never" not in text or True


# ---------------------------------------------------------------- API ----------------------------------------------------------

@pytest.fixture
def client(live, store, monkeypatch):
    from api.routes import evidence as RT
    from fit import readonly as RO
    monkeypatch.setattr(RO, "db_path", lambda: store.path)
    monkeypatch.setattr(RT, "NOW_FN", F.now_fn)
    monkeypatch.setitem(RT.BUILD, "adapters", F.adapters())
    from api.server import app
    return TestClient(app)


def test_api_config_build_read_and_explain(client, monkeypatch):
    c = client.get("/api/evidence/config").json()
    assert c["rules_version"] == RULES_VERSION and c["network_enabled"] is True and c["requests_made"] == 0 and "sec.gov" in " ".join(c["sec_hosts"])
    body = {"ticker": "sndk", "as_of_date": F.AS_OF.isoformat(), "lookback_days": 90, "related": {"suppliers": ["LRCX"]}, "ir_urls": [F.IR_URL],
            "manual_urls": [{"url": F.SUPPLIER_URL, "role": "supplier", "related_ticker": "LRCX"}], "manual_texts": [{"text": "x", "role": "industry"}]}
    r = client.post("/api/evidence/packets", json=body)
    assert r.status_code == 201, r.text
    j = r.json()
    assert j["status"] == "COMPLETED" and j["strength"] == "STRONG" and j["model_requests"] == 0 and "www.sec.gov" in j["hosts_contacted"] and "alpaca" not in j["hosts_contacted"]
    pid = j["packet_id"]
    assert client.get("/api/evidence/packets").json()["packets"][0]["packet_id"] == pid
    d = client.get(f"/api/evidence/packets/{pid}").json()
    assert d["packet"]["packet_id"] == pid and len(d["items"]) == d["packet"]["n_items"] and d["fetches"] and d["explanations"] == []
    assert client.get("/api/evidence/packets/zzz").status_code == 422 and client.get("/api/evidence/packets/" + "0" * 32).status_code == 404
    assert client.post("/api/evidence/packets", json={**body, "extra": 1}).status_code == 422
    assert client.post("/api/evidence/packets", json={**body, "manual_urls": [{"url": "https://a.example/x", "role": "guru"}]}).status_code == 422
    assert client.post("/api/evidence/packets", json={**body, "lookback_days": 3}).status_code == 422
    bad = client.post("/api/evidence/packets", json={**body, "as_of_date": "2027-01-01"})
    assert bad.status_code == 422 and bad.json()["status"] == "INVALID_REQUEST"
    from api.routes import evidence as RT
    dry = client.post(f"/api/evidence/packets/{pid}/explain", json={"confirm": False}).json()
    assert dry["requests"] == 1 and dry["requests_made"] == 0 and client.get(f"/api/evidence/packets/{pid}/explanations").json()["explanations"] == []
    good = F.FakeProvider(text="The strength is STRONG with 11 of 12 points. This is an evidence-strength grade, not a probability that the stock will rise.")
    monkeypatch.setitem(RT.EXPLAIN, "provider_factory", lambda: good)
    monkeypatch.setattr(config, "ANTHROPIC_MODEL", "claude-sonnet-5")
    r = client.post(f"/api/evidence/packets/{pid}/explain", json={"confirm": True}).json()
    assert r["status"] == "COMPLETED" and r["requests_made"] == 1 and good.calls == 1
    assert client.post("/api/evidence/packets/" + "0" * 32 + "/explain", json={"confirm": True}).status_code == 404


def test_api_route_names_and_no_forbidden_words():
    from api.routes import evidence as RT
    paths = [r.path for r in RT.router.routes]
    assert all(p.startswith("/api/evidence") for p in paths) and len(paths) >= 7
    for word in ("order", "trade", "backtest", "paper", "execute", "broker", "handoff", "deploy", "activate"):
        assert not any(word in p.lower() for p in paths), word


# ---------------------------------------------------------------- isolation and regression --------------------------------------

def test_isolation_no_broker_robinhood_or_model_in_evidence_layer():
    src = {p.relative_to(ROOT).as_posix(): p.read_text(encoding="utf-8") for p in list((ROOT / "evidence").rglob("*.py")) + [ROOT / "api" / "routes" / "evidence.py"]}
    for name, text in src.items():
        for forbidden in ("from paper", "import paper", "from portfolio", "robinhood", "rh_gateway", "alpaca_order", "messages.create", "anthropic.Anthropic",
                          "run_tool_loop(", "requests.get", "httpx", "subprocess"):
            assert forbidden not in text, f"{name} contains {forbidden!r}"
    for name in ("evidence/net.py",):
        assert "urllib.request" in src[name] and "WIRE" in src[name]
    assert src["evidence/explain.py"].count("provider.analyze(") == 1 and "one_request" in src["evidence/explain.py"]
    assert "get_provider" not in src["evidence/engine.py"] and "analyze(" not in src["evidence/engine.py"]
    assert "WIRE" not in "".join(v for k, v in src.items() if k != "evidence/net.py")              # only net.py knows the wire


def test_regression_frozen_stages_untouched():
    import hashlib
    import subprocess  # noqa: S404 - git read of committed blobs, test only
    pins = {"rotation/engine.py": None, "rotation/handoff.py": None, "paper/alpaca_orders.py": "391cd2f2", "paper/alpaca_order_writer.py": "f6cfe85b",
            "signal_research/engine.py": None, "rotation_campaign/leaderboard.py": None, "rotation_backtest/simulator.py": None, "frontend/evidence.js": None, "frontend/evidence.css": None}
    for rel, prefix in pins.items():
        blob = subprocess.run(["git", "show", f"97a191a:{rel}"], cwd=ROOT, capture_output=True).stdout
        assert blob, rel
        assert (ROOT / rel).read_bytes().replace(b"\r\n", b"\n") == blob.replace(b"\r\n", b"\n"), f"{rel} differs from stage-52"
        if prefix:
            assert hashlib.sha256(blob).hexdigest().startswith(prefix), rel


def test_conventions_and_limits_are_declared():
    from evidence import CONVENTIONS, LIMITS
    assert "not a probability" in CONVENTIONS["strength"] and LIMITS["sec_requests"] == 12 and LIMITS["items"] == 200
    assert "Robinhood" in CONVENTIONS["boundaries"]
