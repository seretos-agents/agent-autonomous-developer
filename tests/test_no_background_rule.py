"""
Regression tests for ticket #101: background test runs are fatal to a
headless session, and the plugin's own instructions still prescribed them.

`agent-worktree#176` (2026-08-26) lost two sessions on a small package:
attempt 1 backgrounded the suite, armed a `Monitor` and ended its turn
("Nothing more to do until that fires" — nothing ever fired, a headless
process is never woken); attempt 2 backgrounded a suite with a hanging test
and was killed by the harness's 600 s ceiling with the hang's diagnostics
inside it. agents/developer.md at the time *mandated* the
`nohup` + `Monitor` shape for every full-suite run, and the two turn-end
hooks (#93, #23) accepted a `Monitor` as "resolved" — so the exact shape that
killed attempt 1 passed both hooks.

The fix: the rule is absolute (nothing runs in the background, suites run as
synchronous foreground chunks), the contradicting instructions are gone, a
PreToolUse hook refuses the calls up front, the turn-end hooks no longer treat
`Monitor` as a wait, hangs are handled by re-running with a stack-dumping
per-test timeout, and this file keeps the pattern from creeping back into the
docs (it did once already, between #93 and #176).

Behavioural requirements:
  R1/R2 - docs: agents/*.md and skills/*/SKILL.md mention
          `run_in_background: true`, `nohup`, `Monitor`, `Start-Job`,
          `Start-Process` only inside an explicit prohibition; the mandatory
          background paragraph and the `timeout_ms`/`persistent` Monitor
          advice are gone; the chunk pattern with an explicit Bash `timeout`
          is in.
  R3    - hooks/check-no-background.mjs exists, is wired under PreToolUse
          with matcher `Bash|Monitor`, and (executable) refuses
          Bash(run_in_background: true), detaching Bash commands, and Monitor
          inside a pipeline run or a plugin subagent — while passing plain
          foreground calls and staying out of an unscoped (human) session.
  R3b   - the shared transcript walk no longer resolves a backgrounded
          command on a later Monitor call, but does resolve it on the
          PreToolUse refusal marker.
  R4    - developer.md and SKILL.md carry the hang procedure
          (`--timeout-method=thread`, stack dump into the report / `failed`
          event).
  R5    - this file (the doc regression) exists and AGENTS.md names it.
  ship  - the new hook is discovered by the release-payload scanner.
"""

from __future__ import annotations

import json
import pathlib
import re
import shutil
import subprocess

import pytest

import tools.check_plugin_payload as cpp

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
HOOK_PATH = REPO_ROOT / "hooks" / "check-no-background.mjs"
LIB_PATH = REPO_ROOT / "hooks" / "lib" / "turn-end-scan.mjs"
HOOKS_JSON = REPO_ROOT / "hooks" / "hooks.json"
DEVELOPER_MD = REPO_ROOT / "agents" / "developer.md"
REVIEWER_MD = REPO_ROOT / "agents" / "reviewer.md"
SKILL_MD = REPO_ROOT / "skills" / "process-developer" / "SKILL.md"
AGENTS_MD = REPO_ROOT / "AGENTS.md"

MARKER = "[adev-no-background]"


def _read(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8")


def _node() -> str:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not found on PATH")
    return node


# ---------------------------------------------------------------------------
# R1 / R2 / R5 - the docs mention the forbidden shapes only to forbid them
# ---------------------------------------------------------------------------

# Every mention of one of these must sit in a paragraph that forbids it.
FORBIDDEN_NEEDLES = (
    re.compile(r"run_in_background:\s*true"),
    re.compile(r"\bnohup\b"),
    re.compile(r"\bMonitor\b"),
    re.compile(r"\bStart-Job\b"),
    re.compile(r"\bStart-Process\b"),
)

# A paragraph counts as a prohibition when it carries one of these.
PROHIBITION_WORDS = re.compile(
    r"\b(never|forbidden|refuse[sd]?|not allowed|must not|block(s|ed)?|"
    r"den(y|ies|ied)|no longer|is not a wait|prohibit(s|ed)?)\b",
    re.IGNORECASE,
)


def _doc_files() -> list[pathlib.Path]:
    files = sorted((REPO_ROOT / "agents").glob("*.md"))
    files += sorted((REPO_ROOT / "skills").glob("*/SKILL.md"))
    assert files, "no agent/skill markdown found — wrong REPO_ROOT?"
    return files


def _paragraphs(text: str) -> list[tuple[int, str]]:
    """(1-based start line, paragraph text) for every blank-line-separated
    block. Markdown bullets inside one block stay together, which is what
    makes a multi-line prohibition sentence count as one context."""
    out: list[tuple[int, str]] = []
    start = None
    buf: list[str] = []
    for i, line in enumerate(text.splitlines(), start=1):
        if line.strip():
            if start is None:
                start = i
            buf.append(line)
        elif buf:
            out.append((start, "\n".join(buf)))
            start, buf = None, []
    if buf:
        out.append((start, "\n".join(buf)))
    return out


@pytest.mark.parametrize("doc", _doc_files(), ids=lambda p: p.relative_to(REPO_ROOT).as_posix())
def test_docs_mention_background_shapes_only_to_forbid_them(doc: pathlib.Path):
    """Acceptance 1 of #101: `grep -rn "run_in_background: true\\|nohup\\|Monitor"
    agents/ skills/` hits only prohibition sentences. Paragraph-scoped so a
    prohibition that wraps across lines still counts."""
    offenders = []
    for start, para in _paragraphs(_read(doc)):
        hit = [n.pattern for n in FORBIDDEN_NEEDLES if n.search(para)]
        if hit and not PROHIBITION_WORDS.search(para):
            offenders.append((start, hit, para[:160]))
    assert not offenders, (
        f"{doc.relative_to(REPO_ROOT).as_posix()} mentions a background shape "
        f"outside a prohibition (the #93→#176 regression):\n"
        + "\n".join(f"  line {s}: {h} — {p!r}" for s, h, p in offenders)
    )


def test_developer_md_no_longer_mandates_backgrounding():
    text = _read(DEVELOPER_MD)
    for phrase in (
        "always uses the backgrounded",
        "backgrounded form becomes mandatory",
        "timeout_ms",
        "persistent: true",
    ):
        assert phrase not in text, (
            f"agents/developer.md still carries {phrase!r} — the mandatory "
            "background+Monitor paragraph #176 died on must be gone, not "
            "merely contradicted."
        )


def test_skill_no_longer_offers_background_as_an_option():
    text = _read(SKILL_MD)
    assert "followed by an in-turn" not in text
    assert "backgrounded with an" not in text, (
        "SKILL.md Phase 3b still describes the full suite as backgrounded."
    )


def test_developer_md_prescribes_synchronous_chunks_with_bash_timeout():
    text = _read(DEVELOPER_MD)
    assert "synchronous" in text and "chunk" in text.lower()
    assert re.search(r"`timeout`", text), (
        "agents/developer.md must name the explicit Bash `timeout` per chunk."
    )
    assert re.search(r"600\s?000", text), (
        "agents/developer.md must state the tool's 600 000 ms maximum so "
        "chunks are cut with margin under it."
    )


def test_hard_rule_names_every_forbidden_shape():
    for doc in (DEVELOPER_MD, REVIEWER_MD, SKILL_MD):
        text = _read(doc)
        for shape in ("run_in_background: true", "nohup", "Start-Job", "Start-Process", "Monitor"):
            assert shape in text, (
                f"{doc.name}'s Hard Rule must name {shape!r} as forbidden."
            )
        assert "#101" in text


def test_agents_md_names_the_doc_regression_test():
    text = _read(AGENTS_MD)
    assert "#101" in text
    assert pathlib.Path(__file__).name in text, (
        "AGENTS.md must point at this doc regression test so the invariant "
        "and its guard stay discoverable together."
    )
    assert "check-no-background.mjs" in text


# ---------------------------------------------------------------------------
# R4 - the hang case is a procedure, not a reason to background
# ---------------------------------------------------------------------------


def test_hang_procedure_is_documented():
    developer = _read(DEVELOPER_MD)
    assert "--timeout-method=thread" in developer
    assert re.search(r"stack", developer, re.IGNORECASE)
    skill = _read(SKILL_MD)
    assert re.search(r"stack dump", skill, re.IGNORECASE), (
        "SKILL.md Phase 3b must carry the hung chunk's stack dump into the "
        "`failed` event — the diagnosis #176 never produced."
    )


# ---------------------------------------------------------------------------
# R3 - the PreToolUse hook: exists, wired, node-clean
# ---------------------------------------------------------------------------


def test_hook_exists_and_is_wired():
    assert HOOK_PATH.exists()
    config = json.loads(_read(HOOKS_JSON))
    pre = config.get("hooks", {}).get("PreToolUse", [])
    entries = [e for e in pre if any("check-no-background.mjs" in h.get("command", "") for h in e.get("hooks", []))]
    assert entries, "hooks/hooks.json must wire hooks/check-no-background.mjs under PreToolUse."
    matcher = entries[0].get("matcher", "")
    assert (
        re.fullmatch(matcher, "Bash")
        and re.fullmatch(matcher, "Monitor")
        and re.fullmatch(matcher, "Agent")
    ), (
        "PreToolUse matcher must cover Bash, Monitor and Agent (ticket #139: "
        f"a backgrounded Agent dispatch must reach this hook too); got {matcher!r}"
    )
    assert not re.fullmatch(matcher, "Read"), (
        f"PreToolUse matcher must not accidentally cover Read; got {matcher!r}"
    )
    # The pre-existing hooks must survive.
    matchers = {e.get("matcher") for e in config["hooks"].get("SubagentStop", [])}
    assert {"context-extractor", "developer"} <= matchers
    assert config["hooks"].get("Stop")


def test_hook_node_check():
    node = _node()
    for path in (HOOK_PATH, LIB_PATH):
        result = subprocess.run([node, "--check", str(path)], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr


def test_hook_ships_in_the_release_payload():
    refs = cpp.discover_references(REPO_ROOT)
    assert any(
        r.path == "hooks/check-no-background.mjs" and r.source_file == "hooks/hooks.json"
        for r in refs
    )


# ---------------------------------------------------------------------------
# R3 - the PreToolUse hook: executable behaviour
# ---------------------------------------------------------------------------


def _run_pre(tool_name: str, tool_input: dict, *, cwd: pathlib.Path, agent_type: str | None = None):
    node = _node()
    payload = {"tool_name": tool_name, "tool_input": tool_input, "cwd": str(cwd)}
    if agent_type is not None:
        payload["agent_type"] = agent_type
    return subprocess.run(
        [node, str(HOOK_PATH)], input=json.dumps(payload), capture_output=True, text=True
    )


def _pipeline_cwd(tmp_path: pathlib.Path) -> pathlib.Path:
    (tmp_path / ".adev" / "101-1").mkdir(parents=True)
    return tmp_path


def _assert_refused(result, needle: str = "#101"):
    assert result.returncode == 2, (
        f"expected exit 2 (refused); got {result.returncode}. "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert MARKER in result.stderr, "refusal must carry the transcript marker"
    assert needle in result.stderr


def _assert_allowed(result):
    assert result.returncode == 0, f"expected pass; stderr={result.stderr!r}"
    assert result.stdout.strip() == ""


def test_refuses_run_in_background_in_pipeline_run(tmp_path):
    """Acceptance 4: Bash(run_in_background: true) is refused in a run."""
    cwd = _pipeline_cwd(tmp_path)
    _assert_refused(_run_pre("Bash", {"command": "python -m pytest", "run_in_background": True}, cwd=cwd))


def test_refuses_monitor_in_pipeline_run(tmp_path):
    """Acceptance 4: the #176 attempt-1 shape — Monitor — is refused."""
    cwd = _pipeline_cwd(tmp_path)
    _assert_refused(_run_pre("Monitor", {"path": "suite.log", "until": "passed"}, cwd=cwd), "Monitor")


@pytest.mark.parametrize(
    "command",
    [
        "nohup python -m pytest > suite.log 2>&1 &",
        "python -m pytest > suite.log 2>&1 &",
        "npm test &",
        "Start-Job { pytest }",
        "Start-Process pytest -ArgumentList '-q'",
        "cd /w && nohup npm test",
        "project-issues wait-pipeline --project p --sha abc --timeout 540 &",
    ],
)
def test_refuses_detaching_bash_commands(tmp_path, command):
    cwd = _pipeline_cwd(tmp_path)
    _assert_refused(_run_pre("Bash", {"command": command}, cwd=cwd))


@pytest.mark.parametrize(
    "command",
    [
        "python -m pytest tests/test_a.py -q",
        "npm install && npm test",
        "pytest -q 2>&1 | tail -50",
        "curl 'https://x.example/?a=1&b=2'",
        "sleep 60",
        "git -C /w status",
        "project-issues wait-pipeline --project p --sha abc --timeout 540",
    ],
)
def test_allows_foreground_bash_commands(tmp_path, command):
    cwd = _pipeline_cwd(tmp_path)
    _assert_allowed(_run_pre("Bash", {"command": command, "timeout": 600000}, cwd=cwd))


def test_refuses_for_plugin_subagent_without_adev(tmp_path):
    """A developer dispatch is in scope even when cwd carries no `.adev/`."""
    _assert_refused(
        _run_pre(
            "Bash",
            {"command": "pytest", "run_in_background": True},
            cwd=tmp_path,
            agent_type="agent-autonomous-developer:developer",
        )
    )
    _assert_refused(
        _run_pre("Monitor", {"path": "x.log"}, cwd=tmp_path, agent_type="agent-autonomous-developer:reviewer"),
        "Monitor",
    )


def test_stays_out_of_an_unscoped_session(tmp_path):
    """A human's interactive session that merely loads the plugin keeps
    Monitor and background commands."""
    _assert_allowed(_run_pre("Monitor", {"path": "x.log"}, cwd=tmp_path))
    _assert_allowed(_run_pre("Bash", {"command": "pytest", "run_in_background": True}, cwd=tmp_path))
    _assert_allowed(
        _run_pre("Bash", {"command": "pytest &"}, cwd=tmp_path, agent_type="some-other-plugin:developer-helper")
    )


def test_ignores_other_tools_and_malformed_stdin(tmp_path):
    cwd = _pipeline_cwd(tmp_path)
    _assert_allowed(_run_pre("Read", {"file_path": "x"}, cwd=cwd))
    node = _node()
    result = subprocess.run([node, str(HOOK_PATH)], input="not json", capture_output=True, text=True)
    _assert_allowed(result)


# ---------------------------------------------------------------------------
# Ticket #139 - a backgrounded `Agent` dispatch is refused exactly like Bash
#
# The Agent tool's own schema backgrounds by default: "Agents run in the
# background by default … Set to false only when your very next action
# depends on this agent's result". Only an explicit `run_in_background: false`
# is a foreground dispatch; an absent key or an explicit `true` must be
# refused before the dispatch starts, the same way Bash(run_in_background:
# true) already is (#101). This is the mechanism a long developer dispatch
# needs so it is never silently lost to the harness's 600s background-task
# ceiling in print mode.
# ---------------------------------------------------------------------------


def _agent_input(*, run_in_background: object) -> dict:
    """An Agent tool_input payload per the plan's P1 shape. `None` means the
    key is absent (the real default-background case); any other value is set
    explicitly."""
    payload = {
        "description": "test dispatch",
        "subagent_type": "agent-autonomous-developer:developer",
        "prompt": "do the thing",
    }
    if run_in_background is not None:
        payload["run_in_background"] = run_in_background
    return payload


@pytest.mark.parametrize("run_in_background", [None, True], ids=["absent", "true"])
def test_refuses_backgrounded_agent_dispatch_in_pipeline_run(tmp_path, run_in_background):
    """R1 (#139): an Agent dispatch with no run_in_background key, or an
    explicit True, backgrounds by default per the Agent tool's own schema and
    must be refused before it runs."""
    cwd = _pipeline_cwd(tmp_path)
    result = _run_pre("Agent", _agent_input(run_in_background=run_in_background), cwd=cwd)
    assert result.returncode == 2, (
        f"expected exit 2 (refused); got {result.returncode}. "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert MARKER in result.stderr, "refusal must carry the transcript marker"
    assert "Agent(" in result.stderr, (
        "refusal text must describe the call as an Agent(...) dispatch, not "
        f"a bare command; got: {result.stderr!r}"
    )


def test_allows_explicit_foreground_agent_dispatch(tmp_path):
    """An explicit `run_in_background: false` is the one foreground shape and
    must pass."""
    cwd = _pipeline_cwd(tmp_path)
    _assert_allowed(_run_pre("Agent", _agent_input(run_in_background=False), cwd=cwd))


def test_stays_out_of_unscoped_session_for_agent_dispatch(tmp_path):
    """A human's interactive session (no .adev/, no plugin agent_type) keeps
    the Agent tool's own default background behaviour, same as it keeps
    Monitor and backgrounded Bash."""
    _assert_allowed(_run_pre("Agent", _agent_input(run_in_background=None), cwd=tmp_path))


def test_refuses_agent_dispatch_for_plugin_subagent_without_adev(tmp_path):
    """A developer dispatch is in scope even when cwd carries no `.adev/`
    (same rule as the existing Bash/Monitor case)."""
    result = _run_pre(
        "Agent",
        _agent_input(run_in_background=None),
        cwd=tmp_path,
        agent_type="agent-autonomous-developer:developer",
    )
    assert result.returncode == 2, (
        f"expected exit 2 (refused); got {result.returncode}. "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert MARKER in result.stderr


# ---------------------------------------------------------------------------
# R3b - the shared transcript walk: Monitor resolves nothing, the refusal does
# ---------------------------------------------------------------------------


def _scan(lines: list[str]) -> str | None:
    node = _node()
    script = (
        "import { unresolvedBackgroundCommand } from "
        f"{json.dumps(LIB_PATH.resolve().as_uri())};"
        "let s='';process.stdin.on('data',d=>s+=d).on('end',()=>{"
        "process.stdout.write(JSON.stringify(unresolvedBackgroundCommand(s.split(/\\r?\\n/))))});"
    )
    result = subprocess.run(
        [node, "--input-type=module", "-e", script],
        input="\n".join(lines) + "\n",
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _assistant(name: str, input_: dict) -> str:
    return json.dumps(
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": name, "input": input_}]}}
    )


def _tool_result(text: str) -> str:
    return json.dumps(
        {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": text}]}}
    )


def test_walk_monitor_no_longer_resolves_a_background_command():
    """REGRESSION (#176 attempt 1): background + Monitor + turn end passed
    both turn-end hooks."""
    lines = [
        _assistant("Bash", {"command": "nohup pytest > s.log 2>&1 &", "run_in_background": True}),
        _assistant("Monitor", {"path": "s.log"}),
    ]
    assert _scan(lines) == "nohup pytest > s.log 2>&1 &"


def test_walk_detects_detach_without_the_flag():
    lines = [_assistant("Bash", {"command": "pytest -q > s.log 2>&1 &"})]
    assert _scan(lines) == "pytest -q > s.log 2>&1 &"


def test_walk_refusal_marker_resolves():
    """A call the PreToolUse hook refused never ran — the turn-end hooks must
    not trap the agent behind it."""
    lines = [
        _assistant("Bash", {"command": "pytest", "run_in_background": True}),
        _tool_result(f"{MARKER} refused Bash(run_in_background: true): pytest. Hard Rule (ticket #101) …"),
    ]
    assert _scan(lines) is None


def test_walk_marker_inside_an_assistant_edit_does_not_resolve():
    """Editing this very hook must not look like a refusal."""
    lines = [
        _assistant("Bash", {"command": "pytest", "run_in_background": True}),
        _assistant("Write", {"file_path": "hooks/x.mjs", "content": f"const M = '{MARKER}';"}),
    ]
    assert _scan(lines) == "pytest"


def test_walk_foreground_only_is_clean():
    lines = [
        _assistant("Bash", {"command": "pytest tests/a.py", "timeout": 600000}),
        _assistant("Bash", {"command": "npm install && npm test"}),
    ]
    assert _scan(lines) is None


# ---------------------------------------------------------------------------
# R4 (#139) - the shared walk classifies an Agent dispatch the same way
# ---------------------------------------------------------------------------


def _agent_assistant(run_in_background: object) -> str:
    input_ = {
        "description": "dispatch developer",
        "subagent_type": "agent-autonomous-developer:developer",
        "prompt": "implement the plan",
    }
    if run_in_background is not None:
        input_["run_in_background"] = run_in_background
    return _assistant("Agent", input_)


@pytest.mark.parametrize("run_in_background", [None, True], ids=["absent", "true"])
def test_walk_detects_backgrounded_agent_dispatch(run_in_background):
    """R4 (#139): unresolvedBackgroundCommand must classify an Agent call the
    same way it classifies a backgrounded Bash call — absent key or explicit
    True is unresolved."""
    lines = [_agent_assistant(run_in_background)]
    result = _scan(lines)
    assert result is not None, "an Agent dispatch that backgrounds by default must not be clean"
    assert result.startswith("Agent("), f"expected an Agent(...) label; got {result!r}"


def test_walk_agent_dispatch_explicit_false_is_clean():
    lines = [_agent_assistant(False)]
    assert _scan(lines) is None


def test_walk_agent_dispatch_refusal_marker_resolves():
    """A call the PreToolUse hook refused never ran — the turn-end hooks must
    not trap the agent behind it, same as the Bash case."""
    lines = [
        _agent_assistant(None),
        _tool_result(
            f"{MARKER} refused Agent(agent-autonomous-developer:developer) "
            "(run_in_background not false (default: background)). Hard Rule …"
        ),
    ]
    assert _scan(lines) is None
