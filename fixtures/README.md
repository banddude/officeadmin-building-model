# Fixtures

Only synthetic or explicitly public/cleared fixtures belong here.

`model/v1/` contains the checked-in canonical contract examples:

1. `minimal-room.json`: tiny known-answer building model
2. `garage-route.json`: broad v1 contract coverage for building + electrical + route semantics

`golden/v1/` is the QA-owned cross-workstream regression set. Its
`manifest.json` is the stable discovery surface and includes:

1. rectangular room
2. wall opening / door obstruction
3. panel to EVSE
4. route requiring a vertical elevation change
5. route with multiple valid alternatives
6. impossible route input
7. two-level building
8. synthetic garage integration fixture
9. a schema-valid, reference-invalid negative fixture

Other lanes should consume the golden cases through `oabm.qa` rather than
copying them or creating workstream-local model shapes.

The `model/v1/garage-route.json` fixture remains a contract example. The
QA-owned `golden/v1/synthetic-garage.json` fixture is the broader regression
acceptance case.
