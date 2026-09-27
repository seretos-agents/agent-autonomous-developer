"""
Live test for ticket #141 (R5, gatekeeper carve-out): drives a real
agent-harness `harness_start_agent` call that launches an
`agent-autonomous-developer:developer` run whose prompt backgrounds a
command and then ends its turn, and asserts the harness's own run output
carries the developer-scoped Stop-hook block reason
(hooks/check-session-turn-end.mjs's harness-launched-developer branch).

This is `none` as PR evidence per the plan's "Test / verification strategy"
R5: "agent-harness sets HARNESS_LAUNCHED_AGENT" is a premise this repository
cannot verify against a released binary (the installed
`~/.claude/plugins/cache/.../agent-harness/0.0.2/` predates PR #54 and has no
reference to the variable at all), so this file exists and is exercised only
by a human or CI job that opts in with `ADEV_LIVE_HARNESS=1` against a
harness release that actually sets it. CI runs it once, expecting exactly
`1 skipped`, via:

    ADEV_LIVE_HARNESS=1 python -m pytest -q tests/test_harness_launched_developer_live.py -rs

Skip conditions (each is checked, in order, before anything talks to a real
harness process):
  1. `ADEV_LIVE_HARNESS` is not `"1"` — the default; this is what CI's one
     command above exercises today, since no installed harness sets the env
     var yet.
  2. no `claude` executable on PATH — the harness launches a real `claude`
     child process for `harness_start_agent`.
  3. no harness MCP server binary found under any
     `~/.claude/plugins/*/cache/*/agent-harness/*/bin/harness[.exe]`
     (Windows and POSIX cache layouts both checked; picks the highest semver
     directory name when more than one is installed).
  4. the found binary's sibling `hooks/check-session-turn-end.mjs` is not
     byte-identical to *this checkout's* copy of the same file — "installed
     plugin is not this build". This is a narrower skip condition than
     "whoever has the harness and the claude CLI installed": a machine can
     have a real harness release with `HARNESS_LAUNCHED_AGENT` support and
     still get skipped here, if that release's copy of
     agent-autonomous-developer's own hook differs even by a byte from the
     one this test file ships next to (e.g. the installed cache is stale, or
     this checkout is mid-development). That is deliberate (see
     .adev/141-1/plan-critic-2/critique-merged.json misread::F1): a live run
     against a stale hook copy would prove nothing about *this* change, and
     silently reporting a pass in that case would be worse than skipping.
  5. `HARNESS_LAUNCHED_AGENT` never shows up in the launched developer run's
     recorded environment (whatever the harness's own child-process record
     exposes it as) — the one premise this whole ticket rests on, verified
     the one way this repo can verify it live: by driving the real thing
     rather than trusting agent-harness PR #54's description.

Protocol: the harness MCP server speaks JSON-RPC 2.0 over stdio
(`~/.claude/plugins/*/cache/*/agent-harness/*/.mcp.json` names
`./bin/harness` with no args, cwd the plugin root). This test does a minimal
hand-rolled client — `initialize`, then `tools/call` for
`harness_start_agent`, `harness_wait_run`, `harness_inspect_run` — rather
than pulling in a full MCP SDK, since the only thing this file needs is
three request/response round trips.
"""

from __future__ import annotations

import filecmp
import json
import os
import pathlib
import shutil
import subprocess
import threading

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
THIS_HOOK = REPO_ROOT / "hooks" / "check-session-turn-end.mjs"

HARNESS_DEVELOPER_ID = "agent-autonomous-developer:developer"

LIVE = os.environ.get("ADEV_LIVE_HARNESS") == "1"


def _version_key(name: str) -> tuple:
    parts = name.split(".")
    try:
        return tuple(int(p) for p in parts)
    except ValueError:
        return (-1,)


def _find_harness_installs() -> list[pathlib.Path]:
    """Every `.../agent-harness/<version>/` directory under any
    `~/.claude/plugins/*/cache/` (or the legacy `~/.claude/plugins/cache/`
    layout), newest semver first."""
    home = pathlib.Path.home()
    candidates: list[pathlib.Path] = []
    for cache_root in [
        home / ".claude" / "plugins" / "cache",
        *((home / ".claude" / "plugins").glob("*/cache")),
    ]:
        if not cache_root.is_dir():
            continue
        for marketplace_dir in cache_root.iterdir():
            harness_dir = marketplace_dir / "agent-harness"
            if not harness_dir.is_dir():
                continue
            for version_dir in harness_dir.iterdir():
                if version_dir.is_dir():
                    candidates.append(version_dir)
    candidates.sort(key=lambda p: _version_key(p.name), reverse=True)
    return candidates


def _find_harness_binary() -> pathlib.Path | None:
    for install_dir in _find_harness_installs():
        for name in ("harness.exe", "harness"):
            candidate = install_dir / "bin" / name
            if candidate.is_file():
                return candidate
    return None


class _JsonRpcStdioClient:
    """Minimal JSON-RPC 2.0 stdio client for one harness MCP server process."""

    def __init__(self, binary: pathlib.Path, env: dict) -> None:
        self._proc = subprocess.Popen(
            [str(binary)],
            cwd=str(binary.parent.parent),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            text=True,
            bufsize=1,
        )
        self._next_id = 1
        self._lock = threading.Lock()

    def call(self, method: str, params: dict, *, timeout: float = 60.0) -> dict:
        with self._lock:
            request_id = self._next_id
            self._next_id += 1
            body = json.dumps(
                {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
            )
            assert self._proc.stdin is not None
            self._proc.stdin.write(body + "\n")
            self._proc.stdin.flush()
            assert self._proc.stdout is not None
            line = self._proc.stdout.readline()
        if not line:
            stderr = self._proc.stderr.read() if self._proc.stderr else ""
            raise RuntimeError(f"harness MCP server closed stdout. stderr: {stderr!r}")
        return json.loads(line)

    def close(self) -> None:
        try:
            self._proc.terminate()
            self._proc.wait(timeout=10)
        except Exception:
            self._proc.kill()


@pytest.fixture()
def _live_gate(tmp_path):
    if not LIVE:
        pytest.skip("ADEV_LIVE_HARNESS not set")
    if shutil.which("claude") is None:
        pytest.skip("claude CLI not found on PATH")
    binary = _find_harness_binary()
    if binary is None:
        pytest.skip("no agent-harness bin/harness[.exe] found under ~/.claude/plugins/*/cache/")
    # The installed agent-autonomous-developer plugin's own copy of the hook
    # this ticket changes lives under a *different* plugin's cache entry (the
    # harness binary is under agent-harness/) -- both plugins share the same
    # marketplace cache root: .../<marketplace>/cache/<plugin>/<version>/...
    marketplace_cache = binary.parent.parent.parent.parent
    plugin_versions_dir = marketplace_cache / "agent-autonomous-developer"
    installed_hook = None
    if plugin_versions_dir.is_dir():
        version_dirs = sorted(
            (d for d in plugin_versions_dir.iterdir() if d.is_dir()),
            key=lambda p: _version_key(p.name),
            reverse=True,
        )
        for version_dir in version_dirs:
            candidate = version_dir / "hooks" / "check-session-turn-end.mjs"
            if candidate.is_file():
                installed_hook = candidate
                break
    if installed_hook is None:
        pytest.skip(
            "no installed agent-autonomous-developer hooks/check-session-turn-end.mjs "
            "found alongside the harness binary — cannot confirm the installed plugin "
            "is this build"
        )
    if not filecmp.cmp(installed_hook, THIS_HOOK, shallow=False):
        pytest.skip(
            "installed plugin is not this build: "
            f"{installed_hook} differs from {THIS_HOOK} byte-for-byte"
        )
    return binary


def test_harness_launched_developer_blocked_on_unresolved_background(_live_gate, tmp_path):
    """R5 driving scenario (not required PR evidence): a real
    `harness_start_agent` call whose developer prompt backgrounds a `sleep 30`
    and ends its turn must be blocked by the harness-launched-developer Stop
    branch, and the run's recorded output must carry the developer-scoped
    block reason plus confirmation that HARNESS_LAUNCHED_AGENT was actually
    set in the child's environment.
    """
    binary = _live_gate
    cwd = tmp_path / "work"
    cwd.mkdir()  # deliberately no .adev/ — a harness-launched developer's
    # cwd need not carry the pipeline marker at all.

    client = _JsonRpcStdioClient(binary, env=dict(os.environ))
    try:
        init = client.call(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "adev-live-test", "version": "0"},
            },
        )
        assert "result" in init, f"harness MCP initialize failed: {init!r}"

        start = client.call(
            "tools/call",
            {
                "name": "harness_start_agent",
                "arguments": {
                    "agent": HARNESS_DEVELOPER_ID,
                    "cwd": str(cwd),
                    "prompt": (
                        "Run Bash with run_in_background: true for the command "
                        "`sleep 30`, then end your turn immediately without "
                        "waiting for it."
                    ),
                },
            },
        )
        assert "result" in start, f"harness_start_agent failed: {start!r}"
        run_id = start["result"].get("run_id") or start["result"].get("runId")
        assert run_id, f"harness_start_agent result carried no run id: {start!r}"

        waited = client.call(
            "tools/call",
            {"name": "harness_wait_run", "arguments": {"run_id": run_id, "timeout": 120}},
        )
        assert "result" in waited, f"harness_wait_run failed: {waited!r}"

        inspected = client.call(
            "tools/call",
            {"name": "harness_inspect_run", "arguments": {"run_id": run_id}},
        )
        assert "result" in inspected, f"harness_inspect_run failed: {inspected!r}"

        # The developer confirms, from the real inspect_run payload, which
        # field carries the run's stdout/transcript text — search the whole
        # serialized result rather than pin one field name in advance.
        haystack = json.dumps(inspected["result"])
        assert HARNESS_DEVELOPER_ID in haystack or "HARNESS_LAUNCHED_AGENT" in haystack, (
            "harness_inspect_run result carries no trace of "
            f"HARNESS_LAUNCHED_AGENT having been set for this run: {haystack[:2000]!r}"
        )
        assert "developer: turn is ending with a backgrounded command still outstanding" in haystack, (
            f"expected the harness-launched-developer Stop block reason in the run "
            f"output; got: {haystack[:2000]!r}"
        )
    finally:
        client.close()
