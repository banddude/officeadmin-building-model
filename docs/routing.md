# Deterministic 3D electrical routing

`oabm.routing` consumes the canonical v1 `BuildingModel` and emits canonical `Route` and `RouteFitting` objects. It does not define a second semantic model and has no IFC/Bonsai dependency.

## Algorithm

The v1 router is deterministic and geometry-driven:

1. Resolve the declared start/end `Port` objects and honor their direction vectors with short exit/entry stubs.
2. Expand hard obstacle and keep-out geometry by object clearance, caller clearance, and half the routed nominal diameter.
3. Build a sparse rectilinear 3D coordinate grid from endpoint anchors, obstacle/constraint extents, level elevations, wall centerlines, and ceiling geometry.
4. Run deterministic Dijkstra search over adjacent grid coordinates. Search state includes incoming direction, bend count, and which hard required corridors have been visited.
5. Score length plus bend cost, optional vertical cost, soft-obstacle penalty, preferred-corridor discount, and wall/ceiling pathway discount.
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

## Bend limits and fitting decisions

`RoutingOptions` controls bend penalty, maximum bend count, port-stub length, clearances, search margin, and soft preferences. These are algorithm controls, not a replacement for canonical model fields.

Every centerline direction change becomes an ordered `RouteFitting`. Ninety-degree and forty-five-degree changes use `elbow-90` and `elbow-45`; other angles use `elbow` with the exact `angle_radians`.

## Stable identity and provenance

A route ID is derived from the stable logical connection: model ID, route type, start port ID, and end port ID. It is intentionally independent of mutable route geometry.

Fitting IDs are derived from the route ID plus the cumulative direction-transition signature. This keeps fitting identity stable when geometry moves without changing route topology and avoids IDs based on list position.

Routes and fittings carry `router` provenance with method `deterministic-rectilinear-v1`.

## Failure behavior

`NoRouteError` is raised when no path satisfies hard geometry, required corridors, endpoint direction stubs, and bend limits. `RoutingError` is used for invalid requests or unsupported active constraint semantics.

## Geometry note

The routing grid uses conservative axis-aligned bounds to generate candidate coordinates. Rotated `Box3D` geometry is transformed to its enclosing world-space bounds. Active `RouteConstraint` values backed by `Polyline3D` or planar `Polygon3D` are then evaluated against their actual geometry with clearance, route radius, and corridor tolerance applied as distance tolerances; their AABBs are only broad-phase filters. Non-planar or degenerate polygon constraints fail explicitly instead of being reinterpreted. Canonical obstacle and surface-path bounds remain conservative in this first deterministic engine.
