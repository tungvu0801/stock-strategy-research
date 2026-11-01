"""
market_intel/news - Stage 7.2: deterministic news processing (design C.10; DESIGN_72 section 4). Never a model, never a network call.

  attribution.py  canonical URL, host, evidence tier (evidence.rules.tier_for), deny-host metadata, the <= 300-character snippet
  dedup.py        clusters: exact provider id, canonical URL, normalised headline hash, near-duplicate headlines (pre-registered)
  entities.py     ticker / entity mentions with method and confidence, common-word tickers, PRIMARY / MENTION / ROUNDUP roles

Raw articles are always kept (the store's NEWS observations); deduplication is a cluster id, never a delete.
"""
