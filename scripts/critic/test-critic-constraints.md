# Fixed project constraints

These are stable facts about how the project the test code was written for is worked on. They are
included in every review package unchanged. They are context for judging the test code, not
requirements the test code is measured against — the requirement above is what it is measured
against.

- The project's technology stack is not fixed by this document: it is auto-detected from the
  repository at the start of the work. The tests use the project's existing test framework and
  the concrete test command the plan names. Do not assume any particular language, framework or
  assertion library beyond what the test code itself shows.
- **The production code these tests drive does not exist yet.** These tests were written first, on
  purpose, and are supposed to be failing right now. That a test currently fails is the expected
  state and never a defect. What is being judged is whether it would still fail once a *wrong*
  implementation existed.
- An assertion that only checks a literal string is present in a config, workflow, or docs
  file — including a markdown heading's exact text, a bare string-presence check, or a JSON
  key's mere presence — is a `critical`, `tautology`-lens finding in its own right, layer
  `plan`: it proves nothing about the requirement it is supposed to demonstrate, since a wrong
  config, a right heading over the wrong behaviour, or a present-but-unused JSON key can satisfy
  it as easily as a correct implementation. Such a requirement should have been declared
  `ci-evidence` or `none`, not `driving-test` (the plan misdeclared the requirement, not that
  the test author wrote it badly).
- One driving test per requirement the plan declares evidence kind `driving-test` for is the
  minimum; additional coverage of behaviour that already exists may be present and may already
  pass. A passing test for existing behaviour is not a defect, and a driving test is not weaker for
  being the only red one. A requirement the plan declares `existing-suite`, `ci-evidence`, or
  `none` is out of scope for this critique — its evidence, if any, is not a test batch this lens
  judges.
- Tests exercise the same public seam real callers use (the module's public API, the CLI, the
  HTTP surface, the exported function — whatever the project's callers actually go through).
  Visibility is never widened to make something testable, so an assertion limited to what the real
  seam exposes is a constraint the test is right to respect, not a weakness.
- The requirement anchor is the plan. There is no separate test-case design document: the plan is
  expected to state what will prove each behaviour, and the tests are judged directly against
  that statement.
