# Opt-in saved physical-sheet delivery

This is a generated artifact package, not an OfficeAdmin upload, interactive
model editor, engineering approval, or permit/construction drawing set.
Existing saved manifest versions and legacy review drawing bytes are unchanged.
A `review_drawings` reference may now explicitly reference a
`review-sheet-delivery-v1` configuration instead of the legacy
`review-electrical-drawings-v1` configuration.

## Explicit input

`ReviewSheetDeliveryConfig` in `oabm.pipeline.review_sheet_delivery` contains:

- `version`: exactly `review-sheet-delivery-v1`;
- `drawings`: the unchanged `ReviewDrawingConfig`, including explicit plans,
  model bounds, cut/depth intervals, scale, visibility and review render density;
- `sheets`: one to 32 `ReviewSheet` entries, each with `SheetSpec` paper,
  one to 64 `ViewPlacement` entries, and unique requested `svg`/`dxf` formats;
- `glyphs`: an explicit tuple of `ReviewGlyph(token, segments_mm)` definitions.

Use the typed constructors and `to_json()` to write a saved configuration.
Update the enclosing saved manifest's SHA-256 reference when changing it.
Paper titles must equal `INCOMPLETE REVIEW - NOT FOR PERMIT OR CONSTRUCTION`
and reserve at least 25 mm for the titleblock; renderers additionally reject
insufficient text capacity. The revision and supplied fields remain explicit.
No automatic paper size, fit-to-page scale, standard glyph, or electrical design
fact is invented. Existing model and view identity stays authoritative.

## Supplied symbol presentation

Glyph segment coordinates are paper millimetres relative to the original symbol
anchor, +X right and +Y down, with **no rotation**. They are caller-supplied marks,
not automatic engineering conventions. Their fixed physical size is converted
to the derived view's model coordinates using that view's explicit scale.
Deterministic new line IDs retain original canonical source IDs and metadata;
the package records original symbol/token-to-line joins. Unknown tokens, symbol
text without explicit placement, collapsed segments, and out-of-view glyphs
fail the export rather than disappearing. Original symbolic drawings remain in
the package. Unplaced views are retained but are not required to resolve glyphs.
There is no canonical writeback or reverse import contract.

## Outputs and verification

The existing saved runner publishes the additional files together with its
normal delivery via the same exclusive attempt and atomic directory publication:

- requested physical SVG/DXF files and their source-map JSON companions;
- `sheet-delivery.json`, binding canonical model hash, configuration fingerprint,
  original and rendered drawing hashes, review diagnostics, glyph joins and the
  exact archive member inventory;
- `generated-sheet-sources.zip`, a genuine ZIP with sorted member names, fixed
  timestamps, permissions and stored (uncompressed) bytes for deterministic replay.

The ZIP includes only generated drawings, generated panel facts, the explicit
sheet configuration, review diagnostics and export companions. Original PDFs,
images, proposal journals and evidence documents are not copied into it. This
is **not anonymization**: generated labels, supplied fields and provenance can
still be project-private and must retain their authorized destination/visibility.

The original drawing document retains complete owner-relative provenance and
confidence, including optional qualifiers/attributes. Generic exporter maps are
presentation joins, not a replacement for that source index or canonical model.
The package preserves omitted/clipped electrical IDs and unadmitted proposals;
source coverage remains unavailable and physical completeness unverified.
Panel schedules remain generated JSON facts, not automatically laid-out tables.

`COMPLETED.json` binds exact persisted bytes, including the ZIP. Saved revision
and job reuse re-generate all requested drawing artifacts from the retained
canonical snapshot/config and compare exact bytes, so changing hashes alone does
not admit stale, mixed or tampered sheets. Missing/partial artifacts and failed
exports are not delivery success. A configuration change creates a new export
attempt while reusing already-completed provider work when its inputs are unchanged.
Old deliveries remain immutable. A caller is responsible for preserving the
trusted completion/manifest roots; hashes alone are not signatures.

## Dependencies and consumption boundary

SVG uses no external rasterizer. DXF explicitly requires the existing optional
`cad` extra and fails when unavailable; it is not silently omitted. No PNG/PDF
preview is generated in this increment. An explicitly installed and verified
renderer plus visual QA is required for any later preview step. Text extents,
collisions, fonts and client display remain review concerns.

The genuine archive can be consumed through an application's existing authorized
artifact upload/download flow. This implementation performs no app upload,
changes no MIME/security/visibility policy, and does not expand the strict
OfficeAdmin review-bundle contract. App acceptance is a separate step.
