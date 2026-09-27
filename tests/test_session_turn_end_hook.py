"""
Regression tests for ticket #23: the top-level `process-developer` session ends
its turn while work is still outstanding.

#93 established the rule for *subagents* (a subagent's turn ending terminates
it). #23 showed the same holds for the **top-level session** when it runs
headless: in `claude -p` there is no loop that wakes the session after its
turn ends, so ending the turn ends the process. Measured on
`lib-python-worktree` #140 across three attempts with
CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS unset (default 600s), at 0, and at 2h —
all three died. The env var only controls how long the process loiters before
being killed; it never turns waiting into resuming.

The damage was twofold: the attempt was lost, and the work with it — the
caller prescribes `worktree_remove` after a failed second attempt, so anything
not pushed is destroyed (on #140: 1979 insertions across 15 files with HEAD
still on `main`).

The backstop is a Stop hook (hooks/check-session-turn-end.mjs) that blocks the
stop on either condition, scoped to a live pipeline run by the presence of
`<cwd>/.adev/`.

Behavioural requirements:
  BR1  - the hook script exists and is wired into hooks/hooks.json under Stop.
  BR2  - executable: blocks when the transcript shows an unresolved
         Bash(run_in_background: true) call.
  BR3  - executable: does NOT block when a Monitor call resolves the wait and
         the worktree is clean and pushed.
  BR4  - executable: blocks when the worktree holds uncommitted changes, even
         with no background command in play.
  BR5  - executable: blocks when the worktree holds commits that no remote has.
  BR6  - scope gate: a directory without `.adev/` is never blocked (a Stop hook
         fires in every session that loads this plugin, including a human's).
  BR7  - loop guard: `stop_hook_active: true` never blocks a second time.
  BR8  - fail-safe: malformed stdin, a missing transcript and a non-git cwd all
         pass rather than block.
  BR9  - SKILL.md carries the "Turn-end discipline" rule the hook enforces, and
         no longer claims the session's background processes survive its turn.
  BR10 - AGENTS.md documents the cross-file invariants (shared scan lib, the
         `.adev/` scope gate, and that the env var is not the fix).
  BR11 - the new hook and its lib are discovered by the existing
         ${CLAUDE_PLUGIN_ROOT} release-payload scanner, so they ship on a
         marketplace install.
"""

from __future__ import annotations

import json
import pathlib
import shutil
import subprocess

import pytest

import tools.check_plugin_payload as cpp

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
HOOK_PATH = REPO_ROOT / "hooks" / "check-session-turn-end.mjs"
LIB_PATH = REPO_ROOT / "hooks" / "lib" / "turn-end-scan.mjs"
HOOKS_JSON = REPO_ROOT / "hooks" / "hooks.json"
SKILL_MD = REPO_ROOT / "skills" / "process-developer" / "SKILL.md"
AGENTS_MD = REPO_ROOT / "AGENTS.md"


def _read(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8")


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


# ---------------------------------------------------------------------------
# BR1 - hook exists and is wired into hooks.json
# ---------------------------------------------------------------------------


def test_hook_script_and_lib_exist():
    assert HOOK_PATH.exists(), f"{HOOK_PATH} does not exist."
    assert LIB_PATH.exists(), f"{LIB_PATH} does not exist."


def test_hook_wired_in_hooks_json():
    config = json.loads(_read(HOOKS_JSON))
    stop_entries = config.get("hooks", {}).get("Stop", [])
    assert stop_entries, 'hooks/hooks.json must register a "Stop" hook.'
    commands = [
        h.get("command", "")
        for entry in stop_entries
        for h in entry.get("hooks", [])
    ]
    assert any("check-session-turn-end.mjs" in c for c in commands), (
        'hooks/hooks.json "Stop" must invoke hooks/check-session-turn-end.mjs.'
    )
    # The pre-existing SubagentStop entries must survive this change.
    subagent_stop = config.get("hooks", {}).get("SubagentStop", [])
    matchers = {entry.get("matcher") for entry in subagent_stop}
    assert {"context-extractor", "developer"} <= matchers, (
        "hooks/hooks.json lost a pre-existing SubagentStop entry; "
        f"found matchers: {matchers}"
    )


def test_hook_and_lib_node_check():
    node = _node()
    for path in (HOOK_PATH, LIB_PATH):
        result = subprocess.run(
            [node, "--check", str(path)], capture_output=True, text=True
        )
        assert result.returncode == 0, (
            f"node --check {path.name} failed (exit {result.returncode}):\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )


# ---------------------------------------------------------------------------
# Executable behaviour
# ---------------------------------------------------------------------------


def _tool_use_line(name: str, input_: dict) -> str:
    return json.dumps(
        {
            "type": "assistant",
            "message": {"content": [{"type": "tool_use", "name": name, "input": input_}]},
        }
    )


def _make_worktree(tmp_path: pathlib.Path, *, adev: bool = True) -> pathlib.Path:
    """A git checkout with one commit on a feature branch, a bare 'remote' it
    is fully pushed to, and (by default) the `.adev/` marker that scopes the
    hook to a live pipeline run."""
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
    g("checkout", "-qb", "pkg/1-demo")
    g("push", "-q", "-u", "origin", "pkg/1-demo")

    if adev:
        (work / ".adev" / "1-1").mkdir(parents=True)
    return work


def _run_hook(cwd: pathlib.Path, transcript_lines: list[str] | None, **extra):
    node = _node()
    payload = {"cwd": str(cwd), **extra}
    if transcript_lines is not None:
        transcript = cwd.parent / "transcript.jsonl"
        transcript.write_text("\n".join(transcript_lines) + "\n", encoding="utf-8")
        payload["transcript_path"] = str(transcript)
    return subprocess.run(
        [node, str(HOOK_PATH)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
    )


def _assert_blocked(result, needle: str):
    assert result.returncode == 0
    assert result.stdout.strip(), (
        f"expected a block decision; got empty stdout. stderr: {result.stderr!r}"
    )
    decision = json.loads(result.stdout)
    assert decision.get("decision") == "block"
    assert needle in decision.get("reason", ""), (
        f"block reason should mention {needle!r}; got: {decision.get('reason')!r}"
    )


def _assert_passed(result):
    assert result.returncode == 0
    assert result.stdout.strip() == "", f"expected no block; got: {result.stdout!r}"


def test_blocks_unresolved_background_command(tmp_path):
    """REGRESSION (#23): the session backgrounds the suite and ends its turn
    'to pick up when it exits' — which kills the process."""
    work = _make_worktree(tmp_path)
    lines = [
        _tool_use_line(
            "Bash",
            {"command": "pytest -q > suite.log 2>&1", "run_in_background": True},
        )
    ]
    _assert_blocked(_run_hook(work, lines), "#23")


def test_still_blocks_when_a_monitor_was_armed(tmp_path):
    """REGRESSION (agent-worktree#176 attempt 1, ticket #101): background +
    Monitor + turn end used to be "the sanctioned shape" and passed; the
    session died anyway. A Monitor resolves nothing."""
    work = _make_worktree(tmp_path)
    lines = [
        _tool_use_line(
            "Bash",
            {"command": "pytest -q > suite.log 2>&1", "run_in_background": True},
        ),
        _tool_use_line("Monitor", {"path": "suite.log"}),
    ]
    _assert_blocked(_run_hook(work, lines), "#101")


def test_passes_with_foreground_calls_and_clean_tree(tmp_path):
    """The only sanctioned shape: blocking foreground calls, nothing
    outstanding in git."""
    work = _make_worktree(tmp_path)
    lines = [
        _tool_use_line("Bash", {"command": "pytest -q tests/test_a.py", "timeout": 600000}),
        _tool_use_line("Bash", {"command": "sleep 60"}),
    ]
    _assert_passed(_run_hook(work, lines))


def test_blocks_on_uncommitted_work(tmp_path):
    """REGRESSION (#23/#22): the run ends with the implementation uncommitted;
    the caller's worktree_remove would destroy it."""
    work = _make_worktree(tmp_path)
    (work / "src.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    _assert_blocked(_run_hook(work, []), "push")


def test_blocks_on_unpushed_commits(tmp_path):
    """Committed is not enough — the worktree is removed, so a commit that no
    remote has dies with it."""
    git = _git()
    work = _make_worktree(tmp_path)
    (work / "src.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    subprocess.run([git, "-C", str(work), "add", "-A"], check=True)
    subprocess.run(
        [git, "-C", str(work), "commit", "-qm", "work"],
        check=True,
        capture_output=True,
    )
    _assert_blocked(_run_hook(work, []), "unpushed")


def _agent_tool_use_line(run_in_background: object) -> str:
    """An Agent tool_use transcript line per the plan's P1 shape. `None`
    means the key is absent — the real default-background case, since the
    Agent tool's own schema backgrounds unless told `false`."""
    input_ = {
        "description": "dispatch developer",
        "subagent_type": "agent-autonomous-developer:developer",
        "prompt": "implement the plan",
    }
    if run_in_background is not None:
        input_["run_in_background"] = run_in_background
    return _tool_use_line("Agent", input_)


@pytest.mark.parametrize("run_in_background", [None, True], ids=["absent", "true"])
def test_blocks_unresolved_background_agent_dispatch(tmp_path, run_in_background):
    """R3 (#139): a long developer dispatch made via the Agent tool
    backgrounds by default (no run_in_background key, or an explicit True) —
    the #23 backstop must catch an unrefused one exactly like a backgrounded
    Bash call, or the session ends its turn, the dispatch is killed by the
    harness's background-task ceiling, and no event is ever posted."""
    work = _make_worktree(tmp_path)
    lines = [_agent_tool_use_line(run_in_background)]
    result = _run_hook(work, lines)
    assert result.returncode == 0
    assert result.stdout.strip(), (
        f"expected a block decision; got empty stdout. stderr: {result.stderr!r}"
    )
    decision = json.loads(result.stdout)
    assert decision.get("decision") == "block"
    reason = decision.get("reason", "")
    assert "#23" in reason
    assert "Agent(" in reason, f"block reason must describe the Agent(...) dispatch; got: {reason!r}"


def test_passes_when_agent_dispatch_was_refused(tmp_path):
    """A call the PreToolUse hook already refused never ran — blocking the
    stop would trap the agent behind a call it cannot undo."""
    work = _make_worktree(tmp_path)
    lines = [
        _agent_tool_use_line(None),
        json.dumps(
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "t1",
                            "content": (
                                "[adev-no-background] refused "
                                "Agent(agent-autonomous-developer:developer) "
                                "(run_in_background not false (default: background)). …"
                            ),
                        }
                    ]
                },
            }
        ),
    ]
    _assert_passed(_run_hook(work, lines))


def test_passes_with_explicit_foreground_agent_dispatch(tmp_path):
    """An explicit `run_in_background: false` is the one foreground shape and
    must not block."""
    work = _make_worktree(tmp_path)
    lines = [_agent_tool_use_line(False)]
    _assert_passed(_run_hook(work, lines))


def test_stop_hook_active_never_blocks_on_agent_dispatch_either(tmp_path):
    """One clear message per turn is a backstop; a second one for the same
    unresolved Agent dispatch would be a hang."""
    work = _make_worktree(tmp_path)
    lines = [_agent_tool_use_line(None)]
    _assert_passed(_run_hook(work, lines, stop_hook_active=True))


def test_scope_gate_no_adev_directory(tmp_path):
    """A Stop hook fires in every session that loads this plugin. Without the
    `.adev/` marker of a live pipeline run it must never block — otherwise a
    human's interactive session gets blocked for having a dirty tree."""
    work = _make_worktree(tmp_path, adev=False)
    (work / "src.py").write_text("dirty\n", encoding="utf-8")
    _assert_passed(_run_hook(work, []))


def test_stop_hook_active_never_blocks_twice(tmp_path):
    """One clear message is a backstop; an unbreakable loop is a hang."""
    work = _make_worktree(tmp_path)
    (work / "src.py").write_text("dirty\n", encoding="utf-8")
    _assert_passed(_run_hook(work, [], stop_hook_active=True))


def test_fail_safe_on_malformed_stdin():
    node = _node()
    result = subprocess.run(
        [node, str(HOOK_PATH)], input="not json", capture_output=True, text=True
    )
    _assert_passed(result)


def test_fail_safe_on_missing_transcript(tmp_path):
    """An unreadable transcript must not block — but the git conditions still
    apply, so use a clean worktree here."""
    work = _make_worktree(tmp_path)
    _assert_passed(_run_hook(work, None))


def test_fail_safe_on_non_git_cwd(tmp_path):
    plain = tmp_path / "plain"
    (plain / ".adev").mkdir(parents=True)
    _assert_passed(_run_hook(plain, []))


# ---------------------------------------------------------------------------
# BR9 / BR10 - the prose the hook backs up
# ---------------------------------------------------------------------------


def test_skill_documents_turn_end_discipline():
    skill = _read(SKILL_MD)
    assert "Turn-end discipline" in skill, (
        "SKILL.md must carry a 'Turn-end discipline' section — the hook is a "
        "backstop for a rule, not a replacement for one."
    )
    assert "ending your turn ends this process" in skill.lower(), (
        "SKILL.md must state plainly that a headless session's turn end is a "
        "process end."
    )


def test_skill_no_longer_claims_background_processes_survive():
    """The Phase 6 text used to read 'a subagent's background processes die
    with its turn; yours do not' — the second half is exactly the false belief
    #23 disproved."""
    skill = _read(SKILL_MD)
    assert "die with its turn; yours do not" not in skill, (
        "SKILL.md still claims the top-level session's background processes "
        "survive its turn. #23 measured otherwise."
    )


def test_agents_md_documents_the_invariants():
    agents = _read(AGENTS_MD)
    for needle in ("#23", "turn-end-scan.mjs", ".adev/", "CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS"):
        assert needle in agents, (
            f"AGENTS.md must document {needle!r} as part of the #23 invariants."
        )


# ---------------------------------------------------------------------------
# BR11 - release payload
# ---------------------------------------------------------------------------


def test_hook_ships_in_the_release_payload():
    refs = cpp.discover_references(REPO_ROOT)
    referenced = {r.path for r in refs}
    assert "hooks/check-session-turn-end.mjs" in referenced, (
        "the new Stop hook must be referenced via ${CLAUDE_PLUGIN_ROOT} so the "
        "release-payload gate covers it; found: "
        f"{sorted(p for p in referenced if p.startswith('hooks/'))}"
    )


def test_shared_lib_ships_with_the_hooks():
    """The lib is imported by relative path, so the ${CLAUDE_PLUGIN_ROOT}
    scanner cannot see it — its shipping rests entirely on release.yml staging
    `hooks/` as a whole. Assert that directly: a staging step narrowed to
    individual files would silently break both hooks on a marketplace install.
    """
    workflow = _read(REPO_ROOT / ".github" / "workflows" / "release.yml")
    staged = cpp.staged_paths_from_workflow(workflow)
    assert "hooks" in staged, (
        "release.yml must stage the whole hooks/ directory — "
        "hooks/lib/turn-end-scan.mjs is imported by relative path and is "
        f"invisible to the reference scanner. Staged: {sorted(staged)}"
    )
    assert "cp -a hooks/." in workflow, (
        "release.yml's hooks staging must be a recursive copy (`cp -a hooks/.`) "
        "so hooks/lib/ comes along."
    )


# ---------------------------------------------------------------------------
# The developer agent's suite-run instructions must not contradict a project's
# own measured procedure, and must not leave Monitor at its 300s default.
# ---------------------------------------------------------------------------


def test_developer_defers_to_the_project_suite_procedure():
    """`lib-python-worktree`'s AGENTS.md prescribes timed chunks run
    synchronously; this agent's generic rule says "always background +
    Monitor". Without an explicit precedence rule the agent reads both and
    picks one at random."""
    developer = _read(REPO_ROOT / "agents" / "developer.md")
    assert "AGENTS.md" in developer, (
        "agents/developer.md must tell the developer to read the project's own "
        "AGENTS.md before running the suite."
    )
    assert "wins" in developer, (
        "agents/developer.md must state which of the two procedures takes "
        "precedence when a project documents its own."
    )


def test_developer_runs_the_suite_in_foreground_chunks():
    """Since #101 there is no Monitor and no background run at all: the
    suite is cut into synchronous foreground chunks, each with an explicit
    Bash `timeout`. (The predecessor of this test asserted Monitor's
    `timeout_ms` — that whole pattern is gone, see
    tests/test_no_background_rule.py.)"""
    developer = _read(REPO_ROOT / "agents" / "developer.md")
    assert "timeout_ms" not in developer
    assert "chunk" in developer.lower()
    assert "`timeout`" in developer
