"""
Stage 5.4.1 - the setup evidence check reads the same packet as PPE Confidence (DESIGN_58 section 5: READY_BLOCKED_NO_EVIDENCE means "no
COMPLETED Stage 5.4 packet"). Before: the setup read the newest packet of ANY status (trader_view/research.py), PPE Confidence the newest
COMPLETED one (ppe/evidence_context.py), so a newer FAILED build hid a valid packet from the evidence gate only. Packets are built here with
the Stage 5.4 test fakes (no socket: the FakeWire) into a temporary store.
  E01-E05 the fix · F01-F02 its scope (one function changed; READY thresholds, PPE scoring, the setup engine and the frozen hashes unchanged)
"""
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import config  # noqa: E402
import evidence_fakes as F  # noqa: E402
import freeze_ledger as FL  # noqa: E402
import setups as S  # noqa: E402
from evidence import engine as EN  # noqa: E402
from evidence import net  # noqa: E402
from evidence.store import EvidenceStore  # noqa: E402
from ppe.evidence_context import latest_snapshot  # noqa: E402
from setups.engine import evidence_gate  # noqa: E402
from test_setups_58 import make_stream  # noqa: E402
from trader_view.research import EvidenceResearchAdapter, pick  # noqa: E402

AFTER = F.NOW + timedelta(hours=3)                                                   # the reading time: after every packet built below
FAILED_BARS = {"bars": lambda symbols, lookback_days: {}}                            # no daily bars: the build FAILS with NO_MARKET_DATA


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "EVIDENCE_NETWORK_ENABLED", True)
    monkeypatch.setattr(net, "WIRE", F.FakeWire())
    monkeypatch.setattr(config, "SEC_USER_AGENT", F.SEC_UA)
    monkeypatch.setattr(config, "EVIDENCE_IR_DOMAINS", F.IR_DOMAINS)
    return EvidenceStore(tmp_path / "ev.db")


def build(store, at, ok=True, **over):
    p = EN.build_packet(F.request(**over), adapters=F.adapters(**({} if ok else FAILED_BARS)), now_fn=lambda: at, store=store)["packet"]
    assert p["status"] == ("COMPLETED" if ok else "FAILED"), p["failure_code"]
    return p


def reader(store, now=AFTER):
    return EvidenceResearchAdapter(store_factory=lambda: store, now_fn=lambda: now)


def setup_inputs(tmp_path, store, now=AFTER):
    """The scheduled path: SetupService.inputs_for reads the stream's research.latest (no prefetched evidence)."""
    _, svc, _ = make_stream(tmp_path, research=reader(store, now))
    return svc.inputs_for("SNDK", now)


def ppe_packet(store, now=AFTER):
    snap = latest_snapshot("SNDK", now, store)                                       # PPE Confidence's own reader, unchanged
    return snap.packet_id if snap else None


# ---------------------------------------------------------------- E the fix ------------------------------------------------------
def test_E01_a_newer_failed_build_never_hides_the_valid_completed_packet_from_the_setup(tmp_path, store):
    ok = build(store, F.NOW)
    bad = build(store, F.NOW + timedelta(hours=2), ok=False)
    assert [p["packet_id"] for p in store.packets(ticker="SNDK")] == [bad["packet_id"], ok["packet_id"]]      # the FAILED build is the newest row
    e = reader(store).latest("SNDK")
    assert e.status == "LOADED" and e.packet_status == "COMPLETED" and e.packet_id == ok["packet_id"] and e.created_at == ok["created_at"]
    assert "FAILED" in e.message and "NO_MARKET_DATA" in e.message                     # the failed rebuild stays visible, as a note
    i = setup_inputs(tmp_path, store)
    assert i.evidence_available is True and i.evidence_packet_id == ok["packet_id"] and i.evidence_created_at == ok["created_at"]
    assert i.evidence_strength == ok["strength"] and i.evidence_cause == ok["cause_json"]["verdict"]
    assert evidence_gate(i) != S.R_BLOCK_NO_EVIDENCE                                   # before the fix: READY_BLOCKED_NO_EVIDENCE


def test_E02_ppe_confidence_and_the_setup_resolve_the_same_packet(tmp_path, store):
    first = build(store, F.NOW)
    second = build(store, F.NOW + timedelta(hours=1), lookback_days=60)
    build(store, F.NOW + timedelta(hours=2), ok=False)
    assert ppe_packet(store) == reader(store).latest("SNDK").packet_id == second["packet_id"] == setup_inputs(tmp_path, store).evidence_packet_id
    # read at a time between the two: both see only the first (a later packet is invisible to both)
    mid = F.NOW + timedelta(minutes=30)
    assert ppe_packet(store, mid) == reader(store, mid).latest("SNDK").packet_id == first["packet_id"]
    # two COMPLETED packets created in the same second: both take the higher packet_id
    twin = build(store, F.NOW + timedelta(hours=1), lookback_days=45)
    rows = store.packets(ticker="SNDK")
    assert pick(rows, AFTER)["packet_id"] == ppe_packet(store) == max(second["packet_id"], twin["packet_id"])
    # a packet stamped after the reading time is skipped by both
    build(store, AFTER + timedelta(hours=5), lookback_days=30)
    assert reader(store).latest("SNDK").packet_id == ppe_packet(store) == max(second["packet_id"], twin["packet_id"])
    assert first["packet_id"] != second["packet_id"]


def test_E03_no_completed_packet_fails_safely(tmp_path, store):
    # no packet at all: not loaded, no evidence, READY blocked
    assert reader(store).latest("SNDK").status == "NOT_LOADED" and ppe_packet(store) is None
    i = setup_inputs(tmp_path, store)
    assert i.evidence_available is False and i.evidence_packet_id is None and evidence_gate(i) == S.R_BLOCK_NO_EVIDENCE
    # only FAILED builds: the newest FAILED one is shown, read as no evidence (as before the fix)
    build(store, F.NOW, ok=False)
    bad = build(store, F.NOW + timedelta(hours=1), ok=False)
    e = reader(store).latest("SNDK")
    assert e.status == "LOADED" and e.packet_status == "FAILED" and e.packet_id == bad["packet_id"] and e.message.startswith("Stored packet FAILED")
    i = setup_inputs(tmp_path, store)
    assert i.evidence_available is False and i.evidence_packet_id is None and evidence_gate(i) == S.R_BLOCK_NO_EVIDENCE and ppe_packet(store) is None
    # a COMPLETED packet stamped after the reading time is not evidence yet, for either reader
    build(store, AFTER + timedelta(hours=1))
    i = setup_inputs(tmp_path, store)
    assert i.evidence_available is False and evidence_gate(i) == S.R_BLOCK_NO_EVIDENCE and ppe_packet(store) is None
    # an unreadable store is reported, never read as evidence
    boom = EvidenceResearchAdapter(store_factory=lambda: (_ for _ in ()).throw(OSError("disk"))).latest("SNDK")
    assert boom.status == "FAILED" and "OSError" in boom.message


@pytest.mark.parametrize("age_days, stale", [(30, False), (31, True)])
def test_E04_stale_evidence_behaviour_is_unchanged(tmp_path, store, age_days, stale):
    ok = build(store, F.NOW)
    build(store, F.NOW + timedelta(hours=1), ok=False)                                 # a newer failed rebuild does not change the age rule
    now = F.NOW + timedelta(days=age_days)
    i = setup_inputs(tmp_path, store, now)
    assert i.evidence_packet_id == ok["packet_id"] and i.evidence_age_days == age_days and S.EVIDENCE_MAX_AGE_DAYS == 30
    gate = evidence_gate(i)                                                            # at 30 days the packet's own content decides (the fixture's
    assert (gate == S.R_BLOCK_EVIDENCE_STALE) is stale and gate != S.R_BLOCK_NO_EVIDENCE   # company-specific packet: READY_BLOCKED_BY_EVIDENCE)
    snap = latest_snapshot("SNDK", now, store)
    assert snap.packet_id == ok["packet_id"] and snap.age_days == age_days               # PPE reads the same packet at the same age


def test_E05_the_display_keeps_its_fields_and_the_stream_still_reads_on_subscribe_and_refresh_only(store):
    ok = build(store, F.NOW)
    e = reader(store).latest("sndk")
    assert e.symbol == "SNDK" and e.packet_status == "COMPLETED" and e.message is None and e.data_status == "STORED"
    assert [r["rung"] for r in e.rungs] == [1, 2, 3, 4, 5, 6] and e.n_items == ok["n_items"] and e.as_of_date == F.AS_OF.isoformat()
    assert EvidenceResearchAdapter(store_factory=lambda: store).latest("SNDK").packet_id == ok["packet_id"]   # the default clock: now


# ---------------------------------------------------------------- F scope -------------------------------------------------------
def _git(*a):
    return subprocess.run(["git", *a], cwd=ROOT, capture_output=True, text=True).stdout


def at_base(rel):
    out = subprocess.run(["git", "show", f"{FL.BASE_541}:{rel}"], cwd=ROOT, capture_output=True).stdout
    assert out, f"{rel} is not in {FL.BASE_541} (fetch-depth 0 is required)"
    return out.decode("utf-8").replace("\r\n", "\n")


def post(rel):
    """main after PR #36 (Stage 5.4.1 merged): what Stage 5.4.1 left, whatever a later fix changed since."""
    out = subprocess.run(["git", "show", f"{FL.BASE_611FIX}:{rel}"], cwd=ROOT, capture_output=True).stdout
    assert out, f"{rel} is not in {FL.BASE_611FIX} (fetch-depth 0 is required)"
    return out.decode("utf-8").replace("\r\n", "\n")


# every Stage 5.4.1 edit of a file no ledger entry can hold (an earlier stage's own test file): (text on main, text now), replayed by F03
REPLAY_541 = {
    'tests/test_bot_activity_68.py': [
        ('    assert new == FL.NEW_68 | FL.NEW_69 | FL.NEW_691 | FL.NEW_610A | FL.NEW_610C, {"unexpected_new": sorted(new - FL.NEW_68 - FL.NEW_69 - FL.NEW_691 - FL.NEW_610A - FL.NEW_610C),\n                                                                     "missing_new": sorted((FL.NEW_68 | FL.NEW_69 | FL.NEW_691 | FL.NEW_610A | FL.NEW_610C) - new)}   # + Stage 6.9 / 6.9.1 / 6.10A / 6.10C\n',
         '    assert new == FL.NEW_68 | FL.NEW_69 | FL.NEW_691 | FL.NEW_610A | FL.NEW_610C | FL.NEW_541, {"unexpected_new": sorted(new - FL.NEW_68 - FL.NEW_69 - FL.NEW_691 - FL.NEW_610A - FL.NEW_610C - FL.NEW_541),\n                                                                     "missing_new": sorted((FL.NEW_68 | FL.NEW_69 | FL.NEW_691 | FL.NEW_610A | FL.NEW_610C | FL.NEW_541) - new)}   # + Stage 6.9 / 6.9.1 / 6.10A / 6.10C / 5.4.1\n'),
    ],
    'tests/test_decision_flow_610c.py': [
        ('\n\ndef test_F01_stage_610c_changes_exactly',
         '\n\ndef post(rel):\n    """main after PR #35 (Stage 6.10C merged): what Stage 6.10C left, whatever a later stage changed since."""\n    out = subprocess.run(["git", "show", f"{FL.BASE_541}:{rel}"], cwd=ROOT, capture_output=True).stdout\n    assert out, f"{rel} is not in {FL.BASE_541} (fetch-depth 0 is required)"\n    return out.decode("utf-8").replace("\\r\\n", "\\n")\n\n\ndef test_F01_stage_610c_changes_exactly'),
        ('    changed = set(_git("diff", "--name-only", FL.BASE_610C).split()) | set(_git("ls-files", "--others", "--exclude-standard").split())\n    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    in_base = set(_git("ls-tree", "-r", "--name-only", FL.BASE_610C).split())\n    new, edited = {p for p in changed if p not in in_base}, {p for p in changed if p in in_base}\n',
         '    stage610c = set(_git("diff", "--name-only", FL.BASE_610C, FL.BASE_541).split())          # Stage 6.10C itself, between two fixed commits (main before / after PR #35)\n    in_base = set(_git("ls-tree", "-r", "--name-only", FL.BASE_610C).split())\n    new, edited = {p for p in stage610c if p not in in_base}, {p for p in stage610c if p in in_base}\n'),
        ('    for d in STAGE_DIRS_NOT_TOUCHED:\n        assert not [p for p in changed if p.startswith(d + "/")], d                          # no execution, strategy, risk, broker, market-data or policy file\n    assert not changed & {"frontend/index.html",',
         '    changed = set(_git("diff", "--name-only", FL.BASE_610C).split()) | set(_git("ls-files", "--others", "--exclude-standard").split())\n    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    later = FL.NEW_541 | FL.EDITED_541                                                         # Stage 5.4.1, pinned by its own F tests\n    assert changed == FL.NEW_610C | FL.EDITED_610C | later, {"unexpected": sorted(changed - FL.NEW_610C - FL.EDITED_610C - later),\n                                                              "missing": sorted((FL.NEW_610C | FL.EDITED_610C | later) - changed)}\n    for d in STAGE_DIRS_NOT_TOUCHED:\n        assert not [p for p in stage610c if p.startswith(d + "/")], d                        # no execution, strategy, risk, broker, market-data or policy file\n        assert not [p for p in changed - later if p.startswith(d + "/")], d                  # ... and none since but Stage 5.4.1\'s declared files\n    assert not stage610c & {"frontend/index.html",'),
        ('            assert now_text(rel) == at_base(rel), rel                                   # the executor, the strategy, startup demotion and MANUAL_ARM: untouched\n',
         "            assert post(rel) == at_base(rel), rel                                       # the executor, the strategy, startup demotion and MANUAL_ARM: untouched\n            assert now_text(rel) == post(rel) or rel in FL.SEMANTIC_541, rel               # since then: only Stage 5.4.1's declared correction\n"),
        ('    for rel, edits in REPLAY_610C.items():\n        text = now_text(rel)\n',
         '    for rel, edits in REPLAY_610C.items():\n        text = post(rel)\n'),
        ('    fl, base = now_text("tests/freeze_ledger.py"), at_base("tests/freeze_ledger.py")\n',
         '    fl, base = post("tests/freeze_ledger.py"), at_base("tests/freeze_ledger.py")\n'),
        ('    assert set(FL.LEDGER) == set(old)                                                     # no new key: every edited registered file was already registered\n    assert {k for k in old if old[k] != FL.LEDGER[k]} == (FL.EDITED_610C & set(old))      # every re-hash is a declared Stage 6.10C edit\n',
         '    mine = ast.literal_eval(led(fl)[len("LEDGER = "):])                                   # the ledger Stage 6.10C left (main after PR #35)\n    assert set(mine) == set(old)                                                          # no new key: every edited registered file was already registered\n    assert {k for k in old if old[k] != mine[k]} == (FL.EDITED_610C & set(old))           # every re-hash is a declared Stage 6.10C edit\n'),
    ],
    'tests/test_forward_paper_610a.py': [
        ('    later = FL.NEW_610C | FL.EDITED_610C                                                       # Stage 6.10C, pinned by its own F tests\n',
         '    later = FL.NEW_610C | FL.EDITED_610C | FL.NEW_541 | FL.EDITED_541                         # Stage 6.10C / 5.4.1, each pinned by its own F tests\n'),
        ('        assert not [p for p in changed - later if p.startswith(d + "/")], d                  # ... and none since but Stage 6.10C\'s declared files\n',
         '        assert not [p for p in changed - later if p.startswith(d + "/")], d                  # ... and none since but Stage 6.10C\'s / 5.4.1\'s declared files\n'),
    ],
    'tests/test_robinhood_portfolio_69.py': [
        ('    later = FL.NEW_691 | FL.EDITED_691 | FL.NEW_610A | FL.EDITED_610A | FL.NEW_610C | FL.EDITED_610C   # Stage 6.9.1 (FL.SEMANTIC_691), Stage 6.10A (FL.SEMANTIC_610A), Stage 6.10C,\n',
         '    later = FL.NEW_691 | FL.EDITED_691 | FL.NEW_610A | FL.EDITED_610A | FL.NEW_610C | FL.EDITED_610C | FL.NEW_541 | FL.EDITED_541   # Stage 6.9.1 (FL.SEMANTIC_691), Stage 6.10A (FL.SEMANTIC_610A), Stage 6.10C, Stage 5.4.1 (FL.SEMANTIC_541),\n'),
    ],
    'tests/test_unified_exposure_gate_691.py': [
        ('    later = FL.NEW_610A | FL.EDITED_610A | FL.NEW_610C | FL.EDITED_610C                       # Stage 6.10A / 6.10C, each pinned by its own F tests\n',
         '    later = FL.NEW_610A | FL.EDITED_610A | FL.NEW_610C | FL.EDITED_610C | FL.NEW_541 | FL.EDITED_541   # Stage 6.10A / 6.10C / 5.4.1, each pinned by its own F tests\n'),
    ],
}


def test_F01_the_fix_changes_one_function_and_nothing_in_the_setup_engine_ppe_evidence_or_execution():
    assert FL.BASE_541 == "a7dc4456b407458c9751ea88398cee37f3084d2f" and subprocess.run(["git", "cat-file", "-e", f"{FL.BASE_541}^{{commit}}"], cwd=ROOT).returncode == 0
    stage541 = set(_git("diff", "--name-only", FL.BASE_541, FL.BASE_611FIX).split())        # Stage 5.4.1 itself, between two fixed commits (main before / after PR #36)
    assert stage541 == FL.NEW_541 | FL.EDITED_541, {"unexpected": sorted(stage541 - FL.NEW_541 - FL.EDITED_541), "missing": sorted(FL.NEW_541 | FL.EDITED_541 - stage541)}
    changed = set(_git("diff", "--name-only", FL.BASE_541).split()) | set(_git("ls-files", "--others", "--exclude-standard").split())
    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}
    later = FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70                                                  # the Stage 6.1.1 CSS fix, pinned by its own F tests
    assert changed == FL.NEW_541 | FL.EDITED_541 | later, {"unexpected": sorted(changed - FL.NEW_541 - FL.EDITED_541 - later), "missing": sorted((FL.NEW_541 | FL.EDITED_541 | later) - changed)}
    assert set(FL.SEMANTIC_541) == {"trader_view/research.py"}
    for d in ("setups", "ppe", "evidence", "execution", "proposals", "safety", "autorun", "decision_flow", "dynamic_universe", "unified_portfolio"):
        for p in sorted((ROOT / d).glob("*.py")):
            rel = p.relative_to(ROOT).as_posix()
            assert post(rel) == at_base(rel), rel
            assert p.read_text(encoding="utf-8").replace("\r\n", "\n") == post(rel), rel      # since then: Stage 6.10D touches none of these
    for rel in sorted(p.relative_to(ROOT).as_posix() for p in (ROOT / "trader_view").glob("*.py")):
        if rel != "trader_view/research.py":
            assert post(rel) == at_base(rel), rel
            assert (ROOT / rel).read_text(encoding="utf-8").replace("\r\n", "\n") == post(rel), rel


def test_F03_every_bookkeeping_edit_is_pinned_exactly_and_the_ledger_changes_only_the_declared_entries():
    import ast
    for rel, edits in REPLAY_541.items():
        text = post(rel)
        for old, new in edits:
            assert text.count(new) == 1, (rel, new[:100])
            text = text.replace(new, old)
        assert text == at_base(rel), rel
    assert set(REPLAY_541) == FL.EDITED_541 - set(FL.LEDGER) - {"tests/freeze_ledger.py"}   # every other edited file carries a ledger entry
    fl, base = post("tests/freeze_ledger.py"), at_base("tests/freeze_ledger.py")
    para = fl[fl.index("\nStage 5.4.1 (evidence gate"):fl.index('"""', fl.index("\nStage 5.4.1 (evidence gate"))]
    rebuilt = fl.replace(para, "", 1).replace(" + NEW_610C + NEW_541 plus this ledger.", " + NEW_610C plus this ledger.", 1)
    decl = rebuilt[rebuilt.index("BASE_541 = "):rebuilt.index("LEDGER = {")]
    rebuilt = rebuilt.replace(decl, "", 1)
    led = lambda t: t[t.index("LEDGER = {"):t.index("}\n", t.index("LEDGER = {")) + 2]  # noqa: E731
    rebuilt = rebuilt.replace(led(rebuilt), led(base), 1)
    assert rebuilt == base                                                                # the declarations, the paragraph, one line and the ledger - nothing else
    old = ast.literal_eval(led(base)[len("LEDGER = "):])
    mine = ast.literal_eval(led(fl)[len("LEDGER = "):])                                   # the ledger Stage 5.4.1 left (main after PR #36)
    assert set(mine) - set(old) == {"trader_view/research.py"}                            # one new key: the corrected reader
    assert {k for k in old if old[k] != mine[k]} == FL.EDITED_541 & set(old)               # every re-hash is a declared Stage 5.4.1 edit
    for rel in FL.LEDGER:
        assert FL.view(rel, FL.now_text(rel)) == FL.base_text(rel), rel


def test_F02_ready_thresholds_ppe_scoring_and_the_frozen_hashes_are_unchanged():
    out = subprocess.run([sys.executable, "-c", "import config, forward_validation as FV; from execution import exposure_gate as G; "
                          "print(FV.frozen_hash()); print(G.contract_hash(G.from_config(config))); print(G.contract_hash(G.GateSettings(enabled=True)))"],
                         cwd=ROOT, capture_output=True, text=True)
    assert out.stdout.split() == ["b5d4a56ef5bba61f8250b3cd25182cc1655738adf0605a77671fd029883f8530",
                                  "2552bd6639f691b828624250665519cc90c4a00da7f70893dbb635f7551006fa",
                                  "d2819342f6c8ccb1cd4a0ce2bfb5d1b0664f2453770c3e6736f7be420a17aed4"], out.stderr[-600:]
    import ppe as P
    assert (S.WATCH_ENTER_STRETCH, S.READY_CONFIDENCE, S.READY_DEMAND, S.READY_DEMAND_COVERAGE, S.READY_PPE_COVERAGE, S.EVIDENCE_MAX_AGE_DAYS) == (-40.0, 40, 60, 0.60, 0.60, 30)
    assert (P.NORMAL_BAND, P.CONFIDENCE_BASE, P.CAPS["WEAK"], P.CAPS["MODERATE"]) == (40, 50, 50, 84)
