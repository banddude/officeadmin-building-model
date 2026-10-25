# OfficeAdmin Building Model

Public R&D repo for the semantic 3D building and electrical modeling engine that will eventually integrate with OfficeAdmin.

## North star

One canonical semantic 3D model, many inputs and outputs.

Inputs can include RoomPlan/LiDAR scans, PDFs, IFC/Bonsai, and manual editing. Outputs can include routed electrical systems, takeoff quantities, estimating inputs, plans, elevations, sections, schedules, and IFC.

The model is the source of truth. A floor plan is a derived view of that model.

## Workstreams

- `src/oabm/model`: canonical model and contracts
- `src/oabm/electrical`: explicit physical-product intent/catalog producers
- `src/oabm/building_assemblies`: explicit preset-driven physical building parts
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

See [Physical electrical boxes](docs/electrical-boxes.md) for the explicit
contract 1.1 migration, selection boundary, and unresolved quantity limitations.

See [Raceway and cable selections](docs/raceway-selections.md) for explicit
contract 1.2 product/reference decisions and unresolved mixed-run limitations.
See [Shared supports](docs/shared-supports.md) for explicit contract 1.3 shared
assemblies, individually counted components, and unresolved attachment inputs.
See [Exact-product route fittings](docs/route-fitting-selection-contract.md)
for the opt-in contract 1.5 catalog-pair selection, unresolved cases, and
derived consumer/replay boundaries.
See [Preset-driven wall assemblies](docs/building-assembly-wall-producer-v1.md)
for opt-in 1.6 opening-aware timber framing and drywall. Separately selected
[door/window inserts](docs/building-assembly-opening-inserts-v1.md) and
[suspended ceilings](docs/building-assembly-suspended-ceiling-producer-v1.md)
add physical frames, leaf/pane geometry, T runners, tiles and hanger wires.
GLB previews and quantities share those canonical physical component IDs. Populated assemblies are explicitly
refused by routing, IFC, drawings and unsupported QA consumers until those
lanes implement physical-component semantics. This is not full-room framing
and does not automate engineering decisions.
See [Layered raceway policy](docs/layered-raceway-policy.md) for portable caller
baseline/company/job permissions compiled into existing explicit selection rules.
See [Panel schedule facts](docs/panel-schedule-facts.md) for an opt-in grouped
report of supplied circuit values, missing facts and connected-load completeness.

See [Object scope ledger](docs/scope-ledger.md) for source-bound per-object
scope facts, separate counts and explicit confirmed-unmarked caller policy.

See [Bounded proposal ranking](docs/bounded-proposals.md) for strict source-bound
semantic rankings, configured fake-adapter roles and measured vision gates.

See [Layer selections](docs/layer-selections.md) for portable saved layer
selections, the explicit membership resolver, and the GLB visibility handoff.

See [Branch wall-evidence guard](docs/branch-wall-evidence-guard.md) for the
source-bound architectural wall evidence handoff and open-chain rejection.

See [Open-source reuse audit](docs/open-source-reuse-audit.md) for component
decisions and the reproducible, public-safe calculation prototype from #93.

## Agent entrypoint

Coding agents should read `AGENTS.md` first. It contains the implementation, fix-pass, independent-review, duplicate-PR, testing, safety, and dependency-relay rules needed to work from a short issue/PR assignment without private chat context. See `docs/agent-playbook.md` for minimal dispatch prompts.

## CI

CI is intentionally small and isolated from OfficeAdmin. It runs on standard GitHub-hosted Ubuntu runners.

## License

No open-source license has been selected yet. Public visibility alone does not grant reuse rights.

The two upstream files in `spikes/graph_studio/upstream/` retain their separate
Apache-2.0 license and NOTICE; see the spike README for attribution and hashes.

Native IFC4 wall/opening intake has a separate explicit [bounded reader](docs/native-ifc-import.md); strict OABM IFC round-trip behavior remains unchanged.
