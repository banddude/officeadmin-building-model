# Deterministic 3D electrical routing

For architectural plans without located source equipment or circuiting, see [Proposed electrical design](proposed-electrical-design.md). Generated placement, circuits, and runs remain design assumptions.

`oabm.routing` consumes the canonical v1 `BuildingModel` and emits canonical `Route` and `RouteFitting` objects. It does not define a second semantic model and has no IFC/Bonsai dependency.

## Algorithm

The v1 router is deterministic and geometry-driven:

1. Resolve the declared start/end `Port` objects and honor their direction vectors with short exit/entry stubs.
2. Expand hard obstacle and keep-out geometry by object clearance, caller clearance, and half the routed nominal diameter.
3. Build a sparse rectilinear 3D coordinate grid from endpoint anchors, obstacle/constraint extents, level elevations, wall faces and end caps, opening extents, and ceiling geometry. Wall centerlines are deliberately not grid planes so a crossing cannot ride a centerline row for free.
4. Run deterministic Dijkstra search over adjacent grid coordinates. Search state includes incoming direction, bend count, which hard required corridors have been visited, and the last strict side of a wall centerline seen inside a wall solid.
5. Score length plus bend cost, optional vertical cost, soft-obstacle penalty, preferred-corridor discount, wall/ceiling pathway discount, and wall penetration cost.
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

### Walls as traversable obstacles

A canonical `Wall` is treated as an obstacle on one level: its footprint is the centerline swept by `thickness_m` between the wall base and `base + height_m`. Travel along a wall face or parallel to the centerline stays legal and keeps the lower-cost surface-pathway discount; conduit is not pushed through walls for free.

- A route segment may cross the footprint through an `Opening` whose `host_id` references that wall: the crossing is free and leaves no trace.
- Any other crossing of the footprint is a penetration. It is allowed at `RoutingOptions.wall_penetration_cost_m` (default 3.0, priced proportionally to the crossed depth versus wall thickness) and every penetrated wall id is recorded, sorted, on the route under `penetrated_wall_ids`.
- Crossings are detected by strict side changes of the wall centerline, tracked in the search state so a crossing split across grid nodes still charges exactly once.
- Routing below the wall base or above its top stays outside the footprint and is not a penetration; model slabs or ceilings explicitly when they constrain the path.

Walls never make a route impossible by themselves: crossing is always permitted through an opening or at the penetration cost.

## Bend limits and fitting decisions

`RoutingOptions` controls bend penalty, maximum bend count, port-stub length, clearances, search margin, soft preferences, and the wall penetration cost. These are algorithm controls, not a replacement for canonical model fields.

Every centerline direction change becomes an ordered `RouteFitting`. Ninety-degree and forty-five-degree changes use `elbow-90` and `elbow-45`; other angles use `elbow` with the exact `angle_radians`.

## Stable identity and provenance

A route ID is derived from the stable logical connection: model ID, route type, start port ID, and end port ID. It is intentionally independent of mutable route geometry.

Fitting IDs are derived from the route ID plus the cumulative direction-transition signature. This keeps fitting identity stable when geometry moves without changing route topology and avoids IDs based on list position.

Routes and fittings carry `router` provenance with method `deterministic-rectilinear-v1`.

## Failure behavior

`NoRouteError` is raised when no path satisfies hard geometry, required corridors, endpoint direction stubs, and bend limits. `RoutingError` is used for invalid requests or unsupported active constraint semantics.

## Geometry note

The routing grid uses conservative axis-aligned bounds to generate candidate coordinates. Active `RouteConstraint` geometry is evaluated against its actual shape: rotated `Box3D` values use their quaternion pose as oriented boxes with Euclidean clearance, route radius, and corridor tolerance; `Polyline3D` and planar `Polygon3D` values use shape-aware distance checks. Their AABBs are only broad-phase filters. Non-planar or degenerate polygon constraints fail explicitly instead of being reinterpreted. Canonical obstacle and surface-path bounds remain conservative in this first deterministic engine.

Wall footprints are broad-phased with a bisect index over their x-intervals with prefix-maximum pruning, then evaluated against the actual centerline, thickness, and height window.
