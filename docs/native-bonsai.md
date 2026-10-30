# Opt-in native Bonsai drawing export

This adapter launches an **explicitly supplied, already installed** Blender/Bonsai
runtime. It is separate from the ordinary Python SVG drawing adapters and is not
enabled by existing production commands. It writes model-derived IFC and native
Bonsai PLAN_VIEW / REFLECTED_PLAN_VIEW / ELEVATION_VIEW SVGs. It does not draw a separate plan from
hand-maintained geometry, change the canonical contract, or imply engineering or
permit acceptance.

## Operator prerequisites and trust boundary

Install a compatible official Blender and Bonsai distribution yourself. The
adapter does not download, install, upgrade, discover or execute a remote runtime.
Supply the Blender executable and a local Python package directory containing
Bonsai and its dependencies. These paths are executable-code trust decisions:
only use installations you trust. Arbitrary scripts, `.blend` inputs, caller SVG
resources, remote IFC/resources, and custom native expressions are not request
inputs. The fixed worker uses a factory-startup/offline runtime, not a user's
existing desktop session or preferences.

The initially supported runtime is exactly Blender 5.2.3 LTS / Bonsai 0.8.5 /
IfcOpenShell 0.8.5 with the matching bundled Python ABI. Other versions are
explicitly refused by this bounded first implementation; a successful probe is not proof that a specific model
can be exported. Keep real job inputs and outputs outside this public checkout.
No credentials, network permissions, OfficeAdmin services or user laptop access
are required by this adapter.

```sh
# Adjust these two paths to your existing trusted installation.
export BLENDER=/opt/blender/blender
export BONSAI_PYTHON=/opt/bonsai-python

PYTHONPATH=src python -m oabm.native_bonsai --probe \
  --blender "$BLENDER" --bonsai-python "$BONSAI_PYTHON" \
  --output /tmp/native-runtime-probe-new

PYTHONPATH=src python -m oabm.native_bonsai \
  --model examples/synthetic-native-bonsai-model.json \
  --request examples/synthetic-native-bonsai-request.json \
  --blender "$BLENDER" --bonsai-python "$BONSAI_PYTHON" \
  --output /tmp/native-drawing-proof-new
```

Output destinations must be fresh. Do not place output over an input or reuse a
previous result directory. The supplied example is entirely synthetic: a tiny
room, three unknown-size point devices, and one explicitly sized generic fan.
It is not a recommended electrical layout or selected product schedule.

## Local-coordinate scope (fail closed)

Native v1 supports only local models within **+/-100 m on every axis**.
Canonical spatial vectors/elevations, native Cartesian points, accumulated
placements and all generated world-space Body vertices are checked, including
excluded/off-view objects. Camera XY extents and the full conservative 10 m
viewing-depth envelope must also fit. CRS/origin_in_crs metadata is unsupported;
do not erase georeferencing to imply equivalent output. Geometry that cannot be
inspected fails rather than being skipped. Annotation XY precision is checked
within 0.01 mm; the receipt records the local-coordinate boundary.

The pinned Bonsai 0.8.5 importer automatically rebases geometry when a coordinate
exceeds its default 1000 m distance limit on any axis. This adapter does not yet
transform cameras/annotations through a false origin. The conservative 100 m
bound keeps all geometry tenfold inside that trigger and bounds Blender float32
placement quantization; any unexpected native offset also fails explicitly.
This is a bounded correctness restriction, not georeferenced export support.

## Appearance boundary: canonical geometry only

This tool starts from canonical JSON and the existing `to_ifc` adapter. It does
**not** consume an existing IFC, reproduce arbitrary IFC Bodies, or reconstruct
external appearance meshes. Canonical obstacle boxes remain conservative
canonical envelopes; they do not become detailed furniture or manufacturer CAD.
A separate, already verified IFC may contain richer derived appearance geometry
that this canonical-only command cannot reproduce. Do not advertise equivalent
appearance fidelity merely because canonical roundtrip succeeds.

If canonical metadata contains `visual_asset_reference`, this bounded consumer
refuses the export rather than ignoring that recognized appearance request or
fetching its resource. This is a consumer guard, not a new canonical field or
parallel semantic model. Raw IFC/resource/script request keys also remain
unsupported. Other descriptive metadata is preserved but is never an instruction
to fetch or materialize visual assets. The receipt explicitly states the
canonical-only appearance boundary. Keep richer-IFC workflows separately
validated; do not remove appearance metadata simply to imply equivalent output.

## Request boundary

The version is `native-bonsai-v1`. Each requested view explicitly supplies its
safe file ID, native target view, canonical level ID, XY center in metres,
absolute cut elevation in metres, width/height in metres, integer scale
denominator, excluded canonical IDs, and annotation list. The example is the
complete executable request. Unknown keys and unsupported view types fail;
there is no automatic fitting, clipping repair or inferred drawing scale. The
bounded native camera depth is 10 m with a 0.002 m near clip; these are recorded
presentation settings, not surveyed building extents. `level_id` identifies the
native camera reference, **not an exclusive storey filter**. Objects from other
levels within the camera volume can be selected; supply explicit canonical
`exclude_ids` when the required view needs that exclusion. The receipt records
native selections instead of claiming pixel visibility.

```json
{
  "id": "plan",
  "target_view": "PLAN_VIEW",
  "level_id": "level:ground",
  "center_m": [2, 1.5],
  "cut_height_m": 1.5,
  "width_m": 6,
  "height_m": 5,
  "scale_denominator": 48,
  "exclude_ids": [],
  "annotations": [
    {"source_id": "device:R1", "kind": "receptacle", "text": "R1",
     "label_offset_mm": [3, 1]}
  ]
}
```

Canonical coordinates remain authoritative. Annotation anchors come from the
referenced device/equipment pose; the request cannot supply a competing XY
position. `label_offset_mm` is paper presentation only, not a moved product or
physical installation dimension. `text` provides a linked text label for a
posed device/equipment, including one with an explicit size. The `luminaire`,
`receptacle`, and `switch` schematic glyph kinds are restricted to matching
unknown-size devices. Their paper glyph dimensions are not physical Body sizes.
Labels are limited to 1–80 printable ASCII characters; native template braces
and backticks are refused. Unsupported text is rejected, never silently rewritten.
No guessed physical box is added. Grouped banks, automatic dimensions, sections, titleblocks and engineering symbols beyond this
bounded vocabulary are not supported by this first public adapter.

Missing canonical dimensions remain missing. Explicit
`acknowledged_body_omissions` names the exact canonical IDs whose adapter Body
omissions the operator accepts for this review export. An unexpected omission
must fail instead of disappearing silently; acknowledgment is not fabrication
of geometry or engineering approval. Existing IFC adapter restrictions still
apply. This command does not repurpose electrical objects as pipe, duct or gas
systems and does not bypass missing typed MEP semantics.

## Evidence, scale and failure handling

Model and request bytes are captured once into immutable input snapshots; all
subsequent work and hashes refer to those bytes, not a later edit to the original
file. A completed export has a receipt identifying the consumed hashes, native
runtime, units, view scale/dimensions, canonical-to-IFC identity and round-trip
checks, annotations and omission/visibility information. Review the receipt and
native SVGs together. A runtime or unsupported-content failure is not a successful
drawing; diagnostics remain explicit and no success receipt is issued.

Scale is configured on the native Bonsai drawing before linework and annotations
are generated, including the camera/render aspect synchronization. It is not
achieved by enlarging the completed SVG: paper symbols retain their proper size.
Native pixel-aspect quantization can cause a small difference between requested
and actual SVG paper extents; preserve the actual recorded dimensions. RCP uses
the native reflected view, not a flipped PLAN SVG.

Unknown elevations, geometry or design intent are not resolved by drawing them.
An annotation's camera-plane Z is a presentation plane, not an equipment mounting
height. Objects outside a requested view or explicitly excluded remain in the
model; visibility reporting must not be interpreted as deletion or a quantity
change. Do not infer circuits, homeruns, panel sizing, connectivity, photometry,
code compliance, survey accuracy or construction readiness from a native export.

Native IFC annotations and presentation resources are derived deliverables.
Canonical source facts, including IDs and provenance, remain the source of truth.
Bonsai can introduce runtime-dependent ordering or timestamps; no byte-identical
IFC/SVG promise is made across versions. Use the semantic round-trip and receipt
checks rather than inferring equivalence from a picture alone.

## Tests: skipped is not executed

Normal request/guard tests do not need Blender. The real native integration test
is explicitly skipped unless **both** runtime environment variables are supplied:

```sh
OABM_TEST_BLENDER="$BLENDER" OABM_TEST_BONSAI_PYTHON="$BONSAI_PYTHON" \
  PYTHONPATH=src pytest -q tests/test_native_bonsai.py
```

A supplied but incompatible or incomplete runtime fails the actual test; it is
not converted into a skip. Report skipped and executed tests separately. The
synthetic integration checks native 1:48 SVGs, preserved input bytes and labels;
it does not establish professional drafting acceptance for a real project.

## Optional source-space area annotations

A view may optionally include `space_annotations`, a list such as:

```json
{"source_id":"space:room", "label":"ROOM", "area_unit":"ft2", "shade":true}
```

Each source must be a unique canonical space on the view's specified level and
must not be excluded. Its finite, horizontal, simple, nonzero polygon is the
sole geometry source. The native fill and text are linked to the space IFC
product; the receipt records the canonical source ID, provenance, area in square
metres and display unit. Units are explicitly `m2` or `ft2`; `shade` is a
boolean choosing a fixed built-in style, not caller-supplied SVG or CSS. The label uses a deterministic interior anchor selected near the centroid, avoiding conservative bounding envelopes of requested point and earlier space annotations. Optionally supply `anchor_xy_m` for an explicit interior presentation anchor; it is refused if unsafe. If no candidate fits the view and avoids known annotation envelopes, export is refused. Other linework, occlusion and unmodeled collisions still require visual review. The displayed number is the
source polygon's XY area, not a field-verified or code-accepted net area. The
qualification **NET AREA UNVERIFIED** remains on the drawing. No height, volume,
occupancy, or engineering conclusion is inferred. Omitting the option leaves
the existing drawing behavior unchanged.
Native float32 polygon XY coordinates and the selected label anchor must reproduce their source values
within 0.01 mm; areas that round to zero at two decimal places in the selected unit are refused; otherwise the space annotation is refused rather than drawing a
visibly displaced or distorted polygon. The native text envelope check is
conservative, but it cannot guarantee against collision or occlusion.

A request-wide polygon validation budget limits the sum of squared selected
vertex counts to 1,000,000; excessive work is refused before native execution.
The request also limits conservative candidate placement work to 2,000,000
vertex, point, or previous-label checks. Explicit anchors count as one candidate.


## Optional isolated cardinal elevations

The same exporter accepts explicit `ELEVATION_VIEW` requests. These are native
orthographic views of the existing canonical adapter geometry, not a separate
renderer or an existing-IFC import path. See
[`synthetic-native-elevations-request.json`](../examples/synthetic-native-elevations-request.json)
for a request using the same synthetic model as the plan example.

```json
{"id":"EAST_LOOK", "target_view":"ELEVATION_VIEW",
 "camera_xyz_m":[-1,2,1.5], "direction":"+X",
 "near_m":0.002, "far_m":6, "width_m":6, "height_m":4,
 "scale_denominator":24, "exclude_ids":[], "annotations":[]}
```

`direction` is the direction **from the camera into the scene**, exactly `+X`,
`-X`, `+Y`, or `-Y`; world +Z is up. It is not the facade's name or outward
normal. The shared drawing projection frame supplies the axes; Blender's camera
uses negative projection-right and negative projection-normal to maintain its
right-handed local frame (+Z backwards). For example, when looking +X, world
+Y is screen-left; looking -X reverses that relationship. `near_m` must be explicitly **0.002**, Bonsai's pinned nominal viewport near
setting. Other values are refused before export: Bonsai 0.8.5 does not preserve
them in its IFC camera. The native SVG drawing cut is the camera plane itself
(depth zero), not a promised 2 mm inset. Choose the cut plane through
`camera_xyz_m`; the adapter never silently shifts it. `far_m` is positive metres
along the look direction; require `0.002 < far_m <= 200`. Receipts distinguish
nominal camera clipping from actual native drawing depth `[0, far_m]`.
All eight native drawing volume corners and the camera
must fit the existing +/-100 m local boundary. The quantized native camera is
checked again before rendering. Width/height, native scale and exclusion-ID
validation follow the existing exporter. Clipping is presentation only: excluded
and out-of-view source products remain in IFC and in the canonical roundtrip.

Elevation requests cannot contain plan-only `level_id`, `center_m`,
`cut_height_m`, or `space_annotations`. `annotations` must be an empty list:
this slice adds no elevation labels, dimension inference, assembly extent or
clear-opening interpretation. Ordinary `drawings.ElevationSpec` / dimensions
remain their existing separate derived-output API, not native annotations.
Unknown source heights remain unknown; camera height does not establish a ceiling.

Isolated elevations disable global referencing and exclude generated drawing /
storey reference markers from the detail. They do not delete model products.
Mixed plan/RCP/elevation requests use the existing eight-view total limit.
Receipts retain explicit camera, look direction, nominal clipping and native drawing depth intervals, native paper
scale and filter IDs; selected objects are not a pixel visibility guarantee.
Operator visual review remains required. Existing appearance, assembly,
privacy, installed-runtime and immutable-input boundaries are unchanged.

The fixed worker limits Blender and supported numerical thread pools to two threads.

## Bounded existing-IFC plumbing preservation (#374)

The separate `export_native_ifc` API / `--source-ifc` CLI option retains an
existing OABM IFC graph rather than regenerating it from canonical JSON. Supply
both the matching canonical `--model` and an explicit local `--source-ifc`.
The request version is `native-bonsai-ifc-v1`; ordinary canonical requests and
`export_native` retain their existing boundary. The input IFC is snapshotted,
hashed and never overwritten. This is **not** a plumbing-to-canonical importer.

Supported native additions are `IfcPipeSegment`, `IfcPipeFitting`,
`IfcSanitaryTerminal`, `IfcWasteTerminal`, their types, explicitly owned ports,
and bounded distribution systems. Their classes, identity, geometry,
properties and connectivity remain native facts, never repurposed electrical
entities. After native save/reopen, every original IFC entity/attribute must
remain unchanged, with one narrowly verified exception: the existing project's
representation-context list may append new drawing Plan contexts, retaining all
original references and their order. Removal, replacement, reordering, changed
physical data or new inverse physical relationships fail the export. The
receipt records this invariant; it is not a byte-identical output-file claim.
Canonical IDs/provenance are separately checked against the supplied model.

### Existing drawings remain opaque

The source is never handed directly to Bonsai. A disposable rendering copy
isolates pre-existing annotations, drawing groups, document references and
presentation property sets. Stored resource paths and text templates are not
loaded or evaluated. Unsupported external geometry/texture/runtime resources
remain refusals. Only newly generated, verified drawing entities are grafted
onto an original-source clone, with references mapped back to stable source
STEP identities. Existing drawing data remains in the final IFC but is not
used in the new drawing. Diagnostics identify this presentation omission.

Existing referenced resource **bytes are not copied or validated**. Their opaque
references are retained, not certified portable or safe to open elsewhere.
New drawing resources are generated locally by the fixed trusted worker and
checked as delivery descendants. This is a bounded preservation route, not a
general existing-document/resource renderer. Do not delete source metadata to
imply equivalent acceptance.

### Explicit unknown geometry and linked review labels

PLAN_VIEW requests may add `native_annotations` with `source_guid`, safe `text`
and `label_offset_mm`. Labels attach to the named plumbing product at its
explicit world-placement XY, on the presentation plane. This is a linked review
label, not inferred mounting, pipe size, connection or air-gap engineering.
Retained geometric separations and explicit source labels can communicate an
air-gap **intent**; they do not establish a required or compliant air-gap size.

The request's `acknowledged_native_body_omissions` must exactly list native
plumbing GUIDs without a Body (the field defaults to an empty list). Bounded
Curve3D/IfcPolyline pipe geometry is retained as linework, not a fabricated
pipe diameter. Placement-only sanitary/waste terminals retain their explicit
unknown geometry, with linked text where requested; no physical envelope is
invented. Empty or unsupported representations fail rather than silently vanish.
The receipt reports native selections and omissions, not pixel visibility or
professional drawing acceptance. IFC4 metre units and the existing local bounds
remain required, including composed world-space curve and solid extents.

Strict `oabm.ifc.from_ifc` refuses typed native plumbing rather than returning a
canonical model that would silently lose it during `to_ifc`. There is no public
lossy-import switch. The drawing worker uses a private canonical projection
only alongside its independent native preservation proof.

Generate the public synthetic sink/receptor source and request with:

```sh
PYTHONPATH=src python examples/synthetic_native_plumbing.py /tmp/plumbing-input
```

This example's supplied geometry and intentionally open receiving connection
are synthetic test facts, not engineering recommendations. Use its emitted
model, IFC and request paths with the explicit runtime CLI above plus
`--source-ifc`. Actual native integration tests require the two runtime
environment variables described above; skipped tests are not runtime proof.
