// LOS Copilot -- a zero-dependency static server + API proxy (Node 18+).
//   node server.js                                   -> http://localhost:5183
//   BACKEND_URL=http://127.0.0.1:8010 PORT=5183 node server.js
//
// The browser talks to ONE origin (no CORS change). Only the routes this page uses are forwarded -- anything else is
// 404 here, so the page can never reach another backend route:
//   GET  /api/v1/fos/form-schema      the case form (fields, required, rules, choices)
//   GET  /api/v1/fos/config           products and workflow states
//   POST /api/v1/fos/applicants       open a case (Idempotency-Key passed through)
//   POST /api/v1/credit/dedupe        the one dedupe API (LOS dedupe + MIFIN + loan history + policy + explanation)
//   GET  /api/v1/credit/dedupe/{id}/reason   the model's 3-point reason for that check (fetched after the page shows)
//   POST /api/v1/auth/login | refresh sign-in (only where the backend runs its identity provider)
//
// Security: CSP, nosniff, no framing, no referrer; request-body limit; upstream timeout; hop-by-hop headers stripped;
// cookies never forwarded (the backend uses bearer tokens). Logs: method, path (never the query), status, time --
// never a body, so a PAN or mobile in a request never reaches a log line.
"use strict";

const http = require("http");
const https = require("https");
const fs = require("fs");
const path = require("path");
const zlib = require("zlib");
const crypto = require("crypto");

const PORT = Number(process.env.PORT || 5183);
const HOST = process.env.HOST || "127.0.0.1";
const BACKEND = new URL(process.env.BACKEND_URL || "http://127.0.0.1:8010");
const MAX_BODY = Number(process.env.MAX_BODY_BYTES || 64 * 1024);
const UPSTREAM_TIMEOUT_MS = Number(process.env.UPSTREAM_TIMEOUT_MS || 45000);
const ROOT = path.join(__dirname, "public");
const client = BACKEND.protocol === "https:" ? https : http;

const ROUTES = [
  ["GET", /^\/api\/v1\/fos\/form-schema$/],
  ["GET", /^\/api\/v1\/fos\/config$/],
  ["POST", /^\/api\/v1\/fos\/applicants$/],
  ["POST", /^\/api\/v1\/credit\/dedupe$/],
  ["GET", /^\/api\/v1\/credit\/dedupe\/ddp_[0-9a-f]{32}\/reason$/],
  ["POST", /^\/api\/v1\/auth\/(login|refresh)$/],
];
const TYPES = {
  ".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8", ".js": "text/javascript; charset=utf-8",
  ".svg": "image/svg+xml", ".ico": "image/x-icon", ".json": "application/json",
};
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
  if (process.env.QUIET) return;
  console.log(`${new Date().toISOString()} ${req.method} ${(req.url || "").split("?")[0]} ${status} ${Date.now() - started}ms`);
}

function sendJson(res, status, body) {
  res.writeHead(status, { "content-type": "application/json", "cache-control": "no-store", ...SECURITY });
  res.end(JSON.stringify(body));
}

function allowed(method, pathname) {
  return ROUTES.some(([m, re]) => m === method && re.test(pathname));
}

function proxy(req, res, started) {
  const headers = { ...req.headers, host: BACKEND.host };
  HOP_BY_HOP.forEach((h) => delete headers[h]);
  delete headers.cookie;
  if (Number(req.headers["content-length"] || 0) > MAX_BODY) {
    log(req, 413, started);
    return sendJson(res, 413, { detail: { code: "PAYLOAD_TOO_LARGE", message: "The request is too large." } });
  }
  const upstream = client.request({
    protocol: BACKEND.protocol, hostname: BACKEND.hostname,
    port: BACKEND.port || (BACKEND.protocol === "https:" ? 443 : 80), path: req.url, method: req.method, headers,
  }, (up) => {
    const out = { ...up.headers, ...SECURITY, "cache-control": "no-store" };
    HOP_BY_HOP.forEach((h) => delete out[h]);
    res.writeHead(up.statusCode || 502, out);
    up.pipe(res);
    up.on("end", () => log(req, up.statusCode, started));
  });
  upstream.setTimeout(UPSTREAM_TIMEOUT_MS, () => upstream.destroy(new Error("UPSTREAM_TIMEOUT")));
  upstream.on("error", (err) => {
    if (res.headersSent) return res.destroy();
    const timeout = err && err.message === "UPSTREAM_TIMEOUT";
    log(req, timeout ? 504 : 502, started);
    sendJson(res, timeout ? 504 : 502, { detail: {
      code: timeout ? "UPSTREAM_TIMEOUT" : "BACKEND_UNREACHABLE",
      message: timeout ? "The LOS backend took too long to answer." : "The LOS backend is not reachable." } });
  });
  let size = 0;
  req.on("data", (chunk) => {
    size += chunk.length;
    if (size > MAX_BODY) { upstream.destroy(); if (!res.headersSent) sendJson(res, 413, { detail: { code: "PAYLOAD_TOO_LARGE" } }); }
  });
  req.pipe(upstream);
}

function serveStatic(req, res, started, pathname) {
  const rel = pathname === "/" ? "/index.html" : pathname;
  const file = path.normalize(path.join(ROOT, rel));
  if (!file.startsWith(ROOT)) { log(req, 403, started); return sendJson(res, 403, { detail: { code: "FORBIDDEN" } }); }
  fs.readFile(file, (err, data) => {
    if (err) {
      // a client-side route (no file extension): the app shell
      if (!path.extname(rel)) return serveStatic(req, res, started, "/");
      log(req, 404, started);
      return sendJson(res, 404, { detail: { code: "NOT_FOUND" } });
    }
    const ext = path.extname(file);
    // the ETag is the CURRENT content's hash, every time: an edited file is never answered "304 not modified"
    const etag = `"${crypto.createHash("sha1").update(data).digest("hex").slice(0, 16)}"`;
    if (req.headers["if-none-match"] === etag) { res.writeHead(304, SECURITY); log(req, 304, started); return res.end(); }
    const headers = { "content-type": TYPES[ext] || "application/octet-stream", etag, "cache-control": "no-cache", ...SECURITY };
    if (/\bgzip\b/.test(req.headers["accept-encoding"] || "") && [".html", ".css", ".js", ".svg", ".json"].includes(ext)) {
      headers["content-encoding"] = "gzip";
      res.writeHead(200, headers);
      res.end(zlib.gzipSync(data));
    } else {
      res.writeHead(200, headers);
      res.end(data);
    }
    log(req, 200, started);
  });
}

function handler(req, res) {
  const started = Date.now();
  const pathname = new URL(req.url, "http://x").pathname;
  if (pathname === "/healthz") return sendJson(res, 200, { status: "ok", backend: BACKEND.origin });
  if (pathname.startsWith("/api/")) {
    if (!allowed(req.method, pathname)) {
      log(req, 404, started);
      return sendJson(res, 404, { detail: { code: "ROUTE_NOT_PROXIED", message: "This page does not use that route." } });
    }
    return proxy(req, res, started);
  }
  if (req.method !== "GET" && req.method !== "HEAD") return sendJson(res, 405, { detail: { code: "METHOD_NOT_ALLOWED" } });
  return serveStatic(req, res, started, pathname);
}

function start(port = PORT, host = HOST) {
  const server = http.createServer(handler);
  return new Promise((resolve) => server.listen(port, host, () => resolve(server)));
}

if (require.main === module) {
  start().then((server) => {
    console.log(`LOS Copilot on http://${HOST}:${server.address().port}  ->  backend ${BACKEND.origin}`);
    const stop = () => server.close(() => process.exit(0));
    process.on("SIGINT", stop);
    process.on("SIGTERM", stop);
  });
}

module.exports = { start, allowed, ROUTES };
