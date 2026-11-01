# Suspended-ceiling producer v1 (issue #356 slice)

This is the first bounded physical ceiling producer in the existing OA
building-model package. It consumes a reviewed canonical `Ceiling` plus an
already-present canonical `Slab` and emits one atomic `suspended_ceiling`
assignment and its physical assembly components. It does not add a second
deck, rewrite source geometry, change the canonical schema, or implement a
separate plugin.

The initial version is intentionally illustrative and does not represent an
approved construction design, manufacturer-exact assembly, or engineering
decision.

## Finite preset and typed inputs

The immutable catalog is
`catalog:oabm-suspended-ceiling@1.0.0`; its single executable preset is
`ceiling.tbar_609_6mm_module_illustrative@1.0.0`, whose default module spacing is
0.6096 m (nominal 24 in). Its typed length parameters include
module spacing, simple T-profile dimensions, tile thickness, tile-edge
clearance, hanger-wire diameter, hanger spacing, hanger end offset and the
conditional tile-top placement offset. Each generated component carries
stable assembly/part-key identity and the resolved catalog/preset provenance.
Source observations, preset defaults and caller overrides remain distinct.
The built-in 609.6 mm generator/preset identities and any referenced built-in
source IDs are reserved for their exact canonical content. To change an input,
use a typed override, or provide a caller-owned catalog/preset ID and revision
and distinct IDs for new caller sources (with the generic module-grid generator
kind if the module default differs).

Published references inform selected defaults, but they do not establish the
modeled section or every installed dimension:

* [Armstrong Prelude XL data sheet](https://www.armstrongceilings.com/content/dam/armstrongceilings/commercial/north-america/data-sheets/prelude-xl-data-sheet.pdf): published overall envelopes inform simplified T-profile width/depth only. It is not a complete section drawing; the generated T profile is explicitly **not manufacturer-exact**.
* [Armstrong ULTIMA item 1910](https://www.armstrongceilings.com/commercial/en/commercial-ceilings-walls/ultima-lay-in-and-tegular-ceiling-tiles/item/1910.html): published nominal module and thickness inform defaults, not measured cut size or installed edge/bearing geometry.
* [QSW ceiling hanger wire submittal](https://qswmfg.com/wp-content/uploads/2018/05/Ceiling-Hanger-Wire-Submittal-Form.pdf): its 0.105 in diameter belongs to that named product only. It is not a universal gauge conversion or a capacity/anchor approval.

The default 3.175 mm per-edge tile clearance is a separate, visible
`illustrative_modeling_assumption`, not an attribution to those products. It
creates 8.73125 mm positive plan overlap on the default T flange. The tile
underside is placed on the flange top plane; plan overlap plus coplanar contact
is not a bearing-capacity result.

## Ceiling datum and selected placement

The canonical IFC/model convention uses the source `Ceiling.footprint` as a
top-face datum; an optional source `Ceiling.thickness_m` extends downward. The
producer preserves this value and emits it as an observed resolved parameter
with source-field provenance when present.

The immutable preset encodes both placement cases:

1. **Observed thickness:** tile-top offset defaults to 0 m, aligning the tile
   top with the source Ceiling top face. If tile thickness plus T-flange
   thickness cannot fit within the observed source thickness, generation
   refuses; it does not relabel or adjust the source.
2. **Thickness absent:** the preset default is the resolved tile thickness
   plus resolved flange thickness, so the T-flange underside aligns with the
   observed footprint plane. The snapshot records the formula and source
   dependency. A caller may override the signed tile-top offset, and that
   choice is separately recorded and checked against deck bounds.

These are explicit placement defaults, not facts read from drawings. Missing
Ceiling or deck geometry evidence is not substituted with preset geometry.

## Geometry and boundary policy

`build_suspended_ceiling_assembly(model, request)` requires an explicit
assembly ID, Ceiling ID, existing deck Slab ID, selector, grid origin, grid
axis angle, and edge policy `cut_tiles_without_perimeter_angle_v1`. The
selected grid must be horizontal and parallel to a strict four-corner
rectangular Ceiling axis. Clockwise and rotated horizontal rectangles are
supported. Skew, nonrectangular, sloped, mismatched-axis, and host-outside-deck
inputs fail closed.

The bounded recipe emits:

* continuous main T runners on module grid lines;
* cross T runners at interior grid rows, ending at main-runner flange side
  faces; cut perimeter-bay cross runners continue to the host edge so interior
  tile seams in those bays still meet a flange;
* rectangular tile panels cut to the host rectangle and the resolved tile
  edge clearance; and
* straight finite vertical wire components from the exact underside face of
  the existing Slab to a named main-runner web-top surface, with typed hanger
  endpoint references.

Every interior tile edge has a positive plan overlap with its corresponding T
flange, and its underside is coplanar with the flange top. Only tile edges on
the four actual outer host-footprint sides are intentionally left without a
modeled runner/perimeter support. The edge-policy/assignment attributes name
those sides. Perimeter cross-runner ends are free at the host boundary: no
wall support or connector is represented.

At a main/cross runner joint, the cross-runner flange end has a positive-area
surface contact with the main-runner flange side and the constant-profile
solids do not positively overlap. The simple T extrusion leaves an
upper-web-side separation; connector clips/tabs, splices, complete tee end
geometry and mechanical connection are not represented. The assignment
discloses that simplification and does not claim complete joint behavior.

## Fail-closed limits and downstream use

The producer refuses a missing or mismatched deck, incomplete source geometry
evidence, unknown preset/override, invalid geometry/dimensions, too-thin
observed Ceiling, unsupported boundary policy, direct append-API assignment replacement,
and models with existing electrical routes. Explicit preset edits use the
[regeneration coordinator](building-assembly-regeneration.md) before routing;
automatic dependent-route regeneration remains unsupported. Component-count and model-wide GLB-vertex costs are checked
before runner-span or component allocation.

The aggregate Ceiling remains host metadata and the original Slab remains the
only deck identity. No duplicate `deck_panel` or billable Ceiling shell is
generated. The existing GLB and quantity consumers read the exact same child
component IDs; no custom ceiling-only export or takeoff calculation is used.
Material identity stays unknown. Anchor/hardware selection, substrate design,
hanger capacity, fire/listing review, code approval and structural engineering
are not performed.

Implementation modules are `oabm.building_assemblies.suspended_ceiling_catalog`
and `oabm.building_assemblies.suspended_ceiling`. The request, resolver, builder
and default catalog are also exported from `oabm.building_assemblies`.
The combined synthetic helper
`oabm.building_assemblies.combined_fixtures.synthetic_wall_inserts_ceiling_assembly()`
composes a wall, two inserts and a ceiling region in one model. It is not an
enclosed-room or whole-building proof.

For these populated 1.6 assemblies, the existing selected deck's GLB body uses
the same top-face/downward-thickness convention as the canonical model, so
rendered hanger endpoints contact the actual rendered underside. A selected
overhead deck does not move a room's floor plate up to roof height. Earlier
schema and empty-upgrade previews retain their historical behavior.

## Focused known-answer test

The synthetic 4 m x 3 m case contains 6 main runners, 28 cross runners, 35
tiles and 18 wires (87 children). Tests validate datum/bounds, every interior
tile seam's flange coverage, real flange-side contacts, absence of
cross/main positive-volume overlap, exact hanger endpoints/lengths, repeatable
JSON and GLB bytes, GLB node identity, and quantity counts/geometry totals.
The test file is
`tests/test_building_assembly_suspended_ceiling.py`; the entire repository
suite is intentionally left to the integration owner.

## Explicit existing-assignment edits

The append-only producer boundaries described above remain unchanged.
[Atomic preset regeneration](../docs/building-assembly-regeneration.md) now
coordinates explicit supported assignment replacements, preserves source facts
and semantic IDs, and refuses existing routes. Its enclosed synthetic room
example supersedes the earlier one-wall proof for room composition; it does not
add joined-corner engineering or automatic route invalidation.
