#!/usr/bin/env bash
#
# canonical_dir — the form of a directory path that means the same thing on both sides of this
# machine's two path namespaces.
#
# The Bash tool on this machine is Git Bash (MSYS2), which synthesises a single POSIX tree over
# the drives: `/c`, `/e`, … carry a drive letter, but `/` is the Git installation and `/tmp` is
# C:/Users/<user>/AppData/Local/Temp. Those last two carry no drive letter, and that is where the
# two namespaces disagree: a native Windows process resolves a leading slash against the root of
# the *current drive*, so the string "/tmp/x" names AppData\Local\Temp\x to this shell and
# C:\tmp\x to a file tool — a real, silently created, different directory.
#
# That matters wherever a path crosses from a script into an agent: the critic runners write with
# the shell and then report where they wrote, and plan-critic/unit-test-critic read those reports with
# native file tools. `cd && pwd` is the wrong thing to report, because it answers in the dialect
# only one of the two sides speaks. cygpath -m answers in the mixed form (`C:/Users/…`) — a drive
# letter with forward slashes, which the shell and native tools resolve identically.
#
# Sourced, not executed: `. "$(dirname "$0")/win-path.sh"`.

# Echoes the canonical absolute form of an existing directory. Falls back to the shell's own answer
# where cygpath does not exist (a non-Windows host), which is correct there for the same reason it
# is wrong here: on such a host there is only one namespace to be right about.
canonical_dir() {
  local resolved
  resolved="$(cd "$1" && pwd)" || return 1
  if command -v cygpath >/dev/null 2>&1; then
    cygpath -m "$resolved"
  else
    printf '%s\n' "$resolved"
  fi
}
