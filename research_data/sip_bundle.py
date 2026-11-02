"""research_data/sip_bundle.py — the isolated SIP daily-bar research dataset (Stage 7.8B-1). Three separate steps:

    python -m research_data.sip_bundle plan  --out PLAN.json [--start 2016-01-04] [--end YYYY-MM-DD]       offline: the exact request list + its sha256
    python -m research_data.sip_bundle fetch --plan PLAN.json --plan-sha256 HASH --approval "OPERATOR-APPROVAL:<ref>" --out DIR [--env-file PATH]
                                                                                                         network: raw responses only (GET)
    python -m research_data.sip_bundle build --out DIR                                                     offline: raw -> sip_bundle.json

plan writes every request the fetch may make: per adjustment (all, raw, split) the 81 research stocks + SPY in chunks of 20, feed=sip, one
window [start 00:00Z, the day after end 00:00Z) (the endpoint's end is inclusive and a daily bar is stamped midnight New York, so this end
takes the end session and nothing after it), plus SEC's company_tickers.json (ticker -> CIK). fetch REFUSES without an operator approval
reference and the plan's sha256; it only GETs the plan's URLs, follows next_page_token inside the same request (at most MAX_PAGES_PER_REQUEST
pages, else it stops), keeps every response gzipped with its sha256 and never writes or prints a credential. build re-creates the dataset
from the archived responses alone (every sha256 verified), declares volume_feed "SIP", and never repairs a bar: an invalid bar becomes a gap,
listed. The Stage 6.4 IEX builder (value_quality/ingest.py) is not used and not changed."""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
import os
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from research_data import (ADJUSTMENTS, ALLOWED_URLS, API_FEED, BARS_URL, BENCHMARK, BUILDER_VERSION, CHUNK, DAILY_BAR_STAMPS, DEFAULT_START, FEED,
                           MAX_PAGES_PER_REQUEST, MIN_INTERVAL_S, PAGE_LIMIT, RESEARCH_UNIVERSE, SEC_MIN_INTERVAL_S, SEC_TICKERS_URL, approved,
                           output_problem)

BUNDLE_NAME = "sip_bundle.json"
BARS_PARAMS = {"symbols", "timeframe", "start", "end", "feed", "adjustment", "limit", "sort"}   # exactly these, per bar request


class BuildError(ValueError):
    pass


class FetchRefused(PermissionError):
    pass


def _canon(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha256_of(obj) -> str:
    return hashlib.sha256(_canon(obj)).hexdigest()


def sec_symbol(sym: str) -> str:
    return sym.replace(".", "-")


def symbols() -> List[str]:
    return list(RESEARCH_UNIVERSE) + [BENCHMARK]


# ---------------------------------------------------------------- the plan (offline) ----------------------------------------------------------------
def request_plan(start: str = DEFAULT_START, end: Optional[str] = None, universe: Optional[List[str]] = None) -> dict:
    end = end or (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()   # completed sessions only (the fetch refuses today)
    if not (date.fromisoformat(start) <= date.fromisoformat(end)):
        raise ValueError("start must not be after end")
    syms = list(universe or symbols())
    window = {"start": f"{start}T00:00:00Z", "end": f"{(date.fromisoformat(end) + timedelta(days=1)).isoformat()}T00:00:00Z"}
    reqs = [{"name": "sec_company_tickers", "kind": "SEC", "url": SEC_TICKERS_URL, "params": {}}]
    for adj in ADJUSTMENTS:
        for k in range(0, len(syms), CHUNK):
            chunk = syms[k:k + CHUNK]
            reqs.append({"name": f"bars_{adj}_{k // CHUNK:02d}", "kind": "BARS", "url": BARS_URL, "adjustment": adj, "symbols": chunk,
                         "params": {"symbols": ",".join(chunk), "timeframe": "1Day", **window, "feed": API_FEED, "adjustment": adj,
                                    "limit": PAGE_LIMIT, "sort": "asc"}})
    return {"builder": BUILDER_VERSION, "feed": FEED, "start": start, "end": end, "window": window, "universe": syms, "requests": reqs,
            "guards": {"method": "GET", "allowed_urls": list(ALLOWED_URLS), "max_pages_per_request": MAX_PAGES_PER_REQUEST, "page_limit": PAGE_LIMIT,
                       "min_interval_s": MIN_INTERVAL_S, "sec_min_interval_s": SEC_MIN_INTERVAL_S}}


# ---------------------------------------------------------------- the fetch (network; refused without approval) ---------------------------------------
Transport = Callable[[str, dict, dict], Tuple[int, bytes]]          # (url, params, headers) -> (status, body); GET only


def http_transport(timeout: float = 30.0) -> Transport:
    import requests                                                         # only here: the network path

    def get(url: str, params: dict, headers: dict) -> Tuple[int, bytes]:
        if url not in ALLOWED_URLS:
            raise FetchRefused(f"not an allowed URL: {url}")
        r = requests.get(url, params=params, headers=headers, timeout=timeout, allow_redirects=False)   # a 3xx is a non-200: the fetch stops
        return r.status_code, r.content
    get.network = True                                                      # type: ignore[attr-defined]  its archive is HISTORICAL
    return get


def _credentials(env_file: Optional[str]) -> Tuple[str, str, str]:
    """(alpaca key, alpaca secret, SEC user agent) by NAME: from an explicit env_file first, else the process environment. Never printed or
    stored."""
    vals = {}
    if env_file and Path(env_file).is_file():
        for line in Path(env_file).read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if "=" in s and not s.startswith("#"):
                k, v = s.split("=", 1)
                vals[k.strip()] = v.strip().strip('"').strip("'")
    get = lambda n: (vals.get(n) or os.environ.get(n) or "").strip()  # noqa: E731
    return get("ALPACA_API_KEY"), get("ALPACA_SECRET_KEY"), get("SEC_USER_AGENT")


def fetch(plan: dict, *, out: Path, approval: str, plan_sha256: str, transport: Transport, alpaca_key: str, alpaca_secret: str, sec_user_agent: str,
          sleep: Callable[[float], None] = time.sleep, now: Callable[[], str] = lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")) -> Path:
    """The plan's requests, once each (pagination inside a request only). Refused without an approval reference and the plan's own sha256."""
    if not approved(approval):
        raise FetchRefused("a fetch needs an operator approval reference (OPERATOR-APPROVAL:<reference>)")
    if not isinstance(plan_sha256, str) or plan_sha256 != sha256_of(plan):
        raise FetchRefused("the plan's sha256 does not match the approved one")
    if plan.get("builder") != BUILDER_VERSION or set(plan.get("guards", {}).get("allowed_urls") or []) != set(ALLOWED_URLS):
        raise FetchRefused("not a plan of this builder")
    win = plan.get("window")
    if not isinstance(win, dict) or not win.get("start") or not win.get("end"):
        raise FetchRefused("the plan has no window")
    allowed_syms = set(symbols())
    for req in plan.get("requests") or []:                                       # every request is checked before the first one is made
        p = req.get("params") or {}
        if req.get("url") not in ALLOWED_URLS or req.get("kind") not in ("SEC", "BARS") or (req["kind"] == "SEC") != (req["url"] == SEC_TICKERS_URL):
            raise FetchRefused(f"{req.get('name')}: not an allowed request")
        if req["kind"] == "SEC" and p:
            raise FetchRefused(f"{req.get('name')}: the SEC request takes no parameter")
        if req["kind"] == "BARS" and (set(p) != BARS_PARAMS or p.get("feed") != API_FEED or p.get("timeframe") != "1Day" or p.get("sort") != "asc"
                                      or not isinstance(p.get("limit"), int) or p["limit"] > PAGE_LIMIT
                                      or (p.get("start"), p.get("end")) != (win["start"], win["end"])
                                      or p.get("adjustment") not in ADJUSTMENTS or p.get("adjustment") != req.get("adjustment")
                                      or not req.get("symbols") or p.get("symbols") != ",".join(req["symbols"]) or not set(req["symbols"]) <= allowed_syms):
            raise FetchRefused(f"{req.get('name')}: outside the builder's scope (parameters, feed, timeframe, page size, window, adjustment or "
                               "symbols)")
    if str(win.get("end") or "9999")[:10] > now()[:10]:                          # the window's end is exclusive: the last day is before today
        raise FetchRefused(f"the window ends {win.get('end')}: only sessions before today (UTC) - the current session's bar may be incomplete")
    if not (alpaca_key and alpaca_secret and sec_user_agent):
        raise FetchRefused("credentials (ALPACA_API_KEY, ALPACA_SECRET_KEY) and SEC_USER_AGENT are required (read by name only)")
    out = Path(out)
    problem = output_problem(out)
    if problem:
        raise FetchRefused(problem)
    if out.exists() and any(out.iterdir()):
        raise FetchRefused(f"{out} is not empty: a fetch never overwrites")
    network = bool(getattr(transport, "network", False))                       # recorded: an archive fetched over the network is HISTORICAL
    (out / "raw").mkdir(parents=True, exist_ok=True)
    responses: List[dict] = []
    last = {"SEC": 0.0, "BARS": 0.0}

    def keep(name: str, req: dict, page: int, params: dict, body: bytes) -> None:
        fname = f"{name}_p{page:03d}.json.gz"
        (out / "raw" / fname).write_bytes(gzip.compress(body, mtime=0))
        responses.append({"name": name, "page": page, "file": fname, "url": req["url"], "params": params, "sha256": hashlib.sha256(body).hexdigest(),
                          "bytes": len(body), "fetched_at": now()})

    for req in plan["requests"]:
        if req["url"] not in ALLOWED_URLS:
            raise FetchRefused(f"{req['name']}: not an allowed URL")
        is_sec = req["kind"] == "SEC"
        headers = {"User-Agent": sec_user_agent, "Accept-Encoding": "identity"} if is_sec else \
            {"APCA-API-KEY-ID": alpaca_key, "APCA-API-SECRET-KEY": alpaca_secret, "Accept": "application/json"}
        token, page = None, 0
        while True:
            wait = (SEC_MIN_INTERVAL_S if is_sec else MIN_INTERVAL_S) - (time.monotonic() - last[req["kind"]])
            if wait > 0:
                sleep(wait)
            params = dict(req["params"], **({"page_token": token} if token else {}))
            try:
                status, body = transport(req["url"], params, headers)
            except Exception as exc:                                             # noqa: BLE001  stops the fetch like a non-200 (no retry)
                _write_manifest(out, plan, plan_sha256, approval, responses, complete=False, network=network,
                                stopped=f"{req['name']} page {page}: {type(exc).__name__}")
                raise FetchRefused(f"{req['name']} page {page}: {type(exc).__name__} - stopped (no retry)") from None
            last[req["kind"]] = time.monotonic()
            if status != 200:
                _write_manifest(out, plan, plan_sha256, approval, responses, complete=False, network=network, stopped=f"{req['name']} page {page}: HTTP {status}")
                raise FetchRefused(f"{req['name']} page {page}: HTTP {status} - stopped (no retry)")
            keep(req["name"], req, page, params, body)
            page += 1
            token = None if is_sec else (json.loads(body).get("next_page_token") or None)
            if not token:
                break
            if page >= MAX_PAGES_PER_REQUEST:
                _write_manifest(out, plan, plan_sha256, approval, responses, complete=False, network=network,
                                stopped=f"{req['name']}: more than {MAX_PAGES_PER_REQUEST} pages")
                raise FetchRefused(f"{req['name']}: more than {MAX_PAGES_PER_REQUEST} pages - a materially broader request; stopped")
    return _write_manifest(out, plan, plan_sha256, approval, responses, complete=True, network=network, stopped=None)


def _write_manifest(out: Path, plan: dict, plan_sha256: str, approval: str, responses: List[dict], *, complete: bool, network: bool,
                    stopped: Optional[str]) -> Path:
    path = Path(out) / "manifest.json"
    path.write_text(json.dumps({"builder": BUILDER_VERSION, "plan": plan, "plan_sha256": plan_sha256, "approval": approval, "complete": complete,
                                "network": network, "stopped": stopped, "retrieved_at": max((r["fetched_at"] for r in responses), default=None), "responses": responses},
                               indent=1, sort_keys=True), encoding="utf-8")
    return path


# ---------------------------------------------------------------- the build (offline) ---------------------------------------------------------------------
def _pages(out: Path, man: dict, name: str) -> List[dict]:
    rows = sorted((r for r in man["responses"] if r["name"] == name), key=lambda r: r["page"])
    if not rows:
        raise BuildError(f"{name}: no archived response")
    pages = []
    for r in rows:
        body = gzip.decompress((Path(out) / "raw" / r["file"]).read_bytes())
        if hashlib.sha256(body).hexdigest() != r["sha256"]:
            raise BuildError(f"{r['file']}: sha256 differs from the manifest")
        pages.append(json.loads(body))
    return pages


def _bars(out: Path, man: dict, adj: str) -> Dict[str, List[dict]]:
    got: Dict[str, Dict[str, dict]] = {}
    for req in man["plan"]["requests"]:
        if req["kind"] != "BARS" or req["adjustment"] != adj:
            continue
        for page in _pages(out, man, req["name"]):
            for sym, rows in (page.get("bars") or {}).items():
                if sym not in req["symbols"]:
                    raise BuildError(f"{req['name']}: unrequested symbol {sym}")
                for b in rows:
                    t = str(b.get("t") or "")
                    if t[10:] not in DAILY_BAR_STAMPS:
                        raise BuildError(f"{sym} {t}: not a daily-bar stamp (midnight New York)")
                    d = t[:10]
                    if d in got.setdefault(sym, {}):
                        raise BuildError(f"{sym} {d}: duplicate {adj} bar")
                    got[sym][d] = {"date": d, "open": float(b["o"]), "high": float(b["h"]), "low": float(b["l"]), "close": float(b["c"]),
                                   "volume": float(b.get("v") or 0)}
    return {s: [m[d] for d in sorted(m)] for s, m in got.items()}


def build(out: Path, kind: str = "HISTORICAL") -> Path:
    """Offline: the dataset from the archived responses only (every sha256 verified). kind HISTORICAL for real responses; SYNTHETIC in tests."""
    if kind not in ("HISTORICAL", "SYNTHETIC"):
        raise BuildError("kind must be HISTORICAL or SYNTHETIC")
    out = Path(out)
    man = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    if not man.get("complete"):
        raise BuildError(f"the fetch is incomplete ({man.get('stopped')}): no dataset is built from it")
    if man.get("plan_sha256") != sha256_of(man["plan"]):
        raise BuildError("the archived plan does not match its sha256")
    if kind == "SYNTHETIC" and man.get("network"):
        raise BuildError("an archive fetched over the network is HISTORICAL, never SYNTHETIC")
    path = out / BUNDLE_NAME
    if path.exists():
        raise BuildError(f"{path} exists: a dataset is never overwritten")
    tickers = _pages(out, man, "sec_company_tickers")[0]
    cik_of = {str(v["ticker"]).upper(): (int(v["cik_str"]), str(v.get("title") or "")) for v in tickers.values()}
    universe = list(man["plan"]["universe"])                                    # the plan's symbols (default: the research universe + SPY)
    issuers: Dict[str, dict] = {}
    missing = []
    for sym in (s for s in universe if s != BENCHMARK):
        hit = cik_of.get(sec_symbol(sym))
        if hit is None:
            missing.append(sym)
            continue
        cik10 = f"{hit[0]:010d}"
        issuers.setdefault(cik10, {"name": hit[1], "symbols": []})["symbols"].append(sym)
    raw = {adj: _bars(out, man, adj) for adj in ADJUSTMENTS}
    bars, removed = {}, []
    for sym, rows in raw["all"].items():
        keep = []
        for r in rows:
            o, h, lo, c = r["open"], r["high"], r["low"], r["close"]
            if not all(math.isfinite(x) for x in (o, h, lo, c)) or min(o, h, lo, c) <= 0 or lo > min(o, c) + 1e-9 or h < max(o, c) - 1e-9:
                removed.append({"symbol": sym, "date": r["date"], "ohlc": [o, h, lo, c]})
            else:
                keep.append(r)
        bars[sym] = keep
    series = {"raw_close": {}, "raw_volume": {}, "split_ratio": {}}
    for sym in universe:
        sp = {b["date"]: b["close"] for b in raw["split"].get(sym, [])}
        series["raw_close"][sym] = {b["date"]: b["close"] for b in raw["raw"].get(sym, [])}
        series["raw_volume"][sym] = {b["date"]: b["volume"] for b in raw["raw"].get(sym, [])}
        series["split_ratio"][sym] = {d: round(c / sp[d], 6) for d, c in series["raw_close"][sym].items() if sp.get(d)}
    provenance = {
        "source": "Alpaca market data, SIP feed (all US exchanges), daily bars (raw, split-adjusted, all-adjusted); SEC company_tickers.json (issuer CIK)",
        "volume_feed": FEED, "retrieved_at": man["retrieved_at"], "bar_frequency": "1Day",
        "adjustment": "bars all-adjusted (splits + dividends: total return); the raw series (raw_close, raw_volume) unadjusted; split_ratio = raw / "
                      "split-adjusted close",
        "missing_data_policy": "a missing session is a gap, never filled; a bar with a non-positive or inconsistent OHLC field is removed (a gap, listed "
                               "in invalid_bars_removed); a symbol without a SEC CIK has no issuer (listed)",
        "corporate_actions": "splits / reverse splits visible in the raw / split-adjusted close ratio; dividends only in the all-adjusted bars; symbol "
                             "changes: today's symbols (the provider's as-of mapping), not recorded",
        "universe": ("fixed research universe: the 81 stocks of value_quality.RESEARCH_UNIVERSE (today's large caps) + SPY" if universe == symbols() else
                     f"a subset of the research universe: {len(universe)} symbols") + "; point-in-time constituents and delisted securities are NOT "
                    "available: SURVIVORSHIP_BIAS_PRESENT",
        "point_in_time": "prices only: every bar is a completed session; the issuer map is today's SEC ticker map (not point in time)",
        "builder": BUILDER_VERSION, "start": man["plan"]["start"], "end": man["plan"]["end"], "window": man["plan"]["window"],
        "invalid_bars_removed": removed, "sec_symbols_without_cik": missing, "plan_sha256": man["plan_sha256"], "fetch_approval": man.get("approval"),
        "raw_manifest_sha256": hashlib.sha256((out / "manifest.json").read_bytes()).hexdigest(),
    }
    path.write_text(json.dumps({"kind": kind, "provenance": provenance, "bars": bars, "aux": {"series": series, "static": {"issuers": issuers}}},
                               sort_keys=True), encoding="utf-8")
    return path


# ---------------------------------------------------------------- CLI -------------------------------------------------------------------------------------
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m research_data.sip_bundle", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan")
    p.add_argument("--out", required=True)
    p.add_argument("--start", default=DEFAULT_START)
    p.add_argument("--end", default=None)
    f = sub.add_parser("fetch")
    f.add_argument("--plan", required=True)
    f.add_argument("--plan-sha256", required=True)
    f.add_argument("--approval", required=True)
    f.add_argument("--out", required=True)
    f.add_argument("--env-file", default=None)
    b = sub.add_parser("build")
    b.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    if a.cmd == "plan":
        plan = request_plan(a.start, a.end)
        problem = output_problem(a.out)
        if problem or Path(a.out).exists():
            print(f"REFUSED: {problem or f'{a.out} exists (a plan is never overwritten)'}")
            return 2
        Path(a.out).write_text(json.dumps(plan, indent=1, sort_keys=True), encoding="utf-8")
        print(f"plan: {len(plan['requests'])} requests (+ pages); sha256 {sha256_of(plan)}")
        return 0
    if a.cmd == "fetch":
        plan = json.loads(Path(a.plan).read_text(encoding="utf-8"))
        key, secret, ua = _credentials(a.env_file)
        try:
            path = fetch(plan, out=Path(a.out), approval=a.approval, plan_sha256=a.plan_sha256, transport=http_transport(), alpaca_key=key,
                         alpaca_secret=secret, sec_user_agent=ua)
        except FetchRefused as exc:
            print(f"REFUSED: {exc}")
            return 2
        print(f"fetched: manifest {path}")
        return 0
    print(f"built: {build(Path(a.out))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
