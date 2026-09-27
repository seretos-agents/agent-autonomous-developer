/**
 * hooks/lib/turn-end-scan.mjs
 *
 * Shared helpers for the three "nothing ever runs in the background" hooks:
 *
 *   - hooks/check-no-background.mjs               (PreToolUse, #101, #139)
 *       refuses the call before it happens: Bash(run_in_background: true),
 *       a Bash command that detaches (`nohup … &`, `Start-Job`,
 *       `Start-Process`, trailing `&`), every `Monitor` call, and an `Agent`
 *       dispatch that backgrounds (no `run_in_background` key, or any value
 *       other than `false` — the Agent tool's own schema backgrounds by
 *       default).
 *   - hooks/check-developer-background-wait.mjs   (SubagentStop, #93)
 *       the `developer` subagent must not end its turn while a command it
 *       backgrounded is unresolved.
 *   - hooks/check-session-turn-end.mjs            (Stop, #23)
 *       the top-level `process-developer` session must not end its turn while a
 *       command or subagent dispatch it backgrounded is unresolved, nor while
 *       the package's work sits uncommitted in the worktree.
 *
 * All three answer the same question — "is this a backgrounded command or
 * subagent dispatch?" — so the classifier lives here once, and the two
 * turn-end hooks share one transcript walk on top of it.
 *
 * ## Why `Agent` needed its own branch (#139)
 *
 * A headless `process-developer` run dispatches long developer/reviewer/critic
 * work via the `Agent` tool. That tool's own schema backgrounds by default —
 * "Agents run in the background by default … Set to false only when your
 * very next action depends on this agent's result" — so an `Agent` call with
 * no `run_in_background` key, or an explicit `true`, is exactly the shape
 * this file already refuses for `Bash`: a dispatch the harness can silently
 * kill at the 600 s background-task ceiling in print mode, with no event
 * ever posted. Only an explicit `run_in_background: false` is the sanctioned
 * foreground dispatch (see AGENTS.md, "Every Agent dispatch is unnamed,
 * synchronous, fresh").
 *
 * ## Why `Monitor` no longer resolves anything (#101)
 *
 * Until #101 the walk treated a later `Monitor` call as the sanctioned
 * in-turn wait that "closed" a backgrounded command. The live incident
 * `agent-worktree#176` (attempt 1) showed that this is exactly the shape
 * that kills a headless session: the developer backgrounded the suite,
 * armed a `Monitor`, and ended its turn — the hook saw the `Monitor`, said
 * nothing, and the process died with the suite. A `Monitor` is not a wait;
 * it is a promise to be woken, and nothing wakes a headless session. So a
 * backgrounded command is now unresolved for the rest of the transcript.
 * The only thing that does resolve it is the PreToolUse hook having refused
 * it in the first place, recognised by the marker it leaves in the
 * transcript (see NO_BACKGROUND_MARKER) — then nothing is running, and
 * blocking the stop would only trap the agent behind a call it cannot undo.
 *
 * Every function here is total: it returns a null/empty result rather than
 * throwing, because every caller is a fail-safe hook that must never block a
 * turn because of its own bug.
 */

import { readFileSync } from "node:fs";

/**
 * Token the PreToolUse hook puts into its refusal text. When it appears in
 * the transcript after a backgrounded call, that call never ran.
 */
export const NO_BACKGROUND_MARKER = "[adev-no-background]";

/**
 * Bash command shapes that detach work from the calling turn, independent of
 * the `run_in_background` flag:
 *   - `nohup …`                      POSIX detach
 *   - `Start-Job` / `Start-Process`  PowerShell detach
 *   - a trailing `&` (end of command, or before `;` / newline) — the plain
 *     shell background operator. `&&` and `2>&1` do not match: the `&` must
 *     not be preceded by another `&` or by `>`, and must be followed only by
 *     whitespace, `;`, a newline, or the end of the command.
 */
const DETACH_PATTERNS = [
  /(^|[\s;|&(])nohup(\s|$)/,
  /(^|[\s;|&({])Start-Job(\s|$)/i,
  /(^|[\s;|&({])Start-Process(\s|$)/i,
  /(^|[^&>])&\s*(?=$|;|\n)/,
];

/**
 * Classify a Bash tool input. Returns a short reason when the call would
 * run something in the background, null when it is an ordinary foreground
 * call.
 *
 * @param {unknown} input  the `input` object of a Bash tool_use block
 * @returns {string | null}
 */
export function backgroundReasonForBash(input) {
  if (!input || typeof input !== "object") return null;
  if (input.run_in_background === true) return "run_in_background: true";
  const command = typeof input.command === "string" ? input.command : "";
  if (!command) return null;
  if (DETACH_PATTERNS[0].test(command)) return "nohup";
  if (DETACH_PATTERNS[1].test(command)) return "Start-Job";
  if (DETACH_PATTERNS[2].test(command)) return "Start-Process";
  if (DETACH_PATTERNS[3].test(command)) return "trailing `&`";
  return null;
}

/**
 * Classify any tool_use by name/input, dispatching to the right per-tool
 * rule. Returns a short reason when the call would run something in the
 * background, null when it is an ordinary foreground call or a tool this
 * rule does not cover.
 *
 * `"Bash"` delegates to backgroundReasonForBash, unchanged. `"Agent"`
 * backgrounds unless `run_in_background` is explicitly `false` (#139): the
 * Agent tool's own schema backgrounds by default, so an absent key or any
 * other value is unresolved, not just an explicit `true`.
 *
 * @param {string} name   the tool_use `name`
 * @param {unknown} input the tool_use `input`
 * @returns {string | null}
 */
export function backgroundReasonForToolUse(name, input) {
  if (name === "Bash") return backgroundReasonForBash(input);
  if (name === "Agent") {
    const runInBackground =
      input && typeof input === "object" ? input.run_in_background : undefined;
    if (runInBackground === false) return null;
    if (runInBackground === true) return "run_in_background: true";
    return "run_in_background not false (default: background)";
  }
  return null;
}

/**
 * A short human-readable label for a tool_use, for refusal/block messages.
 * A `Bash` call is described by its command; an `Agent` call is described as
 * `Agent(<subagent_type ?? description>)`, since neither a bare command nor
 * a blank label would mean anything for a subagent dispatch.
 *
 * @param {string} name   the tool_use `name`
 * @param {unknown} input the tool_use `input`
 * @returns {string}
 */
export function describeToolUse(name, input) {
  const obj = input && typeof input === "object" ? input : {};
  if (name === "Agent") {
    const label = obj.subagent_type ?? obj.description ?? "(unknown)";
    return `Agent(${label})`;
  }
  return String(obj.command ?? "(unknown command)");
}

/**
 * The agent-name segment of a hook payload's `agent_type`.
 *
 * Plain substring matching is unsafe in this plugin: its id is
 * "agent-autonomous-developer", so every agent_type here
 * ("agent-autonomous-developer:reviewer", "...:planner", ...) contains
 * "developer" as a substring of the *plugin* name. Match the segment after
 * the last ":" (or the whole string when unprefixed) instead.
 *
 * @param {unknown} agentType
 * @returns {string}
 */
export function agentNameOf(agentType) {
  const s = String(agentType ?? "");
  return s.includes(":") ? s.slice(s.lastIndexOf(":") + 1) : s;
}

/**
 * Read a JSONL transcript into lines.
 *
 * @param {unknown} transcriptPath
 * @returns {string[] | null} lines, or null when the path is missing or unreadable
 */
export function readTranscriptLines(transcriptPath) {
  if (!transcriptPath) return null;
  try {
    return readFileSync(String(transcriptPath), "utf8").split(/\r?\n/);
  } catch {
    return null;
  }
}

/**
 * Walk a transcript and report a label for the most recent backgrounded
 * Bash call or Agent dispatch (see backgroundReasonForToolUse) that actually
 * ran — i.e. that the PreToolUse hook did not refuse.
 *
 * A `Monitor` call does NOT resolve it (#101, see the file header). The
 * refusal marker does: a line after the call that carries
 * NO_BACKGROUND_MARKER means the harness rejected it and nothing is running.
 * Only the *last* backgrounded call matters: anything refused earlier in the
 * transcript is not the failure these hooks exist to catch.
 *
 * @param {string[] | null} lines
 * @returns {string | null} the unresolved command/dispatch label, or null when there is none
 */
export function unresolvedBackgroundCommand(lines) {
  if (!Array.isArray(lines)) return null;

  let unresolved = null;

  for (const rawLine of lines) {
    const line = rawLine.trim();
    if (!line) continue;

    let parsed;
    try {
      parsed = JSON.parse(line);
    } catch {
      continue;
    }

    // The PreToolUse refusal shows up in whatever record the harness writes
    // for a denied call (tool_result, system line, …) — never in an
    // assistant record, which is where a tool_use that merely *mentions* the
    // marker (an edit to this very file) would live. Match the marker on the
    // raw line so the exact record shape does not matter.
    if (
      unresolved !== null &&
      parsed?.type !== "assistant" &&
      line.includes(NO_BACKGROUND_MARKER)
    ) {
      unresolved = null;
      continue;
    }

    // Top-level transcripts nest the blocks under `message.content`; subagent
    // transcripts put them directly on `content`. Accept both so one walk
    // serves both hooks.
    const content = Array.isArray(parsed.content)
      ? parsed.content
      : Array.isArray(parsed?.message?.content)
        ? parsed.message.content
        : null;
    if (!content) continue;

    for (const item of content) {
      if (!item || item.type !== "tool_use") continue;
      if (backgroundReasonForToolUse(item.name, item.input) === null) continue;
      unresolved = describeToolUse(item.name, item.input);
    }
  }

  return unresolved;
}

/**
 * Emit a block decision and exit. Stop/SubagentStop hooks block by writing
 * {"decision":"block","reason":"..."} to stdout and exiting 0.
 *
 * @param {string} reason
 * @returns {never}
 */
export function block(reason) {
  process.stdout.write(JSON.stringify({ decision: "block", reason }));
  process.exit(0);
}
