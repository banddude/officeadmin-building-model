# Derived drawing views

`oabm.drawings` turns a validated canonical `BuildingModel` into deterministic 2D plans, elevations, sections, and schedules. These outputs are disposable views. They never replace or mutate the canonical model and they do not introduce a second semantic geometry representation.

## Traceability

Every geometric, symbol, label, and dimension element carries the canonical `source_ids` that produced it. Geometry and symbols also copy canonical confidence and provenance when the source entity provides them. Schedule rows use the canonical entity ID as `source_id`.

Derived element IDs are deterministic hashes of the view ID, role, canonical source IDs, and part index. They are stable for the same view request and source model, but they are not canonical model IDs.

## Projection conventions

All views consume the contract's right-handed, `+Z`-up metre coordinates directly.

- Plan: view plane is canonical `XY`; `+X` is drawing-right and `+Y` is drawing-up. A plan selects one canonical level and clips route/device geometry to the level view range.
- South elevation: observer is on the negative-Y side looking toward `+Y`; drawing-right is `+X`.
- North elevation: observer is on the positive-Y side looking toward `-Y`; drawing-right is `-X`.
- West elevation: observer is on the negative-X side looking toward `+X`; drawing-right is `-Y`.
- East elevation: observer is on the positive-X side looking toward `-X`; drawing-right is `+Y`.
- Section: the caller supplies an `x` or `y` cut coordinate, direction, and finite slice depth. The generator never guesses a section plane.

Views may also apply a 2D crop rectangle and level/category visibility filters. Segment and polygon clipping is deterministic.

## Geometry and annotations

Architecture is projected from canonical spaces, walls, slabs, ceilings, and openings. Electrical equipment and devices use canonical poses and sizes. Canonical routes and fittings are projected when present. A route crossing normal to a section plane is represented by a traceable crossing symbol because its 3D segment collapses to one 2D point.

Default annotation and symbol providers produce stable labels and token symbols, and callers may replace them through `AnnotationProvider` and `SymbolProvider`. Dimensions are drawing annotations derived from visible source geometry. They are not takeoff quantities.

Obstacles and route constraints are optional reference layers and are hidden by default.

## Schedules

The built-in schedules are:

- levels
- spaces
- openings
- electrical equipment
- electrical devices
- routes
- circuits
- conductors

Schedules expose canonical semantic fields and stable IDs. They intentionally do not calculate raceway length, conductor length, material counts, or other takeoff quantities; that belongs to the quantities workstream.

## Determinism

Serialization uses sorted view/schedule/element/row order, sorted JSON keys, finite normalized drawing coordinates, and no runtime-generated identifiers. Repeated generation from the same canonical model and view specifications produces byte-identical JSON.

`generate_standard_package()` creates a plan for every canonical level and the four orthographic elevations. Sections are included only when explicit `SectionViewSpec` values are supplied.
