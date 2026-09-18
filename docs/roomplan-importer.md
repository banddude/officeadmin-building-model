# RoomPlan / LiDAR importer

The RoomPlan lane converts Apple RoomPlan `CapturedRoom` JSON into the canonical model contract in `oabm.model`. It does not create a second semantic schema and it does not flatten the capture into a 2D plan.

## Input

The importer accepts the JSON shape produced from a `CapturedRoom`-compatible encoder. The synthetic fixture at `fixtures/roomplan/captured-room-3d.json` follows that shape and contains walls, a floor, door, window, general opening, object, story metadata, confidence, polygon corners, and source transforms.

`load_captured_room(path)` reads JSON from disk. `import_captured_room(document)` accepts an already decoded mapping.

## Coordinates

RoomPlan and ARKit use a right-handed source frame with `+Y` up. Canonical model v1 is right-handed with `+Z` up. The importer applies one rigid basis conversion to every source point and transform:

```text
RoomPlan (x, y, z) -> canonical (x, -z, y)
```

This is a +90 degree rotation around X, so handedness and metric scale are preserved. The full native 4x4 column-major transform is also retained in each entity's `attributes.roomplan.transform_column_major`. Canonical pose rotations are normalized quaternions.

No XY projection is used. Sloped polygons, nonzero elevations, object poses, wall bases, and opening poses remain 3D.

## Canonical mapping

- RoomPlan stories become canonical `Level` objects. Level elevation comes from floor geometry when present, then wall bases, then object bottoms.
- RoomPlan floors become canonical `Slab` polygons. The largest floor on the CapturedRoom story also supplies the `Space` footprint because a CapturedRoom represents the captured room.
- RoomPlan walls become canonical `Wall` objects. Polygonal/segmented bottoms remain 3D polylines. When RoomPlan supplies `Surface.curve`, the importer deterministically tessellates its local x/z circular arc at no more than 5 degrees per segment and transforms those points into the canonical 3D wall centerline.
- Doors, windows, and generic openings become canonical `Opening` objects hosted on their source `parentIdentifier` wall. If RoomPlan omits the parent, the importer measures the opening against every segment of every same-story wall polyline. It selects a wall only inside the configurable host tolerance and fails explicitly when the nearest candidates are within the configurable ambiguity band (1 cm by default), rather than using canonical ID order as geometric evidence.
- RoomPlan objects become canonical `Obstacle` boxes. This keeps their 3D pose, oriented extent, category, story, provenance, and confidence available to later geometry consumers without adding routing behavior to this lane.
- RoomPlan sections are retained on the model and space attributes. A single non-unidentified section label is also used as `Space.usage`.

RoomPlan surfaces report dimensions but do not necessarily describe physical wall or slab thickness. Canonical v1 requires positive thickness for `Wall` and `Slab`. When the source depth is zero, the importer uses a small configurable surface thickness and marks that value as inferred in source attributes. It does not pretend that the inferred surface depth is a measured construction thickness.

## Source fidelity

Canonical v1 has an intentional JSON-compatible `attributes` escape hatch for source-specific metadata. The importer retains native identifiers, categories, confidence labels, stories, dimensions, complete 4x4 transforms, local polygon corners, completed edges, exact curve metadata, source attributes, and unknown source fields there. Provenance records identify the RoomPlan source and native element ID.

Curved RoomPlan surfaces therefore preserve both representations needed by downstream consumers: the untouched native curve description for source fidelity and a deterministic canonical 3D polyline tessellation for geometry consumers. A future shared need for a first-class curve primitive would still require a versioned canonical contract change rather than a RoomPlan-only type.

## Derived confidence

Source entities keep the confidence RoomPlan reports. Derived entities do not default to certainty:

- A `Level` elevation inherits the minimum confidence of the source geometry used by its selected derivation method (floor surfaces, wall bases, or the selected lowest object bottom). A `default-zero` elevation has confidence `0.0`. If level height is derived from wall tops, overall level confidence is additionally bounded by the contributing top-wall confidence.
- A `Space` confidence is the minimum of its selected source-floor confidence and its derived level confidence.
- An opening with an explicit `parentIdentifier` keeps its source confidence. When the host must be inferred geometrically, opening confidence is bounded by the inferred host wall confidence. The inference distance, in-tolerance candidates, ambiguity band, and confidence rule are retained in RoomPlan attributes.

The same derived confidence is written to the canonical entity and its provenance record, with derivation evidence recorded in provenance/attributes.

## Identity and determinism

Canonical IDs are deterministic UUIDv5-based IDs derived from the CapturedRoom identifier, source element identifier, story, and entity kind. They never depend on array position or mutable geometry.

Input arrays are sorted by stable native identifiers before import, and emitted canonical collections are sorted by canonical ID. Reordering the same CapturedRoom source therefore produces the same canonical JSON.

## Scope

This module owns RoomPlan / LiDAR ingestion only. It does not implement IFC, routing, PDF recognition, quantities, drawing generation, or electrical inference.
