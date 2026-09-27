/**
 * hooks/check-no-background.mjs
 *
 * PreToolUse hook (matcher `Bash|Monitor|Agent`): the mechanical form of the
 * "nothing ever runs in the background" Hard Rule (ticket #101, extended to
 * `Agent` dispatches by #139).
 *
 * ## Why a PreToolUse hook, when two turn-end hooks already exist
 *
 * The SubagentStop hook (#93) and the Stop hook (#23) catch a turn that ends
 * with a backgrounded command outstanding. `agent-worktree#176` showed both
 * ways that is too late:
 *
 *   - attempt 1: the developer backgrounded the suite, armed a `Monitor`,
 *     and ended its turn — the turn-end hooks treated `Monitor` as a wait
 *     and let the stop through; the headless process died with the suite.
 *   - attempt 2: the suite hung; the harness killed the session after its
 *     600 s background-wait ceiling, and every diagnostic (the hang's
 *     stack) died with it. The turn never ended, so no Stop hook ever ran.
 *
 * A backgrounded command is wrong the moment it is *issued*, not the moment
 * the turn ends. So this hook refuses it up front: there is no case in
 * this pipeline where backgrounding is right (agents/developer.md step 4,
 * skills/process-developer/SKILL.md "Turn-end discipline"). A suite that does
 * not fit one Bash call is run as synchronous chunks, one foreground call
 * each, with an explicit `timeout`.
 *
 * ## What it refuses
 *
 *   - `Bash` with `run_in_background: true`
 *   - `Bash` whose command detaches on its own: `nohup …`, `Start-Job`,
 *     `Start-Process`, or a trailing `&` (see lib/turn-end-scan.mjs,
 *     backgroundReasonForToolUse — the same classifier the turn-end hooks
 *     use, so what this hook refuses and what they detect cannot drift
 *     apart)
 *   - every `Monitor` call — a `Monitor` is a promise to be woken, and
 *     nothing wakes a headless session
 *   - `Agent` with no `run_in_background` key, or any value other than
 *     `false` (ticket #139) — the Agent tool's own schema backgrounds by
 *     default, so a long developer/reviewer/critic dispatch is exactly as
 *     exposed to the 600 s background-task ceiling as a backgrounded `Bash`
 *     call, and just as silent when it is lost
 *
 * ## Scope gate
 *
 * A PreToolUse hook fires in every session that loads this plugin,
 * including a human's interactive one, where `Monitor` and background
 * commands are legitimate. The hook activates only when
 *   (a) `<cwd>/.adev/` exists — a live `process-developer` run (same gate as
 *       the Stop hook; `process-developer` creates it in its preconditions and
 *       the caller starts the session with cwd = the worktree), or
 *   (b) the payload's `agent_type` names one of this plugin's subagents
 *       (`developer`, `reviewer`, …), which never have a legitimate use.
 *
 * ## How it refuses
 *
 * Exit code 2 with the reason on stderr — the one blocking mechanism every
 * harness version honours; the reason is fed back to the model as the tool
 * result. The reason carries NO_BACKGROUND_MARKER so the turn-end hooks can
 * see in the transcript that the call was refused and nothing is running
 * (otherwise they would block a stop the agent has no way to satisfy).
 *
 * All failure modes (bad stdin, unknown tool, no scope match) pass —
 * fail-safe, matching the other hooks in this directory.
 */

import { existsSync } from "node:fs";
import path from "node:path";
import process from "node:process";

import {
  NO_BACKGROUND_MARKER,
  agentNameOf,
  backgroundReasonForToolUse,
  describeToolUse,
} from "./lib/turn-end-scan.mjs";

/** Subagents of this plugin for which the rule holds unconditionally. */
const PLUGIN_AGENTS = new Set([
  "developer",
  "reviewer",
  "planner",
  "context-extractor",
  "plan-critic",
  "test-critic",
]);

const RULE =
  "Hard Rule (ticket #101): no test run, build or wait is ever started with " +
  "run_in_background: true, `nohup … &`, Start-Job, Start-Process or Monitor. " +
  "Everything runs synchronously in the foreground, inside this turn. A suite " +
  "that does not fit one Bash call is run as synchronous chunks, one after " +
  "another, each a foreground Bash call with an explicit `timeout` (max " +
  "600000 ms) — the project's AGENTS.md chunks if it names any. A chunk that " +
  "hits the timeout is information, not a reason to background: re-run it " +
  "with a per-test timeout that dumps stacks (e.g. `pytest --timeout=<n> " +
  "--timeout-method=thread`) and put the dump in the change report. Ticket " +
  "#139: an Agent dispatch is the same rule — the Agent tool backgrounds by " +
  "default, so re-issue the call with run_in_background: false when the very " +
  "next action depends on its result. There is no case in which " +
  "backgrounding is right — if you believe you found one, that is a " +
  "`blocked` event, not a background task.";

function refuse(what) {
  process.stderr.write(
    `${NO_BACKGROUND_MARKER} refused ${what}. ${RULE}`,
  );
  process.exit(2);
}

async function main() {
  let payload;
  try {
    const chunks = [];
    for await (const chunk of process.stdin) chunks.push(chunk);
    payload = JSON.parse(Buffer.concat(chunks).toString("utf8"));
  } catch {
    process.exit(0);
  }

  // --- Scope gate ---
  const cwd = String(payload.cwd ?? "");
  const inPipelineRun = Boolean(cwd) && existsSync(path.join(cwd, ".adev"));
  const isPluginAgent = PLUGIN_AGENTS.has(agentNameOf(payload.agent_type));
  if (!inPipelineRun && !isPluginAgent) process.exit(0);

  // --- Decision ---
  const tool = String(payload.tool_name ?? "");
  if (tool === "Monitor") {
    refuse("Monitor");
  }
  const reason = backgroundReasonForToolUse(tool, payload.tool_input);
  if (reason !== null) {
    const label = describeToolUse(tool, payload.tool_input);
    refuse(`${label} (${reason})`);
  }

  process.exit(0);
}

main().catch(() => {
  process.exit(0);
});
