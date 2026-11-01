"""
market_intel/collector - Stage 7.3: the OFFLINE collection and scheduling layer (DESIGN_73_MARKET_INTEL_COLLECTOR.md).

  plans.py      one registered, frozen collection plan per source: cadence, budget, caps, feed-health policy, credential, approvals
  transport.py  the injectable transport: FakeTransport (tests), NoTransport (the default: refuses), error classification
  policy.py     retry with exponential backoff and deterministic jitter; the circuit breaker
  state.py      the collector's own append-only state file (runs, run ends, watermarks, leases, breaker events, gaps)
  runner.py     one bounded collection run: gates, lease, pages, provenance, ingestion through the Stage 7.1 store, seal, watermark
  scheduler.py  deterministic scheduling on an injected clock: due(), tick(), request_stop(), recover() - no thread
  health.py     availability, degraded and stale states per source, on the Stage 7.0 freshness policies

Nothing here can reach a network: Stage 7.3 ships no real transport, starts no thread and adds no config, route or credential. The
runner refuses any transport but FakeTransport, any credential not explicitly present, any blocked plan (both SEC plans) and any sink
job kind but FIXTURE (live collection needs a schema decision - DESIGN_73 section 5).
"""
