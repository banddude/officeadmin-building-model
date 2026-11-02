# Labor reality source checkpoint (#234)

This **unexecuted source slice** depends on unmerged PR #396 / #238 at
`4a8602cce13d0928383cd6b093f56515ed55a07c`, specifically its existing original
quantity contribution hook. It does not modify that hook or the canonical model.
Do not publish this branch as converged or close #234 before that dependency,
coordinated runtime acceptance and private editable standards integration pass.

`oabm.labor.contribution_inventory(model, groups=...)` returns source-bound
selectors for original quantity contributions, not aggregated source membership.
`generate_labor_report(model, request)` detaches and bounds JSON input, captures
and validates the canonical model using the existing bounded snapshot admission,
and emits immutable source/request-bound JSON plus readable CSV. Replaying via
`report.validate_against(model, request)` rejects stale or changed reports.

The request example is in `tests/test_labor_reality.py`. Every field is explicit:
version, normalized source hash, bid groups, versioned policy/source, operation
definitions, finite condition rules, external rates, work allocations and rental
occurrences. No real rates or defaults ship here. Inputs and rates may contain
private data and must remain outside the public checkout. Reports retain rates
and source snapshots, so private reports must also stay outside public artifacts.

Operation eligibility matches category, item type and unit. Preserve the full
variant, assembly, group, evidence and original scalar in each work line.
Fractions conserve scope per exact contribution/operation across phases.
An `exclusive_family` prevents wall length/face/volume or alternative operations
from charging the same installation; conduit install and conductor pull may use
different families. Mixed-basis partial ownership is deliberately refused.
Shared support hardware must select its single canonical contribution once.

Each condition (`occupied`, `hillside`, `no-truck`, `height`) needs an explicit
finite value. Exactly one matching policy rule is applied. Even an ordinary
condition needs an explicit multiplier-one rule. Duplicate matching restrictions
are errors; matched multipliers multiply once. Missing facts/rules/productivity
remain unresolved rather than establishing zero effort or a price.

Height is a supplied band relative to a named known work surface, with evidence
and `measured-split` or `conservative-whole-operation` method. The engine does not
infer a band from route endpoints, average elevation or absolute Z. Measured
splits use independent work fractions that sum to at most one; the caller's
measurement evidence remains subject to acceptance. Automatic geometric split
verification is outside this slice.

Crew roles, counts and rate selections are explicit. A selection may represent
regular, overtime, off-hours or prevailing wage; the engine only checks exact
agreement with the supplied rate response. It never determines legal eligibility.
Each external response includes kind, unit, currency, revision, source, integer
cents, selection and role. Labor uses `person-hour` and rentals use `hire-quantum`.
Base effort = original quantity × fraction × supplied person-hours/unit.
Adjusted effort multiplies matched condition factors. Money uses adjusted
person-hours / crew count × sum(count × role cents/person-hour), rounded once
per work line to integer cents. Unknown inputs produce null amounts.

Explicit work windows require ISO timestamps with UTC offsets, so DST folds are
unambiguous. Windows provide resource occupancy and crew capacity, not an inferred
schedule. A work row has one crew/rate selection; split rows for overtime/off-hours
windows, phases or mobilizations. Windows can retain shutdown gaps. They cannot
overlap within a work row. No cross-row crew identity or scheduling optimizer is
claimed; crew entries are compositions, not named people.

One rental occurrence can serve two objects and is billed once. Explicit hire and
off-hire include retained overnight/shutdown time. Concurrent work assignments
consume capacity, end-before-start at equal timestamps. Required lift/scaffold
kinds must have assigned occurrences. Separate mobilizations require separate
occurrences, and duplicated same-kind ownership is rejected. Quantum billing
rounds the explicit elapsed hire time up by the supplied quantum. This is a
bounded provider contract, not a universal vendor day/week/month tariff. OA's
7/28-day tier coverage needs a future read-only provider adapter.

Parent source verification at private OA commit
`21463cd9293c1203b2a480284d696b6eba7b3b51` establishes an existing editable
base-productivity capability: price-book `laborHours` is hours per unit and the
authenticated item PATCH requires `manage:inventory`. No PATCH was called.
The future read-only adapter must retain unit/rule/rental evidence beyond the
current org-scoped assembly snapshot subset. This is source evidence only,
not live integration or acceptance.

No execution, imports, tests, builds, render jobs or CI dispatch were performed
because the parent placed laptop execution on hold. The synthetic tests are
source-only acceptance work pending that hold's release. Missing private editable
productivity-standard integration, authenticated org-scoped read-only rate adapter, provider
tiers, actual output review and whole issue acceptance remain open. Existing OA
assembly pricing must choose total-hours-in-one-unit **or** one-hour-unit × hours,
never both. This public source neither connects to nor writes OA production.

Height records carry explicit `min_height_m` and `max_height_m` **relative to the
supplied work surface**, and height rules carry finite `height_min_m` and
`height_max_m` bounds. Measured splits must fit the chosen band. A conservative
whole-operation selection can include lower work but must cover the known maximum
in the selected upper band. Unknown surface evidence never becomes absolute-Z
height. Resource rate receipts also declare `quantum_hours`; a conflicting caller
billing quantum is rejected. Labor receipts carry null quantum hours.
