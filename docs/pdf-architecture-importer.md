# Architectural PDF importer

`oabm.importers.pdf_architecture` converts architectural plan PDFs into the canonical `oabm.model` v1 building objects. It does not define a second semantic model and it does not interpret electrical content.

## Public API

```python
from oabm.importers.pdf_architecture import (
    ImportOptions,
    LevelOverride,
    RegistrationHint,
    ScaleOverride,
    import_architectural_pdf,
)

model = import_architectural_pdf(
    "plans.pdf",
    source_id="architectural-set:revision-c",
)
```

Pass a stable logical `source_id` when canonical IDs must survive replacement PDF bytes. The importer records the file SHA-256 separately for traceability. If `source_id` is omitted, the file stem is used as the logical document identity.

## What is promoted

The current deterministic lane reads vector/text PDF primitives and can promote:

- architectural plan sheets, while explicitly ignoring sheets classified as electrical or unrelated;
- an explicit title-block drawing title takes precedence over incidental plan words in notes: interior elevations, details, and millwork sheets are not promoted as floor plans;
- printed imperial scales and `1:N` scales;
- two-point registrations that establish scale, plan rotation, and translation in the canonical XY frame;
- named levels with explicit elevations, or one first/sole local datum at `Z=0` when the source gives no project elevation;
- labelled rectangular spaces bounded by paired vector wall rectangles;
- room labels assigned by text position inside a closed wall loop: dimensions, explicit keynote/note strings, detected leader tags, and recognized title-block strings are excluded first; sheet numbers (`C4.7`), bare scale strings, numbered note entries, sentence-like notes, and single characters are also not eligible, and a two-digit bare number stays eligible only when a plausible name label sits close enough to pair with it, so a stray digit never becomes a room on its own; only after candidates are scoped to one closed enclosure are adjacent room-number/name lines paired, so labels cannot pair across neighboring enclosures; remaining candidates are ranked by enclosure centrality, font size relative to the page median, and room-number pattern; runner-ups are retained in provenance and a `multiple_room_labels_in_enclosure` ambiguity is emitted only when the top two scores are within the small ranking margin; numeric labels and project-specific abbreviations remain supported without ROOM:/SPACE: prefixes; each region records one deterministic `room_label_candidates_rejected` entry counting the candidate texts it did not use, by reason;
- labelled rectangular spaces and walls from untagged ordinary vector lines when two closed axis-aligned wall-face loops prove one enclosure around a unique eligible text label;
- walls from untagged parallel wall faces paired by geometry rather than PDF native IDs: dimension-line evidence and hatch/fill fields are excluded before collinear joining and pairing; remaining line/polyline/curve-derived segments and dashed runs stay eligible, and the source-space gap gate is derived from the resolved sheet scale for a 2 in to 18 in real-world wall-face spacing; unambiguous pairs in a closed loop keep the existing wall/Space behavior, while a uniquely paired face that does not close an enclosure may emit a lower-confidence partial wall only when its scale-derived face span is at least 24 in and an endpoint has a nonparallel corner/junction with another candidate face;
- optional PDF CAD layer names and the default optional-content visibility state are retained on vector source observations. Hidden objects are excluded before text, line, and rectangle extraction; this also prevents an off-layer wall name from contaminating identical visible geometry. When an explicit visible `A-WALL` or `AE-WALL` layer has enough line evidence, geometric wall-face pairing uses that layer first, while preserving the existing length, junction, thickness, scale, and height checks. If it produces no pairs, ordinary vector evidence remains the fallback unless a hidden wall layer makes that fallback ambiguous. Duplicate visible source geometry retains every layer name. Layer evidence can support partial walls; it does not by itself prove a closed room, an inter-sheet registration, or an observed wall height;
- AutoCAD SHX-font text reaches the lane as text observations. SHX strings are drawn as vector strokes, and AutoCAD's PDF export adds one invisible `/Square` annotation per string carrying the string and its outline; `extract_pdf` reads those annotations into displayed, bottom-origin `PdfTextObservation`s with the page `/Rotate` applied, so SHX-drawn drawing titles, room names, and notes classify sheets and name levels like real text. Only annotations authored by CAD SHX text are accepted: reviewer markups, hidden annotations, and annotations whose optional-content group defaults to OFF are ignored, and the annotation author string is never copied into an observation or the model;
- a lower-confidence 2D `Space` when visible `A-WALL`/`AE-WALL` vectors bound exactly one room label and a visible door/window layer supports both ends of each needed opening closure. The sheet-local interior is rasterized at one PDF point per pixel and traced into a source polygon; closure widths are scale-gated. Exterior-connected, multiply labelled, holed, oversized, and near-page-frame regions are rejected. The polygon is marked inferred from observed vectors with wall and opening source-element provenance. Wall thickness, room height, 3D walls, and ceilings remain unresolved;
- a conservative 2D-only space from one unique closed ordinary-vector boundary around a unique room label when no partial paired wall-face evidence is present; wall thickness and walls remain unresolved rather than being invented;
- walls from those paired boundaries when a supported height is present;
- geometric wall-pair identity is anchored by rounded canonical centerline endpoints plus stable sheet and level anchors; source element/native IDs remain provenance only;
- marked door/window openings with dimensions when the host wall and vertical placement are supported;
- slabs only when an explicit floor/slab thickness is present;
- ceiling surfaces when an explicit or caller-supplied room/level height is present.

All semantic output is canonical `Level`, `Space`, `Wall`, `Opening`, `Slab`, `Ceiling`, and `BuildingModel` data. Electrical devices/equipment, routes, IFC objects, quantities, and derived drawings are outside this lane.

## Scale and registration

The first architectural plan that resolves level and scale and actually emits supported canonical spatial geometry may define the project-local XY origin. A resolved plan that emits no supported spatial geometry is recorded as `no_supported_geometry_recognized` with `architectural_geometry_unrecognized`; it does not claim the shared frame. Once a base geometry page exists, an explicit `RegistrationHint` remains authoritative when supplied.

Without a hint, a later sheet or drawing region is first **registered to an already-resolved region of the same level by shared walls** (#72). It uses the #104 wall matcher from `wall_registration.py`, shared with electrical sheet registration, under the same default thresholds:

- walls compared like with like: visible wall-layer segments on both sheets when both have them, otherwise paired wall faces on both;
- the ratio of the two printed scales and no rotation; candidate translations from same-orientation, same-length segment pairs, verified by endpoint matches within 0.05 m;
- at least 8 matched segments and 30% of the sheet's wall evidence, residual RMS within 0.05 m, and inliers spread at least 3 m and a quarter of the target's extent in both axes;
- no second translation with nearly as many valid matches (`competing_transforms`), and no mirrored or turned placement that explains as many wall segments (`orientation_incompatible`, `ambiguous_orientation`);
- only one source drawing in the compared evidence (`multiple_drawing_regions_on_page` otherwise);
- every same-level region that accepts the sheet must place it identically in the canonical frame; otherwise nothing is chosen (`competing_targets`), never by page order or confidence.

An accepted sheet gets frame basis `registered_to_region`, its transform composed from sheet points to the target's sheet points to the canonical frame, and an `inferred` provenance entry. Its confidence is the lower of the target frame's confidence and the wall method's 0.85. The attempt, accepted or refused, is kept in the region's `shared_wall_registration` record with one candidate per target region and its reason codes. Only regions of the same resolved level are candidates; no attempt is made when none exists.

**A wall is materialized once**, however many drawing regions draw it. Registering later sheets to one frame means a plan set that draws a level several times (floor plan, reflected ceiling plan, power plan) places the same walls on the same canonical spot. Before a region's wall is emitted it is compared with the walls other regions of the same level already emitted. It is a repeat when both centerline endpoints correspond: within the wall matcher's 0.05 m endpoint tolerance across the wall, and within the wall thickness along it, because a closed-loop wall ends at the loop vertex and a partial one at its faces' ends. Two real walls cannot overlap that closely.

Merging never crosses frames that rest on different evidence. Each resolved region's frame records its evidence roots: `project_origin` and an explicit two-point registration claim the project root, a `registered_to_region` frame claims its agreeing targets' roots, and a `sheet_geometry_fallback` frame claims only the region itself — the fallback anchors every sheet's largest wall loop at the same canonical spot, so unrelated fallback sheets coincide there by construction and their frames share no evidence. Walls and footprints are shared only between regions whose roots overlap, and each region record lists its roots as `frame.evidence_roots`.

- A repeated wall is not emitted again. The earlier wall keeps its stable id, geometry, confidence, and first provenance. It gains the repeating region's geometric observation as an extra provenance entry, tagged `drawing_region_id` and `repeats_drawing_region_id`.
- The region records `duplicate_wall_identity_across_pages` with the kept `wall_ids` and the `source_region_ids`, and lists those regions in `repeated_geometry_region_ids`. Its record carries `repeated_wall_count`.
- A repeat whose thickness or height differs from the kept wall by more than 0.05 m is recorded as `repeated_wall_dimension_conflict`. The earlier wall is kept unchanged.
- Openings on the repeating sheet are hosted by the kept walls. A space the region emits names the kept wall ids. A geometric wall-loop space is not emitted twice: one whose footprint equals an existing space's on the level is skipped exactly as before, and one that lands within 0.05 m of a space whose frame shares evidence with its own is skipped too. A room kept on the footprint of an evidence-linked earlier room under another name is recorded as `repeated_space_name_conflict`; the named-room identity rule that records `duplicate_room_identity_across_pages` is unchanged.
- A region whose every wall repeats earlier walls is still `resolved`: its frame put its walls on the existing ones, so it is an agreeing target for electrical sheet registration.
- Walls that only resemble an emitted wall stay distinct and are emitted: the same plan on another level, a parallel wall beside it, and a collinear run that is longer or shifted beyond the thickness. The across and along bounds each keep their own lookalikes out: a same-extent parallel wall offset only across the wall, and a shared-end wall that only runs longer, both stay distinct.

This compares canonical geometry, not ids. Wall ids include the sheet anchor, so the same wall drawn on two sheets gets a different id on each; when two sheets print the same sheet number, the ids are identical instead.

If the registration is refused, the sheet keeps exactly its previous behavior. An additional CAD-vector page on an unsplit sheet may then use a lower-confidence sheet-geometry fallback: the importer prefers the largest proven closed wall-loop bounding box and otherwise uses non-title-block drawing extents, anchoring that geometry's lower-left at the project-local origin. Title-block vector geometry identified around explicit sheet/drawing/project metadata is excluded. The fallback method, confidence, source bounding box, and anchor are retained in page metadata and model provenance. Pages without enough vector geometry remain `registration_unresolved`.

A two-point registration controls scale, rotation, and translation. If its computed scale disagrees with a printed or overridden scale beyond `scale_registration_tolerance`, the page is skipped and the disagreement is recorded rather than choosing one silently.

`ScaleOverride` is the explicit escape hatch for sheets that are not to scale or have unsupported/missing scale annotations.

## Drawing regions (#103)

Level and frame are resolved per **drawing region**, not per sheet. Before any level or geometry decision, each architectural sheet is checked for separately drawn plans:

- Wall evidence locates the drawings: visible `A-WALL`/`AE-WALL` layer segments when the sheet has at least eight; otherwise, at a sheet scale that is unambiguous from text or a page-scoped `ScaleOverride`, paired wall faces and nested rectangle pairs whose insets are wall-thickness gaps. Title-block lines and long segments along the media edges are excluded, so a sheet border or title block never locates a drawing.
- Evidence closer than 3 m (and at least 1 in of paper) joins one cluster. A cluster counts as a drawing only with enough segments, a 4 m span, 12 m of wall evidence, and at least a quarter of the largest cluster's wall length. Legend swatches and wall-type keys therefore cannot pose as a second plan.
- A sheet with zero or one qualifying drawing stays **one sheet-scope region** and keeps the page-level behavior described below, including the sheet-geometry registration fallback.
- A sheet with two or more drawings is split. Each region sees only the vectors inside its own extents plus a small margin, and only the text that lies uniquely nearest to it (title-block text and text between drawings stay sheet-level).

For a split sheet, every region must resolve on its own evidence:

- **Level:** a level name printed with that drawing (`LEVEL: 2`, `SECOND FLOOR PLAN`, `EXISTING THIRD FLOOR POWER PLAN`), or a `LevelOverride` whose `region_point_pt` lies inside the drawing. Sheet-level level text is never shared by several drawings. A page-scoped `LevelOverride` does not say which drawing it means and is not applied (`level_override_region_unresolved`).
- **Scale:** a region-scoped or page-scoped `ScaleOverride`, a scale printed with the drawing, or, failing those, an unambiguous sheet-level scale note, which is recorded as inherited at slightly lower confidence. A region's own two-point registration also establishes its scale.
- **Frame:** the first region anywhere in the set that emits geometry defines the project-local origin, as the first page does today. Every other region on a split sheet needs a `RegistrationHint` with `region_point_pt` inside it, or registration by shared walls to an already-resolved region of its own level (see Scale and registration); the sheet-geometry fallback is not used on split sheets, because the drawing extents it anchors on are exactly what differ between floors. A page-scoped hint is not applied (`registration_hint_region_unresolved`).
- Two drawings on one sheet that name the same level are **competing** and neither is promoted (`drawing_regions_share_level`). When their wall evidence is congruent under translation, `drawing_region_geometry_repeated` is added. Repeated geometry with distinct explicit levels (typical floors) is recorded in `repeated_geometry_region_ids` but does not block.

A level name is taken only from level/drawing-title text, never from a note that mentions a floor. A floor plan title may carry a short qualifier after the word PLAN (`SECOND FLOOR PLAN - UNIT A`, `: AREA A`, ` (NORTH)`, at most three words); the text must start with the floor designation, so `SEE SECOND FLOOR FRAMING FOR BLOCKING` is not a level name, and a qualifier without PLAN (`THIRD FLOOR, TYP.`) reads as a note, not a level name. A drawing (or unsplit sheet) that names more than one distinct level is `level_ambiguous` and is not promoted, instead of taking the first name found.

`BuildingModel.attributes["pdf_architecture"]["drawing_regions"]` lists one record per region, in page and position order:

- `region_id` (stable: logical source, page, and rounded source extents; `...|sheet` for an unsplit sheet), `page`, `index`, `scope` (`sheet` or `region`), `source_bbox_pt`, and the wall `evidence` kind and segment count;
- `status`: `resolved` (level, scale, and frame resolved and canonical geometry emitted, or every wall a repeat of already-emitted walls), `no_supported_geometry`, or `unresolved`, with sorted `reason_codes` such as `level_unresolved`, `level_ambiguous`, `level_elevation_unresolved`, `scale_unresolved`, `scale_conflict`, `registration_unresolved`, `scale_registration_conflict`, `drawing_regions_share_level`, `drawing_region_geometry_repeated`, `drawing_regions_overlap`, and `architectural_geometry_unrecognized`;
- `level`: canonical level ID, name, elevation, elevation method, and the source text element IDs for name and elevation;
- `scale`: metres per point, method, source text, and source element ID;
- `frame` (only when `resolved`): target `frame_id`, `basis` (`project_origin`, `explicit_registration`, `registered_to_region`, or `sheet_geometry_fallback`), method, confidence, scale, rotation, and translation from sheet points into the canonical frame; a `registered_to_region` frame additionally records `registered_to_region` with the target region id, page, and frame basis, the agreeing region ids, the evidence kind, scale ratio and translation, the matched wall evidence (inliers, coverage, residual, span, sample), and its confidence;
- `shared_wall_registration` (only when a same-level registration was attempted): `status` (`registered` or `refused`), `reason_codes`, `competing_region_ids` for `competing_targets`, and one candidate per target region with its reason codes, inlier count, coverage, residual, span, competing translation, and strongest mirrored or turned alternative;
- `confidence`: the lowest of level, scale, and frame confidence, and `entity_counts` (entities this region emitted); `repeated_geometry_region_ids`, and `repeated_wall_count` when walls repeated earlier regions' walls.

Page records list their `drawing_region_ids` and the `drawing_region_detection` evidence summary. `sheet_wall_evidence`, `region_wall_evidence`, and `printed_sheet_scale` expose the same read-only source evidence for electrical sheet registration (#104); `use_wall_layers=False` compares a layered sheet with a flattened one on paired wall faces. Ambiguities raised while resolving a region of a split sheet carry its `drawing_region_id`. Electrical registration (#104) consumes resolved regions only.

## Levels and 3D values

Level elevation and level-wide height evidence is reconciled across all architectural plan pages before geometry is materialized. Explicit source evidence upgrades an earlier local-datum/default assumption, and a later `LevelOverride` is authoritative over parsed or assumed values. Conflicting equally authoritative level evidence is retained as an ambiguity, lowers the retained level confidence, and the conflicting page is not materialized until an override resolves it. Conflict diagnostics retain the competing page, value, source text, and source element ID when available. Level provenance points at the page that actually supplies the selected elevation and, when different, the page that supplies the selected level height.

If the first/sole level has no elevation, the importer may establish a project-local `Z=0` datum with reduced confidence. A later distinct level without an elevation relative to known levels is not positioned and is skipped until a `LevelOverride` is supplied.

Ceiling-height annotations are spatially scoped. A note placed inside a resolved room enclosure or explicitly naming one room applies only to that room's `Space.height_m`, wall heights, and ceiling Z. An otherwise-unqualified note can be scoped to a room when exactly one resolved room exists on the page; it is never promoted to the level merely because it sits outside room geometry. Only text that explicitly states a level/floor/typical global ceiling height can supply parsed level-wide height evidence. Multiple distinct room heights on the same level therefore remain independent instead of being collapsed into one story height. `LevelOverride.height_m` is authoritative over parsed heights, while a parsed room height overrides a same/lower-priority global/default height for that room. An explicitly supplied `ImportOptions.default_wall_height_m` remains a low-confidence level-wide input. For vector-geometry pages with no explicit/caller-supplied height, #44 may retain a 9 ft (2.7432 m) level default at `assumed_value_confidence` solely so proven geometric wall-face loops can materialize; provenance and the `level_height_default_assumed` diagnostic make that assumption explicit and later higher-priority evidence replaces it. The conservative single-boundary 2D fallback still never invents walls or a ceiling.

## Stable identity

Canonical IDs use `oabm.model.stable_id` with semantic anchors, never list positions or mutable geometry. Examples include logical source + level + room label + boundary side, and logical source + sheet identifier + PDF-native MCIDs. For ordinary untagged vector enclosures, contributing line element IDs are retained only as provenance; the canonical room/wall IDs remain anchored by logical source, level, room, and wall side. For #44 geometric wall-face loops, canonical wall IDs use logical source + printed sheet anchor + level + rounded canonical centerline endpoints, and loop Space IDs use the ordered set of those geometric wall anchors. Slice 3 room-label association does not replace those geometry-derived IDs; the label text, source element, method, and confidence are attached as semantics/provenance. Extraction order, `native_id`, and whether a label is present therefore do not control the underlying geometric identity. Repeated semantic anchors that would collide are preserved as ambiguity and later geometry is not silently substituted.

## Ambiguity and provenance

Every promoted entity carries confidence and `Provenance(source_kind="architectural_pdf", ...)`. Source-specific details and unresolved decisions are retained under `BuildingModel.attributes["pdf_architecture"]`, including per-page classification/scale/registration metadata and an ordered `ambiguities` list.

Typical ambiguity codes include:

- `scale_unresolved` / `scale_conflict`;
- `scale_registration_conflict` / `registration_unresolved`;
- `architectural_geometry_unrecognized`;
- `ordinary_vector_enclosure_unresolved` / `ordinary_vector_enclosure_ambiguous`;
- `level_ambiguous` / `level_unresolved` / `level_override_region_unresolved`;
- `drawing_regions_share_level` / `drawing_regions_overlap` and the hint codes `scale_override_region_unresolved` / `registration_hint_region_unresolved`;
- `level_elevation_local_datum` / `level_elevation_unresolved`;
- `level_elevation_reconciled` / `level_elevation_conflict`;
- `level_height_reconciled` / `level_height_conflict` / `level_height_default_assumed`;
- `ceiling_height_scope_unresolved` / `room_ceiling_height_conflict`;
- `wall_height_unresolved`;
- `layered_room_3d_extent_unresolved`;
- `duplicate_room_label` / `duplicate_room_identity_across_pages`;
- `room_label_candidates_rejected` (counts of candidate texts not plausible as room labels, by reason; informational, never blocking);
- `opening_host_unresolved`, `opening_identity_unresolved`, and `window_vertical_position_unresolved`.

The ordinary-vector fallback is intentionally narrow: only complete axis-aligned four-line loops are considered. Near-page rectangular sheet frames are rejected before room or wall promotion, whether represented as rectangles, ordinary vectors, or paired wall faces; the page records `sheet_frame_enclosure_rejected`. A finish note containing the word `LEVEL` does not establish a building level: level names need an explicit level label or supported floor designation. Geometric wall-face recognition first removes dimension evidence (nearby dimension-pattern text or endpoint tick/arrow/extension geometry) and hatch evidence (short regular-pitch parallel fields or short strokes bounded by filled regions), then joins near-collinear fragments and applies deterministic parallelism, overlap, and scale-derived 2 in to 18 in spacing gates. Per-page `geometric_wall_pair_diagnostics` records source primitive-family counts, dashed/filled input counts, dimension/hatch rejection counts, joined-run counts, the wall-gap histogram, accepted pairs, and rejection counts for parallelism, overlap, gap range, minimum length, pairing ambiguity, and unsupported partial-wall evidence; no layer/color gate is claimed when that source evidence is unavailable. An unambiguous open pair is retained as a lower-confidence partial wall only when it spans at least 24 in at the resolved sheet scale and has a nonparallel candidate-face corner/junction at an endpoint; isolated or shorter pairs remain unresolved, and tied best-pair evidence remains fail-closed. If paired wall faces are unavailable, exactly one sufficiently large closed loop may support only a 2D Space around a unique room label; any supported inset wall-face side is treated as evidence of an incomplete pair and the single-loop fallback fails closed. Open or competing enclosures remain unresolved instead of being selected by extraction order.

The visible-layer room path is independent of wall-face pairing. It uses explicit wall and opening layers only, closes a wall-face gap only when opening vectors touch both gap ends, and preserves the resulting polygon as an inferred 2D interior. A closed room does not prove the position or thickness of its 3D walls. It also does not register separate sheets; coordinates remain subject to the importer's ordinary registration checks. Distinct floor-level notes or two substantial separated wall drawings on one sheet withhold room promotion with `multiple_layered_drawing_regions_unresolved` until each drawing has its own supported level and registration frame. Sheets whose drawings are separated enough to split are resolved drawing by drawing instead (see Drawing regions); the guard remains for sheets that do not split.

Text extraction also keeps source observations local: words sharing a text baseline are split when a large horizontal gap indicates separate plan annotations. This prevents a room label from being fused with an unrelated distant dimension or keynote while preserving stable text-observation IDs for unchanged local labels.

The rule is conservative: unresolved facts remain unresolved instead of being converted into precise-looking canonical geometry.

## Fixtures and tests

`fixtures/pdf_architecture/v1/simple-floor-plan.pdf` is synthetic and public-safe, and its known-answer canonical model is checked in alongside it. `fixtures/pdf_architecture/v1/cad-export-scale-no-registration.pdf` is the #51 two-page CAD-style source-only fixture: both pages carry printed scale/level text, the second page has no registration cue, and an offset synthetic title block proves the geometry fallback does not anchor on title-block extents; it has no expected-output companion. `fixtures/pdf_architecture/v1/ordinary-vector-room.json` is a tiny synthetic observation fixture for the ordinary untagged line-loop family. `fixtures/pdf_architecture/v1/cad-derived-wall-faces.json` is a geometry-only, public-safe derivative for #44 with no native IDs or customer text; it is source input only, not an expected-output artifact. `fixtures/pdf_architecture/v1/cad-wall-primitive-families.pdf` is the #55 source-only geometry fixture: split collinear line runs, dashed lines, multi-segment polylines, and curve paths form one synthetic wall enclosure with no text or expected-output companion. `fixtures/pdf_architecture/v1/cad-export-geometry-plus-text.pdf` is the #45 geometry-plus-text source fixture. It contains the normalized CAD geometry plus synthetic numeric label and dimension/keynote/title-block distractors and has no expected-output companion. `fixtures/pdf_architecture/v1/dense-room-labels.pdf` is the #52 source-only fixture with three enclosures, adjacent number/name room labels, varied font sizes, dimensions, keynotes, leader tags, and ordinary text distractors; it has no expected-output companion. Tests also cover open/competing enclosures, extraction-order independence, arbitrary numeric/abbreviated room labels, close-score label ambiguity, ambiguous scale, missing height, explicit registration, inter-sheet registration, resolved pages that emit no geometry, stable identity, repeatability, and lane isolation. No customer plan set is used.
