"""
market_intel/news/persist.py - Stage 7.3.1: point-in-time news clusters and ticker mentions, persisted in schema v2 (DESIGN_731 section 3).

Deterministic and model-free: clusters are the Stage 7.2 news.dedup.cluster, mentions the Stage 7.2 news.entities.mentions (no LLM, no
sentiment). The inputs are the stored NEWS facts KNOWN at `as_of` (MarketIntelStoreV2.news_inputs): for each article the latest revision
with available_at <= as_of and ingested_at <= as_of, first seen = the earliest such availability, kept only when that revision is usable.
A mention run also needs an entity map (SEC names, followed universe, aliases) recorded at or before as_of: reference data is point in
time too.

A run is identified by what it read: (kind, algorithm, map, window, input digest). The same inputs give the same run (idempotent: the
existing run is returned); a late correction - a new revision - changes the input digest and gives a new run, and every earlier run stays
(history). The same functions recompute a run from its stored inputs, so verify_integrity can prove a stored run is what its inputs give.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Dict, List, Optional, Sequence, Tuple

from market_intel.news import dedup as DD
from market_intel.news import entities as EN

CLUSTER_ALGORITHM = f"mi_news_clusters_v1:dedup(jaccard_min={DD.JACCARD_MIN},window_hours={DD.WINDOW_HOURS})"
MENTION_ALGORITHM = (f"mi_news_mentions_v1:entities(confidence={json.dumps(EN.CONFIDENCE, sort_keys=True)},roundup_over={EN.ROUNDUP_OVER},"
                     f"catalyst=({EN.CATALYST_MIN_CONFIDENCE},{EN.CATALYST_MAX_TIER}))")
MAP_KEYS = ("sec_names", "universe", "aliases")
_TS = "%Y-%m-%dT%H:%M:%S.%fZ"


def canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_of(obj) -> str:
    return hashlib.sha256(canonical(obj).encode("utf-8")).hexdigest()


def _dt(s: Optional[str]) -> Optional[datetime]:
    return None if s is None else datetime.strptime(s, _TS).replace(tzinfo=timezone.utc)


def normalize_map(entity_map: dict) -> dict:
    """The entity map in canonical form; anything else is refused (ValueError)."""
    from api.validation import validate_symbol
    if not isinstance(entity_map, dict) or set(entity_map) != set(MAP_KEYS):
        raise ValueError(f"an entity map has exactly the keys {MAP_KEYS}")
    sec, uni, al = entity_map["sec_names"], entity_map["universe"], entity_map["aliases"]
    if not isinstance(sec, dict) or not isinstance(uni, (list, tuple)) or not isinstance(al, dict):
        raise ValueError("sec_names {ticker: title}, universe [tickers], aliases {ticker: [names]}")

    def t(x):
        s = validate_symbol(str(x))
        if s != str(x):
            raise ValueError(f"ticker {x!r} is not upper-case")
        return s
    return {"sec_names": {t(k): str(v) for k, v in sorted(sec.items())}, "universe": sorted({t(x) for x in uni}),
            "aliases": {t(k): sorted({str(n) for n in v}) for k, v in sorted(al.items())}}


def items(inputs: Sequence[dict]) -> List[dict]:
    return [{"key": i["natural_key"], "provider": i["provider"], "provider_id": i["source_identifier"], "headline": i["payload"].get("headline"),
             "source": i["payload"].get("source"), "url": i["source_url"], "symbols": list(i["payload"].get("symbols") or []),
             "published_at": _dt(i["published_at"]), "first_seen_at": _dt(i["first_seen_at"]), "tier": i["tier"]} for i in inputs]


def compute_clusters(inputs: Sequence[dict]) -> Tuple[List[dict], List[dict]]:
    """(clusters, members): one member row per input article, one cluster row per cluster."""
    if not inputs:
        return [], []
    by_key = {i["natural_key"]: i for i in inputs}
    res = DD.cluster(items(inputs))
    members = sorted(({"natural_key": r["key"], "cluster_id": r["cluster_id"], "representative": int(bool(r["representative"])), "rule": r["rule"]}
                      for r in res), key=lambda m: m["natural_key"])
    groups: Dict[str, List[dict]] = {}
    for m in members:
        groups.setdefault(m["cluster_id"], []).append(m)
    clusters = []
    for cid, ms in sorted(groups.items()):
        rep = [m["natural_key"] for m in ms if m["representative"]]
        pubs = [by_key[m["natural_key"]]["published_at"] for m in ms if by_key[m["natural_key"]]["published_at"]]
        clusters.append({"cluster_id": cid, "representative_key": rep[0], "member_count": len(ms),
                         "first_seen_at": min(by_key[m["natural_key"]]["first_seen_at"] for m in ms), "first_published_at": min(pubs) if pubs else None})
    return clusters, members


def compute_mentions(inputs: Sequence[dict], entity_map: dict) -> List[dict]:
    m = normalize_map(entity_map)
    out = []
    for i in inputs:
        p = i["payload"]
        for x in EN.mentions(headline=p.get("headline"), summary=p.get("summary"), provider_symbols=p.get("symbols") or [], sec_names=m["sec_names"],
                             universe=m["universe"], aliases=m["aliases"]):
            out.append({"natural_key": i["natural_key"], "symbol": x["symbol"], "method": x["method"], "confidence": float(x["confidence"]),
                        "role": x["role"], "in_headline": int(bool(x["in_headline"])), "catalyst_eligible": int(EN.catalyst_eligible(x, i["tier"]))})
    return sorted(out, key=lambda r: (r["natural_key"], r["symbol"]))


def input_sha256(inputs: Sequence[dict]) -> str:
    return sha256_of(sorted([i["natural_key"], int(i["revision"]), i["record_sha256"], i["first_seen_at"]] for i in inputs))


def output_sha256(clusters: Sequence[dict], members: Sequence[dict], mentions: Sequence[dict]) -> str:
    return sha256_of({"clusters": sorted(canonical(c) for c in clusters), "members": sorted(canonical(m) for m in members),
                      "mentions": sorted(canonical(m) for m in mentions)})


def cluster_run(store, *, as_of: datetime, window_start: datetime, window_end: datetime) -> dict:
    inputs = store.news_inputs(as_of=as_of, window_start=window_start, window_end=window_end)
    clusters, members = compute_clusters(inputs)
    return store.record_news_run(run_kind="CLUSTERS", algorithm=CLUSTER_ALGORITHM, map_sha256="", as_of=as_of, window_start=window_start,
                                 window_end=window_end, inputs=inputs, clusters=clusters, members=members, mentions=[])


def mention_run(store, *, as_of: datetime, window_start: datetime, window_end: datetime, map_sha256: str) -> dict:
    entity_map = store.entity_map(map_sha256, as_of=as_of)
    inputs = store.news_inputs(as_of=as_of, window_start=window_start, window_end=window_end)
    return store.record_news_run(run_kind="MENTIONS", algorithm=MENTION_ALGORITHM, map_sha256=map_sha256, as_of=as_of, window_start=window_start,
                                 window_end=window_end, inputs=inputs, clusters=[], members=[], mentions=compute_mentions(inputs, entity_map))
