"""
dynamic_universe/core.py - Core universe v1 (Stage 6.10C): the broad, slowly changing first layer of the future two-layer universe.

CORE_V1 is a FROZEN literal built once from the data/sector_map.py peers at the Stage 6.10C base (fed5b3c): the map's 77 peers in map order
minus GOOG, the duplicate share class of GOOGL (76), then the three live watchlist symbols the map lacks (79). Sector tags are the map's; SNDK carries
config.EVIDENCE_SECTOR_EXTENSIONS' "Semiconductors" (what ppe.sectors.sector_for returns); CLS and SHOP are classified nowhere in the repo and
read "Unmapped" - a classification is never invented. CORE_V1_SHA256 pins the list, so a later SECTOR_MAP edit cannot change it silently.

load_core() honours DYNAMIC_UNIVERSE_CORE_FILE: a JSON list of {"symbol": ..., "sector": ...} objects replacing the built-in list. Symbols
follow the API's ticker rule (api.validation.validate_symbol: upper-cased), duplicates are dropped, at most CORE_FILE_MAX are kept and every
dropped entry is reported. An unreadable, oversized or empty file falls back to the built-in core with a problem: the core is never empty.
Reading the file is the only I/O here; nothing is requested, written or started.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from dynamic_universe import UNMAPPED

CORE_VERSION = "core_v1"
FILE_VERSION = "core_file"              # a DYNAMIC_UNIVERSE_CORE_FILE core: identified by its own sha256
CORE_FILE_MAX = 500                     # symbols kept from a core file; the rest are dropped and reported
CORE_FILE_MAX_BYTES = 1_000_000         # a larger file is not read (the built-in core applies)
SECTOR_MAX_CHARS = 40
PROBLEMS_MAX = 50                       # individual dropped-entry lines reported; the remainder is counted in one line

CORE_V1: Tuple[Tuple[str, str], ...] = (
    # data/sector_map.py SECTOR_MAP peers at fed5b3c, in map order (GOOG dropped: the duplicate share class of GOOGL)
    ("NVDA", "Semiconductors"), ("AMD", "Semiconductors"), ("INTC", "Semiconductors"), ("MU", "Semiconductors"), ("AVGO", "Semiconductors"),
    ("MRVL", "Semiconductors"), ("QCOM", "Semiconductors"), ("TXN", "Semiconductors"),
    ("MSFT", "Software"), ("ORCL", "Software"), ("CRM", "Software"), ("ADBE", "Software"), ("NOW", "Software"), ("PANW", "Software"),
    ("INTU", "Software"),
    ("JPM", "Financials"), ("BAC", "Financials"), ("GS", "Financials"), ("MS", "Financials"), ("WFC", "Financials"), ("C", "Financials"),
    ("SCHW", "Financials"), ("AXP", "Financials"),
    ("XOM", "Energy"), ("CVX", "Energy"), ("COP", "Energy"), ("SLB", "Energy"), ("OXY", "Energy"), ("EOG", "Energy"),
    ("LLY", "Healthcare"), ("UNH", "Healthcare"), ("JNJ", "Healthcare"), ("MRK", "Healthcare"), ("ABBV", "Healthcare"), ("PFE", "Healthcare"),
    ("TMO", "Healthcare"), ("ISRG", "Healthcare"),
    ("AMZN", "Consumer Discretionary"), ("TSLA", "Consumer Discretionary"), ("HD", "Consumer Discretionary"), ("MCD", "Consumer Discretionary"),
    ("NKE", "Consumer Discretionary"), ("LOW", "Consumer Discretionary"), ("SBUX", "Consumer Discretionary"), ("BKNG", "Consumer Discretionary"),
    ("PG", "Consumer Staples"), ("KO", "Consumer Staples"), ("PEP", "Consumer Staples"), ("WMT", "Consumer Staples"), ("COST", "Consumer Staples"),
    ("PM", "Consumer Staples"),
    ("CAT", "Industrials"), ("GE", "Industrials"), ("RTX", "Industrials"), ("UNP", "Industrials"), ("HON", "Industrials"), ("BA", "Industrials"),
    ("NEE", "Utilities"), ("DUK", "Utilities"), ("SO", "Utilities"), ("D", "Utilities"), ("AEP", "Utilities"),
    ("PLD", "Real Estate"), ("AMT", "Real Estate"), ("EQIX", "Real Estate"), ("SPG", "Real Estate"),
    ("GOOGL", "Communication Services"), ("META", "Communication Services"), ("NFLX", "Communication Services"), ("DIS", "Communication Services"),
    ("T", "Communication Services"), ("VZ", "Communication Services"),
    ("LIN", "Materials"), ("SHW", "Materials"), ("FCX", "Materials"), ("NEM", "Materials"),
    # the live watchlist symbols the map lacks (SNDK: config.EVIDENCE_SECTOR_EXTENSIONS; CLS, SHOP: no classification in the repo)
    ("CLS", "Unmapped"), ("SHOP", "Unmapped"), ("SNDK", "Semiconductors"),
)
CORE_V1_SHA256 = "b157bad8bebf0b3b4f6321b96450230a5803fc650b4f2c03dd84d1660db3a03d"
BUILTIN_SOURCE = "built-in core v1 (data/sector_map.py peers + CLS, SHOP, SNDK)"


def canonical(entries: Iterable[Tuple[str, str]]) -> str:
    """The text the pin is computed over: [[symbol, sector], ...] in list order, compact JSON (forward_capture.project.canonical's settings)."""
    return json.dumps([[s, sec] for s, sec in entries], sort_keys=True, separators=(",", ":"), default=str)


def digest(entries: Iterable[Tuple[str, str]]) -> str:
    return hashlib.sha256(canonical(entries).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Core:
    version: str
    source: str
    entries: Tuple[Tuple[str, str], ...]
    sha256: str
    problems: Tuple[str, ...] = ()

    @property
    def symbols(self) -> Tuple[str, ...]:
        return tuple(s for s, _ in self.entries)

    @property
    def count(self) -> int:
        return len(self.entries)

    def sectors(self) -> Dict[str, int]:
        """{sector: symbols}, in first-seen order of the list."""
        out: Dict[str, int] = {}
        for _, sec in self.entries:
            out[sec] = out.get(sec, 0) + 1
        return out

    def summary(self) -> Dict[str, Any]:
        return {"version": self.version, "count": self.count, "sha256": self.sha256, "source": self.source, "sectors": self.sectors()}


def builtin(problems: Iterable[str] = ()) -> Core:
    """The built-in core v1. A literal that no longer matches its pin is reported, never silently trusted."""
    extra = [] if digest(CORE_V1) == CORE_V1_SHA256 else ["the built-in core v1 does not match CORE_V1_SHA256 (the frozen list was edited)"]
    return Core(CORE_VERSION, BUILTIN_SOURCE, CORE_V1, digest(CORE_V1), tuple(list(problems) + extra))


def _fallback(name: str, why: str, problems: Iterable[str] = ()) -> Core:
    return builtin(list(problems) + [f"core file {name}: {why}; the built-in core v1 applies"])


def _entries(data: List[Any]) -> Tuple[List[Tuple[str, str]], List[str]]:
    """(kept entries, problems) of a parsed core file: every invalid, duplicate or over-cap entry is dropped and reported."""
    from api.validation import validate_symbol
    kept: List[Tuple[str, str]] = []
    seen = set()
    problems: List[str] = []
    over: List[str] = []
    for i, item in enumerate(data, 1):
        sym = item.get("symbol") if isinstance(item, dict) else None
        if not isinstance(sym, str):
            problems.append(f"entry {i}: not an object with a text symbol - dropped")
            continue
        try:
            sym = validate_symbol(sym)
        except ValueError:
            problems.append(f"entry {i}: {sym.strip()[:20]!r} is not a valid ticker symbol - dropped")
            continue
        if sym in seen:
            problems.append(f"entry {i}: {sym} repeats an earlier entry - dropped")
            continue
        seen.add(sym)
        if len(kept) >= CORE_FILE_MAX:
            over.append(sym)
            continue
        sector = item.get("sector")
        if isinstance(sector, str) and sector.strip():
            kept.append((sym, sector.strip()[:SECTOR_MAX_CHARS]))
        else:
            kept.append((sym, UNMAPPED))                                  # a missing sector is Unmapped; one that is not text is also reported
            if sector is not None:
                why = "is blank" if isinstance(sector, str) else "is not text"
                problems.append(f"entry {i}: {sym} sector {why} - read as {UNMAPPED}")
    if len(problems) > PROBLEMS_MAX:
        problems = problems[:PROBLEMS_MAX] + [f"... and {len(problems) - PROBLEMS_MAX} more entries reported (dropped or read as {UNMAPPED})"]
    if over:
        shown = ", ".join(over[:10]) + (f" (+{len(over) - 10} more)" if len(over) > 10 else "")
        problems.append(f"{len(over)} entries beyond the cap of {CORE_FILE_MAX} symbols - dropped: {shown}")
    return kept, problems


def load_core(path: Optional[Any] = None) -> Core:
    """The core in force: `path`, else config.DYNAMIC_UNIVERSE_CORE_FILE, else the built-in v1. Never empty; never raises.
    A relative path is read from the process's working directory."""
    if path is None:
        import config
        path = getattr(config, "DYNAMIC_UNIVERSE_CORE_FILE", "")
    text = str(path or "").strip()
    if not text:
        return builtin()
    p = Path(text)
    name = p.name or text
    try:
        size = p.stat().st_size
        if size > CORE_FILE_MAX_BYTES:
            return _fallback(name, f"{size} bytes is over the {CORE_FILE_MAX_BYTES}-byte limit")
        data = json.loads(p.read_bytes().decode("utf-8-sig"))
    except FileNotFoundError:
        return _fallback(name, "not found")
    except (OSError, UnicodeDecodeError, ValueError, RecursionError) as exc:
        return _fallback(name, f"unreadable ({type(exc).__name__})")
    if not isinstance(data, list):
        return _fallback(name, 'expected a JSON list of {"symbol", "sector"} objects')
    kept, problems = _entries(data)
    if not kept:
        return _fallback(name, "no valid entry", problems)
    return Core(FILE_VERSION, f"file {name}", tuple(kept), digest(kept), tuple(problems))
