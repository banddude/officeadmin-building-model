# Physical assembly consumers: bounded opt-in support

The canonical schema-1.6 physical children now share one neutral mesh adapter,
`oabm.assembly_mesh.assembly_component_mesh`. GLB, assembly cut drawings and the
explicit IFC tessellation use the same Box3D, ProfileExtrusion, PanelGeometry and
WireSweep dimensions/poses. No canonical schema or producer output changes.

## Executable synthetic demonstration

With the existing IFC development dependencies installed:

```sh
PYTHONPATH=src python examples/assembly_consumers.py /tmp/assembly-proof-new
```

The destination must not exist. This writes one canonical synthetic model,
GLB, IFC, plan/section SVG, a coverage/limitations report and exact output hashes.
It does not launch Blender, Bonsai, a browser or a raster renderer. A generated
artifact is not visually accepted until independently inspected.

## Drawing APIs

```python
view = generate_plan(model, PlanSpec(id='framing-plan', level_id=level_id),
                     assembly_geometry='cut')
section = generate_section(model, section_spec, assembly_geometry='cut')
```

Both existing ordinary APIs retain their default fail-closed guard. Explicit
`'cut'` draws the true plane intersection of each physical child mesh, preserves
component and parent source IDs, and suppresses aggregate host linework and
associated host dimensions. Ordinary non-host content retains existing view
semantics. Only selected levels and architecture visibility participate. Explicit
bounds clip cuts; automatic bounds expand to contain the physical cut outlines.
Source geometry, quantities, poses and assembly digests remain unchanged.

Assembly cut lines are distinguished from projected content. Adjacent collinear
triangle fragments merge; internal coplanar triangulation diagonals are removed.
The metadata states that assembly parts beyond the cut plane are not projected.
These APIs do not yet supply assembly hidden-line elevations, hatch/material
patterns, automatic dimensioning, structural approval or manufacturer detail.
`generate_elevation` and legacy `generate_schedules` retain their populated-assembly
guards. Drawing-set and delivery support require the explicit options documented
below; default requests do not silently enable assembly cuts.

## IFC APIs

```python
native = to_ifc(model, assembly_geometry='tessellated')
restored = from_ifc(native)
assert restored.to_dict() == model.to_dict()
# round_trip(model, assembly_geometry='tessellated') is also supported.
```

Default export/round-trip retain their guards. Explicit tessellation exports a
stable-ID IfcElementAssembly per parent and stable-ID physical child proxies,
with canonical metadata, explicit aggregate membership, world-space metre meshes
and native Body tessellation. Child display labels use the canonical name or
semantic part key; that derived label does not invent a canonical name.

This is a viewable/selectable physical IFC representation, not parametric
Bonsai authoring support. Reimport checks native classes, labels, parent/child
membership, triangulation, coordinates and placements against the complete
canonical model. Incompatible native edits refuse rather than retaining stale
canonical geometry or catalog/manufacturer claims. Update canonical facts and
regenerate. Names are likewise source-controlled for this bounded export.
Generic third-party IFC intake remains the separate bounded native importer.

WireSweep tessellation uses straight round segments and does not claim curved
manufactured joints or guaranteed watertight unions. Surface geometry expresses
supplied design dimensions only; attachment, structural capacity, tolerances and
manufacturer fit remain governed by their existing explicit unknowns.

## Connected requests and composed model-view sheets

`generate_drawing_set(..., assembly_geometry='cut', include_schedules=False)` now
supports plan and section batches and indexes both parent and physical child
provenance. Populated assembly elevations or legacy schedules still refuse;
the caller must request only supported consumers. Default requests are unchanged.

The existing model-delivery CLI accepts `model-delivery-v2` options: the original
v1 keys plus explicit `"assembly_geometry": "cut"`. It uses the same canonical
model for the plan DrawingSet and GLB, retains exact input bytes, and records cut
component IDs and limitations in its manifest. V1 remains unchanged and guarded.
This does not silently enable IFC in model delivery or imply curved fittings.

The production coordinator also accepts `production-sheet-set-v2`: all v1 keys
plus `model_views`. In v2, explicitly unrequested cover/site/schedules/details
may be `null`; no fake model, report or empty detail recipe is necessary. Every
ordered job sheet still needs exactly one content owner. Existing v1 complete
packets keep their behavior and use the same coordinator and shared renderer.

A `model_views` row has exact keys:

```json
{
  "sheet_id": "A101",
  "type": "plan",
  "options": {"level_id": "level:proof", "scale": 20},
  "origin_mm": [20, 30],
  "assembly_geometry": "cut",
  "annotations": "none"
}
```

`options` uses existing PlanSpec fields (excluding IDs/visibility) or SectionSpec
fields for `"type":"section"`; `scale` is explicit, and the shared titleblock
must agree. Bounds, when supplied, use min_x/min_y/max_x/max_y in view metres.
The supported body is an architectural cut with electrical/routes/spaces,
symbols, labels and dimensions disabled explicitly. It does not replace their
specialized drafting tools. Unsupported elevations or automatic annotations
refuse before an output attempt is reserved.

`render_scaled_view_body(view, *, drawing_bounds_mm, origin_mm)` is the reusable
adapter underneath: existing model metres become paper millimetres at the exact
view scale, never fitted or shrunk. It supports solid vector outlines and plain
text with real-font bounds; unsupported symbols, dimensions or patterned lines
refuse instead of changing their meaning. Its source map retains primitive IDs,
canonical source IDs, scale, paper origin and view qualifications. Final shared
ink bounds validation remains mandatory.

```sh
PYTHONPATH=src python examples/assembly_production_sheets.py /tmp/physical-packet-new --pdf
# Or rerun the prepared editable input with the existing command:
oabm-production-sheets /tmp/physical-packet-new/inputs/production.json /tmp/physical-packet-another
```

This demonstration produces a two-sheet shared-titleblock plan/section packet
through the real coordinator. A requested cover can coexist with these views;
its final index is reconciled with their IDs. Ordinary non-assembly component
bodies retain their own support contracts. Requesting an unsupported legacy
detail on a populated assembly model still refuses rather than dropping parts.

### Native source-closure verification

Physical assembly reimport is deliberately source-controlled. In addition to
parent/child mesh checks, it re-authors the expected IFC geometry through the
existing `to_ifc` implementation and compares ID-free representation/placement
graphs for assembly parents, children, source hosts, opening hosts, decks and
hanger attachment targets. Coordinates and transforms use absolute 1e-8 numeric
tolerance; topology and relationships remain exact. Ceiling axes, opening voids
and deck solids cannot be edited natively and then silently restored from the
canonical shadow. The exported metre length unit, void/fill links, containment,
aggregation and nesting are checked. Styles and caller groups remain presentation
metadata outside canonical geometry. Update canonical facts and regenerate for
unsupported native edits; this remains separate from ordinary legacy IFC edit
support and the bounded third-party native importer.

### Checked routes and physical cut drawings

Plan and section cuts project ordinary entities from the original validated
canonical model. The drawing adapter no longer constructs an intermediate model
with its assemblies removed: doing so invalidated valid assembly-bound route
receipts and prevented a successfully routed model from reaching plan delivery.
The source model and route receipts remain byte-for-byte unchanged. Legacy
requests without explicit cut mode still refuse populated assemblies, and stale
assembly/route combinations still fail canonical validation.

Run `python examples/routed_assembly_delivery.py FRESH_ROOT` for a connected
synthetic wall/opening/ceiling proof: generated studs, opening inserts, T-bar,
tiles, hangers and deck feed the plenum router; the resulting same model feeds
cut-plan SVGs, quantity counts and a GLB through the existing delivery command.
The endpoints are explicit synthetic plenum connection points. This does not
create wall penetrations, select manufacturer fittings or prove a whole building.
The retained bend report remains a separate check and can require review.

## Explicit existing-assignment edits

The append-only producer boundaries described above remain unchanged.
[Atomic preset regeneration](../docs/building-assembly-regeneration.md) now
coordinates explicit supported assignment replacements, preserves source facts
and semantic IDs, and refuses existing routes. Its enclosed synthetic room
example supersedes the earlier one-wall proof for room composition; it does not
add joined-corner engineering or automatic route invalidation.
