"""tests/i1_builders.py - the reviewed synthetic-fixture builders of the I-1B-1 allow-list (matrix 4.12; Part A 6.4, 6.5).

PINNED_BUILDERS  (builder, expected_fail_step | None): every fixture an admission must accept (or refuse exactly at its declared step), each a
                 named zero-argument function applying its whole derivation chain, exactly as the test that uses it builds it
ATTACK_BUILDERS  the fixtures that must NEVER be pinned (generated random walks, relabelled MS-1-style bars, copied manifests, knob and cut
                 variants nobody pinned, over-cap and non-synthetic-prefix sets)
generate_table() the GENERATED fields of every pinned row, computed by calling the builders and admitting each fixture up to (and past)
                 the allow-list lookup; tests/test_i1_synthetic_boundary.py::test_S09 compares them with research_gate/synthetic_allowlist.py.

Every value is a formula or a constant in code: nothing is read from a market-data source. Real tickers and SEC identifiers appear only as
labels and keys, never with observed values. The hand-written sip_daily_v1 writer uses invented symbols only and writes only under its own
fresh, empty root outside the repository, inside the read guard's write mode, called outside any outer guard. No function-level import
here: every module a builder calls is imported above (no first import can happen inside a read guard)."""
from __future__ import annotations

import collections
import gzip
import hashlib
import json
import sys
import tempfile
from pathlib import Path

import historical_data_fixtures as HF
import md_eval_fixtures as FX
import research_gate as RG
from historical_data import calendar as CAL
from historical_data import canonical as CN
from historical_data import schema as HSC
from historical_data.adapters import sip_daily_v1 as SIP
from historical_data.adapters import synthetic as SY
from historical_data.provider import _TableProvider
from historical_data.status import unavailable as hd_unavailable
from historical_data.status import unknown as hd_unknown
from md_eval.contract import unknown as mc_unknown
from md_eval.double import ContractDouble
RGA = sys.modules["research_gate.admit"]                                        # the module: the package attribute `admit` is the admit() function
from research_gate import snapshot as RGS
from research_gate import synthetic_allowlist as SAL

SourceBundle = collections.namedtuple("SourceBundle", "adapter_id root")
PREFIX = "tests/i1_builders.py::"


# ============================================================================================================ MD-B fixtures (md_eval_fixtures)
def _v1_sessions():
    return FX.v1_double().sessions("0", "9")


def pb_v1_double():
    return FX.v1_double()


def _v1_perturbed(t):
    d = FX.v1_double()
    s = d.sessions("0", "9")
    return d.perturb_after(d.session_times(s[t])[1])


def pb_v1_perturb_205():
    return _v1_perturbed(205)


def pb_v1_perturb_218():
    return _v1_perturbed(218)


def pb_v1_perturb_230():
    return _v1_perturbed(230)


def _v1_truncated(t):
    d = FX.v1_double()
    return d.truncate(d.sessions("0", "9")[t])


def pb_v1_truncate_200():
    return _v1_truncated(200)


def pb_v1_truncate_210():
    return _v1_truncated(210)


def pb_v1_truncate_240():
    return _v1_truncated(240)


def pb_v1_scale_aapl_2():
    return FX.v1_double().scale_factors(FX.AAPL, 2.0)


def pb_v1_scale_aapl_0_5():
    return FX.v1_double().scale_factors(FX.AAPL, 0.5)


def pb_v1_scale_aapl_0_37():
    return FX.v1_double().scale_factors(FX.AAPL, 0.37)


def pb_v1_scale_aapl_1e6p1():
    return FX.v1_double().scale_factors(FX.AAPL, 1e6 + 1)


def pb_v1_views_raw_total_return():
    return FX.v1_double(views=("RAW", "TOTAL_RETURN"))


def pb_v1_unknown_history_msft_spy():
    return FX.v1_double(unknown_history=(FX.MSFT, FX.SPY))


def _flat(n, price=100.0):
    return FX.simple_double({"SIM:X": FX.flat(n, price)}, n=n)


def pb_flat4():
    return _flat(4)


def pb_flat5():
    return _flat(5)


def pb_flat6():
    return _flat(6)


def pb_flat10():
    return _flat(10)


def pb_flat5_view_holds_provider():
    return _flat(5).with_defect(view_holds_provider=True)


def _event_fixture(known):                                                   # tests/test_md_eval_lookahead.py::_event_fixture
    n = 30
    s = FX.simple_double({"SIM:X": FX.flat(n)}, n=n).sessions("0", "9")
    known_at = mc_unknown("NOT_PROVIDED") if known is None else s[known] + "T21:00:00Z"
    e = FX.ev("E-DIV", "SIM:X", "CASH_DIVIDEND", s, 20, cash_amount=1.0, pay_session=s[22], known_at=known_at)
    return FX.simple_double({"SIM:X": FX.flat(n)}, n=n, events=[e])


def pb_la_event_known10():
    return _event_fixture(10)


def pb_la_event_known10_leak():
    return _event_fixture(10).with_defect(leak_known_at=True)


def pb_la_event_unknown():
    return _event_fixture(None)


def pb_la_d02_x_missing6():
    return FX.simple_double({"SIM:X": FX.flat(12, missing=(6,)), "SIM:Y": FX.flat(12)}, n=12)


def pb_la_d02_y_missing6():
    return FX.simple_double({"SIM:X": FX.flat(12), "SIM:Y": FX.flat(12, missing=(6,))}, n=12)


def _sessions(n):
    return FX.simple_double({"SIM:X": FX.flat(n)}, n=n).sessions("0", "9")


def pb_lg_split_fixture():                                                    # tests/test_md_eval_ledger.py::_split_fixture
    n = 10
    prices = {k: [(a, a) if j < 5 else (b, b) for j in range(n)] for k, a, b in
              (("SIM:X", 100.0, 50.0), ("SIM:Y", 90.0, 60.0), ("SIM:Z", 90.0, 60.0), ("SIM:W", 100.0, 50.0), ("SIM:V", 100.0, 50.0))}
    s = _sessions(n)
    events = [FX.ev("S-X", "SIM:X", "SPLIT", s, 5, ratio=2.0), FX.ev("S-W", "SIM:W", "SPLIT", s, 5, ratio=2.0),
              FX.ev("S-V", "SIM:V", "SPLIT", s, 5, ratio=2.0), FX.ev("SD-Y", "SIM:Y", "STOCK_DIVIDEND", s, 5, ratio=1.5, cash_in_lieu_per_share=60.0),
              FX.ev("SD-Z", "SIM:Z", "STOCK_DIVIDEND", s, 5, ratio=1.5)]
    return FX.simple_double(prices, n=n, events=events)


def pb_lg_l03_split():
    n = 10
    return FX.simple_double({"SIM:X": [(100.0, 100.0) if k < 5 else (50.0, 50.0) for k in range(n)]}, n=n,
                            events=[FX.ev("S-X", "SIM:X", "SPLIT", _sessions(n), 5, ratio=2.0)])


def _div_fixture(pay):                                                        # tests/test_md_eval_ledger.py::_div_fixture
    n = 12
    s = FX.simple_double({"SIM:A": FX.flat(n)}, n=n).sessions("0", "9")
    px = [(100.0, 100.0) if k < 6 else (98.0, 98.0) for k in range(n)]
    events = [FX.ev(f"D-{x}", f"SIM:{x}", "CASH_DIVIDEND", s, 6, known_k=2, cash_amount=2.0, pay_session=s[9]) if pay else
              FX.ev(f"D-{x}", f"SIM:{x}", "CASH_DIVIDEND", s, 6, known_k=2, cash_amount=2.0) for x in "ABC"]
    return FX.simple_double({"SIM:A": px, "SIM:B": list(px), "SIM:C": list(px)}, n=n, events=events)


def pb_lg_div_pay():
    return _div_fixture(True)


def pb_lg_div_no_pay():
    return _div_fixture(False)


def _spin_prices(n):
    return {"SIM:P": [(100.0, 100.0) if k < 5 else (80.0, 80.0) for k in range(n)], "SIM:K": [None] * 5 + [(40.0, 40.0)] * 5}


def pb_lg_l06_spin_off():
    n = 10
    return FX.simple_double(_spin_prices(n), n=n, events=[FX.ev("SO", "SIM:P", "SPIN_OFF", _sessions(n), 5, child_instrument_id="SIM:K", child_ratio=0.5)])


def pb_lg_l06_no_child():
    n = 10
    return FX.simple_double({"SIM:P": _spin_prices(n)["SIM:P"], "SIM:K": [None] * n}, n=n,
                            events=[FX.ev("SO", "SIM:P", "SPIN_OFF", _sessions(n), 5, child_instrument_id="SIM:K", child_ratio=0.5)])


def _merger(acquirer_bars, n=10):                                             # tests/test_md_eval_ledger.py::_merger and test_L07
    return FX.simple_double({"SIM:T": [(100.0, 100.0)] * 5 + [None] * 5, "SIM:Q": acquirer_bars}, n=n,
                            events=[FX.ev("M", "SIM:T", "MERGER", _sessions(n), 5, acquirer_instrument_id="SIM:Q", stock_ratio=0.5, cash_amount=10.0)])


def pb_lg_merger():
    return _merger([(200.0, 200.0)] * 10)


def pb_lg_merger_acquirer_late():
    return _merger([None] * 6 + [(200.0, 200.0)] * 4)


def pb_lg_merger_acquirer_none():
    return _merger([None] * 10)


def _delisting(extra):
    n = 10
    s = _sessions(n)
    return FX.simple_double({"SIM:D": [(50.0, 50.0)] * 5 + [None] * 5, "SIM:E": FX.flat(n)}, n=n,
                            events=[FX.ev("DL", "SIM:D", "DELISTING", s, 7, last_trade_session=s[4], last_raw_price=50.0, **extra)])


def pb_lg_l08_delisting_return_unknown():
    return _delisting({})


def pb_lg_l08_delisting_return_m0_3():
    return _delisting({"delisting_return": -0.3})


def pb_lg_l09_events_incomplete():
    return FX.simple_double({"SIM:X": FX.flat(8)}, n=8, completeness="NONE")


def pb_lg_recon():                                                            # tests/test_md_eval_ledger.py::_recon_fixture
    closes = [100.0, 100.0, 110.0, 55.0, 60.0, 58.0, 58.0, 58.0]
    n = len(closes)
    prices = {"SIM:S": [(c, c) for c in closes]}
    factors = {"SIM:S": [(2.0, 2.0 * 60.0 / 58.0) if k < 3 else ((1.0, 60.0 / 58.0) if k < 5 else (1.0, 1.0)) for k in range(n)]}
    s = FX.simple_double({"SIM:S": FX.flat(n)}, n=n).sessions("0", "9")
    events = [FX.ev("SPL", "SIM:S", "SPLIT", s, 3, ratio=2.0), FX.ev("DIV", "SIM:S", "CASH_DIVIDEND", s, 5, cash_amount=2.0, pay_session=s[5])]
    return FX.simple_double(prices, n=n, events=events, factors=factors)


def pb_lg_l15_split_and_dividend():
    n = 10
    s = _sessions(n)
    events = [FX.ev("A-SPLIT", "SIM:X", "SPLIT", s, 5, ratio=2.0), FX.ev("B-DIV", "SIM:X", "CASH_DIVIDEND", s, 5, cash_amount=1.0, pay_session=s[5])]
    return FX.simple_double({"SIM:X": [(100.0, 100.0)] * 5 + [(49.5, 49.5)] * 5}, n=n, events=events)


def pb_lg_l18_given_allocation():
    n = 10
    parent = [(100.0, 100.0) if k < 5 else (80.0, 80.0) for k in range(n)]
    return FX.simple_double({"SIM:P": parent, "SIM:K": [None] * 5 + [(40.0, 40.0)] * 5}, n=n,
                            events=[FX.ev("SO", "SIM:P", "SPIN_OFF", _sessions(n), 5, child_instrument_id="SIM:K", child_ratio=0.5, cost_basis_allocation=0.3)])


def pb_lg_l18_late_child():
    n = 10
    parent = [(100.0, 100.0) if k < 5 else (80.0, 80.0) for k in range(n)]
    return FX.simple_double({"SIM:P": parent, "SIM:K": [None] * 6 + [(40.0, 40.0)] * 4}, n=n,
                            events=[FX.ev("SO", "SIM:P", "SPIN_OFF", _sessions(n), 5, child_instrument_id="SIM:K", child_ratio=0.5)])


def pb_lg_l19_delisted_at_open():
    n = 10
    s = _sessions(n)
    return FX.simple_double({"SIM:D": [(50.0, 50.0)] * 5 + [None] * 5}, n=n,
                            events=[FX.ev("DL", "SIM:D", "DELISTING", s, 5, last_trade_session=s[4], last_raw_price=50.0, delisting_return=0.0)])


def _l20(typ, ratio, post):
    n = 8
    return FX.simple_double({"SIM:X": [(100.0, 100.0)] * 3 + [(post, post)] * 5}, n=n, events=[FX.ev("S", "SIM:X", typ, _sessions(n), 3, ratio=ratio)])


def pb_lg_l20_split():
    return _l20("SPLIT", 2.0, 50.0)


def pb_lg_l20_reverse_split():
    return _l20("REVERSE_SPLIT", 0.1, 1000.0)


def pb_lg_l21_ratio_unknown():
    n = 8
    return FX.simple_double({"SIM:X": [(100.0, 100.0)] * 3 + [(50.0, 50.0)] * 5}, n=n, events=[FX.ev("S", "SIM:X", "SPLIT", _sessions(n), 3)])


def pb_lg_l12_stale_mark():
    n = 10
    return FX.simple_double({"SIM:G": [(100.0, 100.0)] * 6 + [None] * 4, "SIM:H": FX.flat(n)}, n=n)


BOOK_SECTORS = {"SIM:A": "TECH", "SIM:B": "TECH", "SIM:C": "TECH", "SIM:D": "HEALTH", "SIM:E": "HEALTH", "SIM:F": "ENERGY"}   # test_md_eval_book_costs.SECTORS


def pb_bc_book12():                                                           # tests/test_md_eval_book_costs.py::_book_double()
    return FX.simple_double({k: FX.flat(12) for k in BOOK_SECTORS}, n=12, sectors=BOOK_SECTORS)


def pb_bc_book6():
    return FX.simple_double({k: FX.flat(6) for k in BOOK_SECTORS}, n=6, sectors=BOOK_SECTORS)


def pb_bc_k03():
    n = 12
    return FX.simple_double({"SIM:A": [(100.0, 100.0)] * 3 + [(200.0, 200.0)] * (n - 3), "SIM:B": FX.flat(n), "SIM:D": FX.flat(n)}, n=n, sectors=BOOK_SECTORS)


def pb_bc_k08_a_missing5():
    return FX.simple_double({k: FX.flat(12, missing=(5,) if k == "SIM:A" else ()) for k in BOOK_SECTORS}, n=12, sectors=BOOK_SECTORS)


def pb_bc_k08_a_missing4():
    return FX.simple_double({k: FX.flat(12, missing=(4,) if k == "SIM:A" else ()) for k in BOOK_SECTORS}, n=12, sectors=BOOK_SECTORS)


def pb_bc_k09_no_sector():
    return FX.simple_double({"SIM:X": FX.flat(6)}, n=6, sectors={"SIM:X": None})


def pb_bs_b04_equal_weight():
    return FX.simple_double({"SIM:A": FX.flat(8), "SIM:B": FX.flat(8, 50.0)}, n=8)


def _world_double(*, rising=True, views=("RAW", "SPLIT_ADJUSTED", "TOTAL_RETURN"), spy_rising=False):
    """tests/test_md_eval_registry_verdict.py::_world's double, reproduced bit for bit (its pow is reused unchanged: Part A 6.7, a P5b exposure)."""
    n = 60
    closes = [100.0 * (1.01 ** k if rising else 1.0) for k in range(n)]
    spy = [100.0 * (1.005 ** k if spy_rising else 1.0) for k in range(n)]
    prices = {"SIM:X": [((closes[k - 1] if k else closes[k]), closes[k]) for k in range(n)],
              "SIM:SPY": [((spy[k - 1] if k else spy[k]), spy[k]) for k in range(n)]}
    return FX.simple_double(prices, n=n, basis="POINT_IN_TIME", views=views)


def pb_rv_world():
    return _world_double()


def pb_rv_world_flat():
    return _world_double(rising=False)


def pb_rv_world_spy_rising():
    return _world_double(spy_rising=True)


def pb_rv_world_no_total_return():
    return _world_double(views=("RAW", "SPLIT_ADJUSTED"))


# ============================================================================================================ MD-A fixtures
TWIN_N = 260
TWIN_SPLIT = 150


def twin_sessions() -> list:
    return [s for s in CAL.SESSIONS if s >= "2024-01-02"][:TWIN_N]


def _twin_bars(iid, sessions, adj, split_index=None, volume=1_000_000.0):
    out = []
    for k, s in enumerate(sessions):
        sf = 2.0 if (split_index is not None and k < split_index) else 1.0
        o, c = (adj[k - 1] if k else adj[k]) * sf, adj[k] * sf
        out.append(SY.bar(iid, s, o, max(o, c) * 1.005, min(o, c) * 0.995, c, volume, split_factor=sf, total_return_factor=sf))
    return out


def pb_mda_v1_twin():
    """V1's double rebuilt as an MD-A SyntheticProvider on NYSE sessions (the same _cycle_closes prices, a 2:1 AAPL split, issuer ids)."""
    s = twin_sessions()
    aapl = SY.instrument("AAPL", s[0], status="KNOWN", sector="TECH", issuer_id="SEC:0000320193")
    msft = SY.instrument("MSFT", s[0], status="KNOWN", sector="TECH", issuer_id="SEC:0000789019")
    spy = SY.instrument("SPY", s[0], status="KNOWN", asset_type="ETF")
    bars = (_twin_bars(aapl.instrument_id, s, FX._cycle_closes(TWIN_N, 0, 100.0, 0.3), split_index=TWIN_SPLIT)
            + _twin_bars(msft.instrument_id, s, FX._cycle_closes(TWIN_N, 5, 80.0, 1.1))
            + _twin_bars(spy.instrument_id, s, FX._cycle_closes(TWIN_N, 3, 300.0, 2.2), volume=50_000_000.0))
    split = HSC.CorporateEvent(event_id="E-AAPL-SPLIT", instrument_id=aapl.instrument_id, type="SPLIT", effective_session=s[TWIN_SPLIT],
                               known_at=CAL.close_utc(s[TWIN_SPLIT - 10]), ratio=2.0)
    return SY.SyntheticProvider(instruments=[aapl, msft, spy], bars=bars, events=[split], events_completeness="COMPLETE", dataset_id="synthetic:v1-twin")


def pb_mda_lookahead_world():
    return HF.build(HF.spec())


def pb_mda_lookahead_world_perturbed_s20():
    sp = HF.spec()
    return HF.build(HF.perturb(sp, CAL.close_utc(HF.S[20])), dataset_id="synthetic:lookahead-perturbed")


def pb_mda_not_permitted():
    inst, bars = HF.series("AAA", HF.S[:10], [100.0 + k for k in range(10)])
    return HF.provider((inst, bars), licence=HSC.Licence(licence_ref=hd_unknown("NO_LICENCE_FILE"), personal_research_use="NOT_PERMITTED"),
                       dataset_id="synthetic:not-permitted")


def pb_mda_bridge_guards():
    """The bridge's guard dataset: AAA with sector, issuer and asset type Missing and a NO_BAR gap; BBB with one raw_open Missing (RAW_OHL
    declared); a cash dividend on AAA with an unknown pay session."""
    s = HF.S[:12]
    aaa = SY.instrument("AAA", s[0], asset_type=hd_unknown("NOT_IN_SOURCE"))
    bbb = SY.instrument("BBB", s[0], sector="TECH", issuer_id="SEC:0000000001")
    rows = []
    for k, d in enumerate(s):
        if k != 4:
            c = 50.0 + k
            rows.append(SY.bar(aaa.instrument_id, d, c, c + 1.0, c - 1.0, c, 1_000_000.0))
        c = 80.0 + k
        o = hd_unavailable("RAW_OPEN_NOT_IN_SOURCE") if k == 6 else c
        rows.append(SY.bar(bbb.instrument_id, d, o, c + 1.0, c - 1.0, c, 1_000_000.0))
    div = HSC.CorporateEvent(event_id="E-DIV-AAA", instrument_id=aaa.instrument_id, type="CASH_DIVIDEND", effective_session=s[8],
                             known_at=CAL.close_utc(s[2]), cash_amount=0.5)
    return SY.SyntheticProvider(instruments=[aaa, bbb], bars=rows, events=[div], dataset_id="synthetic:bridge-guards")


# ============================================================================================================ the hand-written sip_daily_v1 writer
SIP_SESSIONS_N = 60


def sip_sessions() -> list:
    return [s for s in CAL.SESSIONS if s >= "2025-01-02"][:SIP_SESSIONS_N]


def _canon(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def sip_bundle_files(*, network=False) -> dict:
    """{relative path: bytes} of a complete, offline sip_daily_v1 bundle over the invented symbols: formula prices, a fixed retrieved_at,
    gzip mtime 0, fixed JSON. Nothing is read; the caller writes the files."""
    syms, s = SAL.INVENTED_SYMBOLS, sip_sessions()
    raw_close, raw_volume, split_ratio, adjusted, raw_rows = {}, {}, {}, {}, {}
    for j, sym in enumerate(syms):
        prev = None
        raw_close[sym], raw_volume[sym], split_ratio[sym], adjusted[sym], raw_rows[sym] = {}, {}, {}, [], []
        for k, d in enumerate(s):
            c = 40.0 + 7.0 * j + 0.5 * k
            o = prev if prev is not None else c
            prev = c
            v = 1_000_000.0 + 1000.0 * k
            raw_close[sym][d], raw_volume[sym][d], split_ratio[sym][d] = c, v, 1.0
            adjusted[sym].append({"close": c, "date": d})
            raw_rows[sym].append({"c": c, "h": max(o, c) + 0.5, "l": min(o, c) - 0.5, "o": o, "t": d + "T05:00:00Z", "v": v})
    body = _canon({"bars": raw_rows})
    plan = {"requests": [{"adjustment": "raw", "kind": "BARS", "name": "bars_raw_01"}], "universe": list(syms)}
    man = {"builder": "sip_daily_v1", "complete": True, "network": network, "plan": plan, "plan_sha256": _sha(_canon(plan)),
           "responses": [{"file": "bars_raw_01_p001.json.gz", "name": "bars_raw_01", "page": 1, "sha256": _sha(body)}],
           "retrieved_at": SAL.CAPS["MDA_SIP_FIXTURE_BUNDLE"]["fixed_retrieved_at"]}
    man_bytes = _canon(man)
    bundle = {"aux": {"series": {"raw_close": raw_close, "raw_volume": raw_volume, "split_ratio": split_ratio}}, "bars": adjusted, "kind": "SYNTHETIC",
              "provenance": {"builder": "sip_daily_v1", "raw_manifest_sha256": _sha(man_bytes), "volume_feed": "SIP"}}
    return {"manifest.json": man_bytes, "sip_bundle.json": _canon(bundle), "raw/bars_raw_01_p001.json.gz": gzip.compress(body, compresslevel=9, mtime=0)}


def write_sip_bundle(files: dict) -> Path:
    """A fresh empty root outside the repository (created before the guard), written inside the guard's write mode; never inside an outer guard."""
    root = Path(tempfile.mkdtemp(prefix="i1_sip_"))
    with RGA.read_guard(root=root, allow_write=True):
        (root / "raw").mkdir()
        for rel, data in files.items():
            (root / rel).write_bytes(data)
    return root


def pb_sip_invented_bundle():
    return SourceBundle(SIP.ADAPTER_VERSION, write_sip_bundle(sip_bundle_files()))


# ============================================================================================================ attack builders (never pinned)
def lcg_walk(n, seed=20261118, start=100.0) -> list:
    """A generated random walk with realistic magnitudes (a linear congruential generator; no random module, no clock)."""
    x, p, out = seed, start, []
    for _ in range(n):
        x = (1103515245 * x + 12345) % 2147483648
        p = p * (1.0 + ((x % 2001) - 1000) / 50000.0)
        out.append(round(p, 2))
    return out


def atk_s01_random_walk_double():
    closes = lcg_walk(40)
    return FX.simple_double({"SIM:RW": [((closes[k - 1] if k else closes[k]), closes[k]) for k in range(40)]}, n=40)


def atk_s01_random_walk_provider():
    closes = lcg_walk(40)
    s = HF.S[:40]
    inst = SY.instrument("RW", s[0])
    rows = [dict(instrument_id=inst.instrument_id, session=d, o=(closes[k - 1] if k else closes[k]), h=max(closes[k - 1] if k else closes[k], closes[k]) * 1.01,
                 l=min(closes[k - 1] if k else closes[k], closes[k]) * 0.99, c=closes[k], v=1_000_000.0) for k, d in enumerate(s)]
    return SY.SyntheticProvider(instruments=[inst], rows=rows, dataset_id="synthetic:random-walk")


def atk_s02_ms1_style_relabelled():
    """Generated bars in the MS-1 shape (ALPACA ids, the IEX feed, integrity-style values), relabelled SYNTHETIC. Never read from any archive."""
    s = HF.S[:30]
    inst = SY.instrument("ZZMS", s[0], prefix="ALPACA")
    bars = [SY.bar(inst.instrument_id, d, 20.0 + 0.1 * k, 20.5 + 0.1 * k, 19.5 + 0.1 * k, 20.2 + 0.1 * k, 10_000.0, volume_feed="SINGLE_VENUE:IEX",
                   price_source="SINGLE_VENUE:IEX") for k, d in enumerate(s)]
    return SY.SyntheticProvider(instruments=[inst], bars=bars, volume_feed="SINGLE_VENUE:IEX", price_source="SINGLE_VENUE:IEX", dataset_id="synthetic:ms1-style")


def atk_s02_sip_network_bundle():
    return SourceBundle(SIP.ADAPTER_VERSION, write_sip_bundle(sip_bundle_files(network=True)))


def atk_s03_v1_manifest_on_other_prices():
    return FX.v1_double().perturb_after("2000-01-01T00:00:00Z")             # the pinned manifest and dataset_id, every price changed


def atk_s05_v1_leak_known_at():
    return FX.v1_double().with_defect(leak_known_at=True)


def atk_s06_v1_truncate_220():
    return _v1_truncated(220)


def atk_s07_over_cap():
    return FX.simple_double({f"SIM:I{j:02d}": FX.flat(193) for j in range(26)}, n=193)


def atk_s07_real_prefix():
    return FX.simple_double({"REAL:X": FX.flat(5)}, n=5)


PINNED_BUILDERS = (
    (pb_v1_double, None), (pb_v1_perturb_205, None), (pb_v1_perturb_218, None), (pb_v1_perturb_230, None), (pb_v1_truncate_200, None),
    (pb_v1_truncate_210, None), (pb_v1_truncate_240, None), (pb_v1_scale_aapl_2, None), (pb_v1_scale_aapl_0_5, None), (pb_v1_scale_aapl_0_37, None),
    (pb_v1_scale_aapl_1e6p1, None), (pb_v1_views_raw_total_return, None), (pb_v1_unknown_history_msft_spy, None), (pb_flat4, None), (pb_flat5, None),
    (pb_flat6, None), (pb_flat10, None), (pb_flat5_view_holds_provider, None), (pb_la_event_known10, None), (pb_la_event_known10_leak, None),
    (pb_la_event_unknown, None), (pb_la_d02_x_missing6, None), (pb_la_d02_y_missing6, None), (pb_lg_split_fixture, None), (pb_lg_l03_split, None),
    (pb_lg_div_pay, None), (pb_lg_div_no_pay, None), (pb_lg_l06_spin_off, None), (pb_lg_l06_no_child, None), (pb_lg_merger, None),
    (pb_lg_merger_acquirer_late, None), (pb_lg_merger_acquirer_none, None), (pb_lg_l08_delisting_return_unknown, None),
    (pb_lg_l08_delisting_return_m0_3, None), (pb_lg_l09_events_incomplete, None), (pb_lg_recon, None), (pb_lg_l15_split_and_dividend, None),
    (pb_lg_l18_given_allocation, None), (pb_lg_l18_late_child, None), (pb_lg_l19_delisted_at_open, None), (pb_lg_l20_split, None),
    (pb_lg_l20_reverse_split, None), (pb_lg_l21_ratio_unknown, None), (pb_lg_l12_stale_mark, None), (pb_bc_book12, None), (pb_bc_book6, None),
    (pb_bc_k03, None), (pb_bc_k08_a_missing5, None), (pb_bc_k08_a_missing4, None), (pb_bc_k09_no_sector, None), (pb_bs_b04_equal_weight, None),
    (pb_rv_world, None), (pb_rv_world_flat, None), (pb_rv_world_spy_rising, None), (pb_rv_world_no_total_return, None),
    (pb_mda_v1_twin, None), (pb_mda_lookahead_world, None), (pb_mda_lookahead_world_perturbed_s20, None),
    (pb_mda_not_permitted, {"step": 8, "code": "NOT_EVALUATION"}), (pb_mda_bridge_guards, None), (pb_sip_invented_bundle, None),
)
ATTACK_BUILDERS = (atk_s01_random_walk_double, atk_s01_random_walk_provider, atk_s02_ms1_style_relabelled, atk_s02_sip_network_bundle,
                   atk_s03_v1_manifest_on_other_prices, atk_s05_v1_leak_known_at, atk_s06_v1_truncate_220, atk_s07_over_cap, atk_s07_real_prefix)


# ============================================================================================================ admission helpers and the generator
def qualified(builder) -> str:
    return PREFIX + builder.__name__


def admit_any(fixture):
    """The gate's entry point for the fixture's kind (a source bundle, a contract double, an MD-A provider)."""
    if isinstance(fixture, SourceBundle):
        return RG.admit_from_source(fixture.adapter_id, fixture.root)
    if type(fixture) is ContractDouble:
        return RG.admit_double(fixture)
    return RG.admit(fixture)


def describe(fixture):
    """(kind_of_object, (instrument_count, bar_count, session_span), side_channel_sha256) as the gate counts them."""
    if isinstance(fixture, SourceBundle):
        p = SIP.SipDailyV1Provider(fixture.root)
        kind = "MDA_SIP_FIXTURE_BUNDLE"
    elif type(fixture) is ContractDouble:
        d = fixture
        span = (d._sessions[0], d._sessions[-1]) if d._sessions else (None, None)
        return "MDB_CONTRACT_DOUBLE", (len(d._instruments), sum(len(v) for v in d._bars.values()), span), None
    else:
        p, kind = fixture, "MDA_SYNTHETIC_PROVIDER"
    m = _TableProvider.manifest(p)
    side = RGS.side_channel_sha256(RGS.side_channel_of(p))
    return kind, (len(p._instruments), len(p._bars), (HSC.jv(m.start_session), HSC.jv(m.end_session))), side


STEP_OF_CODE = {"NOT_A_REGISTERED_PROVIDER": 1, "SOURCE_UNREADABLE": 1, "CONTRACT_MISMATCH": 2, "CONTENT_HASH_STALE": 3, "SNAPSHOT_ROUNDTRIP_FAILED": 4,
                "BRIDGE_UNMAPPABLE": 4, "RECORD_INVALID": 5, "DATASET_BLOCKED": 5, "DOUBLE_HISTORICAL_NOT_ADMITTED": 3, "ALLOWLIST_GUARD_FAILED": 7,
                "LICENCE_EVIDENCE_UNDEFINED": 7, "NOT_EVALUATION": 8}


def generated_row(builder, entry_id="SAL-PROBE") -> dict:
    """The GENERATED fields of one pinned builder's row: its fingerprint from an admission against an empty table (SYNTHETIC_NOT_PINNED, a sha
    only), then the step and code an admission against a table holding just that row actually reaches (None: admitted)."""
    fx = builder()
    kind, counts, side = describe(fx)
    saved = SAL.ENTRIES
    try:
        SAL.ENTRIES = ()
        try:
            admit_any(fx)
            raise AssertionError(f"{builder.__name__}: admitted against an empty table")
        except RG.AdmissionRefused as exc:
            if exc.code != RG.SYNTHETIC_NOT_PINNED:
                raise AssertionError(f"{builder.__name__}: refused {exc.code} before the lookup (not pinnable)") from exc
            fp = exc.fingerprint
        row = {"allowlist_entry_id": entry_id, "fingerprint": fp, "kind_of_object": kind, "instrument_count": counts[0], "bar_count": counts[1],
               "session_span": tuple(counts[2]), "side_channel_sha256": side}
        SAL.ENTRIES = (row,)
        try:
            admit_any(fx)
            outcome = None
        except RG.AdmissionRefused as exc:
            outcome = {"step": STEP_OF_CODE.get(exc.code, 0), "code": exc.code}
    finally:
        SAL.ENTRIES = saved
    row = dict(row, expected_fail_step=outcome, origin={"builder": qualified(builder), "test_ids": ()})
    return row


def generate_table() -> list:
    return [generated_row(b) for b, _ in PINNED_BUILDERS]


def projection(entry) -> tuple:
    """The GENERATED fields of an entry, as one comparable row."""
    span = entry.get("session_span")
    return (entry["fingerprint"], entry["kind_of_object"], entry["instrument_count"], entry["bar_count"], tuple(span) if span is not None else None,
            entry["side_channel_sha256"], json.dumps(entry["expected_fail_step"], sort_keys=True), entry["origin"]["builder"])


def entries_literal(rows) -> str:
    """The rows as the ENTRIES literal of research_gate/synthetic_allowlist.py (the reviewed fields from REVIEWED_FIELDS)."""
    out = ["ENTRIES = ("]
    for r in rows:
        rf = SAL.REVIEWED_FIELDS.get(r["origin"]["builder"], {})
        out.append(f"    {{'allowlist_entry_id': {rf.get('allowlist_entry_id', 'SAL-XXXX')!r}, 'fingerprint': {r['fingerprint']!r}, "
                   f"'kind_of_object': {r['kind_of_object']!r},")
        out.append(f"     'origin': {{'builder': {r['origin']['builder']!r}, 'test_ids': {tuple(rf.get('test_ids', ()))!r}}},")
        out.append(f"     'instrument_count': {r['instrument_count']!r}, 'bar_count': {r['bar_count']!r}, 'session_span': {tuple(r['session_span'])!r}, "
                   f"'side_channel_sha256': {r['side_channel_sha256']!r}, 'expected_fail_step': {r['expected_fail_step']!r}}},")
    out.append(")")
    return "\n".join(out)


def table_sha256(entries) -> str:
    return CN.sha256_bytes(CN.dumps(sorted(json.dumps(projection(e)) for e in entries)))
