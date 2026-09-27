"""
tools/live_kill_test.py

Ticket #140 (code half of #138): a one-command harness that reproduces the
ticket's symptom -- "after a pipeline session is killed from outside, files
the developer subagent already wrote never reach git, and the retry starts
from nothing" -- by running a real `claude -p` session, hard-killing it the
instant its turn ends (the terminal `result` stream-json event, and only
that event -- any `tool_result` trigger kills early when a test run sits
between the last edit and the commit, which was round 2's defect), and
reporting whether the files it edited ended up committed on the branch.

Public surface (driving tests import these directly; see
tests/test_live_kill_test.py):

  - run_and_kill(argv, cwd, timeout) -- spawns argv in cwd, reads its
    stream-json stdout, tracks `input.file_path` of Write/Edit/MultiEdit
    tool_use blocks into `edited_files` (repo-relative), and tree-kills the
    process the instant a top-level `{"type": "result"}` event is read.
    Returns {"killed": bool, "trigger": "result" | None, "edited_files": [str]}.
    `trigger` is None on EOF/timeout before a `result` event was ever seen --
    never a verdict in that case. `killed` is True only when the process was
    still alive at the moment this function tried to kill it; a real CLI may
    have already exited on its own (no tool call follows `result`, so the
    verdict is unaffected either way).
  - classify_files(repo, base_sha, edited_files) -- per file: committed when
    `git status --porcelain -- f` is empty and `git log base..HEAD -- f` is
    non-empty; returns (committed_files, uncommitted_files).
  - build_report(repo, base_sha, result) -- combines a run_and_kill() result
    with classify_files() into the one JSON-line report shape: repo, killed,
    trigger, edited_files, committed_files, uncommitted_files.
  - exit_code_for(report) -- 0 = all edited files committed, 1 = some
    uncommitted, 2 = harness error (no `result` trigger observed, or no edit
    ever seen -- never a verdict in either case).
  - main(argv) -- `python tools/live_kill_test.py [--timeout 300]`: creates
    its own temp git repo, runs the real `claude` CLI against DEFAULT_PROMPT,
    prints the one JSON report line, and returns the matching exit code.

Not in scope (per the plan's Mechanism balance / Removed, and the ticket's
own "Not proven by this package"): driving a real developer-*subagent*
dispatch (plugin-loaded session, Agent tool, an existing worktree). This
harness's DEFAULT_PROMPT is a plain `claude -p` session instructed to run
the real scripts/developer-commit.sh as its last tool call -- it proves the
kill/report mechanism against a real CLI process, not that any particular
future agents/developer.md prose (#138, still to be written) actually calls
the script.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
DEVELOPER_COMMIT_SCRIPT = REPO_ROOT / "scripts" / "developer-commit.sh"

_EDIT_TOOL_NAMES = {"Write", "Edit", "MultiEdit"}


# ---------------------------------------------------------------------------
# run_and_kill: drive a stream-json subprocess, kill it at `result`.
# ---------------------------------------------------------------------------


def _spawn(argv: list[str], cwd: str) -> subprocess.Popen:
    kwargs: dict = dict(
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        text=True,
        bufsize=1,
    )
    if sys.platform != "win32":
        kwargs["start_new_session"] = True
    return subprocess.Popen(argv, **kwargs)


def _tree_kill(proc: subprocess.Popen) -> bool:
    """Hard-kill the whole process tree. Returns True only if the process
    was still alive at the moment we tried -- a real CLI may exit first."""
    if proc.poll() is not None:
        return False
    if sys.platform == "win32":
        # `claude` may be a `.cmd` shim; Popen.kill() would reach only the
        # shim, not the real child process it launches. /T kills the tree.
        subprocess.run(
            ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
            capture_output=True,
        )
    else:
        import signal

        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            return False
    return True


def _relativize(file_path: str, cwd: str) -> str:
    if os.path.isabs(file_path):
        file_path = os.path.relpath(file_path, cwd)
    return file_path.replace(os.sep, "/")


def _edited_paths_in(event: dict) -> list[str]:
    message = event.get("message")
    if not isinstance(message, dict):
        return []
    content = message.get("content")
    if not isinstance(content, list):
        return []
    paths = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") != "tool_use":
            continue
        if block.get("name") not in _EDIT_TOOL_NAMES:
            continue
        input_ = block.get("input")
        if not isinstance(input_, dict):
            continue
        file_path = input_.get("file_path")
        if file_path:
            paths.append(file_path)
    return paths


def run_and_kill(argv: list[str], cwd: str, timeout: float) -> dict:
    """Spawn argv in cwd, read its stream-json stdout, and hard-kill the
    whole process tree the instant a top-level `result` event is read."""
    proc = _spawn(argv, cwd)

    line_queue: "queue.Queue[str | None]" = queue.Queue()

    def _reader() -> None:
        try:
            for line in proc.stdout:  # type: ignore[union-attr]
                line_queue.put(line)
        finally:
            line_queue.put(None)  # EOF sentinel

    reader_thread = threading.Thread(target=_reader, daemon=True)
    reader_thread.start()

    edited_files: list[str] = []
    seen: set[str] = set()
    trigger: str | None = None
    eof = False

    start = time.monotonic()
    while True:
        remaining = timeout - (time.monotonic() - start)
        if remaining <= 0:
            break
        try:
            line = line_queue.get(timeout=remaining)
        except queue.Empty:
            break
        if line is None:
            eof = True
            break
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue

        event_type = event.get("type")
        if event_type == "assistant":
            for file_path in _edited_paths_in(event):
                rel = _relativize(file_path, cwd)
                if rel not in seen:
                    seen.add(rel)
                    edited_files.append(rel)
        elif event_type == "result":
            trigger = "result"
            break

    if trigger == "result":
        killed = _tree_kill(proc)
    elif not eof and proc.poll() is None:
        # Timed out while the process was still running, with no `result`
        # ever seen -- clean up rather than leaving it running.
        killed = _tree_kill(proc)
    else:
        killed = False

    return {"killed": killed, "trigger": trigger, "edited_files": edited_files}


# ---------------------------------------------------------------------------
# Report: committed/uncommitted classification and exit code.
# ---------------------------------------------------------------------------


def _git(repo, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
    )


def classify_files(
    repo, base_sha: str, edited_files: list[str]
) -> tuple[list[str], list[str]]:
    """Per the plan: a file is committed when `status --porcelain -- f` is
    empty and `log base..HEAD -- f` is non-empty."""
    committed_files = []
    uncommitted_files = []
    for relpath in edited_files:
        status = _git(repo, "status", "--porcelain", "--", relpath).stdout
        log = _git(repo, "log", f"{base_sha}..HEAD", "--", relpath).stdout
        if status.strip() == "" and log.strip() != "":
            committed_files.append(relpath)
        else:
            uncommitted_files.append(relpath)
    return committed_files, uncommitted_files


def build_report(repo, base_sha: str, result: dict) -> dict:
    committed_files, uncommitted_files = classify_files(repo, base_sha, result["edited_files"])
    return {
        "repo": str(repo),
        "killed": result["killed"],
        "trigger": result["trigger"],
        "edited_files": result["edited_files"],
        "committed_files": committed_files,
        "uncommitted_files": uncommitted_files,
    }


def exit_code_for(report: dict) -> int:
    """0 = all committed, 1 = some uncommitted, 2 = harness error (no
    `result` trigger observed, or no edit ever seen) -- never a verdict."""
    if report["trigger"] is None or not report["edited_files"]:
        return 2
    if report["uncommitted_files"]:
        return 1
    return 0


def _reason_for(report: dict) -> str | None:
    if report["trigger"] is None:
        return "trigger not observed"
    if not report["edited_files"]:
        return "no edit observed"
    return None


# ---------------------------------------------------------------------------
# One-command real run (R4): a temp repo + the real `claude` CLI.
# ---------------------------------------------------------------------------


def _git_env() -> dict:
    env = dict(os.environ)
    env.setdefault("GIT_AUTHOR_NAME", "live-kill-test")
    env.setdefault("GIT_AUTHOR_EMAIL", "live-kill-test@example.invalid")
    env.setdefault("GIT_COMMITTER_NAME", "live-kill-test")
    env.setdefault("GIT_COMMITTER_EMAIL", "live-kill-test@example.invalid")
    return env


def create_temp_repo(path) -> None:
    env = _git_env()
    subprocess.run(["git", "init", "-q", "-b", "live-kill", str(path)], check=True, env=env)
    subprocess.run(
        ["git", "-C", str(path), "config", "commit.gpgsign", "false"], check=True, env=env
    )
    (pathlib.Path(path) / "README.md").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True, env=env)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "seed"], check=True, env=env)


def _rev_parse_head(repo) -> str:
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def default_prompt(repo, script=DEVELOPER_COMMIT_SCRIPT) -> str:
    """The realistic developer shape (plan's Approach, "Default scenario"):
    an edit, a non-edit result (RED-run stand-in), another edit, a second
    non-edit result (GREEN-run stand-in), then the commit script as the
    *last* tool call, then a plain text reply."""
    return (
        "In this directory, using only the Write, Edit and Bash tools, and "
        "in exactly this order:\n"
        "1. Write a file named a.txt containing exactly: hello\n"
        f"2. Run: git -C \"{repo}\" status --short\n"
        "3. Edit a.txt so its content is exactly: hello\\nedited\n"
        f"4. Run: git -C \"{repo}\" diff --stat\n"
        "5. As your very last tool call, run: "
        f"bash \"{script}\" \"{repo}\" \"live kill test\"\n"
        "6. Reply with exactly the word: done\n"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run a real `claude -p` session, hard-kill it at the turn's "
            "terminal `result` event, and report whether the files it "
            "edited ended up committed on the branch."
        )
    )
    parser.add_argument("--timeout", type=int, default=300)
    args = parser.parse_args(argv)

    claude = shutil.which("claude")
    if claude is None:
        print(
            json.dumps({"error": "claude CLI not found on PATH"}),
        )
        return 2

    with tempfile.TemporaryDirectory(prefix="live-kill-") as tmp:
        repo = pathlib.Path(tmp) / "repo"
        create_temp_repo(repo)
        base_sha = _rev_parse_head(repo)

        prompt = default_prompt(repo)
        cli_argv = [
            claude,
            "-p",
            prompt,
            "--output-format",
            "stream-json",
            "--verbose",
            "--allowedTools",
            "Write,Edit,Bash",
        ]

        result = run_and_kill(cli_argv, cwd=str(repo), timeout=args.timeout)
        report = build_report(repo, base_sha, result)
        print(json.dumps(report))

        reason = _reason_for(report)
        if reason:
            print(f"live_kill_test: {reason}", file=sys.stderr)
        return exit_code_for(report)


if __name__ == "__main__":
    raise SystemExit(main())
