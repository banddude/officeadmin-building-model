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

## Multiple details on one sheet

`detail-sheet-set@2` retains the identical `details` list and adds an explicit
`placements` list. Each row supplies `detail_id`, `sheet_id`, and `bounds_mm` as
`[left, top, right, bottom]` in absolute paper millimetres. This is a caller-chosen
slot, not a request to fit or resize the view. Every detail must occur exactly
once and every requested sheet must receive content. Slots must stay inside the
resolved shared drawing area and must not overlap. View geometry, dimension
callouts, all supplied notes and measured text must fit their slot; otherwise
the error identifies the sheet, detail and slot and asks for revised inputs.

The original `detail-sheet-set@1` one-detail-per-sheet mode is unchanged. The
one-page fixture contains the same three details, IDs, numbers, titles, model
bounds and 1:10 / 1:5 / 1:10 scales as the original fixture. Its job explicitly
selects 36 by 24 inch paper and three nonoverlapping slots on sheet ED1. The
shared profile renderer supplies the single company title block.

```sh
PYTHONPATH=src python examples/detail_sheet_set.py \
  --profile fixtures/detail_sheets/profile.json \
  --job fixtures/detail_sheets/job-one-sheet.json \
  --details fixtures/detail_sheets/details-one-sheet.json \
  --output detail-one-page
```

The retained request and detail manifest include the exact slot assignments.
There is no automatic scale adjustment, note omission or fallback pagination.
