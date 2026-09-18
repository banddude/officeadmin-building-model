# Derived drawings and schedules

`oabm.drawings` consumes the merged canonical `BuildingModel` contract and produces
read-only derived drawing data. It does not introduce a second building/electrical
model and does not mutate or reinterpret canonical IDs.

## Outputs

`DrawingGenerator` provides deterministic:

- level plans in canonical XY,
- north/east/south/west orthographic elevations,
- arbitrary vertical sections defined by a canonical XY section line and depth,
- electrical equipment, electrical device, opening, and circuit schedules,
- overall derived dimensions for visible projected geometry,
- dependency-free SVG view rendering and CSV schedule serialization.

`default_set()` generates one plan per level, four cardinal elevations, two central
orthogonal sections when model geometry exists, and the four schedules.

## Identity and provenance

Every projected primitive keeps a `SourceReference` with the canonical entity ID and
a deterministic tuple of source provenance keys. Annotations and schedule rows also
point back to canonical entities. Drawing IDs are stable functions of view IDs,
canonical entity IDs, and projection roles. They are references for derived output,
not new semantic entity IDs.

## Projection and clipping

Projection frames are explicit orthographic bases. Plans use XY with +Z as depth;
elevations use a cardinal horizontal axis and +Z vertically; sections use the section
line horizontally, +Z vertically, and the perpendicular XY axis as cut depth.

Polyline segments are clipped both to optional view depth and to 2D sheet bounds.
Polygons use deterministic Sutherland-Hodgman sheet clipping. Section polylines clip
segments against the cut slab, so an edge that crosses the section remains visible
even when both original endpoints are outside the section depth.

## Visibility and extension hooks

`VisibilityFilter` can include selected canonical categories, exclude entity IDs,
apply a confidence threshold, or restrict electrical system values. Plan generation
also filters level-hosted entities by canonical `level_id` and clips unhosted route
geometry to the selected level's vertical band.

Symbol and annotation hooks receive only canonical entities plus projected context.
They may change drawing presentation without changing canonical model semantics.

## Determinism

Canonical entity collections are sorted by stable ID before drawing/schedule output.
Default views have fixed ordering, schedules have fixed columns and ID-sorted rows,
floating serialization is normalized, SVG/CSV rendering is ordered, and the synthetic
drawing fixture has a checked-in SHA-256 golden digest. Tests also reverse canonical
collection order and require byte-identical serialized output.

## Scope boundary

This lane does not recognize PDFs, import RoomPlan, serialize IFC/Bonsai, route
systems, or calculate takeoff quantities. Routes already present in the canonical
model can be projected as drawing geometry, and circuits can be listed in schedules.
