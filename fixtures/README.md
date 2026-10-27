# Fixtures

Only synthetic or explicitly public/cleared fixtures belong here.

`model/v1/` contains the checked-in canonical contract examples:

1. `minimal-room.json`: tiny known-answer building model
2. `garage-route.json`: broad v1 contract coverage for building + electrical + route semantics

The garage-route fixture is a contract example, not the later convergence acceptance fixture.

Planned golden fixtures remain:

1. rectangular room
2. wall opening / door obstruction
3. panel to EVSE
4. route requiring a vertical elevation change
5. route with multiple valid alternatives
6. impossible route
7. two-level building
8. synthetic garage integration fixture

## RoomPlan

`roomplan/captured-room-3d.json` is a synthetic CapturedRoom-shaped fixture for the RoomPlan / LiDAR importer. It includes nonzero story elevation, 4x4 transforms, polygon surfaces, openings, source confidence, an object, and section metadata. It contains no customer or private scan data.
