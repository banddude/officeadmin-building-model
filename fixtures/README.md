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

Planned golden fixtures remain:

1. rectangular room
2. wall opening / door obstruction
3. panel to EVSE
4. route requiring a vertical elevation change
5. route with multiple valid alternatives
6. impossible route
7. two-level building
8. synthetic garage integration fixture
