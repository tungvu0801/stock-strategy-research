# Vendored: TradingView Lightweight Charts 4.2.3 (Stage 5.5 Trader View)

| | |
|---|---|
| File | `lightweight-charts.standalone.production.js` (163,684 bytes) + `LICENSE` (Apache-2.0) |
| Package | npm `lightweight-charts`, version **4.2.3** (pinned) |
| Source | the npm distributable `dist/lightweight-charts.standalone.production.js` of that version, fetched from the unpkg mirror on 2026-10-20 |
| SHA-256 | `c7dda807d662a95b3d257119ed315cec669e3bdf5aaece75c480a39307f23540` |
| Served at | `/vendor/lightweight-charts/lightweight-charts.standalone.production.js` by the existing StaticFiles mount (`api/server.py`) |

The dashboard has no Node/npm build, so the pinned distributable is vendored instead of installed; `frontend/index.html` loads it
with a relative `<script src>` and there is **no runtime CDN dependency**. `tests/test_trader_view_55.py` pins the hash and the
version banner and fails if index.html ever references an external script host.

To update: download the new pinned version, replace both files, update the hash here and in the test, and record the version in
`trader_view/stream.py` (`CHART_ASSET`).
