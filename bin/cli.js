#!/usr/bin/env node
// npm launcher: checks the Python side, starts a local mock server when no
// backend is supplied, then runs the analysis runner.
import { spawn, spawnSync } from "node:child_process";
import { existsSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { createServer } from "node:net";

const HERE = dirname(fileURLToPath(import.meta.url));
const ROOT = resolve(HERE, "..");
const PORT = Number(process.env.AUDIT_MOCK_PORT || 8899);

function findPython() {
  const candidates = [process.env.PYTHON, "python3", "python"].filter(Boolean);
  for (const candidate of candidates) {
    const probe = spawnSync(candidate, ["-c", "import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)"], {
      stdio: "ignore",
    });
    if (probe.status === 0) return candidate;
  }
  return null;
}

function portFree(port) {
  return new Promise((resolveFree) => {
    const srv = createServer();
    srv.once("error", () => resolveFree(false));
    srv.once("listening", () => srv.close(() => resolveFree(true)));
    srv.listen(port, "127.0.0.1");
  });
}

async function waitForMock(url, attempts = 40) {
  for (let i = 0; i < attempts; i += 1) {
    try {
      const res = await fetch(`${url}/health`);
      if (res.ok) return true;
    } catch {
      /* not up yet */
    }
    await new Promise((r) => setTimeout(r, 200));
  }
  return false;
}

function usage() {
  console.log(`ai-session-audit - analyze AI coding sessions locally, then speak the
scoring-session protocol to a backend you control.

Usage:
  ai-session-audit <project> [--days N] [--limit N] [--model NAME]
                            [--proxy URL] [--results URL] [--token T]
                            [--git-repo PATH] [--author-email EMAIL]
                            [--out DIR] [--no-mock] [--no-enrich-git]
  ai-session-audit --doctor  # self-test the install and the mock protocol

With no --proxy/--results, a local mock backend is started on 127.0.0.1:${PORT}
so the whole run stays on this machine. Point --proxy and --results at your own
service to upload for real.

Requires Python 3.9+ and Node 18+.`);
}


async function runDoctor() {
  const results = [];
  const record = (name, ok, detail = "") => {
    results.push({ name, ok, detail });
    console.log(`${ok ? "PASS" : "FAIL"}  ${name}${detail ? "  " + detail : ""}`);
  };

  record("node", Number(process.versions.node.split(".")[0]) >= 18, `v${process.versions.node}`);

  const probe = spawnSync(python, ["-c", "import sys;print('%d.%d.%d' % sys.version_info[:3])"], { encoding: "utf8" });
  record("python", probe.status === 0, (probe.stdout || "").trim());

  for (const file of ["runner.py", "transport.py", "git_enrich.py", "mock-server.mjs", "pipeline/paxel_reproduction.py"]) {
    record(`file ${file}`, existsSync(join(ROOT, file)));
  }

  if (!(await portFree(PORT))) {
    record("mock port", false, `port ${PORT} busy; set AUDIT_MOCK_PORT`);
    return results;
  }

  const server = spawn(process.execPath, [join(ROOT, "mock-server.mjs")], {
    cwd: ROOT,
    env: { ...process.env, PORT: String(PORT) },
    stdio: ["ignore", "ignore", "inherit"],
  });
  try {
    const base = `http://127.0.0.1:${PORT}`;
    record("mock boot", await waitForMock(base));

    const health = await fetch(`${base}/health`).then((r) => r.json()).catch(() => ({}));
    record("GET /health", health.signatures_loaded === true);

    const session = await fetch(`${base}/api/v1/scoring_sessions`, {
      method: "POST",
      headers: { "content-type": "application/json", "x-yc-token": "doctor" },
      body: JSON.stringify({ git_remote: null, since_epoch: null, client_nonce: "doctor" }),
    }).then((r) => r.json()).catch(() => ({}));
    record("POST /api/v1/scoring_sessions", Boolean(session.id), session.id || "");

    if (session.id) {
      const manifest = await fetch(`${base}/api/v1/scoring_sessions/${session.id}/manifest`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ episode_manifest: { "episode:doctor": ["s1"] } }),
      });
      record("POST /manifest", manifest.status === 200);
    }

    const message = await fetch(`${base}/v1/messages`, {
      method: "POST",
      headers: { "content-type": "application/json", "x-api-key": "doctor", "x-paxel-prompt-id": "episode_scoring" },
      body: JSON.stringify({ model: "doctor", max_tokens: 16, messages: [{ role: "user", content: "ping" }] }),
    }).then((r) => r.json()).catch(() => ({}));
    const text = (message.content || []).map((b) => b.text || "").join("");
    record("POST /v1/messages", Boolean(message.id) && /yc_verify:/.test(text), message.id || "");

    const upload = await fetch(`${base}/api/v1/results`, {
      method: "POST",
      headers: { "content-type": "application/octet-stream", "x-yc-token": "doctor", "x-idempotency-key": "doctor" },
      body: JSON.stringify({ probe: true }),
    });
    record("POST /api/v1/results", upload.status === 201);
  } finally {
    server.kill("SIGTERM");
  }

  const failed = results.filter((r) => !r.ok);
  console.log(`\n${failed.length ? `${failed.length} check(s) failed` : "all checks passed"}`);
  return results;
}

const argv = process.argv.slice(2);
if (argv.includes("--doctor")) {
  const pythonPath = findPython();
  if (!pythonPath) {
    console.error("FAIL  python  not found (need Python 3.9+)");
    process.exit(2);
  }
}
const doctorMode = argv.includes("--doctor");

if (argv.includes("--help") || argv.includes("-h") || argv.length === 0) {
  usage();
  process.exit(0);
}
if (argv.includes("--version") || argv.includes("-v")) {
  const pkg = JSON.parse(await import("node:fs").then((fs) => fs.readFileSync(join(ROOT, "package.json"), "utf8")));
  console.log(pkg.version);
  process.exit(0);
}

const python = findPython();
if (doctorMode) {
  const results = await runDoctor();
  process.exit(results.some((r) => !r.ok) ? 1 : 0);
}
if (!python) {
  console.error("ai-session-audit needs Python 3.9+ on PATH (set PYTHON to an interpreter to override).");
  process.exit(2);
}

const hasBackend = argv.includes("--proxy") || argv.includes("--results");
const wantMock = !argv.includes("--no-mock") && !hasBackend;
let mock = null;
let proxyUrl = "http://127.0.0.1:" + PORT;
let resultsUrl = proxyUrl + "/api/v1/results";

if (wantMock) {
  if (!(await portFree(PORT))) {
    console.error(`Port ${PORT} is already in use; pass --no-mock --proxy URL --results URL, or set AUDIT_MOCK_PORT.`);
    process.exit(2);
  }
  const server = join(ROOT, "mock-server.mjs");
  if (!existsSync(server)) {
    console.error("mock-server.mjs missing from this package.");
    process.exit(2);
  }
  mock = spawn(process.execPath, [server], {
    cwd: ROOT,
    env: { ...process.env, PORT: String(PORT) },
    stdio: ["ignore", "ignore", "inherit"],
  });
  const ready = await waitForMock(proxyUrl);
  if (!ready) {
    console.error("local mock server did not become ready.");
    mock.kill();
    process.exit(3);
  }
  console.log(`[cli] local mock backend on ${proxyUrl} (nothing leaves this machine)`);
}

const runnerArgs = [join(ROOT, "runner.py"), ...argv];
if (wantMock) {
  runnerArgs.push("--proxy", proxyUrl, "--results", resultsUrl, "--token", "local-demo-token");
}

const child = spawn(python, runnerArgs, { cwd: process.cwd(), stdio: "inherit" });
const cleanup = () => {
  if (mock) mock.kill("SIGTERM");
};
process.on("SIGINT", () => {
  cleanup();
  process.exit(130);
});
process.on("SIGTERM", () => {
  cleanup();
  process.exit(143);
});
child.on("exit", (code) => {
  cleanup();
  process.exit(code ?? 1);
});
