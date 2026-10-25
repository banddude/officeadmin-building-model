# First deterministic wall assembly producer

This is a bounded implementation slice for issue #356. It consumes already
reviewed canonical `Wall` and `Opening` records and does not inspect plans,
call an AI/provider, or create a parallel building model. It currently uses the
explicit 1.6 building-assembly records. GLB previews and component quantities
consume these records; unsupported consumers refuse populated assemblies.

## API

```python
from oabm.building_assemblies import (
    WallAssemblyRequest,
    build_wall_assembly,
)

result = build_wall_assembly(
    source_model,
    WallAssemblyRequest(
        assembly_id="assembly:wall-01",
        wall_id="wall:01",
        selected_by="agent:takeoff",
        preset_id="wall.wood_rect_stock",
    ),
)
```

The returned `BuildingModel` contains the validated `BuildingAssembly` and
owned `AssemblyComponent` records. Component IDs are derived from the
assembly ID plus a semantic part key. The source model is immutable; any
invalid input or candidate-model validation error leaves it unchanged. An
existing assignment for the same wall or any pre-existing route requires a
fresh dependent rebuild and is refused; this API never replaces geometry or
claims to invalidate routed systems.

`DEFAULT_PRESET_CATALOG` is an immutable, strictly decoded/encodable catalog
(`catalog:oabm-building-assemblies@1.0.0`). Its only active recipe is
`wall.wood_rect_stock@1.0.0`, a finite rectangular-timber recipe. Unknown
fields, duplicate identities, unsupported presets, wrong parameter kinds or
units, out-of-range overrides, and missing observed host/opening evidence fail
closed. A `ParameterOverride` is caller-authored and remains
`user_override`; every unoverridden value is snapshotted as `preset_default`
with catalog-bound assumption evidence.

## Recipe and geometry

The recipe defaults to a 38.1 mm wall-axis width and 88.9 mm framing depth
based on the NIST minimum-dressed dry 2×4 dimensions; these are parametric
model values, not field-measured lumber. The 12.7 mm drywall dimension is the
published USG 1/2-inch panel thickness and is likewise an editable modeling
assumption. A 406.4 mm stud spacing, member offsets, plate arrangement, single
header/sill dimensions, and single jambs are explicitly declared illustrative
assumptions; they are not plan facts, researched manufacturer rules, a
structural design, or code guidance. Material identity remains `unknown`.

The selected recipe emits repeated rectangular studs, explicit bottom/top
plate courses, single rectangular jamb members and headers, window sills,
cripple segments outside rough-opening bands, and rectangular drywall panels
partitioned around door/window voids on both wall faces. Panels have no
assumed purchased-sheet dimensions, seams, or cut waste. The quantity consumer
reports modeled panel area, not purchased sheet counts.
Door/window openings remain exact source voids; this producer emits no door
leaf/frame, window frame, or glazing inserts. Those require separately chosen
insert recipes in a later slice.

The frame is derived from the ordered two-point horizontal wall centerline;
the left normal is `(-axis_y, axis_x)`. Every generated box/panel is checked
against the unchanged wall envelope and openings by the canonical model
validator. Framing depth plus both drywall layers must equal the observed wall
thickness within `1e-9 m`; no source thickness is rewritten. Stable IDs name
roles/layout slots and opening IDs, not mutable dimensions or list positions.
Input and output digests are computed from the canonical source observations,
resolved parameter/evidence snapshot, and full sorted owned component records.

## Explicitly unsupported

- Cold-formed metal-stud/track recipes, opening insert recipes, curved or
  joined walls, nonrectangular openings, hidden trades, structural adequacy,
  load/header design, fire rating, and code approval.
- Regeneration/replacement of an existing wall assignment, or generation in a
  model that already contains routes. Dependent rebuild and route invalidation
  are not implemented.
- Assembly-aware routing, IFC, drawings, revision-diff, confidence-review and
  review bundles. These entrypoints refuse populated assemblies rather than
  silently dropping parts. GLB and quantities do support physical components
  with aggregate-host suppression; GLB has a model-wide assembly vertex budget
  and refuses coordinate ranges that lose geometry at float32 precision.

The synthetic known-answer helper is
`oabm.building_assemblies.fixtures.synthetic_two_opening_wall_assembly()`: one
4.8 m × 2.4384 m straight wall with a door and an elevated window. It is
public-safe and contains no customer/project data.
