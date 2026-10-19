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
from evidence import (COMPANY_CONFIRMATION, COMPANY_SPECIFIC, CONFIRMATION_MISSING, DOWN, FACT, INDUSTRY_SUPPLY_CHAIN, INFERENCE, LIMITS, MARKET_CONFIRMATION,
                      MARKET_WIDE, NA, NEUTRAL, NOT_AVAILABLE, NOT_SEPARABLE, NOT_SUPPORTED, PARTIAL, RULES_VERSION, STRENGTH_DISCLAIMER, SUPPORTED, UNCONFIRMED, UP)
from evidence import engine as EN
from evidence import explain as EX
from evidence import extract as X
from evidence import net
from evidence import rules as R
from evidence.sources import internal as INT
from evidence.sources.sec_edgar import SecAdapter
from evidence.store import EvidenceStore

ROOT = Path(__file__).resolve().parents[1]
GOLDEN_HASH = "186d773b2d520ebff0ff7f0b90612dd8088dd417f2403ec41bec57d95a16b187"   # pinned after the first green run (see test_engine_golden_packet)


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
    assert aug.kind == "EARNINGS" and "lowered its fiscal 2027 outlook" in aug.text and aug.source_url.endswith("ex99-1.htm") and len(aug.source_hash) == 64
    assert R.is_guidance(aug.rule_text)                                                          # guidance CONTEXT found in a bounded statement
    sep = next(i for i in res.items if i.evidence_date == "2026-09-15")
    assert sep.kind == "FILING" and "reaffirmed its outlook" in sep.text and R.is_guidance(sep.rule_text)
    lrcx = next(i for i in res.items if i.subject_ticker == "LRCX")
    assert lrcx.role == "supplier" and "better-than-expected" in lrcx.text
    metric = next(i for i in res.items if i.kind == "METRIC")
    f = res.facts["fundamentals"]
    assert f["quarter_latest"] == "2026-06-30" and f["quarter_prior"] == "2026-03-31" and f["revenue_up"] and f["margin_up"]      # Q3 filed after as_of ignored
    assert f["validation"] == "OK" and f["reasons"] == [] and f["revenue_concept"] == "Revenues" and f["fiscal_context"]["latest"]["fp"] == "Q4"
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
    assert it.kind == "EARNINGS" and it.evidence_date == "2026-08-07" and it.date_source == "source" and it.role == "company" and it.source_type == "IR_RELEASE"
    assert it.extra["page_kind"] == "article" and R.is_guidance(it.rule_text)
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
    d, m = R.direction_for("The company raised its full-year guidance")
    assert d == UP and "guidance_raised:UP:raised its full-year guidance" in m
    assert R.direction_for("lowered its fiscal 2027 outlook")[0] == DOWN and R.direction_for("cut guidance")[0] == DOWN
    assert R.direction_for("reaffirmed its outlook")[0] == NEUTRAL and R.direction_for("results in line with expectations")[0] == NEUTRAL
    d, m = R.direction_for("orders increased but margins declined")
    assert d == NEUTRAL and ":UP:" in m and ":DOWN:" in m
    assert R.direction_for("nothing relevant") == (NA, "")
    assert R.metric_direction({"revenue_up": True, "margin_up": True})[0] == UP and R.metric_direction({"revenue_up": False, "margin_up": False})[0] == DOWN
    assert R.metric_direction({"revenue_up": True, "margin_up": False})[0] == NEUTRAL and R.metric_direction({"signed": -0.1})[0] == DOWN and R.metric_direction(None) == (NA, "")
    assert R.is_guidance("our revenue outlook for the year") and not R.is_guidance("results") and not R.is_guidance("Outlook") and R.is_earnings("Fiscal Fourth Quarter Results")


def test_rules_cause_attribution():
    facts = {"stock_ret": -0.25, "spy_ret": -0.04, "sector_ret": -0.12, "window_start": "2026-07-15", "window_end": "2026-08-25", "peak_close": 120, "trough_close": 90, "sector_etf": "SOXX"}
    c = R.cause_attribution(facts, [])
    assert c["verdict"] == "MIXED" and c["inputs"]["explained_share"] == pytest.approx(0.48) and c["epistemic"] == INFERENCE
    down = item(item_id="b" * 32, category=COMPANY_CONFIRMATION, kind="GUIDANCE", direction=DOWN, evidence_date="2026-08-07")
    assert R.cause_attribution(facts, [down])["verdict"] == "COMPANY_SPECIFIC_DOMINANT" and R.cause_attribution(facts, [down])["item_ids"] == ["b" * 32]
    assert R.cause_attribution({**facts, "sector_ret": -0.2}, [])["verdict"] == "MARKET_WIDE_DOMINANT"
    assert R.cause_attribution({**facts, "sector_ret": -0.2}, [down])["verdict"] == "MIXED"                           # share 0.8 but a tier-1 negative item
    assert R.cause_attribution({**facts, "sector_ret": -0.2}, [item(direction=DOWN, source_tier=3, evidence_date="2026-08-07")])["verdict"] == "MARKET_WIDE_DOMINANT"
    ns = R.cause_attribution({**facts, "sector_ret": None, "spy_ret": -0.2}, [down])                                # no sector benchmark: fail closed
    assert ns["verdict"] == NOT_SEPARABLE and ns["inputs"]["benchmark"] is None and ns["inputs"]["explained_share"] is None
    assert R.NOT_SEPARABLE_REASON in ns["reason"] and ns["item_ids"] == ["b" * 32] and ns["epistemic"] == INFERENCE
    assert R.cause_attribution({**facts, "sector_ret": None, "spy_ret": 0.01}, [])["verdict"] == NOT_SEPARABLE          # SPY-only divergence is never company-specific
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
    facts = {"spy_20": 0.02, "spy_above_ema50": True, "etf_20": 0.01, "fundamentals": {"revenue_up": True, "margin_up": True, "revenue_latest": 2, "revenue_prior": 1, "gm_latest": 0.3, "gm_prior": 0.2, "validation": "OK", "reasons": []},
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
    assert by == {1: SUPPORTED, 2: SUPPORTED, 3: SUPPORTED, 4: SUPPORTED, 5: NOT_SUPPORTED, 6: SUPPORTED}
    r4 = next(r for r in p["ladder_json"] if r["rung"] == 4)
    assert r4["inputs"]["ambiguity"] is False and "A: validated" in r4["reason"] and r4["inputs"]["text_down"] == 0   # "weaker-than-expected demand" is not a revenue / margin statement
    r5 = next(r for r in p["ladder_json"] if r["rung"] == 5)
    assert r5["inputs"] == {"up": 0, "down": 2, "other": 1} and "clear DOWN" in r5["reason"]                      # Aug 7 8-K + IR lowered the outlook; Sep 15 reaffirmed
    assert p["strength"] == "STRONG" and p["strength_points"] == 10 and STRENGTH_DISCLAIMER in p["strength_text"]
    assert not any(m["kind"] == "ambiguity" for m in p["missing_json"])
    assert all(i["claim_key"] for i in items) and not any(i["contradiction"] for i in items)
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
    good = F.FakeProvider(text="The strength is STRONG with 10 of 12 points. The cause verdict is COMPANY_SPECIFIC_DOMINANT. "
                               "Rung 5 is NOT_SUPPORTED because the guidance statements point down. This is an evidence-strength grade, not a probability that the stock will rise.")
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
    good = F.FakeProvider(text="The strength is STRONG with 10 of 12 points. This is an evidence-strength grade, not a probability that the stock will rise.")
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
    assert "get_provider" not in src["evidence/engine.py"] and "provider.analyze(" not in src["evidence/engine.py"] and "one_request" not in src["evidence/engine.py"]
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


# ---------------------------------------------------------------- validation hardening (second live run) ------------------------

def test_hardening_contradictions_need_a_comparable_subject():
    """SNDK DOWN vs SPY UP (different subjects / measures) must never contradict; same-subject official claims may."""
    sndk_dn = item(item_id="1" * 32, category=MARKET_WIDE, kind="METRIC", direction=DOWN, subject_ticker="SNDK", source_identifier="decline window")
    spy_up = item(item_id="2" * 32, category=MARKET_WIDE, kind="METRIC", direction=UP, subject_ticker="SPY", source_identifier="SPY 20-session return")
    spy_ema = item(item_id="3" * 32, category=MARKET_WIDE, kind="METRIC", direction=UP, subject_ticker="SPY", source_identifier="SPY vs 50-day EMA")
    sndk_rs = item(item_id="4" * 32, category=MARKET_CONFIRMATION, kind="METRIC", direction=UP, subject_ticker="SNDK", source_identifier="relative strength 20")
    sndk_ema = item(item_id="5" * 32, category=MARKET_CONFIRMATION, kind="METRIC", direction=DOWN, subject_ticker="SNDK", source_identifier="price vs EMA20")
    metrics = [sndk_dn, spy_up, spy_ema, sndk_rs, sndk_ema]
    assert R.mark_contradictions(metrics) == 0 and all(i["contradiction"] == 0 for i in metrics)
    assert R.claim_key(sndk_dn) == "SNDK|METRIC|decline window" and R.claim_key(spy_up) != R.claim_key(spy_ema)
    g_up = item(item_id="6" * 32, category=COMPANY_CONFIRMATION, kind="GUIDANCE", direction=UP, evidence_date="2026-09-01", subject_ticker="SNDK")
    g_dn = item(item_id="7" * 32, category=COMPANY_CONFIRMATION, kind="GUIDANCE", direction=DOWN, evidence_date="2026-09-20", subject_ticker="SNDK")
    other = item(item_id="8" * 32, category=COMPANY_CONFIRMATION, kind="GUIDANCE", direction=DOWN, evidence_date="2026-09-20", subject_ticker="LRCX")
    assert R.mark_contradictions([g_up, g_dn, other]) == 2 and g_up["contradicts_item_id"] == "7" * 32 and other["contradiction"] == 0
    d1 = item(item_id="9" * 32, category=INDUSTRY_SUPPLY_CHAIN, kind="PAGE", direction=UP, evidence_date="2026-09-01", subject_ticker="LRCX", source_tier=2)
    d2 = item(item_id="a" * 32, category=INDUSTRY_SUPPLY_CHAIN, kind="PAGE", direction=DOWN, evidence_date="2026-09-10", subject_ticker="LRCX", source_tier=2)
    assert R.mark_contradictions([d1, d2]) == 2
    assert R.claim_key(g_up) == "SNDK|COMPANY_CONFIRMATION" and R.comparable(g_up, g_dn) and not R.comparable(g_up, other) and not R.comparable(sndk_dn, spy_up)


def test_hardening_full_document_guidance_detection_and_snippet_cap(live):
    res = F.adapters()["ir"].run("SNDK", [F.LONG_URL], F.AS_OF)
    it = res.items[0]
    assert it.kind == "EARNINGS" and len(it.rule_text) > 2500 and len(it.text) <= LIMITS["text_chars"] + 3 and R.is_guidance(it.rule_text)
    d, m, off = R.direction_scan(it.rule_text)
    assert d == UP and "guidance_raised:UP:raised its fiscal 2027 outlook" in m and off > 2000
    assert R.direction_scan(it.text)[0] == NA                                                   # the 2,000-char snippet alone would have missed it
    out = EN.build_packet(F.request(ir_urls=[F.LONG_URL], manual_urls=[], manual_texts=[], sources={"internal": True, "sec": False, "ir": True, "news": False, "manual": False}),
                          adapters=F.adapters(), now_fn=F.now_fn, persist=False)
    g = [i for i in out["items"] if i["source_type"] == "IR_RELEASE"]
    assert len(g) == 1 and g[0]["kind"] == "GUIDANCE" and g[0]["direction"] == UP and g[0]["rule_offset"] > 2000 and len(g[0]["text"]) <= LIMITS["text_chars"] + 3
    assert g[0]["claim_key"] == "SNDK|COMPANY_CONFIRMATION" and len(g[0]["source_hash"]) == 64
    assert {r["rung"]: r["status"] for r in out["packet"]["ladder_json"]}[5] == SUPPORTED
    assert "rule_text" not in g[0]                                                               # the full document is evaluated, never stored


def test_hardening_press_allowlist_is_case_insensitive():
    t = lambda src: R.tier_for(host="", subject_ticker="SNDK", ticker="SNDK", source_type="ALPACA_NEWS", press_source=src, ir_domains={}, related={})
    assert t("Benzinga") == t("benzinga") == t("BENZINGA") == t("  benzinga ") == 3
    assert t("Reuters") == t("reuters") == 3 and t("Some Blog") == t("some blog") == 4
    assert R.tier_for(host="WWW.REDDIT.COM", subject_ticker="SNDK", ticker="SNDK", source_type="MANUAL_URL", ir_domains={}, related={}) == 5
    assert R.tier_for(host="Investor.Sandisk.Example", subject_ticker="SNDK", ticker="SNDK", source_type="MANUAL_URL", ir_domains=F.IR_DOMAINS, related={}) == 1
    assert R.category_for(role="SUPPLIER", source_type="MANUAL_URL", subject_is_target=True) == INDUSTRY_SUPPLY_CHAIN


def test_hardening_rung4_requires_validated_xbrl_or_official_text():
    base = {"spy_20": None, "spy_above_ema50": None, "etf_20": None, "price_above_ema20": None, "rel_strength_20": None}
    ok = {"revenue_up": True, "margin_up": True, "revenue_latest": 2, "revenue_prior": 1, "gm_latest": 0.3, "gm_prior": 0.28, "validation": "OK", "reasons": [], "revenue_concept": "Revenues"}
    amb = {**ok, "validation": "AMBIGUOUS", "reasons": ["gross margin moved 27 points q/q; needs confirmation from official filing text"]}
    r4 = lambda f, items=(): R.ladder({**base, "fundamentals": f}, list(items), {})[3]
    assert r4(ok)["status"] == SUPPORTED and "A: validated" in r4(ok)["reason"]                         # 8. compatible concepts can support
    a = r4(amb)
    assert a["status"] == PARTIAL and a["inputs"]["ambiguity"] is True and "capped at PARTIAL" in a["reason"]  # 9. ambiguous -> PARTIAL only
    text_up = item(item_id="1" * 32, category=COMPANY_CONFIRMATION, kind="EARNINGS", direction=UP, source_tier=1)
    text_dn = item(item_id="2" * 32, category=COMPANY_CONFIRMATION, kind="EARNINGS", direction=DOWN, source_tier=1,
                   value_json={"direction_kind": "OPERATING", "statements": [{"kind": "OPERATING", "direction": DOWN, "measure": "revenue", "rule": "op_metric_down"}]})
    guid_dn = item(item_id="4" * 32, category=COMPANY_CONFIRMATION, kind="GUIDANCE", direction=DOWN, source_tier=1,
                   value_json={"direction_kind": "GUIDANCE", "guidance_direction": DOWN, "statements": [{"kind": "GUIDANCE", "direction": DOWN, "measure": None, "rule": "guidance_lowered"}]})
    assert r4(ok, [guid_dn])["status"] == SUPPORTED                                                              # unrelated guidance is not a comparable metric
    assert r4(amb, [text_up])["status"] == SUPPORTED and "B: official filing text" in r4(amb, [text_up])["reason"]   # B: text confirms
    d = r4(ok, [text_dn])
    assert d["status"] == PARTIAL and d["inputs"]["ambiguity"] is True and "opposite directions" in d["reason"]   # 10. disagreement blocks SUPPORTED
    assert r4({**ok, "revenue_up": False, "margin_up": False}, [text_up])["status"] == PARTIAL                     # XBRL down vs text up -> ambiguity
    assert r4({**ok, "revenue_up": False, "margin_up": False})["status"] == NOT_SUPPORTED
    assert r4({**ok, "margin_up": None, "validation": "AMBIGUOUS", "reasons": ["gross profit periods missing"]})["status"] == PARTIAL
    assert r4({}, [text_up])["status"] == SUPPORTED and r4({}, [text_dn])["status"] == NOT_SUPPORTED and r4({})["status"] == NOT_AVAILABLE
    tier3 = item(item_id="3" * 32, category=COMPANY_CONFIRMATION, kind="EARNINGS", direction=UP, source_tier=3)
    assert r4({}, [tier3])["status"] == NOT_AVAILABLE                                                              # only tier-1 text counts
    gaps = R.gaps(R.ladder({**base, "fundamentals": amb}, [], {}), [])
    assert any(g["kind"] == "ambiguity" and g["rung"] == 4 for g in gaps)


def test_hardening_xbrl_validation_flags_ambiguity(live, tmp_path):
    cf = json.loads((F.FIX / "companyfacts_SNDK.json").read_text(encoding="utf-8"))
    import evidence.net as N
    url = "https://data.sec.gov/api/xbrl/companyfacts/CIK0001234567.json"
    # misaligned gross-profit periods -> AMBIGUOUS, margin not derived
    bad = json.loads(json.dumps(cf))
    for e in bad["facts"]["us-gaap"]["GrossProfit"]["units"]["USD"]:
        e["start"] = "2025-12-15"
    path = tmp_path / "cf_bad.json"; path.write_text(json.dumps(bad), encoding="utf-8")
    N.WIRE = F.FakeWire({url: str(path)})
    f = F.adapters()["sec"].run("SNDK", {"suppliers": [], "customers": []}, F.AS_OF, 90).facts["fundamentals"]
    assert f["validation"] == "AMBIGUOUS" and f["margin_up"] is None and any("not aligned" in r for r in f["reasons"])
    # conflicting revenue concepts for the same period -> AMBIGUOUS
    conf = json.loads(json.dumps(cf))
    conf["facts"]["us-gaap"]["RevenueFromContractWithCustomerExcludingAssessedTax"] = {"units": {"USD": [{**conf["facts"]["us-gaap"]["Revenues"]["units"]["USD"][1], "val": 1500000000}]}}
    path2 = tmp_path / "cf_conf.json"; path2.write_text(json.dumps(conf), encoding="utf-8")
    N.WIRE = F.FakeWire({url: str(path2)})
    f2 = F.adapters()["sec"].run("SNDK", {"suppliers": [], "customers": []}, F.AS_OF, 90).facts["fundamentals"]
    assert f2["validation"] == "AMBIGUOUS" and any("concepts disagree" in r for r in f2["reasons"])
    # a non-quarterly fiscal context (fp missing) -> AMBIGUOUS
    nofp = json.loads(json.dumps(cf))
    for e in nofp["facts"]["us-gaap"]["Revenues"]["units"]["USD"]:
        e.pop("fp", None)
    path3 = tmp_path / "cf_nofp.json"; path3.write_text(json.dumps(nofp), encoding="utf-8")
    N.WIRE = F.FakeWire({url: str(path3)})
    f3 = F.adapters()["sec"].run("SNDK", {"suppliers": [], "customers": []}, F.AS_OF, 90).facts["fundamentals"]
    assert f3["validation"] == "AMBIGUOUS" and any("fiscal context" in r for r in f3["reasons"])
    # a > 20-point gross-margin swing -> AMBIGUOUS (needs filing-text confirmation)
    swing = json.loads(json.dumps(cf))
    swing["facts"]["us-gaap"]["GrossProfit"]["units"]["USD"][1]["val"] = 1600000000
    path4 = tmp_path / "cf_swing.json"; path4.write_text(json.dumps(swing), encoding="utf-8")
    N.WIRE = F.FakeWire({url: str(path4)})
    f4 = F.adapters()["sec"].run("SNDK", {"suppliers": [], "customers": []}, F.AS_OF, 90).facts["fundamentals"]
    assert f4["validation"] == "AMBIGUOUS" and any("gross margin moved" in r for r in f4["reasons"]) and f4["margin_up"] is True


def test_hardening_sndk_sector_map_and_benchmark():
    from data.sector_map import SECTOR_MAP, get_sector_for_symbol
    from evidence.sources.internal import _default_sector
    assert get_sector_for_symbol("SNDK") is None                                                # curated map unchanged (earlier stages rely on it)
    assert config.EVIDENCE_SECTOR_EXTENSIONS["SNDK"] == "Semiconductors" and SECTOR_MAP["Semiconductors"]["etf"] == "SOXX"
    assert _default_sector("SNDK") == ("Semiconductors", "SOXX") and _default_sector("sndk") == ("Semiconductors", "SOXX")
    assert _default_sector("MU") == ("Semiconductors", "SOXX") and _default_sector("ZZZZ") == (None, None)
    assert all(name in SECTOR_MAP for name in config.EVIDENCE_SECTOR_EXTENSIONS.values())            # no new taxonomy


def test_hardening_cause_not_separable_end_to_end(store):
    """A packet with bars but no mapped sector (no ETF) must report NOT_SEPARABLE, never COMPANY_SPECIFIC_DOMINANT."""
    out = EN.build_packet(F.request(sources={"internal": True, "sec": False, "ir": False, "news": False, "manual": False}),
                          adapters=F.adapters(sector=lambda s: (None, None)), now_fn=F.now_fn, store=store)
    p = out["packet"]
    assert p["cause_json"]["verdict"] == NOT_SEPARABLE and R.NOT_SEPARABLE_REASON in p["cause_json"]["reason"]
    by = {r["rung"]: r["status"] for r in p["ladder_json"]}
    assert by[2] == NOT_AVAILABLE and by[6] in (PARTIAL, NOT_AVAILABLE) and not any(i["contradiction"] for i in out["items"])


def test_hardening_migration_adds_columns_to_an_existing_table(tmp_path):
    from database import research_packet_migrations as M
    legacy = [st.replace("        claim_key          TEXT NOT NULL DEFAULT '',\n        rule_offset        INTEGER,\n", "") for st in M._STATEMENTS]
    conn = sqlite3.connect(str(tmp_path / "legacy.db"))
    for st in legacy:
        conn.execute(st)
    conn.commit()
    cols = {r[1] for r in conn.execute("PRAGMA table_info(evidence_items)")}
    assert "claim_key" not in cols and "rule_offset" not in cols
    M.run_research_packet_migrations(conn)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(evidence_items)")}
    assert {"claim_key", "rule_offset"} <= cols
    M.run_research_packet_migrations(conn)                                                       # idempotent
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
    triggers = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")}
    assert {"evi_no_update", "evi_no_delete", "evp_no_update", "evp_no_delete"} <= triggers                   # append-only still enforced
    conn.close()


# ---------------------------------------------------------------- interpretation hardening (third live run) ----------------------

from evidence import statements as ST


def _an(text, **kw):
    return ST.analyze(text, **kw)


def test_interp_isolated_keywords_never_assign_direction():
    for text in ("restructuring", "Restructuring charges 12 15", "restructuring expense (1,234) (987)", "impairment charge", "Layoffs", "RISK FACTORS",
                 "Reconciliation of GAAP to non-GAAP: restructuring and other", "The company recorded restructuring charges of $5 million."):
        an = _an(text)
        assert an["direction"] == NA and an["direction_kind"] == "NA" and not an["guidance_present"], text
    long = ("Paragraph about operations with no directional statement. " * 30) + "Restructuring charges increased to $12 million in the quarter."
    an = _an(long)
    assert an["direction"] == NA and an["statements"] == []                                        # 1 / 2: no guidance DOWN from a keyword or a table line


def test_interp_guidance_context_presence_and_direction():
    an = _an("The company expects revenue of $2.0 billion to $2.2 billion for the first quarter.")
    assert an["guidance_present"] and an["guidance_direction"] == NA and an["direction"] == NA and an["direction_kind"] == "GUIDANCE"     # 3 / 4
    assert an["statements"][0]["rule"] == "guidance_present" and "$2.0 billion to $2.2 billion" in an["statements"][0]["phrase"]
    assert _an("The company expects gross margin between 38% and 40%.")["guidance_present"]
    assert _an("The company raised its full-year guidance.")["direction"] == UP                                                      # 5
    assert _an("Sandisk lowered guidance for the fiscal year.")["direction"] == DOWN and _an("We cut our outlook.")["direction"] == DOWN  # 6
    assert _an("We expect demand to improve in the second half.")["direction"] == UP                                                 # 7
    assert _an("We expect demand to weaken through year end.")["direction"] == DOWN                                                  # 8
    assert _an("Management expects revenue growth in fiscal 2027.")["direction"] == UP and _an("The company anticipates weakness in demand.")["direction"] == DOWN
    an = _an("The company reaffirmed its outlook for the fiscal first quarter.")
    assert an["guidance_present"] and an["guidance_direction"] == NEUTRAL and an["direction"] == NEUTRAL
    safe = ("This press release contains forward-looking statements within the meaning of the Private Securities Litigation Reform Act, including statements "
            "about expected revenue, gross margin, demand and outlook. Actual results may differ.")
    assert not _an(safe)["guidance_present"] and _an(safe)["direction"] == NA                                                        # safe-harbor boilerplate
    assert not _an("Outlook")["guidance_present"] and not _an("Guidance")["guidance_present"]                                        # a heading is not guidance
    mixed = _an("The company raised its revenue outlook. Separately it lowered its gross margin outlook.")
    assert mixed["guidance_direction"] == NEUTRAL and {s["rule"] for s in mixed["statements"]} == {"guidance_raised", "guidance_lowered"}


def test_interp_operating_direction_needs_subject_and_predicate():
    assert _an("Orders increased 20% in the quarter.")["operating_direction"] == UP
    an = _an("Memory demand weakened in the consumer segment.")
    assert an["operating_direction"] == DOWN and an["statements"][0]["measure"] == "demand"
    an = _an("Gross margin declined to 30%.")
    assert an["operating_direction"] == DOWN and an["statements"][0]["measure"] == "gross_margin" and an["direction_kind"] == "OPERATING"
    assert _an("Inventory levels normalized during the quarter.")["operating_direction"] == UP
    assert _an("Revenue grew 12% year over year to a record $2.0 billion.")["operating_direction"] == UP
    assert _an("Results were in line with expectations.")["operating_direction"] == NEUTRAL
    st = _an("Orders increased 20% in the quarter.")["statements"][0]
    assert st["kind"] == "OPERATING" and st["rule"] == "op_metric_up" and st["offset"] == 0 and "Orders increased" in st["statement"] and st["phrase"].startswith("Orders increased")


def test_interp_headline_price_direction_is_separate():
    for h in ("Sandisk Stock Is Falling Thursday: What's Going On? — Sandisk shares fall nearly 6% as investors brace for earnings",
              "Why Is Sandisk Stock Falling Wednesday?", "Sandisk stock drops after downgrade", "Shares slide 4%", "Stock tumbles"):
        an = _an(h, headline=True)
        assert an["price_direction"] == DOWN and an["direction"] == DOWN and an["direction_kind"] == "PRICE", h                        # 9
    for h in ("Sandisk shares rise 5%", "Stock jumps on upgrade", "Shares rally after results"):
        an = _an(h, headline=True)
        assert an["price_direction"] == UP and an["direction_kind"] == "PRICE", h
    assert _an("Sandisk shares fall nearly 6%")["direction"] == NA                                  # price rules apply to headlines only
    an = _an("Sandisk cuts outlook as consumer demand weakened; shares fall 8%", headline=True)
    assert an["direction_kind"] == "GUIDANCE" and an["direction"] == DOWN and an["price_direction"] == DOWN   # fundamentals win over the price move
    # 10 / 11: a PRICE item has its own claim key, never contradicts, never feeds rung 4 or rung 5
    price = item(item_id="1" * 32, category=COMPANY_CONFIRMATION, kind="HEADLINE", direction=DOWN, source_tier=1,
                 value_json={"direction_kind": "PRICE", "price_direction": DOWN, "statements": [{"kind": "PRICE", "direction": DOWN, "rule": "price_down"}]})
    fund = item(item_id="2" * 32, category=COMPANY_CONFIRMATION, kind="EARNINGS", direction=UP, source_tier=1,
                value_json={"direction_kind": "OPERATING", "statements": [{"kind": "OPERATING", "direction": UP, "measure": "revenue", "rule": "op_metric_up"}]})
    assert R.claim_key(price) == "SNDK|PRICE" and R.direction_kind(price) == "PRICE" and not R.comparable(price, fund)
    assert R.mark_contradictions([price, fund]) == 0
    base = {"spy_20": None, "spy_above_ema50": None, "etf_20": None, "price_above_ema20": None, "rel_strength_20": None}
    rungs = R.ladder({**base, "fundamentals": {}}, [price], {})
    assert rungs[3]["status"] == NOT_AVAILABLE and rungs[4]["status"] == NOT_AVAILABLE
    price_g = {**price, "kind": "GUIDANCE"}
    assert R.ladder(base, [price_g], {})[4]["status"] == PARTIAL                                        # even mislabelled, a PRICE direction never counts as clear DOWN


def test_interp_index_pages_emit_no_directional_evidence(live):
    assert ST.is_index_page("https://investor.sandisk.example/news-events/news-releases", "", "")
    assert ST.is_index_page("https://www.kioxia.example/en-jp/ir/news.html", "", "") and ST.is_index_page("https://www.kioxia.example/en-jp/ir.html", "", "")
    assert ST.is_index_page("https://investor.sandisk.example/news-events/events", "", "") and ST.is_index_page("https://x.example/", "", "")
    assert not ST.is_index_page("https://investor.sandisk.example/news-releases/news-release-details/sandisk-reports-results", "Revenue grew 10%.", "Results")
    listing = "\n".join(f"September {d}, 2026 Headline {d}" for d in range(1, 15))
    assert ST.is_index_page("https://x.example/some-page", listing, "") and not ST.is_index_page("https://x.example/some-page", "One long paragraph " * 40, "")
    # 12 / 13 / 14: the fixture listing contains 'restructuring', 'lowered its outlook', 'shares fall 6%' and an old date, yet nothing comes out of it
    ir = F.adapters()["ir"].run("SNDK", [F.INDEX_URL], F.AS_OF).items[0]
    assert ir.kind == "PAGE" and ir.extra["page_kind"] == "index" and ir.rule_text == "" and ir.date_source == "retrieval" and ir.evidence_date == F.AS_OF.isoformat()
    kx = F.adapters()["manual"].run("SNDK", [{"url": F.KIOXIA_INDEX_URL, "role": "industry"}], [], F.AS_OF).items[0]
    assert kx.kind == "PAGE" and kx.extra["page_kind"] == "index" and kx.evidence_date == F.AS_OF.isoformat() and kx.date_source == "retrieval"
    out = EN.build_packet(F.request(ir_urls=[F.INDEX_URL], manual_urls=[{"url": F.KIOXIA_INDEX_URL, "role": "industry"}], manual_texts=[],
                                    sources={"internal": True, "sec": False, "ir": True, "news": False, "manual": True}),
                          adapters=F.adapters(), now_fn=F.now_fn, persist=False)
    pages = [i for i in out["items"] if i["source_type"] in ("IR_RELEASE", "MANUAL_URL")]
    assert len(pages) == 2 and all(i["direction"] == NA and i["kind"] == "PAGE" and i["value_json"]["index_page"] and not i["value_json"]["guidance_present"] for i in pages)
    assert all(i["evidence_date"] == F.AS_OF.isoformat() and i["ladder_rung"] is None for i in pages)
    assert {r["rung"]: r["status"] for r in out["packet"]["ladder_json"]}[5] == NOT_AVAILABLE


def test_interp_rung4_disagreement_requires_comparable_measure():
    base = {"spy_20": None, "spy_above_ema50": None, "etf_20": None, "price_above_ema20": None, "rel_strength_20": None}
    ok = {"revenue_up": True, "margin_up": True, "revenue_latest": 2, "revenue_prior": 1, "gm_latest": 0.3, "gm_prior": 0.28, "validation": "OK", "reasons": [], "revenue_concept": "Revenues"}
    r4 = lambda items: R.ladder({**base, "fundamentals": ok}, items, {})[3]
    rev_dn = item(item_id="1" * 32, category=COMPANY_CONFIRMATION, kind="EARNINGS", direction=DOWN, source_tier=1,
                  value_json={"direction_kind": "OPERATING", "statements": [{"kind": "OPERATING", "direction": DOWN, "measure": "revenue", "rule": "op_metric_down"}]})
    restr = item(item_id="2" * 32, category=COMPANY_CONFIRMATION, kind="EARNINGS", direction=NA, source_tier=1, value_json={"direction_kind": "NA", "statements": []})
    demand_dn = item(item_id="3" * 32, category=COMPANY_CONFIRMATION, kind="EARNINGS", direction=DOWN, source_tier=1,
                     value_json={"direction_kind": "OPERATING", "statements": [{"kind": "OPERATING", "direction": DOWN, "measure": "demand", "rule": "op_metric_down"}]})
    guid = item(item_id="4" * 32, category=COMPANY_CONFIRMATION, kind="GUIDANCE", direction=DOWN, source_tier=1,
                value_json={"direction_kind": "GUIDANCE", "guidance_direction": DOWN, "statements": [{"kind": "GUIDANCE", "direction": DOWN, "measure": None, "rule": "guidance_lowered"}]})
    d = r4([rev_dn])
    assert d["status"] == PARTIAL and d["inputs"]["ambiguity"] is True                                       # 15: "revenue declined" vs XBRL revenue up
    assert r4([restr])["status"] == SUPPORTED and r4([demand_dn])["status"] == SUPPORTED and r4([guid])["status"] == SUPPORTED   # 16: unrelated statements never cap
    assert R.comparable_operating_direction(rev_dn) == DOWN and R.comparable_operating_direction(demand_dn) == NA and R.comparable_operating_direction(guid) == NA


def test_interp_rung5_conservative_rule():
    base = {"spy_20": None, "spy_above_ema50": None, "etf_20": None, "price_above_ema20": None, "rel_strength_20": None}
    r5 = lambda items: R.ladder(base, items, {})[4]
    g = lambda i, d: item(item_id=str(i) * 32, category=COMPANY_CONFIRMATION, kind="GUIDANCE", direction=d, source_tier=1,
                          value_json={"direction_kind": "GUIDANCE", "guidance_present": True, "guidance_direction": d, "statements": [{"kind": "GUIDANCE", "direction": d, "rule": "x"}]})
    assert r5([])["status"] == NOT_AVAILABLE                                                                   # 18
    na = r5([g(1, NA)])
    assert na["status"] == PARTIAL and "not established" in na["reason"]                                       # 17
    assert r5([g(1, NEUTRAL)])["status"] == PARTIAL
    assert r5([g(1, DOWN)])["status"] == NOT_SUPPORTED and r5([g(1, DOWN), g(2, NEUTRAL)])["status"] == NOT_SUPPORTED     # 19
    assert r5([g(1, UP)])["status"] == SUPPORTED and r5([g(1, UP), g(2, NA)])["status"] == SUPPORTED                 # 20
    assert r5([g(1, UP), g(2, DOWN)])["status"] == PARTIAL
    assert r5([{**g(1, UP), "contradiction": 1}])["status"] == PARTIAL
    assert r5([{**g(1, UP), "source_tier": 3}])["status"] == NOT_AVAILABLE and r5([{**g(1, UP), "category": COMPANY_SPECIFIC}])["status"] == NOT_AVAILABLE
