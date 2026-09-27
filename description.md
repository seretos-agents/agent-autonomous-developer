Takes one work package — a ticket, or an epic standing for all of its child tickets — from a prepared worktree to a pull request with a **green CI pipeline**, for projects in any language (stack auto-detected). Designed to run headless overnight: it never asks a human, it escalates by writing a `blocked` event on the ticket and ending.

Key capabilities:

- **One skill, one package, one green PR** — `process-developer` runs `context-extractor → planner → plan-critic → developer (tests, RED) → test-critic → developer (implement, GREEN) → reviewer → push → PR → CI gate`. The pipeline, not the local suite, decides when the package is done; a red pipeline is analysed and repaired by the run itself (up to three rounds).
- **Isolated critics, ported from sothis** — plan and test critiques run in separate `claude -p` processes with no project context, no tools and no MCP servers, against verbatim review packages; findings are merged mechanically without a model in the loop.
- **Machine-readable progress on the ticket** — every phase posts an `adev:event` comment (`plan-committed`, `tests-red`, `review-verdict`, `pr-opened`, `ci-red`, `ci-green`, `blocked`, `failed`, …) so an orchestrator — or a human — can reconstruct the run's state without the session.
- **Three-round caps everywhere** — plan critique, test critique, review and CI each stop after three rounds; infrastructure failures count, and reports separate real findings from crashes.
- **Optional Codex and Serena** — the reviewer folds in a Codex correctness pass when the Codex plugin is ready; all agents gain Serena's symbol-aware tools when agent-serena-wrapper is installed. Both degrade silently.
- **No board, no ticket selection, no worktrees** — those belong to the caller (`agent-ticket-orchestrator` in the Seretos ecosystem), which keeps this plugin reusable under any orchestration.
