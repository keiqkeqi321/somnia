#!/usr/bin/env node
"use strict";

const { execSync, spawn } = require("child_process");

const RESET  = "\x1b[0m";
const RED    = "\x1b[31m";
const GREEN  = "\x1b[32m";
const YELLOW = "\x1b[33m";
const CYAN   = "\x1b[36m";
const BOLD   = "\x1b[1m";

function findPython() {
  const candidates = ["python3", "python"];
  for (const cmd of candidates) {
    try {
      const out = execSync(`${cmd} --version`, { stdio: "pipe", encoding: "utf-8" });
      if (out && /Python (\d+)/.test(out)) {
        const major = parseInt(/Python (\d+)/.exec(out)[1], 10);
        if (major >= 3) return { cmd, version: out.trim() };
      }
    } catch (_) { /* next */ }
  }
  return null;
}

console.log(`\n${BOLD}${CYAN}🤖 Somnia${RESET}\n`);

// ─── Step 1: Check Python ────────────────────────────────────
const py = findPython();

if (!py) {
  console.error(
    `${RED}${BOLD}✗ Python not found${RESET}\n\n` +
    `  Somnia requires ${BOLD}Python 3.11+${RESET}.\n\n` +
    `  Install Python first:\n` +
    `    ${CYAN}https://www.python.org/downloads/${RESET}\n\n` +
    `  Or use your package manager:\n` +
    `    macOS:  ${CYAN}brew install python@3.12${RESET}\n` +
    `    Ubuntu: ${CYAN}sudo apt install python3.12${RESET}\n` +
    `    Win:    ${CYAN}winget install Python.Python.3.12${RESET}\n`
  );
  process.exit(1);
}

console.log(`${GREEN}✓${RESET} Found ${py.version}`);

// ─── Step 2: Check openagent pip package ──────────────────────
let hasPkg = false;
try {
  execSync(`${py.cmd} -c "import openagent"`, { stdio: "pipe" });
  hasPkg = true;
} catch (_) {}

if (!hasPkg) {
  console.log(`${YELLOW}⚠${RESET}  somnia Python package not found, installing via pip ...`);
  try {
    execSync(`${py.cmd} -m pip install somnia`, { stdio: "inherit" });
    console.log(`${GREEN}✓${RESET} somnia installed!\n`);
  } catch (_) {
    console.error(
      `${RED}✗ Failed to install somnia via pip.${RESET}\n` +
      `  Try manually: ${CYAN}${py.cmd} -m pip install somnia${RESET}\n`
    );
    process.exit(1);
  }
}

// ─── Step 3: Run openagent ───────────────────────────────────
const args = process.argv.slice(2);
const child = spawn(py.cmd, ["-m", "openagent", ...args], {
  stdio: "inherit",
  env: { ...process.env },
});

child.on("exit", (code) => process.exit(code ?? 0));
