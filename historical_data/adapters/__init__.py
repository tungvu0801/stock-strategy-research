"""historical_data/adapters/ - the v1 adapters, all offline and read-only (contract section 4, P6): SyntheticProvider (fixtures),
Ms1EvidenceProvider (the MS-1 raw archive only, never its SQLite store) and SipDailyV1Provider (the sip_daily_v1 bundle and its archived raw
responses; fixture-tested only). No CRSP, Norgate or Alpaca-fetch adapter exists in v1."""
