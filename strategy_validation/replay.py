"""strategy_validation/replay.py — the REPLAY VALIDATION FOUNDATION (future-ready; nothing here reads a broker or the execution store).

A future stage will ask: for session X, did the paper / live strategy generate the same candidate state as the research engine? The contract:

  CandidateState   (session_date, strategy_id, strategy_version, symbol, state, candidate_id | None, source RESEARCH | PAPER | LIVE)
  ReplaySource     a provider of observed states for a session (the paper side would map the Stage 5.8 setup transitions / Stage 5.9 proposals of
                   that session; not implemented in Stage 6.2)
  compare()        research vs observed -> matched, research_only, observed_only, state mismatches (deterministic, pure)

research_states(run) derives the research side from a stored validation run (one CANDIDATE state per generated candidate on its signal session)."""
from __future__ import annotations

from typing import Iterable, List, Optional, Protocol

from pydantic import BaseModel, ConfigDict


class CandidateState(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    session_date: str
    strategy_id: str
    strategy_version: str
    symbol: str
    state: str                                  # CANDIDATE | NO_CANDIDATE | READY | WATCH | ... (the source's own vocabulary, mapped by the source)
    candidate_id: Optional[str] = None
    source: str = "RESEARCH"                    # RESEARCH | PAPER | LIVE


class ReplaySource(Protocol):
    name: str

    def states_for(self, session_date: str, strategy_id: str) -> List[CandidateState]:
        ...


def research_states(run: dict) -> List[CandidateState]:
    return [CandidateState(session_date=c["signal_date"], strategy_id=c["strategy_id"], strategy_version=c["strategy_version"], symbol=c["symbol"], state="CANDIDATE",
                           candidate_id=c["candidate_id"], source="RESEARCH") for c in run.get("candidates") or []]


def compare(research: Iterable[CandidateState], observed: Iterable[CandidateState]) -> dict:
    key = lambda s: (s.session_date, s.strategy_id, s.symbol)  # noqa: E731
    r = {key(s): s for s in research}
    o = {key(s): s for s in observed}
    matched = sorted(k for k in r if k in o and (r[k].state == o[k].state or o[k].state in ("READY", "CANDIDATE")))
    mismatched = sorted(k for k in r if k in o and k not in matched)
    versions = sorted(k for k in r if k in o and r[k].strategy_version != o[k].strategy_version)
    return {"matched": [list(k) for k in matched], "research_only": [list(k) for k in sorted(set(r) - set(o))], "observed_only": [list(k) for k in sorted(set(o) - set(r))],
            "state_mismatch": [list(k) for k in mismatched], "version_mismatch": [list(k) for k in versions],
            "consistent": not (set(r) ^ set(o)) and not mismatched and not versions}
