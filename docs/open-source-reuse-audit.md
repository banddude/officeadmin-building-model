# Open-source reuse audit (#93)

Updated 2026-10-11 against main `38abc3a5cd165c4175456acb4fb41f06326b3f80`.
This supersedes stale implementation recommendations in the
[2026-09-23 audit](https://github.com/banddude/officeadmin-building-model/issues/93#issuecomment-5799958525).
The goal is a personal, automatic real-plan feasibility proof. Licenses inform
distribution decisions; an inconvenient license alone does not disqualify a
useful prototype. Code without clear reuse terms is not copied.

## What already exists

Read [architecture](architecture.md), [canonical contract](model-contract-v1.md),
[routing](routing.md), [IFC adapter](ifc-adapter.md), [drawings](drawings.md),
[GLB export](glb-export.md) and [responsibility relay](responsibility-relay.md)
before proposing infrastructure. `BuildingModel` owns metres, stable IDs,
ports/circuits, geometry, provenance and deterministic serialization.
RoomPlan and PDF importers emit it; routing consumes it; IFC, GLB, quantities
and SVG drawings derive from it. No candidate becomes a second semantic model.

The September audit's main implementation gaps have changed:

- #85 is closed: IFC now has native architecture/device/equipment/obstacle
  Bodies where explicit geometry permits, with recorded no-Body reasons where
  it does not. Conduit solids, connectivity and native edits already round-trip.
- PR #73 is merged. `src/oabm/importers/tables/` now wraps Camelot and optional
  Docling; PDF semantic validation remains separate from table extraction.
- GLB meshes and conductor display plus canonical derived SVG drawings exist.
  They do not constitute a complete automatic render or native CAD pipeline.
- The router already handles deterministic 3D detours, directions, keep-outs,
  corridors, bends and fitting selection. Existing topology is not replaced
  just because a candidate advertises graph algorithms.
- There is no engineering calculation lane or independently verified
  jurisdiction pack. A canonical circuit load is VA, not an implicit watt value.

Gate B/C tests are public synthetic engine proofs. They do not certify reliable
recognition of normal commercial plan sets or close #42's private real-plan gate.
The [October owner update](https://github.com/banddude/officeadmin-building-model/issues/93#issuecomment-5976212015)
requires 3D, BIM/IFC, renders and CAD drawings; Blender is a tool choice, not an
input or core-model requirement.

## Comparison and decisions

Upstream sources were rechecked on 2026-10-11. Unless pinned below, licenses
describe the checked project rather than a promise about every future release.

| Area / candidate | Capability and canonical integration point | Integration cost, failure modes and distribution information | Decision |
|---|---|---|---|
| [IfcOpenShell / Bonsai](https://github.com/IfcOpenShell/IfcOpenShell), [geometry tree](https://docs.ifcopenshell.org/ifcopenshell-python/geometry_tree.html), [IfcDiff](https://docs.ifcopenshell.org/ifcdiff.html) | Existing `oabm.ifc` maps canonical products, geometry, ports and stable IDs. Native Bodies can feed IFC-side collision/clearance QA; IfcDiff can compare derived IFC revisions. Bonsai edits native objects while OABM controls canonical readback. | Low cost for existing adapter; clearance is only as complete as available Bodies and dimensions. IfcDiff is not the contract's canonical numerical-equivalence comparator. IfcOpenShell LGPL, Bonsai GPL: retain notices and applicable source obligations when distributing components. | **Reuse implemented IFC integration.** Keep canonical adapter and its explicit unknown-geometry decisions. Use the existing geometry/diff tooling for future IFC-side QA rather than writing another solids kernel, clash engine or IFC diff engine. |
| [pdfplumber](https://github.com/jsvine/pdfplumber), [Camelot](https://github.com/camelot-dev/camelot), [Docling](https://github.com/docling-project/docling) | Source primitives and source table cells feed importer evidence. `importers/tables` already returns normalized cells/coordinates from Camelot/Docling; canonical devices/circuits remain the importer output. | Moderate: rotation, fragmented rulings, empty pages and backend failures need explicit handling. A ruled keynote box can look like a schedule; table geometry does not prove panel identity or row meaning. MIT project licenses; Docling's separate models/dependencies need their own distribution review. | **Reuse current extraction adapters.** Keep fail-closed semantic/provenance checks. Stop writing another generic table detector or changing parsers solely to increase recognition counts. |
| [Graph Studio / BuildGraph](https://github.com/muhamadbyaba/Graph-Studio/tree/9cb234266ae06927dcdb1b2de3b0a8a65d8d9f9d/packages/engineering-core) | Pure SI quantities/current/voltage-drop functions can accept a resolved canonical circuit and measured route length, returning a derived report with assumptions. Registry and sizing rules are broader possible reuse, not proven by this spike. | Moderate Python/TypeScript boundary; package is private and requires Node >=23.6. Its draft US pack inherits representative constants and explicitly says VERIFY. Apparent VA must be converted before a watts API. Apache-2.0: retain license/NOTICE; no code-compliance inference. | **Adapt a bounded offline comparison now**, not a runtime dependency. The retained spike below proves only two functions. Pause a custom generic rule/units/trace framework until a follow-up measures registry value against independent known answers. Keep unverified sizing constants out of operational outputs. |
| [TopologicPy / Topologic](https://github.com/wassimj/topologicpy) | Mixed-dimensional topology and spatial graphs could derive space/portal adjacency or candidate corridors from canonical geometry. Return ID-mapped candidate adjacency with confidence, not replacement building objects. | Moderate/high conversion and scientific-stack cost; false adjacency or lost openings/units/provenance would misroute. Current LICENSE and README now both say LGPL-3.0, resolving the earlier README mismatch. Verify the selected Topologic core/package licenses as well. | **Keep current canonical graph/router. Defer** an adjacency spike until a measured spatial failure justifies it. Native solids now exist, so their absence is no longer the deferral reason. No new topology service today. |
| [Speckle server](https://github.com/specklesystems/speckle-server), [specklepy](https://github.com/specklesystems/specklepy) | Derived snapshots could carry canonical IDs, geometry and provenance into interchange, versioning/connectors or web viewing. Export-only mapping keeps source authority in OABM. | High relative to offline proof: server/transport, object mapping and viewer semantics. SDK Apache-2.0; server LICENSE has Apache-2.0 outside its explicitly listed enterprise workspace/gatekeeper directories. Retain notices and review included modules. | **Defer deployment**, not because of hypothetical commercial licensing. Use existing IFC/GLB first. Before custom collaborative history/model serving/web viewing, evaluate this export-only boundary on a demonstrated requirement. |
| [Bonsai pathfinder proposal](https://github.com/IfcOpenShell/IfcOpenShell/issues/6521), [red1oon/mep_engineering](https://github.com/red1oon/mep_engineering), [NetworkX](https://github.com/networkx/networkx), [COMPAS](https://github.com/compas-dev/compas) | Blender MEP tools or graph/geometry helpers overlap our deterministic router. An algorithm helper would operate on canonical obstacles/ports and return candidate paths for OABM validation. | High semantic mapping cost for wholesale replacement: clearance, required corridors, fitting order, stable IDs and determinism remain ours. The Bonsai proposal is now closed, but its issue is not proof of an accepted end-to-end replacement. red1oon README describes direct routing with A* later and has no root license file; do not copy until clarified. NetworkX BSD-3-Clause; COMPAS MIT. | **Keep current router.** Reuse a helper only for a reproducible failed case with better results. Do not rebuild our already-tested expanded routing state around a generic graph library. |
| [Blender command-line rendering](https://docs.blender.org/manual/en/latest/advanced/command_line/arguments.html), [ezdxf](https://github.com/mozman/ezdxf) | Existing derived GLB/IFC can feed a headless renderer. Existing `DrawingView` primitives are the boundary for a future DXF writer; PDF/SVG/meshes do not substitute for editable CAD entities. | Render choices require deterministic camera/material/unit setup and distinction between observed and inferred geometry. CAD needs layer/block/dimension mappings and independent readback. Blender GPL; ezdxf MIT, with normal notice obligations. | **Keep existing GLB/IFC/SVG outputs.** Prefer existing renderer/DXF libraries to a custom render engine or DXF format implementation. Automatic rendering and CAD are still unproved adapters: evaluate them separately with visual/readback known answers before adopting them. This audit does not claim they are delivered. |

## Retained proof, not README claims

The two-file [Graph Studio spike](../spikes/graph_studio/README.md) retains
verbatim upstream source, hashes, Apache license and NOTICE. It requires no
network or upstream checkout to execute. It reads only the public synthetic
garage fixture and does not modify any canonical object.

At `9600 VA`, `240 V`, `7.3 m`, explicit `pf=1` and hypothetical `r=.001 ohm/m`,
the real upstream functions produce `40 A`, `.584 V`, `.24333333333333335%`.
Independent SI formulas supply the expected answer. `pf=.8` must still produce
`40 A`, preventing apparent-power/real-power confusion. Missing phase/load or
route evidence and invalid assumptions fail closed. Zero resistance is a
numeric boundary, not a conductor recommendation. No jurisdiction pack,
ampacity table, shared-neutral rule or wire-size selection is exercised.

Existing retained integration tests supersede the old throwaway IFC/table
spikes: `test_ifc_arch_solids.py` and `test_ifc_device_solids.py` triangulate
native Bodies and check dimensions/volume/EXPRESS/round trips;
`test_pdf_tables.py` exercises real extraction, rotation, cell geometry,
repeatability and fail-closed backend errors. Gate B/C test real producer
objects through route/connected IFC/quantities/drawings, rather than a
candidate's self-described capabilities.

With Python extras `dev,tables,registration` installed and Node 24 on PATH:

```sh
python spikes/graph_studio/spike.py --power-factor 1 --resistance-ohm-per-m 0.001
pytest -q tests/test_reuse_calculation_spike.py tests/test_ifc_arch_solids.py tests/test_ifc_device_solids.py tests/test_pdf_tables.py tests/test_gate_b_synthetic_garage.py tests/test_gate_c_roomplan_end_to_end.py
python -m compileall -q src tests spikes/graph_studio
pytest -q -n auto
```

No new implementation issue is needed merely to close this audit. A later
calculation-registry, clearance-QA, topology, rendering, CAD or transport issue
must name the measured gap, selected integration boundary and independent
acceptance case before adding dependencies. The recommendation to reuse an
existing library is not evidence that its untested adapter is complete.
