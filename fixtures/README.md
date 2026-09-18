# Fixtures

Only synthetic or explicitly public/cleared fixtures belong here.

`model/v1/` contains the checked-in canonical contract examples:

1. `minimal-room.json`: tiny known-answer building model
2. `garage-route.json`: broad v1 contract coverage for building + electrical + route semantics

The garage-route fixture is a contract example, not the later convergence acceptance fixture.

`golden/v1/` is the issue #10 cross-workstream regression suite. It contains the rectangular room, door obstruction, panel-to-EVSE, elevation-change, multiple-valid-route, impossible-route, two-level-building, and synthetic-garage known-answer cases plus intentionally invalid boundary fixtures. See `golden/v1/README.md` and `docs/qa-golden-fixtures.md` for usage.

## RoomPlan

`roomplan/captured-room-3d.json` is a synthetic CapturedRoom-shaped fixture for the RoomPlan / LiDAR importer. It includes nonzero story elevation, 4x4 transforms, polygon and curved wall surfaces, openings, source confidence, an object, and section metadata. It contains no customer or private scan data.
