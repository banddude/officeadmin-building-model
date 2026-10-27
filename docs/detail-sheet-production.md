# Explicit detail sheets

`oabm.drawings.details` composes canonical supported envelopes using three
named, versioned recipes: `equipment-mounting-elevation@1`,
`connection-close-up@1`, and `supported-section@1`. The close-up is an explicit
larger-scale elevation, not invented terminal, conductor, or fastener geometry.
Sections use the existing supported section projection. Populated building
assemblies still fail the original generator guards.

Supply a validated canonical model with positive equipment, box, support
component, or dimension-derived fitting envelopes. Supply explicit canonical
source IDs, stable detail number and sheet ID, title, origin, direction, model
bounds, scale, depth interval, mounting and connection specifications and notes.
Unknown facts may be explicitly described as unspecified. Blank specifications
are refused. There is no new installation model or hardware inference.

The model and editable request in `fixtures/detail_sheets` are synthetic. Source
markers A1 onward follow sorted canonical IDs, independent of entity order.
Projected width/height callouts are view extents; local supplied X/Y/Z dimensions
and world position are separately labeled. Recipe numbers never auto-renumber.

`create_detail_sheet_set` calls the real `oabm.drawings.company_sheets` resolver
and creator. Profile/job files use that module's contract. Detail IDs must match
all resolved sheet IDs. `company_detail_bodies` supplies only SVG groups in paper
millimetres inside the resolved drawing bounds. The shared layer owns company
branding, borders, title blocks, SVG and optional PDF. No duplicate renderer or
PDF conversion is implemented here.

Run with the shared dependency available:

```sh
PYTHONPATH=src python examples/detail_sheet_set.py --profile profile.json --job job.json --output detail-output
```

Every requested view and all body layouts are checked before creating output.
Geometry clipping, missing supported dimensions, unknown IDs, empty projections,
text overflow and insufficient drawing area fail without scale changes. Text
uses the shared installed-font measurement/validation helpers and an explicit
Liberation Sans face. Fontconfig and a readable supported font are required;
there is no character-count fallback. XML-invalid and unsupported non-ASCII
text fails with the affected input field before package staging. Long notes
are refused rather than clipped, silently truncated, or resized. The
package is staged locally and renamed only after shared creation succeeds;
existing output directories are refused. It retains canonical model and detail
request bytes plus a deterministic detail manifest with input hashes. The
shared manifest remains owned by the shared pipeline.

This bounded tool does not select installation arrangements, connect equipment,
size electrical components, verify engineering or imply construction approval.
Unspecified connection geometry stays absent. Multiple closely spaced objects
can still need separate caller-selected details for readability.
