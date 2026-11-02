# Model-derived three-document utility delivery

Source implementation under #361. Runtime and private source-plan visual proof
are pending the execution hold. Do not treat this document as completed filing
or engineering approval. Real inputs and outputs belong outside public source.

Run `oabm-utility-set request.json fresh-private-attempt-directory` (or
`python -m oabm.drawings.utility_delivery ...`). Existing PDF and table/render
extras provide the company-sheet PDF runtime and pypdfium2. The command does not
install software, upload, notify third parties or submit an application.

## Request

The request is strict JSON with `version: "model-utility-set-v1"` and these keys:

| Key | Supplied input |
| --- | --- |
| `model` | Complete canonical 1.8 model JSON path |
| `profile` | Existing company-sheet profile JSON path |
| `job` | Existing company-sheet job, explicitly requesting SVG/PDF and exactly three template sheet IDs |
| `sources` | Source ID to local file path mapping, checked against consumed evidence SHA-256 |
| `load_inputs` | Separately source-bound supplied demand profile JSON path |
| `site` | `sheet_id`, `drawing` and optional paper-space `dimension_offset_mm` |
| `single_line` | `sheet_id` and explicit boolean `service_only` |
| `loads` | `sheet_id` |
| `documents` | Exact `site`, `single_line`, `loads` PDF filenames |
| `field_edit` | Optional explicit canonical XY move and user evidence |

Paths are relative to the request file unless absolute. `site.drawing` contains
only `scale`, `placement_mm`, `padding_mm`, `legend_width_mm`. North and all
physical positions come from the model. The site template scale field must match
`1:N`; no NOT TO SCALE site fallback is allowed. The other templates are the
diagrammatic service and supplied-load documents. Continuation pages derive from
the appropriate template and remain in that document's single PDF.

Filenames must begin with a letter/number, end in `.pdf`, and contain only
letters, numbers, periods, spaces, underscores and hyphens. Names must be distinct
ignoring case. Each PDF is at most 10,000,000 bytes; no document is silently
downsampled, split into extra portal filenames or truncated to meet the limit.

Example field edit (unrelated synthetic values):

```json
"field_edit": {
  "node_id": "service:main",
  "x_m": 4.0,
  "y_m": 3.0,
  "provenance": {
    "source_kind": "manual",
    "source_id": "field:synthetic-event",
    "derivation": "user"
  }
}
```

This moves an already source-located plan-only node, updates its private lateral
start and owned proposed surfaces atomically, preserves IDs/original evidence,
and regenerates every projection/model binding. The original model input stays
unchanged; the edited canonical snapshot is retained in the new delivery.
Unknown or spatially placed equipment is not assigned a guessed XY/Z/terminal
direction. Invalid dependent geometry refuses instead of rerouting by guess.

## Site and measurement semantics

Placed equipment with a known canonical `size` and `pose` uses the convex XY
projection of its actual rotated box corners. This is not an axis-aligned
bounding box, nominal generic square or invented approved-product envelope.
Unknown extents remain cross markers, and enclosure-based dimensions remain
unresolved. Confirmed gas enclosures and source marker points remain distinct.

Point, anchor-to-boundary and enclosure-to-enclosure distances derive from actual
referenced geometry. Overlap/containment yields zero, never a positive clearance
to the far boundary. Every dimension retains its requirement/owner references,
measurement basis and source model hash. A supplied minimum is displayed as a
requirement; horizontal coordinate evidence never becomes engineering approval.

Working-surface width/depth requires a real aligned rectangle, known enclosure
front interface and supplied `ServiceEquipmentFacts.front_normal_radians`.
The front normal is counterclockwise from model +X; the dimension width axis is
perpendicular and depth extends outward from that face. Unknown facing, extents,
irregular geometry or a nonadjacent surface stays unresolved. Actual polygon
edge lengths can still be dimensioned as SURFACE EDGE; those are not claims of
verified working clearance or field-confirmed level construction.

All requirement outcomes, including zero/unresolved measurements, are retained
and qualified on the site sheet. Source references/facts force explicit
service-only SLD selection where the default whole-model projection cannot cover
them. The complete service scope/omission report remains in the package; adding
records never silently replaces the default branch-panel graph.

## Delivery and retained proof

All request/model/profile/job/load JSON is captured through bounded, regular-file
descriptors opened nonblocking. Nonregular inputs refuse before reading; byte
counts and descriptor stability reject replacement/growth races. The company
resolver receives captured JSON bytes instead of reopening mutable paths;
logos use bounded capture and a final source recheck. Ordinary company-renderer
callers and drawing bounds remain unchanged.

All PDFs are prepared and size-checked before reserving the fresh attempt.
The shared company renderer provides real sheet stationery, font checks and PDF
pages. The load body insets measured text and table strokes within those bounds.
Its numeric section prints supplied threshold/factor, first block, remainder,
general/HVAC/floor subtotals, total VA, voltage division and sensitivity
addition/total/current with source IDs and bound numeric formulas. Policy prose
is supplemental, not the only calculation evidence. Components remain a separate
labeled section. Both sections wrap and paginate; no row or source basis is
dropped. Up to 20 pages per document are admitted, and every page gets
a rendered PNG proof. Images have a pixel admission limit and a counted output
stream; the complete attempt is limited to 128 MiB. Exceeding a bound refuses.

`delivery/` contains exactly the three named portal PDFs, sheet SVGs, every page
PNG, original/edited model snapshots, original request/profile/job/load inputs,
detached logo assets and derived site/service/load receipts. `MANIFEST.json`
records hashes, document page counts, source model binding, explicit scope and
unresolved measurements. Inputs/sources are rechecked before publication into
the local `delivery/` directory. Failure retains a `.pending` attempt and a
failure receipt; existing destinations are never reused or recursively removed.

`visual_review` remains pending human comparison to the source architect sheet,
even after PNG generation succeeds. Private page-by-page visual acceptance,
field-location confirmation and separately authorized project delivery remain
required before whole-issue acceptance. There is no automatic public attachment,
production deployment or utility submission.
