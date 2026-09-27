#!/usr/bin/env python3
"""Deterministic renderer for the `<!-- adev:event v1 ... -->` block (#134).

The gatekeeper's symptom (Frame comment 5843754508 on #134): posted
`adev:event` comments reach the ticket in three shapes (raw, fenced,
HTML-escaped), with 4 or 5 gates on the `rounds:` line and a trailing space
after an empty `pr:`/`ci_run:`, so a deterministic external parser
(ecosystem-statistics) misreads or rejects them. This script is the single,
model-free producer of that block: given the fields as flags, it prints the
block in exactly one shape (raw, all five gates, no trailing whitespace) and
nothing else, or rejects the call outright rather than posting a half-block.

Usage:
  python scripts/event_block.py --event <name> --package <id>
      [--attempt N] [--generation G] [--gate NAME=U,F,I ...]
      [--pr N] [--ci-run ID]

Output (stdout, exit 0): the 9-line block, `\n`-joined, one trailing `\n`.
Rejection (exit 2): stdout empty, stderr starts `event_block: error:`.

This module posts nothing itself and calls no MCP/API — it only renders text
handed to it on argv. Wiring every post-site to call it is #135's job, not
this one's (see the plan, `.adev/134-1/plan.md`).
"""
import argparse
import re
import sys

PROG = "event_block"

# The closed 13-name event vocabulary. A test in tests/test_pipeline_contract.py
# ties this tuple to that file's own `EVENTS` list so the two cannot drift
# silently; SKILL.md's prose copy of the same list is #135's concern.
EVENTS = (
    "started", "plan-committed", "plan-critic-verdict", "tests-red",
    "test-critic-verdict", "tests-green", "review-verdict", "pr-opened",
    "ci-red", "replan-triggered", "ci-green", "blocked", "failed",
)

# The five gates, in the fixed output order (SKILL.md lines 39-48 plus the
# `rebase=` gate on line 52). This order is the parser contract.
GATES = ("plan-critic", "test-critic", "review", "ci", "rebase")

# One validation rule for every flag value: reject any whitespace character
# or the substring `-->`. SKILL.md types every value as a single token, and
# `-->` would prematurely close the HTML comment for an external parser.
_BAD_CHARS = re.compile(r"[ \t\n\r]|-->")

_INT_RE = re.compile(r"-?\d+")
_NONNEG_INT_RE = re.compile(r"\d+")


class EventBlockError(Exception):
    """A rejected call. The message becomes `event_block: error: <message>`."""


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        # Never let argparse print its own usage/error text or call
        # sys.exit() directly -- route every usage error through the same
        # `event_block: error:` prefix as every other rejection.
        raise EventBlockError(message)


def _check_no_bad_chars(value, what):
    if _BAD_CHARS.search(value):
        raise EventBlockError(
            f"{what} must not contain whitespace or '-->': {value!r}")


def _parse_nonneg_int(value, what):
    if not _INT_RE.fullmatch(value):
        raise EventBlockError(f"{what} must be an integer: {value!r}")
    n = int(value)
    if n < 0:
        raise EventBlockError(f"{what} must not be negative: {value!r}")
    return n


def _parse_gate(raw):
    """`NAME=U,F,I` -> (name, {"u": ..., "f": ..., "i": ...}), all still str."""
    _check_no_bad_chars(raw, "--gate")
    name, sep, counts = raw.partition("=")
    if not sep:
        raise EventBlockError(f"--gate must be NAME=U,F,I: {raw!r}")
    if name not in GATES:
        raise EventBlockError(f"unknown gate name: {name!r}")
    parts = counts.split(",")
    if len(parts) != 3:
        raise EventBlockError(f"--gate {name} must have exactly U,F,I: {raw!r}")
    for part in parts:
        if not _NONNEG_INT_RE.fullmatch(part):
            raise EventBlockError(
                f"--gate {name} counts must be non-negative integers: {raw!r}")
    u, f, i = parts
    return name, {"u": u, "f": f, "i": i}


def parse_args(argv):
    parser = _Parser(prog=PROG, add_help=False)
    parser.add_argument("--event")
    parser.add_argument("--package")
    parser.add_argument("--attempt", default="1")
    parser.add_argument("--generation", default="1")
    parser.add_argument("--gate", action="append", default=[])
    parser.add_argument("--pr", default="")
    parser.add_argument("--ci-run", default="")
    return parser.parse_args(argv)


def render(args):
    # One rule, applied to every flag value before anything else looks at it.
    _check_no_bad_chars(args.event or "", "--event")
    _check_no_bad_chars(args.package or "", "--package")
    _check_no_bad_chars(args.attempt, "--attempt")
    _check_no_bad_chars(args.generation, "--generation")
    _check_no_bad_chars(args.pr, "--pr")
    _check_no_bad_chars(args.ci_run, "--ci-run")

    if not args.event:
        raise EventBlockError("--event must not be empty")
    if not args.package:
        raise EventBlockError("--package must not be empty")
    if args.event not in EVENTS:
        raise EventBlockError(f"unknown event: {args.event!r}")

    attempt = _parse_nonneg_int(args.attempt, "--attempt")
    generation = _parse_nonneg_int(args.generation, "--generation")
    if generation not in (1, 2):
        raise EventBlockError(f"--generation must be 1 or 2: {args.generation!r}")

    gates = {name: {"u": "0", "f": "0", "i": "0"} for name in GATES}
    seen = set()
    for raw in args.gate:
        name, counts = _parse_gate(raw)
        if name in seen:
            raise EventBlockError(f"duplicate --gate: {name!r}")
        seen.add(name)
        gates[name] = counts

    rounds = " ".join(
        f"{name}={gates[name]['u']}/3({gates[name]['f']}f,{gates[name]['i']}i)"
        for name in GATES
    )

    lines = [
        "<!-- adev:event v1",
        f"event: {args.event}",
        f"package: {args.package}",
        f"attempt: {attempt}",
        f"generation: {generation}/2",
        f"rounds: {rounds}",
        "pr:" + (f" {args.pr}" if args.pr else ""),
        "ci_run:" + (f" {args.ci_run}" if args.ci_run else ""),
        "-->",
    ]
    return "\n".join(lines) + "\n"


def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]
    try:
        args = parse_args(argv)
        block = render(args)
    except EventBlockError as exc:
        print(f"{PROG}: error: {exc}", file=sys.stderr)
        return 2
    sys.stdout.write(block)
    return 0


if __name__ == "__main__":
    sys.exit(main())
