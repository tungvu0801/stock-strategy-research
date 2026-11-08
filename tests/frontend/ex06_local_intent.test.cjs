// EX-06 (D-7 B): the local-intent block at the top of frontend/app.js, extracted and run in a sandbox. Every same-origin request from the
// dashboard carries X-Stock-Agent-Intent: dashboard (the local request gate requires it on /api); other origins are never touched.
const test = require("node:test");
const assert = require("node:assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const src = fs.readFileSync(path.join(__dirname, "..", "..", "frontend", "app.js"), "utf8");
const a = src.indexOf("// EX-06 local intent: begin");
const b = src.indexOf("// EX-06 local intent: end");

function sandbox(href = "http://127.0.0.1:8000/") {
  const seen = [];
  const win = { location: { href }, fetch: async (input, init) => { seen.push([input, init]); return { ok: true }; } };
  vm.runInNewContext(src.slice(a, b), { window: win, Headers, URL, Request });
  return { win, seen };
}
const intent = (init) => new Headers((init && init.headers) || undefined).get("X-Stock-Agent-Intent");

test("EX-06: the block is the first code in app.js, before the api() helper, and touches no DOM or storage", () => {
  assert.ok(a >= 0 && b > a, "marked block present");
  assert.ok(b < src.indexOf("const REFRESH_MS = 30000;") && src.indexOf("const REFRESH_MS = 30000;") < src.indexOf("async function api("));
  assert.ok(!/\bdocument\b|localStorage|sessionStorage|XMLHttpRequest/.test(src.slice(a, b)));
});

test("EX-06: same-origin GET, HEAD, POST, relative and absolute URLs and Request objects all carry the dashboard intent", async () => {
  const { win, seen } = sandbox();
  await win.fetch("/api/execution/status");
  await win.fetch("/api/execution-safety/pause-new-entries", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
  await win.fetch("http://127.0.0.1:8000/api/x", { method: "HEAD" });
  await win.fetch(new URL("/api/y", "http://127.0.0.1:8000/"));
  await win.fetch(new Request("http://127.0.0.1:8000/api/z", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" }));
  assert.deepEqual(seen.map(([, init]) => intent(init)), ["dashboard", "dashboard", "dashboard", "dashboard", "dashboard"]);
  assert.equal(new Headers(seen[1][1].headers).get("Content-Type"), "application/json");
  assert.equal(new Headers(seen[4][1].headers).get("Content-Type"), "application/json");
  assert.equal(seen[1][1].body, "{}");
});

test("EX-06: an existing intent (Stage 4.6B paper-order) and Headers objects are kept as given", async () => {
  const { win, seen } = sandbox();
  await win.fetch("/api/alpaca-paper-orders/confirm", { method: "POST", headers: { "X-Stock-Agent-Intent": "paper-order", "Content-Type": "application/json" }, body: "{}" });
  await win.fetch("/api/x", { headers: new Headers({ "x-stock-agent-intent": "paper-order", Accept: "text/csv" }) });
  assert.deepEqual(seen.map(([, init]) => intent(init)), ["paper-order", "paper-order"]);
  assert.equal(new Headers(seen[1][1].headers).get("Accept"), "text/csv");
});

test("EX-06: other origins, schemes and ports are never touched (the header never leaves the dashboard)", async () => {
  const { win, seen } = sandbox();
  const init = { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" };
  for (const u of ["https://cdn.example/x.js", "http://127.0.0.1:8031/api/x", "http://localhost:8000/api/x", "https://127.0.0.1:8000/api/x", "data:text/plain,hi"]) {
    await win.fetch(u, init);
  }
  assert.ok(seen.every(([, i]) => i === init), "passed through unchanged (same object)");
});

test("EX-06: wrapping is idempotent and the block runs no request itself", () => {
  const { win, seen } = sandbox();
  const once = win.fetch;
  vm.runInNewContext(src.slice(a, b), { window: win, Headers, URL, Request });
  assert.equal(win.fetch, once);
  assert.equal(seen.length, 0);
});
