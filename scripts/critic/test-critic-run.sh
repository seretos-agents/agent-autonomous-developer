#!/usr/bin/env bash
#
# Runs the test critique as one isolated critic.
#
# ISOLATION
#
# The entire value of the test-critic role rests on the critique running WITHOUT the project's
# context: no CLAUDE.md, no skills, no agent definitions, no MCP servers, no tools, no plugins. The
# critic sees exactly the review package it is handed and nothing else. That is what makes its
# answer independently derived rather than a reworded echo of the same context the developer
# worked in — the developer shares every skill and convention document with anyone who would
# review the tests from inside the project, and would hand its blind spots straight on.
#
# Every isolation flag below is load-bearing, and the flag set is deliberately identical across
# both runners (this one and plan-critic-run.sh). Measured on this machine (2026-07-30, claude 2.1.220; re-measured 2026-08-14, claude
# 2.1.229): a plain `claude -p` started in an empty directory inherits user-level plugins, their
# skills, their MCP servers and their hooks — and it also reads a CLAUDE.md sitting in its own
# working directory. Until 2026-08-14 this comment claimed the opposite about that last point. The
# claim was wrong; it understated the exposure rather than weakening the gate, since the flags do
# exclude all of it. Dropping `--setting-sources ""` silently re-contaminates the critic.
# Do not "simplify" this invocation. A provenance record is written next to the result so a given
# run's isolation is checkable rather than assumed.
#
# The static half of check-critic-isolation.sh runs below, on every invocation of this script, so
# this comment is not the only thing standing between a CLI change and a silently contaminated run.
# The expensive --live measurement stays a deliberate act.
#
# ONE LENS, MECHANICAL MERGE
#
# plan-critic runs three critics because a plan can be wrong in three unrelated ways and one critic
# reading for all of them reads for none in particular. That argument does not carry here: there is
# exactly one question at this point in the workflow — which broken implementation would still pass
# this assertion — so a second and third process would answer the same question again rather than a
# different one. Hence one run. If a second lens is ever genuinely justified, add it to
# test-critic-package.sh's LENS_IDS and this script will run it.
#
# The result is nonetheless passed through plan-critic-merge.py, so that the dispatching skill reads
# one file shape (critique-merged.json with severity_counts, lens_runs and findings) from both gates
# instead of two. The merge is mechanical and model-free; with a single lens it deduplicates
# nothing. It only knows the keys findings / solid / unverifiable_without_codebase_access, so the
# per-assertion `assertion_assessments` array is NOT carried into the merged file — it stays in
# critique-tautology.json, which is written alongside. The findings (with their `layer`) are what
# the dispatching skill acts on, and those come through unchanged; anyone wanting the
# assertion-by-assertion view reads the per-lens file.
#
# The merge also stamps every finding with a `finding_class` (ticket #105): "blocking" or "note",
# derived from the lens. `tautology` is not in the merge's note-lens set, so every test-critic
# finding defaults to "blocking" — unchanged behaviour, the default exists for plan-critic's
# untestable/simplifier lenses, not for this gate.
#
# Usage: test-critic-run.sh <plan-file> <tests-file> <output-dir>
#   plan-file    the planner's plan, verbatim
#   tests-file   the test code the developer wrote (diff or full test files, concatenated), verbatim
#
# Writes into <output-dir>, per lens <L>:
#   package-<L>.txt              the assembled review package
#   critique-<L>.raw.json        the CLI result envelope, untouched
#   critique-<L>.json            the validated critique object, as the schema defines it
#   critique-<L>.provenance.txt  how that process was actually started
#   critique-<L>.stderr.txt      its stderr
# and once:
#   critique-merged.json         the findings in the same shape plan-critic-run.sh produces
#
set -euo pipefail

if [ $# -ne 3 ]; then
  echo "usage: $0 <plan-file> <tests-file> <output-dir>" >&2
  exit 2
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# python3 is the usual name; on a Windows host with the Store launcher it may only exist as `python`.
PY="$(command -v python3 || command -v python || true)"
[ -n "$PY" ] || { echo "python3/python not found on PATH" >&2; exit 2; }

# --- isolation pre-flight ------------------------------------------------------------------------
# The static half of the isolation check runs here, on every gate invocation, instead of being asked
# for in prose. The prose version existed in the project this was ported from and was measurably
# not followed: the recorded live measurement sat three CLI upgrades behind the installed version,
# and nothing had noticed. An instruction that depends on somebody remembering it is not a check.
# Costs about a second and no API call; the expensive --live measurement stays a deliberate act.
# Its own output is suppressed on success, so a green pre-flight is silent and a failure is not.
if ! "$SCRIPT_DIR/check-critic-isolation.sh" >/dev/null; then
  echo "FATAL: the isolation pre-flight failed (detail above). Refusing to run a gate whose" >&2
  echo "       isolation is unproven — a contaminated critique is worse than no critique." >&2
  exit 1
fi

PLAN="$(cd "$(dirname "$1")" && pwd)/$(basename "$1")"
TESTCODE="$(cd "$(dirname "$2")" && pwd)/$(basename "$2")"
mkdir -p "$3"
# Canonical, drive-qualified form — every path this script reports is derived from it, and
# test-critic reads those reports with native file tools that resolve a bare "/tmp/…" somewhere
# else entirely. See win-path.sh.
. "$SCRIPT_DIR/win-path.sh"
OUTDIR="$(canonical_dir "$3")"

SCHEMA="$SCRIPT_DIR/test-critic-schema.json"
SYSPROMPT="$SCRIPT_DIR/test-critic-system-prompt.txt"
PACKAGER="$SCRIPT_DIR/test-critic-package.sh"
MERGER="$SCRIPT_DIR/plan-critic-merge.py"

for f in "$PLAN" "$TESTCODE" "$SCHEMA" "$SYSPROMPT" "$PACKAGER" "$MERGER"; do
  [ -f "$f" ] || { echo "missing required file: $f" >&2; exit 2; }
done

# The lens list comes from the packager, so there is exactly one place that defines which lenses
# exist and this script cannot drift out of step with it.
LENSES="$(bash "$PACKAGER" --list-lenses)"

run_one_lens() {
  local lens="$1"
  local pkg="$OUTDIR/package-$lens.txt"
  local raw="$OUTDIR/critique-$lens.raw.json"
  local out="$OUTDIR/critique-$lens.json"
  local prov="$OUTDIR/critique-$lens.provenance.txt"
  local err="$OUTDIR/critique-$lens.stderr.txt"

  bash "$PACKAGER" "$PLAN" "$TESTCODE" "$lens" "$pkg" >/dev/null

  # A fresh, empty directory outside the repository. Not a worktree, not a subdirectory of the
  # project — CLAUDE.md discovery walks parent directories, so anything under the repo root would
  # defeat the isolation.
  local workdir
  workdir="$(mktemp -d)"

  {
    echo "test-critic isolated run"
    echo "lens: $lens"
    echo "timestamp_utc: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "claude_version: $(claude --version 2>&1)"
    echo "cwd: $workdir"
    echo "cwd_contents: $(ls -A "$workdir" | tr '\n' ' ')<empty above means no inherited project files>"
    echo "package: $pkg"
    echo "package_sha256: $(sha256sum "$pkg" | cut -d' ' -f1)"
    echo "package_bytes: $(wc -c < "$pkg")"
    echo "plan_sha256: $(sha256sum "$PLAN" | cut -d' ' -f1)"
    echo "testcode_sha256: $(sha256sum "$TESTCODE" | cut -d' ' -f1)"
    echo "system_prompt_sha256: $(sha256sum "$SYSPROMPT" | cut -d' ' -f1)"
    echo "isolation_flags: --setting-sources '' --strict-mcp-config --disable-slash-commands --tools '' --system-prompt <file> --json-schema <file>"
    echo "model: opus"
  } > "$prov"

  set +e
  (
    cd "$workdir" && claude -p \
      --model opus \
      --effort high \
      --setting-sources "" \
      --strict-mcp-config \
      --disable-slash-commands \
      --tools "" \
      --system-prompt "$(cat "$SYSPROMPT")" \
      --output-format json \
      --json-schema "$(cat "$SCHEMA")" \
      < "$pkg"
  ) > "$raw" 2> "$err"
  local rc=$?
  set -e

  rm -rf "$workdir"
  echo "exit_code: $rc" >> "$prov"

  if [ $rc -ne 0 ]; then
    echo "extraction: skipped (process failed)" >> "$prov"
    return $rc
  fi

  # The CLI wraps the schema-validated object in a result envelope; the extraction happens here,
  # next to the invocation that produced it. Anything unexpected in the envelope fails the run
  # loudly instead of leaving an empty result that reads like a clean bill of health.
  set +e
  "$PY" - "$raw" "$out" <<'PY'
import json, sys
try:
    envelope = json.load(open(sys.argv[1], encoding="utf-8"))
except Exception as exc:
    sys.stderr.write(f"unparseable CLI envelope: {exc}\n"); sys.exit(3)
if envelope.get("is_error"):
    sys.stderr.write(f"CLI reported an error result: {envelope.get('subtype')} "
                     f"{envelope.get('api_error_status')}\n"); sys.exit(4)
critique = envelope.get("structured_output")
if not isinstance(critique, dict) or "findings" not in critique:
    sys.stderr.write("no structured_output with a findings list in the CLI envelope\n"); sys.exit(5)
with open(sys.argv[2], "w", encoding="utf-8") as fh:
    json.dump(critique, fh, indent=2, ensure_ascii=False)
    fh.write("\n")
counts = {"critical": 0, "major": 0, "minor": 0}
for finding in critique["findings"]:
    if finding.get("severity") in counts:
        counts[finding["severity"]] += 1
suspect = sum(1 for a in critique.get("assertion_assessments") or []
              if a.get("verdict") == "suspect")
print(f"{len(critique['findings'])} finding(s): "
      f"critical={counts['critical']} major={counts['major']} minor={counts['minor']}; "
      f"{suspect} assertion(s) judged suspect")
PY
  local erc=$?
  set -e
  echo "extraction_exit_code: $erc" >> "$prov"
  return $erc
}

FAILED=""
for lens in $LENSES; do
  echo "running isolated critic, lens '$lens'..."
  if run_one_lens "$lens"; then
    echo "  lens '$lens': ok -> $OUTDIR/critique-$lens.json"
  else
    echo "  lens '$lens': FAILED (see $OUTDIR/critique-$lens.stderr.txt)" >&2
    FAILED="$FAILED $lens"
  fi
done

MERGE_ARGS=()
for lens in $LENSES; do
  MERGE_ARGS+=("$lens=$OUTDIR/critique-$lens.json")
done

set +e
"$PY" "$MERGER" "$OUTDIR/critique-merged.json" "${MERGE_ARGS[@]}"
MRC=$?
set -e

echo "merged critique: $OUTDIR/critique-merged.json"
echo "provenance record: $OUTDIR/critique-<lens>.provenance.txt"

if [ -n "$FAILED" ] || [ $MRC -ne 0 ]; then
  echo "test-critic run INCOMPLETE — failed lens(es):${FAILED:- none}; merge exit $MRC" >&2
  echo "Report this as a failed gate run. Do not substitute a critique written in project context." >&2
  exit 1
fi

echo "critique complete"
