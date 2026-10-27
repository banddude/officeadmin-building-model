# Local vector PDF for blank sheet sets

Request a combined print PDF together with the original editable SVGs:

```json
"formats": ["svg", "pdf"]
```

Use the existing blank-sheet command and explicit job/sheet configuration:

```sh
python -m pip install -e '.[pdf]'
PYTHONPATH=src python -m oabm.drawings.blank_set job-sheets.json /new/attempt
```

The maintained optional dependency is `CairoSVG>=2.9.1,<3`; `pypdf` is already a
base project dependency. CairoSVG uses a native Cairo library, which must be
installed separately on the host. CairoSVG documents Cairo/libffi requirements
for macOS/Linux/Windows. A missing Python package or unavailable native Cairo
fails a requested PDF with an actionable error. The command never installs
packages, changes host security settings or falls back silently to missing PDFs.
SVG-only and SVG/DXF runs never load the optional PDF converter.

CairoSVG is LGPL-3.0-or-later and supports Python 3.10+; this project's Python
3.11+ range is compatible. It is installed as an optional dependency, not vendored
or modified here. Native Cairo has its own upstream licensing/runtime terms.
No font, renderer binary, network conversion service or private asset is bundled.
Primary references: [CairoSVG documentation](https://cairosvg.org/documentation/)
and [current package metadata](https://pypi.org/project/CairoSVG/).

## Supported conversion boundary

`render_blank_sheet_set_pdf(specs, title=...)` accepts an ordered sequence of
`SheetSpec` objects. It regenerates SVG bytes using the existing blank-sheet
renderer, then converts locally using CairoSVG's `svg2pdf(bytestring=..., dpi=96,
unsafe=False)`. It does not accept a caller URL, arbitrary SVG/XML, images,
external style sheets, file references or browser screenshots. The adapter
rejects unsupported/resource content before it reaches the converter. Literal
URLs in supplied titleblock **text** remain text, not fetched assets.

The supported SVG subset is the internally generated physical blank sheet:
page rectangle/divider, plain titleblock text and metadata. This is not a generic
SVG conversion endpoint. Future model/detail/schedule PDF conversion must extend
this boundary deliberately; do not pass arbitrary imported drawings through it.

CairoSVG creates one vector PDF page per sheet. `pypdf` combines those pages in
caller order and adds bookmarks using the supplied sheet IDs. Paper millimetres
map to PDF points at `72/25.4`; no fit-to-page or rescaling is performed. Each
page must have the supplied physical size, zero origin/rotation, no raster images
and extractable supplied titleblock rows. Whitespace normalization is used only
for the text-admission check; original strings remain in config and SVG output.

The combined PDF is reopened and checked again before return. Page text checks
are not font-shaping or pixel-layout approval. Cairo/font versions can change
appearance. The companion records backend versions and PDF font names; fonts
are selected from those available on the host. Inspect actual cloud-rendered
pages before accepting a packet. No engineering or permit approval is added.

## Files, repeatability and failure behavior

The delivery contains `sheet-set.pdf` and `sheet-set.pdf.map.json` in addition to
all requested SVG/DXF files. The companion binds ordered sheet IDs, physical
sizes, SVG hashes, extracted-text checks, converter versions and final PDF hash.
The publisher checks those SVG hashes against the retained SVG files, then
includes PDF/map receipts in its normal full-package verification and atomic
directory publication.

The assembly creates fresh PDF document metadata and does not copy Cairo's
wall-clock creation metadata. Complete rebuilds are byte-identical with the
same engine, inputs, CairoSVG/Cairo/pypdf versions and installed fonts. This is
not a cross-platform byte-identity or hermetic-runtime claim.

Any missing dependency, conversion, page-size, text, source-binding or publication
failure leaves an unsuccessful pending attempt and no completed delivery. A PDF
request never succeeds with only its SVGs. Retry at a new attempt directory;
existing outputs remain untouched. Blank/no-design status and the generic
bottom titleblock remain explicit. This does not claim private-template fidelity.

## Synthetic verification and cloud handoff

```sh
PYTHONPATH=src python examples/blank_sheet_pdf.py /tmp/blank-sheet-pdf-demo
PYTHONPATH=src python -m pytest -q tests/test_sheet_pdf.py tests/test_blank_sheet_set.py
```

The demo contains two 609.6 x 457.2 mm sheets followed by one 297 x 210 mm sheet,
with shared/per-sheet field and revision/date overrides. It verifies every file
receipt and complete repeated builds. Tests check physical pages, order, text,
vector output, missing dependencies, rejected external resources, bad converter
output, changed source SVG and late failures. Real conversion tests skip clearly
when the optional dependency/native library is unavailable; missing-dependency
and boundary tests still run. Pixel review belongs to the independent cloud QA
step, not these numerical/text checks.
