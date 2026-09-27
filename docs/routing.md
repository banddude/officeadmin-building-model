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

`consolidate_bundled_routes(model, *, tolerance_m=0.01) -> ConsolidationResult(model, report)` rewrites bundled per-circuit routes into the raceways a physical installation would use: one trunk `Route` per maximal stretch where a constant set of same-type routes shares a line, plus branch `Route` records for each member's unshared ends. It is a deterministic post-process on the canonical model; nothing calls it by default and a model with no overlapping runs is returned as the same object.

- **Membership first.** Each shared run is swept between member-coverage boundaries; stretches where the member set changes become separate trunks, and a stretch only one participant covers stays with that route as branch geometry. That is what keeps every conductor's length reproducible: a route that joins mid-corridor is re-pointed to only the trunks it actually rides.
- **Exact lengths.** Every cut point is an existing centerline coordinate, so a split route's pieces sum back to its original length, and a conductor re-pointed across its route's pieces in centerline order keeps its old length exactly. Total route length drops by exactly the detector's `double_counted_length_m`.
- **Ports.** Route ends must sit on ports. Ends that already coincide with an existing port reuse it; other trunk junctions get one inferred port per distinct position, owned by the nearest electrical device or equipment owner (a canonical port must be exportable on a distribution element). A model with no such owner fails closed.
- **Trunk size.** A trunk's `nominal_diameter_m` is the largest member's. Fill-based trade-size upsizing is out of scope; each trunk records `attributes.consolidation = {member_route_ids, member_count}` so a later fill check can upsize it.
- **Fittings.** Turn fittings at cut vertices give way to one `tee` per trunk endpoint where a branch or a continuing trunk touches it; a trunk that simply terminates gets none. Elbows strictly inside a branch survive re-parented with their original ids.
- **Identity and provenance.** Trunk, branch, tee, and junction-port ids are `stable_id` derivations from the sorted member route ids (or the source route and piece ordinal) plus span endpoints rounded to 1e-6. New entities carry `derivation="inferred"` provenance with `method="route-consolidation"`.
- **Report.** `report` counts routes before and after (plus trunks, branches, fittings, and added ports) and carries `shared_length_m` and `double_counted_length_m`, which reconcile the before/after totals.
- **Default untouched.** Router output and quantities are byte-identical when the function is not called.

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
