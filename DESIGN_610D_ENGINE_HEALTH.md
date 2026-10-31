# Stage 6.10D — Engine health visibility (observability only)

**Why.** Deploying Stage 5.4.1 to the live tree (2026-10-31) showed what an operator had to read with raw GETs because no panel showed it:
the commit the running code was started from (`/api/health` has no version), the Stage 6.10A start-up outcome (`auto_run.startup`), the
preflight transition DEGRADED → READY and its non-PASS checks (`auto_run.preflight.checks`), the supervisor's lease / last error
(`auto_run.supervisor`), the worker heartbeat and its staleness (`worker`), the Robinhood gateway state (`/api/unified-portfolio/robinhood`
→ `GATEWAY_DOWN`) and the fact that a tree can be checked out under a running process without anything saying a restart is due.

**What.** Two display-only additions, nothing decided and no POST:

1. `GET /api/ops/engine` (api/routes/ops_status.py, marked block "Engine health visibility, Stage 6.10D"; the marker never starts with
   "Stage 6.1", which the Stage 6.1 freeze tests scan for as a prefix): `deployed` — the commit the running process was imported
   from, read once at import from `.git` without running git (plain repo, a worktree whose `.git` is a `gitdir:` file with `commondir`,
   symbolic or detached HEAD, loose or packed refs; `sha` is `None` with a `source` reason when it cannot be read); `tree_now` — the same
   read now, and `tree_matches_running`; `process` — pid, start time, interpreter, working directory; `runner` — the runner's PID file
   (scripts/run_stock_agent.py) **only** when it names this process, otherwise `None`. No subprocess, no network, no broker, no Robinhood.
2. The **ENGINE fold** under the diagnostics line of the TRADE section (frontend/product_ui.js, marked block "Engine health visibility,
   Stage 6.10D"): `engineModel(activity, engine, robinhood)` turns values the backend already reports into rows — Deployed (sha, branch,
   start time; a differing checked-out tree is a warning asking for a restart), Start-up (Monitor only / demoted with its code / still
   starting), Preflight (status, blocking codes, every non-PASS check; INFO checks are information, not a degradation), Supervisor (error /
   running / lease / last run), Worker (state, heartbeat age, STALE past `stale_after_s`, next run), Auto-run problems, and Robinhood
   (read-only, user managed; a gateway that is down is a warning with the backend's message). The activity payload is the one the Stage 6.8
   block already polls; the descriptor is read once per page; the Robinhood state is read only while the fold is open (at most once a
   minute). Level precedence: bad > warn > ok > unknown; nothing not loaded ever reads as healthy.

**What it does not do.** No change to `execution/`, `safety/`, `autorun/`, `setups/`, `ppe/`, `proposals/`, `trader_view/`, `scripts/`,
`api/server.py`, `config.py`, the header SYSTEM model, the safety gate, Paper Auto, READY thresholds, Robinhood (read-only) or the browser
harness. The runner's own logging (clean shutdown, sleep / resume) belongs to the runtime-reliability work and is deliberately left out.

**Tests.** `tests/frontend/engine_health_610d.test.cjs` (9 node tests on the model, the fold and the wiring) and
`tests/test_engine_health_610d.py` (R01–R04 the descriptor and its parser on every `.git` layout without a subprocess, GET only; N01 the node
tests; F01–F03 the change set is exactly the declared files, the protected directories and files are byte-identical to main, READY
thresholds / PPE scoring / frozen hashes unchanged, every bookkeeping edit pinned, the ledger changes only the declared entries).
Freeze bookkeeping: `tests/freeze_ledger.py` BASE_610D / NEW_610D / EDITED_610D / SEMANTIC_610D = {} / INSTRUMENTATION_610D = {}.
