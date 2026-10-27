# Shared company sheet profiles

A saved JSON company profile plus explicit job inputs produces ordered drawing
sheets with a populated right-hand title rail and an empty drawing area. This is
presentation composition; no engineering approval, seal or surveyed location is
created. The reusable public fixture contains only synthetic identity and data.
Real company profiles, logos and customer job JSON belong outside this public repo.

```sh
python -m oabm.drawings.company_sheets create /private/company.json /private/job.json /fresh/attempt
python examples/company_sheet_set.py /fresh/synthetic-demo
```

SVG is always produced; PDF is requested with `formats: ["svg", "pdf"]` and
requires the `pdf` optional extra plus native Cairo, as described in
[sheet-pdf.md](sheet-pdf.md). Conversion runs locally. Logos are optional bounded
local PNGs, embedded at their original aspect ratio. These are raster logo
components within otherwise vector pages, not whole-page raster conversion.
No address lookup, external asset fetching or network conversion occurs.

See `fixtures/company_sheets/v1/profile.json` and `job.json` for complete editable
examples. Profile version is `company-sheet-profile-v1`; job version is
`company-sheet-job-v1`. Fields are the named slots in those examples, including
optional company/license/contact lines. Date (ISO YYYY-MM-DD) and revision are
required job values. Missing optional values stay blank. Sheet IDs/titles are
explicit and preserve list order. Template dimensions are physical millimetres;
paper, margin, rail width and rail inset can be overridden. The rail hierarchy
scales vertically with paper height; narrow rails and long unbreakable text fail
with capacity errors instead of truncation. Independent pixel/font QA is needed.

Precedence is profile, then job, then per-sheet `fields`, `template`, and `logos`.
An empty field clears text; `null` clears a logo. Profile logo paths resolve beside
the profile, while job/per-sheet paths resolve beside the job JSON. Rendering
snapshots asset bytes and never writes either input. Changing a job's logo or
company text does not alter future jobs.

Persist defaults only through this separate explicit operation:

```sh
python -m oabm.drawings.company_sheets update-defaults /private/company.json /private/changes.json --expected-sha256 CURRENT_PROFILE_SHA256
```

Changes contain only `fields`, `template`, and/or `logos` objects. This shallow
per-section merge validates assets, checks the expected current hash, keeps the
previous JSON in a hash-named `.bak`, and atomically replaces the profile. Paths
in persisted logo changes are relative to the saved profile. Keep exclusive
ownership of that profile during updates; this is an optimistic change guard,
not a cross-process locking protocol. No defaults mutation occurs during create.

## Shared content interface

```python
from oabm.drawings.company_sheets import (
    resolve_company_sheet_set, render_company_sheet_svg, create_company_sheet_set,
)
resolved = resolve_company_sheet_set(profile_path, job_path)
for sheet in resolved.sheets:
    # sheet.id/title/width_mm/height_mm/fields/drawing_bounds_mm
    # drawing_bounds_mm = left, top, right, bottom in paper millimetres
    svg = render_company_sheet_svg(sheet, body_svg=None)
create_company_sheet_set(profile_path, job_path,
    output_directory=fresh_attempt, bodies={"E0.0": '<g>...</g>'})
```

Bodies are engine-generated SVG `g` subtrees in top-left paper millimetres, +Y
down. Supported elements: groups (no transforms), rect, line, circle, polyline,
polygon and plain text. Use explicit physical font sizes and solid `#RRGGBB`
colors. Text fonts are Liberation Sans, DejaVu Sans or sans-serif. Scripts,
images, CSS, transforms, paths and external resources are refused. Consumers own
wrapping, pagination and semantic source maps; this layer checks conservative
text/shape extents against the drawing bounds and supplies the common frame.
Keep ink, including stroke width, inside those bounds. Unsupported content is
an actionable failure, never silently dropped.

Each fresh attempt publishes `delivery/` only after all requested pages/PDF and
hash checks succeed. Failures after reservation retain `.pending` and a
best-effort failure record. Existing attempts are refused. Exclusive ownership
of attempt contents is required throughout generation; validation followed by
rename is atomic publication, not protection from concurrent external writers.
Inputs, resolved fields/template, exact logo bytes, body fragments and runtime
source hashes are retained. Original JSON paths may refer to the original
private directories. Portable editable inputs reference the retained assets;
rebuild with `python -m oabm.drawings.company_sheets rebuild /delivery /fresh/attempt`.
This verifies receipts and calls the same composition API; generated SVG/PDF
bytes match when the recorded source/runtime/fonts match. Protect the whole delivery as private when its inputs
are private. It is not a permit-set completeness or design approval gate.
