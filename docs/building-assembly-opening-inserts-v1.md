# Bounded door/window opening inserts

This bounded producer has public-safe synthetic validation fixtures for issue
#356. It adds
one immutable versioned catalog containing two explicit recipes:

- `opening_insert.door.simple_single_leaf@1.0.0`: one closed flat panel leaf,
  two rectangular jamb members, and one rectangular head member.
- `opening_insert.window.fixed_glazed@1.0.0`: one flat glazing panel with four
  rectangular perimeter-frame members.

Each frame uses a butt-joined partition: the jamb pair spans the full aperture
height and head/sill members stop between the jambs. This closes the outer
corner squares while retaining face-only joints and no positive-volume frame
overlap.

The producer consumes existing canonical `Wall` and `Opening` records; it does
not inspect plans or choose a preset. A caller must provide an
`OpeningInsertAssemblyRequest` with an exact preset ID. Typed values that are
not overridden are stored as `preset_default` assumptions. Explicit changes
remain `user_override` values. All current default dimensions (face widths,
flat-pane/leaf thicknesses, clearances and reveal gaps) are editable
illustrative modeling assumptions, not field measurements, code rules, or
manufacturer dimensions. The aperture depth comes unchanged from the observed
opening depth/wall thickness; no trim or other geometry extends outside the
source void. Material remains `unknown` unless separately supplied by a later
supported workflow.

Example:

```python
from oabm.building_assemblies.opening_insert_presets import (
    DOOR_SINGLE_LEAF_PRESET_ID,
)
from oabm.building_assemblies.opening_inserts import (
    OpeningInsertAssemblyRequest,
    build_opening_insert_assembly,
)

model = build_opening_insert_assembly(
    model,
    OpeningInsertAssemblyRequest(
        assembly_id="assembly:door-01",
        opening_id="opening:door-01",
        selected_by="agent:reviewed-takeoff",
        preset_id=DOOR_SINGLE_LEAF_PRESET_ID,
    ),
)
```

The assignment snapshots source opening and parent-wall evidence, selection
provenance, typed preset/user assumptions, and digests over the exact source
facts and owned parts. Semantic part keys keep component IDs deterministic.
The insert must match the source aperture orientation and fit its exact
width/depth/height; an unsupported opening kind, shape/orientation, invalid
parameter, or non-fitting recipe is refused. One active insert assignment per
opening is allowed. Direct replacement or generation after routes exist is refused here.
The [regeneration coordinator](building-assembly-regeneration.md) supports
explicit pre-routing assignment edits. No source wall/opening record
is changed.

The small catalog has strict `to_dict`/`from_dict` encoding and decoding; extra
keys, unsupported generator tokens, wrong units/default bases, duplicate
preset IDs, or oversized preset/parameter arrays are rejected. The request
caps overrides at five before resolving values, and the producer caps the
combined opening/wall provenance snapshot at 256 records before building
components. Each recipe preflights its finite four/five-component count against
the canonical component limit before allocating physical children.

The producer currently requires its parent wall to have an aggregate wall
assembly. This is required for a GLB preview: it suppresses the opaque legacy
wall shell while displaying the wall components and explicit insert children.
The source void remains a void in the wall assembly; the separate opening
assignment owns its leaf/frame or glazing/frame components. The current GLB
uses the shared assembly-component display treatment for glazing; geometry and
identity are modeled, but optical transparency is not represented by this
slice.

The geometry is basic parameterized box bars and flat panels—not arbitrary
solid boxes passed off as product CAD. No hinge, hardware, swing direction,
egress, fire rating, structural adequacy, optical performance, manufacturer
listing, code approval, or construction authority is inferred. The GLB and
quantity consumers operate on these existing canonical 1.6 component records;
this slice changes neither canonical schema nor those consumers.

Focused public-safe proof: `tests/test_building_assembly_opening_inserts.py`.

## Explicit existing-assignment edits

The append-only producer boundaries described above remain unchanged.
[Atomic preset regeneration](../docs/building-assembly-regeneration.md) now
coordinates explicit supported assignment replacements, preserves source facts
and semantic IDs, and refuses existing routes. Its enclosed synthetic room
example supersedes the earlier one-wall proof for room composition; it does not
add joined-corner engineering or automatic route invalidation.
