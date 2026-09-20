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
- printed imperial scales and `1:N` scales;
- two-point registrations that establish scale, plan rotation, and translation in the canonical XY frame;
- named levels with explicit elevations, or one first/sole local datum at `Z=0` when the source gives no project elevation;
- labelled rectangular spaces bounded by paired vector wall rectangles;
- room labels assigned by text position inside a closed wall loop: dimensions, explicit keynote/note strings, detected leader tags, and recognized title-block strings are excluded first; remaining candidates are ranked by enclosure centrality, font size relative to the page median, and room-number pattern, with an adjacent number/name line treated as one label; runner-ups are retained in provenance and a `multiple_room_labels_in_enclosure` ambiguity is emitted only when the top two scores are within the small ranking margin; numeric labels and project-specific abbreviations remain supported without ROOM:/SPACE: prefixes;
- labelled rectangular spaces and walls from untagged ordinary vector lines when two closed axis-aligned wall-face loops prove one enclosure around a unique eligible text label;
- walls from untagged parallel wall faces paired by geometry rather than PDF native IDs, but only when uniquely paired segments can be joined into an unambiguous closed loop; the same geometry-backed Space is labeled when exactly one eligible text observation falls inside the loop and remains unlabeled when none does;
- a conservative 2D-only space from one unique closed ordinary-vector boundary around a unique room label when no partial paired wall-face evidence is present; wall thickness and walls remain unresolved rather than being invented;
- walls from those paired boundaries when a supported height is present;
- geometric wall-pair identity is anchored by rounded canonical centerline endpoints plus stable sheet and level anchors; source element/native IDs remain provenance only;
- marked door/window openings with dimensions when the host wall and vertical placement are supported;
- slabs only when an explicit floor/slab thickness is present;
- ceiling surfaces when an explicit or caller-supplied room/level height is present.

All semantic output is canonical `Level`, `Space`, `Wall`, `Opening`, `Slab`, `Ceiling`, and `BuildingModel` data. Electrical devices/equipment, routes, IFC objects, quantities, and derived drawings are outside this lane.

## Scale and registration

The first architectural plan that resolves level and scale and actually emits supported canonical spatial geometry may define the project-local XY origin. A resolved plan that emits no supported spatial geometry is recorded as `no_supported_geometry_recognized` with `architectural_geometry_unrecognized`; it does not claim the shared frame. Once a base geometry page exists, an explicit `RegistrationHint` remains authoritative when supplied. If scale is known but no registration cue exists, an additional CAD-vector page may use a lower-confidence sheet-geometry fallback: the importer prefers the largest proven closed wall-loop bounding box and otherwise uses non-title-block drawing extents, anchoring that geometry's lower-left at the project-local origin. Title-block vector geometry identified around explicit sheet/drawing/project metadata is excluded. The fallback method, confidence, source bounding box, and anchor are retained in page metadata and model provenance. Pages without enough vector geometry remain `registration_unresolved`.

A two-point registration controls scale, rotation, and translation. If its computed scale disagrees with a printed or overridden scale beyond `scale_registration_tolerance`, the page is skipped and the disagreement is recorded rather than choosing one silently.

`ScaleOverride` is the explicit escape hatch for sheets that are not to scale or have unsupported/missing scale annotations.

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
- `level_elevation_local_datum` / `level_elevation_unresolved`;
- `level_elevation_reconciled` / `level_elevation_conflict`;
- `level_height_reconciled` / `level_height_conflict` / `level_height_default_assumed`;
- `ceiling_height_scope_unresolved` / `room_ceiling_height_conflict`;
- `wall_height_unresolved`;
- `duplicate_room_label` / `duplicate_room_identity_across_pages`;
- `opening_host_unresolved`, `opening_identity_unresolved`, and `window_vertical_position_unresolved`.

The ordinary-vector fallback is intentionally narrow: only complete axis-aligned four-line loops are considered. Paired wall-face loops must satisfy the configured wall-thickness/span limits. If paired wall faces are unavailable, exactly one sufficiently large closed loop may support only a 2D Space around a unique room label; any supported inset wall-face side is treated as evidence of an incomplete pair and the single-loop fallback fails closed. Open or competing enclosures remain unresolved instead of being selected by extraction order.

Text extraction also keeps source observations local: words sharing a text baseline are split when a large horizontal gap indicates separate plan annotations. This prevents a room label from being fused with an unrelated distant dimension or keynote while preserving stable text-observation IDs for unchanged local labels.

The rule is conservative: unresolved facts remain unresolved instead of being converted into precise-looking canonical geometry.

## Fixtures and tests

`fixtures/pdf_architecture/v1/simple-floor-plan.pdf` is synthetic and public-safe, and its known-answer canonical model is checked in alongside it. `fixtures/pdf_architecture/v1/cad-export-scale-no-registration.pdf` is the #51 two-page CAD-style source-only fixture: both pages carry printed scale/level text, the second page has no registration cue, and an offset synthetic title block proves the geometry fallback does not anchor on title-block extents; it has no expected-output companion. `fixtures/pdf_architecture/v1/ordinary-vector-room.json` is a tiny synthetic observation fixture for the ordinary untagged line-loop family. `fixtures/pdf_architecture/v1/cad-derived-wall-faces.json` is a geometry-only, public-safe derivative for #44 with no native IDs or customer text; it is source input only, not an expected-output artifact. `fixtures/pdf_architecture/v1/cad-export-geometry-plus-text.pdf` is the #45 geometry-plus-text source fixture. It contains the normalized CAD geometry plus synthetic numeric label and dimension/keynote/title-block distractors and has no expected-output companion. `fixtures/pdf_architecture/v1/dense-room-labels.pdf` is the #52 source-only fixture with three enclosures, adjacent number/name room labels, varied font sizes, dimensions, keynotes, leader tags, and ordinary text distractors; it has no expected-output companion. Tests also cover open/competing enclosures, extraction-order independence, arbitrary numeric/abbreviated room labels, close-score label ambiguity, ambiguous scale, missing height, explicit registration, inter-sheet registration, resolved pages that emit no geometry, stable identity, repeatability, and lane isolation. No customer plan set is used.
