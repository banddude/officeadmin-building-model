# Supplied site-plan drawing production

Issue #365 is a bounded presentation adapter. It consumes explicitly supplied
measured/traced drawing geometry and uses the existing `DrawingSet`,
`compose_sheet`, `render_sheet_svg` and shared company sheet profile. It does
not acquire geometry from an address, interpret imagery, create a competing
semantic site model, or write canonical `BuildingModel` data. These are drawing
inputs, not importer outputs or a router's obstacle database. Canonical BIM
import, physical footprints, ports and engineering decisions remain upstream.

The public example `examples/synthetic-site-plan.json` contains only synthetic
coordinates, labels and evidence. No customer project, address, image or logo is
included.

## Request contract

`supplied-site-plan-v1` requires exact top-level keys `version`, `id`, `units`,
`frame`, `features`, `drawing`. Units are explicitly `m`, `mm` or international
`ft` (0.3048 metres). The frame requires an ID and
`axes: "local-xy,+x-right,+y-up"`. Every feature must name that exact frame.
Register other frames upstream. No translation, rotation or CRS lookup occurs.
The adapter converts linear units to existing drawing metres, with the existing
nine-decimal drawing quantization. The original input bytes remain retained.

Every feature requires a stable ID, kind (`building`, `curb`, `parking`,
`landscape`, `equipment`, `route`), `frame_id`, explicit XY `points`, boolean
`closed`, a single-line `label`, and `evidence`. Polygon closure is implicit;
repeating the first point is invalid. Building outlines are closed polygons;
other site boundaries may be open or closed as supplied. Crossed boundaries,
zero area polygons, repeated vertices and nonfinite numbers are rejected.
Equipment is one fixed anchor, represented by a generic 3 mm square, not an
asserted footprint, rating, orientation or clearance.

Evidence requires `source_id`, `reference`, `qualification` and confidence in
[0,1]. All are supplied facts about the drawing input; no evidence is fetched
or promoted to survey accuracy. Original evidence and coordinates are retained
in the audit, and source references flow into the existing renderer's map.
Qualifications print in the legend without truncation.

Routes require `route_status`: `supplied` means an explicit open polyline of at
least two points; `empty` means no geometry; `future` means zero points or an
explicit open polyline. Future geometry prints dashed. Empty and future intents
without geometry print as distinct legend entries and create no guessed line.
The adapter does not infer endpoint connections, conductor or raceway facts.

Drawing settings explicitly require `scale` (positive denominator of 1:N),
`placement_mm` (paper upper-left XY), `padding_mm` (at least 5),
`legend_width_mm` (at least 95) and `north_degrees` (null or [0,360), clockwise
from local +Y). North is omitted as a direction when unknown and the legend says
it was not supplied. The scale bar is exactly 20 paper millimetres. Both views
share scale; there is no automatic fit, rotation or clipping to paper.

## Layout and review

Bounds derive from all supplied geometry, plus explicit padding, with a minimum
65 mm annotation height. The adjacent legend has explicit reserved width.
Sheet composition rejects overflow beyond margins or into the titleblock. If
text is too long or the legend runs out of rows, production fails clearly.

The existing deterministic label pass offsets text and adds source-bound
leaders. It never relocates equipment or route vertices. When no clear label
candidate exists, the full label text and source ID print in the legend, and
the full failure record stays in the audit. Label admission uses conservative
rectangles, not measured fonts. Generic mark envelopes are reserved using the
same physical size as the sheet symbol. Independent rendered visual inspection
is required, especially for dense sites, unusual fonts and Unicode shaping.

SVG contains editable vector geometry and text with source IDs. JSON receipts
retain the exact request, resolved sheet and drawing serialization, coordinate
mapping, original evidence, label failures and hashes. No survey, completeness,
engineering, code or permit approval is asserted.

## Command and shared dependency

```sh
python -m oabm.drawings.site_plan \
  examples/synthetic-site-plan.json \
  examples/synthetic-site-company-profile.json \
  examples/synthetic-site-company-job.json \
  /tmp/new-site-sheet-attempt
```

The command requires the shared `oabm.drawings.company_sheets` dependency. It
calls `resolve_company_sheet_set(profile_path, job_path)` and
`create_company_sheet_set(..., bodies={sheet.id: body})` for exactly one sheet.
The job's explicit `scale` field must match the drawing denominator as `1:N`.
The shared resolver owns profile/job/sheet precedence, assets, the right rail,
paper settings, ordered SVG/PDF production and their receipts. There is no
site-specific company data or alternate titleblock.

`render_site_body(request, resolved_sheet)` is the content-only integration
point for a combined plan-set caller. Its first result is an SVG `g` subtree
using paper millimetres in the resolved drawing bounds. The other results
retain drawing objects, staging composition, audit and source map. A staging
surface satisfies the existing physical renderer's generic stationery
requirement; only source-mapped content nodes are retained, and that temporary
stationery is discarded. Final sheet branding comes exclusively from the
company-sheet tool.

The shared body vocabulary admits solid strokes only. Consequently dashed and
dotted lines become grouped explicit paper-space line segments, with continuous
pattern phase across vertices. Input route/boundary vertices remain untouched
in `drawing.json` and the audit. Unsupported metadata and halos are removed
from the body, while `site-body.map.json` maps the original renderer IDs to
ordered body child paths and keeps source IDs, evidence, style tables and
symbol geometry. Body hashes bind the exact handed-off subtree. The portable
schema checks shape; Python adds geometric/frame/identity and overflow checks.

The new attempt is exclusive and never overwrites existing output. Failures
retain pending artifacts without publishing a completed top-level delivery.
A successful delivery includes the shared tool's returned delivery under
`company-sheet-set`, exact site input, content-only body, drawing JSON, audit,
source map, and a manifest binding all retained files and engine source hashes.
The wrapper checks the shared retained profile/job bytes against the inputs
read before resolution to reject changes during production. Original job asset
paths retain the shared dependency's documented portable-rebuild limitations.

Feature-specific legend entries retain their real feature source IDs. This
includes labels that could not be placed beside geometry, and zero-point empty
or future route intents. Such intents print the complete supplied human label,
feature ID and status plus an explicit no-geometry notice, without adding a
line or symbol. Shared qualification rows reference every feature they qualify.
The audit distinguishes requested primitive IDs from the actual rendered IDs,
including fallback legend entries.

Body text and legend width admission use the shared
`validate_sheet_text`/`measure_sheet_text` helpers from `company_sheets`.
Measurement uses the installed face, advance widths, ink bearings and the shared
1% margin, with `DejaVu Sans` matching the existing content renderer. Measured
widths, family, size and weight are retained per renderer text ID in the body
map. The shared final renderer validates and records actual SVG/PDF font
identity. Unsupported non-ASCII presentation text fails explicitly rather than
substituting glyphs. Supplied spaces remain intact in body text and inherit the
shared sheet's whitespace-preservation behavior. The existing generic label
candidate algorithm still uses conservative collision boxes; it is not an
independent font estimator or a visual approval gate.
