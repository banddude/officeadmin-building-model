# Physical route generation from trusted geometry

This opt-in routing API generates canonical `PhysicalRoute` line/quarter-arc geometry from supplied facts. It does not import plans, choose conductor/raceway sizes, authorize penetrations, select fittings/supports, or certify installation/code compliance. Legacy `Route` APIs and unsupported-consumer refusals remain unchanged.

## Inputs and result

Call `oabm.routing.physical_generate.generate_physical_route(source, context, requirements, source_json=None)` with:

- A current, explicitly upgraded canonical 1.7 pre-route `BuildingModel`.
- `PhysicalRoutingContext`, including exact source/request binding, trusted endpoint ports, allowed-box union, complete geometry coverage, explicit bend penalty and execution limits.
- `PhysicalRouteRequirements` with actual outside diameter, centerline radius, clearance and terminal/inter-bend minima, retaining supplied field-specific provenance.

No solved candidate, sharp polyline or extracted drawing is required. Existing canonical coverage and profile admission apply. Unspecified endpoint-owner geometry is disclosed; hard blockers cannot be excluded. Original source JSON, when available, must be passed to retain original numeric-lexeme admission. Typed request numbers use canonical Python-rendered semantics. No coordinates are snapped during input admission.

The result contains `status`, `reason`, a canonical `route` or `None`, and a deterministic derived JSON report. Only `verified_route` carries a route. That status means live physical feasibility for the captured supplied scope, **not completed search or an optimal route**. The report separately states `search_complete`, termination reason, ranking uncertainty, exact objective, work counters, remaining-work cursor, candidate outcomes and the selected route's live verification receipt.

A verified incumbent may be returned when the bounded search stops. This is explicit partial success: the route passed verification, while the report says search is incomplete. A failed result never carries a successful route. `search_exhausted` concerns only the finite family; `resource_limit` is not a proof of impossibility. Unresolved candidate geometry/comparison remains disclosed. No continuous-space or global optimality claim is made.

The source and request are detached before work and never mutated. A result establishes truth for that captured snapshot, not an atomic guarantee about concurrent caller edits. Retained hashes and receipts never replace current live verification after later changes.

## Finite family and ordering

Family profile `physical-event-planes-single-offset-v1` derives three sorted integer-nanometre coordinate sets from:

1. Unchanged endpoint coordinates, allowed-domain faces and blocker faces.
2. Minimum positive terminal-control anchor positions.
3. Strict outside blocker proposals at tube radius + supplied route/blocker clearances + fixed 1 micrometre certification margin.
4. Inward domain-face proposals with tube radius + route clearance + margin. These do not impose per-box containment; the verifier checks the actual union.
5. Exactly one offset pass using 0, plus/minus centerline radius, and plus/minus (twice radius + positive inter-bend minimum), followed by integer floor/ceiling proposals.

Only generated proposals are rounded. Consumed source coordinates remain exact. Non-endpoint planes are clipped to the union bounding interval and the numerical profile; endpoints are never moved. Product cardinality is checked before search. Exceeding the declared grid cap is an explicit resource result, not silent coarsening.

The graph contains every long cardinal edge between those coordinates. Interior turns are perpendicular; maximal straight lines are coalesced. Walks retain full parent histories and allow repeated virtual corners. A repeated sharp corner is not proof that two actual rounded bends intersect. The family permits up to 511 bends (1023 physical primitives). No position/direction dominance merge discards occupied history.

Examination begins with a deterministic prefix of at most 16 distinct realizable members: direct, endpoint Manhattan permutations, terminal-anchor Manhattan bridges, then parallel lateral doglegs ordered by distance and coordinate. It then uses a control-polygon L1 heuristic with stable push-order ties. That heuristic is an examination order, **not a physical-cost lower bound**. Every complete candidate is converted to canonical geometry and independently live-verified. Exact signatures deduplicate complete candidates only, never search states.

Virtual sharp legs and corners are never used as physical obstacle solids. They can include material absent from a fillet and miss material present along its actual curve.

## Exact geometry, identity and ranking

For virtual corner V, incoming cardinal tangent t, outgoing tangent u and radius R, the joints are V−R*t and V+R*u. The canonical arc stores the positive cardinal axis normal with signed quarter-turn. Its centre and radius derive from the shared joints. Terminal lines have sharp length minus R; inter-bend lines have sharp length minus 2R. All remain positive and meet supplied minima. A single straight route uses the maximum of terminal minima, not their sum.

Route identity is caller-supplied. Joint and segment identities use route-scoped semantic slots and up to 32 deterministic collision probes against the complete source identity set. They do not depend on input collection order or the source hash. Source/model/domain/requirements identity conflicts are rejected. Generated geometry provenance is inferred; requirements retain supplied provenance. Canonical serialization is reparsed and exact joint ticks checked before live verification.

The objective in nanometres is `a + b*pi`, where a is actual straight length plus bend penalties and b is bend count times radius divided by two. Exact rational Machin-series enclosures compare objectives, followed by bend count and canonical geometric signature. Overlapping enclosures are not equality. Comparison uncertainty is retained rather than rounded away. The report does not claim that unexamined or unresolved candidates were beaten.

## Resource accounting

One shared original `total_refinement_cells` work ledger covers charged search/preprocessing and all candidate proof work. Candidate verification does not reset its budget or rewrite the request's limits. Receipts separate candidate-local traces from cumulative session charges. Source/request snapshot admission has independent byte/node/depth caps; abstract work units are not a wall-clock or total CPU guarantee.

Additional fixed profile bounds are 64 MiB reserved search-state retention, 1024 reserved bytes per retained state, 16 MiB reserved exact candidate signatures, 64 objective-series terms, 32 identity collision probes and 8 MiB generation reports. Source/candidate/combined snapshots retain the verifier's 32/16/48 MiB bounds. Report overflow refuses output rather than truncating required scope disclosures. Search cursor units are explicit; unfinished work retains an unknown remaining count rather than claiming zero.

Equivalent source collection permutations preserve geometry, IDs, objective and semantic charged ordering. Source-bound hashes and full receipt bytes may differ because source snapshot order is intentionally retained.

## Verification boundary

Use the final code's tests and receipts for acceptance. A supplied-route verifier pass does not by itself demonstrate route generation. Independent generation tests must check actual canonical geometry, curved-only collisions, false sharp-corner obstructions, safe alternative selection, full-history self avoidance, exact minima, raw-number/source binding, budgets and permutation behavior. Tests that inject a small candidate family are separately labelled; they do not prove completeness of the production event-plane constructor.
