# Deterministic 3D electrical routing

For architectural plans without located source equipment or circuiting, see [Proposed electrical design](proposed-electrical-design.md). Generated placement, circuits, and runs remain design assumptions.

`oabm.routing` consumes the canonical v1 `BuildingModel` and emits canonical `Route` and `RouteFitting` objects. It does not define a second semantic model and has no IFC/Bonsai dependency.

## Algorithm

The v1 router is deterministic and geometry-driven:

1. Resolve the declared start/end `Port` objects and honor their direction vectors with short exit/entry stubs.
2. Expand hard obstacle and keep-out geometry by object clearance, caller clearance, and half the routed nominal diameter.
3. Build a sparse rectilinear 3D coordinate grid from endpoint anchors, obstacle/constraint extents, level elevations, wall centerlines, ceiling geometry, and optional bundle-hint interval endpoints.
4. Run deterministic Dijkstra search over adjacent grid coordinates. Search state includes incoming direction, bend count, and which hard required corridors have been visited.
5. Score length plus bend cost, optional vertical cost, soft-obstacle penalty, preferred-corridor discount, wall/ceiling pathway discount, and optional bundle-hint discount.
6. Simplify collinear points, emit the canonical centerline, then emit ordered canonical fitting decisions at every remaining direction change.

There is no random seed, heuristic learned state, or input-list-order dependence. Equal-cost ties are resolved from sorted canonical geometry and coordinate ordering.

## Constraint semantics

Constraints apply when `applies_to` is empty, contains the route type, or contains `*`.

- hard `keep-out`, `no-go`, `forbidden`, or `avoid`: blocked geometry
- soft `keep-out` / `avoid`: allowed with a cost penalty
- hard `required-corridor`, `required`, `must-pass`, or `must-use`: the route must intersect the corridor at least once
- soft required-corridor and `preferred-corridor` / `preferred` / `corridor`: lower-cost routing region
- unknown active constraint types fail explicitly instead of being silently ignored

Canonical `Obstacle` objects are hard unless `obstacle_type` is `soft` or `advisory`.

Wall and ceiling geometry contributes candidate coordinates and optional lower-cost pathway regions. Walls and ceilings are not implicitly hard obstacles; a caller must represent a true no-go region with `Obstacle` or a hard route constraint.

Glazed walls (`Wall.construction == "glazed"`) are never surface pathways: they contribute no surface rule and get no `surface_path_discount`. Instead, any segment whose midpoint lies inside a glazed wall's padded bounds (the same padding a surface rule would use) costs `glazed_wall_penalty` times its length, so concealment in glazing is discouraged but still allowed when the geometry forces it. Each final route segment whose midpoint remains inside a glazed wall's padded bounds is reported as a `route_in_glazed_wall` entry in the route attributes, with the wall id and segment index. Glazing stays soft: a true no-go region is still the caller's `Obstacle`.

## Caller-supplied allowed route volume (opt-in)

`route_between_ports(..., allowed_domain=AllowedRouteDomain(...))` accepts an
explicit, transient route-request constraint. `AllowedRouteDomain` names the
caller-supplied union of finite `Box3D` volumes; its `kind` and `source_ids`
are descriptive receipt fields, not physical objects. The domain does not
create walls, framing, openings, or other blockers, and it does not replace
canonical model geometry. Existing hard `Obstacle`, hard route constraints,
and `StructuralMember` geometry remain independent no-go volumes.

When supplied, the router requires the complete swept route envelope—not just
ports or corridor visits—to fit in the union of volumes. Each straight segment
and both port stubs are expanded to a conservative axis-aligned box by
the resolved nominal radius plus `RoutingOptions.clearance_m`; a domain-mode
route therefore requires a known positive diameter from the explicit argument
or port data. The nominal-radius envelope is modeled geometry only: actual
raceway/cable outer diameter, fitting envelopes, bend radius, and installation
clearance are not verified. `clearance_m` is a geometric input, not an
installation/code clearance determination. That swept box must be covered by
the domain-volume union. The same check runs after simplification and after
optional bundle-source alignment. Candidate edges that leave the union are
discarded by Dijkstra. Boxes can meet at a seam: union coverage is checked
across the seam rather than requiring each complete edge to fit in one box.
This swept-AABB rule is conservative for round cable/conduit near corners. A
domain must be a nonempty tuple of positive finite, cardinal-axis `Box3D`s;
quarter-turn boxes are accepted only when their eight corners differ from the
nearest signed axis permutation by no more than the declared 1e-9 m positional
epsilon. Non-cardinal rotations are rejected; accepted boxes are not expanded
to the AABB of a genuinely rotated volume. The first implementation caps the
union at 128 boxes.
The lattice is capped at 200,000 candidate nodes, each search at 500,000 pushed
states, and a single union-coverage check at 4,096 uncovered fragments;
exceeding a cap fails explicitly rather than weakening containment.

The router writes the exact caller volume boxes (pose and size, without
rounding), domain id/kind/source ids, nominal route radius, clearance, and
containment method into the router provenance, and marks actual outer diameter
as unverified. Route attributes add
`geometry_check.status = "contained_and_clear_of_checked_blockers"`, the
supported blocker classes and up to 256 sorted blocker IDs, plus explicit
limitations and a generation-time-only revalidation marker. The checked hard
blockers for models without populated assemblies are `Obstacle`,
`StructuralMember`, and hard no-go/avoid `RouteConstraint` geometry. In that
legacy no-assembly case, `Wall` and `Ceiling` geometry remain pathway
preferences, while `Slab` and `Opening` geometry is not checked as a blocker.
If no domain is passed, legacy no-assembly routing is unchanged and emits no
domain metadata. `design_proposed_circuits(..., allowed_domain=...)` passes the
same explicit domain to every generated route leg.

An endpoint or stub outside the selected volume raises `NoRouteError` with a
stable reason code. A route blocked within the union raises
`NoRouteError(reason="no_path_within_allowed_domain")`; this means no path was
found on the bounded deterministic candidate grid, not that continuous free
space was exhaustively proven disconnected. Its sorted `blocker_ids` identify
hard geometry encountered on rejected candidate edges, not a minimal cut set.
`RouteConstraint` required corridors remain visit-once constraints and are
not reinterpreted as allowed volumes. For `design_proposed_circuits`, the
normalized non-null domain request is retained in circuit design metadata;
absence of that field remains the legacy unbounded representation, preserving
default serialization. Reusing a `user_input_id` cannot silently change
between bounded and unbounded routing or replace its domain. Bounded design
also stores a deterministic `bounded-routing-context-v1` fingerprint per leg
over the selected ports, route type/diameter, all routing options, allowed
volumes, and router-relevant model geometry (including framing members and
active constraints), plus a fingerprint of the saved route and fittings.
Reusing that decision requires both fingerprints to match current inputs; a
changed port, clearance, or blocker fails closed and needs a fresh decision ID
to regenerate. The geometry-check receipt describes the calculation when
that route was produced; it is not live validation and does not update after
host-model changes.

### Assembly-aware route slice (#356, opt-in)

A model with populated 1.6 building assemblies can be routed only when the
caller supplies an explicit `AllowedRouteDomain` and a known positive nominal
diameter. The domain is still caller-selected finite free space: its `source_ids`
are descriptive references, not permission to drill, notch, create portals,
cross drywall/deck, or infer an equipment-box exit. Routes and port stubs must
remain inside the swept-envelope union and avoid the hard geometry below.

The existing router temporarily adapts every owned `AssemblyComponent` into a
hard rule keyed by the canonical component ID. `Box3D` children use the router's
exact oriented-box distance checks after a query-only quaternion normalization
matching canonical 1.6 geometry; the source quaternion is not changed. Panels
use their declared mid-plane with half physical thickness plus routed radius
and caller clearance; `WireSweep` children use declared wire radius plus routed
radius and clearance. `Profile` extrusions use a conservative world-axis
enclosing bound. That may block a
C/U/T profile's void, but never treats its void as verified free space. For a
suspended-ceiling assignment that references an existing `Slab`, that same
canonical slab is reused once as a hard blocker from its footprint top face
downward by `thickness_m`; a conservative world-XY footprint bound may
over-block a rotated/irregular edge. A generated `deck_panel` remains its one
owned component blocker.

Wall/ceiling aggregate pathway preferences are suppressed where an assigned
aggregate's physical children replace them. This first slice refuses routing
when any modeled `Wall` or `Ceiling` aggregate is unassigned; its old soft
surface rule is not represented as physical collision coverage. Other `Slab`
records, architectural openings/voids, unmodeled trades, and outside-domain
geometry remain outside this slice. No portals, holes, framing edits,
penetrations, or equipment-box transitions are generated. Routing is a bounded
geometric proposal, not installation engineering or code approval.

`derive_wall_cavity_route_domain(model, assembly_id, nominal_diameter_m=...,
options=...)` is an opt-in adapter for straight horizontal cardinal-axis
`wall.wood_rect_stock@2.0.0` assignments with no hosted openings. It measures
the actual generated drywall inner faces, continuous top/bottom plate stacks,
and full-height ordinary stud boxes; each gap between adjacent studs becomes
one separate route volume. It does not infer wall thickness or use preset
spacing to create free space. The router still checks every generated part as a
hard blocker, so a route stays in its selected bay and cannot pass through a
stud, plate, or panel. Different bays have no connected route volume between
them.

The adapter refuses opening framing, non-cardinal or non-straight walls,
cripples/headers/jacks/kings/sills or other unsupported parts, incomplete or
misaligned drywall/plate geometry, insufficient swept clearance, and inputs
that exceed its 512-part, 128-bay, or 65,536 drywall-coverage-cell limits. Its
domain identity binds the assignment digests, selected wall, all generated
component IDs, diameter, clearance, and derived cells. Route entry re-derives
and compares that exact domain. Stored assembly-aware routes retain the
existing paired context receipt, so changing generated component geometry
requires regenerating the route. The cells authorize no drilling, notching,
penetrations, wall/ceiling transitions, or structural work; routing does not
select circuit sizes or approved installation paths.

An assembly-aware router route retains an `assembly-routing-context-v1`
receipt on its primary bounded-router provenance. Supplemental provenance such
as bundle alignment does not duplicate that receipt. It names sorted
assembly input/output digests, checked component IDs, selected deck IDs, and
the aggregate wall/ceiling IDs whose preferences were suppressed. Canonical
model construction/load requires the assembly receipt to remain paired between
the primary bounded-router provenance and geometry-check record, and rejects
stale, missing, or malformed halves when either side or checked blocker
identities claim assembly routing. Bounded design idempotence includes the same
subreceipt; changed canonical parts require a fresh routing decision. This is a
narrow assembly freshness rule, not generic dependency tracking and not a new
schema field/version. Manual routes and legacy unbounded routes without that
explicit claim are not promoted to assembly-checked routes, and legacy
no-assembly route edit semantics remain unchanged.

## Bundle hints (optional)

A caller that routes runs in sequence can pass earlier runs to `route_between_ports` as `bundle_hints=BundleHints(paths=(...), discount=...)`. Later runs may then follow those paths at reduced edge cost, so home runs from one panel bundle onto a few shared trunks instead of zigzagging independently. The caller decides the order and which paths to pass; the router only prices edges. Hints are caller input only: they are never persisted and never become routes themselves.

Semantics are along-only:

- only axis-aligned hint segments are indexed; diagonal segments are ignored;
- only an edge lying on a hint segment gets the discount (`1.0 - discount`);
- parallel offsets get nothing, edges crossing a hint get nothing, and an edge running past a segment end is not discounted.

Calls that produce no usable index are byte-identical to passing no hints: no argument, `bundle_hints=None`, empty `paths`, diagonal-only paths, or `discount=0`. Any other hint also adds the hint's interval endpoints to the routing lattice, so a hint can change a route even when that route does not follow it.

A hinted route carries two extra attributes: `bundle_hint_discount` (the applied discount) and `bundle_hint_shared_m` (meters of centerline that actually lie on hint segments). Route id, provenance method, and fitting ids are unchanged; hints affect cost and geometry only.

Worked example (synthetic): a panel at `(0, 0, 2.7)` serves a luminaire at `(10, 2, 2.7)` with 0.15 m stubs. Without hints the run crosses on its own line, `((0,0,2.7),(0,0,2.85),(0,2,2.85),(10,2,2.85),(10,2,2.7))`, 12.3 m and 3 bends. With the hint segment `(0,1,2.85)-(10,1,2.85)` at `discount=0.25`, the 10 m trunk ride costs `10 x 0.75 = 7.5` equivalent meters, which beats the two extra bends (`bend_penalty_m` of 0.30 each): the route becomes `((0,0,2.7),(0,0,2.85),(0,1,2.85),(10,1,2.85),(10,2,2.85),(10,2,2.7))`, still 12.3 m of conduit, with `bundle_hint_shared_m == 10.0`.

Callers own the hint list. Pass only earlier runs on the same routing plane, clipped to a window around the new run's endpoints, so the added lattice stays small. A route joins a hint only when the discount on the shared length beats the extra bends the join costs (`bend_penalty_m` each).

## Overlapping runs (read-only)

`find_overlapping_route_runs(model, tolerance_m=0.01)` reports where same-type routes share geometry: collinear, axis-aligned centerline spans whose intersection is longer than the tolerance. The pairwise spans group onto their lines, and each maximal shared run is a connected component of the union of the participating routes' own coverage on its line — a route riding the whole corridor merges the stretches it shares with different routes into one run. Each `RouteOverlap` carries sorted `route_ids`, the `route_type`, `shared_length_m` (the run's union length), the span endpoints, and `double_counted_length_m`: the sum over member routes of each route's covered length inside the run (its own spans unioned) minus the run's union length, i.e. the conduit a per-route takeoff counts more than once. Ordering is deterministic and the model is never mutated. Runs that only touch at a point, cross perpendicularly, sit farther apart than the tolerance, or carry different `route_type`s never overlap.

`extract_quantities` emits one `overlapping_route_runs` warning per affected route type, naming the member routes, the total shared length, and the actual overlap: per-route lengths count the runs' `double_counted_length_m` more conduit than the shared runs occupy. Quantities themselves are unchanged, so reports without overlaps are byte-identical. Consolidating shared runs into trunk routes is #196's separate, opt-in post-process; this helper only tells consumers the overlap is there.

## Consolidating bundled routes (opt-in)

`consolidate_bundled_routes(model, *, tolerance_m=0.025) -> ConsolidationResult(model, report)` rewrites bundled per-circuit routes into the raceways a physical installation would use: one trunk `Route` per maximal stretch where a constant set of same-type routes shares a line, plus branch `Route` records for each member's unshared ends. It is a deterministic post-process on the canonical model; nothing calls it by default and a model with no overlapping runs is returned as the same object.

- **Membership first.** Each shared run is swept between member-coverage boundaries; stretches where the member set changes become separate trunks, and a stretch only one participant covers stays with that route as branch geometry. That is what keeps every conductor's length reproducible: a route that joins mid-corridor is re-pointed to only the trunks it actually rides.
- **Lengths and snaps.** Aligned pieces preserve each conductor's original traversal length exactly. Centerlines within the 25 mm default tolerance snap onto one shared trunk; short connector branches close the geometric gap. Each shifted shared segment and connector carries its original and adjusted coordinates and signed length delta in inferred provenance. A tiny closed detour caused only by this snap is removed with its signed delta recorded. A route that retraces a shared stretch references that physical subtrunk again for conductor length while the raceway is counted once. Offsets beyond tolerance remain unconsolidated.
- **Ports.** Route ends must sit on ports. A shared member's original endpoint may supply the port at that exact position. A new junction gets an inferred `raceway` port owned by the fitting at that position. A straight route split gets a `logical-junction` fitting solely to own its port; it is marked nonmaterial and excluded from material fitting quantities. IFC creates fittings before their owned ports.
- **Trunk size.** A trunk's `nominal_diameter_m` is the largest member's. Fill-based trade-size upsizing is out of scope; each trunk records `attributes.consolidation = {member_route_ids, member_count}` so a later fill check can upsize it.
- **Fittings.** Physical outgoing directions determine one junction fitting per position and route type: a tee where three or more directions meet, an elbow where two directions turn, and no fitting for straight continuation. Branch-interior fittings keep their IDs and appear in the branch's ordered `fitting_ids`. Same-type fittings at the same position on a shared span become one trunk fitting; a retained interior fitting becomes a trunk centerline vertex for IFC ordering.
- **Identity and provenance.** Trunk, branch, fitting, and junction-port ids are deterministic `stable_id` derivations from source route ids, positions, and types. New entities retain their source provenance records and add `derivation="inferred"` with `method="route-consolidation"`.
- **Per-route refusal.** A closed branch detour needing explicit fitting geometry stays in its original route, as does an adjacent source route whose re-use of the same trunk would create consecutive duplicate conductor references. The overlap union is recalculated without those routes; other bundles still consolidate. `routes_left_untouched` counts all original routes kept, while `untouched_route_reasons` lists the refused route IDs and stable reason codes. Its tuples are sorted for deterministic reporting.
- **Report.** `report` counts routes before and after (plus trunks, branches, fittings, and added ports). Its net `double_counted_length_m` reconciles the before/after totals; `within_route_repeated_length_m` identifies retracing inside one route. `snap_adjustments`, `snap_added_length_m`, `snap_collapsed_length_m`, and `conductor_length_delta_m` expose the bounded geometry adjustments. `shared_length_m` is physical trunk length.
- **Length acceptance.** Aligned route pieces preserve each conductor's path length. With near-aligned shared centerlines, a conductor may change only through a recorded connector or collapse bounded by the stated 25 mm snap tolerance per shared segment. Each adjustment records its coordinates and signed length delta in provenance; the report totals the conductor delta. Review the reported delta with the takeoff before using the result.
- **Default untouched.** Router output and quantities are byte-identical when the function is not called.

Whole-route product selections and selection outcomes, including unresolved
outcomes, retain their original routes with reason
`whole_route_product_binding_requires_reselection`. Routes named by support
assemblies or support-selection outcomes retain their identity with reason
`support_binding_requires_reselection` (the product reason takes precedence
when both apply). The eligible overlap union is recalculated, so unrelated
unbound routes can still consolidate. No product or support decision is dropped
or copied to a new trunk. Explicit topology-aware re-selection remains separate
work. This guard does not establish conductor compatibility, fill, ampacity,
support loading, or code compliance.

## Bend limits and fitting decisions

`RoutingOptions` controls bend penalty, maximum bend count, port-stub length, clearances, search margin, soft preferences, and the glazed-wall penalty (default 4.0, must be positive). These are algorithm controls, not a replacement for canonical model fields.

Every centerline direction change becomes an ordered `RouteFitting`. Ninety-degree and forty-five-degree changes use `elbow-90` and `elbow-45`; other angles use `elbow` with the exact `angle_radians`.

## Stable identity and provenance

A route ID is derived from the stable logical connection: model ID, route type, start port ID, and end port ID. It is intentionally independent of mutable route geometry.

Fitting IDs are derived from the route ID plus the cumulative direction-transition signature. This keeps fitting identity stable when geometry moves without changing route topology and avoids IDs based on list position.

Routes and fittings carry `router` provenance with method `deterministic-rectilinear-v1`.

## Failure behavior

`NoRouteError` is raised when no path satisfies hard geometry, required corridors, endpoint direction stubs, and bend limits. `RoutingError` is used for invalid requests or unsupported active constraint semantics.

## Geometry note

The routing grid uses conservative axis-aligned bounds to generate candidate coordinates. Active `RouteConstraint` geometry is evaluated against its actual shape: rotated `Box3D` values use their quaternion pose as oriented boxes with Euclidean clearance, route radius, and corridor tolerance; `Polyline3D` and planar `Polygon3D` values use shape-aware distance checks. Their AABBs are only broad-phase filters. Non-planar or degenerate polygon constraints fail explicitly instead of being reinterpreted. Canonical obstacle and surface-path bounds remain conservative in this first deterministic engine.

## Exact source alignment for typed bundle hints (#223)

`BundleHints(paths=(...), route_type="emt")` explicitly declares the routing type
of a shared corridor. Matching typed hints also enable a bounded alignment pass
before canonical route/fitting construction. `RoutingOptions.bundle_alignment_tolerance_m`
sets the maximum centerline offset (default 0.01 m, zero disables alignment).
Untyped legacy hints retain their existing cost-only behavior and byte-level
output; a mismatched declared type is not used for either discount or alignment.

The pass retains original port positions and direction stubs, uses exact hint
coordinates for the overlapping shared span, and inserts measured rectilinear
connectors where needed. Reversed traversal and partial overlaps retain their
actual connected path. An already shared corridor is never displaced toward a
nearby alternative; tied nearest corridors fail closed. Every proposed path is
rechecked against expanded hard blockers, required-corridor coverage and the
bend limit. Out-of-tolerance or rejected alignments leave the original valid
route untouched with an explicit reason under `attributes.bundle_alignment`.

Successful alignment retains the original routing provenance and adds an inferred
`bundle-source-alignment` record with source/aligned endpoints, offset, tolerance
and signed length delta. Route identity is unchanged. Fittings and measured route
length are generated from the final centerline, so downstream conductor takeoff
includes connector length rather than omitting a branch-to-trunk gap. This is a
geometric proposal; it does not add a physical minimum bend-radius design rule.
