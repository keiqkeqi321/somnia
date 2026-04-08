"use strict";
// =============================================================
//  postinstall.js — automatically pip install somnia
//  Runs after `npm install somnia` or `npx somnia`.
// =============================================================

const { execSync } = require("child_process");

function findPython() {
  const candidates = ["python3", "python"];
  for (const cmd of candidates) {
    try {
      const result = execSync(`${cmd} --version`, {
        stdio: "pipe",
        encoding: "utf-8",
      });
      if (result && result.includes("Python")) return cmd;
    } catch (_) {}
  }
  return null;
}

const pythonCmd = findPython();

if (!pythonCmd) {
  console.log(
    "⚠️  Python not found. Skipping automatic pip install.\n" +
    "   Please install Python 3.11+ and then run:\n" +
    "     pip install somnia"
  );
  process.exit(0);
}

// Check if somnia is already installed
try {
  execSync(`${pythonCmd} -c "import openagent"`, { stdio: "pipe" });
  console.log("✅ somnia Python package is already installed.");
} catch (_) {
  console.log("📦 Installing somnia Python package via pip ...");
  try {
    execSync(`${pythonCmd} -m pip install somnia`, { stdio: "inherit" });
    console.log("✅ somnia installed successfully!");
  } catch (err) {
    console.error(
      "⚠️  Failed to auto-install somnia via pip.\n" +
      "   Please install manually: pip install somnia"
    );
    process.exit(0);
  }
}
