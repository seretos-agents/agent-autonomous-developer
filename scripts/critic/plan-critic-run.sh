#!/usr/bin/env bash
#
# Runs the plan critique as several isolated critics and merges their findings.
#
# ISOLATION
#
# The entire value of the plan-critic role rests on each critique running WITHOUT the project's
# context: no CLAUDE.md, no ticket beyond what the package contains, no skills, no agent
# definitions, no MCP servers, no tools, no plugins. A critic sees exactly the review package it
# is handed and nothing else. That is what makes its answer independently derived rather than a
# reworded echo of the same context the plan was written in.
#
# Every isolation flag below is load-bearing, and the flag set is deliberately identical across
# both runners (this one and test-critic-run.sh). Measured on this machine (2026-07-30, claude 2.1.220; re-measured 2026-08-14, claude
# 2.1.229): a plain `claude -p` started in an empty directory inherits user-level plugins, their
# skills, their MCP servers and their hooks — and it also reads a CLAUDE.md sitting in its own
# working directory. Until 2026-08-14 this comment claimed the opposite about that last point. The
# claim was wrong; it understated the exposure rather than weakening the gate, since the flags do
# exclude all of it. Dropping `--setting-sources ""` silently re-contaminates the critic.
# Do not "simplify" this invocation. The guarantee applies per process, so every run gets the
# identical flag set and each writes its own provenance record — independently isolated runs, not
# one run whose isolation is assumed to cover the others.
#
# The static half of check-critic-isolation.sh runs below, on every invocation of this script, so
# this comment is not the only thing standing between a CLI change and a silently contaminated run.
# The expensive --live measurement stays a deliberate act.
#
# THE LENSES
#
# One critic reading for everything at once reads for nothing in particular: it finds whichever
# weakness is most conspicuous and stops. The runs are identical — same package, same system
# prompt, same isolation — except for a fixed focus block, so each stumbles over a different class
# of defect: requirements the plan never addresses, requirements it addresses but misreads,
# behaviour stated too vaguely for a failing test to be derived from it, and mechanism the plan
# adds without a justification that holds. The lens texts are fixed in plan-critic-package.sh;
# nothing here or above it chooses them.
#
# The runs are independent by construction. PARTs 1-4 of every lens's package (spec, constraints,
# scope, plan) are byte-identical — only PART 5 (the lens block, last) differs — so the four
# packages share a cacheable prefix. They still default to a parallel start (below), which defeats
# that cache: nothing about independence *requires* simultaneity, and `ADEV_PLAN_CRITIC_CACHE_WARM`
# is the switch to trade wall-clock for a cache-warm start once that trade is measured (see
# run_one_lens and the loop below) — never see each other's output either way, which is what keeps
# the results independent data points rather than one opinion agreed to by everyone.
#
# EFFORT PER LENS (ticket #105)
#
# Only the missed/misread lenses can produce a blocking finding (see plan-critic-merge.py and
# skills/process-developer/SKILL.md's Phase 2); untestable/simplifier are always notes. Effort is
# split accordingly: missed/misread run at --effort high, untestable/simplifier at --effort medium.
# --effort is not one of the isolation flags check-critic-isolation.sh enforces (see that script's
# FLAGS list), so varying it per lens does not touch isolation.
#
# MERGE
#
# The findings are merged by plan-critic-merge.py — plain deterministic code, no model. See that
# file for why that matters. This script's exit code is 0 only if every critic returned a usable
# critique; a partial run is written out but reported as a failure, because a gate that looks
# complete on a partial result is worse than one that says it is not.
#
# Usage: plan-critic-run.sh <spec-file> <scope-file> <plan-file> <output-dir>
#   spec-file    the ticket package (title, body, comments; for an epic: plus all children), verbatim
#
# Writes into <output-dir>, per lens <L>:
#   package-<L>.txt              the assembled review package (diff these: only PART 5 differs)
#   critique-<L>.raw.json        the CLI result envelope, untouched
#   critique-<L>.json            the validated critique object, as the schema defines it
#   critique-<L>.provenance.txt  how that process was actually started
#   critique-<L>.stderr.txt      its stderr
# and once, across all lenses:
#   critique-merged.json         the merged findings the dispatching skill reads
#
set -euo pipefail

if [ $# -ne 4 ]; then
  echo "usage: $0 <spec-file> <scope-file> <plan-file> <output-dir>" >&2
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

SPEC="$(cd "$(dirname "$1")" && pwd)/$(basename "$1")"
SCOPE="$(cd "$(dirname "$2")" && pwd)/$(basename "$2")"
PLAN="$(cd "$(dirname "$3")" && pwd)/$(basename "$3")"
mkdir -p "$4"
# Canonical, drive-qualified form — every path this script reports is derived from it, and
# plan-critic reads those reports with native file tools that resolve a bare "/tmp/…" somewhere
# else entirely. See win-path.sh.
. "$SCRIPT_DIR/win-path.sh"
OUTDIR="$(canonical_dir "$4")"

SCHEMA="$SCRIPT_DIR/plan-critic-schema.json"
SYSPROMPT="$SCRIPT_DIR/plan-critic-system-prompt.txt"
PACKAGER="$SCRIPT_DIR/plan-critic-package.sh"
MERGER="$SCRIPT_DIR/plan-critic-merge.py"

for f in "$SPEC" "$SCOPE" "$PLAN" "$SCHEMA" "$SYSPROMPT" "$PACKAGER" "$MERGER"; do
  [ -f "$f" ] || { echo "missing required file: $f" >&2; exit 2; }
done

# The lens list comes from the packager, so there is exactly one place that defines which lenses
# exist and this script cannot drift out of step with it.
LENSES="$(bash "$PACKAGER" --list-lenses)"

# Effort per lens (ticket #105) — see the EFFORT PER LENS comment above.
lens_effort() {
  case "$1" in
    missed|misread) echo high ;;
    *) echo medium ;;
  esac
}

# Cache-warm-first switch (ticket #105), default off. When "1", the caller runs the first lens to
# completion before starting the rest in parallel, so the three later processes hit a warm prompt
# cache on PARTs 1-4 (byte-identical across lenses). Flip only after measuring wall clock and
# usage.cache_read_input_tokens per lens (from critique-<lens>.raw.json) warm vs. cold on the same
# package — this is a one-line switch specifically so that measurement can happen without touching
# the invocation itself.
ADEV_PLAN_CRITIC_CACHE_WARM="${ADEV_PLAN_CRITIC_CACHE_WARM:-0}"

run_one_lens() {
  local lens="$1"
  local start_mode="$2"
  local effort
  effort="$(lens_effort "$lens")"
  local pkg="$OUTDIR/package-$lens.txt"
  local raw="$OUTDIR/critique-$lens.raw.json"
  local out="$OUTDIR/critique-$lens.json"
  local prov="$OUTDIR/critique-$lens.provenance.txt"
  local err="$OUTDIR/critique-$lens.stderr.txt"

  bash "$PACKAGER" "$SPEC" "$SCOPE" "$PLAN" "$lens" "$pkg" >/dev/null

  # A fresh, empty directory outside the repository, one per lens. Not a worktree, not a
  # subdirectory of the project — CLAUDE.md discovery walks parent directories, so anything under
  # the repo root would defeat the isolation.
  local workdir
  workdir="$(mktemp -d)"

  {
    echo "plan-critic isolated run"
    echo "lens: $lens"
    echo "timestamp_utc: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "claude_version: $(claude --version 2>&1)"
    echo "cwd: $workdir"
    echo "cwd_contents: $(ls -A "$workdir" | tr '\n' ' ')<empty above means no inherited project files>"
    echo "package: $pkg"
    echo "package_sha256: $(sha256sum "$pkg" | cut -d' ' -f1)"
    echo "package_bytes: $(wc -c < "$pkg")"
    echo "spec_sha256: $(sha256sum "$SPEC" | cut -d' ' -f1)"
    echo "plan_sha256: $(sha256sum "$PLAN" | cut -d' ' -f1)"
    echo "system_prompt_sha256: $(sha256sum "$SYSPROMPT" | cut -d' ' -f1)"
    echo "isolation_flags: --setting-sources '' --strict-mcp-config --disable-slash-commands --tools '' --system-prompt <file> --json-schema <file>"
    echo "model: opus"
    echo "effort: $effort"
    echo "start_mode: $start_mode"
  } > "$prov"

  set +e
  (
    cd "$workdir" && claude -p \
      --model opus \
      --effort "$effort" \
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

  # The CLI wraps the schema-validated object in a result envelope; the merge consumes plain
  # critique objects, so the unwrapping happens here, next to the invocation that produced it.
  # Anything unexpected in the envelope fails the lens loudly instead of merging an empty result.
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
print(f"{len(critique['findings'])} finding(s)")
PY
  local erc=$?
  set -e
  echo "extraction_exit_code: $erc" >> "$prov"
  return $erc
}

FAILED=""
declare -A PIDS=()

if [ "$ADEV_PLAN_CRITIC_CACHE_WARM" = "1" ]; then
  # Warm-first: run the first lens to completion so its cache-write establishes the shared PARTs
  # 1-4 prefix, then start the rest in parallel to read it warm. Trades wall clock (one lens runs
  # serially before the rest) for input-token cost on three of the four processes — see the EFFORT
  # PER LENS / cache-warm comment above.
  FIRST=""
  for lens in $LENSES; do FIRST="$lens"; break; done
  REST=""
  for lens in $LENSES; do [ "$lens" = "$FIRST" ] || REST="$REST $lens"; done

  echo "running isolated critics: '$FIRST' first (warm-first), then$REST in parallel..."
  if run_one_lens "$FIRST" "warm-first" > "$OUTDIR/run-$FIRST.log" 2>&1; then
    echo "  lens '$FIRST': ok"
  else
    echo "  lens '$FIRST': FAILED (see $OUTDIR/critique-$FIRST.stderr.txt and $OUTDIR/run-$FIRST.log)" >&2
    FAILED="$FAILED $FIRST"
  fi
  for lens in $REST; do
    run_one_lens "$lens" "warm-first" > "$OUTDIR/run-$lens.log" 2>&1 &
    PIDS[$lens]=$!
    echo "  lens '$lens' -> pid ${PIDS[$lens]}"
  done
  for lens in $REST; do
    if wait "${PIDS[$lens]}"; then
      echo "  lens '$lens': ok"
    else
      echo "  lens '$lens': FAILED (see $OUTDIR/critique-$lens.stderr.txt and $OUTDIR/run-$lens.log)" >&2
      FAILED="$FAILED $lens"
    fi
  done
else
  echo "running $(echo "$LENSES" | wc -w) isolated critics in parallel..."
  for lens in $LENSES; do
    run_one_lens "$lens" "parallel" > "$OUTDIR/run-$lens.log" 2>&1 &
    PIDS[$lens]=$!
    echo "  lens '$lens' -> pid ${PIDS[$lens]}"
  done
  for lens in $LENSES; do
    if wait "${PIDS[$lens]}"; then
      echo "  lens '$lens': ok"
    else
      echo "  lens '$lens': FAILED (see $OUTDIR/critique-$lens.stderr.txt and $OUTDIR/run-$lens.log)" >&2
      FAILED="$FAILED $lens"
    fi
  done
fi

MERGE_ARGS=()
for lens in $LENSES; do
  MERGE_ARGS+=("$lens=$OUTDIR/critique-$lens.json")
done

set +e
"$PY" "$MERGER" "$OUTDIR/critique-merged.json" "${MERGE_ARGS[@]}"
MRC=$?
set -e

echo "merged critique: $OUTDIR/critique-merged.json"
echo "provenance records: $OUTDIR/critique-<lens>.provenance.txt"

if [ -n "$FAILED" ] || [ $MRC -ne 0 ]; then
  echo "plan-critic run INCOMPLETE — failed lens(es):${FAILED:- none}; merge exit $MRC" >&2
  echo "Report this as a failed gate run. Do not treat a partial result as a complete critique." >&2
  exit 1
fi

echo "all lenses completed"
