/**
 * hooks/check-developer-background-wait.mjs
 *
 * SubagentStop hook: blocks the `developer` subagent from ending its turn
 * while a command or subagent dispatch it backgrounded — Bash(run_in_background:
 * true), a command that detaches itself (`nohup … &`, `Start-Job`,
 * `Start-Process`, trailing `&`), or an `Agent` dispatch not given an explicit
 * `run_in_background: false` — is still unresolved (tickets #93, #101, #139).
 *
 * A subagent's turn ending does not suspend it, it TERMINATES it: the
 * harness kills any background command the subagent started, and the
 * outer process then reports a hollow "success" even though no real
 * verification result was ever produced. agents/developer.md's Hard Rules
 * forbid backgrounding outright, but two live incidents (#93, then
 * agent-worktree#176 attempt 1) showed the model doing it anyway — the
 * second time with a `Monitor` armed, which the previous version of this
 * hook accepted as "the sanctioned in-turn wait". It is not a wait: a
 * headless process is never woken, so a `Monitor` followed by a turn end is
 * the same death as no `Monitor` at all. Since #101 nothing resolves a
 * backgrounded command except the PreToolUse hook
 * (hooks/check-no-background.mjs) having refused it in the first place.
 *
 * This hook is the second line: the PreToolUse hook refuses the call, this
 * one catches a turn ending with one outstanding anyway (a harness that did
 * not run the PreToolUse hook, a detach shape the classifier missed).
 *
 * The same mistake one level up — the top-level `process-developer` session
 * ending its turn with an outstanding background command, which in headless
 * `claude -p` ends the whole process — is caught by the Stop hook
 * hooks/check-session-turn-end.mjs (ticket #23). The transcript walk and the
 * classifier both hooks need live in lib/turn-end-scan.mjs.
 *
 * Decision logic:
 *   - Only activates for the "developer" agent (agent_type check).
 *   - Steps aside when `stop_hook_active` is set: the agent cannot un-issue
 *     a call, so one clear message per turn is a backstop and a second one
 *     would be a hang.
 *   - Reads the JSONL transcript and walks it in order for the most recent
 *     backgrounded Bash call or backgrounded Agent dispatch that the
 *     PreToolUse hook did not refuse.
 *   - If one is outstanding -> block with a clear message.
 *   - All failure modes (bad stdin, unreadable transcript, wrong agent)
 *     are treated as "do not block" (fail-safe / exit 0), matching the
 *     existing hooks/check-mcp-availability.mjs.
 *
 * Block output: write JSON {"decision":"block","reason":"..."} to stdout, exit 0.
 * Pass: exit 0 with no stdout.
 */

import process from "node:process";

import {
  agentNameOf,
  block,
  readTranscriptLines,
  unresolvedBackgroundCommand,
} from "./lib/turn-end-scan.mjs";

async function main() {
  // --- 1. Read and parse stdin as the hook payload ---
  let payload;
  try {
    const stdinChunks = [];
    for await (const chunk of process.stdin) {
      stdinChunks.push(chunk);
    }
    const raw = Buffer.concat(stdinChunks).toString("utf8");
    payload = JSON.parse(raw);
  } catch {
    // Malformed stdin — fail-safe, do not block.
    process.exit(0);
  }

  // --- 2. Gate on the agent name being exactly "developer" ---
  // Plain substring matching (agentType.includes("developer")) is unsafe
  // here: this plugin's own id is "agent-autonomous-developer", so every
  // agent_type in this plugin ("agent-autonomous-developer:reviewer",
  // "...:planner", "...:context-extractor", ...) contains "developer" as a
  // substring of the *plugin* name, not the agent name. agentNameOf matches
  // the agent-name segment exactly instead.
  if (agentNameOf(payload.agent_type) !== "developer") {
    process.exit(0);
  }

  // --- 3. Never block twice on the same turn ---
  if (payload.stop_hook_active === true) process.exit(0);

  // --- 4. Read and scan the JSONL transcript ---
  const lines = readTranscriptLines(
    payload.agent_transcript_path ?? payload.transcript_path,
  );
  const unresolvedBgCommand = unresolvedBackgroundCommand(lines);

  // --- 5. Decision ---
  if (unresolvedBgCommand) {
    block(
      "developer: turn is ending with a backgrounded command still " +
        `outstanding (${unresolvedBgCommand}). This is the ticket #93 / #101 ` +
        "anti-pattern: a subagent's turn ending TERMINATES it, it is never " +
        "suspended and resumed, so the backgrounded process is about to be " +
        'killed and any "I\'ll resume once it completes" expectation cannot ' +
        "be honored — a Monitor does not change that, nothing wakes a " +
        "headless process. Backgrounding was never allowed (agents/developer.md " +
        "Hard Rules, ticket #101). Continue this turn and wait for that " +
        "command to finish with a blocking foreground Bash call (poll its log " +
        "or pid with an in-command loop, explicit `timeout`), or kill it and " +
        "re-run the work as synchronous foreground chunks; then finish the " +
        "change report with an explicit PASS/FAIL result.",
    );
  }

  process.exit(0);
}

main().catch(() => {
  // Any unexpected error — fail-safe, do not block.
  process.exit(0);
});
