"""
tests/test_db_bootstrap_busy.py — the bounded WAL bootstrap of database/database.py (post-Stage-6.1.2 fix; CI on main 7961cfe:
tests/test_aapl_smoke_60.py::test_X02 got return codes [0, 1] instead of [0, 3]).

Root cause: two smoke processes started together both construct ResearchDatabase(<runtime>/aapl_paper_smoke_reads.db) before either takes the
smoke's OS lock; for the NEW file both run PRAGMA journal_mode=WAL, the switch needs an exclusive lock, and SQLite answers SQLITE_BUSY ("database is
locked") for it WITHOUT waiting on the busy handler - so the 5 s connect timeout never helps and the losing process crashed (exit 1) before it could
reach the lock check that makes it exit 3 (REFUSED). Fix: _enable_wal() reads the persistent journal mode first (no exclusive lock needed once the
file is in WAL) and retries only the switch, a bounded number of times with a fixed backoff; the last failure re-raises the original error.

  C  real processes: many processes bootstrap ONE new file at the same instant - none crashes, the file ends in WAL
  U  _enable_wal: bounded retries, exact backoff, deterministic final failure, non-busy errors never retried, an existing WAL file is not switched
  F  freeze: database/database.py differs from main 7961cfe only by the listed edits; the three reconciled freeze tests and the CI manifest only by
     their listed edits; nothing else changed (execution, safety, the smoke script, the lease / OS lock and every exactly-once guard are untouched)
"""
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import database.database as DB  # noqa: E402

BASE = "7961cfe1f91fb2a64d3b3dc3a7d5e8067e389937"                                          # origin/main with Stage 6.1.2 merged (PR #20), CI red on test_X02
NEW_FIX = {"tests/test_db_bootstrap_busy.py"}
CHANGED_FIX = {"database/database.py", "tests/test_forward_validation_61.py", "tests/test_product_ui_611.py", "tests/test_product_flow_612.py", ".github/ci/pytest_groups.json"}

DB_EDITS = [
    ("simpler and safer than a shared connection + lock for a local single-user\napp. WAL mode lets reads and the writer proceed without blocking each other.\n\"\"\"\nimport sqlite3\nimport threading\n",
     "simpler and safer than a shared connection + lock for a local single-user\napp. WAL mode lets reads and the writer proceed without blocking each other.\n\n"
     "Bootstrap race (two processes creating the same NEW file at once): switching a\nrollback-journal database to WAL needs an exclusive lock, and SQLite answers\n"
     "SQLITE_BUSY (\"database is locked\") for that switch without waiting on the busy\nhandler. WAL is persistent, so _enable_wal() only switches when the file is not\n"
     "in WAL yet, and retries the switch a bounded number of times\n(WAL_SWITCH_ATTEMPTS, backoff WAL_SWITCH_BACKOFF_S, at most ~5 s of sleep);\n"
     "after the last attempt the original OperationalError is raised unchanged.\n\"\"\"\nimport sqlite3\nimport threading\nimport time\n"),
    ("def _int_to_bool(value) -> Optional[bool]:\n    return None if value is None else bool(value)\n",
     "def _int_to_bool(value) -> Optional[bool]:\n    return None if value is None else bool(value)\n\n\n"
     "WAL_SWITCH_ATTEMPTS = 8\nWAL_SWITCH_BACKOFF_S = (0.05, 0.1, 0.2, 0.4, 0.8, 1.6, 2.0)      # between the attempts: 5.15 s at most, then the original error\n\n\n"
     "def _is_busy(exc: sqlite3.OperationalError) -> bool:\n    msg = str(exc).lower()\n    return \"database is locked\" in msg or \"database is busy\" in msg\n\n\n"
     "def _enable_wal(conn: sqlite3.Connection, sleep=time.sleep) -> None:\n"
     "    \"\"\"Put the database in WAL mode. Reading the mode needs no exclusive lock; only a file not yet in WAL is switched, and\n"
     "    that switch (SQLITE_BUSY without the busy handler during a concurrent bootstrap) is retried a bounded number of times.\"\"\"\n"
     "    for attempt in range(WAL_SWITCH_ATTEMPTS):\n        try:\n            if str(conn.execute(\"PRAGMA journal_mode;\").fetchone()[0]).lower() == \"wal\":\n                return\n"
     "            conn.execute(\"PRAGMA journal_mode=WAL;\")\n            return\n        except sqlite3.OperationalError as exc:\n"
     "            if not _is_busy(exc) or attempt == WAL_SWITCH_ATTEMPTS - 1:\n                raise\n            sleep(WAL_SWITCH_BACKOFF_S[attempt])\n"),
    ("        conn.row_factory = sqlite3.Row\n        conn.execute(\"PRAGMA journal_mode=WAL;\")\n        conn.execute(\"PRAGMA busy_timeout=5000;\")\n        conn.execute(\"PRAGMA foreign_keys=ON;\")\n",
     "        conn.row_factory = sqlite3.Row\n        conn.execute(\"PRAGMA busy_timeout=5000;\")\n        try:\n            _enable_wal(conn)\n        except BaseException:\n            conn.close()\n"
     "            raise\n        conn.execute(\"PRAGMA foreign_keys=ON;\")\n"),
]
EDITS_61_FIX = [                                                                                 # tests/test_forward_validation_61.py (replayed by 6.1.1 F03 and 6.1.2 F02 too)
    ('           "DESIGN_612_UNIFIED_PRODUCT_FLOW.md", "DESIGN_612_STRATEGY_REGISTRY.md", "DESIGN_612_QUANTCONNECT_ADAPTER.md", "STRATEGY_STANDARD_V1.md"}   # Stage 6.1.2 files\n',
     '           "DESIGN_612_UNIFIED_PRODUCT_FLOW.md", "DESIGN_612_STRATEGY_REGISTRY.md", "DESIGN_612_QUANTCONNECT_ADAPTER.md", "STRATEGY_STANDARD_V1.md"}   # Stage 6.1.2 files\n'
     'NEW_FIX = {"tests/test_db_bootstrap_busy.py"}   # the WAL bootstrap fix (tests/test_db_bootstrap_busy.py pins database/database.py)\n'),
    ('    assert new == NEW_FILES | NEW_611 | NEW_612, {"unexpected_new": sorted(new - NEW_FILES - NEW_611 - NEW_612), "missing_new": sorted((NEW_FILES | NEW_611 | NEW_612) - new)}   # Stage 6.1.1 / 6.1.2 files added\n',
     '    assert new == NEW_FILES | NEW_611 | NEW_612 | NEW_FIX, {"unexpected_new": sorted(new - NEW_FILES - NEW_611 - NEW_612 - NEW_FIX), "missing_new": sorted((NEW_FILES | NEW_611 | NEW_612 | NEW_FIX) - new)}   # Stage 6.1.1 / 6.1.2 files + the WAL bootstrap fix\n'),
    ('    allowed = ADDITIVE | set(RECONCILED_TESTS) | {"tests/test_account_refresh_601.py", "tests/test_forward_validation_61.py"} | CI_FILES   # Stage 6.1.1 reconciles this file in place\n',
     '    allowed = ADDITIVE | set(RECONCILED_TESTS) | {"tests/test_account_refresh_601.py", "tests/test_forward_validation_61.py", "database/database.py"} | CI_FILES   # Stage 6.1.1 reconciles this file in place; the WAL bootstrap fix\n'),
    ('            if rel in ADDITIVE or rel in NEW_FILES:\n',
     '            if rel in ADDITIVE or rel in NEW_FILES or rel == "database/database.py":   # database.py: the bounded WAL bootstrap, pinned line by line by tests/test_db_bootstrap_busy.py\n'),
]
EDITS_611_FIX = [                                                                                # tests/test_product_ui_611.py (replayed by 6.1.2 F02 too)
    ('           "DESIGN_612_UNIFIED_PRODUCT_FLOW.md", "DESIGN_612_STRATEGY_REGISTRY.md", "DESIGN_612_QUANTCONNECT_ADAPTER.md", "STRATEGY_STANDARD_V1.md"}   # Stage 6.1.2 files (tests/test_product_flow_612.py pins them)\n',
     '           "DESIGN_612_UNIFIED_PRODUCT_FLOW.md", "DESIGN_612_STRATEGY_REGISTRY.md", "DESIGN_612_QUANTCONNECT_ADAPTER.md", "STRATEGY_STANDARD_V1.md"}   # Stage 6.1.2 files (tests/test_product_flow_612.py pins them)\n'
     'from test_db_bootstrap_busy import EDITS_61_FIX, NEW_FIX  # noqa: E402 - the WAL bootstrap fix: its new test file and its in-place edits of the Stage 6.1 test\n'),
    ('                   "browser_tests/flows.py", "README.md", ".github/ci/pytest_groups.json", "api/server.py"}   # Stage 6.1.2: two additive router lines in api/server.py (checked below)\n',
     '                   "browser_tests/flows.py", "README.md", ".github/ci/pytest_groups.json", "api/server.py", "database/database.py"}   # Stage 6.1.2: two additive router lines in api/server.py (checked below); the WAL bootstrap fix\n'),
    ('    assert new == NEW_611 | NEW_612, {"unexpected_new": sorted(new - NEW_611 - NEW_612), "missing_new": sorted((NEW_611 | NEW_612) - new)}   # Stage 6.1.2 files added\n',
     '    assert new == NEW_611 | NEW_612 | NEW_FIX, {"unexpected_new": sorted(new - NEW_611 - NEW_612 - NEW_FIX), "missing_new": sorted((NEW_611 | NEW_612 | NEW_FIX) - new)}   # Stage 6.1.2 files + the WAL bootstrap fix\n'),
    ('    for old, new in EDITS_612_61:                                                                 # Stage 6.1.2: its two in-place edits, applied after the six\n'
     '        assert text.count(old) == 1 and new not in text, old[:70]\n        text = text.replace(old, new)\n',
     '    for old, new in EDITS_612_61:                                                                 # Stage 6.1.2: its two in-place edits, applied after the six\n'
     '        assert text.count(old) == 1 and new not in text, old[:70]\n        text = text.replace(old, new)\n'
     '    for old, new in EDITS_61_FIX:                                                                 # the WAL bootstrap fix: its in-place edits, applied after Stage 6.1.2\'s\n'
     '        assert text.count(old) == 1 and new not in text, old[:70]\n        text = text.replace(old, new)\n'),
]
GROUPS_EDIT_FIX = ('        "test_aapl_smoke_60.py",\n        "test_notifications_44.py",', '        "test_aapl_smoke_60.py",\n        "test_db_bootstrap_busy.py",\n        "test_notifications_44.py",')
EDITS_612_FIX = [                                                                                # tests/test_product_flow_612.py
    ('                   "tests/frontend/product_ui.test.cjs", "browser_tests/flows.py", "README.md", ".github/ci/pytest_groups.json"}\n',
     '                   "tests/frontend/product_ui.test.cjs", "browser_tests/flows.py", "README.md", ".github/ci/pytest_groups.json", "database/database.py"}   # + the WAL bootstrap fix\n'
     'from test_db_bootstrap_busy import EDITS_61_FIX, EDITS_611_FIX, GROUPS_EDIT_FIX, NEW_FIX  # noqa: E402 - the WAL bootstrap fix pins (tests/test_db_bootstrap_busy.py)\n'),
    ('    assert new == NEW_612, {"unexpected_new": sorted(new - NEW_612), "missing_new": sorted(NEW_612 - new)}\n',
     '    assert new == NEW_612 | NEW_FIX, {"unexpected_new": sorted(new - NEW_612 - NEW_FIX), "missing_new": sorted((NEW_612 | NEW_FIX) - new)}   # + the WAL bootstrap fix test\n'),
    ('            assert now_text(rel) == blob(rel), f"{rel} differs from {BASE}"\n',
     '            assert rel == "database/database.py" or now_text(rel) == blob(rel), f"{rel} differs from {BASE}"   # database.py: the WAL bootstrap fix, pinned by tests/test_db_bootstrap_busy.py\n'),
    ('    assert replay(blob("tests/test_product_ui_611.py"), EDITS_611) == now_text("tests/test_product_ui_611.py"), "tests/test_product_ui_611.py: other than the listed in-place edits"\n',
     '    assert replay(blob("tests/test_product_ui_611.py"), EDITS_611 + EDITS_611_FIX) == now_text("tests/test_product_ui_611.py"), "tests/test_product_ui_611.py: other than the listed in-place edits"\n'),
    ('    assert replay(blob("tests/test_forward_validation_61.py"), EDITS_612_61) == now_text("tests/test_forward_validation_61.py"), "tests/test_forward_validation_61.py: other than the two listed edits"\n',
     '    assert replay(blob("tests/test_forward_validation_61.py"), EDITS_612_61 + EDITS_61_FIX) == now_text("tests/test_forward_validation_61.py"), "tests/test_forward_validation_61.py: other than the listed edits"\n'),
    ('    assert blob(".github/ci/pytest_groups.json").count(old) == 1 and blob(".github/ci/pytest_groups.json").replace(old, new) == now_text(".github/ci/pytest_groups.json")\n',
     '    assert blob(".github/ci/pytest_groups.json").count(old) == 1 and blob(".github/ci/pytest_groups.json").replace(old, new).replace(*GROUPS_EDIT_FIX) == now_text(".github/ci/pytest_groups.json")\n'),
]


def blob(rel: str) -> str:
    r = subprocess.run(["git", "show", f"{BASE}:{rel}"], cwd=ROOT, capture_output=True)
    assert r.returncode == 0, f"{rel} missing in {BASE} (fetch-depth 0 needed)"
    return r.stdout.replace(b"\r\n", b"\n").decode("utf-8")


def now_text(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8").replace("\r\n", "\n")


def replay(base: str, edits) -> str:
    text = base
    for old, new in edits:
        assert text.count(old) == 1 and new not in text, old[:80]
        text = text.replace(old, new)
    return text


# ================================================================ C real processes ==============================================================

CHILD = r"""
import sys, time
sys.path.insert(0, sys.argv[1])
import database.database as dbm
start = float(sys.argv[3])
while time.time() < start:
    pass
try:
    dbm.ResearchDatabase(sys.argv[2])
    print("OK")
except Exception as e:
    print("ERR", type(e).__name__, e)
"""


def test_C01_many_processes_bootstrapping_one_new_file_at_the_same_instant_never_crash_and_the_file_ends_in_wal(tmp_path):
    n, rounds, results = 8, 3, []
    for r in range(rounds):
        db = tmp_path / f"round{r}" / "shared.db"
        start = time.time() + 6.0                                                                # every child imports first, then waits for the same instant
        procs = [subprocess.Popen([sys.executable, "-c", CHILD, str(ROOT), str(db), str(start)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True) for _ in range(n)]
        outs = [p.communicate(timeout=180)[0].strip().splitlines() for p in procs]
        results += [o[-1] if o else "NO OUTPUT" for o in outs]
        with sqlite3.connect(str(db)) as conn:
            assert conn.execute("PRAGMA journal_mode;").fetchone()[0].lower() == "wal"           # WAL is preserved
    assert results == ["OK"] * (n * rounds), [x for x in results if x != "OK"]


# ================================================================ U _enable_wal =================================================================

class FakeCursor:
    def __init__(self, v):
        self.v = v

    def fetchone(self):
        return (self.v,)


class FakeConn:
    """Answers PRAGMA journal_mode; with the current mode and fails the switch `fail` times with `msg`."""

    def __init__(self, fail=0, mode="delete", msg="database is locked", read_fail=0):
        self.fail, self.mode, self.msg, self.read_fail, self.calls = fail, mode, msg, read_fail, []

    def execute(self, sql):
        self.calls.append(sql)
        if sql == "PRAGMA journal_mode;":
            if self.read_fail:
                self.read_fail -= 1
                raise sqlite3.OperationalError(self.msg)
            return FakeCursor(self.mode)
        if sql == "PRAGMA journal_mode=WAL;":
            if self.fail:
                self.fail -= 1
                raise sqlite3.OperationalError(self.msg)
            self.mode = "wal"
            return FakeCursor("wal")
        raise AssertionError(sql)


def test_U01_a_busy_switch_is_retried_with_the_fixed_backoff_and_succeeds():
    slept, c = [], FakeConn(fail=3)
    DB._enable_wal(c, sleep=slept.append)
    assert c.mode == "wal" and c.calls.count("PRAGMA journal_mode=WAL;") == 4 and slept == list(DB.WAL_SWITCH_BACKOFF_S[:3])
    slept, c = [], FakeConn(read_fail=2, mode="wal")                                                # a busy read of the mode is retried the same way
    DB._enable_wal(c, sleep=slept.append)
    assert "PRAGMA journal_mode=WAL;" not in c.calls and slept == list(DB.WAL_SWITCH_BACKOFF_S[:2])


def test_U02_bounded_and_deterministic_the_last_busy_attempt_raises_the_original_error():
    slept, c = [], FakeConn(fail=10 ** 6)
    with pytest.raises(sqlite3.OperationalError, match="database is locked"):
        DB._enable_wal(c, sleep=slept.append)
    assert c.calls.count("PRAGMA journal_mode=WAL;") == DB.WAL_SWITCH_ATTEMPTS == 8                  # never an unbounded loop
    assert slept == list(DB.WAL_SWITCH_BACKOFF_S) and len(slept) == DB.WAL_SWITCH_ATTEMPTS - 1 and abs(sum(slept) - 5.15) < 1e-9
    assert DB._is_busy(sqlite3.OperationalError("database is locked")) and DB._is_busy(sqlite3.OperationalError("Database is busy"))


def test_U03_a_non_busy_error_is_never_retried_and_an_existing_wal_file_is_never_switched():
    slept, c = [], FakeConn(fail=1, msg="disk I/O error")
    with pytest.raises(sqlite3.OperationalError, match="disk I/O error"):
        DB._enable_wal(c, sleep=slept.append)
    assert slept == [] and c.calls.count("PRAGMA journal_mode=WAL;") == 1
    c = FakeConn(mode="wal")
    DB._enable_wal(c, sleep=lambda s: (_ for _ in ()).throw(AssertionError("no sleep")))
    assert c.calls == ["PRAGMA journal_mode;"]                                                       # WAL is persistent: no exclusive lock is ever requested again


def test_U04_a_fresh_research_database_is_in_wal_and_every_connection_keeps_the_busy_timeout(tmp_path):
    d = DB.ResearchDatabase(tmp_path / "x.db")
    with d._connect() as conn:
        assert conn.execute("PRAGMA journal_mode;").fetchone()[0].lower() == "wal" and conn.execute("PRAGMA busy_timeout;").fetchone()[0] == 5000
        assert conn.execute("PRAGMA foreign_keys;").fetchone()[0] == 1


# ================================================================ F freeze ======================================================================

def test_F01_only_the_bootstrap_and_its_listed_reconciliations_changed():
    tracked = set(subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard"], cwd=ROOT, capture_output=True, text=True).stdout.split("\n")) - {""}
    in_base = set(subprocess.run(["git", "ls-tree", "-r", "--name-only", BASE], cwd=ROOT, capture_output=True, text=True).stdout.split("\n")) - {""}
    assert in_base, "the base commit must be reachable (fetch-depth 0)"
    new = {p for p in tracked - in_base if not p.startswith(("scratchpad/", "runtime/", "data/", "browser_tests/artifacts/")) and not p.endswith((".pyc", ".db"))}
    changed = set(subprocess.run(["git", "diff", "--name-only", BASE], cwd=ROOT, capture_output=True, text=True).stdout.split("\n")) - {""} - new
    assert new == NEW_FIX, {"unexpected_new": sorted(new - NEW_FIX), "missing_new": sorted(NEW_FIX - new)}
    assert changed == CHANGED_FIX, {"unexpected_changes": sorted(changed - CHANGED_FIX), "missing_changes": sorted(CHANGED_FIX - changed)}
    assert replay(blob("database/database.py"), DB_EDITS) == now_text("database/database.py"), "database/database.py: other than the listed bootstrap edits"
    assert replay(blob("tests/test_forward_validation_61.py"), EDITS_61_FIX) == now_text("tests/test_forward_validation_61.py")
    assert replay(blob("tests/test_product_ui_611.py"), EDITS_611_FIX) == now_text("tests/test_product_ui_611.py")
    assert replay(blob("tests/test_product_flow_612.py"), EDITS_612_FIX) == now_text("tests/test_product_flow_612.py")
    groups = blob(".github/ci/pytest_groups.json")
    assert groups.count(GROUPS_EDIT_FIX[0]) == 1 and groups.replace(*GROUPS_EDIT_FIX) == now_text(".github/ci/pytest_groups.json")
    for rel in ("scripts/run_aapl_paper_smoke.py", "execution/service.py", "execution/store.py", "execution/scheduler.py", "execution/broker.py", "safety/service.py", "config.py"):
        assert now_text(rel) == blob(rel), f"{rel} must not change: the lease, the OS lock and every exactly-once guard stay as they are"
