# Agent operating rules

This repository is intentionally designed so a fresh coding agent can be pointed at the repo plus an issue or PR number and work without needing private chat context.

**Read this file first. Then read `README.md`, the relevant issue or PR in full, and the documents it references.**

## Source of truth

The merged canonical model contract from completed issue #2 is the foundation of every lane.

Canonical contract locations:

- `src/oabm/model/`
- `contracts/`
- the model contract documentation referenced by issue #2

Do not silently create a competing representation.

If the contract is genuinely insufficient:

1. stop the lane-specific workaround,
2. open a narrowly scoped contract-change issue,
3. explain the missing capability and affected lanes,
4. keep the current lane isolated until that contract question is resolved.

Do not casually modify the canonical contract from an importer, router, IFC, quantity, drawing, or QA lane.

## Coordinate, identity, and serialization discipline

Unless the canonical contract says otherwise:

- preserve stable canonical IDs,
- use the canonical coordinate and unit conventions,
- preserve provenance and confidence,
- preserve explicit ambiguity rather than inventing certainty,
- validate cross-references,
- keep serialization deterministic,
- keep generated views and exports derived from the canonical model.

The model is the source of truth. PDFs, IFC, drawings, schedules, takeoffs, and visualizations are inputs or derived outputs, not alternative sources of truth.

## Public repository safety

This repository is public.

Never commit:

- customer plans,
- customer names or addresses,
- private project documents,
- production exports,
- credentials, tokens, cookies, API keys, or secrets,
- personal information,
- proprietary OfficeAdmin production data.

Fixtures must be synthetic or explicitly public-safe.

Do not touch OfficeAdmin production services, databases, billing, payroll, authentication, or customer data from this repository.

## Worktree and branch rules

- One issue or fix assignment per branch and worktree.
- Never do implementation work in a shared `main` checkout.
- Start from current `origin/main` unless the assignment explicitly says to work on an existing PR branch.
- Keep commits within the owning workstream unless the issue is a named convergence gate.
- Remove abandoned local worktrees when finished.
- Do not force-push over another agent's active branch unless the assignment explicitly gives you ownership of that branch.

## Determine your role

A fresh agent should identify which of these roles it was assigned.

### Role A: implement an issue

If the user says something like `Take issue #8` or points you to an open implementation issue:

1. Read `README.md`.
2. Read this `AGENTS.md`.
3. Read the full issue, including every comment.
4. Read every dependency listed by the issue.
5. Read the merged #2 canonical model contract.
6. Read relevant files under `docs/`.
7. Confirm the issue is not still blocked by an unmet dependency.
8. Work in a dedicated branch/worktree.
9. Implement the issue completely within its lane.
10. Add deterministic public-safe fixtures and tests.
11. Run CI-equivalent tests locally.
12. Push the branch.
13. Open a PR that closes the issue.
14. Do not merge your own implementation PR unless the user explicitly assigned you both implementation and acceptance authority.

Do not stop after planning, scaffolding, branch creation, or partial implementation if the issue is implementable.

### Role B: fix a reviewed PR

If the user says something like `Fix PR #16` or assigns a PR that already has blocking review findings:

1. Read `README.md`, this file, and relevant docs.
2. Read the underlying issue in full.
3. Read the merged #2 canonical model contract.
4. Read the entire PR diff and every PR comment/review.
5. Treat every unresolved substantive review finding as required work.
6. Work on the existing PR branch unless the PR or user says otherwise.
7. Fix the root cause, not just the reviewer example.
8. Add regression tests for each bug fixed.
9. Run the full CI-equivalent suite.
10. Push to the existing PR.
11. Leave a concise PR comment summarizing fixes and exact tests.
12. Do not merge your own fix pass. A fresh independent reviewer should re-review it.

Do not dismiss a review finding merely because CI is green.

### Role C: independently review a PR

If the user says something like `Review PR #20`, you are an acceptance reviewer, not the author.

1. Read `README.md`, this file, relevant docs, the underlying issue, and the merged #2 canonical model contract.
2. Read the complete PR diff, commits, comments, and current CI results.
3. Verify the implementation against the issue acceptance criteria and lane boundaries.
4. Inspect whether it silently changes or bypasses the canonical contract.
5. Run additional tests, targeted reproductions, or local inspection when CI does not prove a requirement.
6. Check for private/customer data or secrets.
7. Check deterministic behavior, stable identity, provenance/confidence, units/coordinates, and cross-reference validity where applicable.
8. Do not approve based only on green CI.

If the PR fully satisfies the issue and repository rules:

- merge it using the repository's normal merge strategy,
- make sure the issue is closed or updated,
- update dependency/gate labels when appropriate.

If substantive problems exist:

- leave precise blocking comments with reproducible examples,
- do not merge,
- leave the issue open.

Never lower the acceptance bar because another lane is waiting.

## Competing PRs for one issue

Sometimes multiple agents may implement the same issue.

If assigned to review competing PRs:

1. Read the issue and canonical contract first.
2. Review every competing PR against the same acceptance criteria.
3. Do not choose by PR age, size, agent identity, or test count alone.
4. Prefer the implementation that fully satisfies the issue, best preserves contract boundaries, is deterministic, and has the strongest meaningful tests.
5. Merge only a qualifying winner.
6. Close redundant PRs with a short explanation linking to the selected implementation.
7. If no implementation fully qualifies, merge none and leave required-fix comments on the strongest candidate(s).

Do not combine competing implementations unless the issue genuinely requires useful pieces from more than one and the resulting integration remains clean.

## Lane ownership

Keep these responsibilities separated:

- `src/oabm/model`: canonical semantic model and validation
- `src/oabm/ifc`: IFC / IfcOpenShell / Bonsai interoperability and round trip
- `src/oabm/routing`: deterministic 3D electrical routing
- `src/oabm/importers/roomplan`: RoomPlan / LiDAR ingestion
- `src/oabm/importers/pdf_architecture`: architectural PDF ingestion
- `src/oabm/importers/pdf_electrical`: electrical PDF ingestion
- `src/oabm/quantities`: model and route derived quantities
- `src/oabm/drawings`: plans, elevations, sections, schedules, and derived drawing outputs
- `fixtures` and QA tests: known-answer public-safe regression data

Examples:

- Importers produce canonical objects. They do not invent a separate model.
- Routers consume canonical geometry and emit canonical routes. They do not materialize IFC.
- IFC code materializes and reads canonical objects. It does not decide routing.
- Quantities consume canonical objects/routes. They do not remeasure drawings.
- Drawings derive views from canonical objects/routes. They do not become a second geometry source.

## Dependency relay

This project is sequenced by prerequisites, not time estimates.

Read `docs/responsibility-relay.md` and the current GitHub issue state before starting convergence work.

Current gate definitions are represented by issues and may evolve. In general:

- Gate A: canonical model contract
- Gate B: synthetic engine vertical slice
- Gate C: real-input end-to-end proof
- Gate D: architectural + electrical PDF convergence
- final gate: private real-project acceptance

A convergence issue may start only when its listed dependencies are actually complete and merged.

If a dependency has an open PR, that is not the same as complete.

## Testing standard

Every implementation lane should include deterministic tests and public-safe fixtures appropriate to its scope.

Prefer tiny known-answer cases over giant opaque samples.

At minimum, consider:

- normal valid input,
- edge geometry,
- invalid or broken references,
- deterministic repeatability,
- stable identity,
- serialization/schema validity,
- units and coordinate correctness,
- ambiguity/provenance/confidence,
- cross-reference/connectivity validity.

Run the repository's CI-equivalent commands locally before opening or updating a PR.

Green CI is necessary, not sufficient for acceptance.

## CI

This public R&D repository intentionally uses lightweight standard GitHub-hosted Ubuntu CI. Do not replace it with OfficeAdmin's CI and do not introduce a dependency on `officeadmin-books`.

Keep CI focused on this engine's schema, unit tests, deterministic fixtures, and integration proofs.

## PR expectations

Implementation PRs should state:

- issue closed,
- scope implemented,
- important contract choices,
- tests run and exact result,
- public-safe fixture additions,
- anything intentionally deferred by the issue.

Fix-pass updates should state:

- each blocking review finding addressed,
- regression test added for each finding,
- exact test result.

Review comments should be precise enough that a fresh agent can fix the issue without private chat context.

## Completion report

When an agent finishes an assignment, report:

- role performed,
- issue/PR number,
- branch,
- final commit SHA or merge SHA,
- exact tests/checks,
- PR URL if applicable,
- whether anything remains blocked,
- which dependency/gate this work unlocks.

## Prime directive

Do not optimize for making a PR green. Optimize for preserving one coherent, deterministic, semantic 3D building/electrical model that every lane can trust.
