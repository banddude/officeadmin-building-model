# Explicit raceway and cable selections (contract 1.2)

Issue #266 gives route product decisions a typed canonical home. This is an
explicit caller-input boundary, not an automatic wiring-method, fill, derating,
voltage-drop, jurisdiction, or listed-product verification engine. Those remain
#228/#229. [Shared supports and substrate interfaces](shared-supports.md) use
the explicit 1.3 contract boundary; inherited native raceway checks remain active.

## Version and ownership

The default stays 1.0. Version 1.0 and shipped 1.1 retain their JSON schemas and
serialized behavior. Both reject the new keys even when empty. The explicit
`upgrade_to_v1_2(model)` preserves existing objects, IDs, and box/outcome data,
and adds empty `raceway_selections` and `raceway_selection_outcomes` arrays.
Both arrays and the 1.1 box arrays are required by the standalone 1.2 schema.
Unknown fields/versions fail closed. `upgrade_to_v1_1` refuses a 1.2 downgrade.
The existing box producer may recompute its own inventory within a 1.2 model.

A `RacewaySelection` is a nonphysical decision about one complete `Route`.
It carries catalog/revision/product identity, the route type, wiring method,
trade-size designation for raceway or listed-assembly designation for cable,
a typed `RuleReference` (authority, revision, rule ID), and an optional separate
catalog nominal-diameter claim. It has no independent geometry or ports. The
existing route owns geometry and installed length. There is exactly one active
outcome per route and intent; a selected outcome references the matching
selection/route/product/basis. Duplicate or contradictory selections are invalid.

## Explicit producer

`oabm.electrical.select_raceways(model, intents=..., catalog=..., rules=...)`
requires opt-in 1.2. `RacewayIntent` supplies the intent identity, route identity,
actor, requested product, caller rule reference, and request scope.
`RacewayCatalogEntry` supplies product/method/designation and optional nominal
claim with explicit provenance. `RacewayRule` is caller-authored data granting
one product/route-type pairing under its reference. It carries no code constants,
automatic conditions, or claim of verified approval. Missing or conflicting
input produces a typed unresolved outcome rather than a guessed product.

Whole-route selection is the only supported scope in this slice. A `mixed_run`
or `partial_route` request produces `mixed_run_unsupported` or
`partial_route_unsupported`, with no selected whole-route product. Multiple
requests on one route are rejected as contradictory, not merged. A caller must
resolve segment ownership through separate future contract work rather than
hiding a one-product assumption. This limitation appears in schema, outcomes,
IFC, GLB, schedules, quantity warnings, and tests.

A cable product cannot replace a conduit route. Catalog route type must match
exactly, and its wiring method must agree with the canonical route family.
Unrecognized route families remain unresolved rather than receiving a default
product method. A source-stated `Route.nominal_diameter_m` is never rewritten.
If present, the catalog nominal claim must be known and agree within the fixed
1e-9 metre validation tolerance (zero relative tolerance); otherwise selection
is unresolved. If the source diameter is unknown, an explicit catalog claim may
be retained separately without filling the route field or generating exterior
geometry from a trade-size name. Neither a nominal diameter nor a designation
is an inferred exterior cross-section or verified fill capacity.

The producer recomputes its complete own inventory with IDs based on model and
intent identity, not mutable product fields or sequence order. Repeated calls
are idempotent; revised inputs replace current outcomes without accumulating
obsolete history. Inventory from another source is refused. Catalog scopes are
preserved exactly; unscoped catalog records qualify only consumed catalog
fields. User intent qualifies requested ownership/inputs, caller rule evidence
qualifies the reference, and status/reasons/selection links are scoped inferred
decisions. Consumed input and `code_verification_performed=False` remain auditable.

## Consumers and native edits

IFC keeps the existing native route segments and adds readable product/basis
properties to their route system and segments. Typed selection/outcome records
are non-geometric annotations. Outcome annotations, systems, and segments expose
route, status, request scope, unresolved reasons, and requested product/reference
where known, so ordinary IFC viewers need not parse CanonicalJson to find gaps.
Native route-axis edits read back with product/decision identity intact; this
retains an explicit whole-route choice, not a claim that edited lengths were
recalculated to code. Native method, source-diameter Body, product/reference or
uncertainty contradictions fail closed rather than hiding behind the JSON shadow.
Managed optional product/reference properties must be absent when the typed
outcome omits them. A plain 1.2 upgrade with empty decision arrays must not carry
native selection/outcome property sets; adding those claims without typed
records is refused. Unrelated custom viewer notes remain allowed.

GLB keeps the same number of physical meshes and exposes selected product/basis
and outcomes on route extras, with outcomes also in asset metadata. Drawings
retain route geometry and add product/reference and outcome schedules, explicitly
marking code verification as not performed. Quantity route-length lines include
catalog/designation/reference variants and retain all evidence. The scalar length
consumes route centerline evidence, not unrelated product evidence. Each physical
route is counted once; each conductor retains its actual traversed route length
and count. Unresolved selection and unverified code sizing remain warnings, so
known route length does not masquerade as complete selected-product takeoff.

QA iteration and routing/design ordering include the new typed records.
PDF architecture/electrical convergence refuses downstream selections/outcomes
rather than dropping them; explicitly empty 1.2 inputs preserve the version.
Selection belongs after registered convergence and routing in the documented
`src/oabm/electrical` lane. Synthetic tests cover missing/conflicting/mixed
requests, migrated boxes, actual router output, native IFC edits, and all derived
consumer boundaries. These tests are engineering fixtures, not installation
certification.
