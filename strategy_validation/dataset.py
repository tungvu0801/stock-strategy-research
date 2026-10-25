"""strategy_validation/dataset.py — the bar dataset a validation runs on (completed daily sessions per symbol), its content hash (dataset_id) and the
CHRONOLOGICAL train / OOS split. There is no random split: market time series are split by date, the earlier share for development (TRAIN), the
later share untouched (OOS). A dataset too short for an adequate OOS window yields an empty or short OOS slice, which the verdict reports as
INSUFFICIENT_DATA.

Provenance (Stage 6.3.1): the dataset DECLARES source, retrieved_at, bar_frequency, adjustment and missing_data_policy (plus corporate_actions,
universe and point_in_time when the adapter requires them); dataset_id, start, end, symbols and sessions are DERIVED from the bars. A run
without complete provenance may execute ephemerally but is never persisted (strategy_validation/integrity.py)."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import math
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from strategy_validation import DEFAULT_TRAIN_RATIO, TRAIN_RATIO_BOUNDS
from strategy_validation.models import Bar


class DatasetError(ValueError):
    pass


def _canonical_rows(bars: Dict[str, List[Bar]]) -> str:
    rows = [[s, b.date, b.open, b.high, b.low, b.close, b.volume] for s in sorted(bars) for b in bars[s]]
    return json.dumps(rows, separators=(",", ":"))


class Dataset:
    def __init__(self, bars: Dict[str, Iterable], *, kind: str = "HISTORICAL", source: str = "", dataset_id: Optional[str] = None,
                 provenance: Optional[dict] = None):
        if kind not in ("HISTORICAL", "SYNTHETIC"):
            raise DatasetError("dataset kind must be HISTORICAL or SYNTHETIC")
        clean: Dict[str, List[Bar]] = {}
        for sym, rows in bars.items():
            seen, out = set(), []
            for r in rows:
                b = r if isinstance(r, Bar) else Bar(**r)
                vals = (b.open, b.high, b.low, b.close)
                if any(not isinstance(v, (int, float)) or math.isnan(v) or math.isinf(v) or v <= 0 for v in vals):
                    raise DatasetError(f"{sym} {b.date}: OHLC must be positive finite numbers")
                if b.low > min(b.open, b.close) + 1e-9 or b.high < max(b.open, b.close) - 1e-9:
                    raise DatasetError(f"{sym} {b.date}: inconsistent OHLC (low above open/close or high below open/close)")
                if b.date in seen:
                    raise DatasetError(f"{sym} {b.date}: duplicate bar")
                seen.add(b.date)
                out.append(b)
            clean[str(sym).upper()] = sorted(out, key=lambda x: x.date)
        if not any(clean.values()):
            raise DatasetError("the dataset has no bars")
        self.bars = clean
        self.kind = kind
        self.source = source
        self.declared = {k: v for k, v in dict(provenance or {}).items() if v not in (None, "")}
        self.sessions: List[str] = sorted({b.date for rows in clean.values() for b in rows})
        self.dataset_id = dataset_id or ("sha256:" + hashlib.sha256(_canonical_rows(clean).encode("utf-8")).hexdigest()[:24])
        self._index = {s: {b.date: i for i, b in enumerate(rows)} for s, rows in clean.items()}

    @property
    def start(self) -> str:
        return self.sessions[0]

    @property
    def end(self) -> str:
        return self.sessions[-1]

    def bar(self, symbol: str, date: str) -> Optional[Bar]:
        i = self._index.get(symbol, {}).get(date)
        return None if i is None else self.bars[symbol][i]

    def bars_after(self, symbol: str, date: str) -> List[Bar]:
        """Every bar of the symbol strictly after `date` (chronological)."""
        i = self._index.get(symbol, {}).get(date)
        return [] if i is None else self.bars[symbol][i + 1:]

    def next_bar(self, symbol: str, date: str) -> Optional[Bar]:
        after = self.bars_after(symbol, date)
        return after[0] if after else None

    def until(self, date: str) -> "Dataset":
        """The point-in-time view at the close of `date`: every bar after it removed (the truncation-invariance causality check and the
        boundary-safe TRAIN simulation)."""
        return Dataset({s: [b for b in rows if b.date <= date] for s, rows in self.bars.items()}, kind=self.kind, source=self.source, provenance=self.declared)

    def has_bar_after(self, symbol: str, date: str) -> bool:
        rows = self.bars.get(symbol) or []
        return bool(rows) and rows[-1].date > date

    def provenance_record(self) -> dict:
        """What the run stores: the declared fields as given, the derived fields computed here (never taken from the declaration)."""
        return {**self.declared, "dataset_id": self.dataset_id, "kind": self.kind, "start": self.start, "end": self.end, "symbols": sorted(self.bars),
                "sessions": len(self.sessions), "source_file": self.source}


def load(path: str, provenance: Optional[dict] = None) -> Dataset:
    """A CSV (symbol,date,open,high,low,close[,volume]) or JSON ({"bars": {symbol: [{date, open, high, low, close, volume}]}}) file of completed
    daily sessions. The dataset_id is the content hash, so the same file always reproduces the same run."""
    p = Path(path)
    if not p.is_file():
        raise DatasetError(f"dataset file not found: {path}")
    text = p.read_text(encoding="utf-8")
    bars: Dict[str, list] = {}
    kind = "HISTORICAL"
    declared: dict = {}
    if p.suffix.lower() == ".json":
        obj = json.loads(text)
        kind = obj.get("kind", "HISTORICAL")                                    # a fixture file declares itself SYNTHETIC (the store refuses it outside tests)
        declared = dict(obj.get("provenance") or {})
        for sym, rows in (obj.get("bars") or {}).items():
            bars[sym] = [Bar(**r) for r in rows]
    else:
        for r in csv.DictReader(io.StringIO(text)):
            bars.setdefault(r["symbol"].strip().upper(), []).append(Bar(date=r["date"].strip(), open=float(r["open"]), high=float(r["high"]), low=float(r["low"]),
                                                                         close=float(r["close"]), volume=float(r.get("volume") or 0)))
    return Dataset(bars, kind=kind, source=str(p.name), provenance={**declared, **dict(provenance or {})})   # an explicit --provenance file wins


def dump(dataset: Dataset, path: str) -> None:
    Path(path).write_text(json.dumps({"kind": dataset.kind, "provenance": dataset.declared,
                                      "bars": {s: [b.model_dump() for b in rows] for s, rows in dataset.bars.items()}}), encoding="utf-8")


def split(dataset: Dataset, train_ratio: float = DEFAULT_TRAIN_RATIO) -> dict:
    lo, hi = TRAIN_RATIO_BOUNDS
    if not (lo <= float(train_ratio) <= hi):
        raise DatasetError(f"train_ratio must be within {lo}-{hi} (chronological development share)")
    n = len(dataset.sessions)
    k = int(math.floor(n * float(train_ratio)))
    train, oos = dataset.sessions[:k], dataset.sessions[k:]
    if not train:
        raise DatasetError("the dataset is too short for a train window")
    return {"method": "CHRONOLOGICAL", "train_ratio": float(train_ratio), "sessions": n, "train_start": train[0], "train_end": train[-1],
            "oos_start": oos[0] if oos else None, "oos_end": oos[-1] if oos else None, "train_sessions": len(train), "oos_sessions": len(oos)}


def segment_of(signal_date: str, split_info: dict) -> str:
    return "TRAIN" if signal_date <= split_info["train_end"] else "OOS"
