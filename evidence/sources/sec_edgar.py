"""
evidence/sources/sec_edgar.py — SEC EDGAR adapter (DESIGN_54 §3). Official filings of the target and of declared related tickers.

Requests, in this fixed order and never more than `max_requests` per build:
  1. company_tickers.json             ticker -> CIK (skipped for tickers with a configured override)
  2. submissions/CIK##########.json   recent filings; 8-K (items 2.02 / 7.01 / 8.01), 10-Q, 10-K inside the window
  3. companyfacts/CIK##########.json  quarterly revenue and gross profit -> one computed METRIC item
  4. for up to `max_8k` 8-Ks of the target: the filing's index.json, then its EX-99 press release (else the primary document)
  5. for each related ticker (budget permitting): submissions, then index + document of its latest 8-K 2.02
SEC requires a descriptive User-Agent ("Name email"): the adapter refuses to run without one and records a note instead.
A filing whose document was not fetched (budget exhausted) still yields a metadata-only FACT item from the submissions index.
"""
from __future__ import annotations

import json
import re
import time
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional

import config
from evidence import COMPANY_CONFIRMATION, LIMITS
from evidence import extract as X
from evidence import net
from evidence.sources.base import AdapterResult, SourceItem

HOSTS = ("sec.gov",)
TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
FORMS = ("8-K", "10-Q", "10-K")
EIGHT_K_ITEMS = ("2.02", "7.01", "8.01")
REVENUE_CONCEPTS = ("Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax", "SalesRevenueNet")
_EX99 = re.compile(r"ex[-_]?99", re.I)


def submissions_url(cik10: str) -> str:
    return f"https://data.sec.gov/submissions/CIK{cik10}.json"


def companyfacts_url(cik10: str) -> str:
    return f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik10}.json"


def index_url(cik10: str, accession: str) -> str:
    return f"https://www.sec.gov/Archives/edgar/data/{int(cik10)}/{accession.replace('-', '')}/index.json"


def doc_url(cik10: str, accession: str, filename: str) -> str:
    return f"https://www.sec.gov/Archives/edgar/data/{int(cik10)}/{accession.replace('-', '')}/{filename}"


class SecAdapter:
    name = "sec_edgar"

    def __init__(self, *, fetch_fn: Callable = net.fetch, now_fn: Optional[Callable[[], datetime]] = None, user_agent: Optional[str] = None,
                 max_requests: Optional[int] = None, min_interval_s: Optional[float] = None, sleep_fn: Callable[[float], None] = time.sleep, max_8k: int = 2):
        self.fetch_fn, self.now_fn, self.sleep_fn, self.max_8k = fetch_fn, now_fn or (lambda: datetime.now(timezone.utc)), sleep_fn, max_8k
        self.user_agent = config.SEC_USER_AGENT if user_agent is None else user_agent
        self.max_requests = int(config.EVIDENCE_SEC_MAX_REQUESTS if max_requests is None else max_requests)
        self.min_interval_s = float(config.EVIDENCE_SEC_MIN_INTERVAL_S if min_interval_s is None else min_interval_s)
        self._last = None

    # ---------------------------------------------------------------- transport --------------------------------------------
    def _get(self, res: AdapterResult, url: str, purpose: str, max_bytes: Optional[int] = None) -> Optional[net.FetchResult]:
        if res.requests >= self.max_requests:
            res.notes.append(f"SEC request budget ({self.max_requests}) exhausted before {purpose}: {url}")
            return None
        if self._last is not None and self.min_interval_s > 0:
            self.sleep_fn(self.min_interval_s)
        self._last = True
        cap = {} if max_bytes is None else {"max_bytes": int(max_bytes)}      # Evidence refresh, Stage 5.4.2: a per-endpoint cap (companyfacts only)
        fr = self.fetch_fn(url, purpose=purpose, allowed_hosts=HOSTS, headers={"User-Agent": self.user_agent}, now_fn=self.now_fn, **cap)
        res.add_fetch(fr)
        return fr

    @staticmethod
    def _json(fr: Optional[net.FetchResult]):
        if fr is None or not fr.ok:
            return None
        try:
            return json.loads(fr.text)
        except ValueError:
            fr.status, fr.error = "PARSE_FAILED", "body is not JSON"
            return None

    # ---------------------------------------------------------------- pieces -----------------------------------------------
    def cik_for(self, res: AdapterResult, tickers: List[str], overrides: Dict[str, str]) -> Dict[str, Optional[str]]:
        out: Dict[str, Optional[str]] = {t: (str(overrides[t]).zfill(10) if t in overrides else None) for t in tickers}
        if all(out.values()):
            return out
        data = self._json(self._get(res, TICKERS_URL, "ticker -> CIK map"))
        if isinstance(data, dict):
            by = {str(v.get("ticker", "")).upper(): str(v.get("cik_str", "")).zfill(10) for v in data.values() if isinstance(v, dict)}
            for t in tickers:
                if out[t] is None:
                    out[t] = by.get(t)
                    if out[t] is None:
                        res.notes.append(f"{t}: not in the SEC ticker map (set EVIDENCE_CIK_JSON to override)")
        else:
            res.notes.append("SEC ticker map unavailable; tickers without an override are skipped")
        return out

    def filings(self, res: AdapterResult, ticker: str, cik10: str, start: date, end: date) -> List[dict]:
        fr = self._get(res, submissions_url(cik10), f"{ticker} filings index")
        data = self._json(fr)
        if not isinstance(data, dict):
            return []
        recent = (data.get("filings") or {}).get("recent") or {}
        rows = []
        n = len(recent.get("form", []))
        for i in range(n):
            form = recent["form"][i]
            fdate = recent.get("filingDate", [""] * n)[i]
            if form not in FORMS or not (start.isoformat() <= fdate <= end.isoformat()):
                continue
            items = [x.strip() for x in str(recent.get("items", [""] * n)[i] or "").split(",") if x.strip()]
            if form == "8-K" and not any(x in EIGHT_K_ITEMS for x in items):
                continue
            rows.append({"form": form, "filing_date": fdate, "accession": recent.get("accessionNumber", [""] * n)[i],
                         "primary": recent.get("primaryDocument", [""] * n)[i], "items": items, "report_date": recent.get("reportDate", [""] * n)[i],
                         "fetch_index": len(res.fetches) - 1, "source_hash": fr.content_hash if fr else None})
        rows.sort(key=lambda r: (r["filing_date"], r["accession"]), reverse=True)
        return rows

    def document(self, res: AdapterResult, ticker: str, cik10: str, filing: dict) -> Optional[dict]:
        idx = self._json(self._get(res, index_url(cik10, filing["accession"]), f"{ticker} {filing['form']} {filing['accession']} index"))
        if idx is None:
            return None
        names = [it.get("name", "") for it in ((idx.get("directory") or {}).get("item") or []) if isinstance(it, dict)]
        chosen = next((nm for nm in names if _EX99.search(nm) and nm.lower().endswith((".htm", ".html", ".txt"))), None) or filing["primary"]
        if not chosen:
            return None
        fr = self._get(res, doc_url(cik10, filing["accession"], chosen), f"{ticker} {filing['form']} {filing['accession']} document")
        if fr is None or not fr.ok:
            return None
        text, title, _meta = X.parse_html(fr.text)
        return {"text": text, "title": title, "hash": fr.content_hash, "url": fr.final_url, "fetch_index": len(res.fetches) - 1}

    def fundamentals(self, res: AdapterResult, ticker: str, cik10: str, as_of: date) -> Optional[dict]:
        fr = self._get(res, companyfacts_url(cik10), f"{ticker} company facts (XBRL)",
                       max_bytes=config.EVIDENCE_SEC_FACTS_MAX_BYTES)   # Evidence refresh, Stage 5.4.2: large caps' companyfacts exceed EVIDENCE_MAX_BYTES
        data = self._json(fr)
        if not isinstance(data, dict):
            return None
        gaap = (data.get("facts") or {}).get("us-gaap") or {}

        def quarters(concept: str) -> Dict[str, dict]:
            """Quarterly (80-100 day) USD facts by period end, filed on or before as_of; the latest filing wins per period.
            A period reported with conflicting values across filings is remembered (`conflict`) for the validation below."""
            out: Dict[str, dict] = {}
            for e in ((gaap.get(concept) or {}).get("units") or {}).get("USD", []):
                try:
                    s, en = date.fromisoformat(e["start"]), date.fromisoformat(e["end"])
                except (KeyError, ValueError, TypeError):
                    continue
                filed = str(e.get("filed") or "")
                if filed and filed > as_of.isoformat():
                    continue                                                    # point-in-time: facts filed after as_of did not exist yet
                if not (80 <= (en - s).days <= 100) or en > as_of:
                    continue
                key = en.isoformat()
                prev = out.get(key)
                if prev is not None and abs(float(prev["val"]) - float(e["val"])) > 0.005 * max(abs(float(prev["val"])), 1.0):
                    e = {**e, "conflict": True}
                if prev is None or filed >= str(prev.get("filed") or ""):
                    out[key] = {**e, "conflict": bool(e.get("conflict") or (prev or {}).get("conflict"))}
            return out

        by_concept = {c: quarters(c) for c in REVENUE_CONCEPTS}
        concept = next((c for c in REVENUE_CONCEPTS if len(by_concept[c]) >= 2), None)
        if concept is None:
            res.notes.append(f"{ticker}: fewer than two quarterly revenue facts in company facts")
            return None
        rev = by_concept[concept]
        gp = quarters("GrossProfit")
        ends = sorted(rev)
        latest, prior = ends[-1], ends[-2]
        r1, r0 = float(rev[latest]["val"]), float(rev[prior]["val"])
        reasons: List[str] = []
        # --- deterministic validation (DESIGN_54 §5, rung 4): concept identity, period type, fiscal context, units, GP/revenue relation ---
        others = [c for c in REVENUE_CONCEPTS if c != concept and len(by_concept[c]) >= 1]
        for c in others:
            for end_, e in by_concept[c].items():
                if end_ in rev and abs(float(e["val"]) - float(rev[end_]["val"])) > 0.005 * max(abs(float(rev[end_]["val"])), 1.0):
                    reasons.append(f"revenue concepts disagree for {end_} ({concept} vs {c})")
                    break
        if rev[latest].get("conflict") or rev[prior].get("conflict"):
            reasons.append("conflicting revenue values for the same period across filings")
        gap_days = (date.fromisoformat(latest) - date.fromisoformat(prior)).days
        if not 80 <= gap_days <= 100:
            reasons.append(f"quarters are not consecutive ({gap_days} days apart)")
        for lbl, e in (("latest", rev[latest]), ("prior", rev[prior])):
            if str(e.get("fp") or "") not in ("Q1", "Q2", "Q3", "Q4") or not str(e.get("form") or "").startswith(("10-Q", "10-K")):
                reasons.append(f"{lbl} quarter lacks a quarterly fiscal context (fp={e.get('fp')}, form={e.get('form')})")
        if r1 <= 0 or r0 <= 0:
            reasons.append("non-positive revenue value")
        gm1 = gm0 = None
        if latest in gp and prior in gp and gp[latest].get("start") == rev[latest].get("start") and gp[prior].get("start") == rev[prior].get("start"):
            g1, g0 = float(gp[latest]["val"]), float(gp[prior]["val"])
            if 0 <= g1 <= r1 and 0 <= g0 <= r0 and r1 and r0:
                gm1, gm0 = g1 / r1, g0 / r0
                if abs(gm1 - gm0) > 0.20:
                    reasons.append(f"gross margin moved {abs(gm1 - gm0) * 100:.0f} points q/q; needs confirmation from official filing text")
            else:
                reasons.append("gross profit outside [0, revenue] for a period — concepts not comparable")
        else:
            reasons.append("gross profit periods missing or not aligned with the revenue periods — gross margin not derived")
        validation = "OK" if not reasons else "AMBIGUOUS"
        return {"revenue_concept": concept, "revenue_latest": r1, "revenue_prior": r0, "quarter_latest": latest, "quarter_prior": prior, "gm_latest": gm1, "gm_prior": gm0,
                "revenue_up": r1 > r0, "margin_up": (None if gm1 is None or gm0 is None else gm1 >= gm0), "validation": validation, "reasons": reasons,
                "fiscal_context": {"latest": {k: rev[latest].get(k) for k in ("fy", "fp", "form", "filed", "accn")}, "prior": {k: rev[prior].get(k) for k in ("fy", "fp", "form", "filed", "accn")}},
                "hash": fr.content_hash, "fetch_index": len(res.fetches) - 1, "url": fr.final_url, "filed": rev[latest].get("filed")}

    # ---------------------------------------------------------------- run --------------------------------------------------
    def run(self, ticker: str, related: Dict[str, List[str]], as_of: date, lookback_days: int, cik_overrides: Optional[Dict[str, str]] = None) -> AdapterResult:
        res = AdapterResult(adapter=self.name)
        if not self.user_agent:
            res.notes.append("SEC adapter skipped: SEC_USER_AGENT is not configured (SEC requires 'Name email')")
            return res
        retrieved = self.now_fn().isoformat(timespec="seconds")
        start = as_of - timedelta(days=lookback_days)
        rel_tickers = [t.upper() for group in related.values() for t in group]
        ciks = self.cik_for(res, [ticker] + rel_tickers, {k.upper(): v for k, v in (cik_overrides or config.EVIDENCE_CIK_OVERRIDES or {}).items()})
        cik = ciks.get(ticker)
        if not cik:
            return res
        docs_done = 0
        for f in self.filings(res, ticker, cik, start, end=as_of):
            item = self._filing_item(ticker, ticker, "company", f, retrieved)
            if f["form"] == "8-K" and docs_done < self.max_8k:
                d = self.document(res, ticker, cik, f)
                if d:
                    docs_done += 1
                    item = self._enrich(item, d)
            res.items.append(item)
        fund = self.fundamentals(res, ticker, cik, as_of)
        if fund:
            res.facts["fundamentals"] = {k: fund[k] for k in ("revenue_concept", "revenue_latest", "revenue_prior", "quarter_latest", "quarter_prior", "gm_latest", "gm_prior",
                                                               "revenue_up", "margin_up", "validation", "reasons", "fiscal_context")}
            res.items.append(SourceItem(
                subject_ticker=ticker, source_type="SEC_FILING", source_name="SEC XBRL company facts", source_url=fund["url"],
                source_identifier=f"companyfacts CIK{cik}", published_at=fund.get("filed"), retrieved_at=retrieved, evidence_date=fund["quarter_latest"],
                kind="METRIC", role="company", category=COMPANY_CONFIRMATION, source_hash=fund["hash"], fetch_index=fund["fetch_index"],
                value=res.facts["fundamentals"],
                text=(f"Quarterly revenue {fund['quarter_latest']}: {fund['revenue_latest']:,.0f} USD vs prior quarter {fund['quarter_prior']}: "
                      f"{fund['revenue_prior']:,.0f} USD; gross margin {_pct(fund['gm_latest'])} vs {_pct(fund['gm_prior'])} (SEC XBRL, as filed)")))
        for role, group in (("supplier", related.get("suppliers", [])), ("customer", related.get("customers", []))):
            for rt in [t.upper() for t in group]:
                rcik = ciks.get(rt)
                if not rcik:
                    continue
                rows = [f for f in self.filings(res, rt, rcik, start, as_of) if f["form"] == "8-K"]
                for f in rows[:1]:
                    item = self._filing_item(ticker, rt, role, f, retrieved)
                    d = self.document(res, rt, rcik, f)
                    if d:
                        item = self._enrich(item, d)
                    res.items.append(item)
        return res

    @staticmethod
    def _filing_item(ticker: str, subject: str, role: str, f: dict, retrieved: str) -> SourceItem:
        kind = "EARNINGS" if ("2.02" in f["items"]) else "FILING"
        text = f"{subject} filed Form {f['form']} on {f['filing_date']}" + (f" (items {', '.join(f['items'])})" if f["items"] else "") + f"; accession {f['accession']}."
        return SourceItem(subject_ticker=subject, source_type="SEC_FILING", source_name=f"SEC EDGAR {f['form']}", source_url=None, source_identifier=f["accession"],
                          published_at=f["filing_date"], retrieved_at=retrieved, evidence_date=f["filing_date"], kind=kind, text=text, role=role,
                          source_hash=f.get("source_hash"), fetch_index=f.get("fetch_index"), extra={"form": f["form"], "items": f["items"], "report_date": f.get("report_date")})

    @staticmethod
    def _enrich(item: SourceItem, d: dict) -> SourceItem:
        rule_text = ((f"{d['title']}: " if d["title"] else "") + d["text"])[:LIMITS["rule_text_chars"]]   # rules see the whole document
        body = X.snippet(d["text"], LIMITS["text_chars"] - 120)
        item.text = (f"{d['title']}: " if d["title"] else "") + body if body else item.text                # display snippet only
        item.rule_text = rule_text                                                                # GUIDANCE is decided by the statement analysis
        item.source_url, item.source_hash, item.fetch_index = d["url"], d["hash"], d["fetch_index"]
        return item


def _pct(x) -> str:
    return "n/a" if x is None else f"{x:.1%}"
