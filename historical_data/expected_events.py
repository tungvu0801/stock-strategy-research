"""
historical_data/expected_events.py - the coded expected-events table: REFERENCE DATA, not provider events (contract v2 A9).

It is never emitted by events(), never booked and never changes events_completeness. Its only effect is on WARN classification:
  * an entry with status VERIFIED whose symbol, session and type match a DAY_JUMP reclassifies that jump from WARN DAY_JUMP_UNCLASSIFIED to
    the acceptance record's classified_jumps (with the entry ID); INFERRED, UNVERIFIED, OBSERVED, RULE and UNRESOLVED never classify - a
    matching entry is listed only;
  * a VERIFIED spin-off is a "spin-off indicated" (A9) for SPIN_OFF_NOT_ADJUSTED;
  * the SNDK ticker-reuse rule may ANNOTATE a POSSIBLE_TICKER_REUSE warning, never upgrade or suppress it (A9).

Source: the operator's 78B v2 scratch list (stage-78b2b-remediation-PAUSED/scratchpad_copy/readiness78b2b/expected_events_78b.json,
version "78B2B-expected-events-v2"), entry by entry, plus the five open contradictions of
project-planning/wave3-research-phase0/evidence/AUDIT_B_DATA_AND_EVENTS.md (contradictions 3-6): HON 2026-06-29, BKNG 2026-04-06,
KLAC 2026-06-12, the PFE spin-off date and XOM's CIK change. Those are OWNER decisions: their entries are UNRESOLVED and resolve nothing.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Tuple

TABLE_VERSION = "md_a_expected_events_v1"
SOURCE = "78B2B-expected-events-v2 (operator scratch list, 2026-11-02) + AUDIT_B contradictions 3-6 (UNRESOLVED)"
STATUSES = ("VERIFIED", "INFERRED", "UNVERIFIED", "OBSERVED", "RULE", "UNRESOLVED")
CLASSIFYING_STATUS = "VERIFIED"
TYPES = ("SPLIT", "REVERSE_SPLIT", "SPIN_OFF", "EXCHANGE_OFFER", "SYMBOL_CHANGE", "LISTING_BOUNDARY", "TICKER_REUSE_RULE", "HOLDING_COMPANY_CHANGE")
JUMP_TYPES = ("SPLIT", "REVERSE_SPLIT", "SPIN_OFF")                     # the table types that move a raw close


@dataclass(frozen=True)
class ExpectedEvent:
    entry_id: str
    symbol: str
    type: str
    session: Optional[str]                    # the ex / first adjusted / effective session; None when only a month is known
    status: str
    ratio: Optional[float] = None
    note: str = ""

    def __post_init__(self):
        if self.status not in STATUSES or self.type not in TYPES:
            raise ValueError(f"{self.entry_id}: status {self.status} / type {self.type}")

    def to_json(self) -> dict:
        return {"entry_id": self.entry_id, "symbol": self.symbol, "type": self.type, "session": self.session, "status": self.status,
                "ratio": self.ratio, "note": self.note}


def _e(i, sym, typ, sess, status, ratio=None, note=""):
    return ExpectedEvent(f"EE-{i:03d}", sym, typ, sess, status, ratio, note)


TABLE: Tuple[ExpectedEvent, ...] = (
    # splits (first adjusted session = the ex session)
    _e(1, "AAPL", "SPLIT", "2020-08-31", "VERIFIED", 4.0),
    _e(2, "TSLA", "SPLIT", "2020-08-31", "VERIFIED", 5.0),
    _e(3, "NEE", "SPLIT", "2020-10-27", "VERIFIED", 4.0),
    _e(4, "NVDA", "SPLIT", "2021-07-20", "VERIFIED", 4.0),
    _e(5, "GE", "REVERSE_SPLIT", "2021-08-02", "VERIFIED", 0.125, "1-for-8 reverse split"),
    _e(6, "ISRG", "SPLIT", "2021-10-05", "UNVERIFIED", 3.0),
    _e(7, "AMZN", "SPLIT", "2022-06-06", "VERIFIED", 20.0),
    _e(8, "GOOG", "SPLIT", "2022-07-18", "VERIFIED", 20.0),
    _e(9, "GOOGL", "SPLIT", "2022-07-18", "VERIFIED", 20.0),
    _e(10, "TSLA", "SPLIT", "2022-08-25", "VERIFIED", 3.0),
    _e(11, "PANW", "SPLIT", "2022-09-14", "UNVERIFIED", 3.0),
    _e(12, "WMT", "SPLIT", "2024-02-26", "VERIFIED", 3.0, "distributed after the close of 2024-02-23"),
    _e(13, "NVDA", "SPLIT", "2024-06-10", "VERIFIED", 10.0, "distributed after the close of 2024-06-07"),
    _e(14, "AVGO", "SPLIT", "2024-07-15", "VERIFIED", 10.0, "distributed after the close of 2024-07-12"),
    _e(15, "LRCX", "SPLIT", "2024-10-03", "VERIFIED", 10.0),
    _e(16, "PANW", "SPLIT", "2024-12-16", "VERIFIED", 2.0),
    _e(17, "NFLX", "SPLIT", "2025-11-17", "VERIFIED", 10.0),
    _e(18, "NOW", "SPLIT", "2025-12-18", "VERIFIED", 5.0, "effective 2025-12-17 16:05 ET"),
    _e(19, "HON", "REVERSE_SPLIT", "2026-06-29", "UNRESOLVED", 0.5,
       "OWNER DECISION (contradiction 3): VERIFIED 1-for-2 reverse split + spin-off in the 78B list, a 'provider artifact' in DESIGN_65 / 7.8C"),
    # spin-offs (ex session)
    _e(20, "DHR", "SPIN_OFF", "2016-07-05", "VERIFIED", None, "Fortive (FTV), 1 FTV per 2 DHR"),
    _e(21, "HON", "SPIN_OFF", "2016-10-03", "VERIFIED", None, "AdvanSix (ASIX), 1 per 25 HON; distributed Saturday 2016-10-01"),
    _e(22, "HON", "SPIN_OFF", "2018-10-01", "UNVERIFIED", None, "Garrett Motion (GTX)"),
    _e(23, "HON", "SPIN_OFF", "2018-10-29", "UNVERIFIED", None, "Resideo (REZI)"),
    _e(24, "RTX", "SPIN_OFF", "2020-04-03", "VERIFIED", None, "Carrier (CARR) and Otis (OTIS); UTX renamed RTX and merged with Raytheon Co."),
    _e(25, "PFE", "SPIN_OFF", "2020-11-17", "UNRESOLVED", None,
       "OWNER DECISION (contradiction 5): Upjohn -> Viatris; 2020-11-16 (7.8B-2A plan) vs 2020-11-17 (78B list, INFERRED)"),
    _e(26, "MRK", "SPIN_OFF", "2021-06-03", "VERIFIED", None, "Organon (OGN), 1 OGN per 10 MRK"),
    _e(27, "IBM", "SPIN_OFF", "2021-11-04", "VERIFIED", None, "Kyndryl (KD), 1 KD per 5 IBM"),
    _e(28, "T", "SPIN_OFF", "2022-04-11", "INFERRED", None, "WarnerMedia -> Warner Bros. Discovery (WBD), 0.241917 WBD per T"),
    _e(29, "GE", "SPIN_OFF", "2023-01-04", "VERIFIED", None, "GE HealthCare (GEHC), 1 GEHC per 3 GE"),
    _e(30, "DHR", "SPIN_OFF", "2023-10-02", "VERIFIED", None, "Veralto (VLTO)"),
    _e(31, "GE", "SPIN_OFF", "2024-04-02", "VERIFIED", None, "GE Vernova (GEV), 1 GEV per 4 GE"),
    _e(32, "HON", "SPIN_OFF", "2025-10-30", "VERIFIED", None, "Solstice Advanced Materials (SOLS), 1 SOLS per 4 HON"),
    _e(33, "HON", "SPIN_OFF", "2026-06-29", "UNRESOLVED", None,
       "OWNER DECISION (contradiction 3): Honeywell Aerospace (HONA) with the same-day reverse split"),
    # exchange offers (no pro-rata adjustment; month only)
    _e(34, "LLY", "EXCHANGE_OFFER", None, "UNVERIFIED", None, "Elanco (ELAN), around 2019-03"),
    _e(35, "DHR", "EXCHANGE_OFFER", None, "UNVERIFIED", None, "Envista (NVST), around 2019-12"),
    _e(36, "JNJ", "EXCHANGE_OFFER", None, "UNVERIFIED", None, "Kenvue (KVUE), around 2023-08"),
    # ticker changes
    _e(37, "META", "SYMBOL_CHANGE", "2022-06-09", "VERIFIED", None, "from FB; full history from 2016-01-04 under META"),
    _e(38, "BKNG", "SYMBOL_CHANGE", "2018-02-27", "UNVERIFIED", None, "from PCLN"),
    _e(39, "SPGI", "SYMBOL_CHANGE", "2016-04-28", "UNVERIFIED", None, "from MHFI"),
    _e(40, "RTX", "SYMBOL_CHANGE", "2020-04-03", "VERIFIED", None, "from UTX; UTX history before 2020-04-03"),
    # listing boundaries
    _e(41, "LIN", "LISTING_BOUNDARY", "2018-10-31", "VERIFIED", None, "a new issuer (Praxair PX ceased 2018-10-30)"),
    _e(42, "UBER", "LISTING_BOUNDARY", "2019-05-10", "UNVERIFIED"),
    _e(43, "SNDK", "LISTING_BOUNDARY", "2025-02-13", "OBSERVED", None, "the first SNDK bar in the Alpaca IEX bundle; the regular-way date is UNVERIFIED"),
    # identity reuse (annotation only, A9)
    _e(44, "SNDK", "TICKER_REUSE_RULE", "2025-01-01", "RULE", None,
       "no SNDK bar before 2025-01-01 (C9): the old SanDisk, a different issuer, traded as SNDK until about 2016-05-12 (UNVERIFIED)"),
    # holding-company / domicile changes (no price effect)
    _e(45, "AVGO", "HOLDING_COMPANY_CHANGE", None, "UNVERIFIED", None, "2016-02-01 / 2018-04-04"),
    _e(46, "DIS", "HOLDING_COMPANY_CHANGE", "2019-03-20", "UNVERIFIED"),
    _e(47, "MRVL", "HOLDING_COMPANY_CHANGE", "2021-04-20", "UNVERIFIED"),
    _e(48, "BLK", "HOLDING_COMPANY_CHANGE", "2024-10-01", "UNVERIFIED"),
    # the remaining open contradictions (owner decisions; AUDIT_B contradictions 4 and 6)
    _e(49, "BKNG", "SPLIT", "2026-04-06", "UNRESOLVED", None,
       "OWNER DECISION (contradiction 4): a split-ratio event in the IEX data that the 78B list omits"),
    _e(50, "KLAC", "SPLIT", "2026-06-12", "UNRESOLVED", None,
       "OWNER DECISION (contradiction 4): a split-ratio event in the IEX data that the 78B list omits"),
    _e(51, "XOM", "HOLDING_COMPANY_CHANGE", None, "UNRESOLVED", None,
       "OWNER DECISION (contradiction 6): the SEC map points XOM to CIK 0002115436 'ExxonMobil Holdings Corp' (filings from 2026-07-31)"),
)
UNRESOLVED_CONTRADICTIONS = {"HON": ("EE-019", "EE-033"), "PFE": ("EE-025",), "BKNG": ("EE-049",), "KLAC": ("EE-050",), "XOM": ("EE-051",)}


def entries(symbol: str, *, types=None, session: Optional[str] = None, table: Tuple[ExpectedEvent, ...] = TABLE) -> List[ExpectedEvent]:
    return [e for e in table if e.symbol == symbol and (types is None or e.type in types) and (session is None or e.session == session)]


def classifying(e: ExpectedEvent) -> bool:
    """Only VERIFIED entries classify (A9)."""
    return e.status == CLASSIFYING_STATUS
