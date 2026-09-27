#!/usr/bin/env bash
#
# Assembles the review package handed to one isolated critic.
#
# Assembly is a script rather than a writing task on purpose. The curator of a review package is
# the one role that could quietly defang the critique — by paraphrasing a requirement, or by
# leaving out the section a plan is weakest against. So the sources are fixed here and copied
# verbatim, and the curating agent contributes only the scope pointer and the plan itself. What
# the critic sees is therefore reproducible from the inputs, and the package file stays on disk
# for anyone who wants to check that.
#
# The full specification is included, never a selection of "relevant" sections: selection is the
# omission risk, and the critic has no repository access to make up for anything left out. It also
# lets the critic notice a plan claiming work that the specification assigns to a different system.
#
# The specification here is the TICKET PACKAGE: the verbatim title, body and comments of the ticket
# and, for an epic, of every child ticket. The dispatching skill assembles that file from the
# tracker without editing it; this script copies it whole. There is no repository-root argument,
# by contrast with the sothis original this was ported from, because nothing in the package is
# read out of the repository.
#
# THE LENS
#
# The gate runs several critics, never one. They are identical in every respect that matters —
# same isolation, same specification, same constraints, same scope, same plan, same system prompt
# — and differ only in a single fixed focus block appended as PART 5. That is what makes the
# results comparable and the packages diffable: run this script once per lens id with the same
# other inputs, and `diff` shows PART 5 and nothing else.
#
# The lens texts live in this script, hardcoded, and the caller may only name one of the ids below.
# An agent choosing or authoring its own focus would put the curator back into a pipeline built to
# have none: the easiest way to defuse a critique is to aim it somewhere harmless.
#
# Usage: plan-critic-package.sh <spec-file> <scope-file> <plan-file> <lens-id> <output-package>
#        plan-critic-package.sh --list-lenses
#   spec-file    the ticket package (title, body, comments; for an epic: the epic plus all child
#                tickets), verbatim as the tracker holds it
#   scope-file   short statement of the behavioural scope this plan was assigned, written by the
#                dispatching plan-critic agent, plus the round number if this is a re-review
#   plan-file    the planner's plan, verbatim and unedited
#   lens-id      one of: missed | misread | untestable | simplifier
#
set -euo pipefail

# The single source of truth for which lenses exist; plan-critic-run.sh reads it from here rather
# than keeping its own copy, so the two cannot drift apart.
LENS_IDS="missed misread untestable simplifier"

if [ "${1:-}" = "--list-lenses" ]; then
  for lens in $LENS_IDS; do echo "$lens"; done
  exit 0
fi

if [ $# -ne 5 ]; then
  echo "usage: $0 <spec-file> <scope-file> <plan-file> <lens-id> <output-package>" >&2
  echo "       $0 --list-lenses" >&2
  exit 2
fi

SPEC="$1"
SCOPE="$2"
PLAN="$3"
LENS="$4"
OUT="$5"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

for f in "$SPEC" "$SCRIPT_DIR/plan-critic-constraints.md" "$SCOPE" "$PLAN"; do
  [ -f "$f" ] || { echo "missing required file: $f" >&2; exit 2; }
done

# An unknown lens id is a hard failure, never a silently lens-less package: a critic without a
# lens would answer a different question than the two it is being compared against.
case " $LENS_IDS " in
  *" $LENS "*) ;;
  *) echo "unknown lens id: '$LENS' (expected one of: $LENS_IDS)" >&2; exit 2 ;;
esac

emit_lens() {
  case "$1" in
    missed)
      cat <<'LENS_MISSED'
This run's lens: REQUIREMENTS THE PLAN NEVER ADDRESSES.

Read the specification against the scope assigned to this plan and look for requirements that
fall inside that scope and that the plan does not address at all. Not addressed badly, not
addressed partially — absent. Something the specification demands, that nothing in the plan
would produce, and that no plan step is even aimed at.

Judge coverage, not quality. A requirement the plan does address but gets wrong is not this
run's finding, however wrong it is — report only what is missing outright. Requirements outside
the assigned scope are not findings either; if you mention one, say explicitly that it is out of
scope.

The plan's Symptom anchor line (the first line of its Test/verification strategy section) is also
this lens's concern (ticket #108): verify the quoted sentence, or a `none:<category>` claim,
against the specification itself — textual presence somewhere in the ticket is necessary but never
sufficient on its own. The anchor is a `critical` `gap` when the sentence it quotes is not the
ticket's actual stated runtime symptom (a sentence that appears in the ticket somewhere, but is not
what the ticket reports as actually failing, does not count). It is likewise a `critical` `gap`
when the plan claims `Symptom: none:<category>` while the specification does state a runtime
symptom — a `none` claim is not a way to skip this branch.
LENS_MISSED
      ;;
    misread)
      cat <<'LENS_MISREAD'
This run's lens: REQUIREMENTS THE PLAN ADDRESSES BUT MISREADS.

For each requirement the plan does address, hold what the requirement actually demands against
what the plan would actually produce, and look for the gap between them. Read too narrowly or too
broadly; a condition, qualifier or edge case dropped; a value, threshold or unit the
specification fixes and the plan quietly changes; the requirement satisfied under one
circumstance the specification states but not another; work the specification assigns to a
different system; an approach that would satisfy the words of the requirement while missing what
it is plainly for.

Judge fidelity, not coverage. A requirement the plan never mentions at all is not this run's
finding — quote the plan's own words next to the requirement's and show where they diverge.
LENS_MISREAD
      ;;
    untestable)
      cat <<'LENS_UNTESTABLE'
This run's lens: BEHAVIOUR STATED TOO VAGUELY TO DRIVE A FAILING TEST.

This lens's ordinary derivability judgment — whether a failing test can be derived from the plan's
own words — applies ONLY to requirements the plan itself declares evidence kind `driving-test`. A
requirement declared `existing-suite`, `ci-evidence`, or `none` is out of scope for that judgment;
do not fault a workflow-file or documentation requirement for lacking a derivable test, that is
what those other kinds exist for, and a finding against one of them on that basis would be a
finding against a rule the plan is right to follow. The one exception to this scoping is the
ticket #108 symptom carve-out below, which applies specifically when the requirement covering the
specification's stated runtime symptom is the one declaring `none`, `existing-suite`, `ci-evidence`,
or manual instead of `driving-test` — that is the failure mode the carve-out exists to catch, not an
exception you have to reconcile against the paragraph above.

For each `driving-test` requirement, it has to be described sharply enough that a test designer
could derive from it a driving test that fails for the right reason before the production code
exists. Judge whether such a test could be derived at all: are the trigger, the observable result,
and the conditions under which it must hold stated precisely enough to tell a pass from a failure?
Wording that cannot come out false — "handles it correctly", "works as expected", "properly
synchronised" — is what you are looking for, along with missing thresholds, unstated timing
bounds, and results named without saying what would be observed.

Hard limit on this lens, and it is not negotiable: do NOT enumerate concrete test cases and do
NOT propose any. A plan is required to state what will prove each behaviour, at a level a test
designer can work from — it is explicitly NOT required to list test cases, and a finding that
demands them would be a finding against a rule the plan is right to follow. You judge only
WHETHER a failing test is derivable from the plan's own words, and where it is not, name what is
missing from the description. Never which test to write.

A related carve-out (ticket #108): when the specification states a runtime symptom, find
the requirement that is supposed to cover it. If that requirement declares `none`, `existing-suite`,
`ci-evidence`, or manual verification as its evidence — rather than `driving-test` — report a
`critical` finding, kind `gap`, `violated_criterion` the ticket's quoted symptom sentence — UNLESS
that requirement's evidence line names something that is itself a plausible way to exercise the
symptom quoted in the plan's own `Symptom (verbatim from ticket):` anchor line: either a
`Substitute execution: <command> — expected: <what the output should show> — output pasted in the
PR body` line naming a `<command>`, or, for `ci-evidence`, the specific CI run/job it names as
demonstrating the requirement (`agents/planner.md` requires `ci-evidence` to name one). In either
case, if the named command or CI run/job plausibly exercises the anchor symptom, the coverage is
solid, not a finding — its evidence is the pasted command output or the CI run, not a test. Read
the `<command>` or the named CI run/job against the quoted anchor sentence exactly as you would read
a requirement against the specification elsewhere in this lens: a command or CI run/job with no
discernible relation to the anchor's stated symptom, or one whose own `expected:` clause (for
`Substitute execution`) it would satisfy regardless of whether the symptom is present or fixed (e.g.
`Substitute execution: echo success — expected: prints success`), does NOT qualify for the carve-out
— report the same `critical`/`gap` finding you would report had the line been absent entirely. This
applies equally to `ci-evidence`: naming an unrelated lint/build job, or an existing test suite that
does not cover the symptom, does not qualify. `ci-evidence` is exempt only from the ordinary
driving-test derivability judgment above — merely declaring the kind and naming *some* CI run is not
by itself an exemption from this symptom carve-out; the named run still has to plausibly cover the
symptom.
LENS_UNTESTABLE
      ;;
    simplifier)
      cat <<'LENS_SIMPLIFIER'
This run's lens: MECHANISM THE PLAN ADDS WITHOUT EARNING IT.

Every plan in this project must carry a mechanism balance — see the STRUCTURAL REQUIREMENT bullet
in the fixed project constraints above, which is the criterion your findings quote. If the plan
has no such section at all, that is one finding, severity major, kind gap, and it is not
negotiable: a plan whose growth is unstated cannot be weighed against anything, and nothing
elsewhere in the plan substitutes for it.

Where the section exists, hold it against the rest of the plan. Growth itself is allowed — a
feature may legitimately cost mechanism — but it has to be argued rather than declared. Read every
justification line and ask whether it says why the addition cannot be had by removing or reshaping
something that already exists. A line that only restates what the new mechanism does — "the lock
prevents concurrent scans", "the flag lets callers opt out", "the cache avoids repeated lookups" —
is not a justification, and saying so is this run's finding, kind gap. Read the plan's steps back
against the Added list too: a constant, flag, tag, reason code or special-case branch the approach
introduces but the balance does not name is an unlisted addition, kind gap. An Added list that
outweighs Removed without one word about what was considered for removal is an unargued one, kind
risk.

Then look for duplication. Where the specification, the scope, or the plan itself says the
affected code already holds two or more mechanisms answering the same question — two timeout
regimes, two caches, two engines, two independent enumerations — a plan that adds a third is a
major finding, kind double-claim; name the existing pair it joins and quote the words that
establish it. And look at what the plan acts on: the symptom the specification actually reports,
or an internal quantity the plan has decided stands in for it? Where it is the latter, quote the
specification's symptom next to the plan's target and show the substitution, kind contradiction.

You have no access to the repository, and this is the lens most tempting to guess with. You may
assert that a duplicate mechanism already exists ONLY from words that are in the specification,
the scope statement or the plan, and you quote those words. A suspicion that the code probably
already has such a mechanism, with nothing in the package saying so, is not a finding here: record
it among the claims you cannot verify without the codebase and leave it there.

Use only these finding kinds, and map them exactly as instructed above, no others: gap for a
missing balance, a missing entry, or a missing justification; double-claim for a third mechanism
answering a question two already answer; contradiction where the plan's target contradicts a
symptom the specification names; risk for a justification that is present but does not hold.

Hard limit on this lens, and it is not negotiable: do NOT design the simpler alternative. Naming
what an addition duplicates, and that its justification does not hold, is the whole of the job —
which constant to delete, which two mechanisms to collapse, and what the smaller design would look
like are the planner's decisions, never yours. Do not count mechanisms the package never mentions,
and do not treat "this plan adds things" as a finding by itself: an addition whose justification
actually holds is a correct result and belongs in what you record as solid.
LENS_SIMPLIFIER
      ;;
  esac
}

{
  cat <<'HEADER'
You are reviewing an implementation plan against the specification it is supposed to satisfy.

This message contains everything you get: the specification in full, fixed project constraints,
the scope this plan was assigned, the plan itself, and the review lens for this run. There is no
codebase to consult and no further information available. Judge the plan on what is here.

Work through the plan step by step and dependency by dependency, judging each against a specific
requirement you quote from the specification. Record what is sound as explicitly as what is not.
Any claim the plan makes about code, assets, or behaviour that already exists is something you
cannot verify from here — record it as an unverified assumption rather than accepting it or
treating it as an error. Do not invent problems; an empty findings list is a correct result for a
good plan. Do not rewrite the plan.

================================================================================
PART 1 — SPECIFICATION: THE TICKET (verbatim, complete, authoritative)
================================================================================
HEADER
  cat "$SPEC"
  cat <<'MID1'

================================================================================
PART 2 — FIXED PROJECT CONSTRAINTS
================================================================================
MID1
  cat "$SCRIPT_DIR/plan-critic-constraints.md"
  cat <<'MID2'

================================================================================
PART 3 — SCOPE ASSIGNED TO THIS PLAN
================================================================================
MID2
  cat "$SCOPE"
  cat <<'MID3'

================================================================================
PART 4 — THE PLAN UNDER REVIEW (verbatim, unedited)
================================================================================
MID3
  cat "$PLAN"
  cat <<'MID4'

================================================================================
PART 5 — REVIEW LENS FOR THIS RUN
================================================================================

Three other reviewers are reading this same package right now under different lenses. Everything
above is identical for all four of us; only this section differs. So report findings for your
lens only and leave theirs to them — a problem you can see but that belongs to another lens will
be caught there, and duplicating it here only makes the merged report harder to act on. What you
record as solid, and the claims you cannot verify, are not restricted by the lens: write those for
the whole plan as usual.

MID4
  emit_lens "$LENS"
} > "$OUT"

echo "package written to $OUT ($(wc -c < "$OUT") bytes, lens: $LENS)"
