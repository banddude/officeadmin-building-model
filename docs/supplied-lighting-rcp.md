# Supplied 2D lighting / reflected-ceiling review sheet

This is an **explicit source-drawing presentation**, not a 3D physical model or
an importer that accepts devices into BIM. It draws caller-supplied XY paths,
fixture marks, aim targets, labels, zone names and notes on the existing shared
company frame. Mounting elevation and ceiling height are unknown in v1. It
never inserts a zero-height physical device, infers a ceiling/host, calculates
lighting or installation, or establishes an installed quantity. The printed
fixture/track counts are **design counts**, not procurement or installed counts.
Independent source and visual review is required; fixture labels can overlap in
congested layouts and are not collision-resolved. Track tags use bounded candidates
near supplied endpoints and avoid fixture/annotation labels and other track tags,
but are not checked against all source linework; impossible tag placement refuses.
Small white measured backplates make fixture, annotation and track labels legible
over source linework. They mask lines visually but never modify retained input
geometry; check for obscured symbols or overlapping labels in the final sheet.

Run the public synthetic example from the checkout with the [PDF runtime](sheet-pdf.md)
installed; the destination must not exist:

```sh
PYTHONPATH=src python -m oabm.drawings.lighting_rcp \
  examples/synthetic_lighting_rcp/request.json \
  examples/synthetic_lighting_rcp/profile.json \
  examples/synthetic_lighting_rcp/job.json /tmp/lighting-rcp-demo
```

After installation the equivalent is `oabm-lighting-rcp REQUEST PROFILE JOB FRESH_ATTEMPT`.
The job must have exactly one shared sheet, with the requested sheet ID and a
scale field matching the drawing denominator (e.g. `1:20`). Outputs:

- `delivery/company/delivery/sheets/` and `sheet-set.pdf` (if requested),
  composed by the existing company renderer and PDF adapter;
- `delivery/inputs/lighting-request.json` and `inputs/sources/<id>.bin`, the
  exact request and SHA-verified source bytes; original paths remain in request;
- `delivery/source-map.json`, source-to-body child mapping, original XY and
  track/head membership, declared frame, unknowns and design-count status;
- `delivery/LIGHTING-MANIFEST.json` hashes the retained files and nested shared
  delivery. The manifest identifies source snapshots by ID.

Keep private inputs, source evidence and the entire output outside the public
checkout. Re-run from the retained request and original/reconstructed private
source paths at a **fresh** attempt; this v1 wrapper does not provide a portable
one-command rebuild. Failures after reservation retain `.pending/` and
`failure.json`, never a published delivery. Keep exclusive ownership of inputs
and attempt contents throughout; hashes and atomic rename do not protect against
concurrent external writers or guarantee power-loss durability.

## Input contract

See `examples/synthetic_lighting_rcp/request.json` for a complete public-safe
example. Exactly these top-level keys are required: `version` (the literal
`supplied-lighting-rcp-v1`), `id`, `units`, `frame`, `sources`, `context`,
`fixtures`, `tracks`, `zones`, `notes`, `declared_counts`, `drawing`,
`physical_elevation_m`, and `ceiling_height_m`. Optional `annotations` is an
ordered list; omitting it is equivalent to `[]`. Both height values must be
`null`. Coordinates, view bounds and frame origin use explicit `m`, `mm`, or
international `ft`; no source coordinate conversion or unit inference occurs.

- `frame` is `{id, origin:[x,y], x_axis:"right", y_axis:"up", qualification}`.
  Origin is recorded metadata, not a second translation; all input XY values are
  already in this frame. A source with another axis/frame must be transformed
  explicitly before submission; retain the transform and its qualification as
  source evidence. Equal coordinates in separate frames do not imply registration.
- Each source is `{id,path,sha256,qualification}`. Paths resolve beside the
  request, and `sha256` is the lowercase hash of the exact source bytes. Each
  source must be nonempty and at most 64 MiB; total source bytes at most 256 MiB.
  The wrapper snapshots those files without interpreting or uploading them.
- `context`: ordered `{id,points:[[x,y],...],closed,source_ids}` vector paths;
  closed paths have at least 3 distinct non-repeated closing points, open paths
  at least 2. Do not repeat the first point as the last of a closed path.
- `fixtures`: ordered `{id,kind,symbol,zone_id,xy,aim_xy,source_ids}`. `kind`
  is supplied text. `symbol` is an explicit generic paper mark: `track_head`,
  `downlight`, `linear`, or `generic`. `zone_id` and `aim_xy` may be null; a
  supplied aim is a distinct XY point. A line and small endpoint marker show
  the supplied aim, not a photometric beam or verified targeting. Symbols are
  fixed paper sizes and are not physical footprints.
- `tracks`: ordered `{id,points,head_ids,source_ids}` explicit open polylines;
  `head_ids` names supplied fixtures with the `track_head` symbol. A head may
  not belong to two tracks.
  Membership is preserved, not inferred or checked for physical fit.
- `zones`: ordered `{id,label,source_ids}`; `notes`: ordered
  `{id,text,source_ids}`. These print in the optional notes column (or below the view if omitted), after
  the required unknown-height notice, design counts and symbol/kind legend.
  Kind and zone summaries are explicitly design counts. Numbered general notes,
  human-readable fixture types and their actual generic vector marks, and zone
  summaries are presented under separate headings. Track IDs are drawn near
  endpoints and retained with their chosen paper anchors in the source map. `annotations`, if present,
  are ordered `{id,text,xy,source_ids}`; they print at the explicitly supplied
  anchor and can describe rooms, stations or dimension text. They are not
  fixtures or calculated dimensions. Witness lines can be supplied as context.
- Every row uses a layout-wide unique, nonempty, single-line ID (up to 128
  characters) and a nonempty `source_ids` list of declared, unique source IDs.
  Source IDs use 1–64 portable letters/digits/periods/underscores/hyphens
  beginning with a letter or digit; source IDs become retained filenames. `declared_counts` is
  `{context,fixtures,tracks}` with exact integer list counts.
- `drawing` has `{sheet_id,scale,origin_mm:[x,y],view_bounds:[min_x,min_y,max_x,max_y]}`
  and optional `notes_bounds_mm:[left,top,right,bottom]`.
  Scale is the positive denominator of 1:N; origin is the top-left of the
  view rectangle in paper millimetres. The y-up drawing frame is mapped to
  the sheet's y-down paper coordinates without changing input points. All
  path/fixture/aim/annotation anchors must be inside the explicit view bounds,
  and the view rectangle and all actual ink must fit the shared drawing area.
  The optional notes rectangle is an explicitly supplied paper-mm column, inside
  the shared drawing area and at least 3 mm clear of the entire view rectangle.
  It wraps note/legend text without discarding characters and refuses overflow.
  With a notes column, actual view ink (including symbol and label extents) must
  also stay inside the view rectangle, including the padded label backplates;
  pad view bounds for marks near an edge.
  Without it, reserve enough space **below** the view for 3.2 mm notes/zone names;
  split long supplied notes into separate sourced rows if needed. Text never
  shrinks or truncates. There is no automatic fit, collision avoidance, inferred
  scale or clipping.

For embedding in another existing shared-sheet orchestrator, Python callers use
`render_lighting_rcp_body(request, sheet, source_bytes={source_id: bytes, ...})`
to receive `(body_svg, source_map)`, then pass the body to
`create_company_sheet_set(..., bodies={sheet.id: body_svg})`. The caller of this
lower-level API owns retention of the exact request/source bytes and source map.
The standalone command performs that retention and calls the same shared
renderer; it does not implement another titleblock or PDF path.
