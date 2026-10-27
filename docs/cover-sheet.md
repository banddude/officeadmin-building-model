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

Body text currently supports printable ASCII (U+0020-U+007E) with an SVG request
for DejaVu Sans. PDF font realization remains subject to shared font validation.
Unsupported glyphs produce a field-specific diagnostic before publication,
including the code point and character position. Source input is preserved.
Wrapping uses the shared measured text-width helper, including glyph ink
bearings and its rounding margin. Fontconfig and a readable installed font
are shared runtime requirements.
