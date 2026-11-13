// Credit document readiness dashboard -- a zero-dependency static server + API proxy (Node 18+).
//   node server.js                 -> http://localhost:5181
//   BACKEND_URL=http://127.0.0.1:8010 PORT=5181 node server.js
// Only the document readiness API (/api/v1/credit/document-readiness/*) is forwarded to the LOS backend, so the browser talks to ONE
// origin (no CORS change) and this page can never reach any other backend route.
//
// PRODUCTION (2026-10-15): security headers + CSP, gzip, cached static files with ETag, a request-body limit, upstream
// timeouts, hop-by-hop headers stripped, X-Forwarded-*, /healthz, graceful shutdown, one access-log line per request.
// Auth: the browser's Authorization header is passed through; BACKEND_TOKEN (optional) is used when the browser sends
// none -- set it only when this server itself sits behind the organisation's SSO.
"use strict";

const http = require("http");
const https = require("https");
const fs = require("fs");
const path = require("path");
const zlib = require("zlib");
const crypto = require("crypto");

const PORT = Number(process.env.PORT || 5181);
const HOST = process.env.HOST || "0.0.0.0";
const BACKEND = new URL(process.env.BACKEND_URL || "http://127.0.0.1:8010");
const BACKEND_TOKEN = process.env.BACKEND_TOKEN || "";
const API_PREFIX = "/api/v1/credit/document-readiness/";
const MAX_BODY = Number(process.env.MAX_BODY_BYTES || 64 * 1024);             // 64 KB: the requests are tiny
const UPSTREAM_TIMEOUT_MS = Number(process.env.UPSTREAM_TIMEOUT_MS || 30000);  // the answer never waits for the AI
const STATIC_MAX_AGE = Number(process.env.STATIC_MAX_AGE || 0);              // seconds; 0: always revalidate (ETag)
const ROOT = path.join(__dirname, "public");
const client = BACKEND.protocol === "https:" ? https : http;

const TYPES = {
  ".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8", ".js": "text/javascript; charset=utf-8",
  ".svg": "image/svg+xml", ".png": "image/png", ".ico": "image/x-icon", ".json": "application/json",
};
const COMPRESSIBLE = new Set([".html", ".css", ".js", ".svg", ".json"]);
const HOP_BY_HOP = ["connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailer",
  "transfer-encoding", "upgrade"];
const SECURITY = {
  "content-security-policy": [
    "default-src 'self'", "script-src 'self'", "style-src 'self' https://fonts.googleapis.com",
    "font-src https://fonts.gstatic.com", "img-src 'self' data:", "connect-src 'self'",
    "frame-ancestors 'none'", "base-uri 'none'", "form-action 'self'",
  ].join("; "),
  "x-content-type-options": "nosniff",
  "x-frame-options": "DENY",
  "referrer-policy": "no-referrer",
  "permissions-policy": "camera=(), microphone=(), geolocation=()",
  "cross-origin-opener-policy": "same-origin",
};

function log(req, status, started) {
  const ms = Date.now() - started;
  console.log(`${new Date().toISOString()} ${req.method} ${(req.url || "").split("?")[0]} ${status} ${ms}ms`);
}

function sendJson(res, status, body, extra = {}) {
  res.writeHead(status, { "content-type": "application/json", ...SECURITY, ...extra });
  res.end(JSON.stringify(body));
}

// ---------------------------------------------------------------- API proxy (document readiness routes only)
function proxy(req, res, started) {
  const headers = { ...req.headers, host: BACKEND.host };
  HOP_BY_HOP.forEach((h) => delete headers[h]);
  delete headers.cookie;                                                  // the backend uses bearer tokens, not cookies
  if (!headers.authorization && BACKEND_TOKEN) headers.authorization = `Bearer ${BACKEND_TOKEN}`;
  headers["x-forwarded-for"] = [req.headers["x-forwarded-for"], req.socket.remoteAddress].filter(Boolean).join(", ");
  headers["x-forwarded-proto"] = req.headers["x-forwarded-proto"] || "http";
  const length = Number(req.headers["content-length"] || 0);
  if (length > MAX_BODY) { log(req, 413, started); return sendJson(res, 413, { detail: { error: "PAYLOAD_TOO_LARGE" } }); }

  const upstream = client.request({
    protocol: BACKEND.protocol, hostname: BACKEND.hostname, port: BACKEND.port || (BACKEND.protocol === "https:" ? 443 : 80),
    path: req.url, method: req.method, headers,
  }, (up) => {
    const out = { ...up.headers, ...SECURITY, "cache-control": "no-store" };
    HOP_BY_HOP.forEach((h) => delete out[h]);
    res.writeHead(up.statusCode || 502, out);
    up.pipe(res);
    up.on("end", () => log(req, up.statusCode, started));
  });
  upstream.setTimeout(UPSTREAM_TIMEOUT_MS, () => upstream.destroy(new Error("timeout")));
  upstream.on("error", (err) => {
    log(req, 502, started);
    if (res.headersSent) return res.destroy();
    const timeout = err && err.message === "timeout";
    sendJson(res, timeout ? 504 : 502, { detail: {
      error: timeout ? "BACKEND_TIMEOUT" : "BACKEND_UNREACHABLE",
      message: timeout ? "The LOS backend took too long to answer. Retry in a moment."
        : "The LOS backend is not reachable. Retry in a moment; if it persists, contact support." } });
  });
  let seen = 0;
  req.on("data", (chunk) => {
    seen += chunk.length;
    if (seen > MAX_BODY) { upstream.destroy(); if (!res.headersSent) sendJson(res, 413, { detail: { error: "PAYLOAD_TOO_LARGE" } }); req.destroy(); }
  });
  req.pipe(upstream);
}

// ---------------------------------------------------------------- static files (cached in memory, gzip, ETag)
const cache = new Map();                                                  // file -> {mtimeMs, body, gz, etag}
function load(file, cb) {
  fs.stat(file, (err, st) => {
    if (err || !st.isFile()) return cb(err || new Error("not a file"));
    const hit = cache.get(file);
    if (hit && hit.mtimeMs === st.mtimeMs) return cb(null, hit);
    fs.readFile(file, (e, body) => {
      if (e) return cb(e);
      const entry = { mtimeMs: st.mtimeMs, body, etag: `"${crypto.createHash("sha1").update(body).digest("base64url").slice(0, 16)}"`,
        gz: COMPRESSIBLE.has(path.extname(file)) ? zlib.gzipSync(body) : null };
      cache.set(file, entry);
      cb(null, entry);
    });
  });
}

function serve(req, res, started) {
  if (req.method !== "GET" && req.method !== "HEAD") {
    log(req, 405, started); return sendJson(res, 405, { detail: { error: "METHOD_NOT_ALLOWED" } }, { allow: "GET, HEAD" });
  }
  let clean;
  try { clean = decodeURIComponent((req.url || "/").split("?")[0]); } catch { log(req, 400, started); return sendJson(res, 400, { detail: { error: "BAD_PATH" } }); }
  let file = path.normalize(path.join(ROOT, clean === "/" ? "index.html" : clean));
  if (!file.startsWith(ROOT + path.sep) && file !== ROOT) { log(req, 403, started); res.writeHead(403, SECURITY); return res.end(); }
  const isPage = !path.extname(file);                                     // /anything without an extension -> the page
  if (isPage) file = path.join(ROOT, "index.html");
  load(file, (err, entry) => {
    if (err) { log(req, 404, started); res.writeHead(404, { "content-type": "text/plain", ...SECURITY }); return res.end("Not found"); }
    const html = path.extname(file) === ".html";
    const headers = { "content-type": TYPES[path.extname(file)] || "application/octet-stream", ...SECURITY, etag: entry.etag,
      // the page always revalidates (a new deploy shows at once); assets cache briefly and revalidate by ETag
      // the page AND its assets revalidate on every load (ETag -> 304, so it stays fast): a new deploy shows at
      // once instead of a cached app.js running against a new page (STATIC_MAX_AGE > 0 restores browser caching)
      "cache-control": html || !STATIC_MAX_AGE ? "no-cache" : `public, max-age=${STATIC_MAX_AGE}`, vary: "accept-encoding" };
    if (req.headers["if-none-match"] === entry.etag) { log(req, 304, started); res.writeHead(304, headers); return res.end(); }
    const gzip = entry.gz && /\bgzip\b/.test(req.headers["accept-encoding"] || "");
    if (gzip) headers["content-encoding"] = "gzip";
    const body = gzip ? entry.gz : entry.body;
    headers["content-length"] = body.length;
    res.writeHead(200, headers);
    res.end(req.method === "HEAD" ? undefined : body);
    log(req, 200, started);
  });
}

// ---------------------------------------------------------------- routing
const server = http.createServer((req, res) => {
  const started = Date.now();
  const url = req.url || "/";
  if (url === "/healthz") return sendJson(res, 200, { status: "ok" });
  if (url.startsWith("/api/")) {
    if (!url.startsWith(API_PREFIX)) { log(req, 404, started); return sendJson(res, 404, { detail: { error: "NOT_FOUND" } }); }
    return proxy(req, res, started);
  }
  return serve(req, res, started);
});
server.headersTimeout = 20000;
server.requestTimeout = UPSTREAM_TIMEOUT_MS + 10000;
server.listen(PORT, HOST, () => {
  console.log(`Document readiness dashboard  ->  http://localhost:${PORT}`);
  console.log(`API proxied to                ->  ${BACKEND.origin}${API_PREFIX}*`);
});

// graceful shutdown: finish the requests in flight, then exit (a container stop / Ctrl+C)
function shutdown(signal) {
  console.log(`${signal}: closing`);
  server.close(() => process.exit(0));
  setTimeout(() => process.exit(0), 10000).unref();
}
process.on("SIGTERM", () => shutdown("SIGTERM"));
process.on("SIGINT", () => shutdown("SIGINT"));
