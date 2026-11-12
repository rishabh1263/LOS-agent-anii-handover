// server.js behind a SYNTHETIC BACKEND (a tiny Node server answering with the real fixture) -- node --test.
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const http = require("node:http");
const fs = require("node:fs");
const path = require("node:path");

const FIXTURE = fs.readFileSync(path.join(__dirname, "fixtures", "dedupe_existing_complete.json"), "utf8");
const seen = [];
let mode = "ok";

const fakeBackend = http.createServer((req, res) => {
  let body = "";
  req.on("data", (c) => { body += c; });
  req.on("end", () => {
    seen.push({ method: req.method, url: req.url, headers: req.headers, body });
    if (mode === "slow") return;                                         // never answers: the proxy must time out
    if (req.url === "/api/v1/credit/dedupe") {
      res.writeHead(200, { "content-type": "application/json", connection: "keep-alive" });
      return res.end(FIXTURE);
    }
    if (req.url === "/api/v1/fos/applicants") {
      res.writeHead(201, { "content-type": "application/json", "idempotent-replayed": "true" });
      return res.end(JSON.stringify({ case_id: "CASE-SYN0000001" }));
    }
    res.writeHead(200, { "content-type": "application/json" });
    res.end(JSON.stringify({ fields: [] }));
  });
});

let base;
let proxyServer;

test.before(async () => {
  await new Promise((r) => fakeBackend.listen(0, "127.0.0.1", r));
  process.env.BACKEND_URL = `http://127.0.0.1:${fakeBackend.address().port}`;
  process.env.UPSTREAM_TIMEOUT_MS = "300";
  process.env.MAX_BODY_BYTES = "2048";
  process.env.QUIET = "1";
  const { start } = require("../server.js");
  proxyServer = await start(0, "127.0.0.1");
  base = `http://127.0.0.1:${proxyServer.address().port}`;
});

test.after(() => { proxyServer.close(); fakeBackend.close(); });

test("the dedupe API is forwarded with the token and returned unchanged", async () => {
  mode = "ok";
  const res = await fetch(`${base}/api/v1/credit/dedupe`, { method: "POST", headers: { "content-type": "application/json",
    authorization: "Bearer t-1", cookie: "session=secret" }, body: JSON.stringify({ case_id: "C" }) });
  assert.equal(res.status, 200);
  assert.equal((await res.json()).outcome, "REVIEW_REQUIRED");
  const last = seen.at(-1);
  assert.equal(last.headers.authorization, "Bearer t-1");
  assert.equal(last.headers.cookie, undefined);                        // cookies never forwarded
  assert.equal(res.headers.get("x-frame-options"), "DENY");
  assert.match(res.headers.get("content-security-policy"), /default-src 'self'/);
});

test("Idempotency-Key goes through and Idempotent-Replayed comes back", async () => {
  const res = await fetch(`${base}/api/v1/fos/applicants`, { method: "POST", headers: { "content-type": "application/json",
    "idempotency-key": "k-123" }, body: "{}" });
  assert.equal(res.status, 201);
  assert.equal(seen.at(-1).headers["idempotency-key"], "k-123");
  assert.equal(res.headers.get("idempotent-replayed"), "true");
});

test("only the page's routes are proxied; anything else is 404 here", async () => {
  for (const [method, url] of [["GET", "/api/v1/credit/dedupe"], ["POST", "/api/v1/los/process"],
    ["GET", "/api/v1/fos/cases"], ["POST", "/api/v1/credit/dedupe/assess"], ["DELETE", "/api/v1/fos/applicants"]]) {
    const before = seen.length;
    const res = await fetch(`${base}${url}`, { method });
    assert.equal(res.status, 404, `${method} ${url}`);
    assert.equal((await res.json()).detail.code, "ROUTE_NOT_PROXIED");
    assert.equal(seen.length, before, "never reached the backend");
  }
});

test("a body over the limit is refused before the backend", async () => {
  const before = seen.length;
  const res = await fetch(`${base}/api/v1/credit/dedupe`, { method: "POST", headers: { "content-type": "application/json" },
    body: JSON.stringify({ x: "y".repeat(4000) }) });
  assert.equal(res.status, 413);
  assert.equal(seen.length, before);
});

test("a backend that does not answer is a 504 with a code, never a hang", async () => {
  mode = "slow";
  const res = await fetch(`${base}/api/v1/fos/form-schema`);
  assert.equal(res.status, 504);
  assert.equal((await res.json()).detail.code, "UPSTREAM_TIMEOUT");
  mode = "ok";
});

test("a backend that is down is a 502 with a code", async () => {
  const { start } = require("../server.js");
  const saved = process.env.BACKEND_URL;
  // a fresh module instance pointed at a closed port
  delete require.cache[require.resolve("../server.js")];
  process.env.BACKEND_URL = "http://127.0.0.1:1";
  const { start: startDown } = require("../server.js");
  const down = await startDown(0, "127.0.0.1");
  const res = await fetch(`http://127.0.0.1:${down.address().port}/api/v1/fos/config`);
  assert.equal(res.status, 502);
  assert.equal((await res.json()).detail.code, "BACKEND_UNREACHABLE");
  down.close();
  process.env.BACKEND_URL = saved;
  assert.ok(start);
});

test("the page and its assets are served with security headers; no path escapes public/", async () => {
  const page = await fetch(`${base}/`);
  assert.equal(page.status, 200);
  assert.match(await page.text(), /Dedupe Agent/);
  assert.match(page.headers.get("content-security-policy"), /script-src 'self'/);
  for (const asset of ["/core.js", "/app.js", "/styles.css", "/theme.js"]) {
    assert.equal((await fetch(`${base}${asset}`)).status, 200, asset);
  }
  const escape = await fetch(`${base}/..%2f..%2fserver.js`);
  assert.notEqual(escape.status, 200);
  assert.equal((await fetch(`${base}/healthz`)).status, 200);
});

test("an edited file is served fresh: its ETag follows the content, never a stale 304", async () => {
  const file = path.join(__dirname, "..", "public", "etag-probe.txt.js");
  try {
    fs.writeFileSync(file, "// v1\n");
    const first = await fetch(`${base}/etag-probe.txt.js`);
    const tag = first.headers.get("etag");
    assert.equal((await fetch(`${base}/etag-probe.txt.js`, { headers: { "if-none-match": tag } })).status, 304);
    fs.writeFileSync(file, "// v2 -- edited\n");
    const after = await fetch(`${base}/etag-probe.txt.js`, { headers: { "if-none-match": tag } });
    assert.equal(after.status, 200, "the old ETag must not match the edited file");
    assert.match(await after.text(), /v2/);
  } finally {
    fs.rmSync(file, { force: true });
  }
});

test("the server logs no request body", async () => {
  const src = fs.readFileSync(path.join(__dirname, "..", "server.js"), "utf8");
  const logFn = src.slice(src.indexOf("function log("), src.indexOf("function sendJson"));
  assert.ok(!/body/.test(logFn) && !/query|search/.test(logFn.replace("split(\"?\")[0]", "")));
});

test("the reason route: GET only, only a dedupe request id", () => {
  const { allowed } = require("../server.js");
  assert.ok(allowed("GET", "/api/v1/credit/dedupe/ddp_0123456789abcdef0123456789abcdef/reason"));
  assert.ok(!allowed("POST", "/api/v1/credit/dedupe/ddp_0123456789abcdef0123456789abcdef/reason"));
  assert.ok(!allowed("GET", "/api/v1/credit/dedupe/../admin/reason"));
  assert.ok(!allowed("GET", "/api/v1/credit/dedupe/ddp_x/reason"));
});

