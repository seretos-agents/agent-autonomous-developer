"""
Driving tests for ticket #140 (code half of #138): `tools/live_kill_test.py`,
the one-command harness that reproduces the ticket's symptom -- "after a
pipeline session is killed from outside, files the developer subagent
already wrote never reach git, and the retry starts from nothing" -- against
a scripted fake CLI instead of the real `claude` binary.

Behavioural requirement (see the approved plan, .adev/140-1/plan.md):
  R3 - `run_and_kill(argv, cwd, timeout)` drives a subprocess that speaks
       stream-json, tracks `input.file_path` of Write/Edit/MultiEdit
       tool_use blocks into `edited_files` (repo-relative), and hard-kills
       the whole process tree the instant a `result` event is read -- the
       one and only kill trigger (round 2's `--edits N` / non-edit-result
       trigger is gone). Returns {"killed": bool, "trigger": "result"|None,
       "edited_files": [str]}. `trigger` is None on EOF/timeout before a
       `result` event was ever seen (never a verdict in that case).

  Scenario A (`test_kill_at_turn_end_without_commit_loses_files`): the fake
  CLI writes two files, emits an intermediate non-edit (Bash) result, writes
  a second file, then `result` -- with no commit step at all. The kill lands
  at `result`; both files must still be uncommitted, reproducing the ticket
  symptom.

  Scenario B (`test_intermediate_non_edit_results_do_not_kill_before_commit`):
  the fake CLI additionally runs the *real* `scripts/developer-commit.sh` as
  its last tool call before `result`, with two intermediate non-edit (Bash)
  results in between the edits and the commit. Both files must end up
  committed -- proving neither intermediate non-edit result triggered an
  early kill (the defect round 2's plan had, per the plan's "Approach").

`tools/live_kill_test.py` does not exist yet at this phase, so every test
below imports it lazily inside the test function (mirroring
tests/test_release_scripts.py's precedent for tools/prev_release_tag.py):
the designed RED here *is* `ModuleNotFoundError: tools.live_kill_test`,
which is why the import happens per-test rather than at module scope --
otherwise a single missing module would fail this whole file's collection
instead of each test individually.

The fake CLI (`_write_fake_cli`) is a tiny stdlib-only Python script driven
by a JSON "actions" list: each `write`/`edit` action really writes the file
(so the repo's on-disk state at kill time is real), each `bash` action
really runs the given argv (so Scenario B's commit step is the real
production script, not a stub), and the script always ends by printing the
canned `result` event (unless told not to) and then sleeping, so the
harness's kill is always a kill of a process still genuinely alive -- never
a race against the fake CLI exiting on its own first.
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import sys
import time
import uuid

from tests.test_release_scripts import BASH, REPO_ROOT, _require_tool

SCRIPT = REPO_ROOT / "scripts" / "developer-commit.sh"

GIT = shutil.which("git")


def _require_git() -> None:
    _require_tool(GIT, "git")


def _git_env() -> dict:
    env = dict(os.environ)
    env.update(
        {
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.invalid",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.invalid",
        }
    )
    return env


def _make_plain_repo(tmp_path: pathlib.Path, *, branch: str = "live-kill") -> pathlib.Path:
    """A temp git repo with one commit, no remote -- per the plan, live_kill_test
    itself never pushes; only the commit half is exercised here."""
    work = tmp_path / "repo"
    env = _git_env()
    subprocess.run([GIT, "init", "-q", "-b", branch, str(work)], check=True, env=env)
    subprocess.run(
        [GIT, "-C", str(work), "config", "commit.gpgsign", "false"], check=True, env=env
    )
    (work / "README.md").write_text("seed\n", encoding="utf-8")
    subprocess.run([GIT, "-C", str(work), "add", "-A"], check=True, env=env)
    subprocess.run([GIT, "-C", str(work), "commit", "-qm", "seed"], check=True, env=env)
    return work


def _head(repo: pathlib.Path) -> str:
    return subprocess.run(
        [GIT, "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _is_committed(repo: pathlib.Path, base_sha: str, relpath: str) -> bool:
    """Per the plan: committed when `status --porcelain -- f` is empty and
    `log base..HEAD -- f` is non-empty."""
    status = subprocess.run(
        [GIT, "-C", str(repo), "status", "--porcelain", "--", relpath],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    log = subprocess.run(
        [GIT, "-C", str(repo), "log", f"{base_sha}..HEAD", "--", relpath],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return status.strip() == "" and log.strip() != ""


# ---------------------------------------------------------------------------
# Fake CLI: a tiny stdlib-only stream-json emitter driven by a JSON action
# list. Written fresh per test into tmp_path so nothing is shared/cached
# across tests.
# ---------------------------------------------------------------------------

_FAKE_CLI_SRC = '''\
import json
import subprocess
import sys
import time


def main():
    with open(sys.argv[1], "r", encoding="utf-8") as f:
        payload = json.load(f)

    next_id = 1
    for action in payload["actions"]:
        kind = action["kind"]
        if kind in ("write", "edit"):
            with open(action["path"], "w", encoding="utf-8") as out:
                out.write(action["content"])
            event = {
                "type": "assistant",
                "message": {
                    "content": [{
                        "type": "tool_use",
                        "id": "t%d" % next_id,
                        "name": "Write" if kind == "write" else "Edit",
                        "input": {"file_path": action["path"]},
                    }]
                },
            }
            next_id += 1
            print(json.dumps(event), flush=True)
        elif kind == "bash":
            subprocess.run(action["argv"], check=False)
            event = {
                "type": "assistant",
                "message": {
                    "content": [{
                        "type": "tool_use",
                        "id": "t%d" % next_id,
                        "name": "Bash",
                        "input": {"command": " ".join(action["argv"])},
                    }]
                },
            }
            next_id += 1
            print(json.dumps(event), flush=True)
        elif kind == "text":
            event = {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": action["text"]}]},
            }
            print(json.dumps(event), flush=True)
        elif kind == "raw":
            print(action["text"], flush=True)
        else:
            raise ValueError("unknown action kind: %r" % (kind,))

    if payload.get("emit_result", True):
        print(json.dumps({"type": "result", "subtype": "success"}), flush=True)

    time.sleep(float(payload.get("sleep_after", 60.0)))


if __name__ == "__main__":
    main()
'''


def _write_fake_cli(
    tmp_path: pathlib.Path,
    actions: list[dict],
    *,
    emit_result: bool = True,
    sleep_after: float = 60.0,
) -> tuple[pathlib.Path, pathlib.Path]:
    tag = uuid.uuid4().hex
    fake_cli = tmp_path / f"fake_claude_{tag}.py"
    fake_cli.write_text(_FAKE_CLI_SRC, encoding="utf-8")
    actions_path = tmp_path / f"actions_{tag}.json"
    actions_path.write_text(
        json.dumps({"actions": actions, "emit_result": emit_result, "sleep_after": sleep_after}),
        encoding="utf-8",
    )
    return fake_cli, actions_path


# ---------------------------------------------------------------------------
# R3 - driving tests
# ---------------------------------------------------------------------------


def test_kill_at_turn_end_without_commit_loses_files(tmp_path):
    import tools.live_kill_test as lkt

    _require_git()
    work = _make_plain_repo(tmp_path)
    base_sha = _head(work)

    actions = [
        {"kind": "write", "path": "a.txt", "content": "A\n"},
        {"kind": "bash", "argv": [GIT, "-C", str(work), "status", "--short"]},
        {"kind": "write", "path": "b.txt", "content": "B\n"},
        {"kind": "text", "text": "done"},
    ]
    fake_cli, actions_path = _write_fake_cli(tmp_path, actions)

    started = time.monotonic()
    result = lkt.run_and_kill(
        [sys.executable, str(fake_cli), str(actions_path)],
        cwd=str(work),
        timeout=30,
    )
    elapsed = time.monotonic() - started

    assert result["trigger"] == "result"
    assert result["killed"] is True
    assert elapsed < 30, (
        f"took {elapsed:.1f}s -- the kill must happen at `result`, not after "
        "the fake CLI's 60s post-result sleep"
    )
    assert sorted(result["edited_files"]) == ["a.txt", "b.txt"]

    uncommitted = [f for f in result["edited_files"] if not _is_committed(work, base_sha, f)]
    assert sorted(uncommitted) == ["a.txt", "b.txt"], (
        "REGRESSION for the ticket's symptom: with no commit step ever run, "
        "a kill exactly at `result` must leave both edited files uncommitted"
    )


def test_intermediate_non_edit_results_do_not_kill_before_commit(tmp_path):
    import tools.live_kill_test as lkt

    _require_git()
    _require_tool(BASH, "bash")
    work = _make_plain_repo(tmp_path)
    base_sha = _head(work)

    actions = [
        {"kind": "write", "path": "a.txt", "content": "A\n"},
        {"kind": "bash", "argv": [GIT, "-C", str(work), "status", "--short"]},
        {"kind": "edit", "path": "a.txt", "content": "A\nedited\n"},
        {"kind": "write", "path": "b.txt", "content": "B\n"},
        {"kind": "bash", "argv": [GIT, "-C", str(work), "diff", "--stat"]},
        {"kind": "bash", "argv": [BASH, str(SCRIPT), str(work), "live kill test"]},
        {"kind": "text", "text": "done"},
    ]
    fake_cli, actions_path = _write_fake_cli(tmp_path, actions)

    started = time.monotonic()
    result = lkt.run_and_kill(
        [sys.executable, str(fake_cli), str(actions_path)],
        cwd=str(work),
        timeout=30,
    )
    elapsed = time.monotonic() - started

    assert result["trigger"] == "result"
    assert result["killed"] is True
    assert elapsed < 30
    assert sorted(result["edited_files"]) == ["a.txt", "b.txt"]

    for relpath in result["edited_files"]:
        assert _is_committed(work, base_sha, relpath), (
            f"{relpath} must be committed -- an early kill at either "
            "intermediate non-edit (Bash) result would have fired before "
            "the real developer-commit.sh call and left it uncommitted"
        )


# ---------------------------------------------------------------------------
# Additional coverage (edge cases; may already pass, no RED required)
# ---------------------------------------------------------------------------


def test_eof_without_result_reports_no_trigger(tmp_path):
    import tools.live_kill_test as lkt

    _require_git()
    work = _make_plain_repo(tmp_path)
    actions = [{"kind": "write", "path": "a.txt", "content": "A\n"}]
    fake_cli, actions_path = _write_fake_cli(
        tmp_path, actions, emit_result=False, sleep_after=0.0
    )

    result = lkt.run_and_kill(
        [sys.executable, str(fake_cli), str(actions_path)],
        cwd=str(work),
        timeout=5,
    )

    assert result["trigger"] is None
    assert result["killed"] is False, "the fake CLI exited on its own; there was nothing left to kill"


def test_no_edit_observed_reports_empty_edited_files(tmp_path):
    import tools.live_kill_test as lkt

    _require_git()
    work = _make_plain_repo(tmp_path)
    actions = [
        {"kind": "bash", "argv": [GIT, "-C", str(work), "status", "--short"]},
        {"kind": "text", "text": "done"},
    ]
    fake_cli, actions_path = _write_fake_cli(tmp_path, actions)

    result = lkt.run_and_kill(
        [sys.executable, str(fake_cli), str(actions_path)],
        cwd=str(work),
        timeout=30,
    )

    assert result["trigger"] == "result"
    assert result["edited_files"] == []


def test_non_json_lines_are_ignored(tmp_path):
    import tools.live_kill_test as lkt

    _require_git()
    work = _make_plain_repo(tmp_path)
    actions = [
        {"kind": "raw", "text": "not json at all"},
        {"kind": "write", "path": "a.txt", "content": "A\n"},
        {"kind": "text", "text": "done"},
    ]
    fake_cli, actions_path = _write_fake_cli(tmp_path, actions)

    result = lkt.run_and_kill(
        [sys.executable, str(fake_cli), str(actions_path)],
        cwd=str(work),
        timeout=30,
    )

    assert result["trigger"] == "result"
    assert result["edited_files"] == ["a.txt"]
