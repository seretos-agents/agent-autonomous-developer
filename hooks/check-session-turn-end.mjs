/**
 * hooks/check-session-turn-end.mjs
 *
 * Stop hook: the mechanical backstop for ticket #23 — the top-level
 * `process-developer` session ending its turn while work is still outstanding.
 *
 * ## Why this exists one level above the #93 hook
 *
 * #93 established that a *subagent* ending its turn is terminated, not
 * suspended. #23 showed the same thing is true of the **top-level session**
 * when it runs headless: in `claude -p` there is no interactive loop to wake
 * the session after its turn ends, so **ending the turn ends the process**.
 * Three attempts on lib-python-worktree #140 proved it independently of the
 * documented workaround:
 *
 *   | attempt | CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS | outcome                        |
 *   |---------|--------------------------------------|--------------------------------|
 *   | 1       | unset (default 600s)                 | "Background tasks still running after 600s; terminating." |
 *   | 2       | 0                                     | dead after 87s — `0` means *no* wait, not "wait forever"  |
 *   | 3       | 7200000 (2h)                          | dead after 19.5 min, clean `result`, empty stderr          |
 *
 * The env var only controls how long the process loiters before it is killed;
 * it never turns waiting into resuming. The agent in attempt 3 had even
 * diagnosed the mechanic correctly and moved the suite run "under my own turn
 * so it will survive" — and then ended that turn, which killed it anyway.
 *
 * ## What it checks
 *
 * Two independent conditions, both scoped to a live `process-developer` run:
 *
 *   A. **Unresolved backgrounded command or subagent dispatch** — the #23
 *      anti-pattern proper. Same detection as the #93 SubagentStop hook
 *      (shared in lib/), because it is the same mistake at a different
 *      level — since #139 this also covers a backgrounded `Agent` dispatch
 *      (no `run_in_background` key, or any value other than `false`), not
 *      just a backgrounded `Bash` call. Since #101 a `Monitor` call no
 *      longer counts as resolving it (agent-worktree#176 died with one
 *      armed); the PreToolUse hook hooks/check-no-background.mjs refuses the
 *      call up front, and this condition is the backstop behind it.
 *
 *   B. **Unpreserved work** — the worktree has uncommitted changes, or commits
 *      that exist on no remote. This is the damage #23 and #22 actually did:
 *      the orchestrator's retry path prescribes `worktree_remove` after a
 *      failed second attempt, so anything not pushed is destroyed. On #140
 *      that was 1979 insertions across 15 files with HEAD still on `main`;
 *      on #139 the analogous loss had already happened and had to be
 *      recovered by hand as a patch. Whatever ends a run, the work must
 *      survive it — a retry that starts from committed state is cheap, one
 *      that starts from nothing pays for orientation, planning and critique
 *      all over again.
 *
 * ## Scope gate — why this cannot fire in a normal session
 *
 * A Stop hook fires on *every* turn end of *every* session that loads this
 * plugin, including a human's interactive one. Blocking those would be
 * intolerable. The gate is the presence of `<cwd>/.adev/`: `process-developer`
 * creates `<worktree_path>/.adev/<package>-<attempt>/` in its preconditions,
 * and `start-package-session.sh` starts the session with cwd = the worktree.
 * No `.adev/` directory, no pipeline run, no hook.
 *
 * `stop_hook_active` caps this at a single block per turn: if the session
 * still ends its turn after being told once, the hook steps aside rather than
 * looping. One clear message is a backstop; an unbreakable loop is a hang.
 *
 * All failure modes (bad stdin, unreadable transcript, git unavailable, no
 * `.adev/`) are treated as "do not block" — fail-safe, matching
 * hooks/check-mcp-availability.mjs and hooks/check-developer-background-wait.mjs.
 *
 * ## The harness-launched-developer branch (ticket #141)
 *
 * When agent-harness's `harness_start_agent` launches a `developer`, it runs
 * as a *top-level* `claude` process — not a subagent — so neither
 * `SubagentStop` nor `hooks/check-developer-background-wait.mjs` ever fires
 * for it. Only this Stop hook does, and a top-level Stop payload carries no
 * `agent_type` the way SubagentStop does, so the only identity signal is
 * `process.env.HARNESS_LAUNCHED_AGENT` (agent-harness PR #54, `<plugin>:<name>`).
 *
 * This branch runs **before** the `.adev/` scope gate (a harness-launched
 * developer's cwd need not carry `.adev/` at all) and checks *exact* string
 * equality against `HARNESS_DEVELOPER_ID`, never `agentNameOf` — that helper
 * drops the plugin qualifier, which would wrongly let `other-plugin:developer`
 * through (misread::F1). It applies condition A (unresolved backgrounded
 * command/dispatch) with the SubagentStop hook's own message prefix, then
 * **always exits** — condition B (the orchestrator's commit/push nag) never
 * applies to a developer child: the orchestrator pushes, not the developer
 * (#138/#140), so a harness-launched developer must never see it, even in a
 * cwd that happens to carry `.adev/`.
 *
 * Block output: write JSON {"decision":"block","reason":"..."} to stdout, exit 0.
 * Pass: exit 0 with no stdout.
 */

import { execFileSync } from "node:child_process";
import { existsSync } from "node:fs";
import path from "node:path";
import process from "node:process";

import {
  block,
  readTranscriptLines,
  unresolvedBackgroundCommand,
} from "./lib/turn-end-scan.mjs";

/**
 * The exact `HARNESS_LAUNCHED_AGENT` value agent-harness sets for a
 * `harness_start_agent`-launched `developer` run of this plugin. Full-string
 * equality, plugin qualifier included — never `agentNameOf` (see file header).
 */
const HARNESS_DEVELOPER_ID = "agent-autonomous-developer:developer";

/**
 * Run a git command in `cwd` and return trimmed stdout, or null on any
 * failure (git missing, not a repository, non-zero exit, timeout).
 */
function git(cwd, args) {
  try {
    return execFileSync("git", ["-C", cwd, ...args], {
      encoding: "utf8",
      timeout: 10_000,
      stdio: ["ignore", "pipe", "ignore"],
    }).trim();
  } catch {
    return null;
  }
}

async function main() {
  // --- 1. Read and parse stdin as the hook payload ---
  let payload;
  try {
    const chunks = [];
    for await (const chunk of process.stdin) chunks.push(chunk);
    payload = JSON.parse(Buffer.concat(chunks).toString("utf8"));
  } catch {
    process.exit(0);
  }

  // --- 2. Never block twice on the same turn ---
  if (payload.stop_hook_active === true) process.exit(0);

  // --- 2b. Harness-launched developer branch (#141) — before the .adev/ gate ---
  if (process.env.HARNESS_LAUNCHED_AGENT === HARNESS_DEVELOPER_ID) {
    const harnessLines = readTranscriptLines(payload.transcript_path);
    const harnessUnresolved = unresolvedBackgroundCommand(harnessLines);
    if (harnessUnresolved) {
      block(
        "developer: turn is ending with a backgrounded command still " +
          `outstanding (${harnessUnresolved}). This is the ticket #93 / #101 ` +
          "anti-pattern: a subagent's turn ending TERMINATES it, it is never " +
          "suspended and resumed, so the backgrounded process is about to be " +
          'killed and any "I\'ll resume once it completes" expectation cannot ' +
          "be honored — a Monitor does not change that, nothing wakes a " +
          "headless process. Backgrounding was never allowed (agents/developer.md " +
          "Hard Rules, ticket #101). Continue this turn and wait for that " +
          "command to finish with a blocking foreground Bash call (poll its log " +
          "or pid with an in-command loop, explicit `timeout`), or kill it and " +
          "re-run the work as synchronous foreground chunks; then finish the " +
          "change report with an explicit PASS/FAIL result. (ticket #141)",
      );
    }
    // Condition B (commit/push) never applies to a developer child — the
    // orchestrator pushes, not the developer (#138/#140) — so this session
    // always ends here, before the .adev/ scope gate below can even be
    // reached, regardless of whether cwd happens to carry `.adev/`.
    process.exit(0);
  }

  // --- 3. Scope gate: only inside a live process-developer run ---
  const cwd = String(payload.cwd ?? "");
  if (!cwd || !existsSync(path.join(cwd, ".adev"))) process.exit(0);

  // --- 4. Condition A: a backgrounded command or subagent dispatch nothing waited on ---
  const lines = readTranscriptLines(payload.transcript_path);
  const unresolved = unresolvedBackgroundCommand(lines);
  if (unresolved) {
    block(
      "process-developer: the turn is ending with a backgrounded command or " +
        `subagent dispatch still unresolved (${unresolved}). This is the ` +
        "ticket #23 anti-pattern. This session is headless (claude -p): " +
        "there is no loop that wakes it after the turn ends, so ENDING THE " +
        "TURN ENDS THE PROCESS and that command or dispatch is killed with " +
        "it — no wait-ceiling setting changes that (measured on #140 at " +
        "600s, at 0, and at 2h; all three died). A Monitor does not help " +
        "either — nothing wakes a headless process (agent-worktree#176, " +
        "ticket #101). Backgrounding was never allowed, and an Agent " +
        "dispatch with no run_in_background key (or any value other than " +
        "false) backgrounds by default just like Bash(run_in_background: " +
        "true) (ticket #139). Continue this turn and wait for it with a " +
        "blocking foreground call — re-issue an Agent dispatch with " +
        "run_in_background: false, or poll a Bash command's log/pid in an " +
        "in-command loop with an explicit `timeout` — or kill it and re-run " +
        "the work as synchronous foreground chunks. Do not end the turn " +
        "expecting to be resumed.",
    );
  }

  // --- 5. Condition B: work that would not survive the turn ---
  const dirty = git(cwd, ["status", "--porcelain"]);
  if (dirty === null) process.exit(0); // not a git checkout / git unavailable
  const unpushed = git(cwd, ["rev-list", "--count", "HEAD", "--not", "--remotes"]);
  const unpushedCount = Number.parseInt(unpushed ?? "0", 10) || 0;

  if (dirty !== "" || unpushedCount > 0) {
    const branch = git(cwd, ["rev-parse", "--abbrev-ref", "HEAD"]) ?? "(unknown)";
    const changed = dirty === "" ? 0 : dirty.split(/\r?\n/).length;
    block(
      "process-developer: the turn is ending with work that no remote has " +
        `(branch ${branch}: ${changed} changed path(s), ${unpushedCount} ` +
        "unpushed commit(s)). The caller removes this worktree after a failed " +
        "attempt, so anything not pushed is destroyed and the retry pays for " +
        "context, planning and critique a second time (tickets #22, #23; on " +
        "#140 this was 1979 insertions across 15 files). Commit everything on " +
        "the feature branch and `git -C <worktree_path> push -u origin " +
        "<branch>` BEFORE ending the turn — this holds for every ending, " +
        "including `blocked` and `failed`, not just the happy path. If the " +
        "state is genuinely not worth keeping, commit it anyway: a discarded " +
        "commit costs nothing, a lost implementation costs the whole attempt.",
    );
  }

  process.exit(0);
}

main().catch(() => {
  // Any unexpected error — fail-safe, do not block.
  process.exit(0);
});
