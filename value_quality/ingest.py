"""value_quality/ingest.py — the SEPARATED data-ingestion step of Stage 6.4 (the only network access; the replay itself is offline).

    python -m value_quality.ingest fetch --out runtime/vq [--env-file PATH] [--start 2020-07-27] [--end YYYY-MM-DD]   network: raw responses only
    python -m value_quality.ingest build --out runtime/vq                                                             offline: raw -> vq_bundle.json

fetch (read only, public / licensed market data): SEC EDGAR (company_tickers.json -> CIK; companyfacts XBRL; submissions -> SIC) and Alpaca market
data (IEX daily bars: RAW, SPLIT-adjusted and ALL-adjusted). Every response is kept gzipped under <out>/raw with its SHA-256, URL and fetch time
in <out>/manifest.json. build re-creates the validation bundle from those archived responses alone (verifying every SHA-256), so a bundle can be
rebuilt and audited without the network. Nothing here places, previews or cancels an order; no broker endpoint is called (market data only).
Output stays under runtime/ (git-ignored)."""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import time
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Dict, List

from value_quality import ALL_CONCEPTS, BENCHMARK, CONCEPTS, FEASIBILITY_TAGS, RESEARCH_UNIVERSE

SEC_MIN_INTERVAL_S = 0.15                        # < 10 requests per second (SEC fair-access policy)
DEFAULT_START = "2020-07-27"                     # the first session of Alpaca's IEX daily history
ADJUSTMENTS = ("all", "raw", "split")


def _sec_symbol(sym: str) -> str:
    return sym.replace(".", "-")


class Fetch:
    def __init__(self, out: Path, user_agent: str, alpaca_key: str, alpaca_secret: str):
        if not user_agent:
            raise SystemExit("SEC_USER_AGENT is required (SEC fair-access policy: 'Name email')")
        self.out, self.ua, self.key, self.secret = Path(out), user_agent, alpaca_key, alpaca_secret
        (self.out / "raw").mkdir(parents=True, exist_ok=True)
        self.manifest: List[dict] = []
        self._last = 0.0

    def _keep(self, name: str, url: str, body: bytes) -> None:
        (self.out / "raw" / f"{name}.json.gz").write_bytes(gzip.compress(body, mtime=0))
        self.manifest.append({"name": name, "url": url, "sha256": hashlib.sha256(body).hexdigest(), "bytes": len(body),
                              "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds")})

    def sec(self, url: str, name: str):
        wait = SEC_MIN_INTERVAL_S - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        req = urllib.request.Request(url, headers={"User-Agent": self.ua, "Accept-Encoding": "identity"})
        with urllib.request.urlopen(req, timeout=90) as r:
            body = r.read()
        self._last = time.monotonic()
        self._keep(name, url, body)
        return json.loads(body)

    def bars(self, symbols: List[str], start: str, end: str, adjustment: str) -> None:
        from alpaca.data.enums import Adjustment, DataFeed
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame
        cli = StockHistoricalDataClient(self.key, self.secret)
        out: Dict[str, List[dict]] = {}
        for i in range(0, len(symbols), 20):
            chunk = symbols[i:i + 20]
            req = StockBarsRequest(symbol_or_symbols=chunk, timeframe=TimeFrame.Day, start=datetime.fromisoformat(start).replace(tzinfo=timezone.utc),
                                   end=datetime.fromisoformat(end).replace(hour=23, tzinfo=timezone.utc), adjustment=Adjustment(adjustment), feed=DataFeed.IEX)
            data = cli.get_stock_bars(req).data
            for sym in chunk:
                out[sym] = [{"date": b.timestamp.date().isoformat(), "open": float(b.open), "high": float(b.high), "low": float(b.low), "close": float(b.close),
                             "volume": float(b.volume or 0)} for b in data.get(sym, [])]
        self._keep(f"alpaca_bars_{adjustment}", f"alpaca://bars/1Day/iex/{adjustment}?start={start}&end={end}", json.dumps(out, sort_keys=True).encode("utf-8"))


def fetch(out: Path, user_agent: str, key: str, secret: str, start: str, end: str) -> Path:
    f = Fetch(out, user_agent, key, secret)
    tickers = f.sec("https://www.sec.gov/files/company_tickers.json", "sec_company_tickers")
    cik_of = {str(v["ticker"]).upper(): int(v["cik_str"]) for v in tickers.values()}
    done = set()
    for sym in RESEARCH_UNIVERSE:
        cik = cik_of.get(_sec_symbol(sym))
        if cik is None or cik in done:
            continue
        done.add(cik)
        f.sec(f"https://data.sec.gov/submissions/CIK{cik:010d}.json", f"sec_submissions_{cik:010d}")
        f.sec(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json", f"sec_companyfacts_{cik:010d}")
    for adj in ADJUSTMENTS:
        f.bars(list(RESEARCH_UNIVERSE) + [BENCHMARK], start, end, adj)
    path = Path(out) / "manifest.json"
    path.write_text(json.dumps({"retrieved_at": max(m["fetched_at"] for m in f.manifest), "start": start, "end": end, "responses": f.manifest}, indent=1),
                    encoding="utf-8")
    return path


def _raw(out: Path, name: str, manifest: Dict[str, dict]):
    body = gzip.decompress((Path(out) / "raw" / f"{name}.json.gz").read_bytes())
    if hashlib.sha256(body).hexdigest() != manifest[name]["sha256"]:
        raise SystemExit(f"{name}: SHA-256 differs from the manifest")
    return json.loads(body)


def _facts(cf: dict) -> Dict[str, List[dict]]:
    keep = {}
    g = (cf.get("facts") or {}).get("us-gaap") or {}
    for tag in list(ALL_CONCEPTS) + list(FEASIBILITY_TAGS):
        f = g.get(tag)
        if not f:
            continue
        unit = "shares" if tag in CONCEPTS["share_count"] else "USD"
        rows = (f.get("units") or {}).get(unit) or []
        keep[tag] = sorted(({k: r.get(k) for k in ("start", "end", "val", "filed", "form", "fy", "fp", "accn")} for r in rows),
                           key=lambda r: (r["end"], r.get("start") or "", r["filed"], r["accn"]))
    return keep


def build(out: Path) -> Path:
    """Offline: the validation bundle from the archived raw responses only (every SHA-256 verified against the manifest)."""
    man = json.loads((Path(out) / "manifest.json").read_text(encoding="utf-8"))
    by = {m["name"]: m for m in man["responses"]}
    tickers = _raw(out, "sec_company_tickers", by)
    cik_of = {str(v["ticker"]).upper(): int(v["cik_str"]) for v in tickers.values()}
    issuers, fundamentals, missing = {}, {}, []
    for sym in RESEARCH_UNIVERSE:
        cik = cik_of.get(_sec_symbol(sym))
        if cik is None:
            missing.append(sym)
            continue
        cik10 = f"{cik:010d}"
        if cik10 not in fundamentals:
            sub = _raw(out, f"sec_submissions_{cik10}", by)
            fundamentals[cik10] = _facts(_raw(out, f"sec_companyfacts_{cik10}", by))
            issuers[cik10] = {"name": sub.get("name"), "sic": int(sub["sic"]) if str(sub.get("sic") or "").isdigit() else None,
                              "sic_description": sub.get("sicDescription"), "fiscal_year_end": sub.get("fiscalYearEnd"), "symbols": []}
        issuers[cik10]["symbols"].append(sym)
    bars = {adj: _raw(out, f"alpaca_bars_{adj}", by) for adj in ADJUSTMENTS}
    all_bars, removed = {}, []                                                       # an invalid bar becomes a gap: never repaired, always listed
    for sym, rows in bars["all"].items():
        keep = []
        for r in rows:
            o, h, lo, c = r["open"], r["high"], r["low"], r["close"]
            if min(o, h, lo, c) <= 0 or lo > min(o, c) + 1e-9 or h < max(o, c) - 1e-9:
                removed.append({"symbol": sym, "date": r["date"], "ohlc": [o, h, lo, c]})
            else:
                keep.append(r)
        all_bars[sym] = keep
    series = {"raw_close": {}, "raw_volume": {}, "split_ratio": {}}
    for sym in list(RESEARCH_UNIVERSE) + [BENCHMARK]:
        sp = {b["date"]: b["close"] for b in bars["split"].get(sym, [])}
        series["raw_close"][sym] = {b["date"]: b["close"] for b in bars["raw"].get(sym, [])}
        series["raw_volume"][sym] = {b["date"]: b["volume"] for b in bars["raw"].get(sym, [])}
        series["split_ratio"][sym] = {d: round(c / sp[d], 6) for d, c in series["raw_close"][sym].items() if sp.get(d)}   # raw / split-adjusted
    provenance = {
        "source": "SEC EDGAR XBRL companyfacts + submissions (SIC); Alpaca market data, IEX feed, daily bars (raw, split-adjusted, all-adjusted)",
        "retrieved_at": man["retrieved_at"], "bar_frequency": "1Day",
        "adjustment": "P/L on all-adjusted bars (splits + dividends: total return); price filters and market cap on RAW closes with the share count "
                      "re-based by the split ratio (raw / split-adjusted close)",
        "missing_data_policy": "a missing session is a gap, never filled; a bar with a non-positive or inconsistent OHLC field is removed (a gap, listed "
                               "in invalid_bars_removed); a missing fundamental makes its factor missing (never zero); a share count below 1M is an "
                               "XBRL scale error and is rejected (never rescaled); a stock without >= 2 value and >= 2 quality factors is not ranked; an "
                               "issuer without a SEC CIK is excluded",
        "corporate_actions": "splits / reverse splits from the raw / split-adjusted close ratio (share counts re-based from their filing date to the "
                             "rebalance); dividends only in the all-adjusted P/L series; symbol changes: none recorded (the universe is keyed by "
                             "today's symbols)",
        "universe": "fixed research universe: the 81 stocks of the Stage 3.2 store (today's large caps), minus SEC SIC 6000-6799, one listing per "
                    "issuer (CIK); point-in-time constituents and delisted securities are NOT available: SURVIVORSHIP_BIAS_PRESENT",
        "point_in_time": "a SEC fact is visible from the session after its filing date; per period the highest-priority concept visible at the "
                         "rebalance wins and within it the latest filing (a restatement is visible only after it was filed); period_end, filed, "
                         "available_at, concept and accession are kept per value; SIC is today's classification (not point in time)",
        "sec_symbols_without_cik": missing, "start": man["start"], "end": man["end"], "invalid_bars_removed": removed,
        "raw_manifest_sha256": hashlib.sha256((Path(out) / "manifest.json").read_bytes()).hexdigest(),
    }
    bundle = {"kind": "HISTORICAL", "provenance": provenance, "bars": all_bars,
              "aux": {"series": series, "events": {"facts": fundamentals}, "static": {"issuers": issuers}}}
    path = Path(out) / "vq_bundle.json"
    path.write_text(json.dumps(bundle, sort_keys=True), encoding="utf-8")
    return path


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m value_quality.ingest", description="Stage 6.4 data ingestion (market data + SEC; never orders)")
    sub = p.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch")
    f.add_argument("--out", default="runtime/vq")
    f.add_argument("--env-file", default="")
    f.add_argument("--start", default=DEFAULT_START)
    f.add_argument("--end", default=date.today().isoformat())
    b = sub.add_parser("build")
    b.add_argument("--out", default="runtime/vq")
    a = p.parse_args(argv)
    if a.cmd == "fetch":
        import os
        env = dict(os.environ)
        if a.env_file:
            from dotenv import dotenv_values
            env.update({k: v for k, v in dotenv_values(a.env_file).items() if v})
        print(f"manifest: {fetch(Path(a.out), env.get('SEC_USER_AGENT', ''), env.get('ALPACA_API_KEY', ''), env.get('ALPACA_SECRET_KEY', ''), a.start, a.end)}")
        return 0
    print(f"bundle: {build(Path(a.out))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
