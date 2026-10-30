# BIM repository workflow audit

Read-only source audit, 27 October 2026. No code changes, installs, tests, model rendering, publication or merges were performed for this audit.

## What actually connects

The repository is a canonical-model engine with several distinct production paths. It is not one integrated Bonsai drafting application. Existing importers, evidence review, routing, quantities, IFC, GLB, drawings and saved-job orchestration are real code. They have different admission contracts and coverage. A caller or agent still supplies review decisions, source registrations, provider adapters, catalog choices and design facts where required.

Main examined: 500e001a0364515cc4390d55e78ba05c13fa27a8 (local origin/main snapshot; not a fresh network fetch). Candidate examined separately: 86bc527f4a0e23fc7294161985d9cd69a0175aa3. Candidate is unmerged. This dated audit records local source state and does not claim merge or publication acceptance. Mechanical sample visual checks do not establish professional drafting acceptance: the parent withdrew style acceptance after comparing against the real reference. Single-line graphics remain a schematic box/connector presentation rather than an accepted reference-style electrical drawing; table and page composition also need professional acceptance.

Main has zero installed console-script registrations, but six package CLI implementations. Candidate adds six installed command names and seven additional package CLI implementations: thirteen implementations total. Company sheets has three subcommands, yielding fifteen package leaf actions; installed aliases expose eight leaf actions. Aliases are not separate engines. All registered names and all package parser implementations were inspected statically. The appendices inventory all 140 main and 155 candidate Python source modules, and all syntactically public top-level definitions (main 269 functions/343 classes; candidate 317 functions/358 classes). Those definition counts include helpers and data contracts, not independent end-user tools.

### Top-down flow

1. PDF vector/text observations, RoomPlan JSON or native IFC enter bounded importers. Observations and source hashes survive as evidence; unsupported or ambiguous facts are not equivalent to accepted geometry.
2. Electrical source proposals and reviewed placement/design decisions can be admitted into the canonical BuildingModel. Architecture/electrical PDF convergence requires resolved registration. Provider invocation requires caller-supplied adapters; the package is not a configured hosted AI service.
3. Explicit model ports, circuit/design intents, route domains and catalogs feed routing and physical selection. Validation and evidence review are separate from engineering approval.
4. The canonical model feeds quantities, reports, GLB, ordinary drawing views and IFC, subject to different support boundaries. Populated building assemblies work in quantities/GLB but ordinary drawings and IFC explicitly refuse them.
5. Saved-job/delivery/revision APIs persist or replay trusted inputs and immutable artifacts. Model delivery exports an already prepared model; it is not ingest, takeoff or design automation.
6. Candidate production sheets consume profile/job/config plus an already prepared model and explicit body inputs. Cover/site/single-line/schedules/details share title blocks, identity, manifests and PDF assembly. This path uses custom SVG and CairoSVG/pypdf, not Bonsai's documentation engine or the existing ezdxf sheet adapter.

## Important boundaries

- Bonsai support is actual IFC interoperability, not a bundled runtime. src/oabm/ifc/adapter.py imports IfcOpenShell; no tracked source imports bonsai or bpy. Optional ifc dependencies declare IfcOpenShell/numpy. docs/gate-b-synthetic-garage.md records a historical actual Blender 5.2.1/Bonsai 0.8.5 edit/save/reopen exercise. Automated Bonsai-equivalent tests edit native IFC through IfcOpenShell; they do not launch Bonsai. Native import is bounded; canonical from_ifc is a separate strict OABM roundtrip contract.
- ezdxf already exists as optional cad dependency and src/oabm/drawings/sheet_dxf.py produces physical paperspace DXF. It handles a bounded primitive subset and refuses unsupported symbols/fills. Candidate blank-set can request it. Candidate company/production SVG/PDF commands bypass it; this does not mean CAD export is missing.
- src/oabm/assembly_compat.py refuses populated building assemblies from ordinary drawing generators and IFC. Do not extrapolate GLB/quantity assembly support to those exports.
- reviewed_electrical.py requires coverage_report=None because the source-coverage contract is not integrated; complete source takeoff, complete buildable model, code compliance verified and production-live claims are explicitly false. Its in-memory delivery does not dispatch, infer, write, upload, route or calculate code compliance.
- Route-bend QA checks per-route interior sharp vertices with supplied radius assumptions. Consolidated cross-route endpoint elbows are outside that contract. GLB conduit remains straight-cylinder joins, not manufactured curved fittings or fit certification.
- Candidate README says single-line integration is pending although production_set includes it. Actual code takes precedence over that stale sentence.
- Installed ChatGPT BIM skill is a separate bundled-runtime facade. It offers probe/validate/quantities/glb/plan-svg/route and explicitly relies on manual visual interpretation/review, local runtime availability and explicit model facts. Its facade prepends its own runtime/src; it does not automatically run this checkout or expose candidate production commands. A provider read of bundled runtime pyproject failed, so exact installed snapshot equivalence was not verified.

## Evidence interpretation

This audit used exact git archive source, AST definitions/parsers/imports/calls, source guards, test references and tracked acceptance documents. No full tests were rerun. Prior local candidate receipt records 4169 passed, 2 skipped, 2 xfailed; that is inherited execution evidence, not fresh audit execution or drafting approval. Historical Bonsai acceptance is a tracked report, not a new application test. The API appendix lists lexical test references as discovery evidence, not proof every branch is tested. External installation/runtime availability was not changed or certified.

## Operation inventory

Each row states actual input -> output -> next consumer, principal implementation, and limits. Unless marked candidate, it exists on examined main as well. Detailed signatures, documentation, direct call names and test references for every module follow in the API appendix.

### Ingest/evidence

**Architectural PDF.** PDF or extracted PdfDocumentObservation + source ID/options -> canonical architecture plus diagnostics/provenance -> convergence/model consumers. Source: `src/oabm/importers/pdf_architecture/importer.py; extract.py`. Limit: Vector/text/layer heuristics and explicit scale/region/level contracts; not universal plan understanding.

**Electrical PDF.** Extracted PDF/source observations -> device/schedule/branch evidence and bounded canonical entities -> review/convergence. Source: `src/oabm/importers/pdf_electrical/importer.py; extract.py`. Limit: Legacy recognition and AI-first reviewed-symbol lane coexist; schematic marks do not prove surveyed poses.

**AI symbol review.** Source PDFs/crops, requests, adapters, journals -> verified proposals/review -> materialize_reviewed_symbols. Source: `src/oabm/importers/pdf_electrical/symbol_stage.py; proposals/symbols.py`. Limit: Trusted review/registration required; no hidden deterministic fallback or automatic human authentication.

**Placement/source assertions.** Source-bound marks, evidence documents, review decisions -> qualified placements -> canonical admission. Source: `src/oabm/importers/pdf_electrical/placement_assertions.py`. Limit: Source identity and qualification checked; unknown coordinates are not survey evidence.

**Schedules/table extraction.** PDF page/regions + Camelot or Docling -> deterministic cells/confidence/diagnostics -> schedule review. Source: `src/oabm/importers/tables; pdf_electrical/schedule_tables.py; schedule_review.py`. Limit: Optional backend; cell normalization is explicit; extraction confidence is not design verification.

**RoomPlan.** CapturedRoom JSON + source/options -> rotated Z-up canonical room model -> validation/export. Source: `src/oabm/importers/roomplan/importer.py`. Limit: Bounded Apple capture schema, not a measured-construction certification.

**Registration/convergence.** Architecture model and both source observations + transforms/options -> registered electrical pages and joined model -> downstream exports. Source: `src/oabm/importers/pdf_convergence`. Limit: Wall/grid/point/mask registration has ambiguity/refusal contracts; optional cv2/pycpd; not proximity-based identity guessing.

**PDF display and source review.** PDF geometry/display transforms or source review facts -> normalized coordinates/2D source review -> evidence consumers. Source: `src/oabm/importers/pdf_display.py; exports/source_review_2d.py`. Limit: Source-space review is distinct from canonical surveyed geometry.


### Model/assemblies

**Canonical model/serialization/upgrades.** Typed entities or JSON + version -> validated BuildingModel/current schema -> all consumers. Source: `src/oabm/model/model.py; model/entities.py; model/v1.py`. Limit: Validation checks contracts, not engineering approval; upgrades preserve explicit schema history.

**Wall/opening/ceiling recipes.** Explicit dimensions/preset and placements -> building assembly parts/provenance -> quantities/GLB. Source: `src/oabm/building_assemblies/wall.py; opening_inserts.py; suspended_ceiling.py`. Limit: Finite reusable recipes; manufacturer/internal fit unknown; drawings/IFC guard populated assemblies.

**Layers and assembly geometry.** Canonical layer/selection/assembly data -> selection plans/physical primitives -> GLB/quantity/support consumers. Source: `src/oabm/model/layers.py; assembly_geometry.py; geometry_primitives.py`. Limit: Presentation selections do not rewrite source facts.


### Electrical/routing

**Equipment placement.** Model + user placement or proposal/options -> placement proposal/new model -> design/routing. Source: `src/oabm/routing/placement.py`. Limit: Explicit provenance; a proposal is not approved location evidence.

**Architectural electrical design.** Model + design options -> proposed equipment/circuits/design result -> reviewed application/routing. Source: `src/oabm/routing/design.py`. Limit: Bounded design intents and required facts, not automatic code/service sizing.

**Port-to-port route.** Model + endpoint ports/domain/options -> RouteResult and route geometry -> model/physical selection. Source: `src/oabm/routing/router.py`. Limit: Explicit endpoint topology; obstacles/domains/options determine path; no future-stub implicit connectivity.

**Route domains.** Wall cavity or suspended ceiling assembly evidence -> constrained route domain -> router. Source: `src/oabm/routing/router.py`. Limit: Requires supported geometry; routing domain is design intent.

**Overlap/consolidation.** Explicit routes + options -> overlap findings/bundled trunk and branches -> quantities/fittings. Source: `src/oabm/routing/overlap.py; consolidate.py`. Limit: Cross-route junctions differ from interior-vertex bend QA.

**Boxes/raceways/supports/fittings.** Model + explicit intents/catalog/policy -> typed selection reports/proposals -> physical entities/replay. Source: `src/oabm/electrical/boxes.py; raceways.py; supports.py; fittings.py`. Limit: Catalog eligibility and dimensional assumptions do not establish manufacturer fit or engineering approval.


### Catalog/assets

**Catalog manifest search.** Local manifest + filters/expected hash -> deterministic catalog records -> selection APIs. Source: `src/oabm/electrical/catalog_manifest.py`. Limit: Local catalog integrity/query, not live procurement or verified installed product.

**Code/policy profiles.** Supplied local reference profile + intent -> compiled selection policy -> raceway/fitting selection. Source: `src/oabm/electrical/code_profiles.py; policy.py`. Limit: Static reference/provenance, not code compliance certification.


### Quantities/validation

**Quantity extraction.** Validated canonical model -> quantity items and architecture claims -> estimating/review. Source: `src/oabm/quantities/core.py; architecture.py`. Limit: Model-derived only; missing source scope remains missing, no complete takeoff guarantee.

**Panel/scope/support reports.** Model + report options -> circuit/connected-VA rows, scope ledger, coverage -> review/drawing schedules. Source: `src/oabm/reports/panel_schedules.py; scope_ledger.py; support_coverage.py`. Limit: Reports supplied facts/unknowns/conflicts; does not invent demand/service sizing/breakers/spares.

**Confidence/takeoff/golden/revision QA.** Model/proposals/review config or base/target -> findings/diffs/comparisons -> caller review. Source: `src/oabm/qa/confidence_review.py; takeoff_comparison.py; golden.py; revision_diff.py`. Limit: Evidence-qualified checks; passing is not complete source coverage or automatic approval.

**Bend preflight.** Model + explicit route radii -> bend report -> optional delivery gate. Source: `src/oabm/qa/route_bends.py`. Limit: Interior sharp vertices only; unsupported consolidated junctions must be treated explicitly.


### IFC/Bonsai

**Canonical IFC export/roundtrip.** Canonical model -> deterministic IFC4 bytes -> external Bonsai/IFC editor -> strict from_ifc/model comparison. Source: `src/oabm/ifc/adapter.py; comparison.py`. Limit: OABM metadata/IDs retained; populated assemblies refused; no Bonsai drawing engine invoked.

**Native IFC intake.** Native IFC bytes/path + source ID -> bounded native import result -> canonical review. Source: `src/oabm/importers/ifc_native.py`. Limit: Unsupported products remain visible in diagnostics; not equivalent to canonical roundtrip coverage.


### Visualization/drawings

**GLB/layers.** Model + GLB options/selection -> GLB bytes -> viewer/manual visual QA. Source: `src/oabm/exports/gltf.py; layers.py`. Limit: Supports assembly geometry; straight conduit cylinders; no Blender invocation or fit approval.

**Ordinary views/schedules.** Model + explicit plan/elevation/section options -> DrawingSet/primitives/schedules -> SVG/physical sheets. Source: `src/oabm/drawings/generator.py; projection.py; model.py`. Limit: Populated assemblies refused; schedules may remain metadata in ordinary sheet SVG.

**View SVG/label layout.** DrawingView + layout options -> SVG and placement report -> sheet/model delivery. Source: `src/oabm/drawings/svg.py; label_layout.py`. Limit: Deterministic supported label bounds/omission diagnostics; not full permit composition.

**Physical sheet SVG/DXF.** SheetSpec + DrawingSet + placements -> physical vector SVG or DXF/source map -> CAD/review. Source: `src/oabm/drawings/sheets.py; sheet_svg.py; sheet_dxf.py`. Limit: Generic composition, bounded DXF primitives, existing review safeguards retained.


### Delivery

**Proposal dispatch/transport.** Requests + policy/config + injected adapters -> ranked/symbol/circuit proposal runs/journal -> review. Source: `src/oabm/proposals/dispatch.py; circuit_design.py; symbols.py; takeoff.py`. Limit: No configured provider network integration by itself; experiments distinct from production policy.

**Reviewed circuit application.** Explicit request + model/review/assertions/dispositions/evidence -> all-or-nothing updated model -> reviewed delivery. Source: `src/oabm/pipeline/reviewed_circuit_design.py`. Limit: Missing facts defer; caller authorizes reviewer and design facts.

**Reviewed electrical delivery.** Saved trusted review inputs -> immutable in-memory payloads/reports -> saved persistence. Source: `src/oabm/pipeline/reviewed_electrical.py`. Limit: Coverage contract absent; all complete/buildable/code/live claims false.

**Saved electrical/job/revisions.** Hash-bound local manifest + fresh job/run roots -> immutable snapshots/delivery/revision comparison -> review/handoff. Source: `src/oabm/pipeline/saved_electrical.py; saved_job.py; saved_revisions.py`. Limit: No uncertain provider retry; incomplete journals in doubt; new run IDs for retry.

**Review drawing/sheet delivery.** Explicit drawing config + coherent model/facts/payload status -> supplemental views/archive -> reviewer. Source: `src/oabm/pipeline/review_drawings.py; review_sheet_delivery.py; sheet_coherence.py`. Limit: Refuses mixed revisions; supplemental coverage not automatic engineering approval.

**Fitting replay/review bundles.** Exact saved manifest/artifact inputs -> regenerated fitting artifacts or deterministic review bundle -> verification. Source: `src/oabm/pipeline/saved_route_fitting_replay.py; exports/review_bundle.py`. Limit: Reproducibility requires retained inputs; integrity does not authenticate engineering facts.

**Model delivery CLI/API.** Prepared model + delivery options + fresh output -> view SVG/GLB/reports/input/output hashes -> viewer/rebuild. Source: `src/oabm/exports/model_delivery.py`. Limit: Opt-in bend policies; not PDF permit-sheet generator or ingest/design command.


### Candidate sheet production

**Blank set.** Shared job fields + ordered SheetSpec list -> blank drawing bodies/titleblocks SVG/DXF + retained config/manifest. Source: `src/oabm/drawings/blank_set.py`. Limit: First-class blank sheets, no fake geometry; generic original block distinct from company rail.

**Company profile/create/rebuild/update.** Saved profile/assets + job overrides + bodies -> shared right-rail SVG/PDF + receipts; explicit update persists defaults. Source: `src/oabm/drawings/company_sheets.py`. Limit: Job overrides never persist; actual font coverage/hash/metrics/XML/round-join constraints; private assets stay outside repo.

**PDF adapter.** Renderer-owned trusted SVG pages -> dimension/order-preserving PDF via CairoSVG/pypdf. Source: `src/oabm/drawings/sheet_pdf.py; company_sheets.py`. Limit: Optional pdf extra/native Cairo required; no external web conversion; readable PDF text repair differs from SVG halo.

**Cover pagination.** Explicit scope/contacts/notes/map/index -> paginated body SVG/layout/stable index -> shared company composer. Source: `src/oabm/drawings/cover_sheet.py`. Limit: Caller-supplied facts; fixed point includes continuation pages, no auto address fetch.

**Site layout.** Explicit canonical geometry/features/qualification + fixed or allowed-fit scales -> body/coverage/selected scale -> shared composer. Source: `src/oabm/drawings/site_plan.py`. Limit: Preserves source poses; selected detail/overview qualification; no invented site survey.

**Single-line.** Canonical electrical topology + explicit IDs/options -> schematic pages/connection register -> shared composer. Source: `src/oabm/drawings/single_line/topology.py; layout.py; render.py; production.py`. Limit: Topology from actual ports/circuits, bounded pagination and continuation refs; schematic boxes not accepted professional reference drafting.

**Schedule pages.** PanelScheduleReport/DrawingSchedule + layout options -> packed/paginated tables and canonical-row manifest -> shared composer. Source: `src/oabm/drawings/schedule_layout.py; schedule_set.py`. Limit: Unknown/conflict indicators and connected VA only; preserves whitespace/facts, no demand/service calculations.

**Detail recipes/slots.** Explicit recipe dimensions/scale/notes and slots -> reusable detail bodies at supplied scale -> shared composer. Source: `src/oabm/drawings/details.py`. Limit: Multiple details per sheet with bounds/overlap refusals; not unrestricted building-assembly drawing support.

**Complete production coordinator.** Profile/job/ordered production config + prepared canonical model -> coherent cover/site/SLD/schedules/details SVG/PDF, retained inputs/manifest. Source: `src/oabm/drawings/production_set.py`. Limit: One shared model and stable final index; no source interpretation/engineering approval; professional style acceptance withdrawn.


## Complete CLI inventory

### main

Installed names: none

**src/oabm/electrical/catalog_manifest.py**

- L660: `argparse.ArgumentParser(description=__doc__)`
- L661: `parser.add_argument('manifest', type=Path)`
- L662: `parser.add_argument('--query', default='')`
- L663: `parser.add_argument('--kind', choices=sorted(_V2_FIELDS))`
- L664: `parser.add_argument('--manufacturer')`
- L665: `parser.add_argument('--expected-sha256')`

**src/oabm/exports/model_delivery.py**

- L222: `argparse.ArgumentParser(description=__doc__)`
- L223: `parser.add_argument('model', type=Path)`
- L224: `parser.add_argument('options', type=Path)`
- L225: `parser.add_argument('output_directory', type=Path, help='fresh attempt directory, never overwritten')`

**src/oabm/pipeline/saved_electrical.py**

- L322: `argparse.ArgumentParser(description=__doc__)`
- L323: `parser.add_argument('manifest', help='explicit local saved-input JSON manifest')`
- L324: `parser.add_argument('--output-root', required=True, help='private local output parent')`
- L325: `parser.add_argument('--run-id', required=True, help='fresh version label; never overwritten')`

**src/oabm/pipeline/saved_job.py**

- L458: `argparse.ArgumentParser(description=__doc__)`
- L459: `parser.add_argument('manifest')`
- L459: `parser.add_argument('--job-root', required=True)`

**src/oabm/pipeline/saved_revisions.py**

- L362: `argparse.ArgumentParser(description=__doc__)`
- L363: `parser.add_argument('base')`
- L364: `parser.add_argument('target')`

**src/oabm/qa/route_bends.py**

- L158: `argparse.ArgumentParser(description=__doc__)`
- L159: `parser.add_argument('model', type=Path)`
- L160: `parser.add_argument('radii', type=Path, help='JSON object mapping canonical route IDs to centerline radius in metres')`

### candidate

Installed names: {"oabm-production-sheets": "oabm.drawings.production_set:main", "oabm-single-line": "oabm.drawings.single_line.production:main", "oabm-company-sheets": "oabm.drawings.company_sheets:main", "oabm-site-plan": "oabm.drawings.site_plan:main", "oabm-panel-schedule-sheets": "oabm.drawings.schedule_set:main", "oabm-blank-sheets": "oabm.drawings.blank_set:main"}

**src/oabm/drawings/blank_set.py**

- L256: `argparse.ArgumentParser(description=__doc__)`
- L257: `parser.add_argument('config', type=Path, help='explicit job metadata and ordered blank-sheet list JSON')`
- L258: `parser.add_argument('output_directory', type=Path, help='fresh attempt directory; never overwritten')`

**src/oabm/drawings/company_sheets.py**

- L563: `argparse.ArgumentParser(description=__doc__)`
- L564: `sub.add_parser('create')`
- L564: `create.add_argument('profile')`
- L564: `create.add_argument('job')`
- L564: `create.add_argument('output_directory')`
- L565: `sub.add_parser('rebuild')`
- L565: `rebuild.add_argument('delivery')`
- L565: `rebuild.add_argument('output_directory')`
- L566: `sub.add_parser('update-defaults')`
- L566: `update.add_argument('profile')`
- L566: `update.add_argument('changes')`
- L566: `update.add_argument('--expected-sha256', required=True)`

**src/oabm/drawings/cover_sheet.py**

- L466: `argparse.ArgumentParser(description=__doc__)`
- L467: `parser.add_argument('cover', type=Path)`
- L468: `parser.add_argument('profile', type=Path)`
- L469: `parser.add_argument('job', type=Path)`
- L470: `parser.add_argument('output', type=Path)`

**src/oabm/drawings/production_set.py**

- L179: `argparse.ArgumentParser(description=__doc__)`
- L179: `parser.add_argument('request')`
- L179: `parser.add_argument('output_directory')`

**src/oabm/drawings/schedule_set.py**

- L189: `argparse.ArgumentParser(description=__doc__)`
- L191: `parser.add_argument(name, type=Path)`

**src/oabm/drawings/single_line/production.py**

- L98: `argparse.ArgumentParser(description=__doc__)`
- L99: `parser.add_argument('model', type=Path)`
- L100: `parser.add_argument('profile', type=Path)`
- L101: `parser.add_argument('job', type=Path)`
- L102: `parser.add_argument('output_directory', type=Path)`

**src/oabm/drawings/site_plan.py**

- L619: `argparse.ArgumentParser(description=__doc__)`
- L620: `parser.add_argument('request', type=Path)`
- L621: `parser.add_argument('profile', type=Path, help='shared company profile JSON')`
- L622: `parser.add_argument('job', type=Path, help='shared explicit job/sheet request JSON')`
- L623: `parser.add_argument('output', type=Path, help='fresh output directory')`

**src/oabm/electrical/catalog_manifest.py**

- L660: `argparse.ArgumentParser(description=__doc__)`
- L661: `parser.add_argument('manifest', type=Path)`
- L662: `parser.add_argument('--query', default='')`
- L663: `parser.add_argument('--kind', choices=sorted(_V2_FIELDS))`
- L664: `parser.add_argument('--manufacturer')`
- L665: `parser.add_argument('--expected-sha256')`

**src/oabm/exports/model_delivery.py**

- L222: `argparse.ArgumentParser(description=__doc__)`
- L223: `parser.add_argument('model', type=Path)`
- L224: `parser.add_argument('options', type=Path)`
- L225: `parser.add_argument('output_directory', type=Path, help='fresh attempt directory, never overwritten')`

**src/oabm/pipeline/saved_electrical.py**

- L322: `argparse.ArgumentParser(description=__doc__)`
- L323: `parser.add_argument('manifest', help='explicit local saved-input JSON manifest')`
- L324: `parser.add_argument('--output-root', required=True, help='private local output parent')`
- L325: `parser.add_argument('--run-id', required=True, help='fresh version label; never overwritten')`

**src/oabm/pipeline/saved_job.py**

- L458: `argparse.ArgumentParser(description=__doc__)`
- L459: `parser.add_argument('manifest')`
- L459: `parser.add_argument('--job-root', required=True)`

**src/oabm/pipeline/saved_revisions.py**

- L362: `argparse.ArgumentParser(description=__doc__)`
- L363: `parser.add_argument('base')`
- L364: `parser.add_argument('target')`

**src/oabm/qa/route_bends.py**

- L158: `argparse.ArgumentParser(description=__doc__)`
- L159: `parser.add_argument('model', type=Path)`
- L160: `parser.add_argument('radii', type=Path, help='JSON object mapping canonical route IDs to centerline radius in metres')`


## Supplemental scripts and installed plugin

Examples and spikes are executable demonstrations, not installed production services. The exhaustive scripts appendix records their parsers and inputs, including physical-sheet SVG/DXF, graph studio, display-thickness/layer-selection evidence and candidate production examples. No evidence here that a spike is an accepted production workflow.

The inspected installed ChatGPT BIM facade exposes six commands: probe checks local runtime/dependencies; validate reads canonical JSON; quantities emits model-derived quantity JSON; glb writes preview GLB; plan-svg writes a model-derived view SVG; route computes explicit port-to-port route output. Its own runtime/src is imported. ChatGPT visual interpretation and fact-ledger review are manual/agent steps upstream, not code in that facade. Candidate cover/site/company/production APIs are not automatically exposed through this older facade.

## Completion and remaining uncertainty

All six installed candidate entry registrations and all thirteen package CLI parser implementations were accounted for, with main membership distinguished. Every source module and public top-level definition is included in the appendices. Static test references and direct call expressions are provided so individual contracts can be checked without treating helper counts as product features. This is source inspection, not dynamic execution of every CLI or exhaustive behavioral proof. Optional dependency installation and private production assets were not audited for availability. No current Bonsai runtime/render was performed. Professional drafting acceptance remains outstanding. This document records the current state rather than an approval to release.

## Implementation addendum: connected drafting review checkpoint

The inventory above remains tied to main `500e001` and candidate `86bc527`; it is not a release approval. Subsequent local work connects the existing production coordinator to the component renderers rather than replacing their source reports. A complete synthetic sample now retains one canonical electrical model through schematic topology, the wide feeder register, selected useful panel tables and explicit detail geometry. Cover pagination derives its index from the actual output sheets. Site graphics are retained as an explicit separate input with their own source frame and qualification; proximity does not establish model or survey identity.

Assembly source checkpoint `e06e194db12f7e4ef6afb9acac627666c201e42c` adds bounded opt-in physical consumers. Ordinary drawing sets and model delivery accept explicit `assembly_geometry="cut"` for supported plan/section intersections. The scaled production-view adapter preserves the requested scale and canonical parent/child identities. IFC export accepts explicit `assembly_geometry="tessellated"` for supported component meshes; native reimport checks the represented geometry and the exact assembly source dependency closure, including wall openings and level placement. Default guards remain. Elevation projection, arbitrary edited native geometry, regenerated parametric assemblies and manufacturer fit are not implied by these modes. Author-run focused evidence was 63 passing tests; independent source review passed this checkpoint.

`examples/reference_production_set.py` prepares representative, residential and commercial synthetic jobs through the same coordinator. Job fields, final cover index, model fingerprints, feeder identities, detail source IDs, retained graphics and artifact hashes are checked by `tests/test_reference_production_set.py`. These checks establish data connectivity, not professional drafting acceptance. Cover `907ec757`, site `70dfa9b2`, schedule position repair `52fc4981` and detail `74f82c68` received scoped independent visual review on their supplied fixtures. Single-line and combined-sheet composition remain subject to actual reference-style pixel review; earlier mechanical checks and unclipped outputs do not establish that acceptance. No Bonsai drawing-engine integration is claimed by these custom SVG/PDF commands.
