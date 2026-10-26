"""
tests/freeze_ledger.py - the Stage 6.8 post-freeze ledger (account position visibility + Bot Activity; DESIGN_68 section 6).

The earlier freeze tests pin execution, Trader View, route and UI files to their own base commits. Stage 6.8 changes some of those files on
purpose. Instead of loosening any freeze test, every such file is registered here with the SHA-256 of the exact text Stage 6.8 leaves, and
the freeze tests read it through `view()`: a registered file whose text matches its hash is seen exactly as it was on main (BASE_68), where
every freeze test passed. Any other change - or any later edit of a registered file without updating its hash - still fails, and
tests/test_bot_activity_68.py::test_F01 proves that the change set since main is exactly NEW_68 + NEW_69 plus this ledger.
Stage 6.9 extends the same ledger (its edits of registered files are new hashes; its new files are NEW_69) - BASE_68 stays the view base.
"""
import hashlib
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE_68 = "b2a20ea0e5418f92f2c943dcc54a7268260f1a9b"                                     # main: Stage 6.7 merged (PR #30)
NEW_68 = {"execution/account_positions.py", "execution/activity.py", "tests/test_bot_activity_68.py", "tests/frontend/bot_activity.test.cjs", "tests/freeze_ledger.py",
          "DESIGN_68_ACCOUNT_POSITIONS_BOT_ACTIVITY.md"}
NEW_69 = {"unified_portfolio/__init__.py", "unified_portfolio/fixture.py", "unified_portfolio/robinhood.py", "unified_portfolio/state.py",
          "api/routes/unified_portfolio.py", "tests/test_robinhood_portfolio_69.py", "tests/frontend/unified_portfolio.test.cjs",
          "DESIGN_69_ROBINHOOD_UNIFIED_PORTFOLIO.md"}                                    # Stage 6.9 (Robinhood read-only portfolio): its new files
LEDGER = {
    ".github/ci/pytest_groups.json": "d238c2c03d0728f7a2f3fae6245a7f48187134ccbb7a9587fe37bee3ab951e9d",
    "api/routes/account_refresh.py": "26f4ebfa3059e2d7f24fe608a8d4f0a6c582daaa237828920e561bbcc4891319",
    "api/routes/execution.py": "9fe4a8b3f88e015b3fa6fcbfcd149a230c504a3d5baaec312a7d5d553d1d6e00",
    "api/server.py": "d09fe59e15c566c715039e91aed42cc19ebc75e6397e976ae3d88f98368de9f5",
    "browser_tests/flows.py": "dd5e3daf0e584fab86df6e658b79d097942f3de9813d7700af80aa9d292d1d0e",
    "config.py": "b2ff670e49cc6b0ba1afa218387d99b74bc762b06472333e1999b54996ebfd4d",
    "execution/models.py": "177a81a89604583cfffc9254da31227c1f35fb98e376dbfdeca9448128c1bfa8",
    "execution/scheduler.py": "6d8459eecb230fa52193c24a6fd0aade17de2959162aefb43010f3da61f57183",
    "execution/service.py": "5c09941615195dba6b50c41976b02b5a9b087c0cd3a0c805445ad2c8b91017e6",
    "frontend/product_ui.css": "ba1285b4c1cb1fcc49380f55cb3d871323a695649c227b797ed4450212de7d5c",
    "frontend/product_ui.js": "725a1d584ede808f6d3df15e732df6622211d42ccd13eab49839c4f6c697ea8f",
    "frontend/trader_view.js": "cc83857e686c92ea04235c86954c96ea505073381d31664af26d1f18c356bebd",
    "tests/frontend/product_flow.test.cjs": "36c3b0c3729d8cab7bb009972dd9209e8ecf269648c3a14980aac3a439139306",
    "tests/frontend/product_ui.test.cjs": "929211ac8de8181d05cac3ab3c3dc8450f55c07c865b07a09f3034eb82433238",
    "tests/frontend/strategy_validation.test.cjs": "f3c9b1b9ff398d0deb83cf2fe90d06278ecd15fb05ef9be9d191502de6c75e59",
    "tests/test_account_refresh_601.py": "42aeb7db6c614d1c0cf41cc7d9c8bf8bbcabebd5301d7dea1fafba4e0eab2587",
    "tests/test_cash_flow_602.py": "15581c69f6fe12e475f7311a03652bb0e128e06eaaa7bc227eab6281eae141ec",
    "tests/test_ci_groups_603.py": "8b1b0a4659424e7fc733a275dae76e3b72ac3f7df156234a8bf8c72f3e57c477",
    "tests/test_db_bootstrap_busy.py": "0253939a872b26f83dd479c31432adc75c5ba7000d3842a84dcc8d3717b38d86",
    "tests/test_demand_57.py": "2b76510307e4973f2c932ca64fb6759990d2e9ebd6328e1a3a6b8f4cb2a16ba0",
    "tests/test_execution_60.py": "578562ba9f96c1fa337669c91269abe011783ff54b9c08f84b20d99688bd24cf",
    "tests/test_execution_safety_591.py": "e5d2a1faefc7be7f04077f744155c2e9dbe82b2b0b35ebe2cff27f26b39f0c89",
    "tests/test_forward_validation_61.py": "d7b5260e7b70bdae9344f5d26212ac0dcf3200651b374ab72e668166a4363fe7",
    "tests/test_product_flow_612.py": "c0fc36938250a543a3072a5138bb365f10cb2b8ca0e0091ab481cbfd7c1dc8ab",
    "tests/test_product_ui_611.py": "e999aa905d267ffdb024a608bc2f87cc10aba679b62c0dbb49e1e955c4fd908b",
    "tests/test_proposals_59.py": "9ac55e1325936c2d084474f6207a4efb0cbc618e98edc9dcbdec626965a081bd",
    "tests/test_regression_isolation.py": "f596017894f68ced030b1d4de720b201150b9637d390c2211347f9bb7cb15b83",
    "tests/test_setups_58.py": "e4d4bbaff2c9c380d117345767119c05197ed93dd8cd647e8003aaae9f3ce307",
    "tests/test_strategy_validation_62.py": "1442dbf624b1806bfcdc45ec93e046ba222af5e9066498aa8b626f750a6605a8",
    "tests/test_trader_view_55.py": "998366a1d1cf3bc939f9ca00bcdaa8f60824be2b3712619b10947578433047e3",
    "tests/test_trader_view_561.py": "c98637d7b4acaf253c26bfee172a0b1d775454ac912c8a2406b33f5f87eae620",
    "trader_view/positions.py": "3765462dea8fcbb52b6c1880338a8e0e1c5151a60091ef0d11c3489a55267558",
    "trader_view/stream.py": "63980bf7566ddbeb210c0a390348fc8672c9a5fdd31893de5803952907f7b8c3",
}


def _norm(text: str) -> str:
    return text.replace("\r\n", "\n")


def sha(text: str) -> str:
    return hashlib.sha256(_norm(text).encode("utf-8")).hexdigest()


def now_text(rel: str) -> str:
    return _norm((ROOT / rel).read_text(encoding="utf-8"))


def base_text(rel: str) -> str:
    out = subprocess.run(["git", "show", f"{BASE_68}:{rel}"], cwd=ROOT, capture_output=True).stdout
    assert out, f"{rel} is not in {BASE_68} (fetch-depth 0 is required)"
    return _norm(out.decode("utf-8"))


def view(rel: str, text: str) -> str:
    """`text` (the current file) as the freeze tests see it: a registered file is main's version, after proving it is exactly the registered one."""
    if rel not in LEDGER:
        return text
    base = base_text(rel)
    if _norm(text) == base:
        return base                                                                      # already read through the ledger (e.g. undo_62 after a shadowed read)
    assert sha(text) == LEDGER[rel], f"{rel} differs from the Stage 6.8 ledger entry (tests/freeze_ledger.py): register the change or revert it"
    return base


def view_bytes(rel: str, data: bytes) -> bytes:
    return data if rel not in LEDGER else view(rel, data.decode("utf-8")).encode("utf-8")


def unchanged(paths, blob_fn) -> set:
    """The registered files among `paths` that, read through the ledger, equal the caller's base (`blob_fn(rel)`, str or bytes): for that freeze
    test they did not change. A registered file that differed from the caller's base on main stays in its change set and is checked as before."""
    out = set()
    for rel in paths:
        if rel in LEDGER:
            b = blob_fn(rel)
            if view(rel, now_text(rel)) == _norm(b.decode("utf-8") if isinstance(b, bytes) else b):
                out.add(rel)
    return out
