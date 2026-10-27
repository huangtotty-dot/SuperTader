#!/usr/bin/env node
"use strict";

const http = require("node:http");
const path = require("node:path");
const fs = require("node:fs/promises");

const WEB_ROOT = path.resolve(__dirname, "..", "web");
const MIME_TYPES = {
  ".html": "text/html; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".svg": "image/svg+xml",
  ".png": "image/png",
  ".jpg": "image/jpeg",
  ".jpeg": "image/jpeg",
  ".gif": "image/gif",
  ".ico": "image/x-icon",
  ".woff": "font/woff",
  ".woff2": "font/woff2",
};

function readOption(name, fallback) {
  const equalsPrefix = `--${name}=`;
  const inline = process.argv.slice(2).find(arg => arg.startsWith(equalsPrefix));
  if (inline) return inline.slice(equalsPrefix.length);
  const index = process.argv.indexOf(`--${name}`);
  return index >= 0 && index + 1 < process.argv.length
    ? process.argv[index + 1]
    : fallback;
}

const host = readOption("host", "127.0.0.1");
const port = Number(readOption("port", "7100"));
if (!Number.isInteger(port) || port < 1 || port > 65535) {
  console.error("Invalid --port: expected an integer from 1 to 65535");
  process.exit(1);
}

function send(res, status, body, method = "GET") {
  res.writeHead(status, { "Content-Type": "text/plain; charset=utf-8", "Cache-Control": "no-store" });
  res.end(method === "HEAD" ? undefined : body);
}

const server = http.createServer(async (req, res) => {
  const method = req.method || "GET";
  if (method !== "GET" && method !== "HEAD") {
    res.setHeader("Allow", "GET, HEAD");
    send(res, 405, "Method Not Allowed", method);
    return;
  }

  let pathname;
  try {
    const url = new URL(req.url || "/", `http://${req.headers.host || "localhost"}`);
    pathname = decodeURIComponent(url.pathname);
  } catch {
    send(res, 400, "Bad Request", method);
    return;
  }
  if (pathname.includes("\0")) {
    send(res, 400, "Bad Request", method);
    return;
  }

  const requestedPath = pathname === "/" ? "/index.html" : pathname;
  const filePath = path.resolve(WEB_ROOT, `.${requestedPath}`);
  const relative = path.relative(WEB_ROOT, filePath);
  if (!relative || relative.startsWith("..") || path.isAbsolute(relative)) {
    send(res, 403, "Forbidden", method);
    return;
  }

  try {
    const stat = await fs.stat(filePath);
    if (!stat.isFile()) {
      send(res, 404, "Not Found", method);
      return;
    }
    const body = await fs.readFile(filePath);
    res.writeHead(200, {
      "Content-Type": MIME_TYPES[path.extname(filePath).toLowerCase()] || "application/octet-stream",
      "Content-Length": body.length,
      "Cache-Control": "no-store",
      "X-Content-Type-Options": "nosniff",
    });
    res.end(method === "HEAD" ? undefined : body);
  } catch (error) {
    if (error && (error.code === "ENOENT" || error.code === "ENOTDIR")) {
      send(res, 404, "Not Found", method);
      return;
    }
    send(res, 500, "Internal Server Error", method);
  }
});

server.on("error", error => {
  console.error(`Preview server error: ${error.message}`);
  process.exitCode = 1;
});
server.listen(port, host, () => {
  console.log(`SuperTrader preview: http://${host}:${port}/`);
  console.log(`Static root: ${WEB_ROOT}`);
});

for (const signal of ["SIGINT", "SIGTERM"]) {
  process.on(signal, () => server.close(() => process.exit(0)));
}
