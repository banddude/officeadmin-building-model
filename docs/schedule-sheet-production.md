# Supplied panel facts on physical sheets

`oabm-panel-schedule-sheets MODEL PROFILE JOB SETTINGS OUTPUT` (or
`python -m oabm.drawings.schedule_set ...`) consumes an explicit canonical
BuildingModel, the shared company-sheet profile, a shared job with exactly one
schedule template sheet, and explicit layout settings. It produces editable
vector SVG sheets and optional combined vector PDF through
`oabm.drawings.company_sheets.create_company_sheet_set`. Company identity,
logo/field precedence, frame, title rail, paper dimensions and PDF conversion
remain owned by that shared tool. There is no schedule-specific title block.

The input model supplies panel/circuit/load facts; it is not reconstructed from
a schedule. Existing `generate_panel_schedule_report` and
`panel_report_to_drawing_schedules` own the report, owner assignment, arithmetic,
unknowns and duplicate printed-label conflicts. Circuit tables now visibly name
conflicting circuit IDs. Every panel receives its circuit table and supplied
connected-load summary. Unsupported source owners remain in an explicit table.
An empty panel has an explicit no-listed-circuits note and no invented data row.

Connected VA is only the supplied known subtotal. Missing loads remain unknown.
No demand factors, service sizing, breaker selection, spare slots or regulatory
approval are inferred. The result is presentation output requiring review.

In the drafted card presentation, each panel heading prints its stable canonical
panel ID as well as any distinct supplied name; that identity repeats on every
continuation. Conflicting opaque labels and missing/blank labels print the
individual canonical circuit ID in the affected row. An unsupported-source-owner
row also prints its actual circuit, source-port and owner IDs; none of those
facts is promoted into a panel assignment. The row map retains the complete
conflict-group IDs and canonical source bindings. Long text wraps at measured
paper size and may increase pagination; an individual row that cannot fit is
refused rather than truncated or scaled down.

## Synthetic demonstration

After the shared company profile dependency is installed:

```sh
PYTHONPATH=src python examples/panel_schedule_sheets.py \
  fixtures/company_sheets/v1/profile.json \
  fixtures/schedule_sheets/v1/job.json /tmp/synthetic-schedule-demo
```

This uses compact packing and generates two explicitly supplied synthetic panels and 90 circuit rows,
including an explicit zero, an unknown load and a duplicate opaque circuit
label. The large panel spans two pages; both supplied-load summaries and the
second panel share remaining space on page two. The same data needs five pages
in explicit one-table-per-sheet mode. The settings input is generated
beside the editable canonical model; production callers supply their own files:

```json
{
  "version": "panel-schedule-sheet-set-v1",
  "layout": {
    "text_height_mm": 2.5,
    "line_height_mm": 4.0,
    "padding_mm": 1.5,
    "column_widths_mm": {},
    "packing": "compact"
  }
}
```

`contracts/schedule-sheet-settings-v1.schema.json` describes the settings shape.
The runtime also enforces line height at least 1.5 times text height. Column
widths are optional paper-mm overrides keyed by existing schedule column key;
remaining columns divide remaining body width equally. Unknown keys, nonfinite
values, impossible widths and oversized rows fail explicitly. Use wider paper,
wider columns or smaller text to resolve overflow. No text is ellipsized.

The shared renderer supports XML `space="preserve"` on group/text elements.
Leading, trailing and repeated supplied ASCII spaces are retained exactly in
source, wrapped cells and SVG. The schedule renderer explicitly opts into that
mode. XML-invalid/control text and missing glyphs fail through shared
`validate_sheet_text`; an explicit source newline is laid out as separate lines.
No source value is silently trimmed or canonicalized.

## Layout and traceability

`paginate_schedules(drawing_set, resolved_company_sheet, layout=...)` is a
reusable derived-table paginator. It accepts existing `DrawingSchedule`s and
retains the canonical source index. It produces paper-mm SVG `<g>` bodies,
editable derived drawing JSON, and one row-map entry per actual source row.
The body stays inset from the shared `drawing_bounds_mm`; it cannot enter the
rail. The default `packing: "one_table_per_sheet"` starts each table on a new page.
`packing: "compact"` instead packs complete pre-paginated sections vertically in
source order, using remaining space when the next section fits. It does not
shrink text, split rows, change wrapping or reorder panels. Each section retains
its own full panel identity/title, column headers and schedule N/M reference.
Continuation sections retain repeated headers. A fixed gap separates sections.
A section that cannot fit in remaining space starts a new physical page.
Final sheet IDs and row coordinates are assigned after packing. No table is
omitted to obtain fewer pages.
Rows stay intact across page boundaries. Long opaque
identifiers wrap deterministically using shared `measure_sheet_text` actual
installed-font advances and ink bearings (including its 1% safety margin),
with binary search for the longest fitting substring. All characters, including
spaces, remain in the wrapped source lines.
The shared renderer's supported DejaVu Sans font is used. Glyph and pixel
appearance still require independent review.

Continuation sheets increment the trailing numeric sheet ID while retaining
its zero padding: E6.0 becomes E6.1, E6.2, etc. A nonnumeric prefix gets a
three-digit suffix. Title-rail capacity is checked by the shared renderer;
choose a short template ID or explicitly widen the shared profile rail if it
fails. Each footer prints schedule page N/M and its physical sheet ID.

The manifest's `sheet_order` and retained expanded shared job are the final
physical sheet index, suitable for the cover/index consumer. Its
`schedule_pages` list indexes table sections, so multiple entries can point at
one physical sheet in compact mode. Each compact section includes its bounds;
rows keep final physical page/ID, translated bounds and regenerated primitive
references. `SchedulePage.sections` is authoritative for packed pages containing
multiple tables; the singular schedule fields are null on those pages.

`delivery/SCHEDULE-MANIFEST.json` binds each row to schedule ID, zero-based
row index, canonical source ID, physical sheet/page, bounds, exact original
cells, wrapped cell lines, and primitive IDs in the retained drawing JSON.
It binds all shared exports and retained inputs by hash and lists panel
subtotals/unknown-load IDs copied from the fact report. Repeated headers are
presentation content and are excluded from source row counts. Original values,
confidence, provenance and report diagnostics remain in retained model/report
JSON. Source indexes preserve actual canonical IDs; schedule/layout IDs never
become model entities.

`delivery/schedule-source/` retains the exact original four inputs, portable
profile plus expanded shared job, effective logo bytes, report, schedule data,
per-page drawing JSON and SVG body. The portable profile/expanded job and
retained assets can be passed to the shared company tool with the retained
bodies to reproduce the sheets without relying on original asset paths.
The original shared MANIFEST remains intact; the schedule manifest is the
outer receipt and includes its hash.

The exporter checks exact SVG text line retention, row count/order/coverage,
PDF page count and physical dimensions, and every projected nonspace glyph run
in PDF extraction. Cairo writes PDF spaces as positioned gaps, so extraction
cannot distinguish one space from two or retain all leading/trailing spaces.
The manifest states that limit explicitly; exact whitespace remains in the
source/SVG and actual PDF pixel regressions verify wider internal gaps and
leading offsets for `A B`, `A  B` and ` A B `. Shared PDF checks also verify the
embedded font identities used for measured layout. Missing/unsupported PDF glyphs fail instead of issuing a
successful packet. Output must be a fresh path. A failure retains a failure
receipt (or raises before reserving the path for invalid input); there is no
`delivery` directory until the whole packet succeeds. No canonical mutation,
network call, external service, or production execution is involved.

PDF text verification compares every projected non-space Unicode glyph in order.
Printable Unicode spaces such as NBSP may be represented by Cairo as positioned
gaps or extracted as ordinary spaces. Their exact code points and counts remain
bound in canonical input, row cells, wrapped lines and SVG; PDF extraction does
not prove those whitespace identities. Missing visible glyphs still fail.

### Circuits without supplied positions

Drafted presentation uses a single eight-column circuit list when a panel has
listed circuits but no supplied side/row positions. It prints the circuit label,
description, poles, supplied OCPD, connected VA and supplied phase values across
the card. It does not draw a fictitious empty right-hand bank or imply that every
listed circuit occupies the left side. The position qualification is visible,
and row receipts mark `display_side: unpositioned` while preserving null supplied
side/row values. Panels with explicit positions retain the mirrored left/right
panel-card layout. The packet identifies this presentation as
`paired-panel-card-v2`; canonical facts, totals and source coverage are unchanged.
