#!/usr/bin/env bash
# scripts/developer-commit.sh <worktree_path> <message>
#
# Ticket #140 (code half of #138): the commit-only Checkpoint step a
# developer dispatch calls as its own last tool call, so a hard kill between
# the subagent's last edit and the orchestrating turn's own Checkpoint does
# not lose the work. Style follows scripts/ci-wait-pipeline.sh: bash, set -u,
# every git call is `git -C "$wt"`.
#
# No push path here -- pushing (and push-mode selection) stays with the
# orchestrating turn's Checkpoint. This script only ever commits locally, or
# skips, never touching the remote.
#
# Exit codes:
#   0 - committed, or a guard applied and the script skipped.
#   2 - a required argument is missing, or <worktree_path> is not a git work
#       tree.
#   <n> - a git failure's own exit code, passed straight through.
set -u

wt="${1:-}"
message="${2:-}"

if [ -z "$wt" ] || [ -z "$message" ]; then
  echo "usage: developer-commit.sh <worktree_path> <message>" >&2
  exit 2
fi

if ! git -C "$wt" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "developer-commit: not a git work tree: $wt" >&2
  exit 2
fi

# Skip guards, in SKILL.md Checkpoint step-1 order: clean, then rebase, then
# detached. A skip touches nothing -- HEAD and the tree are left exactly as
# found.

if [ -z "$(git -C "$wt" status --porcelain)" ]; then
  echo "developer-commit: skipped (clean)"
  exit 0
fi

# --absolute-git-dir (not the plan's --git-path) so the rebase-state check is
# correct regardless of this script's own cwd, not just $wt's.
git_dir="$(git -C "$wt" rev-parse --absolute-git-dir)" || exit 2
if [ -d "$git_dir/rebase-merge" ] || [ -d "$git_dir/rebase-apply" ]; then
  echo "developer-commit: skipped (rebase)"
  exit 0
fi

branch="$(git -C "$wt" rev-parse --abbrev-ref HEAD)"
if [ "$branch" = "HEAD" ]; then
  echo "developer-commit: skipped (detached)"
  exit 0
fi

git -C "$wt" add -A
rc=$?
if [ "$rc" -ne 0 ]; then
  exit "$rc"
fi

git -C "$wt" commit -q -m "$message"
rc=$?
if [ "$rc" -ne 0 ]; then
  exit "$rc"
fi

sha="$(git -C "$wt" rev-parse --short HEAD)"
echo "developer-commit: committed $sha"
exit 0
