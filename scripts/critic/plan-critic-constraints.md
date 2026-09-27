# Fixed project constraints

These are stable facts about how the project the plan is written for is worked on. They are
included in every review package unchanged. They are context for judging the plan, not
requirements the plan is measured against — the specification above is what the plan is measured
against. The single exception is the bullet marked STRUCTURAL REQUIREMENT below: it states the
sections a plan in this project must contain whatever its ticket asks for, no ticket restates it,
and it is therefore the only text here that a finding may quote as its violated criterion.

- The project's technology stack is not fixed by this document: it is auto-detected from the
  repository at the start of the work, and the plan is expected to name the concrete test command
  it will use. Do not assume any particular language, framework or tooling beyond what the plan and
  the specification state.
- Every behavioural requirement in the plan declares an evidence kind: `driving-test`,
  `existing-suite`, `ci-evidence`, or `none`, one per requirement (a package may mix kinds). A
  requirement declared `driving-test` is implemented test-first — a driving test written and
  failing for the right reason before the production code exists, passing afterwards — and the
  plan is expected to say what will prove it, at a level a test designer can work from, not to
  enumerate concrete test cases itself. A requirement declared `existing-suite`, `ci-evidence`, or
  `none` carries no such obligation; a workflow-file or documentation change is the ordinary case
  for the latter two, not an exception that needs defending.
- STRUCTURAL REQUIREMENT. A finding that one of these sentences is violated quotes the sentence it
  is about as its violated criterion, reflowed onto one line with single spaces:
  - An implementation plan must state the goal, the approach, the affected files (new and
    modified), and the test-verification strategy.
  - An implementation plan must carry a mechanism balance: an "Added" list naming every new module
    constant, flag or parameter, registry, cache or lock, tag or reason code, and special-case
    branch the plan introduces, each with one line saying why it cannot be avoided by removing or
    reshaping something that already exists, and a "Removed" list naming what the plan deletes.
  A plan is allowed to add mechanism; it is not allowed to add it silently, and the balance is what
  makes that addition visible.
- The specification is the ticket: its title, body and comments. For an epic it is the epic plus
  all of its child tickets. Comments may refine or overrule the body; where they conflict, the later
  comment is the current reading unless the scope statement says otherwise.
- The plan is judged against the scope stated below, not against everything the ticket touches on.
  A plan is not incomplete merely because it does not restate the ticket, and a requirement the
  scope statement explicitly excludes is not a gap.
