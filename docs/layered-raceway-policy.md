# Finite layered raceway permissions

Issue #278 adds a standalone caller-policy input contract, not a new building
model version. The default model and shipped 1.0/1.1/1.2/1.3 schemas, geometry,
serialization and consumer semantics remain unchanged. This is the bounded
headless input/compiler foundation for #229/#228/#227; those parents remain
open for verified jurisdiction/code data, richer conditions, automatic design,
OfficeAdmin editing/history, natural-language input and cost/labor previews.

## Portable input

`contracts/raceway-policy-v1.schema.json` defines policy version `1.0.0`.
`oabm.electrical.RacewayPolicy.load`, `from_json`, and `from_dict` strictly read
it. The document has a stable `policy_id`, a finite `catalog_products` registry,
and `RacewayPolicyRule` records. Each record has an explicit `RuleReference`
(authority/revision/stable rule ID), layer, boolean enabled state, explanation,
actor ID, one existing lowercase whole-route `route_type`, product permissions,
and explicit source provenance. An empty permission array means permission for
no products, not an unconstrained default.

Unknown fields/versions/predicates, missing fields, malformed identities,
duplicate identities (including two revisions of the same authority/rule ID),
duplicate products and dangling registry references fail closed. JSON duplicate
keys and nonfinite constants are rejected. Constructors and readers use the
same invariants. Provenance confidence/page types reject booleans and nonfinite
values. Input evidence paths must resolve on the input record, for example
`permitted_products` or `reference.revision`; they are not output basis paths.
The reader also checks reference integrity and owner-relative scope resolution,
which JSON Schema alone cannot express.

The public fixture `fixtures/policies/v1/layered-raceway-policy.json` supplies
synthetic products and caller policy only. A layer named baseline is not a
verified NEC edition, local amendment, product listing or installation approval.
There are no electrical code tables or prices in this boundary.

## Evaluation and compilation

`compile_raceway_policy(policy, route_types=...)` evaluates the existing route
types explicitly requested by the caller. It consumes no model geometry and
cannot change a route, source diameter, intent, or catalog dimensions.

For each evaluated type, an explicit baseline is required. All baseline records
must be enabled; neither company nor job input can disable them. Permissions
within a layer are intersected as sets, independent of input order:

1. Intersect every baseline record's finite product set.
2. Check each enabled company record against that baseline intersection, then
   intersect all valid company records with it.
3. Check each enabled job record against the effective baseline/company set,
   then intersect all valid job records with it.

Each record is checked against the completed *prior* layer. A record is not
judged against whichever same-layer record happened to be visited first.
Optional disabled records are explicitly retained in the audit snapshot and
ignored in permission/basis calculation. Job records cannot relax effective
company restrictions. An attempted widening makes the evaluated route type
unresolved, rather than quietly trimming an invalid record. The diagnostic
names the offending reference, each excluded product, and every enabled earlier
record that excludes that product. Missing baseline and empty intersections
also yield deterministic unresolved diagnostics and no compiled permissions.
When an earlier layer widens illegally, compilation stops for that type;
remaining original records are still available in `source_rules`.

Each accepted product/type pair becomes an existing `RacewayRule`. Its generated
`RuleReference` hashes the normalized policy identity/version, product/type,
and complete contributing records, including revisions, content, explanations,
actors and evidence. Permutations do not alter it; changed contributing facts
or revisions do. Changing a disabled or unrelated record does not change an
unaffected effective basis. Compilation snapshots source evidence rather than
keeping mutable caller attribute dictionaries as hidden live state.

Original observed/user/inferred input facts retain their original scopes inside
separate `source_rule` snapshots. Edges qualifying the compiled output `basis`
are explicitly inferred and bounded by contributing evidence confidence; no
observed permission scope is renamed to an observed output reference. Every
compiled edge and compilation result has `code_verification_performed=False`.
All contributing explanations and facts survive the existing selector's
provenance path. The compiler chooses no product or nominal size.

## Explicit selector and consumers

The caller selects a product and its effective compiled reference explicitly in
`RacewayIntent`, then passes `compilation.rules` to `select_raceways`. Existing
catalog/method/diameter checks remain authoritative. A blocked or absent compiled
basis produces an unresolved existing selection outcome, while the caller keeps
the full policy diagnostics; the compiler does not invent a replacement product.

The resulting typed selection uses the existing canonical model version 1.2 or
1.3. JSON and native IFC retain effective basis and original evidence. Native
route/segment property sets expose the generated reference and unverified state.
GLB route extras, drawing schedules and quantity variants carry the same basis.
No additional physical solid, route count or installed length is introduced;
conductors retain their original traversed route lengths. Pure importer inputs
remain free of downstream selections; policy evaluation belongs after routing.

`tests/test_layered_raceway_policy.py` proves an actual public RoomPlan capture
import, explicit synthetic endpoints, obstacle-aware router output, portable
policy compilation and explicit selection through JSON, IFC/readback, GLB,
drawings and quantities. Unit cases cover baseline/company/job intersections,
precise widening blockers, disabled records, missing/empty results, malformed
portable inputs, weak field-qualified evidence and permutation/revision changes.
