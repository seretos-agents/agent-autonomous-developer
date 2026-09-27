#!/usr/bin/env python3
"""Did the pipeline actually run what this package added to it? (ticket #122)

Phase 6 used to post `ci-green` as soon as every run on the PR head reported
green. A workflow the diff added a job to, but which never ran for the PR (a
`workflow_dispatch`-only release workflow, a brand-new workflow file), is
invisible in that JSON: the promised job is *absent*, not red. This check
looks for that absence.

This module does not itself claim what "green" means for a run: that
vocabulary (status `completed`, conclusion `success`) is defined by the
agent-project-issues run-vocabulary contract — see the skill section
"Reading a run's `status` and `conclusion`" (this contract is documented as
of agent-project-issues 0.3.11, ticket agent-project-issues#357) — and
`has_successful_run` below just applies it.

Input (stdin, one JSON object):
  {"worktree": "<path>", "base": "<branch>", "head": "<sha>",
   "runs": [{"name", "status", "conclusion", "head_sha", "url"}, ...]}
  `runs` is `list_pipeline_runs(commit_sha=head)`'s `runs` array.

For every workflow file under .github/workflows/ that the diff base...head adds
or modifies, the jobs present at head but not at the merge base are the
*added jobs*. Each such workflow needs a completed, successful run in `runs`
whose name equals the workflow's `name:` (its path when it has none),
case-insensitively. Otherwise it is a gap.

Output (stdout): `verdict: ok` or `verdict: gap` plus one `gap: ...` line per
workflow. Exit: 0 ok, 2 gap, 1 unusable input (diagnostic on stderr, never ok).

Deterministic and offline: no network, no model, only literal `git` argv.
"""
import json
import re
import subprocess
import sys

WORKFLOW_DIR = ".github/workflows/"
JOB_KEY = re.compile(r"^(\s+)([A-Za-z0-9_][\w.-]*|\"[^\"]+\"|'[^']+')\s*:\s*(#.*)?$")
NAME_KEY = re.compile(r"^name\s*:\s*(.*?)\s*$")


class Unusable(Exception):
    pass


def git(worktree, *args):
    proc = subprocess.run(["git", "-C", worktree, *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    return proc.returncode, proc.stdout, proc.stderr


def resolve(worktree, ref):
    for candidate in (ref, "origin/" + ref):
        rc, out, _ = git(worktree, "rev-parse", "--verify", "--quiet", candidate + "^{commit}")
        if rc == 0 and out.strip():
            return out.strip()
    raise Unusable(f"cannot resolve base {ref!r} in {worktree!r}")


def unquote(text):
    text = text.split(" #", 1)[0].strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "'\"":
        text = text[1:-1]
    return text


def parse_workflow(text):
    """(name or None, set of job ids) from a workflow file, without a YAML library."""
    name, jobs = None, set()
    lines = text.splitlines()
    in_jobs, child_indent = False, None
    for line in lines:
        if not in_jobs:
            m = NAME_KEY.match(line)
            if m and name is None and m.group(1):
                name = unquote(m.group(1))
            if re.match(r"^jobs\s*:\s*(#.*)?$", line):
                in_jobs = True
            continue
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line[0].isspace():  # next top-level key ends the jobs block
            in_jobs = False
            continue
        m = JOB_KEY.match(line)
        if not m:
            continue
        indent = len(m.group(1).expandtabs())
        if child_indent is None:
            child_indent = indent
        if indent == child_indent:
            jobs.add(unquote(m.group(2)))
    return name, jobs


def added_jobs_by_workflow(worktree, base, head):
    rc, out, err = git(worktree, "diff", "--name-status", "--no-renames", f"{base}...{head}")
    if rc != 0:
        raise Unusable(f"git diff {base}...{head} failed: {err.strip()}")
    rc, mb, _ = git(worktree, "merge-base", base, head)
    base_ref = mb.strip() if rc == 0 and mb.strip() else base
    found = {}
    for row in out.splitlines():
        status, _, path = row.partition("\t")
        if status[:1] == "D" or not path.startswith(WORKFLOW_DIR):
            continue
        if not path.endswith((".yml", ".yaml")):
            continue
        rc, head_text, err = git(worktree, "show", f"{head}:{path}")
        if rc != 0:
            raise Unusable(f"cannot read {path} at {head}: {err.strip()}")
        rc, base_text, _ = git(worktree, "show", f"{base_ref}:{path}")
        name, head_jobs = parse_workflow(head_text)
        _, base_jobs = parse_workflow(base_text if rc == 0 else "")
        added = sorted(head_jobs - base_jobs)
        if added:
            found[path] = (name or path, added)
    return found


def has_successful_run(runs, workflow_name):
    wanted = workflow_name.lower()
    # "completed" + "success" is the agent-project-issues run-vocabulary
    # contract cited in the module docstring above, not this script's own
    # assumption.
    return any(
        isinstance(r, dict)
        and str(r.get("name", "")).lower() == wanted
        and r.get("status") == "completed"
        and r.get("conclusion") == "success"
        for r in runs
    )


def main():
    try:
        try:
            payload = json.loads(sys.stdin.read())
        except ValueError as exc:
            raise Unusable(f"input is not valid JSON: {exc}")
        if not isinstance(payload, dict):
            raise Unusable("input JSON must be an object")
        for key in ("worktree", "base", "head", "runs"):
            if key not in payload:
                raise Unusable(f"missing key: {key}")
        if not isinstance(payload["runs"], list):
            raise Unusable("`runs` must be a list")
        worktree, head = str(payload["worktree"]), str(payload["head"])
        base = resolve(worktree, str(payload["base"]))
        gaps = []
        for path, (wf_name, jobs) in sorted(added_jobs_by_workflow(worktree, base, head).items()):
            if not has_successful_run(payload["runs"], wf_name):
                gaps.append(
                    f"gap: {path} adds job(s) {', '.join(jobs)} but no successful run of "
                    f"workflow {wf_name!r} exists at {head[:12]}"
                )
    except Unusable as exc:
        print(f"ci-promised-check: {exc}", file=sys.stderr)
        return 1
    if gaps:
        print("verdict: gap")
        print("\n".join(gaps))
        return 2
    print("verdict: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
