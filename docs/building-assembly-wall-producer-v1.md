# Deterministic wood-wall assembly producer

This is a bounded implementation slice for issue #356. It consumes already
reviewed canonical `Wall` and `Opening` records; it does not inspect plans,
call an AI/provider, or create a parallel building model. It uses the canonical
1.6 building-assembly records. GLB previews and component quantities consume
these records; unsupported consumers refuse populated assemblies.

## API

```python
from oabm.building_assemblies import (
    OpeningFramingSpecification,
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
        opening_framing_specs=(OpeningFramingSpecification(
            opening_id="opening:door-01",
            header_depth_m=0.1397,
            header_ply_thickness_m=0.0381,
            header_ply_count=2,
            header_spacer_thickness_m=0.0127,
            bearing_m=0.0381,
            king_count_per_side=1,
            jack_count_per_side=1,
        ),),
    ),
)
```

The returned `BuildingModel` contains a validated `BuildingAssembly` and its
owned `AssemblyComponent` records. Component IDs derive from the assembly ID
plus semantic part keys. The source model is immutable; invalid inputs and
candidate-model validation failures leave it unchanged. The append-only API refuses an existing assignment or any pre-existing route.
Use [explicit regeneration](building-assembly-regeneration.md) to edit a supported
existing assignment before routing; routed output requires its upstream checkpoint.

`DEFAULT_PRESET_CATALOG` is a finite, immutable catalog at
`catalog:oabm-building-assemblies@2.0.0`; the current recipe is
`wall.wood_rect_stock@2.0.0` with generator `oabm-wall-producer/2.0.0`.
Header section depth, ply thickness/count/spacer, end bearing, and king/jack
counts are required in one explicit `OpeningFramingSpecification` for every
source door or window. The producer refuses missing or extra specifications,
invalid dimensions/build-up, unsupported support patterns, inadequate space,
and adjacent opening-frame envelopes that overlap. These values are user
selected geometry inputs captured as typed `user_override` parameters in the
assembly digest; the producer does not infer a header from span.

Canonical `Opening.pose` and `Opening.size` are treated as the exact rough-
opening void between the framing supports, not as nominal door-leaf or window-
unit dimensions. The producer never shrinks or moves this source void to fit
the framing; king/jack faces are positioned directly outside its unchanged
horizontal limits. Door/window inserts are a separate later selection.

Catalog and preset identity `1.0.0` remain reserved with their original
single-jamb recipe and values so old catalog snapshots and saved canonical
models remain readable. That historical recipe can still be decoded for
inspection, but `build_wall_assembly` refuses to generate new assemblies from
it. Existing simplified models are not migrated or relabeled as conventional
framing; regenerate only from a new source model and an explicitly reviewed
v2 selection after any needed downstream rebuild.

## Recipe and geometry

The current finite recipe uses a 38.1 mm wall-axis width and 88.9 mm framing
depth based on NIST minimum-dressed dry 2×4 dimensions; these are parametric
model values, not field-measured lumber. The 12.7 mm drywall dimension is the
published USG 1/2-inch panel thickness and is likewise an editable modeling
assumption. A 406.4 mm stud spacing, member offsets, and plate arrangement
remain illustrative assumptions rather than plan facts or code rules. Material
identity remains `unknown`.

For each explicitly selected conventional opening pattern, full-height king
studs use canonical `component_kind="stud"` and continue between the plate
faces. Jack/trimmer studs are separate `stud` components positioned directly
under the header ends; their top faces meet the header undersides. Each header
ply is a separate canonical `header` component with its explicit section
depth and through-wall ply thickness. A nonzero, explicitly selected
`header_spacer_thickness_m` is modeled as separate `header` components between
adjacent plies; zero explicitly selects no spacer. Any positive spacer must
exceed the generator's geometric tolerance and is modeled as a physical
component; sub-tolerance positive dimensions are rejected, never silently
omitted. Spacer material remains unknown. Header length is the unchanged
source opening width plus the two selected bearing lengths. To avoid
unsupported gaps or partial support, the bearing must exactly span the
selected jack pack; each header end meets that pack and the adjacent king
boundary. Total ply thickness plus explicit spacer thickness must fit inside
the selected wall framing depth. Window sill and door/header cripple locations
are derived only at the selected regular stud-grid stations. At each station,
the recipe subtracts the actual vertical intervals occupied by king/jack
supports, so clear grid-aligned cripples above a jack/header can remain. A
narrow opening can legitimately produce no cripple at any clear grid station;
the recipe does not invent a center/off-grid member or claim that the resulting
framing has structural capacity. Door framing omits the bottom-plate segment
across the exact source door opening. Ordinary field studs are not emitted
where their actual stock volume would overlap king/jack supports.

`Provenance.attributes["framing_role"]` distinguishes `king`, `jack`,
`ordinary`, `cripple`, `header_ply`, `header_spacer`, and `rough_sill`; stable
semantic part keys carry opening IDs, sides, member slots and ply slots. The canonical model has
no competing subtype schema, and opening source dimensions/host facts remain
unchanged. Header and support geometry is a deterministic rendering of the
selected inputs—not a load calculation, structural member recommendation,
construction design, or code approval.

The implementation pattern is bounded to conventional king/jack/header
geometry when that pattern is explicitly selected. It does not imply that
jacks are universal: the HUD Residential Structural Design Guide (2000),
§5.5.1 and Figure 5.5 (printed pp. 5-32–5-33), illustrates one conventional
header/jack/king arrangement; §5.5.4 (printed p. 5-36) discusses header span
and bearing. Its alternative details and APA's M400 guide describe other
layouts, so a different framing system must be explicitly selected. These
are geometry references only, not current local-code certification; no
dimensions from them are used to size this preset. See the [HUD 2000
guide](https://www.huduser.gov/Portal/Publications/pdf/residential-old2000.pdf)
and [APA M400 guide](https://www.apawood.org/guides-tools-training/technical-document-library/construction-guides/advanced-framing/).

The separately selected
[opening-insert producer](building-assembly-opening-inserts-v1.md) supplies
door leaves/frames and window frames/glazing without changing the wall
producer or its exact source voids. The framing recipe also emits rectangular
drywall panels partitioned around those voids on both wall faces. It does not
assume purchased-sheet dimensions, seams or cut waste; the quantity consumer
reports modeled panel area, not purchased sheet counts.

The frame derives from the ordered two-point horizontal wall centerline; its
left normal is `(-axis_y, axis_x)`. Generated boxes/panels are checked against
the unchanged wall envelope and openings by the canonical model validator.
Framing depth plus both drywall layers must equal the observed wall thickness
within `1e-9 m`; no source thickness is rewritten. Input and output digests
cover source observations, catalog/preset snapshots, explicit opening
specifications, evidence, and the full sorted owned component records.

## Explicitly unsupported

- Cold-formed metal-stud/track recipes; nonbearing, advanced-framing, or other
  opening patterns unless separately implemented and explicitly selected;
  shared-member/corner solutions; curved or joined walls; nonrectangular
  openings; hidden trades; structural adequacy; load/header design; fire
  rating; and code approval. Typed non-framed host construction and explicit
  metal/steel stud facts in the limited `stud_material`, `stud_system`, or
  `framing_system` metadata fields are refused. Within those fields only,
  `wood ... with steel/metal ties, tie connectors, nails, nail plates, or
  fasteners` is recognized as an explicit accessory phrase; the classifier
  removes only that phrase before checking for remaining metal/steel/cold-
  formed system terms. Ambiguous or mixed structural systems therefore refuse
  instead of being silently classified as wood. Fastener/tie fields outside
  the three framing fields and unrelated prose are not framing facts. If host
  construction or relevant material evidence is unknown, selection of this
  wood preset is recorded as an explicit preset assumption, not as an observed
  wood fact.
- Direct replacement through this append-only producer, or generation in a
  model that already contains routes. Explicit preset edits use the atomic
  [regeneration coordinator](building-assembly-regeneration.md); automatic
  route invalidation remains unsupported.
- Routing beyond the bounded opt-in slice in
  [3D electrical routing](routing.md): populated assemblies require an explicit
  `AllowedRouteDomain`, supported physical-component blockers, and complete
  assignment coverage for aggregate walls and ceilings. A derived domain is
  limited to the supported rectangular ceiling plenum above generated runners
  and tiles and below an existing Slab. This creates no holes or penetrations
  and is not installation engineering or code approval. IFC, drawings,
  revision-diff, confidence-review and review bundles still refuse populated
  assemblies rather than silently dropping parts. GLB and quantities support
  physical components with aggregate-host suppression; GLB has a model-wide
  assembly vertex budget and refuses coordinate ranges that lose geometry at
  float32 precision.

The synthetic known-answer helper is
`oabm.building_assemblies.fixtures.synthetic_two_opening_wall_assembly()`: one
4.8 m × 2.4384 m straight wall with a door and an elevated window. Its explicit
header build-up/bearing/support values are synthetic modeling choices, not
engineered sizing recommendations. It is public-safe and contains no
customer/project data.

## Explicit existing-assignment edits

The append-only producer boundaries described above remain unchanged.
[Atomic preset regeneration](../docs/building-assembly-regeneration.md) now
coordinates explicit supported assignment replacements, preserves source facts
and semantic IDs, and refuses existing routes. Its enclosed synthetic room
example supersedes the earlier one-wall proof for room composition; it does not
add joined-corner engineering or automatic route invalidation.
