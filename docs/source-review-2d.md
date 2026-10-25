# Source-registered 2D review export

`exports.source_review_2d.export_source_review_2d` creates a deterministic JSON
manifest and self-contained HTML viewer from explicit symbol runs, original PDF
bytes, source images, trusted journal paths, full-page backgrounds and named
`SourceReviewFrame` declarations. It rebuilds the existing `SymbolReview` through
`build_symbol_review` and revalidates every background against the original PDF.
It makes no provider calls and does not receive or modify a `BuildingModel`.

Each page has an explicit local XY origin in displayed bottom-origin PDF points,
a declared metres-per-point scale, rotation, frame name, rationale and source
citations. An origin and relative level zero are coordinate conventions, not
physical site measurements. The export leaves physical elevation, canonical
level ID, mounting height and inter-page construction registration null. Equal
coordinates on different pages never imply coincident construction objects.
The viewer keeps pages separate, with pan/zoom, mark selection, ID/text search,
work-status filtering and original-versus-reviewed evidence inspection.

`SourceReviewAssertion` binds a separate source interpretation to the complete
original proposal fingerprint, reviewer identity/derivation and supplied source
citations. Original geometry, confidence, unknowns and provenance remain intact.
An agent source-supported/corrected decision cannot grant canonical acceptance,
create quantities or replace the canonical intake contract. Corrected source
classifications are displayed separately from raw proposals.

`PendingSourceSplit` stores a parent binding, description and citations only.
At least two children are required for a split-required parent. Child anchors,
bounds and local XY remain null until separately localized/reviewed; the parent
box is retained as context rather than duplicated into invented devices.
Duplicate candidates and complete extraction coverage remain in the manifest.
Every observation retains a canonical defer action and is quantity-ineligible.

Full-page backgrounds keep their own render policy/version and hashes. When used
with the versioned printable-annotation renderer, a new background may contain
reviewed annotation evidence that the historical inference image omitted. Both
source references stay distinct; this does not retroactively change what the
provider saw. The exporter works with existing source-image APIs and requires no
canonical-schema, proposal, importer, GLB or quantity changes.

The HTML is an offline artifact with embedded PNGs and escaped JSON. It performs
no fetches, writes, acceptance actions or external navigation. Save customer
exports privately. Public tests contain independently authored synthetic PDFs.
