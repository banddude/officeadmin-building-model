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
- walls from those paired boundaries when a supported height is present;
- additional paired vector wall boundaries when the PDF supplies stable MCIDs and the sheet has a stable printed sheet identifier;
- marked door/window openings with dimensions when the host wall and vertical placement are supported;
- slabs only when an explicit floor/slab thickness is present;
- ceiling surfaces when an explicit or caller-supplied room/level height is present.

All semantic output is canonical `Level`, `Space`, `Wall`, `Opening`, `Slab`, `Ceiling`, and `BuildingModel` data. Electrical devices/equipment, routes, IFC objects, quantities, and derived drawings are outside this lane.

## Scale and registration

The first resolved architectural plan may define the project-local XY origin if it has a supported printed or overridden scale. Every additional plan page must have an explicit `RegistrationHint` before its geometry can share that canonical frame.

A two-point registration controls scale, rotation, and translation. If its computed scale disagrees with a printed or overridden scale beyond `scale_registration_tolerance`, the page is skipped and the disagreement is recorded rather than choosing one silently.

`ScaleOverride` is the explicit escape hatch for sheets that are not to scale or have unsupported/missing scale annotations.

## Levels and 3D values

An explicit source elevation is preferred. If the first/sole level has no elevation, the importer may establish a project-local `Z=0` datum and records that decision as an ambiguity with reduced confidence. A later level without an elevation relative to known levels is not positioned and is skipped until a `LevelOverride` is supplied.

Wall/ceiling height comes from a supported ceiling-height annotation, `LevelOverride.height_m`, or an explicitly supplied `ImportOptions.default_wall_height_m`. The importer does not invent a generic story height. If height is unresolved, it may emit a 2D footprint-backed canonical `Space` but will not fabricate 3D walls or a ceiling.

## Stable identity

Canonical IDs use `oabm.model.stable_id` with semantic anchors, never list positions or mutable geometry. Examples include logical source + level + room label + boundary side, and logical source + sheet identifier + PDF-native MCIDs. Repeated semantic anchors that would collide are preserved as ambiguity and later geometry is not silently substituted.

## Ambiguity and provenance

Every promoted entity carries confidence and `Provenance(source_kind="architectural_pdf", ...)`. Source-specific details and unresolved decisions are retained under `BuildingModel.attributes["pdf_architecture"]`, including per-page classification/scale/registration metadata and an ordered `ambiguities` list.

Typical ambiguity codes include:

- `scale_unresolved` / `scale_conflict`;
- `scale_registration_conflict` / `registration_unresolved`;
- `level_elevation_local_datum` / `level_elevation_unresolved`;
- `wall_height_unresolved`;
- `duplicate_room_label` / `duplicate_room_identity_across_pages`;
- `opening_host_unresolved`, `opening_identity_unresolved`, and `window_vertical_position_unresolved`.

The rule is conservative: unresolved facts remain unresolved instead of being converted into precise-looking canonical geometry.

## Fixtures and tests

`fixtures/pdf_architecture/v1/simple-floor-plan.pdf` is synthetic and public-safe. Its known-answer canonical model is checked in alongside it. Tests also construct synthetic observations for ambiguous scale, missing height, explicit registration, inter-sheet registration, stable identity, repeatability, and lane isolation. No customer plan set is used.
