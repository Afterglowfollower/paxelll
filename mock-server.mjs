// Local mock of the Paxel server side, used only to capture what the real
// client sends. Nothing here talks to YC; every request is logged to
// requests.ndjson and answered with a minimal, structurally valid response.
import { createServer } from "node:http";
import { appendFileSync, mkdirSync } from "node:fs";

const PORT = Number(process.env.PORT || 8899);
const LOG = process.env.LOG || "/tmp/paxel-capture/requests.ndjson";
mkdirSync("/tmp/paxel-capture", { recursive: true });

const SCORING_JSON = {
  title: "Mock episode for contract capture",
  facts: "The developer inspected the mock transcript and made a small edit.",
  interpretation: "Shows a bounded edit loop with verification.",
  counterweight: "Single short episode limits confidence.",
  confidence: 0.6,
  scores: {
    execution_leverage: 6.5,
    steering: 6.0,
    engineering_quality: 6.5,
    product_thinking: 5.0,
    planning: 5.5,
  },
};

let nonceCounter = 0;

function anthropicMessage(model, extraText = "") {
  nonceCounter += 1;
  const text = `${extraText}${extraText ? "\n\n" : ""}${JSON.stringify(SCORING_JSON)}\n<!-- yc_verify:req_mock_${nonceCounter}:hmac_mock_${nonceCounter} -->`;
  return {
    id: `msg_mock_${nonceCounter}`,
    type: "message",
    role: "assistant",
    model: model || "claude-haiku-4-5",
    content: [{ type: "text", text }],
    stop_reason: "end_turn",
    stop_sequence: null,
    usage: { input_tokens: 120, output_tokens: 80 },
  };
}

function readBodyRaw(req) {
  return new Promise((resolve) => {
    const chunks = [];
    let size = 0;
    req.on("data", (c) => {
      size += c.length;
      if (size <= 4 * 1024 * 1024) chunks.push(c);
    });
    req.on("end", () => resolve(Buffer.concat(chunks)));
  });
}

const server = createServer(async (req, res) => {
  const buf = await readBodyRaw(req);
  const body = buf.toString("utf8");
  const path = (req.url || "/").split("?")[0];
  let status = 200;
  let payload = {};

  if (req.method === "GET" && path === "/health") {
    payload = { status: "ok", signatures_loaded: true, mock: true };
  } else if (req.method === "POST" && path === "/validate") {
    payload = { valid: true, mock: true };
  } else if (req.method === "POST" && path === "/api/v1/proxy/preflight") {
    payload = {
      contract_version: "00000000000000aa",
      bun_signatures_stale: false,
      bun_signatures_age_ms: 1000,
      bun_signatures_refresh_interval_ms: 3600000,
      mock: true,
    };
  } else if (req.method === "POST" && path === "/v1/messages") {
    const promptId = req.headers["x-paxel-prompt-id"] || "";
    if (promptId === "session_narrative" || promptId === "session_narrative_merge") {
      nonceCounter += 1;
      const prose = [
        "## Goal",
        "Mock narrative for contract capture.",
        "## What the Developer Decided",
        "Ran one bounded edit loop and verified it.",
        "## Key Decisions",
        "Kept the change small.",
        "## Problems Encountered",
        "None observed.",
        "## Observations",
        "Evidence is thin in this synthetic fixture.",
      ].join("\n");
      payload = {
        id: "msg_mock_" + nonceCounter,
        type: "message",
        role: "assistant",
        model: "mock-narrative",
        content: [{ type: "text", text: prose + "\n<!-- yc_verify:req_narr_" + nonceCounter + ":hmac_narr_" + nonceCounter + " -->" }],
        stop_reason: "end_turn",
        stop_sequence: null,
        usage: { input_tokens: 80, output_tokens: 40 },
      };
    } else {
    let model = null;
    try {
      model = JSON.parse(body).model;
    } catch {}
    payload = anthropicMessage(model);
    }
  } else if (req.method === "POST" && path === "/api/v1/scoring_sessions") {
    status = 201;
    payload = { id: "ss_mock_0001", manifest_committed: false, mock: true };
  } else if (
    req.method === "POST" &&
    path.startsWith("/api/v1/scoring_sessions/") &&
    path.endsWith("/manifest")
  ) {
    status = 200;
    payload = { ok: true, mock: true };
  } else if (req.method === "POST" && path.endsWith("/api/v1/results")) {
    status = 201;
    payload = {
      slug: "mock-slug-0001",
      results_url: "http://127.0.0.1:8899/r/mock-slug-0001",
      idempotent_replay: false,
      mock: true,
    };
  } else {
    status = 404;
    payload = { error: "mock: unhandled route", method: req.method, path };
  }

  appendFileSync(
    LOG,
    JSON.stringify({
      at: new Date().toISOString(),
      method: req.method,
      path,
      headers: req.headers,
      bodyBytes: buf.length,
      gzip: buf.length > 2 && buf[0] === 0x1f && buf[1] === 0x8b,
      bodyBase64: buf.toString("base64"),
      body,
      responseStatus: status,
    }) + "\n",
  );

  const out = JSON.stringify(payload);
  res.writeHead(status, {
    "content-type": "application/json",
    "content-length": Buffer.byteLength(out),
  });
  res.end(out);
});

server.listen(PORT, "0.0.0.0", () => {
  console.log(`mock paxel server listening on :${PORT}, log=${LOG}`);
});
