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
Text layout also requires Fontconfig (`fc-match`) and readable installed fonts.
Pillow measures the selected face's actual advances and ink bounds; the resolved
family is written into SVG and font-file hashes/PostScript names are recorded
in the delivery. For example, a machine without Liberation Sans may explicitly
resolve Arial; that actual family is measured and written into SVG. The PDF
adapter checks its embedded PostScript font names against those resolved faces
and refuses silent converter substitution. This validates the installed fallback,
not fidelity to a reference font that was unavailable. Matching source/runtime
and font hashes are required for byte-identical rebuilds across hosts.
Presentation text supports XML-valid printable Unicode only when every character
has a glyph in the actual selected face. FontTools reads that face's Unicode cmap
from the same byte snapshot used by Pillow; missing glyphs identify the code point,
character position and resolved font. Names such as José/Peña, curly punctuation,
degree and Ø are admitted when covered. A diameter symbol absent from the face is
refused rather than shown as tofu. Controls, formatting controls, invalid XML and
surrogates remain errors. Text is never normalized, replaced or truncated. All inputs reject XML-invalid characters and each final SVG
is parsed before publication. No address lookup, external asset fetching or network conversion occurs.

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
wrapping, pagination and semantic source maps; this layer checks measured
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

Consumers should reuse `measure_sheet_text(text, font_size_mm, *,
font_family="Liberation Sans", font_weight=400) -> float` for wrapping and
capacity checks. The result is occupied width in millimetres, including ink
bearings and a one-percent margin. `validate_sheet_text(text) -> str` exposes
the same XML-safe printable-Unicode contract. Its optional `font_family` and
`font_weight` keywords enforce actual glyph coverage; callers validating source
before choosing a font may omit the family. Measurement and rendering always
check the selected face. Final shared composition still
checks positioned content; measured width alone is not a layout approval.

Joined polyline/polygon strokes use an explicit emitted `stroke-linejoin="round"`
policy. Non-round requested joins are refused. Round joins keep ink within the
vertex bounds plus half stroke width; default SVG miters can exceed that bound
at acute angles and are not admitted. No clipping is used to enforce this.

Glyph coverage uses the maintained [FontTools TTFont cmap API](https://fonttools.readthedocs.io/en/latest/ttLib/ttFont.html#fontTools.ttLib.ttFont.TTFont.getBestCmap); no fonts are bundled or downloaded.

Delivery fingerprints include only faces used by rendered SVG text. Unused font
aliases are never resolved or decoded. A requested unreadable font identifies the
requested/resolved family and required runtime/format. Supported local containers
are TrueType/OpenType sfnt and collections; WOFF/WOFF2 webfonts are explicitly
refused, without adding optional webfont decoder dependencies. No unrelated font
can make an otherwise supported SVG-only job fail during manifest generation.

On taller paper, rail zones expand but typography is capped at the reference
physical size. A 900 mm-high sheet does not enlarge type into an unchanged
87.136 mm rail. Smaller sheets retain the existing proportional type policy.
Explicit rail widths are never silently widened; when supplied content cannot
fit that physical width, the measured capacity diagnostic still requires an
explicit template/content revision. View geometry and scales are unaffected.

### Explicit retained presentation images

Python composition may supply `images={sheet_id: (SheetImageAsset(...),)}` to
`create_company_sheet_set`; `render_company_sheet_svg` accepts the same typed
assets through its `images` argument. Each asset contains an ID, detached PNG
bytes, paper-mm `(left, top, right, bottom)` bounds, nonempty attribution and
string provenance. PNGs are limited to 8 MiB/16 megapixels, eight per sheet, with
nonoverlapping bounds inside the drawing area. The producer reserves the image
region and prints required attribution visibly in its own body layout.

This channel emits its own embedded image after validating bytes and placement.
It does not permit image elements, remote URLs or scripts in producer SVG bodies,
and does not fetch assets. Original PNGs, exact hashes, attribution and provenance
are retained in the delivery; rebuild uses those retained bytes. Profile/job
schemas and saved defaults are unchanged. A map producer must separately qualify
provider, location and scale; an image does not establish surveyed coordinates.
