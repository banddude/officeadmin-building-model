# Architectural + electrical PDF convergence

`oabm.importers.pdf_convergence` is Gate D. It joins the already-canonical
outputs of the architectural PDF and electrical PDF lanes into one canonical
`BuildingModel`.

It does not parse PDFs, classify symbols, infer architectural geometry, or
define another model. Those responsibilities remain in
`pdf_architecture`, `pdf_electrical`, and `oabm.model`.

## Input contract

`converge_pdf_models(architecture, electrical)` accepts the canonical outputs
of the two merged PDF lanes.

Before convergence:

- the electrical lane must have explicit page registration;
- its `CoordinateSystem.frame_id` and registered target frame must equal the
  architectural model frame;
- unregistered single-page or incomplete multi-page electrical geometry is
  rejected instead of being overlaid by coincidence;
- IDs and source recognition from both lanes are treated as authoritative input
  identity and are not regenerated.

The architecture lane remains the source for levels, spaces, walls, slabs,
ceilings, openings, and their geometry. The electrical lane remains the source
for recognized equipment, devices, ports, circuits, and electrical evidence.

## Spatial attachment

For every recognized electrical equipment/device, convergence uses the
registered canonical position to resolve architectural attachment:

1. resolve a level;
2. resolve a containing space on that level when unique;
3. only when the electrical source carries a physical host hint, resolve that
   host against architectural geometry.

A single architectural level is sufficient level evidence. With multiple
levels, registered Z and plan containment are reconciled. Conflicting or
multiple candidates remain ambiguity rather than being selected by ordering.

Space containment uses the canonical space footprint.

A `wall` host hint selects the nearest wall on the resolved level only when it
is within the configured distance and not tied with another plausible wall.
`ceiling` and `floor` hints require unique polygon containment in a canonical
ceiling or slab. Unsupported host families, including a `pole` when the
canonical architecture contains no corresponding host object, remain
unresolved.

No host hint means no invented `host_id`.

## Vertical placement

The electrical recognition lane intentionally leaves a source-page position at
the registered page plane. If it carries one explicit mounting height,
convergence places the object at:

`resolved level elevation + mounting height`.

Ambiguous mounting heights are preserved as ambiguity and are not applied.

A uniquely resolved slab or ceiling can supply its host plane. If that plane
conflicts materially with an explicit mounting height, the physical host remains
unresolved instead of silently overriding either source.

Ports preserve their canonical IDs and local offset from their owners. When an
owner moves vertically during convergence, its ports receive the same
translation so circuit references remain valid.

## Provenance, confidence, and ambiguity

All source provenance from both lanes is retained.

A spatially resolved electrical entity receives an additional
`pdf-convergence` provenance record identifying the architectural
level/space/host used. Its confidence is never increased by convergence: the
result is bounded by the electrical recognition confidence and the confidence
of the architectural entities used for attachment.

Unresolved evidence is stored deterministically under
`BuildingModel.attributes.pdf_convergence.ambiguities` and summarized per
electrical entity. Representative codes include:

- `level_unresolved`, `level_ambiguous`, `level_evidence_conflict`;
- `space_unresolved`, `space_ambiguous`;
- `mounting_height_ambiguous`, `mounting_height_without_level`;
- `host_hint_ambiguous`, `wall_host_unresolved`, `wall_host_ambiguous`;
- `ceiling_host_unresolved` / `ceiling_host_ambiguous`;
- `floor_host_unresolved` / `floor_host_ambiguous`;
- `host_vertical_conflict` and `unsupported_host_hint`.

The rule is conservative: ambiguity changes metadata, not canonical identity.

## Determinism and public proof

`fixtures/pdf_convergence/v1/simple-garage-convergence.json` reuses the
existing public-safe synthetic architecture and electrical fixtures and adds
only their shared-frame registration plus known spatial answers.

Regression tests prove:

- registered electrical objects resolve to the canonical level/space and a
  wall-mounted EVSE resolves to the existing canonical wall;
- original electrical entity/port/circuit IDs and references survive;
- source provenance and confidence are preserved/conservatively combined;
- input collection ordering does not affect canonical JSON;
- unregistered electrical geometry is rejected;
- equally plausible wall hosts remain explicit ambiguity.

## Electrical sheet registration (#104)

`register_electrical_sheets(architecture, architecture_source, electrical_source)`
proposes each electrical page's transform into the canonical frame of one
**resolved** architectural drawing region (#103). `architecture_source` must be
the sheet observations that produced `architecture` (checked by content hash);
`electrical_source` is the same vector/text/layer extraction applied to the
electrical PDF. `register_electrical_pdf(...)` extracts both from paths. Neither
model is modified.

When both documents have the same SHA-256 and identical observations for a
page, that page can reuse its uniquely resolved whole-sheet architectural
frame. This records `shared_source_page` evidence, zero sheet translation,
the source digest and the target frame's confidence. Its declared scale must
agree with the frame. Bounded drawings, competing frames, changed observations
and different source digests continue through geometric registration.

Evidence is deterministic and compared like with like:

- **wall vectors**: visible `A-WALL`/`AE-WALL` segments (xref prefixes such as
  `xref_Floor Plan|A-Wall` included) on both sheets when both have them,
  otherwise paired wall faces on both sheets. Pairs made only of curve segments
  (grid or keynote circles) are not wall evidence;
- **grid bubbles**: a short label (`A`, `B`, `1`, `2.1`) enclosed by a drawn
  ring. A label drawn twice on one sheet is dropped.

Matching uses the ratio of the two printed scales and no rotation. Translation
candidates come from same-orientation, same-length segment pairs; each candidate
is verified by counting electrical segments whose endpoints both land on one
architectural segment within `tolerance_m` (default 0.05 m), then refined. The
wall matcher (voting, verification, uniqueness, and the orientation guard) lives
in `oabm.importers.pdf_architecture.wall_registration` and is shared with the
architecture importer, which registers later architectural sheets to resolved
same-level regions with it (#72).

A geometric registration is accepted only when all of these hold:

- at least `min_inliers` (8) matched segments and `min_coverage` (30%) of the
  page's wall evidence;
- residual RMS within `max_residual_m` (0.05 m);
- inliers spread at least `min_span_m` (3 m) and a quarter of the target's
  extent in both axes;
- no second translation with nearly as many valid matches;
- no mirrored or rotated placement (mirror × four quarter turns) explains as
  many electrical wall segments as the identity placement. Symmetric walls match
  a flipped sheet almost as well as the true one, so this is checked before
  accepting;
- when at least two grid labels are shared, they land on their architectural
  bubbles under the wall placement. One stray label is tolerated when at least
  two others agree, for example a keynote tag that happens to share a grid
  label;
- when the electrical drawing prints one level name and the target level is
  named, the names agree. "Second Floor", "2nd Floor" and "2" are the same;
- every region that accepts the page gives the same level and the same canonical
  placement.

Grid labels register a page on their own when walls are absent or too weak and
at least two shared labels agree. Grid labels that agree with the wall placement
also resolve walls that are symmetric under a mirror or turn.

Otherwise the page stays `registration_pending` with a stable reason:

- `scale_unresolved`, `no_resolved_architectural_region`;
- `missing_registration_evidence`, which also carries a bounded question for a
  later #98 vision task (nothing is inferred);
- `insufficient_matched_evidence`, `evidence_clustered`, `excessive_residual`,
  `grid_labels_inconsistent`, `registration_methods_disagree`;
- `competing_transforms`: repeated geometry inside one region. With the
  optional phase-correlation cross-check below, a near-tie the global mask
  settles becomes an accepted tie-break instead, and a tie the global mask
  cannot settle gains `phase_correlation_inconclusive`;
- `ambiguous_orientation`: a mirrored or turned placement explains exactly as
  many wall segments and no grid labels settle it;
- `level_name_mismatch`: the electrical drawing names a different level;
- `competing_targets`: several regions or levels accept the page with different
  placements. The choice is never made by page order;
- `orientation_incompatible`: a mirrored or turned placement explains more wall
  segments than the identity, or is the only one that matches. The matching
  configuration is reported in `diagnostics`, never applied;
- `scale_incompatible`: the walls match only at a scale other than the printed
  ratio. This is a diagnostic only, never applied;
- `drawing_regions_overlap`: the extents of two drawings on one page overlap, so
  a point cannot be assigned to one drawing;
- `phase_correlation_disagrees`: the accepted placement survived every wall and
  grid check, but the optional global mask places the page confidently
  elsewhere. The evidence is unresolved, not wrong, so the page stays pending.

### Optional phase-correlation cross-check

`SheetRegistrationOptions(phase_correlation=True)` enables an independent,
global translation estimate: the electrical drawing's wall segments, scaled by
the same printed-scale ratio the candidates use, are registered onto the
architecture region's wall segments by whole-mask phase correlation
(`oabm.importers.pdf_convergence.mask_registration`, OpenCV's
`phaseCorrelate` — the optional `registration` extra; enabling the option
without it raises the extra's `ImportError`). Repetitive commercial plans
often leave two wall translations nearly tied; the mask's correlation peak is
evidence neither candidate has.

For every registration decision — the page-level one and each per-drawing
one — the option runs once against the would-be placement and records

```
phase_correlation: {dx_pt, dy_pt, peak, converged, agrees_with}
```

in the page or drawing record (`dx_pt`/`dy_pt` in architecture sheet points,
`peak` the correlation response, `agrees_with` the candidates-list index of
the candidate it agreed with or `null`). Then:

- **One accepted candidate.** Converged (peak at or above
  `phase_correlation_min_peak`, default 0.10) and the shift lands within
  `phase_correlation_agree_tol_m` (default 0.10 m, compared in model metres
  through the architecture scale) of the accepted translation: the
  registration stands and the agreement is recorded. Converged and outside
  the tolerance: the page is refused with `phase_correlation_disagrees` —
  a wrong accept is worse than a pending page.
- **A refusal of `competing_transforms` only** (the wall matcher's
  `competing_ratio` near-tie), converged, with exactly one of the two
  contested translations (the best wall match and its recorded runner-up)
  inside the tolerance: that candidate is accepted as a tie-break, with
  method `wall_vectors_phase_correlation_tiebreak` (confidence 0.75) and
  `tie_break: true` in the registration record. Zero or several agreeing
  contested translations keep the refusal and add
  `phase_correlation_inconclusive`.
- **Not converged**, and every other refusal: no change to the decision;
  only the diagnostics are recorded.

With the option off (the default) no correlation runs and every record is
byte-identical to a run without it. The shift never proposes a placement of
its own: it only confirms, breaks near-ties, or vetoes.

**A page holding several drawings** (#72), for example two floor plans side by
side, is split the way the architecture importer splits a sheet (#103). Each
drawing is registered on its own evidence:

- its walls;
- grid bubbles inside its extents;
- level names printed with it.

The rules above apply per drawing. A drawing may register to a different level
from the others on its page.

The page is `registered` only when **every** drawing registers and their extents
do not overlap. It then has no page transform. It has one
`DrawingRegionTransform` per drawing instead: the drawing's scope (its wall
extents plus the #103 annotation margin, in displayed page points) and that
drawing's `PdfPageTransform`. The page record has `registration_mode:
per_drawing` and one entry per drawing in `drawings`, with that drawing's
candidates and registration. If any drawing is refused, the page stays pending
with that drawing's reasons.

A registered page's `PdfPageTransform` composes electrical sheet point →
architectural sheet point → canonical frame. It targets the model `frame_id`,
sets `z_m` to the target level's elevation, and carries its `registration`
record:

- method and `derivation: inferred`;
- target region, page, and level;
- scale ratio and translation;
- inlier count, coverage, span, and residual;
- a sample of matched source element IDs;
- confidence: the lower of the region's confidence and the method's (0.95 for
  walls and grids together, 0.85 walls, 0.80 grids, 0.75 wall vectors with a
  phase-correlation tie-break).

The electrical importer records that registration as an `inferred` model
provenance entry per page, while device source positions remain observed.

`ElectricalSheetRegistration.page_transforms()` returns transforms only when
**every** page is registered. A single-drawing page maps to its
`PdfPageTransform`, and a multi-drawing page to its tuple of
`DrawingRegionTransform`. Otherwise it returns `None`, the electrical import
stays `registration_pending`, and `converge_pdf_models` refuses it. One page's
transform is never offered for another page or a partial document.

The electrical importer places each recognized point on a multi-drawing page with
the transform of the unique drawing whose extents contain it. A point outside
every drawing, or inside two, cannot be placed. The whole document then stays
`registration_pending` (`registration_mode: drawing-region-transforms-unresolved`,
with the unplaced points listed in `drawing_region_assignment`), and convergence
refuses it. When every point is placed, convergence accepts the document like
any registered one; each device carries its `drawing_region_bbox_pt`, and its Z
comes from its own drawing's level.

These placements appear under the `pdf_electrical` lane keys:
`registration_mode: explicit-page-and-drawing-transforms` marks a document
placed by supplied page and per-drawing transforms, and
`drawing_region_transforms` lists, per page, each drawing's scope and
`PdfPageTransform` as `DrawingRegionTransform.to_attributes()`.
`page_transforms_supplied` records that the caller supplied transforms even
when the per-drawing assignment later fell back to best effort.

### Ring geometry and font offsets (#248)

Grid-bubble observations use the enclosing ring's geometric center, rather
than assuming the font bounding-box center is the ring center. The source
helper extracts closed non-branching endpoint cycles after pruning dangling
leaders, checks circularity and containment, and refuses competing rings or
repeated labels in the selected drawing scope. Four cubic cardinal endpoints
are supported; straight polygon rings require at least eight vertices so a
square box is not promoted to a circle. Open, filled, dashed, elliptical and
branching outlines are not accepted.

This changes source evidence only. Existing scale, span, mirror, competing
target and wall/grid consistency checks still govern registration. A short
label inside a ring is not by itself proof of a structural grid axis, and ring
centers must not be used to align disjoint architectural drawings whose labels
can sit at different positions along the same axis.
