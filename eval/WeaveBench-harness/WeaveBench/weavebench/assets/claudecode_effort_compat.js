// WeaveBench compatibility shim for Claude Code 2.1.76 on Anthropic-compatible
// gateways. Claude Code 2.1.76 accepts --effort, but may send the
// older Anthropic thinking payload:
//   { "thinking": { "type": "enabled", "budget_tokens": ... } }
// Some gateway-backed endpoints expect:
//   { "thinking": { "type": "adaptive" },
//     "output_config": { "effort": "high" } }
//
// This file is loaded only when NODE_OPTIONS=--require points at it. It leaves
// the official Claude Code runtime/tarball unchanged and rewrites only outbound
// JSON request bodies while WEAVEBENCH_CLAUDECODE_EFFORT_COMPAT=1.

"use strict";

const http = require("node:http");
const https = require("node:https");

const ACTIVE = process.env.WEAVEBENCH_CLAUDECODE_EFFORT_COMPAT === "1";

const DEBUG = process.env.WEAVEBENCH_CLAUDECODE_EFFORT_PATCH_DEBUG === "1";
const ALLOWED_EFFORTS = new Set(["low", "medium", "high", "max"]);

function log(message) {
  if (DEBUG) {
    process.stderr.write(`[weavebench-effort-compat] ${message}\n`);
  }
}

function selectedEffort() {
  const raw = (
    process.env.WEAVEBENCH_ANTHROPIC_OUTPUT_EFFORT ||
    process.env.CLAUDE_CODE_EFFORT_LEVEL ||
    "high"
  ).trim().toLowerCase();
  return ALLOWED_EFFORTS.has(raw) ? raw : "high";
}

function maybeRewriteJsonText(text) {
  if (!ACTIVE || !text || typeof text !== "string") return null;

  let payload;
  try {
    payload = JSON.parse(text);
  } catch {
    return null;
  }

  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    return null;
  }

  const thinking = payload.thinking;
  if (!thinking || typeof thinking !== "object") return null;

  if (thinking.type !== "enabled") return null;

  const effort = selectedEffort();
  const next = {
    ...payload,
    thinking: { type: "adaptive" },
    output_config: {
      ...(payload.output_config || {}),
      effort,
    },
  };

  log(`rewrote thinking.enabled to adaptive/output_config.effort=${effort}`);
  return JSON.stringify(next);
}

function bodyToText(body) {
  if (typeof body === "string") return body;
  if (Buffer.isBuffer(body)) return body.toString("utf8");
  if (body instanceof Uint8Array) return Buffer.from(body).toString("utf8");
  return null;
}

function rewriteBody(body) {
  const text = bodyToText(body);
  if (text === null) return null;
  const rewritten = maybeRewriteJsonText(text);
  return rewritten === null ? null : rewritten;
}

function cloneHeadersWithoutContentLength(headers) {
  if (!headers) return headers;
  const next = new Headers(headers);
  next.delete("content-length");
  return next;
}

function patchFetch() {
  if (typeof globalThis.fetch !== "function") return;

  const originalFetch = globalThis.fetch;
  globalThis.fetch = function patchedFetch(input, init) {
    const requestInit = init || {};
    const rewritten = rewriteBody(requestInit.body);
    if (rewritten === null) {
      return originalFetch.apply(this, arguments);
    }

    const nextInit = {
      ...requestInit,
      body: rewritten,
      headers: cloneHeadersWithoutContentLength(requestInit.headers),
    };
    return originalFetch.call(this, input, nextInit);
  };
}

function normalizeEndArgs(args) {
  const out = { chunk: undefined, encoding: undefined, callback: undefined };
  if (args.length === 0) return out;
  if (typeof args[0] === "function") {
    out.callback = args[0];
    return out;
  }
  out.chunk = args[0];
  if (typeof args[1] === "function") {
    out.callback = args[1];
  } else {
    out.encoding = args[1];
    out.callback = args[2];
  }
  return out;
}

function chunkToBuffer(chunk, encoding) {
  if (chunk === undefined || chunk === null) return null;
  if (Buffer.isBuffer(chunk)) return chunk;
  if (chunk instanceof Uint8Array) return Buffer.from(chunk);
  return Buffer.from(String(chunk), encoding || "utf8");
}

function patchRequestModule(moduleObject) {
  const originalRequest = moduleObject.request;

  moduleObject.request = function patchedRequest() {
    const req = originalRequest.apply(this, arguments);
    if (!ACTIVE) return req;

    const originalWrite = req.write;
    const originalEnd = req.end;
    const chunks = [];

    req.write = function patchedWrite(chunk, encoding, callback) {
      const buf = chunkToBuffer(chunk, encoding);
      if (buf) chunks.push(buf);
      if (typeof encoding === "function") {
        process.nextTick(encoding);
      } else if (typeof callback === "function") {
        process.nextTick(callback);
      }
      return true;
    };

    req.end = function patchedEnd() {
      const parsed = normalizeEndArgs(arguments);
      const finalChunk = chunkToBuffer(parsed.chunk, parsed.encoding);
      if (finalChunk) chunks.push(finalChunk);

      const originalBody = Buffer.concat(chunks);
      const rewritten = rewriteBody(originalBody);
      if (rewritten !== null) {
        try {
          req.removeHeader("content-length");
          req.setHeader("content-length", Buffer.byteLength(rewritten));
        } catch {
          // Some request implementations freeze headers at this point.
        }
        if (rewritten.length > 0) {
          originalWrite.call(req, rewritten);
        }
      } else if (originalBody.length > 0) {
        originalWrite.call(req, originalBody);
      }

      return originalEnd.call(req, parsed.callback);
    };

    return req;
  };
}

patchFetch();
patchRequestModule(http);
patchRequestModule(https);
log("loaded");
