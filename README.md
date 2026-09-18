# OfficeAdmin Building Model

Public R&D repo for the semantic 3D building and electrical modeling engine that will eventually integrate with OfficeAdmin.

## North star

One canonical semantic 3D model, many inputs and outputs.

Inputs can include RoomPlan/LiDAR scans, PDFs, IFC/Bonsai, and manual editing. Outputs can include routed electrical systems, takeoff quantities, estimating inputs, plans, elevations, sections, schedules, and IFC.

The model is the source of truth. A floor plan is a derived view of that model.

## Workstreams

- `src/oabm/model`: canonical model and contracts
- `src/oabm/ifc`: IFC / IfcOpenShell adapters and round trip
- `src/oabm/routing`: deterministic 3D electrical routing
- `src/oabm/importers/roomplan`: RoomPlan / scan ingestion
- `src/oabm/importers/pdf_architecture`: architectural plan ingestion
- `src/oabm/importers/pdf_electrical`: electrical plan ingestion
- `src/oabm/quantities`: model-derived takeoff quantities
- `src/oabm/drawings`: generated plans, elevations, sections, and schedules
- `src/oabm/qa`: reusable canonical handoff validators and golden regression harness
- `fixtures`: known-answer synthetic buildings and routes

See `docs/responsibility-relay.md` for the dependency graph.

## Agent entrypoint

Coding agents should read `AGENTS.md` first. It contains the implementation, fix-pass, independent-review, duplicate-PR, testing, safety, and dependency-relay rules needed to work from a short issue/PR assignment without private chat context. See `docs/agent-playbook.md` for minimal dispatch prompts.

## CI

CI is intentionally small and isolated from OfficeAdmin. It runs on standard GitHub-hosted Ubuntu runners.

## License

No open-source license has been selected yet. Public visibility alone does not grant reuse rights.
