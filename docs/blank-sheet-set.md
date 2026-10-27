# Blank sheet production from job information

One supported command produces an ordered set of blank sheets with populated
title blocks, before a model or design exists:

```sh
PYTHONPATH=src python -m oabm.drawings.blank_set job-sheets.json /new/attempt
```

After normal package installation, the equivalent installed command is
`oabm-blank-sheets job-sheets.json /new/attempt`. Python callers use the same
engine API:

```python
from oabm.drawings.blank_set import create_blank_sheet_set
output = create_blank_sheet_set("job-sheets.json", output_directory="new-attempt")
```

This is blank stationery, not a completed plan set or engineering approval.
It requires no canonical model, fake view, review/proposal inputs, customer
plans, logos or private template. Existing incomplete-review sheet safeguards
remain unchanged. This initial public template is a generic bottom titleblock;
it makes no claim of fidelity to any private company or Sacramento-style set.

## Explicit editable input

```json
{
  "version": "blank-sheet-set-v1",
  "set_id": "EXAMPLE-SET",
  "job": {"Project": "Synthetic Workshop", "Prepared by": "Example Team"},
  "paper": {
    "width_mm": 609.6, "height_mm": 457.2,
    "margin_mm": 10, "titleblock_height_mm": 60
  },
  "revision": "0",
  "date": "2026-01-15",
  "formats": ["svg"],
  "sheets": [
    {"id": "E0.0", "title": "General information"},
    {"id": "E1.0", "title": "Power plan - blank",
     "fields": {"Prepared by": "Example Drafting Team"}},
    {"id": "E2.0", "title": "Equipment details - blank",
     "revision": "A", "date": "2026-02-01",
     "paper": {"width_mm": 297, "height_mm": 210}}
  ]
}
```

All job values are supplied strings; no project name, address, author, current
date, revision, signature or stamp is invented. Use only information appropriate
to the output's destination. Job configs can be private even though the engine
and synthetic examples are public.

- `job` supplies shared titleblock key/value rows. Each sheet's `fields` replaces
  matching shared keys by **exact spelling** and can add rows. An explicitly
  empty string clears a shared value. Case-only duplicate names are rejected.
- Sheet `revision`/`date` override the explicit shared revision/date. Otherwise
  they inherit those supplied values. Dates must be real ISO `YYYY-MM-DD` dates.
  The `Date` row is generated from that supplied value; date/revision/sheet/title
  names are reserved so conflicting freeform rows cannot shadow identity.
- Paper dimensions, margin and titleblock height are millimetres. Shared paper
  settings are explicit; a sheet may override individual dimensions. There is
  no fit-to-page scaling. Positive drawing area and titleblock are required.
- Sheet IDs/titles are explicit. List order is preserved, not sorted by ID.
  IDs are unique ignoring case, with 1-64 portable letters/digits/`.`/`_`/`-`,
  starting with a letter or digit. Filenames include a padded ordinal and ID.
- `formats` must include `svg`; optional `dxf` uses the existing `cad` extra.
  A requested unavailable format fails rather than silently disappearing.

The existing renderers print ID/title/revision and sorted supplied field rows.
They reject excessive row count, insufficient titleblock height, unsupported
text or overwide rows without truncating or shrinking content. Errors identify
the sheet, format and likely adjustments (`titleblock_height_mm`, row count,
field length or format dependency). SVG supports single-line XML-valid text;
the existing DXF adapter supports plain ASCII text and rejects unsupported text.
Format-specific typography follows the existing adapters, not a new branded
layout engine. Actual fonts and visual readability still need renderer QA.

## Outputs and first-class blank composition

Successful output is `attempt/delivery/`:

- ordered `sheets/001-E0.0.svg`, etc., and optional `.dxf` files;
- per-format mapping manifests and `.sheet.json` compositions;
- exact editable `config.json` and normalized `resolved-sheets.json`, including
  effective sheet settings and which shared/per-sheet field supplied each value;
- `MANIFEST.json`, ordered sheet index, status, runtime/source fingerprints and
  relative-path SHA-256/byte-count receipts for all files except the manifest;
- `READ-ME.txt` recording blank/no-design status and output limitations.

`compose_blank_sheet(SheetSpec(...))`, `render_blank_sheet_svg(spec)` and
`blank_sheet_to_dxf(spec)` are reusable first-class blank APIs. They produce
empty drawing bodies with `model_id: null`, no views and no canonical source
index. They do not manufacture a BuildingModel or a dummy view. The original
`compose_sheet` and review pipeline still require valid real view placements.
Both blank and populated sheets share the existing SVG/DXF rendering code.

The command exclusively reserves a fresh attempt. Outputs are staged under
`.pending/`, verified, and published together by directory rename. Never reuse
an existing attempt, including a failed one; retry at a new path. Structural
config errors fail before reservation. Rendering/capacity/dependency failures
retain a pending attempt, with best-effort `failure.json`, and publish no
`delivery/`. The original exception is preserved if failure reporting also fails.

Maintain exclusive ownership of attempt contents throughout generation and
publication. Hash validation plus rename is not protection against an external
writer, a power-loss durability guarantee or a signature. No files are uploaded
by this command. CLI exit 0 means blank files were published, never design or
permit readiness; exit 1 means failure.

## PDF boundary

**Combined PDF is not supported in this batch.** The repository has no supported
SVG/DXF-to-PDF conversion route. Its declared `pypdf` dependency reads/assembles
PDFs and cannot convert these sheet SVGs. A `pdf` format request is refused
explicitly before output reservation; every manifest records this limitation.
The physical-size SVGs and editable DXFs are usable outputs, but this command
does not promise a combined print PDF. No rasterizer or new dependency is added.

## Executable synthetic demonstration

```sh
PYTHONPATH=src python examples/blank_sheet_set.py /tmp/blank-set-demo
# No optional CAD dependency:
PYTHONPATH=src python examples/blank_sheet_set.py /tmp/blank-svg-demo --svg-only
```

The three-sheet example proves order, shared/per-sheet fields, mixed paper sizes,
explicit date/revision overrides, every file hash and byte-identical complete
rebuilds. It serializes vector output without a local renderer. Cloud visual
inspection is required before accepting the generic titleblock presentation.
