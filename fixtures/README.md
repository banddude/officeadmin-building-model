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

These fixtures are synthetic. They contain no customer plan content.

`pdf_convergence/v1/` contains Gate D's public-safe convergence manifest. It
reuses the synthetic architectural garage and electrical-sheet fixtures and adds
only the explicit shared-frame registration plus known hosting answers. See
`docs/pdf-convergence.md`.

`golden/v1/` is the issue #10 cross-workstream regression suite. It contains the rectangular room, door obstruction, panel-to-EVSE, elevation-change, multiple-valid-route, impossible-route, two-level-building, and synthetic-garage known-answer cases plus intentionally invalid boundary fixtures. See `golden/v1/README.md` and `docs/qa-golden-fixtures.md` for usage.

## RoomPlan

`roomplan/captured-room-3d.json` is a synthetic CapturedRoom-shaped fixture for the RoomPlan / LiDAR importer. It includes nonzero story elevation, 4x4 transforms, polygon and curved wall surfaces, openings, source confidence, an object, and section metadata. It contains no customer or private scan data.
