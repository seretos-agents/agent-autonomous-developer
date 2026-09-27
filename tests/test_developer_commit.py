"""
Driving tests for ticket #140 (code half of #138): `scripts/developer-commit.sh`,
the commit-only checkpoint step a developer dispatch calls as its last tool
call so a hard kill between the subagent's last edit and the orchestrator's
own Checkpoint doesn't lose the work.

Behavioural requirements (see the approved plan, .adev/140-1/plan.md):
  R1 - a dirty tree (modified + untracked files) is committed in one commit
       holding both files, the tree ends clean, and the bare "origin" remote
       is never touched -- pushing stays with the Checkpoint, not this
       script. Edge cases: a clean tree with an unpushed commit still skips
       (remote unchanged); a multi-line message round-trips via
       `log -1 --format=%B`.
  R2 - three skip guards, each leaving HEAD and `status --porcelain`
       unchanged, rc 0, printing `developer-commit: skipped (<guard>)`:
       clean tree, a real conflicting rebase in progress, a detached HEAD
       with dirty files. Edge case: a missing argument or non-repo path
       exits 2.

`scripts/developer-commit.sh` does not exist yet at this phase -- per the
plan, the designed RED for every test here is the script's absence: running
`bash <missing path> ...` exits 127 (bash reports "No such file or
directory"), which is *not* the rc/stdout these tests assert, so every test
fails for that reason until the script is implemented in the next phase.

Git identity is supplied via GIT_AUTHOR_*/GIT_COMMITTER_* environment
variables (never `git config user.*`), per the plan's stated test strategy.
"""

from __future__ import annotations

import os
import pathlib
import re
import shutil
import subprocess

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


def _g(repo: pathlib.Path, *args: str, check: bool = True, env: dict | None = None):
    return subprocess.run(
        [GIT, "-C", str(repo), *args],
        check=check,
        capture_output=True,
        text=True,
        env=env if env is not None else _git_env(),
    )


def _make_repo(tmp_path: pathlib.Path, *, branch: str = "main") -> tuple[pathlib.Path, pathlib.Path]:
    """A work checkout + a bare 'origin' it fully tracks on `branch`."""
    remote = tmp_path / "origin.git"
    work = tmp_path / "work"
    env = _git_env()

    subprocess.run([GIT, "init", "--bare", "-q", str(remote)], check=True, env=env)
    subprocess.run([GIT, "init", "-q", "-b", branch, str(work)], check=True, env=env)
    _g(work, "config", "commit.gpgsign", "false", env=env)
    (work / "README.md").write_text("seed\n", encoding="utf-8")
    _g(work, "add", "-A", env=env)
    _g(work, "commit", "-qm", "seed", env=env)
    _g(work, "remote", "add", "origin", str(remote), env=env)
    _g(work, "push", "-q", "-u", "origin", branch, env=env)
    return work, remote


def _head(repo: pathlib.Path) -> str:
    return _g(repo, "rev-parse", "HEAD").stdout.strip()


def _rev_count(repo: pathlib.Path) -> int:
    return int(_g(repo, "rev-list", "--count", "HEAD").stdout.strip())


def _porcelain(repo: pathlib.Path) -> str:
    return _g(repo, "status", "--porcelain").stdout


def _remote_head(remote: pathlib.Path, branch: str = "main") -> str:
    return _g(remote, "rev-parse", branch).stdout.strip()


def _in_rebase(repo: pathlib.Path) -> bool:
    git_dir = repo / ".git"
    return (git_dir / "rebase-merge").exists() or (git_dir / "rebase-apply").exists()


def _run_script(work: pathlib.Path, message: str) -> subprocess.CompletedProcess:
    """Invoke the real script under test. Must pass env=_git_env() -- unlike
    every other subprocess call in this file, this one runs the production
    script itself (which shells out to `git commit`), so it needs the same
    GIT_AUTHOR_*/GIT_COMMITTER_* identity as every other git call here. A CI
    runner has no global git identity configured (a local dev machine
    typically does), so omitting this made every commit-path test fail with
    "Author identity unknown" in CI while the skip-guard tests (which never
    reach `git commit`) passed regardless."""
    _require_tool(BASH, "bash")
    return subprocess.run(
        [BASH, str(SCRIPT), str(work), message],
        capture_output=True,
        text=True,
        env=_git_env(),
    )


# ---------------------------------------------------------------------------
# R1 - commits a dirty tree, never pushes
# ---------------------------------------------------------------------------


def test_dirty_tree_is_committed_and_not_pushed(tmp_path):
    _require_git()
    work, remote = _make_repo(tmp_path)
    before_count = _rev_count(work)
    before_remote_sha = _remote_head(remote)

    (work / "README.md").write_text("seed\nchanged\n", encoding="utf-8")
    (work / "new.txt").write_text("new\n", encoding="utf-8")

    result = _run_script(work, "checkpoint: driving test for #140")

    assert result.returncode == 0, (
        f"rc={result.returncode} stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    match = re.match(r"^developer-commit: committed ([0-9a-f]{7,40})\s*$", result.stdout.strip())
    assert match, f"unexpected stdout: {result.stdout!r}"
    assert _porcelain(work) == "", "tree must be clean after the commit"
    assert _rev_count(work) == before_count + 1, "exactly one new commit expected"
    assert _remote_head(remote) == before_remote_sha, (
        "developer-commit.sh must never push -- the bare origin's main must "
        "stay exactly where it was"
    )

    # Test-critic tautology::F2: the printed sha must be the *real* new HEAD,
    # not just any 7-40 hex string (a script could print a fixed/stale sha
    # and this regex alone would still pass).
    printed_sha = match.group(1)
    real_head = _head(work)
    real_short = _g(work, "rev-parse", "--short", "HEAD").stdout.strip()
    assert printed_sha == real_short or real_head.startswith(printed_sha), (
        f"printed sha {printed_sha!r} does not match the real new HEAD "
        f"{real_head!r} (short {real_short!r})"
    )

    changed = _g(work, "show", "--name-only", "--format=", "HEAD").stdout.split()
    assert set(changed) == {"README.md", "new.txt"}, (
        f"the one new commit must hold both files; got {changed!r}"
    )


def test_clean_tree_with_unpushed_commit_is_skipped_and_not_pushed(tmp_path):
    """Edge case for R1: a clean tree with a commit the remote does not have
    yet still skips -- developer-commit.sh never pushes, on any path."""
    _require_git()
    work, remote = _make_repo(tmp_path)
    (work / "README.md").write_text("seed\nunpushed\n", encoding="utf-8")
    _g(work, "add", "-A")
    _g(work, "commit", "-qm", "unpushed work")
    before_head = _head(work)
    before_remote_sha = _remote_head(remote)

    result = _run_script(work, "checkpoint")

    assert result.returncode == 0, (
        f"rc={result.returncode} stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert result.stdout.strip() == "developer-commit: skipped (clean)"
    assert _head(work) == before_head
    assert _remote_head(remote) == before_remote_sha


def test_multiline_message_round_trips(tmp_path):
    """Edge case for R1: a multi-line commit message round-trips exactly via
    `log -1 --format=%B`."""
    _require_git()
    work, _remote = _make_repo(tmp_path)
    (work / "README.md").write_text("seed\nagain\n", encoding="utf-8")
    message = "checkpoint: subject line\n\nBody line one.\nBody line two."

    result = _run_script(work, message)

    assert result.returncode == 0, (
        f"rc={result.returncode} stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    logged = _g(work, "log", "-1", "--format=%B").stdout.rstrip("\n")
    assert logged == message


# ---------------------------------------------------------------------------
# R2 - skip guards
# ---------------------------------------------------------------------------


def test_clean_tree_skips(tmp_path):
    _require_git()
    work, _remote = _make_repo(tmp_path)
    before_head = _head(work)

    result = _run_script(work, "checkpoint")

    assert result.returncode == 0, (
        f"rc={result.returncode} stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert result.stdout.strip() == "developer-commit: skipped (clean)"
    assert _head(work) == before_head
    assert _porcelain(work) == ""


def test_rebase_in_progress_skips(tmp_path):
    """A real conflicting rebase: `status --porcelain` is not empty (the
    conflicted file), so this exercises the clean-check falling through to
    the rebase-check, not merely "rebase dir exists"."""
    _require_git()
    env = _git_env()
    work, _remote = _make_repo(tmp_path)

    _g(work, "checkout", "-qb", "feature", env=env)
    (work / "README.md").write_text("seed\nfeature change\n", encoding="utf-8")
    _g(work, "commit", "-aqm", "feature change", env=env)

    _g(work, "checkout", "-q", "main", env=env)
    (work / "README.md").write_text("seed\nmain change\n", encoding="utf-8")
    _g(work, "commit", "-aqm", "main change", env=env)

    _g(work, "checkout", "-q", "feature", env=env)
    rebase = _g(work, "rebase", "main", check=False, env=env)
    assert rebase.returncode != 0, "fixture broken: expected the rebase to hit a real conflict"
    assert _in_rebase(work), "fixture broken: rebase did not leave rebase state"

    before_head = _head(work)
    before_status = _porcelain(work)

    result = _run_script(work, "checkpoint")

    assert result.returncode == 0, (
        f"rc={result.returncode} stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert result.stdout.strip() == "developer-commit: skipped (rebase)"
    assert _head(work) == before_head
    assert _porcelain(work) == before_status
    assert _in_rebase(work), "the script must not touch the in-progress rebase"


def test_detached_head_skips(tmp_path):
    _require_git()
    env = _git_env()
    work, _remote = _make_repo(tmp_path)
    seed_sha = _head(work)

    _g(work, "checkout", "-q", seed_sha, env=env)
    (work / "README.md").write_text("seed\ndetached change\n", encoding="utf-8")

    before_head = _head(work)
    before_status = _porcelain(work)
    assert before_status != "", "fixture broken: expected a dirty detached tree"

    result = _run_script(work, "checkpoint")

    assert result.returncode == 0, (
        f"rc={result.returncode} stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert result.stdout.strip() == "developer-commit: skipped (detached)"
    assert _head(work) == before_head
    assert _porcelain(work) == before_status


def test_missing_message_argument_exits_two(tmp_path):
    """Edge case for R2: a missing <message> exits 2 -- against a *real,
    existing* git work tree, so this can only pass because the script itself
    checks for <message>, not incidentally because the work-tree check also
    returns 2 for a path that doesn't exist (test-critic tautology::F1)."""
    _require_git()
    _require_tool(BASH, "bash")
    work, _remote = _make_repo(tmp_path)

    result = subprocess.run(
        [BASH, str(SCRIPT), str(work)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2, f"missing argument: rc={result.returncode} stderr={result.stderr!r}"


def test_non_repo_path_exits_two(tmp_path):
    """Edge case for R2: a non-repo path exits 2 even when <message> is
    given, isolating the work-tree check from the argument-count check."""
    _require_tool(BASH, "bash")

    not_a_repo = tmp_path / "not-a-repo"
    not_a_repo.mkdir()
    result = subprocess.run(
        [BASH, str(SCRIPT), str(not_a_repo), "msg"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2, f"non-repo path: rc={result.returncode} stderr={result.stderr!r}"
