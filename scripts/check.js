// Pre-publish guard: required files present, no captured payloads, no逆向源码.
import { existsSync, readdirSync, readFileSync, statSync } from "node:fs";
import { join, dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const required = [
  "bin/cli.js",
  "runner.py",
  "transport.py",
  "git_enrich.py",
  "mock-server.mjs",
  "prompts/episode_scoring.txt",
  "prompts/session_narrative.txt",
  "pipeline/paxel_reproduction.py",
];
const problems = [];
for (const file of required) {
  if (!existsSync(join(ROOT, file))) problems.push(`missing: ${file}`);
}

function walk(dir) {
  const out = [];
  for (const entry of readdirSync(dir)) {
    if (["node_modules", ".git", "__pycache__", "paxel-local-out", "paxel-reproduction"].includes(entry)) continue;
    const full = join(dir, entry);
    const stat = statSync(full);
    if (stat.isDirectory()) out.push(...walk(full));
    else out.push(full);
  }
  return out;
}

for (const file of walk(ROOT)) {
  if (file.endsWith("results-payload.json") || file.endsWith("run-summary.json")) {
    problems.push(`captured payload would ship: ${file}`);
  }
  if (file.includes("/paxel-audit/")) problems.push(`extracted client source would ship: ${file}`);
}

const pkg = JSON.parse(readFileSync(join(ROOT, "package.json"), "utf8"));
if (pkg.name === "paxel" || pkg.name.startsWith("paxel-")) {
  problems.push(`package name must not be a Paxel trademark: ${pkg.name}`);
}

if (problems.length) {
  console.error("prepack check failed:\n - " + problems.join("\n - "));
  process.exit(1);
}
console.log("prepack check ok");
