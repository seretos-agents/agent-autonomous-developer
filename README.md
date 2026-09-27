<img src="assets/icon.png" alt="agent-autonomous-developer icon" width="96" />

# agent-autonomous-developer

A Claude Code **skill + agents** plugin that takes one **work package** — a
[agent-project-issues](https://github.com/seretos-agents/agent-project-issues) ticket, or an epic
standing for all of its child tickets — from a prepared worktree to a pull request with a
**green CI pipeline**, for projects in **any language**. Built to run headless overnight: it
never asks a human; it escalates by writing a `blocked` event on the ticket and ending.

Ships **only skill/agent content** — no binaries, no MCP server of its own. It drives the
agent-project-issues MCP (see [Dependencies](#dependencies)).

## What it does

One skill, not model-invocable — the caller invokes it by slash command from a worktree:

```
/agent-autonomous-developer:process-developer package=<id> project_id=<project> worktree_path=<abs path> base_branch=<branch>
```

| Phase | Agent | Result |
|---|---|---|
| 0 | the skill itself | **orient on the branch**: a fresh branch falls straight through; a branch with an open PR that is already CI-green on its exact HEAD but sits behind a moved base skips 1–4 and goes straight to Phase R instead |
| R | `developer` (conflict-scoped) | *repair-only lane*: rebase onto the current base, resolve conflicts narrowly, re-verify, re-review only if the diff shape changed |
| 1 | `context-extractor` | distilled context + a **verbatim transcript** of the package (for the critics) |
| 2 | `planner` → `plan-critic` | a code-grounded plan, judged by four isolated critics (missed / misread / untestable / simplifier) |
| 3a | `developer` (`phase=tests`) → `test-critic` | driving tests proven RED, judged by an isolated critic ("which wrong implementation still passes?") |
| 3b | `developer` (`phase=implement`) | GREEN, full suite locally (a pre-filter, not a verdict) |
| 4 | `reviewer` (+ Codex pass when available) | `APPROVE` / `CHANGES_REQUESTED`, full re-review per fix round |
| 5 | the skill itself | commit, push (reusing an already-open PR for this head instead of opening a second) |
| 6 | the skill itself | **CI gate**: poll the pipeline, repair red runs, up to three rounds |

Every phase posts an `adev:event` comment on the package ticket (`started`, `plan-committed`,
`plan-critic-verdict`, `tests-red`, `test-critic-verdict`, `tests-green`, `review-verdict`,
`pr-opened`, `ci-red`, `replan-triggered`, `ci-green`, `blocked`, `failed`). The only success
event is `ci-green`. State lives in the ticket, never in the session.

**Round caps are progress-based, not just round-counted.** Phases 2, 3a and 4 have a soft cap of
three rounds and a hard cap of six: a round that finds something genuinely new (a deterministic
fingerprint check, no model) keeps the loop going past round 3; a round that only repeats an
earlier finding triggers one **replan** (a fresh `planner` dispatch with the full findings
history folded in, round counters reset) before falling through to `failed`. CI and the Phase R
rebase keep their original hard cap of three, unchanged — see `AGENTS.md`, "Round caps are
progress-based, not just round-counted".

### What it deliberately does not do

- **No human in the loop.** The dispatching session passes `--disallowedTools AskUserQuestion`.
  A question the run cannot answer from ticket, siblings and code becomes a `blocked` event
  with the question, options, a recommendation, and what was already checked.
- **No board, no ticket selection, no worktrees.** The caller — in the Seretos ecosystem
  `agent-ticket-orchestrator` — owns those. This plugin sees one package and one worktree.
- **No mergeability check, no merge.** This plugin never calls `get_pr` or `merge_pr` — CI
  conclusion is its own verdict; whether the PR is actually mergeable is entirely the caller's
  concern. A retry it is asked to run may reuse an already-open PR (Phase 0/5), but it never
  decides *when* to retry — that decision, including "the merge failed on a conflict, retry",
  is made by the caller.
- **No "not included" lists.** A package is done when all of it is done; otherwise it is
  `blocked` or `failed`.

### Isolated critics

`plan-critic` and `test-critic` run the critique in separate `claude -p` processes with
`--setting-sources "" --strict-mcp-config --disable-slash-commands --tools ""`, started in a
temporary directory outside the repository, against review packages assembled by script from
verbatim inputs. Findings are merged by deterministic code. `scripts/critic/check-critic-isolation.sh`
runs as a pre-flight on every gate; run it with `--live` after a CLI upgrade. Ported from the
`sothis` project's gates.

## Dependencies

This plugin is inert without the agent-project-issues MCP plugin enabled **in the consuming
session** (ticket/PR/comment/pipeline operations: `get_ticket`, `list_hierarchy`,
`list_comments`, `add_comment`, `create_pr`, `list_pipeline_runs`, `get_pipeline_run`,
`get_pipeline_step_log`, …). It needs version **0.2.5 or newer** for the pipeline tools.

```json
// .claude/settings.json (or settings.local.json) of the target project
"enabledPlugins": {
  "agent-autonomous-developer@agent-marketplace": true,
  "agent-project-issues@agent-marketplace": true
}
```

Declared in `.claude-plugin/plugin.json` under `dependencies`, but the marketplace registry does
not auto-install them today — enabling them is the consumer's responsibility. The project needs
PR write access configured (see the agent-project-issues setup docs); merging is the caller's job.

## Install

```
/plugin marketplace add seretos-agents/modular-software-factory
/plugin install agent-autonomous-developer@modular-software-factory
```

Then enable the MCP dependency as shown above.

## Usage

Normally you do not invoke this plugin yourself — `agent-ticket-orchestrator`'s `run` skill
prepares a worktree per package and starts a headless session in it. To drive one package by
hand, prepare a worktree on a feature branch and run, from inside it:

```
/agent-autonomous-developer:process-developer package=42 project_id=<project> worktree_path=<abs path> base_branch=main
```

> **Breaking change (ticket #126):** the entry skill was called `process-ticket` until this
> release. There is no alias for the old name — a caller that dispatches by slash command
> (`agent-ticket-orchestrator`, `agent-batch-reviewer`, your own scripts) needs the matching
> version that invokes `process-developer`. Parameters, events and behaviour are unchanged.

> **Scope:** any language — the worker agents **auto-detect** the project's stack and test
> command (`python -m pytest`, `npm test`, `go test`, `cargo test`, …) from its config files.
> The project id is **not** auto-detected — pass it explicitly.

## Branches

- `main` — source of truth.
- `release` — orphan branch, force-pushed by `release.yml`; install-ready files only
  (`.claude-plugin/plugin.json`, `skills/`, `agents/`, `assets/`, `scripts/`, `hooks/`,
  `README.md`, `description.md`, `LICENSE`).

## Release

```
Actions → release → Run workflow → version=X.Y.Z
```

Stamps the version into `plugin.json` (CI only — never hand-bump it), pushes the orphan
`release` branch, tags `agent-autonomous-developer--vX.Y.Z`, and dispatches to
`seretos-agents/modular-software-factory` (category `skill`) via `MARKETPLACE_DISPATCH_TOKEN` — the dispatch
payload includes a `changelog` field (the release's own generated notes, read back rather than
recomputed) that `modular-software-factory` renders into the registry PR body.
