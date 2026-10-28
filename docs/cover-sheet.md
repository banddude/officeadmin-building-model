# Cover sheet

Input: project details, scope, an ordered sheet list, contacts, and a saved
company profile. Explicit job notes and a supplied vector location diagram are
optional.

Output: a finished cover sheet as editable SVG and vector PDF in the saved
company style, retaining physical paper dimensions, supplied facts and file
hashes.

Use `python -m oabm.drawings.cover_sheet COVER.json PROFILE.json JOB.json OUTPUT`.
`examples/cover-sheet/input.json` is a synthetic cover request. The shared company
job request identifies the cover sheet and supplies its titleblock values. The
cover index must exactly match the complete shared sheet set, including ordered
sheet IDs and titles. Contradictions fail before an output directory is created.

The shared `company_sheets` tool resolves profile, job and per-sheet values. Cover
composition uses that sheet's `drawing_bounds_mm` and passes its body as a paper
millimetre SVG group to `create_company_sheet_set`. The shared tool produces the
ordered SVG/PDF set with one consistent border, rail and titleblock.

Content wraps within two columns. Capacity errors identify the panel and required
space. Increase the saved paper size or revise the supplied content, then retry.
A successful delivery appears together in a fresh output directory. Supplied
facts remain in `cover-input.json`, including text that wraps across lines.

The synthetic example uses `examples/cover-sheet/job.json` and
`fixtures/company_sheets/v1/profile.json`. Integration tests verify the ordered
SVG/PDF set, physical dimensions, retained inputs and repeatable rebuild.
Independent visual acceptance remains pending.

Body text currently supports selected-font-covered printable Unicode with an SVG request
for DejaVu Sans. PDF font realization remains subject to shared font validation.
Unsupported glyphs produce a field-specific diagnostic before publication,
including the code point and character position. Source input is preserved.
Wrapping uses the shared measured text-width helper, including glyph ink
bearings and its rounding margin. Fontconfig and a readable installed font
are shared runtime requirements.

Large scopes, contact lists, project fields, notes and sheet indexes flow onto
numbered cover continuation pages at the configured text size. The complete
visible index includes those continuation sheets. Content and source offsets
remain in the layout receipts; input text is preserved.

Optional request settings:

```json
"pagination": {
  "continuation_ids": ["G002", "G003", "G004"],
  "continuation_title": "Cover continuation",
  "text_height_mm": 3
}
```

Supply enough IDs for the resulting pages. Alternatively set
`continuation_id_prefix` to generate that prefix followed by the cover page
number. With neither setting, trailing digits in the original cover ID advance;
IDs without trailing digits use their first character plus the page number.
IDs are checked against the complete set, ignoring case. Collisions require a
caller-selected numbering scheme.

For coordinated production, call
`paginate_cover(config, bounds_mm, sheet_index=expanded_index)` before export.
The supplied index includes all other producers' pages and the original cover,
excluding this cover's own continuations. The result exposes ordered `.pages`
with `id`, `title`, `body_svg` and `layout`, plus the complete derived
`.sheet_index` and `.iterations`. Clone the original shared cover template for
the returned pages, use the returned complete index and pass the returned bodies
to shared company composition. Resolution stabilizes the cover page count and
its own index in at most 64 passes; unresolved layouts fail without export.
The standalone command performs this same planning and shared composition.

### Optional reference sections and Apple vicinity map

Supply ordered text lists in `code_basis`, `existing_system`,
`linework_conventions`, and `key_abbreviations`. Each list becomes a numbered
section and continues across sheets without discarding source facts.

The CLI defaults to `--map-mode auto`: project location goes through the
installed OfficeAdmin client to Apple geocoding and the existing Apple snapshot
route. Ambiguous results and material differences between the supplied and resolved full
address require explicit `--map-candidate-index` and
`--map-candidate-confirmation` confirmation. Candidate error responses include
a fingerprint binding the normalized query, Apple provider, exact returned label
and coordinates. A retry must match that fingerprint before imagery acquisition;
an index alone cannot confirm a location. Conservative street
abbreviations, punctuation, and common state abbreviations normalize automatically. Provider fallback and
redirects fail closed. Credentials remain in the existing client and server.
`--map-mode cached` requires a fresh verified private cache; `--offline` or
`--map-mode supplied` preserves the supplied-vector-map workflow. The Python
API defaults to supplied mode.

The full uncropped PNG, Apple attribution, source receipt and SHA256 are retained
through shared `SheetImageAsset` composition. `CoverPage.images` exposes the
typed placements for plan-set coordinators. The current OA snapshot response
lacks verified geographic image bounds: acquired maps are explicitly labeled
VICINITY ONLY and contain no inferred site marker. This limitation concerns
marker placement; address-to-image acquisition works independently.
