"""
Driving tests for ticket #141: `hooks/check-session-turn-end.mjs`'s existing
Stop hook must recognise a *harness-launched* (top-level, not SubagentStop)
`developer` run and apply the same "no unresolved backgrounded command"
guard `check-developer-background-wait.mjs` already applies to a
`SubagentStop`-dispatched developer -- while never reaching the
orchestrator's own commit/push nag (condition B), which does not apply to a
developer child.

Background: when agent-harness's `harness_start_agent` launches a
`developer`, it runs as a *top-level* `claude` process, so neither
`SubagentStop` nor `hooks/check-developer-background-wait.mjs` ever fires for
it -- only the top-level `Stop` hook does, and today that hook only knows
about the orchestrator's own two conditions (A: unresolved background
command; B: uncommitted/unpushed work). A harness-launched developer needs
condition A applied with the developer's own message, and must never see
condition B at all -- the orchestrator pushes, not the developer (#138/#140).

`process.env.HARNESS_LAUNCHED_AGENT` (agent-harness PR #54, `<plugin>:<name>`)
is the only identity signal available on a top-level Stop payload -- there is
no `agent_type` field on that payload the way there is on SubagentStop.

Per the approved plan (.adev/141-1/plan.md), the new branch:
  - fires only on an EXACT match of HARNESS_LAUNCHED_AGENT against the
    literal "agent-autonomous-developer:developer" (not agentNameOf-based:
    that helper drops the plugin qualifier and would wrongly let
    "other-plugin:developer" through -- misread::F1);
  - runs before the existing `.adev/` scope gate (a harness-launched
    developer's cwd need not carry `.adev/`);
  - reuses unresolvedBackgroundCommand/backgroundReasonForToolUse from
    hooks/lib/turn-end-scan.mjs unchanged, including its Agent-dispatch
    detection (no `run_in_background` key, or any value other than `false`
    -- ticket #139; verified against the actual lib code, not just the
    plan's word for it: see hooks/lib/turn-end-scan.mjs
    backgroundReasonForToolUse's `name === "Agent"` branch);
  - blocks with the same class of message check-developer-background-wait.mjs
    produces for the native SubagentStop case (shares its
    "developer: turn is ending with a backgrounded command still
    outstanding (<label>)" prefix);
  - always exits after that branch runs, so condition B never reaches a
    harness-launched developer session, even in a cwd that happens to carry
    `.adev/`.

Behavioural requirements (see plan.md's "Test / verification strategy"):
  R1 - harness-launched developer with an unresolved background command is
       blocked, with the developer-scoped reason, regardless of `.adev/`.
  R2 - harness-launched developer with nothing outstanding gets no block and
       no commit/push nag, even in a dirty/unpushed `.adev/` worktree.
  R3 - the env var must match exactly; unset, wrong-agent, wrong-plugin, or
       plugin-dropped values must not take the harness-developer branch, and
       an unset env var with `.adev/` still gets the pre-existing
       process-developer reason unchanged.

The CI tests resolve the Stop commands straight from hooks/hooks.json
(`hooks.Stop[*].hooks[*].command`, `${CLAUDE_PLUGIN_ROOT}` replaced by the
repo root) rather than hardcoding the hook's path, so a second Stop entry
added later would be exercised here too, the same way the harness itself
would run every registered Stop hook. Helpers otherwise follow
tests/test_session_turn_end_hook.py.
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
HOOKS_JSON = REPO_ROOT / "hooks" / "hooks.json"

HARNESS_DEVELOPER_ID = "agent-autonomous-developer:developer"


def _node() -> str:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not found on PATH")
    return node


def _git() -> str:
    git = shutil.which("git")
    if git is None:
        pytest.skip("git not found on PATH")
    return git


def _stop_commands() -> list[str]:
    """The Stop hook command strings from hooks/hooks.json, with
    ${CLAUDE_PLUGIN_ROOT} resolved to the repo root -- exactly what the
    harness itself would invoke."""
    config = json.loads(HOOKS_JSON.read_text(encoding="utf-8"))
    entries = config.get("hooks", {}).get("Stop", [])
    commands = []
    for entry in entries:
        for h in entry.get("hooks", []):
            cmd = h.get("command", "")
            cmd = cmd.replace("${CLAUDE_PLUGIN_ROOT}", str(REPO_ROOT))
            commands.append(cmd)
    assert commands, "hooks/hooks.json must register at least one Stop hook."
    return commands


def _make_worktree(tmp_path: pathlib.Path, *, adev: bool) -> pathlib.Path:
    """A git checkout with one commit on a feature branch, a bare 'remote'
    it is fully pushed to, and (optionally) the `.adev/` marker."""
    git = _git()
    remote = tmp_path / "remote.git"
    work = tmp_path / "work"
    subprocess.run([git, "init", "--bare", "-q", str(remote)], check=True)
    subprocess.run([git, "init", "-q", "-b", "main", str(work)], check=True)

    def g(*args: str, check: bool = True):
        return subprocess.run(
            [git, "-C", str(work), *args], check=check, capture_output=True, text=True
        )

    g("config", "user.email", "test@example.invalid")
    g("config", "user.name", "Test")
    g("config", "commit.gpgsign", "false")
    (work / "README.md").write_text("seed\n", encoding="utf-8")
    (work / ".gitignore").write_text(".adev/\n", encoding="utf-8")
    g("add", "-A")
    g("commit", "-qm", "seed")
    g("remote", "add", "origin", str(remote))
    g("checkout", "-qb", "pkg/141-demo")
    g("push", "-q", "-u", "origin", "pkg/141-demo")

    if adev:
        (work / ".adev" / "141-1").mkdir(parents=True)
    return work


def _tool_use_line(name: str, input_: dict) -> str:
    """Top-level-session transcript shape: content nested under
    message.content (matches tests/test_session_turn_end_hook.py, since this
    is the same Stop hook)."""
    return json.dumps(
        {
            "type": "assistant",
            "message": {"content": [{"type": "tool_use", "name": name, "input": input_}]},
        }
    )


def _agent_tool_use_line(run_in_background: object) -> str:
    """An Agent dispatch of the developer subagent. `None` means the key is
    absent -- the real default-background shape the plan's misread::F2 note
    asks to pin exactly (an Agent tool_use with run_in_background omitted or
    true), not some other shape."""
    input_ = {
        "description": "dispatch developer",
        "subagent_type": HARNESS_DEVELOPER_ID,
        "prompt": "implement the plan",
    }
    if run_in_background is not None:
        input_["run_in_background"] = run_in_background
    return _tool_use_line("Agent", input_)


def _env_with(harness_agent: str | None) -> dict:
    env = dict(os.environ)
    if harness_agent is None:
        env.pop("HARNESS_LAUNCHED_AGENT", None)
    else:
        env["HARNESS_LAUNCHED_AGENT"] = harness_agent
    return env


def _run_stop_hooks(
    cwd: pathlib.Path,
    transcript_lines: list[str] | None,
    *,
    harness_agent: str | None,
    stop_hook_active: bool = False,
) -> list[subprocess.CompletedProcess]:
    """Run every registered Stop command (currently one) against the given
    payload and environment, returning each command's CompletedProcess."""
    _node()
    payload: dict = {"cwd": str(cwd)}
    if stop_hook_active:
        payload["stop_hook_active"] = True
    if transcript_lines is not None:
        transcript = cwd.parent / "transcript.jsonl"
        transcript.write_text("\n".join(transcript_lines) + "\n", encoding="utf-8")
        payload["transcript_path"] = str(transcript)

    env = _env_with(harness_agent)
    results = []
    for cmd in _stop_commands():
        results.append(
            subprocess.run(
                cmd,
                shell=True,
                input=json.dumps(payload),
                capture_output=True,
                text=True,
                env=env,
            )
        )
    return results


def _only_decision(results: list[subprocess.CompletedProcess]):
    """Assert every Stop command exited 0, and return the single non-empty
    decision if any (there is currently exactly one Stop entry, but this
    stays correct if a second is ever added)."""
    decisions = []
    for result in results:
        assert result.returncode == 0, (
            f"Stop hook must always exit 0; got {result.returncode}. "
            f"stdout={result.stdout!r} stderr={result.stderr!r}"
        )
        if result.stdout.strip():
            decisions.append(json.loads(result.stdout))
    return decisions


# ---------------------------------------------------------------------------
# R1 - harness-launched developer, unresolved background command -> blocked
# ---------------------------------------------------------------------------


BACKGROUNDED_SHAPES = [
    pytest.param(
        lambda: _tool_use_line(
            "Bash", {"command": "pytest -q > suite.log 2>&1", "run_in_background": True}
        ),
        id="bash-run_in_background-true",
    ),
    pytest.param(
        lambda: _tool_use_line("Bash", {"command": "nohup pytest -q > suite.log 2>&1 &"}),
        id="bash-nohup",
    ),
    pytest.param(
        lambda: _tool_use_line("Bash", {"command": "Start-Job { pytest -q }"}),
        id="bash-start-job",
    ),
    pytest.param(
        lambda: _tool_use_line("Bash", {"command": "Start-Process pytest -ArgumentList '-q'"}),
        id="bash-start-process",
    ),
    pytest.param(lambda: _agent_tool_use_line(None), id="agent-run_in_background-absent"),
]


@pytest.mark.parametrize("make_line", BACKGROUNDED_SHAPES)
def test_harness_developer_blocks_unresolved_background(tmp_path, make_line):
    """R1 driving test. Runs in a tmp dir WITHOUT `.adev/` -- a
    harness-launched developer's cwd need not carry the pipeline marker at
    all, and the branch must fire on the env var alone.

    Expected RED reason (current, unmodified hook): the no-`.adev/` scope
    gate exits 0 with empty stdout before any harness-aware branch exists.
    """
    work = tmp_path / "cwd"
    work.mkdir()
    lines = [make_line()]
    results = _run_stop_hooks(work, lines, harness_agent=HARNESS_DEVELOPER_ID)
    decisions = _only_decision(results)
    assert decisions, (
        "expected a block decision from a Stop hook; got no stdout from any "
        f"of them. raw results: {[(r.stdout, r.stderr) for r in results]!r}"
    )
    assert len(decisions) == 1, f"expected exactly one block; got {decisions!r}"
    decision = decisions[0]
    assert decision.get("decision") == "block"
    reason = decision.get("reason", "")
    assert reason.startswith(
        "developer: turn is ending with a backgrounded command still outstanding"
    ), f"block reason must carry the developer-scoped prefix; got: {reason!r}"


def test_harness_developer_stop_hook_active_does_not_block_twice(tmp_path):
    """Additional edge-case coverage for R1: `stop_hook_active: true` must
    not block a second time, same as every other Stop condition."""
    work = tmp_path / "cwd"
    work.mkdir()
    lines = [_agent_tool_use_line(None)]
    results = _run_stop_hooks(
        work, lines, harness_agent=HARNESS_DEVELOPER_ID, stop_hook_active=True
    )
    decisions = _only_decision(results)
    assert decisions == [], f"stop_hook_active must suppress a second block; got {decisions!r}"


# ---------------------------------------------------------------------------
# R2 - harness-launched developer, nothing outstanding -> no block, no nag
# ---------------------------------------------------------------------------


def test_harness_developer_never_told_to_commit_or_push(tmp_path):
    """R2 driving test. A worktree WITH `.adev/`, uncommitted changes and an
    unpushed commit, whose transcript has only foreground calls. No Stop
    entry may block -- the orchestrator's condition B (commit/push) must
    never reach a harness-launched developer session.

    Expected RED reason (current, unmodified hook): condition B blocks with
    "work that no remote has ... push" because the hook has no awareness of
    HARNESS_LAUNCHED_AGENT at all yet.
    """
    git = _git()
    work = _make_worktree(tmp_path, adev=True)
    (work / "src.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    subprocess.run([git, "-C", str(work), "add", "-A"], check=True)
    subprocess.run(
        [git, "-C", str(work), "commit", "-qm", "work"], check=True, capture_output=True
    )
    lines = [_tool_use_line("Bash", {"command": "pytest -q tests/test_a.py", "timeout": 600000})]
    results = _run_stop_hooks(work, lines, harness_agent=HARNESS_DEVELOPER_ID)
    for result in results:
        assert result.returncode == 0
        assert result.stdout.strip() == "", (
            "a harness-launched developer must never see the commit/push nag "
            f"(or any block); got: {result.stdout!r}"
        )


def test_harness_developer_background_reason_never_mentions_commit_or_push(tmp_path):
    """Additional edge-case coverage for R2: in the same dirty/unpushed
    `.adev/` worktree, a backgrounded command still gets the developer
    reason (condition A), and that reason must not mention push or commit --
    proving condition B is structurally unreachable, not merely untriggered
    in the "nothing outstanding" case above."""
    git = _git()
    work = _make_worktree(tmp_path, adev=True)
    (work / "src.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    subprocess.run([git, "-C", str(work), "add", "-A"], check=True)
    subprocess.run(
        [git, "-C", str(work), "commit", "-qm", "work"], check=True, capture_output=True
    )
    lines = [_agent_tool_use_line(None)]
    results = _run_stop_hooks(work, lines, harness_agent=HARNESS_DEVELOPER_ID)
    decisions = _only_decision(results)
    assert decisions and decisions[0].get("decision") == "block"
    reason = decisions[0].get("reason", "")
    assert "push" not in reason
    assert "commit" not in reason


# ---------------------------------------------------------------------------
# R3 - exact-match guard: wrong/absent env value never takes the branch
# ---------------------------------------------------------------------------


NON_MATCHING_ENV_VALUES = [
    pytest.param(None, id="unset"),
    pytest.param("agent-autonomous-developer:reviewer", id="reviewer"),
    pytest.param("other-plugin:developer", id="other-plugin-developer"),
    pytest.param("developer", id="bare-developer"),
    pytest.param("other-plugin:developer-tools", id="other-plugin-developer-tools"),
]


@pytest.mark.parametrize("env_value", NON_MATCHING_ENV_VALUES)
def test_non_developer_or_absent_env_does_not_take_developer_branch(tmp_path, env_value):
    """R3 driving test (guard). An unresolved background command, a tmp dir
    WITHOUT `.adev/`, and the env var set to anything other than the exact
    id must not block -- none of these values may take the harness-developer
    branch. `other-plugin:developer` in particular pins misread::F1: an
    agentNameOf-based check (which drops the plugin qualifier) would
    wrongly match this value, and this case exists to catch exactly that
    wrong implementation.

    Expected RED reason: none. All cases pass before the change too (the
    no-`.adev/` scope gate exits 0 regardless of the env var, since the
    harness-aware branch does not exist yet) -- this is a guard test pinning
    a future implementation mistake, not a symptom of current missing
    behaviour.
    """
    work = tmp_path / "cwd"
    work.mkdir()
    lines = [_agent_tool_use_line(None)]
    results = _run_stop_hooks(work, lines, harness_agent=env_value)
    decisions = _only_decision(results)
    assert decisions == [], (
        f"env value {env_value!r} must not take the harness-developer branch; "
        f"got {decisions!r}"
    )


def test_unset_env_with_adev_still_uses_process_developer_reason(tmp_path):
    """R3 driving test (guard), continued: with the env var unset but
    `.adev/` present, an unresolved background command must still block --
    through the pre-existing, unchanged process-developer condition A, not
    silently swallowed by the new branch's absence. This is the one R3 case
    that already blocks before the change (the harness branch is additive,
    not a replacement), so RED for this specific assertion is the
    'process-developer:' prefix already being correct today; it exists to
    catch a wrong implementation that broke the old path while adding the
    new one.
    """
    work = _make_worktree(tmp_path, adev=True)
    lines = [_agent_tool_use_line(None)]
    results = _run_stop_hooks(work, lines, harness_agent=None)
    decisions = _only_decision(results)
    assert decisions and decisions[0].get("decision") == "block"
    reason = decisions[0].get("reason", "")
    assert reason.startswith("process-developer:"), (
        f"unset env var with .adev/ must keep the pre-existing "
        f"process-developer reason; got: {reason!r}"
    )
