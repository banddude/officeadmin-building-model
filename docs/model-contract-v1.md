# Canonical model contract v1

`oabm.model` and `contracts/oabm-model-v1.schema.json` are the shared boundary for every workstream. Importers emit these objects, routing consumes building geometry and emits these route objects, IFC maps these objects to/from IFC, and quantities/drawings consume them. Workstreams must not create competing semantic models.

## Version

The initial serialized contract is `1.0.0`.

- A document must declare `schema_version: "1.0.0"`.
- The Python v1 reader rejects other versions rather than guessing.
- Incompatible shape or meaning changes require a new version and migration notes.
- The first version has no migration because there is no predecessor.

The schema file is the portable language-neutral contract. The Python dataclasses add reference-integrity and geometric validation that JSON Schema alone cannot express cleanly.

## Coordinates and units

Canonical geometry uses a project-local Cartesian frame:

- right-handed coordinates
- `+Z` is up
- `XY` is the plan plane
- metres (`m`) for every linear value
- radians (`rad`) for every angle
- rotations are normalized quaternions
- all geometry in a document is expressed in the document's `coordinate_system.frame_id`

Optional `crs`, `origin_in_crs`, and `true_north_radians` preserve georeferencing without forcing every source to be georeferenced. Importers are responsible for transforming source coordinates into the canonical frame and recording source identity/method in provenance.

Polygon closure is implicit: do not repeat the first point as the final point. Route and wall polylines are ordered and may not contain consecutive duplicate points.

## Stable IDs

Entity IDs are document-global and must remain stable across serialization, IFC round trips, edits that do not replace the entity, and derived outputs. Never derive an ID from list position, transient memory address, or mutable geometry.

If a source has a stable native identifier, an importer may map it deterministically. If it does not, `stable_id(kind, source_key)` provides a deterministic UUIDv5-based ID. The `source_key` itself must be stable.

## Building entities

The v1 building layer includes:

- `Level`: elevation and optional story height
- `Space`: level-hosted polygon footprint and optional height/usage
- `Wall`: 3D centerline, thickness, height, and level
- `Slab`: 3D polygon footprint, thickness, and level
- `Ceiling`: 3D polygon footprint, optional thickness, and level
- `Opening`: host reference, type, oriented pose, and size

Geometry coordinates are authoritative. Level references provide semantic organization and hosting; they do not replace explicit 3D coordinates.

## Electrical entities and ports

`ElectricalEquipment` covers equipment such as panelboards, switchboards, transformers, and distribution gear. `ElectricalDevice` covers endpoint or field devices such as EVSE, receptacles, junction boxes, luminaires, and switches. The type fields are stable string tokens rather than a closed enum so new device families do not require a schema break.

`Port` is the canonical connection point. A port has an owner, pose, direction, domain, role, optional nominal diameter, and optional explicit port-to-port connectivity. Explicit connectivity must be symmetric. Routes reference their endpoint ports instead of embedding endpoint coordinates independently.

## Obstacles and route constraints

`Obstacle` represents geometry a router must account for and can carry an additional clearance. `RouteConstraint` represents keep-outs, required/preferred corridors, or other route rules, with hard/soft semantics and optional route-type scope.

Constraint and obstacle geometry uses the same canonical `Box3D`, `Polyline3D`, and `Polygon3D` primitives.

## Routes and fittings

A `Route` contains:

- a route type such as `emt`, `pvc`, `tray`, or `cable`
- start and end port IDs
- an ordered 3D centerline
- optional nominal diameter
- an ordered list of fitting IDs

The first centerline point must equal the start port position and the final point must equal the end port position. `RouteFitting` stores the selected fitting type, pose, optional nominal diameter, and optional bend angle. A fitting must belong to the route that lists it; the route's fitting list preserves order.

This representation is geometric and application-neutral. The IFC lane decides how it maps to IFC segments/fittings/ports; the router decides how the centerline and fitting decisions are produced.

## Circuits and conductors

`Circuit` links a source port to one or more load ports and can carry circuit number, voltage, poles, phase, load, and route references. `Conductor` links to a circuit and can carry role, material, size designation, count, insulation, and route references.

These are deliberately hooks, not an electrical-calculation engine. They preserve enough semantic identity for import, routing, IFC, takeoff, and drawing workstreams to agree on the same objects.

## Provenance and confidence

Every entity can carry:

- `confidence` from `0.0` to `1.0`
- zero or more `Provenance` records
- JSON-compatible `attributes` for source-specific or not-yet-canonical metadata

A provenance record identifies the source kind and source ID and may include a native element ID, 1-based page number, method, confidence, and attributes. Importers should retain source-native IDs here even when canonical IDs are opaque.

`Provenance.derivation` states whether the claim was observed, supplied by a
user, or inferred. `Provenance.scope_paths` optionally names the canonical
field paths that record qualifies, for example `("thickness_m",)` on a wall or
`("attributes.mounting",)` on an entity that carries mounting in its source
attributes. A path is relative to the record's owner. It must exist on that
owner, and an explicit scope must be nonempty, unique, and syntactically valid.
The owner and each path are validated when a `BuildingModel` is constructed.
An omitted/null scope qualifies the whole owner. A consumer comparing a
derived claim's consumed paths uses the canonical `provenance_applies_to()`
rule: a scoped record applies when a consumed path is the same as, contains,
or is contained by one of the record's paths. Unknown or empty typed scopes
are invalid; they must never cause an inferred record to be ignored and a
quantity to be promoted to observed.

This optional field is a compatible v1 extension. Legacy records without it
remain byte-identical and are treated as entity-wide. RoomPlan's former
`attributes.assumed_dimension` convention now emits `scope_paths` instead;
consumers must not infer scope from a free-form attribute. Branches that used
`assumed_field` or `assumed_dimension` for other claims should migrate to the
same field before merging. This change does not assert that their payload
fields, such as electrical box mounting, are otherwise canonical.

`attributes` is an extension escape hatch, not a place to duplicate fields already defined by the contract. If multiple workstreams need the same attribute semantically, promote it into the canonical contract.

## Serialization invariants

Canonical JSON is UTF-8 JSON produced by `BuildingModel.to_dict()` / `to_json()` and validates against `contracts/oabm-model-v1.schema.json`.

The Python reader is strict about unknown fields and validates:

- schema version
- globally unique entity IDs
- level, space, host, port, route, fitting, circuit, and conductor references
- symmetric explicit port connectivity
- route endpoint/port alignment
- route/fitting ownership and order
- positive physical dimensions and finite numbers
- canonical units and coordinate conventions

This strictness is intentional: a workstream should fail at its handoff boundary instead of silently inventing a divergent interpretation.

## Public fixtures

`fixtures/model/v1/minimal-room.json` is the smallest building example. `fixtures/model/v1/garage-route.json` exercises the v1 building, electrical, port, obstacle/constraint, route/fitting, circuit/conductor, provenance, and confidence fields. The garage fixture is contract coverage only; the later synthetic-garage convergence gate owns routing/IFC acceptance behavior.
