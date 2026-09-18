# Derived drawings

`oabm.drawings` turns a canonical `BuildingModel` into deterministic presentation artifacts. The canonical model remains the only semantic source of truth: drawing views, annotations, dimensions, SVG, and schedules are outputs only and are never read back as model geometry.

## Output model

`DrawingSet` contains orthographic `DrawingView` objects, semantic schedules, and a stable source index. Every derived primitive, dimension, and schedule row references one or more canonical entity IDs. The source index retains canonical confidence and provenance so a rendered item can be traced back to the model without copying or redefining the canonical entity.

Drawing output has its own presentation version (`1.0.0`). This is deliberately separate from the canonical model schema version because changing presentation metadata or rendering details must not mutate the model contract.

## Views

- Plans use the canonical XY plane and a level-relative cut plane. Level hosting plus explicit 3D geometry controls visibility.
- Elevations and sections use an orthonormal frame derived from an explicit origin and view direction.
- Bounds clip projected polylines and polygons deterministically. Geometry outside a view is omitted; geometry crossing a boundary is clipped rather than discarded.
- Sections distinguish geometry crossing the section plane from geometry merely visible within the configured depth.
- Wall thickness and height are derived from canonical wall centerlines and dimensions. Equipment, devices, openings, obstacles, and fittings use their canonical poses and sizes where available.
- Routes use their canonical centerlines. The drawings lane never reroutes them or recomputes takeoff lengths.

## Visibility and annotation hooks

`VisibilityPolicy` controls architecture, electrical, routes, spaces, obstacles, constraints, labels, and dimensions. Obstacles and route constraints are hidden by default and can be enabled for coordination views.

`SymbolProvider` and `LabelProvider` are presentation hooks. They receive canonical entities and the view type and return display-only symbols or labels. Hooks cannot change canonical model objects.

## Schedules

Schedules are stable-ID-sorted views of canonical semantics for rooms, openings, electrical equipment, electrical devices, and circuits. They intentionally do not calculate conduit, cable, or conductor quantities; that belongs to the quantities workstream.

## Determinism

Generation sorts canonical inputs by stable IDs, quantizes output coordinates, uses content-derived primitive IDs, and serializes JSON with sorted keys. Reordering canonical collections therefore does not change output. `fixtures/drawings/v1/expected-drawing-set.sha256` is a golden fingerprint of the serialization generated from the synthetic drawing fixture and fixed view specifications.
