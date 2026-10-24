"""
Stage 5.6.1 — TRADER VIEW UI POLISH + DOCUMENTATION CLEANUP (UI only). Focused tests:
  1 render module served and loaded in order · 2 node tests of the exact markup (18 polish items) · 3 no recommendation / probability
  language in the UI · 4 no Market Demand UI · 5 Prepare Buy / Sell are disabled buttons, backend actions unchanged · 6 README cleanup +
  the two tracked caveats · 7 CSS: focus styles, ≥ 11 px text, PPE colours are not the price green / red · 8 harness flow registered
  without moving the pinned flow order · 9 backend, PPE, evidence and prior-stage tests byte-identical to the base commit (UI only)
"""
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

BASE = "24ae7d5"                                                                       # origin/main the stage was branched from (Stage 5.6 merged)
FRONT = ROOT / "frontend"
VIEW = (FRONT / "trader_view.js").read_text(encoding="utf-8")
RENDER = (FRONT / "trader_render.js").read_text(encoding="utf-8")
CSS = (FRONT / "trader_view.css").read_text(encoding="utf-8")
HTML = (FRONT / "index.html").read_text(encoding="utf-8")
README = (ROOT / "README.md").read_text(encoding="utf-8")
DISCLAIMER = "PPE Confidence measures confidence in the overreaction classification, not the probability that the stock will rise."
DEMAND = re.compile(r"(?i)order.?flow|buyer.?pressure|setup readiness")      # Stage 5.7 added the Market Demand section itself; these remain non-features


def code_only(src: str) -> str:
    """JS without // comments (string literals that contain // are not used in these files)."""
    return "\n".join(re.sub(r"(^|\s)//.*$", "", line) for line in src.splitlines())


@pytest.fixture
def api():
    from api.server import app
    return TestClient(app)


# ---------------------------------------------------------------- 1 wiring ------------------------------------------------------

def test_01_render_module_served_and_loaded_between_core_and_view(api):
    html = api.get("/").text
    for asset in ("trader_core.js", "trader_render.js", "trader_view.js", "trader_view.css"):
        assert asset in html and api.get("/" + asset).status_code == 200, asset
    assert html.index("lightweight-charts.standalone.production.js") < html.index("trader_core.js") < html.index("trader_render.js") < html.index("trader_view.js")
    assert "javascript" in api.get("/trader_render.js").headers["content-type"]
    assert "module.exports = api" in RENDER and "root.TraderRender = api" in RENDER                 # same UMD shape as trader_core.js
    assert "const R = window.TraderRender;" in VIEW and "if (!tabEl || !body || !core || !R) return;" in VIEW
    assert not re.search(r"https?://", RENDER) and "fetch(" not in RENDER and "WebSocket" not in RENDER   # pure: no network, no socket


# ---------------------------------------------------------------- 2 node ---------------------------------------------------------

def test_02_node_tests_of_the_rendered_markup():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed; the markup rules are also exercised by the browser harness flow")
    cmd = [node, "--test", "--test-reporter=tap", "--test-reporter-destination=stdout", str(ROOT / "tests" / "frontend" / "trader_render.test.cjs")]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=str(ROOT), timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "# fail 0" in r.stdout, r.stdout
    passed = int(re.search(r"# pass (\d+)", r.stdout).group(1))
    assert passed >= 20, r.stdout
    core = subprocess.run(cmd[:-1] + [str(ROOT / "tests" / "frontend" / "trader_core.test.cjs")], capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=str(ROOT), timeout=120)
    assert core.returncode == 0 and "# pass 6" in core.stdout                                        # the Stage 5.5 node tests still pass as they were


# ---------------------------------------------------------------- 3 / 4 language -------------------------------------------------

def test_03_no_recommendation_or_probability_language_in_the_ui():
    view, render = code_only(VIEW), code_only(RENDER)
    for name, src in (("trader_view.js", view), ("trader_render.js", render), ("trader_view.css", CSS)):
        assert not re.search(r"(?i)recommend", src), name
        assert not re.search(r"(?i)probability|expected.return|price target|target price|win rate|reversal", src), name
        assert not re.search(r"(?i)\b(buy now|sell now|enter now|trade now|you should)\b", src), name
    assert not re.search(r"(?i)\b(buy|sell)\b", render)                                               # the render module names no side at all
    for line in view.splitlines():                                                                     # the view names a side only in the disabled action labels
        if re.search(r"(?i)\b(buy|sell)\b", line):
            assert re.search(r"prepare_buy|prepare_sell|Prepare Buy|Prepare Sell", line), line
    assert DISCLAIMER not in VIEW and DISCLAIMER not in RENDER                                          # the disclaimer always comes from the server result
    assert "${esc(p.disclaimer || \"\")}" in VIEW and 'class="tv-disclaimer"' in RENDER


def test_04_no_order_flow_or_readiness_score_ui():
    """Stage 5.6.1 shipped no Market Demand UI; Stage 5.7 added the Market Demand section (tests/test_demand_57.py). What stays out of the
    UI in both stages: order-flow, buyer-pressure and setup-readiness scores."""
    for name, src in (("trader_view.js", VIEW), ("trader_render.js", RENDER), ("trader_view.css", CSS)):
        assert not DEMAND.search(src), name
    import browser_tests.flows as F
    import inspect
    assert not DEMAND.search(inspect.getsource(F.trader_view))
    assert not re.search(r"(?i)readiness|order.?flow", html_of_trader_tab())


def html_of_trader_tab() -> str:
    start = HTML.index('id="tab-trader"')
    return HTML[start:HTML.index("</section>", start)]


# ---------------------------------------------------------------- 5 actions ------------------------------------------------------

def test_05_prepare_buy_and_sell_are_disabled_buttons_and_backend_actions_unchanged():
    from trader_view.models import default_actions
    acts = {a.id: a for a in default_actions()}
    assert acts["prepare_buy"].enabled is False and acts["prepare_sell"].enabled is False and acts["add_alert"].enabled is False and acts["research"].enabled is True
    assert 'aria-disabled="${a.enabled ? "false" : "true"}"${a.enabled ? "" : " disabled"}' in VIEW
    assert "if (b && !b.disabled) onAction(b.dataset.action);" in VIEW
    assert '{ id: "prepare_buy", label: "Prepare Buy", enabled: false' in VIEW and '{ id: "prepare_sell", label: "Prepare Sell", enabled: false' in VIEW
    assert ".tv-act.tv-disabled, .tv-act:disabled" in CSS
    assert VIEW.count("ws.send(") == 1 and "st.ws.send(JSON.stringify(m))" in VIEW                   # still one socket writer
    assert "dropSocket" in VIEW and "st.ws.close()" in VIEW and "ws.send" not in VIEW.split("dropSocket")[1]   # the test hook only closes, never sends


# ---------------------------------------------------------------- 6 docs ---------------------------------------------------------

def test_06_readme_describes_the_implemented_ppe_and_tracks_the_two_caveats():
    assert "PPE placeholder (NOT AVAILABLE until Stage 5.6)" not in README
    tv = README[README.index("## Trader View (Stage 5.5"):README.index("## PPE engine (Stage 5.6")]
    assert "SIMULATED DATA" in tv and "ALPACA MARKET DATA" in tv and "disabled" in tv and "CONNECTED / RECONNECTING / DISCONNECTED" in tv and "STALE" in tv
    ppe = README[README.index("## PPE engine (Stage 5.6"):]
    ppe = ppe[:ppe.index("\n## ", 1)] if "\n## " in ppe[1:] else ppe
    assert "not live-tested" in ppe and "fake-client tests" in ppe and "credentials" in ppe              # caveat 1: real Alpaca market-data path
    assert "Market Demand" in ppe and "not part of PPE" in ppe and ("future work" in ppe or "separate score" in ppe)   # caveat 2: Market Demand is separate (Stage 5.7 implemented it as its own section)
    assert "could not be replayed" in ppe and "EVIDENCE_NOT_AVAILABLE" in ppe and "descriptive only" in ppe
    flat = " ".join(ppe.replace("*", "").split())                                                      # the README wraps lines
    assert DISCLAIMER in flat
    rest = flat.replace(DISCLAIMER, "")                                                                   # affirmative predictive claims only; "not a probability of recovery" is a negation
    assert not re.search(r"(?i)(?<!not )(?<!never )predicts? (future )?(price|return)s?|(?<!not )(?<!never )will (rise|recover)|(?<!not a )(?<!not the )probability of (a )?(rise|recovery)", rest)
    assert "scratchpad/s561/AUDIT_561.md" in README and (ROOT / "scratchpad" / "s561" / "AUDIT_561.md").exists()


# ---------------------------------------------------------------- 7 css ----------------------------------------------------------

def test_07_css_focus_styles_min_font_and_restrained_ppe_colours():
    assert ".tv :focus-visible" in CSS
    sizes = [int(x) for x in re.findall(r"font-size:\s*(\d+)px", CSS)]
    assert sizes and min(sizes) >= 11, sorted(set(sizes))
    up = re.search(r"\.tv-ppe-val\.up \{([^}]*)\}", CSS).group(1)
    down = re.search(r"\.tv-ppe-val\.down \{([^}]*)\}", CSS).group(1)
    assert "var(--tv-up)" in up and "var(--tv-down)" in down and "--green" not in up and "--red" not in down
    root = re.search(r":root \{([^}]*)\}", CSS).group(1)
    assert re.search(r"--tv-up:\s*#9ecbff", root) and re.search(r"--tv-down:\s*#e8a66b", root)       # blue-ish / orange-ish, not the price palette
    assert ".tv.tv-stale" in CSS and ".tv-skel-bar" in CSS and ".tv-source.sim" in CSS and ".tv-conn.warn .tv-dot" in CSS
    assert re.search(r"\.tv-grid \{[^}]*grid-template-columns: 200px minmax\(0, 1fr\) 3[2-4]0px", CSS)        # 320 px in 5.6.1; 340 px since Stage 5.7 (Market Demand card)
    assert "@media (max-width: 1300px)" in CSS and "@media (max-width: 1100px)" in CSS


# ---------------------------------------------------------------- 8 harness ------------------------------------------------------

def test_08_harness_flow_registered_without_moving_the_pinned_order():
    import browser_tests.flows as F
    names = [f.__name__ for f in F.FLOWS]
    assert "trader_view" in names and names.index("trader_view") == names.index("small_screens") + 1 and names.index("trader_view") < names.index("automation")
    assert names[0] == "dashboard" and names[-8:] == ["automation", "saved_scans", "daily_brief", "brief_delivery", "notification_click", "paper_portfolio", "alpaca_paper", "alpaca_orders"]
    src = (ROOT / "browser_tests" / "flows.py").read_text(encoding="utf-8")
    assert 'c.shot("trader_view", "#tv-body")' in src and 'c.shot("trader_view_ppe", "#tv-ppe")' in src and 'c.shot(f"trader_view_{w}")' in src   # stable screenshot names
    assert "TraderView.dropSocket()" in src and "__tvSwitch(" in src and "TV_SIZES" in src
    server = (ROOT / "browser_tests" / "app_server.py").read_text(encoding="utf-8")
    assert 'os.environ["TRADER_DATA_MODE"] = "simulated"' in server and 'config.TRADER_DATA_MODE = "simulated"' in server   # the harness never reaches a feed


# ---------------------------------------------------------------- 9 UI only ------------------------------------------------------

# Stage 5.6.1 was UI only. Stage 5.7 (Market Demand) legitimately extended trader_core.js, trader_view/{__init__,market_real,models,simulated,stream}.py,
# api/routes/trader_view.py, config.py and the Stage 5.5 / 5.6 test expectations (state key, refresh message, GET route); those files are pinned
# against the Stage 5.7 base in tests/test_demand_57.py instead. Everything below is still byte-identical to 24ae7d5.
UNCHANGED = ["trader_view/adapters.py", "trader_view/hub.py", "trader_view/positions.py", "trader_view/ppe_provider.py", "trader_view/research.py",
             "ppe/__init__.py", "ppe/confidence.py", "ppe/engine.py", "ppe/evidence_context.py", "ppe/features.py", "ppe/inputs.py", "ppe/models.py", "ppe/replay.py",
             "ppe/sectors.py", "ppe/stretch.py", "evidence/engine.py", "evidence/rules.py", "evidence/store.py", "database/migrations.py",
             "paper/alpaca_order_writer.py", "paper/alpaca_orders.py", "paper/alpaca_readonly.py", "portfolio/provider.py", "DESIGN_56_PPE_ENGINE.md",
             "tests/test_evidence_54.py", "tests/test_browser_harness_39.py", "tests/frontend/trader_core.test.cjs",
             "scratchpad/s56/PPE_VALIDATION_56.md"]


def test_09_backend_ppe_evidence_and_prior_tests_identical_to_base():
    def blob(rel):
        out = subprocess.run(["git", "show", f"{BASE}:{rel}"], cwd=ROOT, capture_output=True).stdout
        assert out, rel
        return out.replace(b"\r\n", b"\n")
    for rel in UNCHANGED:
        assert (ROOT / rel).read_bytes().replace(b"\r\n", b"\n") == blob(rel), f"{rel} differs from {BASE} (Stage 5.6.1 is UI only)"
    base_html = blob("frontend/index.html").decode("utf-8").splitlines()
    now_html = HTML.splitlines()
    # >>> Stage 6.1: index.html carries two marked, additive Stage 6.1 blocks (the Forward Paper Validation stylesheet and script); they are stripped before
    # the Stage 5.6.1 line rule below, so the allowed added lines stay exactly the Stage 5.8 / 5.9 / 5.9.1 / 6.0 ones
    import re as _re61
    now_html = _re61.sub(r"(?ms)^<!-- >>> Stage 6\.1\b.*?^<!-- <<< Stage 6\.1 -->\n", "", "\n".join(now_html) + "\n").splitlines()
    # <<< Stage 6.1
    # >>> Stage 6.1.1: index.html also carries two marked, additive Product UI 6.1.1 blocks (the product shell stylesheet and script, pinned against the
    # Stage 6.1.1 base by tests/test_product_ui_611.py); they are stripped as well, so the line rule below still sees exactly the Stage 5.8 - 6.0 lines
    now_html = _re61.sub(r"(?ms)^<!-- >>> Product UI 6\.1\.1\b.*?^<!-- <<< Product UI 6\.1\.1 -->\n", "", "\n".join(now_html) + "\n").splitlines()
    # <<< Stage 6.1.1
    assert [l for l in base_html if l not in now_html] == []
    assert [l for l in now_html if l not in base_html] == ['    <div id="setup-alerts"></div>', '<script src="trader_render.js"></script>', '<script src="setup_alerts.js"></script>', '<script src="trade_review.js"></script>', '<script src="risk_controls.js"></script>', '<script src="paper_bot.js"></script>']   # + Stage 5.8's two additive lines + Stage 5.9's, 5.9.1's and 6.0's scripts
