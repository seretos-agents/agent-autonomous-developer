#!/usr/bin/env python3
"""
Merges the critiques of the plan-critic lenses into one report.

This step is deliberately mechanical: it reads JSON, concatenates, and applies one exact-match
dedup rule. There is no `claude` invocation here and no model in the loop, by design. An LLM
placed between the isolated critics and the dispatching skill's decision would be a curator — able to
smooth away a finding on its own judgment — and that is precisely the failure mode the rest of
this gate is built to prevent (the ticket spec is copied into the package verbatim for the same
reason). If this merge looks like it could be "smarter", that is the intended shape, not a gap.

Rules, in full:

  * Nothing is dropped. Every finding from every lens is present in the output, either as a
    finding of its own or inside the `merged_from` record of the finding it collapsed into.
  * Two findings are merged ONLY when their quoted requirement string (`violated_criterion`) is
    byte-for-byte identical. No trimming, no case folding, no fuzzy matching — a string differing
    by a single trailing space is a different string and both findings survive. A redundant
    finding costs the dispatching skill a moment; a silently swallowed one costs the gate its purpose.
  * A merged group keeps the highest severity present in it (critical > major > minor), and the
    surviving finding is the member that carried that severity — earliest lens wins a tie.
  * A finding with a missing or empty `violated_criterion` is never merged with anything.
  * Findings are the only thing deduplicated. The "solid" list and the unverifiable-claims list are
    concatenated across lenses with their lens tagged on, because three views of the same step are
    three data points, not a duplicate.
  * Every finding is stamped with a `finding_class`, derived from its `(lens, severity)` pair —
    never from a field a critic sets itself, and never a judgment this script makes. `missed` and
    `misread` findings are "blocking" at every severity; `simplifier` findings are "note" at every
    severity; `untestable` findings are "blocking" only when `severity == "critical"`, else "note"
    (ticket #108 — an `untestable`+`critical` finding means the ticket's stated symptom is
    exercised by no test at all, which the round-cap loop must not be allowed to grind on forever
    the way a `major`/`minor` `untestable` note may legitimately be argued out later). Any lens not
    named above (including a future one, and `tautology` from the test-critic gate, which reuses
    this merge) defaults to "blocking" at every severity — the safe direction, since a lens is
    note-only, or severity-conditional, only by an explicit ticket decision (#105, #108), never by
    omission. A merged group's class is the STRONGEST class present in it, not the survivor's own:
    a `missed` finding and a `simplifier` finding can share a byte-identical `violated_criterion`
    (the mechanism-balance sentence is exactly this case), and the severity-based survivor could be
    the `simplifier` one — without taking the max, a real blocking finding would be silently
    declassed to a note because of which member happened to win the severity tie.

Deciding whether a finding is real remains the dispatching skill's job. This script does not filter, rank,
or interpret anything — the class derivation is a fixed table keyed on `lens`, not an assessment of
the finding's content.

Usage: plan-critic-merge.py <output-json> <lens>=<critique-json> [<lens>=<critique-json> ...]
"""
import json
import os
import sys

SEVERITY_RANK = {"critical": 3, "major": 2, "minor": 1}

# Lenses that are always "note", regardless of severity (ticket #105): they read the plan as a
# document, not the plan against real code, and on a large plan they reliably surface something
# every round. `untestable` used to be in this set unconditionally too; ticket #108 makes it
# severity-conditional instead (see UNTESTABLE_BLOCKING_SEVERITIES and finding_class() below) — an
# `untestable`+`critical` finding means the ticket's stated symptom is exercised by no test at all,
# which is exactly the case #105's "note, never a reason for another round" rule was never meant to
# cover. Any lens NOT named in finding_class() below (including a lens added later, and `tautology`
# from the test-critic gate) defaults to "blocking" at every severity — see the module docstring.
NOTE_LENSES = {"simplifier"}
# untestable is "blocking" only at these severities (ticket #108); every other severity is "note",
# matching the pre-#108 behaviour lenses in NOTE_LENSES already had unconditionally.
UNTESTABLE_BLOCKING_SEVERITIES = {"critical"}
CLASS_RANK = {"blocking": 1, "note": 0}


def severity_rank(finding):
    return SEVERITY_RANK.get(finding.get("severity"), 0)


def finding_class(lens, severity):
    if lens in NOTE_LENSES:
        return "note"
    if lens == "untestable":
        return "blocking" if severity in UNTESTABLE_BLOCKING_SEVERITIES else "note"
    # missed, misread, and any unnamed/future lens default to blocking at every severity.
    return "blocking"


def main(argv):
    if len(argv) < 3:
        sys.stderr.write(
            "usage: plan-critic-merge.py <output-json> <lens>=<critique-json> ...\n")
        return 2

    out_path = argv[1]
    sources = []
    for spec in argv[2:]:
        if "=" not in spec:
            sys.stderr.write(f"expected <lens>=<path>, got: {spec}\n")
            return 2
        lens, path = spec.split("=", 1)
        sources.append((lens, path))

    lens_runs = []
    findings_in = []
    solid = []
    unverifiable = []

    for lens, path in sources:
        if not os.path.isfile(path):
            # A lens that produced no file is recorded, never silently ignored: the merged report
            # has to show which perspectives actually ran, so a 2-of-3 run cannot be mistaken for
            # a complete one.
            lens_runs.append({"lens": lens, "status": "missing", "source": path,
                              "finding_count": 0, "error": "critique file not found"})
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (json.JSONDecodeError, OSError) as exc:
            lens_runs.append({"lens": lens, "status": "unreadable", "source": path,
                              "finding_count": 0, "error": str(exc)})
            continue

        lens_findings = data.get("findings") or []
        for index, finding in enumerate(lens_findings):
            record = dict(finding)
            record["lens"] = lens
            record["id"] = f"{lens}::{finding.get('id', index + 1)}"
            record["finding_class"] = finding_class(lens, finding.get("severity"))
            findings_in.append(record)

        solid.extend(f"[{lens}] {item}" for item in (data.get("solid") or []))
        unverifiable.extend(
            f"[{lens}] {item}"
            for item in (data.get("unverifiable_without_codebase_access") or []))

        lens_runs.append({"lens": lens, "status": "ok", "source": path,
                          "finding_count": len(lens_findings)})

    # Group by the exact quoted requirement string, preserving first-appearance order. Findings
    # without a usable criterion get a key that cannot collide with any other finding's.
    groups = []
    index_by_key = {}
    for position, finding in enumerate(findings_in):
        criterion = finding.get("violated_criterion")
        key = criterion if isinstance(criterion, str) and criterion != "" \
            else ("\x00unmergeable", position)
        if key in index_by_key:
            groups[index_by_key[key]].append(finding)
        else:
            index_by_key[key] = len(groups)
            groups.append([finding])

    merged_findings = []
    deduplicated_groups = 0
    for group in groups:
        # max() over a stable list returns the first maximum, so an earlier lens wins a tie.
        survivor_index = max(range(len(group)), key=lambda i: severity_rank(group[i]))
        survivor = dict(group[survivor_index])
        # The group's class is the strongest class present, not the severity-survivor's own — see
        # the module docstring for why (a missed/simplifier pair sharing one violated_criterion).
        survivor["finding_class"] = "blocking" if any(
            f.get("finding_class") == "blocking" for f in group) else "note"
        if len(group) > 1:
            deduplicated_groups += 1
            survivor["merged_from"] = [
                {"lens": other.get("lens"), "id": other.get("id"),
                 "severity": other.get("severity"), "kind": other.get("kind"),
                 "title": other.get("title"), "what": other.get("what")}
                for i, other in enumerate(group) if i != survivor_index
            ]
        merged_findings.append(survivor)

    severity_counts = {"critical": 0, "major": 0, "minor": 0}
    blocking_severity_counts = {"critical": 0, "major": 0, "minor": 0}
    for finding in merged_findings:
        severity = finding.get("severity")
        if severity in severity_counts:
            severity_counts[severity] += 1
            if finding.get("finding_class") == "blocking":
                blocking_severity_counts[severity] += 1

    merged = {
        "lens_runs": lens_runs,
        "severity_counts": severity_counts,
        "blocking_severity_counts": blocking_severity_counts,
        "merge_stats": {
            "findings_in": len(findings_in),
            "findings_out": len(merged_findings),
            "groups_deduplicated": deduplicated_groups,
            "lenses_ok": sum(1 for run in lens_runs if run["status"] == "ok"),
            "lenses_expected": len(sources),
        },
        "findings": merged_findings,
        "solid": solid,
        "unverifiable_without_codebase_access": unverifiable,
    }

    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(merged, fh, indent=2, ensure_ascii=False)
        fh.write("\n")

    print(f"merged critique written to {out_path}: "
          f"{len(findings_in)} findings in, {len(merged_findings)} out "
          f"({deduplicated_groups} group(s) deduplicated on an identical requirement string); "
          f"critical={severity_counts['critical']} major={severity_counts['major']} "
          f"minor={severity_counts['minor']}; "
          f"blocking critical={blocking_severity_counts['critical']} "
          f"major={blocking_severity_counts['major']}")

    # A merge over an incomplete set of lenses is still written — the caller needs to see what
    # did come back — but it is not reported as a clean run.
    return 0 if all(run["status"] == "ok" for run in lens_runs) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
