/**
 * scripts/codex-review.mjs
 *
 * Bundled Codex review script for the reviewer agent.
 * Provides a platform-agnostic way to run the Codex correctness pass,
 * avoiding the Windows-sandbox empty-diff problem with adversarial-review.
 *
 * Invocation:
 *   node scripts/codex-review.mjs <mode> <companion-path> <default-branch>
 *
 * Modes:
 *   working-tree  — stages all changes with git add -A, collects staged diff,
 *                   feeds it to Codex via task --prompt-file (platform-agnostic,
 *                   works on Windows with unstaged changes).
 *   branch        — runs adversarial-review --wait --base origin/<default-branch>
 *                   (completeness: kept for non-Windows / CI use).
 *
 * Output contract (stdout):
 *   - Free-form finding lines (may include "file:line — recommendation (codex)")
 *   - Last non-empty line is exactly one of:
 *       VERDICT: APPROVE
 *       VERDICT: CHANGES_REQUESTED
 *   - Soft-fail (Codex unavailable, empty diff, error):
 *       Single "Codex review unavailable: <reason>" line, NO VERDICT line.
 *
 * Exit code: always 0. The script never blocks the reviewer pipeline.
 * --write is NEVER passed to Codex; this script is review-only.
 */

import { writeFileSync, unlinkSync, existsSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { spawnSync } from "node:child_process";
import process from "node:process";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/**
 * Run a command synchronously and return { stdout, stderr, status }.
 * Always returns an object — never throws.
 */
function run(cmd, args, options = {}) {
  const result = spawnSync(cmd, args, {
    encoding: "utf8",
    maxBuffer: 50 * 1024 * 1024, // 50 MB
    ...options,
  });
  return {
    stdout: result.stdout ?? "",
    stderr: result.stderr ?? "",
    status: result.status ?? 1,
    error: result.error ?? null,
  };
}

/**
 * Emit a soft-fail message and exit 0.
 * Soft-fail = no VERDICT line, just a single explanatory line.
 */
function softFail(reason) {
  process.stdout.write(`Codex review unavailable: ${reason}\n`);
  process.exit(0);
}

// ---------------------------------------------------------------------------
// working-tree mode
// ---------------------------------------------------------------------------

function runWorkingTreeMode(companionPath, _defaultBranch) {
  // 1. Stage all working-tree changes (unstaged + untracked).
  //    This is what makes the approach platform-agnostic on Windows.
  //    Side-effect: collapses any partial-stage state the caller set up.
  //    All working-tree changes are staged to collect a platform-agnostic
  //    diff for the Codex pass; partial-stage state is not preserved.
  const addResult = run("git", ["add", "-A"]);
  if (addResult.status !== 0) {
    softFail("git add -A failed: " + (addResult.stderr || "unknown error").trim());
  }

  // 2. Collect the staged diff (platform-agnostic, works on Windows).
  const diffResult = run("git", ["diff", "--staged"]);
  if (diffResult.status !== 0) {
    softFail("git diff --staged failed: " + (diffResult.stderr || "unknown error").trim());
  }

  const diff = diffResult.stdout;

  // 3. If diff is empty → soft-fail (nothing to review).
  if (!diff || diff.trim() === "") {
    softFail("empty working-tree diff");
    // process.exit(0) already called inside softFail; return for clarity.
    return;
  }

  // 4. Write a temp file containing a prompt header + the diff.
  const tmpFile = join(
    tmpdir(),
    `codex-review-${Date.now()}-${Math.random().toString(36).slice(2)}.diff`
  );

  let cleanupDone = false;
  function cleanup() {
    if (cleanupDone) return;
    cleanupDone = true;
    try {
      if (existsSync(tmpFile)) unlinkSync(tmpFile);
    } catch {
      // Ignore cleanup errors — temp files are best-effort.
    }
  }

  try {
    const promptContent =
      "Review the following working-tree diff for correctness bugs. " +
      "Return file:line findings only. Do not edit any files.\n\n" +
      diff;
    writeFileSync(tmpFile, promptContent, "utf8");
  } catch (err) {
    softFail("failed to write temp file: " + String(err.message || err));
  }

  // 5. Run the companion via task --prompt-file. Never pass --write.
  let companionResult;
  try {
    companionResult = run("node", [companionPath, "task", "--prompt-file", tmpFile]);
  } catch (err) {
    cleanup();
    softFail("companion invocation failed: " + String(err.message || err));
  } finally {
    cleanup();
  }

  // 6. On non-zero exit or empty stdout → soft-fail.
  if (companionResult.status !== 0 || !companionResult.stdout.trim()) {
    softFail("companion exited with error");
    return;
  }

  // 7. Parse companion output: extract file:line findings and compute verdict.
  const output = companionResult.stdout;
  const findings = extractFindings(output);

  emitFindingsAndVerdict(findings);
}

// ---------------------------------------------------------------------------
// branch mode
// ---------------------------------------------------------------------------

function runBranchMode(companionPath, defaultBranch) {
  // 1. Run adversarial-review in foreground. Never pass --write.
  //
  //    Normalize the branch name: `git symbolic-ref --short refs/remotes/origin/HEAD`
  //    returns "origin/main" (already remote-qualified), so strip any leading
  //    "origin/" before prepending it — otherwise we'd get "origin/origin/main".
  //    Both "main" and "origin/main" must yield "--base origin/main".
  const normalizedBranch = defaultBranch.replace(/^origin\//, "");
  const base = `origin/${normalizedBranch}`;
  const result = run("node", [
    companionPath,
    "adversarial-review",
    "--wait",
    "--base",
    base,
  ]);

  // 2. Non-zero exit → soft-fail.
  if (result.status !== 0 || !result.stdout.trim()) {
    softFail("adversarial-review failed");
    return;
  }

  // 3. Parse JSON output from the companion.
  let parsed;
  try {
    parsed = JSON.parse(result.stdout.trim());
  } catch {
    // Output is not JSON — treat as free-form text findings.
    const findings = extractFindings(result.stdout);
    emitFindingsAndVerdict(findings);
    return;
  }

  // Map structured findings to "file:line — recommendation (codex)" lines.
  const findings = [];
  if (Array.isArray(parsed.findings)) {
    for (const entry of parsed.findings) {
      const file = entry.file ?? "unknown";
      const line = entry.line_start ?? entry.line ?? "?";
      const rec = entry.recommendation ?? entry.message ?? String(entry);
      findings.push(`${file}:${line} — ${rec} (codex)`);
    }
  }

  // Verdict: "needs-attention" or any findings → CHANGES_REQUESTED.
  if (parsed.verdict === "needs-attention" || findings.length > 0) {
    for (const f of findings) process.stdout.write(f + "\n");
    process.stdout.write("VERDICT: CHANGES_REQUESTED\n");
  } else {
    process.stdout.write("VERDICT: APPROVE\n");
  }
}

// ---------------------------------------------------------------------------
// Shared helpers
// ---------------------------------------------------------------------------

/**
 * Extract finding lines from free-form companion text.
 * A finding is any line that looks like "file:line" or "file:line:col".
 * We tag each with "(codex)".
 */
function extractFindings(text) {
  const findings = [];
  // Match lines containing a file:line reference: path/file.ext:NUMBER
  const findingPattern = /^.*\S+\.\w+:\d+.*$/;
  for (const line of text.split(/\r?\n/)) {
    const trimmed = line.trim();
    if (!trimmed) continue;
    if (findingPattern.test(trimmed)) {
      // Avoid double-tagging if "(codex)" already present.
      if (!trimmed.includes("(codex)")) {
        findings.push(trimmed + " (codex)");
      } else {
        findings.push(trimmed);
      }
    }
  }
  return findings;
}

/**
 * Write findings to stdout, then append VERDICT line.
 */
function emitFindingsAndVerdict(findings) {
  for (const f of findings) {
    process.stdout.write(f + "\n");
  }
  if (findings.length > 0) {
    process.stdout.write("VERDICT: CHANGES_REQUESTED\n");
  } else {
    process.stdout.write("VERDICT: APPROVE\n");
  }
}

// ---------------------------------------------------------------------------
// Entry point
// ---------------------------------------------------------------------------

async function main() {
  const [, , mode, companionPath, defaultBranch] = process.argv;

  if (!mode || !companionPath || !defaultBranch) {
    softFail(
      "usage: node scripts/codex-review.mjs <working-tree|branch> <companion-path> <default-branch>"
    );
    return;
  }

  if (mode === "working-tree") {
    runWorkingTreeMode(companionPath, defaultBranch);
  } else if (mode === "branch") {
    runBranchMode(companionPath, defaultBranch);
  } else {
    softFail(`unknown mode: ${mode}`);
  }
}

main().catch((err) => {
  // Any unexpected error → write unavailable message and exit 0.
  // The reviewer must never be blocked by this script.
  process.stdout.write(
    `Codex review unavailable: unexpected error — ${String(err?.message || err)}\n`
  );
  process.exit(0);
});
