# Fixtures

Only synthetic or explicitly public/cleared fixtures belong here.

`model/v1/` contains the checked-in canonical contract examples:

1. `minimal-room.json`: tiny known-answer building model
2. `garage-route.json`: broad v1 contract coverage for building + electrical + route semantics

The garage-route fixture is a contract example, not the later convergence acceptance fixture.

`pdf_electrical/` contains source-observation fixtures for the electrical PDF
recognition lane:

1. `synthetic-sheet-e1.json`: panel, EVSE symbol, mounting note, and inferable circuit
2. `ambiguous-sheet-e1.json`: ambiguous symbol, conflicting mounting heights, and an incomplete circuit reference
3. `geometry-only-power-sheet-with-legend.pdf`: Slice 4 source-only acceptance PDF with six geometry-only field glyphs, three drawn legend prototypes and labels, and two unmatched glyphs that must remain unresolved. There is no paired expected-output artifact.
4. `two-page-sheet-local-legend.pdf`: Slice 4 regression PDF. Page 1 has the drawn legend and field glyphs; page 2 repeats only field glyph geometry and must not inherit page 1 legend types. There is no paired expected-output artifact.
5. `geometry-only-power-sheet-edge-legend.pdf`: Slice 7 source-only acceptance PDF. A `SYMBOLS` legend block sits at the sheet edge inside a page frame and title block, with a leader from the heading; six field symbols carry geometry only and must be typed from the detected block. There is no paired expected-output artifact.
6. `geometry-only-power-sheet-dense-legend.pdf`: Slice 7 source-only regression PDF with no legend heading. The importer must find the aligned three-row glyph/label table by density and repeated symbol geometry. There is no paired expected-output artifact.
7. `separate-sheet-explicit-legend-reference.pdf`: Slice 7 source-only three-page regression PDF. Page 1 carries `GENERAL NOTES AND LEGEND` on sheet `E-001`; pages 2 and 3 contain field glyphs and explicit references by sheet name and legend title. Cross-sheet typing is allowed only through those source references. There is no paired expected-output artifact.

These fixtures are synthetic. They contain no customer plan content.

`pdf_convergence/v1/` contains Gate D's public-safe convergence manifest. It
reuses the synthetic architectural garage and electrical-sheet fixtures and adds
only the explicit shared-frame registration plus known hosting answers. See
`docs/pdf-convergence.md`.

`golden/v1/` is the issue #10 cross-workstream regression suite. It contains the rectangular room, door obstruction, panel-to-EVSE, elevation-change, multiple-valid-route, impossible-route, two-level-building, and synthetic-garage known-answer cases plus intentionally invalid boundary fixtures. See `golden/v1/README.md` and `docs/qa-golden-fixtures.md` for usage.

## RoomPlan

`roomplan/captured-room-3d.json` is a synthetic CapturedRoom-shaped fixture for the RoomPlan / LiDAR importer. It includes nonzero story elevation, 4x4 transforms, polygon and curved wall surfaces, openings, source confidence, an object, and section metadata. It contains no customer or private scan data.
