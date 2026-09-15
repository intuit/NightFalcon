#!/usr/bin/env node
"use strict";

const fs = require("node:fs");
const path = require("node:path");
const { spawnSync } = require("node:child_process");

const root = path.resolve(__dirname, "../../..");
const canonicalPackage = path.join(root, "src/nightfalcon");
if (!fs.statSync(canonicalPackage).isDirectory()) {
  console.error("nightfalcon: canonical Python package is missing");
  process.exit(1);
}

const candidates = [process.env.NIGHTFALCON_PYTHON, "python3", "python"].filter(Boolean);
let python = null;
for (const candidate of candidates) {
  const probe = spawnSync(candidate, ["-c", "import sys; raise SystemExit(sys.version_info < (3, 11))"]);
  if (probe.status === 0) {
    python = candidate;
    break;
  }
}
if (python === null) {
  console.error("nightfalcon: Python 3.11 or later is required");
  process.exit(1);
}

const env = { ...process.env };
env.PYTHONPATH = path.join(root, "src") + (env.PYTHONPATH ? path.delimiter + env.PYTHONPATH : "");
const result = spawnSync(python, ["-m", "nightfalcon", ...process.argv.slice(2)], {
  cwd: root,
  env,
  stdio: "inherit",
});
if (result.error) {
  console.error(`nightfalcon: ${result.error.message}`);
  process.exit(1);
}
process.exit(result.status === null ? 1 : result.status);
