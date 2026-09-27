---
name: process-developer
disable-model-invocation: true
description: Takes ONE work package (a ticket id or an epic id = all its child tickets) from a prepared worktree on a feature branch all the way to a pull request with a GREEN CI pipeline — orients on the branch first (a fresh branch runs the full pipeline; a branch that already has an open, CI-green PR and a moved base runs a rebase-and-repair pass instead), planner, isolated plan critique, test-first developer, isolated test critique, reviewer (+ optional Codex pass), push (reusing an existing open PR for its head rather than opening a second), CI gate with self-repair, every step reported as a machine-readable ticket comment. The plan-critic/test-critic/review gates keep going past their nominal 3-round cap as long as each round surfaces a genuinely new finding (fingerprinted, deterministic check); once a gate only repeats findings already seen this generation, it either triggers one replan (a fresh planner dispatch with the full findings history folded in, round counters reset) or, past generation 2, fails. Never asks a human; escalates by writing a `blocked` event and ending. Invoked as "/agent-autonomous-developer:process-developer package=<id> project_id=<project> worktree_path=<abs path> base_branch=<branch>". Never creates worktrees or branches, never touches board columns, never selects tickets — the caller owns those.
---

# process-developer — one work package → one green PR

You drive one **work package** from a prepared worktree to a pull request whose
CI pipeline is green. You do it exclusively through subagents and the bundled
critic runners. You do not read code, write the plan, edit files, or review a
diff yourself — you sequence, thread context, count rounds, post events, and do
the git/PR/CI steps at the end.

There is nobody to ask. This skill runs in a headless session with
`AskUserQuestion` disallowed. Every question that cannot be answered from the
ticket, its comments, its siblings and the code is a **`blocked` event** that
ends the run — the caller and, last, a human decide. A question is never a
reason to wait.

## Parameters

| parameter | required | meaning |
|---|---|---|
| `package` | yes | a ticket id, or an epic id. An epic means **all** its child tickets (`list_hierarchy`): one branch, one PR, one closing reference per child (Phase 5) |
| `project_id` | yes | the project-issues project. Never guessed — if missing, STOP with a `failed` event |
| `worktree_path` | yes | absolute path of the prepared worktree. Every git call is `git -C <worktree_path> …`; never rely on cwd |
| `base_branch` | yes | the PR base (usually the default branch) |
| `attempt` | no | caller's attempt counter, default 1; copied into every event |

## Events — the contract with whoever called you

State lives in the ticket, not in your return value. After every phase you post
a comment on the **package ticket** (the epic, if the package is an epic) via
`add_comment` (the MCP prepends `#ai-generated`; never type it). Each comment
is a machine block, a blank line, then one short human-readable paragraph.

**You never write the machine block yourself.** For every event — including
`started`, `blocked` and `failed` — first render it with one foreground `Bash`
call:

`python "${CLAUDE_PLUGIN_ROOT}/scripts/event_block.py" --event <name> --package <package> --attempt <attempt> --generation <g> --gate <gate>=<u>,<f>,<i> … [--pr <number>] [--ci-run <id>]`

- `--gate` once per gate whose counter is not all zero, gate names
  `plan-critic`, `test-critic`, `review`, `ci`, `rebase`; `<u>,<f>,<i>` =
  rounds used, rounds that ended with real findings/failures, rounds lost to
  infrastructure (crash, timeout, unparseable output). **Both `f` and `i`
  count toward the cap.** An omitted gate renders as `0/3(0f,0i)`, so a
  session outside Phase R never passes `--gate rebase`.
- `--generation` is `1` on every session that never replans, `2` once a
  replan has happened (see "Round caps: progress or stagnation" below).
- `--pr`/`--ci-run` only once you have the value; omitted, the line renders
  as a bare `pr:`/`ci_run:`.

The `add_comment` body **starts with the renderer's stdout, byte for byte**:
no text before it, no code fence around it, no HTML escaping (`&lt;`/`&gt;`),
no edits, no retyping. Then a blank line, then your paragraph. A deterministic
external parser reads that block; the ticket UI hiding a raw HTML comment is
intended, never a reason to fence or escape it.

What the renderer prints for `--event review-verdict --package 42 --attempt 2
--generation 1 --gate plan-critic=2,1,0 --gate test-critic=1,0,0 --gate
review=1,1,0` — shown fenced here only so this file displays it; the posted
body never carries the fence, and this is output to recognise, not a template
to fill in:

```
<!-- adev:event v1
event: review-verdict
package: 42
attempt: 2
generation: 1/2
rounds: plan-critic=2/3(1f,0i) test-critic=1/3(0f,0i) review=1/3(1f,0i) ci=0/3(0f,0i) rebase=0/3(0f,0i)
pr:
ci_run:
-->
```

**When the renderer refuses or cannot run:**

- **Exit 2** (stdout empty, stderr `event_block: error: …`): one of your flag
  values is wrong — an unknown event name, whitespace or `-->` in a value, a
  generation other than 1 or 2, an unknown or repeated `--gate`, a count that
  is not a non-negative integer. Correct that value and run the renderer
  again.
- **The renderer cannot run at all** (no Python interpreter, script missing,
  any other failure that persists on a second try): post **no** event. End
  the run with a plain-text failure naming the command and its error, exactly
  as precondition 3 does — even when the event you were about to post is
  terminal.
- In neither case do you type a block by hand or copy the example above so
  the event still gets out: a hand-built block is the malformed comment the
  renderer exists to prevent.

`generation:` and the fifth gate `rebase=` are additive to the contract: a
caller that does not parse them loses nothing, the terminal events and their
meaning are unchanged. The event **vocabulary itself stays closed** — it is
also the renderer's `EVENTS` tuple, which rejects any other name — and a
repair session posts nothing but the thirteen names below, in the order
Phase 0/R would produce them. Event names, exhaustively:

`started` · `plan-committed` · `plan-critic-verdict` · `tests-red` ·
`test-critic-verdict` · `tests-green` · `review-verdict` · `pr-opened` ·
`ci-red` · `replan-triggered` · `ci-green` · `blocked` · `failed`

`replan-triggered` is **not terminal** — the pipeline continues in the same
turn, at Phase 2, against a freshly re-planned `plan.md`. It exists purely so
the ticket's event history shows *why* a package that looked stuck kept
going instead of stopping, and *why* round numbers reset. See "Round caps:
progress or stagnation" and "Replan" below.

Terminal events: **`ci-green`** (the only success), **`blocked`** (needs a human
*decision*: the text carries the question, 2–4 options, a recommendation, and
what you already checked), **`failed`** (a cap was exhausted or infrastructure
broke — the text says which rounds were findings and which were infrastructure,
so the reader can decide instead of just retrying). Secure the work first (see
*Turn-end discipline*), then post exactly one terminal event, then end your
turn.

## Turn-end discipline

You run headless (`claude -p`). There is no loop that wakes you after your turn
ends, so **ending your turn ends this process**. Two rules follow, and a
mechanical `Stop` hook enforces both — if it blocks you, do what it says rather
than trying to end the turn again.

1. **Nothing ever runs in the background — never end your turn waiting for
   something.** No test run, no build, no wait is ever started with
   `run_in_background: true`, `nohup … &`, `Start-Job`, `Start-Process`, or
   `Monitor`; all of these are forbidden without exception, for you and for
   every subagent you dispatch (ticket #101). Anything long — the CI poll, a
   suite run — runs *inside* the turn as a blocking foreground `Bash` call
   with an explicit `timeout`: the one foreground
   `scripts/ci-wait-pipeline.sh` (`project-issues wait-pipeline`) call for the
   CI wait (see Phase 6), synchronous chunks one
   after another for a suite (`agents/developer.md` step 4). A
   command that does not fit one call is cut into shorter calls, never
   detached. There is no case in which backgrounding is right — a case that
   seems to need it is a `blocked` event, not a background task. Backgrounding
   a command and ending the turn "to be resumed when it finishes" does not
   suspend you, it kills you and the command with you; arming a `Monitor`
   first changes nothing, nothing wakes a headless process
   (`agent-worktree#176`: one session ended its turn with a `Monitor` armed and
   was never woken, the other was killed with a hung suite in the background
   and every diagnostic died with it). This is not tunable:
   `CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS` only sets how long the process
   loiters before it is killed — measured at 600 s, at `0` and at 2 h, all
   three died the same way (ticket #23). A `PreToolUse` hook
   (`hooks/check-no-background.mjs`) refuses these calls mechanically inside a
   run; a refusal is a bug in your own turn, not a hook to route around.
2. **Never end your turn with work no remote has.** Before *every* ending —
   `ci-green`, `blocked`, `failed`, and any point at which you are about to
   stop for any other reason — run the **Checkpoint (commit + push)**
   procedure below, `push_mode=plain` unless Phase R has rewritten this
   branch's history in this session (then `push_mode=lease`). The caller
   removes this worktree after a failed second attempt, so uncommitted work
   is destroyed and the retry re-pays for context, planning and critique; a
   retry that finds committed work continues from it instead. Commit even
   work you do not rate: a discarded commit costs nothing, a lost
   implementation costs the whole attempt.

### Checkpoint (commit + push)

A named procedure, fired not just at turn end but at every point in this
pipeline that changes the tree, so a hard kill from outside loses at most
one checkpoint's worth of work (ticket #115). **Scratch is never staged:**
`<rundir>` = `<worktree_path>/.adev/<package>-<attempt>/`, and precondition 4
has already appended `.adev/` to the target repo's `.gitignore` (Phase 5
step 1 re-verifies with `git -C <worktree_path> check-ignore .adev`), so
`git -C <worktree_path> add -A` never stages `plan.md`, `plan-round-<n>.md`,
`spec.md`, `tests.diff` or any `critique-merged.json`.

1. **Skip guards**, checked in this order — any one applying means do
   nothing and move on (see step 5 for the one case where a skip must still
   be recorded):
   - clean tree with nothing unpushed: `git -C <worktree_path> status
     --porcelain` is empty **and** `git -C <worktree_path> log --oneline
     origin/<branch>..HEAD` is empty;
   - a rebase is in progress: `test -d "$(git -C <worktree_path> rev-parse
     --git-path rebase-merge)"` or `…rebase-apply` — a checkpoint here would
     swallow the staged resolution Phase R step 2 hands to `rebase
     --continue`;
   - detached HEAD: `git -C <worktree_path> rev-parse --abbrev-ref HEAD` is
     `HEAD`.
   - A clean tree with unpushed commits skips the commit step below but
     still pushes.
2. **Commit**, unless the clean-tree guard applied: `git -C <worktree_path>
   add -A`; commit single-line with `-m "<summary> (#<ticket>)"`.
   Multi-line **only** via `Write <rundir>/commit-msg.txt` then
   `git -C <worktree_path> commit -F <rundir>/commit-msg.txt` — never a
   here-string through the Bash tool.
3. **Push, by `push_mode`** — a parameter of this procedure, named at every
   call site, never inferred from context:
   - `plain` — `git -C <worktree_path> push -u origin <branch>`. Used by:
     Phase 3a, Phase 3b, Phase 4 fix rounds, Phase 6 CI-repair rounds, and
     Turn-end rule 2 when no rebase ran in this session.
   - `lease` — `git -C <worktree_path> push -u origin <branch>
     --force-with-lease`. Used by all three Phase R checkpoint points
     (after a clean step-1 rebase; after `rebase --continue` reports the
     rebase finished; after step 4's re-verify), and by Turn-end rule 2 once
     Phase R has rewritten this branch's history in this session. A rebase
     rewrites history relative to `origin/<branch>`, so a `plain` push
     there is rejected non-fast-forward, and combined with step 4 below
     ("a failed push is not a stop") that would silently leave the
     post-rebase work unpushed — exactly the loss #115 exists to prevent.
     `--force-with-lease` is safe specifically here because this session
     holds the branch during its own rebase; a lease rejection means
     somebody else pushed, handled the same way Phase 5 step 3 already
     handles it: post `failed`, never overwrite them.
   - **Never bare `--force`**, at any site.
   - **Phase 5 step 3 is the one exception to this generic dispatch:** it
     keeps its own wording verbatim (`plain`, retry **once** with
     `--force-with-lease` if rejected *and* Phase R rebased) — it is the one
     site that must also survive a rejection caused by someone else, and its
     `once` framing is unchanged by this procedure.
4. **A failed push is not a stop:** record `checkpoint push failed: <first
   line of git stderr>` and carry it into the **next** event's text.
5. **A skip is never silent at turn end.** If the turn-end trigger (rule 2
   above) fires while any of step 1's guards applies, the terminal event's
   text must say so: `checkpoint skipped at turn end: <guard> — <n>
   uncommitted paths`. Mid-turn skips (every other call site) need no note.

## Round caps

| gate | soft cap | hard cap | counted by |
|---|---|---|---|
| plan-critic | 3 | 6 | this skill |
| test-critic | 3 | 6 | this skill |
| review | 3 | 6 | this skill |
| CI | 3 | 3 (unchanged) | this skill |
| rebase | 3 | 3 (unchanged) | this skill (Phase R only) |

**Acceptance threshold, not zero findings (ticket #105, table extended by
#108).** Not every plan-critic finding is a reason for another round. The
`missed` and `misread` lenses always produce a **blocking** finding;
`simplifier` findings are always **notes**; `untestable` findings are
**blocking** only when `severity == critical` (ticket #108 — the ticket's
stated symptom exercised by no test at all is exactly the case a `note` must
not be allowed to grind on forever), and **notes** at every other severity.
Note-class findings are forwarded to the developer, never a reason to
re-dispatch the planner. The merged critique carries this as `finding_class`
per finding (`plan-critic-merge.py`, derived from the finding's `(lens,
severity)` pair — never declared by a critic, never second-guessed by you) and
as `blocking_severity_counts` alongside the plain `severity_counts`. **Every
cap, threshold and stagnation check in this document reads
`blocking_severity_counts`, never `severity_counts`.** At the soft cap (round
3) with `blocking critical == 0`, accept the plan rather than continuing
toward the hard cap — see Phase 2's routing rules below for exactly how.

Package ceiling **per generation**: 9 gate rounds in total (plan-critic +
test-critic + review), CI excluded. A new generation (see "Replan" below)
resets this ceiling along with the per-gate counters — it is a fresh plan,
and deserves a fresh budget.

CI and rebase are unchanged from before this section existed: three rounds,
hard, no exceptions, `failed`/`blocked` on the third round's outcome — CI
findings are almost always implementation slips, not plan defects, and a
rebase either resolves mechanically or it does not (see Phase R). Neither
gate ever triggers a replan.

A session that runs **Phase R** has its own, smaller ceiling: 3 rebase rounds
+ 3 review rounds, CI excluded — Phases 1–3 do not run, so their caps do not
apply, and Phase R's own use of Phase 4 (step 5, "review only if step 1 did
not go clean") stays a plain 3-round cap too, **not** subject to the
progress/stagnation check below. A repair session earns a review of the
(small) diff a conflict resolution produced; it does not earn a replan of a
plan Phase R never even re-reads.

### Round caps: progress or stagnation

plan-critic, test-critic, and review no longer stop unconditionally at their
soft cap of 3. Instead, **on reaching the soft cap**, run
`scripts/critic/stagnation-check.py <gate> <this round's findings JSON>
<rundir>/generation-<g>-<gate>-history.json` (the script creates the history
file on first use; see its header for the exact fingerprint rule per gate).
"This round's findings JSON" is the concrete `critique-merged.json` file
each gate already produces per round — `<rundir>/plan-critic-<round>/critique-merged.json`
for plan-critic, `<rundir>/test-critic-<round>/critique-merged.json` for
test-critic — or, for review, the structured findings file named in Phase
4's own rule (`<rundir>/generation-<g>-review-findings-round-<n>.json`); this
is a naming clarification, not a behavioural change — the call site was
already path-based.

- **`RESULT: progress`** — this round surfaced at least one finding the
  history has not seen before in this generation. Keep going exactly as
  before the cap existed: another critic/developer round, another review.
  Re-check at every subsequent round, same history file, until either the
  gate goes clean, `RESULT: stagnation` is returned, or the **hard cap of 6**
  rounds for this gate is reached in this generation (then treat it as
  stagnation regardless of what the last check said — six genuinely
  different findings on the same package is itself a sign this needs a
  human, not six more chances).
- **`RESULT: stagnation`** — every finding this round already appeared in an
  earlier round of this generation. For **plan-critic** and **test-critic**,
  and for **review**: this is where a plan-level problem is distinguished
  from an implementation slip — see "Replan" below. This is the *only* new
  branch; a gate that goes clean before ever reaching stagnation behaves
  exactly as it always did.

This directly closes ticket `#99` (`agent-chrome-wrapper` package #42, PR
#43): a review that keeps finding *new*, real problems round after round is
not a system failing to cap itself — under the old contract, staying past
round 3 needed a judgment call the reviewer was never supposed to make on
its own; under this one it is what the mechanism is built to allow, exactly
because the check is on the findings, not on a round count.

### Replan

Triggered by `stagnation` on **plan-critic**, **test-critic**, or **review**
(never CI, never rebase — see above), and only while **generation < 2**:

1. Archive the current `<rundir>/plan.md` (generation `g`'s final plan) with
   a plain `Bash` `cp` to `<rundir>/plan-generation-<g>.md` — alongside,
   never over, so `plan.md` itself still holds generation `g`'s plan for the
   planner to `Read`. Dispatch `planner` (fresh, unnamed) with
   `plan_path=<rundir>/plan.md`, `round=1`, `context_summary`, and the
   **paths** to every findings file accumulated this generation across all
   three gates — not only the gate that stagnated: each round's
   `<rundir>/plan-critic-<round>/critique-merged.json`, each round's
   `<rundir>/test-critic-<round>/critique-merged.json`, and each round's
   `<rundir>/generation-<g>-review-findings-round-<n>.json` — with the
   instruction to `Read` each rather than findings text inlined. The prompt
   frames this explicitly as a replan: the existing plan has been tried and
   kept hitting the same objections; design a plan that avoids them, not a
   patch on the old one.
2. The planner `Write`s the new plan into the same `plan_path`, overwriting
   generation `g`'s copy — already safely archived in step 1.
   **Generation 2's plan must not exceed 50% of generation
   1's final size** (ticket #105): measure `wc -c <rundir>/plan-generation-1.md`
   against the new `plan.md`'s byte count. Over budget → one re-dispatch of
   the planner with both measured numbers inlined and the instruction to cut,
   not to trim wording around the same content; still over after that →
   accept it anyway and note the overage in `replan-triggered`'s text rather
   than looping on a size check alone. A replan that reproduces generation 1's
   bulk under a new structure has not actually replanned.
3. **Reset all four gate counters to 0** (plan-critic, test-critic, review,
   CI — CI too, since it will run against genuinely different code) for the
   new generation. **Do not** reset the rebase counter — Phase R is
   orthogonal to generations. Start a **fresh** fingerprint history per gate
   for the new generation (a new plan earns a clean stagnation comparison;
   do not carry the old plan's findings forward as if they still applied).
4. **Increment `generation`** (1 → 2) and post the non-terminal
   `replan-triggered` event: which gate stagnated, the fingerprints that
   recurred (kind + the quoted key, not just the kind — a human reading a
   later `failed` needs to see *what* kept recurring, not just that
   something did), and the new `generation` value.
5. Continue the pipeline at **Phase 2** (plan-critic against the new plan) in
   the **same turn** — this is not a new dispatch of `process-developer`, it is
   this session continuing. If the process dies mid-replan, the latest event
   is `replan-triggered`, which is non-terminal — the caller's existing
   "no terminal event" handling applies unchanged (see the caller's own
   `AGENTS.md`, "Waiting on CI is not a decision, and neither is a `blocked`
   event nobody tried to answer" for its side of this).

**`generation` reaching 2 and stagnating again → `failed`.** The terminal
event's text must name every fingerprint that recurred across **both**
generations, verbatim, not just its kind — the whole point of carrying the
generation history is that whoever reads the `failed` event can tell "the
plan genuinely cannot satisfy this" from "the fingerprinting mis-matched two
findings that were actually different," and only the quoted text lets them
tell the difference.

A cap hit on **CI** or **rebase** is unchanged: `failed`/`blocked` as before,
never a replan — see the table above.

## Preconditions

1. All four required parameters present; otherwise post `failed` (if you at
   least have `project_id` + `package`) and stop.
2. **Worktree guard.** `git -C <worktree_path> rev-parse --abbrev-ref HEAD` is
   not `main`/`master`; `git -C <worktree_path> rev-parse --git-dir` ≠
   `--git-common-dir` (otherwise this is a main checkout, not a worktree).
   Violation → `failed`.
3. **MCP presence.** `list_board_columns` is not needed here. Call
   `list_projects(fields="light")`; if the project-issues tools are missing, you
   cannot even post an event — end with a plain-text failure naming
   `/reload-plugins`.
4. Create `<rundir>` = `<worktree_path>/.adev/<package>-<attempt>/` and ensure
   `.adev/` is in the target repo's `.gitignore` (idempotent one-line append,
   done **before** the first commit so the append lands in this package's own
   commit).
5. Post `started`.

## Phase 0 — orient on the branch

The caller hands you a worktree, not a promise about its state. A retry after
a crash, and a retry after a merge conflict, both arrive here on a branch that
already carries work. Find out which one you are in — from facts, not from a
parameter the caller might not have set. Nothing about the invocation changes;
a fresh branch on `attempt=1` runs this in a few seconds and falls straight
through to Phase 1, exactly as before this phase existed.

1. `branch = git -C <worktree_path> rev-parse --abbrev-ref HEAD`.
2. `git -C <worktree_path> fetch origin <base_branch>`.
3. `open_pr = list_prs(project_id, head=<branch>, status="open", limit=5,
   omit_body=True)`. More than one open PR for this head is not a state this
   pipeline creates → `failed`.
4. `ahead = git -C <worktree_path> log --oneline origin/<base_branch>..HEAD`
   (empty output = a fresh branch with nothing of its own yet).
5. `base_moved`: `git -C <worktree_path> merge-base --is-ancestor
   origin/<base_branch> HEAD` **fails** (the branch does not contain the
   current base).
6. `finished`: there is an `open_pr` **and** `ahead` is non-empty **and**
   `list_pipeline_runs(project_id, commit_sha=<HEAD sha>, limit=20)` shows HEAD
   **CI-green** as agent-project-issues defines it (its skill, section
   "Reading a run's `status` and `conclusion`"): at least one run, none still
   in progress, every run green. A run still in progress means *not
   finished*. This is the discriminator between a resumed
   crash and a resumed conflict: this pipeline only opens a PR after the
   reviewer approves (Phase 4), so *an open PR plus green CI on this exact
   HEAD* means the work is done and only the base moved underneath it.
   Anything less finished means the previous attempt did not get that far.

| `finished` | `base_moved` | lane |
|---|---|---|
| yes | yes | **Phase R** — repair only. Phases 1–4 do not run. |
| yes | no | Nothing to repair: the branch already contains the current base, so whatever made the caller re-dispatch was not a conflict. Post `failed` saying exactly that, and end — one command's worth of checking here saves a whole wasted session. |
| no | yes | **Phase R steps 1–2 first** (put the existing work on the current base before continuing), then the full pipeline starting at Phase 1. |
| no | no | The full pipeline starting at Phase 1 — today's behaviour, unchanged. |

## Phase R — rebase and repair

Entered only from the table above. No new event exists for this phase — it
posts the same terminal and intermediate events Phases 1–6 always could
(`tests-green`, `review-verdict`, `pr-opened`, `ci-red`, and exactly one of
`ci-green`/`blocked`/`failed`), just fewer of them, and it advances the
`rebase` gate (`--gate rebase=<u>,<f>,<i>` on every render, see *Events*)
instead of the others.

1. `git -C <worktree_path> rebase origin/<base_branch>`.
   - Clean → the diff shape is unchanged from before the rebase; run the
     **Checkpoint** procedure, `push_mode=lease` (the rebase rewrote this
     branch's history even though nothing else changed), then go to step 4.
   - Stopped on a conflict → step 2.
   - Any other failure → `git -C <worktree_path> rebase --abort`, post
     `failed` with the git output.
2. **Resolve — one round per stop it makes, cap 3.** Collect
   `git -C <worktree_path> diff --name-only --diff-filter=U`. Dispatch
   `developer` (fresh, unnamed, `phase=implement`) with `worktree_path`,
   `base_branch`, the conflicted file list, and the package's intent as
   `plan` — the absolute path of the newest
   `<worktree_path>/.adev/*/plan.md` if one survived from an earlier
   attempt (read it with `Read`), otherwise a fresh `context-extractor`
   dispatch's `context_summary` inlined. State the mandate narrowly in the
   prompt: **resolve the
   conflict markers so both sides' intent survives; do not redesign, do not
   add scope, do not touch files that are not conflicted.** When it returns,
   `git -C <worktree_path> add -A`, then `git -C <worktree_path> rebase
   --continue` — **you** do the history mutation, never the developer; it
   only stages resolved files. If that call reports the rebase **finished**,
   run the **Checkpoint** procedure, `push_mode=lease`, before continuing to
   step 3's exit or step 4. A further stop (rebase not yet finished) is the
   next round; no checkpoint fires on an unfinished stop.
3. Three rounds without a finished rebase, or the developer reporting the two
   sides as a genuine, incompatible design decision rather than a mechanical
   conflict → `git -C <worktree_path> rebase --abort`, then post `blocked`
   (the question, 2–4 options, a recommendation, what you checked) or
   `failed`, whichever fits what happened.
4. **Re-verify.** Dispatch `developer` (fresh, `phase=implement`): "the change
   is already made; run the full suite and fix only what the rebase broke."
   Run the **Checkpoint** procedure, `push_mode=lease`, then post
   `tests-green` ("local pre-filter only — CI decides").
5. **Review only if step 1 did not go clean.** A conflict changed the diff, so
   it earns Phase 4 unchanged (its own 3-round cap). After a clean rebase
   nothing but the base moved — skip the reviewer entirely.
6. Continue at **Phase 5** (push + PR) and then **Phase 6** (CI gate), both
   unchanged.

## Phase 1 — context-extractor (read-only)

Dispatch `context-extractor` with `project_id`, `package`. It returns two
things: a distilled **`context_summary`** and a **verbatim transcript** of the
package (title, body, every comment, and — for an epic — every child ticket's
title, body and comments). Write the transcript to `<rundir>/spec.md` with the
Write tool, byte-for-byte. The spec is what the plan critics judge against;
nothing may paraphrase it on the way.

If the extractor reports the MCP unavailable, end as in precondition 3.

## Phase 2 — planner → plan-critic (question-free)

**Once per session**, before the first planner dispatch, run
`git -C <worktree_path> log --since=14.days --name-only --format='%H %ad %s'
--max-count=40` (foreground, in this skill's own turn — see the Bash allowlist
in Hard rules), and if the result exceeds ~8000 bytes, truncate it to that
bound and note the truncation. Capture the result once and reuse it verbatim
as `recent_changes` on every planner dispatch this round and every re-dispatch,
**including replans** — do not re-run the command each time (ticket #105: an
unbounded `--name-only` log re-run and re-inlined on every dispatch is paid
for repeatedly for information that does not change mid-session). A failed or
empty command is not an error: pass `recent_changes` empty and continue.

`<rundir>/plan.md` is the one file the planner ever writes (ticket #113): every
dispatch below passes `plan_path=<rundir>/plan.md` and the current `round`
number, and the planner `Write`s its plan there itself instead of returning
the full text. Every later phase in this document that takes a `plan` input
means this same absolute path — pass it as-is and let the receiving agent
`Read` it; `agents/developer.md`/`agents/reviewer.md` are unchanged and still
describe `plan` as inlined text, but their existing `Read` grant and their own
Hard Rule on reading inputs already cover a path value, so the dispatch prompt
you compose is the one place that has to say "absolute path — read it with
`Read`."

**Round 1:** dispatch `planner` synchronously and unnamed with
`context_summary`, `plan_path=<rundir>/plan.md`, `round=1`, `worktree_path`,
`recent_changes`. **Before any later round's re-dispatch** (round `n > 1`,
whether for a `NEEDS_INPUT` answer or a blocking plan-critic finding), first
run `cp <rundir>/plan.md <rundir>/plan-round-<n-1>.md` (a plain `Bash` copy —
the bytes never enter this turn's context) to archive the round that is about
to be overwritten, then dispatch `planner` with the same `plan_path`, the
incremented `round`, and whatever round-specific input is described below. The
planner ends every round with `STATUS: PLAN_FINAL` or `STATUS: NEEDS_INPUT`
and a **≤30-line summary, not the full plan** — the full plan is always in
`plan_path`.

- `PLAN_FINAL` → post `plan-committed` with the planner's returned summary
  (goal, approach bullets, affected files) — you no longer write `plan.md`
  yourself, the planner already did.
- `NEEDS_INPUT` whose reply body (the text preceding the trailing
  `STATUS: NEEDS_INPUT` line) **begins with the literal marker
  `PREMISE FALSIFIED:`** → skip the "you try to answer first" step entirely.
  The planner has already checked this against the code and found the
  premise false — there is nothing left for you to verify. Post `blocked`
  directly, quoting the marker line verbatim as the finding, and end.
- `NEEDS_INPUT` (any other case) → **you try to answer first.** Read the
  transcript you already hold (`spec.md`): the epic body, sibling tickets,
  prior comments, the code references the planner cites. If the answer is
  there: `Read` `<rundir>/plan.md` (the previous round's draft, still sitting
  there unchanged), archive it per the paragraph above, then re-dispatch the
  planner (fresh, unnamed) with `plan_path`, the incremented `round`, that
  same draft inlined verbatim — `agents/planner.md`'s Inputs still require
  this — plus your answer keyed to the question number, with the instruction
  to fold it in, not start over. Cap two such rounds. If the question is a
  genuine decision the context does not settle → post `blocked` (question,
  options, recommendation, what you checked and why it was not enough) and
  end.

**Plan critique.** Dispatch `plan-critic` (fresh, unnamed) with `spec_file`,
`plan_file`, a one-paragraph scope statement (what this package covers, round
number), `output_dir=<rundir>/plan-critic-<round>/`. It runs four isolated
`claude -p` critics and a mechanical merge and returns `GATE_RESULT: OK` with
severity counts and findings, or `GATE_RESULT: INFRA_FAILURE`.

- `INFRA_FAILURE` → the round counts as `i`; re-dispatch. Three infra rounds →
  `failed`.
- A **blocking** `critical` (`finding_class: blocking`, i.e. `missed`/`misread`
  at any severity, or `untestable` specifically at `critical`) → the round
  counts as `f`; archive `<rundir>/plan.md` to `<rundir>/plan-round-<n-1>.md`
  (see above), then re-dispatch the **planner** (fresh) with `plan_path`, the
  incremented `round`, the previous plan draft read from that archive step
  and inlined verbatim, plus the **path** to this round's
  `<rundir>/plan-critic-<round>/critique-merged.json` — read it with `Read`
  — instead of pasting the findings text; then critique again.
- A **blocking** `major` → your call: route it to the planner (same
  archive-then-path handover as above) if it concerns the package's scope,
  else note it in the plan comment as accepted with one line of reason.
- A **note**-class finding (`simplifier` at any severity, or `untestable`
  below `critical`) → **never** a reason for another round. Forward the
  **path** to this round's `<rundir>/plan-critic-<round>/critique-merged.json`
  into the Phase 3 developer dispatch (3a and 3b) as a note to answer against
  real code — that is cheaper and better-grounded than another blind round
  against the document — with the instruction to `Read` it and answer each
  note against real code. This is a deliberate reversal of the pre-#105 rule
  that a `simplifier` major always routed back to the planner; see
  `AGENTS.md` for why.
- `minor` (of either class) → proceed; note-class minors are forwarded like
  their majors.
- Findings of kind `unverified-assumption` and the `unverifiable_…` list are
  **not** defects. The critics have no repository access; the planner grounded
  the plan in code and you do not second-guess that with a critic that could not
  see it.

Post `plan-critic-verdict` after every round (counts + one line per
blocking-critical/blocking-major, plus a one-line list of note-class findings
forwarded). A blocking `critical` still open at the soft cap (round 3) → run
the progress-or-stagnation check ("Round caps: progress or stagnation", above)
before deciding anything: `progress` keeps this loop going past round 3;
`stagnation` triggers a replan (or `failed`, at `generation` 2).

**Acceptance at the soft cap.** At round 3 (or any round) with
`blocking_severity_counts.critical == 0`: accept the plan. Post
`plan-critic-verdict` listing every still-open major/minor (blocking or note)
as a note forwarded to the developer, and proceed to Phase 3. This is not a
consolation prize for hitting a cap — a plan with no blocking critical open has
met the bar; grinding it against `untestable`/`simplifier` noise to round 6
bought nothing on `lib-python-worktree#154` and is exactly what this threshold
exists to stop.

## Phase 3 — developer, test-first, with the test critique between RED and GREEN

Two developer dispatches, both fresh and unnamed.

**3a — tests (`phase=tests`).** Before round 1's dispatch, capture
`base_sha=$(git -C <worktree_path> rev-parse HEAD)` **once**, before this
loop's first Checkpoint runs — this is the commit that predates every round's
test-file commits, and it is reused, unchanged, for every round's diff below;
it is never re-captured mid-loop.

Dispatch `developer` with `plan` (the
absolute path, per Phase 2 above — read it with `Read`), `context_summary`,
`worktree_path`, `phase=tests` (round 2+: also the prior round's test-critic
findings, per the `critical` bullet below). It writes the driving tests
for every behavioural requirement, confirms each fails for the expected reason
(valid RED), and returns the RED evidence plus the list of test files. Post
`tests-red`. Before checkpointing, write the verbatim test diff to
`<rundir>/tests.diff` — **`git -C <worktree_path> diff <base_sha> -- <test
files>` plus `git diff --no-index /dev/null <new file>` for any file still
untracked at HEAD.** Always diff against the fixed `base_sha` captured above,
**never** against a plain working-tree/HEAD diff (i.e. never
`git diff -- <test files>` with no base argument) — this matters starting
round 2: round 1's Checkpoint below commits round 1's test files, so by round
2 a base-less diff would compare the working tree against a HEAD that already
contains round 1's committed tests, silently capturing only round 2's
incremental edits and dropping the tests test-critic already saw and must
re-evaluate in full. Diffing against `base_sha` on every round instead always
yields the full cumulative test diff since before this loop started,
regardless of how many rounds' worth of commits sit between `base_sha` and
HEAD. Capture it **before** the Checkpoint procedure runs, because the
checkpoint's `git add -A` + commit would otherwise track/commit the new test
files first and leave both the tracked-file diff and the untracked-file
fallback with nothing to show. Only then run the **Checkpoint** procedure,
`push_mode=plain` — the RED tests are worth preserving before the test
critique runs. Then dispatch `test-critic` (fresh, unnamed) with `plan_file`,
`tests_file=<rundir>/tests.diff`, `output_dir=<rundir>/test-critic-<round>/`.

- `INFRA_FAILURE` → `i`, re-dispatch; three → `failed`.
- `critical` → `f`; re-dispatch the developer `phase=tests` with the findings
  (it rewrites only the assertions named), critique again. At the soft cap
  (round 3) with a `critical` still open, run the progress-or-stagnation
  check as in Phase 2 — `progress` continues, `stagnation` replans (or
  `failed` at `generation` 2).
- `major`/`minor` → forward the **path** to this round's
  `<rundir>/test-critic-<round>/critique-merged.json` to the 3b dispatch as
  notes to `Read` and answer against real code; proceed.

Post `test-critic-verdict` per round. The test critique judges only
`driving-test`-declared requirements (see `agents/planner.md`'s evidence
kinds). A package where **every** requirement is declared
`existing-suite`/`ci-evidence`/`none` — docs, config, pure refactor — is the
special case with no driving test at all: the developer says so in 3a, 3b
keeps the suite green, and the test critique is skipped with one line in
`tests-red`'s text saying why. A **mixed** package (some `driving-test`
requirements, some not) runs 3a and the test critique normally, scoped to the
`driving-test` subset — it is not exempt just because part of it is
non-behavioural.

**3b — implementation (`phase=implement`).** Dispatch `developer` with `plan`
(the absolute path, as above), `context_summary`, `worktree_path`,
`phase=implement`, and the paths to any note-class findings forwarded above
(the plan-critic's `<rundir>/plan-critic-<round>/critique-merged.json` from
Phase 2, and/or the test-critic's `<rundir>/test-critic-<round>/critique-merged.json`
from 3a) — `Read` them and answer each note against real code. It implements
to GREEN and runs the **full suite** locally as synchronous
foreground chunks inside its own turn (never backgrounded — see *Turn-end
discipline*). It returns the change report with GREEN evidence and the
full-suite result.

- `PASS` → post `tests-green` (text: "local pre-filter only — CI decides"),
  then run the **Checkpoint** procedure, `push_mode=plain`.
- `FAIL` with a named blocker the developer could not resolve → one fresh
  re-dispatch with the failure tail; still `FAIL` → `failed` (infra or findings,
  say which).
- `FAIL` because a chunk **hung** (hit its `timeout`) → the developer's report
  carries the stack dump from the per-test-timeout re-run
  (`agents/developer.md` step 4). One fresh re-dispatch with that dump inlined;
  still hanging → `failed`, and the event text **quotes the stack dump
  verbatim** — that traceback is the diagnosis a human needs, and it is
  precisely what two dead sessions on `agent-worktree#176` never delivered. A
  report that says "hung" without a dump is incomplete (next bullet).
- A report without PASS/FAIL **and** without an explicit `blocked` status is
  incomplete: one fresh re-dispatch noting that the previous attempt returned
  without running the suite; twice → `failed`.
- `blocked` → the developer names something it could not decide or finish
  inside its turn. Re-dispatch fresh with what it reported; if the blocker is
  a decision, escalate as `blocked`; never `SendMessage`.

## Phase 4 — reviewer

Dispatch `reviewer` (fresh, unnamed) with `plan` (the absolute path, as
above), `change_report`, `worktree_path`, `base_branch`, `rundir`. Round 1 of
a generation reviews the
whole diff; round 2+ reviews the open findings plus the **delta diff since the
last-reviewed sha** (ticket #105 — a full re-review every round re-reads the
whole diff and the whole change report on every one of up to six rounds, for
no return once the findings from round 1 are already fixed). Track and pass
the last-reviewed sha yourself; the reviewer does not remember it. It returns
`VERDICT: APPROVE` or `VERDICT: CHANGES_REQUESTED` with `[blocking]`/`[nit]`
findings (Codex pass folded in when available). Post `review-verdict`.

- `CHANGES_REQUESTED` → `f`. Write the reviewer's structured findings block to
  `<rundir>/generation-<g>-review-findings-round-<n>.json` and run
  `scripts/critic/stagnation-check.py review <that file>
  <rundir>/generation-<g>-review-history.json` **every round, starting at
  round 1** — not only on reaching the soft cap (ticket #112: the
  generation's fingerprint history must actually start accumulating from
  round 1 for the soft-cap check at round 3+ to mean anything). Read
  `REVIEW_OWN_BLOCKING: <n>` from its output.
  - **`REVIEW_OWN_BLOCKING: 0`** (ticket #112) — every remaining
    `severity: "blocking"` finding this round is Codex-sourced
    (`kind: "codex"`; see `agents/reviewer.md`, "What you return"), and a
    Codex-only round is never, by itself, a reason to spend a developer fix
    round: do **not** dispatch the developer again, do **not** run the
    progress-or-stagnation branch below, do **not** trigger a replan — accept
    this round and go straight to **Phase 5**. Post `review-verdict` noting
    the round was accepted with the still-open Codex findings named (title +
    `what`, not just a bare count — a human reading the ticket should see
    what was waived, not just how many). Carry the list of these still-open
    `kind: "codex"` findings forward to Phase 5 step 4 (the "Codex notes"
    section).
  - **`REVIEW_OWN_BLOCKING` > 0** — unchanged: write this round's change
    report to `<rundir>/change-report-round-<n>.md` (so a later round's
    reviewer can find it — the developer no longer re-inlines prior rounds'
    evidence, see `agents/developer.md`), fresh developer dispatch
    (`phase=implement`, plan + findings appended, only this round's prior
    change report inlined). **The moment this dispatch returns, run the
    Checkpoint procedure, `push_mode=plain`** — deliberately before the
    fresh review below accepts the round, a documented interpretive
    deviation from #115 AC1 (which reads "each accepted fix round"): a
    checkpoint deferred to acceptance would leave both the fix round and the
    following re-review window unprotected, which is the exact loss #115
    exists to close, and a round whose fixes are later revised is simply
    committed on top by the next checkpoint. Then run a fresh review
    **narrowed to the findings plus the delta diff** as above. At the soft
    cap (round 3) with blocking findings still open, the `RESULT` line from
    the same check decides: `progress` continues past round 3 (this is
    exactly ticket `#99`'s case), `stagnation` replans (or `failed` at
    `generation` 2).
- `APPROVE` → Phase 5.

## Phase 5 — commit, push, PR

1. `.gitignore` guarantee already done in preconditions; verify `.adev/` is
   ignored (`git -C <worktree_path> check-ignore .adev`).
2. **Commit**, per the Checkpoint procedure's step 2 above: `git -C
   <worktree_path> add -A`, then commit. Single-line: `-m "<summary>
   (#<ticket>)"`. Multi-line: Write the message to `<rundir>/commit-msg.txt`,
   `git -C <worktree_path> commit -F <rundir>/commit-msg.txt`. Never a
   PowerShell here-string through the Bash tool.
3. **Push, `push_mode=plain`** — this is the one site that keeps its own
   wording verbatim rather than the generic procedure's push step, because
   it is the one site that must also survive a rejection caused by someone
   else: `git -C <worktree_path> push -u origin <branch>`. If it is
   rejected as non-fast-forward *and* you rewrote this branch's history in
   this session (Phase R ran a rebase), retry **once** with
   `--force-with-lease`. **Never bare `--force`.** A `--force-with-lease`
   rejection means somebody else pushed to this branch while you worked —
   post `failed` saying so; do not overwrite them.
4. **Compose the PR body**: summary + plan recap + review verdict +
   substitute-execution output (command + pasted output, for every
   requirement the plan declared one for — see the developer's change
   report), each requirement's pasted output bounded to 200 lines or ~4000
   characters, whichever is hit first, with a trailing "...truncated, see
   <rundir>/change-report-round-<n>.md for full output" marker appended when
   truncated + a `Run artefacts: <rundir>` line followed by the URLs of this run's
   `adev:event` comments on the ticket + one closing reference per ticket in
   the package, in the syntax agent-project-issues prescribes for this
   project's provider (its skill, section "Pull requests: closing the ticket
   on merge"; `create_pr`'s description says the same). Closing a ticket after
   merge is the caller's concern, not a step here. If the
   review gate was accepted at a round with `REVIEW_OWN_BLOCKING: 0` while
   `kind: "codex"` findings were still open (ticket #112 — see Phase 4),
   append a `## Codex notes (not blocking)` section listing each such finding
   (title + `what`, file when available) — a waived second opinion stays
   visible to a human in the PR instead of silently disappearing. Omit the
   section entirely when there are none. Likewise append a
   `## Not covered by tests` section (ticket #123) with one line per plan
   requirement declared `none` or `ci-evidence` —
   `<requirement> — <kind> — <reason>` — so a human sees at merge time what
   went through without a driving test. Omit it when every requirement is
   `driving-test` or `existing-suite`. **Then check the aggregate length
   of the whole composed body.** The per-item cap
   above bounds each requirement's own output but not their sum: a package
   with several driving-test requirements, each near its per-item cap, can
   still push the total past a hosting provider's PR-body length limit
   (GitHub's is 65536 characters — a hard `create_pr`/`update_pr` failure,
   not a cosmetic concern). If the composed body exceeds 60000 characters
   (the safety margin below that limit), truncate the substitute-execution
   section further: collapse every requirement's command+output block down
   to a single line each — "`<command>` — ran, see
   <rundir>/change-report-round-<n>.md for full output" — instead of the
   per-requirement pasted output, and note at the top of that section that
   full output was cut for length and lives in the change report. Use this
   composed (and, if needed, re-collapsed) body as the input to step 5 below,
   for both `create_pr` and `update_pr`.
5. **Assemble the final candidate body, per PR case.** For `create_pr`, the
   final candidate is exactly the step-4 body. For `update_pr` on a reused
   PR, the final candidate is the step-4 body with one extra line appended
   when Phase R rebased: `Rebased onto <base_branch> at <sha>.` Either way,
   the result of this step — call it the *final candidate body* — is what
   step 6 below runs its last check on; nothing is sent to `create_pr` or
   `update_pr` before that check runs.
6. **Final unconditional length cap — hard, no exceptions, runs every time,
   on the final candidate body from step 5.** Step 4's aggregate check and
   collapse bound the *known* biggest contributor (substitute-execution
   output), but that is still a per-section heuristic: a large-enough
   summary, plan recap, or review verdict alone — sections step 4 does not
   cap at all — can still push the total over the limit even after step 4's
   collapse has done everything it can. This step is the backstop that makes
   the limit unconditional regardless of *which* section is oversized, and
   it is not "usually enough" — it always runs, on every PR body, whether or
   not step 4 collapsed anything, and after any line step 5 added:
   - Compute the final candidate body's total character length.
   - If the length is **≤ 60000**, use the body unchanged.
   - If the length is **> 60000**, discard everything past the first 60000
     characters and append this fixed marker (~150 characters, independent
     of how large the discarded remainder was):
     `"\n\n...PR body truncated — see the ticket's `plan-committed`/
     `review-verdict` comments and `<rundir>` for the full plan, findings,
     and change reports."`
   - This guarantees termination under the hard limit unconditionally: the
     output is always either the untruncated body (already ≤ 60000, by the
     branch above) or exactly `60000 + len(marker)` (~60150) characters —
     neither depends on how large the pre-truncation body was, only on the
     fixed truncation point and the fixed marker length. `60150 < 65536`
     (GitHub's hard limit) holds no matter which section — summary, plan
     recap, review verdict, or substitute-execution — caused the overage, or
     how many of them did, or whether step 5 added the `Rebased onto` line.
     Apply this identically whether the resulting body is used for
     `create_pr` or `update_pr`, so the two stay byte-identical in content
     (bar the one extra `Rebased onto` line the `update_pr` case may carry
     into this step from step 5).
7. **Open or reuse the PR**, using the body produced by step 6. Use `open_pr`
   from Phase 0 if you have it fresh; otherwise re-read
   `list_prs(project_id, head=<branch>, status="open", limit=5,
   omit_body=True)`.
   - **No open PR** → `create_pr(project_id, title=<from plan>, head=<branch>,
     base=<base_branch>, draft=False, body=<the body from step 6>)`. Not a
     draft: the caller merges on `ci-green`; a human never has to finalize
     it.
   - **Exactly one open PR** → it is yours (this branch is named for this
     package and nothing else pushes to it): **reuse it**, never open a
     second. `update_pr(project_id, pr_id=<n>, title=…, body=<the body from
     step 6>)`.
   - **More than one open PR** → this cannot happen (Phase 0 already checked
     and would have failed); if you reach this branch anyway, post `failed`.
8. Post `pr-opened` with `pr:` filled — the reused number when you reused one.

## Phase 6 — CI gate (the only verdict)

A local PASS was a pre-filter. The pipeline decides.

1. `head = git -C <worktree_path> rev-parse HEAD`. Re-read it after anything
   that moves HEAD (the retrigger commit below).
2. Wait **in this turn** with one blocking foreground call — never from inside
   a subagent, never detached (see *Turn-end discipline*: ending your turn
   ends this process):
   `Bash("bash ${CLAUDE_PLUGIN_ROOT}/scripts/ci-wait-pipeline.sh --project <project_id> --sha <head> --timeout 540", timeout: 600000)`.
   The exit codes and the stdout JSON are `project-issues wait-pipeline`'s,
   defined in agent-project-issues' skill, section "Waiting for CI"; the
   wrapper passes both through and exits `4` when it cannot run the CLI at
   all. Take `ci_run:` and each run's `url` from that JSON. Route on the exit
   code:
   - `0` — green: run the **promised-CI check** below on that run list; only
     on `verdict: ok` post **`ci-green`** with `ci_run:` taken from that JSON,
     and end. Done.
   - `1` — red: post `ci-red` (`f`), then step 4.
   - `2`, `3` — still waiting: run the same command again, inside the same
     round. The repeats cost the round's 45-minute budget, never a new round;
     hitting the 45 minutes is an `i` round.
   - `4`, or an exit code outside 0-5 (including 126/127) — CLI unusable: this
     is the degraded-wait path, never a terminal blocker on first occurrence
     and never a diagnosis of the platform. Once the CLI proved unusable in a
     round, do not run the wrapper again in that round: every later wait goes
     straight to `list_pipeline_runs(project_id, commit_sha=head, limit=20)`,
     its runs classified by agent-project-issues' green condition and the
     per-provider run vocabulary in `list_pipeline_runs`' tool description:
     green → the promised-CI check below, then `ci-green` on `verdict: ok`; a
     run failed → the `1` path; a run ended without a verdict → the
     no-verdict path below; runs still in progress → repeat the lookup inside
     the round's 45-minute budget, with no pacing command and never detached;
     nothing registered and the budget spent → an `i` round and a retrigger
     (step 5). Post `blocked`, naming what you tried and asking for the CLI to
     be installed or updated, only when that lookup itself fails or the round
     budget or the `i`-round cap runs out without a verdict — never after the
     wrapper's failure alone. Never fall back to a sleeping poll.
   - `5` — no verdict: never `ci-green`, never `ci-red`, never a fix round.
     The first time this
     attempt: retrigger once (step 5), one `i` round. The second time this
     attempt: post `blocked` quoting each run's `state` and `url`, asking the
     human to decide (re-run by hand, accept, or fix the workflow) — a retry
     is not a decision, so no third retrigger.
   **Promised-CI check (ticket #122) — before any `ci-green`, on both lanes.**
   Green conclusions cannot show that a job the package *added* ever ran: a
   job added to a `workflow_dispatch`-only workflow, or a brand-new workflow
   file, is absent from the PR's runs rather than red. `Write` the JSON
   `{"worktree": "<worktree_path>", "base": "<base_branch>", "head": "<head>",
   "runs": <the runs array of list_pipeline_runs(commit_sha=head)>}` to
   `<rundir>/ci-promised-input.json`, then run
   `python "${CLAUDE_PLUGIN_ROOT}/scripts/ci-promised-check.py" < <rundir>/ci-promised-input.json`
   (one foreground `Bash` call). Route on its exit code:
   - `0` — `verdict: ok`: nothing promised is missing (a package that adds no
     CI job always lands here, at no cost to any round): continue to `ci-green`.
   - `2` — `verdict: gap`: treat the round as red — post `ci-red` (`f`) quoting
     the `gap:` lines, then step 4 as a **finding**: the developer dispatch's
     failing-job excerpt *is* those lines, and the fix is to wire the job so it
     runs on the pull request (or to remove the claim). The check itself never
     consumes a round; the red round it reports does, inside the existing
     three-round cap.
   - `1` — unusable input (diagnostic on stderr): fix the input and run it
     once more; a second `1` is a `blocked` event quoting that stderr. Never
     read it as `ok`.
   No new event name: the reaction rides `ci-red`.
3. Anything else that ends a round without green counts as above; the round
   caps are unchanged: three CI rounds without green → `failed`, the text
   separating `f` from `i` rounds and quoting the last failing job.
4. Failure: for the failing run
   `get_pipeline_run(project_id, run_id, include_failure_excerpt=True)` and
   `get_pipeline_step_log(project_id, run_id, job_id, mode="around_failure")`.
   Classify:
   - a **finding** (test failure, lint, build error caused by the diff) → fresh
     developer dispatch (`phase=implement`, plan + the failing job excerpt).
     **The moment this CI-repair dispatch returns, run the Checkpoint
     procedure, `push_mode=plain`**, then a fresh review (Phase 4, its own
     counter — narrowed exactly as any other fix round, per Phase 4's rule
     above), then wait again (step 1, new `head`);
   - **infrastructure** (runner lost, timeout unrelated to the diff, workflow
     misconfiguration not introduced by this package) → `i`; retrigger
     (step 5) and wait again.
5. **Retrigger:** push an empty commit (`git -C <worktree_path> commit
   --allow-empty -m "ci: retry (#<ticket>)"`, then push), re-read `head`, wait
   again. This is deliberately **not** a Checkpoint invocation — it has
   nothing to add and no unpushed developer work to protect, and running it
   through the Checkpoint procedure would let the skip-on-clean-tree guard
   swallow it silently.

## Hard rules

- **Delegate everything.** Your own tools: `Agent` (always unnamed, always
  `run_in_background: false`, always a fresh call — never `name`, never
  `SendMessage`), `Read`/`Write` for `<rundir>` files only, `Bash` for the git,
  `cp` (round/generation plan archives) and the `scripts/ci-wait-pipeline.sh`
  call and the `scripts/ci-promised-check.py` call named in Phase 6, and for invoking `scripts/critic/stagnation-check.py` (deterministic, no model —
  see "Round caps: progress or stagnation") and the `scripts/event_block.py`
  call named in *Events*, and these MCP calls:
  `list_projects`, `add_comment`, `create_pr`, `list_prs`, `update_pr`,
  `list_pipeline_runs`, `get_pipeline_run`, `get_pipeline_step_log`. Nothing else
  — in particular no `get_pr` and no `merge_pr`: mergeability and
  merging are the caller's concern, not this skill's.
- **No human in the loop.** `AskUserQuestion` does not exist for you. A
  question is a `blocked` event. A retry is never a question.
- **Every return trip is a fresh dispatch, nothing re-fetched.** Subagents
  cannot refetch. The plan and any plan-critic/test-critic findings are
  handed on **by absolute path** (ticket #113 — `plan_path`, and each gate's
  `critique-merged.json`), read with `Read` by whoever receives them; the
  change report is still inlined, but only the **current round's**, never the
  whole accumulated chain (ticket #105).
- **Never on main, never create the branch/worktree, never `-C`-less git.**
- **Never move a board column.** The caller owns the board.
- **One terminal event, then stop.** Do not keep working after `ci-green`,
  `blocked`, or `failed`. `replan-triggered` is the one non-terminal event
  that is *expected* to be followed by more work in the same turn — see
  "Replan".
- **Commit and push before every turn end, and never end a turn to wait.**
  See *Turn-end discipline*; a `Stop` hook enforces both.
- **No "not included" lists.** A package is done when all of it is done. If
  part of it cannot be done, that is `blocked` or `failed`, not a PR with a
  caveat.
