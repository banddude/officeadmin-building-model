# OfficeAdmin Building Model

Public R&D repo for the semantic 3D building and electrical modeling engine that will eventually integrate with OfficeAdmin.

## Start here

To install the local runtime, run a synthetic complete set, prepare private job
inputs, check a production request, create populated-titleblock blank sheets,
and deliver model/GLB previews, follow the **[operator start-here guide](docs/operator-start-here.md)**.
The combined workflow is on the development PR branch until merged. Keep real
job inputs and outputs outside this public checkout; these are review tools,
not a claim of engineering approval or complete permit sets.

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
GLB previews and quantities share those canonical physical component IDs.
Routing supports an opt-in bounded assembly-aware slice with explicit
`AllowedRouteDomain` volumes; a geometry-derived plenum domain is available only
for the supported rectangular suspended-ceiling case documented in
[3D electrical routing](docs/routing.md). Default IFC/drawing consumers and unsupported QA consumers still refuse populated
assemblies. [Physical assembly consumers](docs/physical-assembly-consumers.md)
provide explicit opt-in IFC tessellation and plan/section cut outlines; assembly
elevations remain unsupported. Explicit cut-only drawing sets and versioned
model/physical-sheet deliveries use the supported plan/section adapters. This is not full-room framing,
does not authorize penetrations, and does not automate engineering decisions.
See [Layered raceway policy](docs/layered-raceway-policy.md) for portable caller
baseline/company/job permissions compiled into existing explicit selection rules.
See [Physical schedule sheets](docs/schedule-sheet-production.md) for explicit supplied
panel/circuit facts laid out as editable vector tables using the shared company
profile, repeated headers, deterministic pagination and verified PDF output.

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

See [Conduit bend preflight](docs/route-bend-preflight.md) for a read-only
canonical-route gate that checks elbow inventory and caller-supplied bend-radius
space before producing plan and 3D deliveries.

See [One-command model delivery](docs/model-delivery.md) to run the bend gate
and requested plan/GLB exports together with retained inputs, explicit review
status and a deterministic hash manifest.

See [Bounded model-view label placement](docs/view-label-layout.md) for
deterministic SVG label offsets, source-bound leaders and explicit unplaced
label reports. The model delivery workflow applies this pass to plans.

See [Blank sheet production](docs/blank-sheet-set.md) for one command taking
job metadata and an ordered sheet list to populated-titleblock blank SVGs and
optional DXFs, without a model or design. The optional [local PDF adapter](docs/sheet-pdf.md)
adds a combined vector PDF with explicit native/runtime dependencies.

See [Electrical single-line production](docs/electrical-single-line.md) for explicit
canonical equipment/circuit topology, compact connected branch diagrams, equipment
continuation references and editable SVG/PDF packets using the shared company profile.

## Agent entrypoint

Coding agents should read `AGENTS.md` first. It contains the implementation, fix-pass, independent-review, duplicate-PR, testing, safety, and dependency-relay rules needed to work from a short issue/PR assignment without private chat context. See `docs/agent-playbook.md` for minimal dispatch prompts.

## CI

CI is intentionally small and isolated from OfficeAdmin. It runs on standard GitHub-hosted Ubuntu runners.

## License

No open-source license has been selected yet. Public visibility alone does not grant reuse rights.

The two upstream files in `spikes/graph_studio/upstream/` retain their separate
Apache-2.0 license and NOTICE; see the spike README for attribution and hashes.

Native IFC4 wall/opening intake has a separate explicit [bounded reader](docs/native-ifc-import.md); strict OABM IFC round-trip behavior remains unchanged.

See [Shared company sheet profiles](docs/company-sheet-profile.md) for saved company defaults, job-only overrides and reusable right-rail sheet/PDF composition.
See [Supplied site-plan production](docs/supplied-site-plan.md) for explicit
site geometry to scaled editable sheets using the shared company sheet profile.

See [Electrical sheet composition](docs/electrical-sheet-composition.md) for shared schematic/feeder/register regions and explicit useful-panel selection. The connected drafting samples are `examples/reference_production_set.py --variant reference|residential|commercial` (supply a fresh output path); their visual review status remains explicit.

See [Shared production chain](docs/production-sheet-set.md) for one ordered cover/site/single-line/schedule/detail SVG/PDF packet using one company profile and canonical model. See the [dated current-state audit](docs/current-state-audit-2026-10-27.md) for implementation boundaries and outstanding professional drafting acceptance.

See [Checked assembly routing](docs/checked-assembly-routing.md) for explicit routing candidates gated by assembly geometry and bend preflight, with a canonical output for downstream delivery.

See [Supplied 2D lighting/RCP review sheets](docs/supplied-lighting-rcp.md) for source-bound XY presentation with unknown heights and no physical BIM placement.

See [Native Bonsai review drawings](docs/native-bonsai.md) for explicitly supplied
local Blender/Bonsai runtimes, immutable canonical input snapshots, native IFC/PLAN/RCP
SVG output, linked paper annotations and fail-closed omissions. This optional tool
does not install runtimes, infer geometry or establish engineering acceptance.
