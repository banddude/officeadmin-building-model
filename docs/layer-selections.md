# Saved model layers and presentation

`oabm.model.layers` resolves portable saved selections over explicit canonical
membership. `contracts/oabm-layers-v1.schema.json` remains unchanged. A layer has
an ID, name, explicit member IDs and optional supplied system, trade and phase
labels. Labels do not infer membership or claim that an unmodeled system exists.
An assistant may supply membership using known canonical IDs.

A saved preset assigns every configured layer exactly one state: visible, hidden
or ghosted. Unknown fields, duplicate IDs/names/members, missing preset states,
dangling members and unknown level/isolate references refuse. Configuration JSON
round trips deterministically with `LayerSelectionConfig.dump/load`.

## Shared resolution

```python
from oabm.model import resolve_preset

selection = resolve_preset(config, model, preset="preset:review",
    isolate=(), level_ids=("level:one",), unassigned="error")
```

The resolver is the single state decision for all consumers:

1. Expand explicit host/assembly membership through canonical ownership. Building
   assembly parents include their physical components; walls include hosted
   openings and their selected inserts; ceiling hosts include their selected grid,
   tile and hanger children. A shared-support parent includes its components and
   interfaces. Shared supports **do not** inherit the systems of supported loads
   or routes: classify the shared assembly explicitly, once.
2. Overlapping layer membership uses strictest state: hidden, then ghosted, then
   visible. A separately classified child can therefore be hidden while its parent
   is visible. Layer ordering does not change the result.
3. `unassigned_ids` reports whole-configuration coverage, even under isolate or a
   level filter. Required `unassigned` policy is visible, hidden, ghosted or error.
4. Isolate considers only the selected layers and hides every other object.
5. A level filter hides objects outside the requested levels, including objects
   with unknown levels. Opening levels come from their canonical host; route
   levels come from the start-port owner. No level is inferred from coordinates.

The universe includes walls, slabs, spaces, ceilings, openings, building assembly
parents and components, equipment/devices, routes, boxes, structural members,
shared support parents/components/interfaces and fitting occurrences. Ports,
product selections, outcomes and physical-route joints/segments remain metadata;
explicit membership in these is reported as `out_of_scope_member_ids`. Conductor
meshes follow their owning route rather than becoming a second quantity.

Ceilings and openings without physical components remain addressable semantic
records in GLB; selection adds no invented ceiling thickness, opening solid or
hardware. Aggregate hosts have no duplicate body. Physical assembly components
are the actual canonical meshes. Empty layers and per-system coverage are always
reported. Selection never changes canonical schemas, identity, provenance,
geometry or whole-model quantities.

## GLB and retained saved requests

```python
from oabm.exports import build_layer_glb

delivery = build_layer_glb(model, config, preset="preset:review",
    unassigned="error", isolate=(), level_ids=("level:one",),
    physical_routes=False, include_obstacles=False)
```

This source-bound entrypoint captures and validates detached model/configuration
snapshots, resolves the saved request immediately before export, and returns
`source_model_json`, `config_json`, `manifest_json` and exact `glb_bytes`.
The manifest retains source/configuration, preset/isolate/level selection, complete
resolution, source/GLB hashes, replayable `export_options`, unassigned coverage and
`unknown_level_hidden_ids`. Saved per-level requests are retained here, without
changing the portable preset schema. Decode the retained source for downstream
consumers; integer normalization during canonical decoding can affect source hashes.

Every canonical universe ID appears once. State groups form a verified tree with
no orphan nodes or duplicate parents. Canonical assembly ownership stays in node
extras while display groups flatten the scene hierarchy, allowing independent
child states. Ghost meshes use real translucent materials for architecture,
assembly components, devices/equipment, routes/wires, boxes, fitting envelopes,
structural members, supports and supplied box obstacles. Meshless semantic records
have no surface to dim. `unsupported_ghost_ids` is empty for current supported kinds.

Hidden geometry remains in the GLB under `KHR_node_visibility`. Viewers must honor
that extension; a generic viewer may show hidden nodes. The backend alone does
not establish generic viewer compatibility. The specific verified local
OfficeAdmin GLB integration is described below.

`physical_routes=True` explicitly enables canonical curved routes.
`include_obstacles=True` includes all supplied obstacles in coverage, even hidden
or unassigned ones. Obstacles have unknown canonical level and are hidden by a
level filter. Unsupported obstacle geometry refuses even when hidden. Both flags
still default false. No routing, clearance or engineering approval is implied.

Existing finite-capture safeguards remain: independent model/configuration capture
budgets of 32 MiB, 250,000 nodes and depth 64; a separate 32 MiB/250,000 scalar-node
request budget; occurrence charging before deduplication. Cyclic, oversized/deep
metadata and infinite request iterables terminate with explicit diagnostics.

For lower-level use, `layer_glb_plan(model, selection, ...)` returns groups,
hidden_groups and dimmed_ids for `to_glb`. Arbitrary resolutions cannot prove
source freshness; prefer the source-bound entrypoint for delivery.

## Drawings and CAD/DXF

```python
from oabm.exports import apply_layer_selection

selected_view = apply_layer_selection(base_view, model, selection)
```

Generate a normal canonical drawing view first (use `assembly_geometry="cut"`
for populated assemblies), then apply the same resolution. The adapter removes
hidden primitives and annotations, renders ghosts with the explicit light-grey
thin `ghost` stroke and omits dimensions whose sources are hidden or ghosted.
Assembly-cut geometry belongs to its first source ID (the physical child), whose
resolved state already includes inherited membership. This avoids applying a
parent's state a second time. Support attachments become explicit paper-sized
cross/label vector marks; they do not claim hardware geometry. Other unsupported
symbol tokens still refuse in the existing bounded DXF adapter.

The selected DrawingView goes unchanged to `view_to_svg`, physical sheet SVG/PDF,
and `sheet_to_dxf`. DXF uses actual light-grey CAD entities for ghosts; hidden
objects are absent from this derived plotted view. Canonical model/quantities and
retained source remain complete. Current assembly drawings are exact plane cuts,
not hidden-line elevations or a promise to draw parts outside the view cut/depth.
Structural members use their canonical oriented envelopes in plans/sections.

## IFC

```python
from oabm.ifc import to_ifc

native = to_ifc(model, assembly_geometry="tessellated",
    layer_resolution=selection)
```

The same complete resolution assigns actual native shape representations to
`IfcPresentationLayerWithStyle` states. Hidden sets `LayerOn=False` and
`LayerFrozen=True`; visible/ghosted layers remain on. Ghost Body items receive
an actual `IfcSurfaceStyleRendering` with transparency 0.82. All products and
geometry remain in the IFC, including hidden objects, so canonical round trips
and stable GlobalIds are preserved. Derived route segments follow the route;
canonical fittings keep their independently resolved states. Logical parents
have no duplicate representation. No selection means existing IFC bytes remain
unchanged. IFC viewers must honor native presentation-layer flags for initial
hiding; interactive IFC viewer controls have not been verified here. The local
OfficeAdmin GLB acceptance below does not establish IFC viewer compatibility.
Existing IFC boundaries, including refusal of canonical physical curved routes,
remain intact.

## Reproducible synthetic demonstration

```sh
PYTHONPATH=src python examples/assembly_layer_selections.py /tmp/fresh-layer-room
```

The completed enclosed synthetic room includes wall framing/drywall, door/window
inserts, ceiling runners/tiles/hangers, explicit electrical power/lighting, a box,
route, shared support and structural member. Two saved presets reload from disk.
Five deliveries demonstrate reset, hidden/ghosted review, isolated power and two
level filters. Every delivery includes GLB, selection manifest, actual plan and
ceiling-cut SVG/DXF with companions, and IFC. `REPORT.json`, `quantities.json`,
`SHA256SUMS` and a fixed-camera `presets.png` retain evidence. The PNG reads actual
indexed GLB buffers, material alphas and inherited visibility using the existing
offline example decoder. It is display evidence, not an interactive viewer claim.
The example also proves unknown-level and deliberately unassigned disclosure.

The earlier `examples/physical_route_layers.py` remains the curved-route and
obstacle demonstration. The enclosed-room example uses explicit supplied
memberships; automatic classification is a separate supported entrypoint below.
Both examples use synthetic inputs. Artifact generation alone does not decide
issue closure.

## Automatic classification from existing canonical facts

`oabm.exports.classify_model_layers(model, include_obstacles=False)` generates
portable layers automatically, without caller-written member lists. Its detached
`LayerClassification` retains `source_model_json`, `config_json` and a source-hash
bound `manifest_json`; `.configuration` decodes the portable configuration.
The generated `preset:classified-all` is an all-visible reset; callers can add
saved hide/ghost presets over its stable generated layer IDs.

Trade and category follow canonical collection/kind facts, including actual
`tbar_main_runner`/`tbar_cross_runner` components in `ceiling_grid`. Explicit
`ElectricalDevice.system` and `ElectricalEquipment.system` values are retained
verbatim. Route/fitting categories are raceway, and shared supports are independent
of the systems they support. Construction-status layers consume only the existing
validated `attributes.pdf_electrical.scope_status` metadata on devices/equipment,
using the scope ledger's shared structural evidence validator. There is no
unmarked-object fallback. Status labels retain the exact source meanings (`new`,
`existing_to_remain`, `removed`, `relocated`); circuit electrical phase is never
interpreted as construction status.

Every supported universe ID receives trade/system/status memberships, including
explicit `unclassified` and `unresolved` buckets. The manifest lists each object's
source basis, canonical level and missing/contradictory facts. These unknown
buckets are not inferred systems or assertions that unmodeled site work, low
voltage, security, fire alarm or construction status exists. Names, prose,
nearest-object geometry and endpoint systems do not supply missing facts. The
normal ownership resolver can expand host/assembly membership, so category layers
can overlap; per-object physical quantities remain unchanged.

```sh
PYTHONPATH=src python examples/classified_layer_selections.py /tmp/fresh-classified-layers
```

This independent synthetic example supplies explicit system/status facts for two
devices, keeps an unknown device, saves/reloads two presets, and exports reset,
review, per-level and ceiling-grid-isolate GLBs with saved request manifests.
Automatic membership in this bounded taxonomy is implemented and verified from
explicit canonical facts. It does not establish broad plan recognition or invent
missing system/status facts. Interactive OfficeAdmin acceptance is a separate
verified local integration below; final issue acceptance remains a review/gate
decision.

Native viewer probe: Blender 5.2.2 LTS/Bonsai 0.8.5 imported the synthetic isolate
GLB and displayed its hidden room geometry. That installed import path does not
honor the saved `KHR_node_visibility` state. No manual native-object hiding is
counted as preset consumption. This remains a consumer-specific compatibility
failure. The offline renderer is not interactive; the separate actual Chrome
OfficeAdmin harness below establishes the bounded live-control acceptance.

## Native named memberships and assembly-aware review delivery

`to_ifc(..., layer_delivery=delivery)` accepts the existing source-bound
`LayerGlbDelivery`. It replays the retained model/configuration/request/GLB,
uses the verified resolution for presentation states, and writes one standard
`IfcGroup` per original named layer. Membership expands only canonical ownership;
a product can belong to several groups, without copying geometry. Empty layers
remain named empty groups. `OABM_LayerSelections` on the project retains the full
portable configuration, saved request, resolution, explicit/expanded/out-of-scope
IDs and native group identities. Names exceeding IFC's 255-character label
capacity refuse; no silent truncation changes their meaning.
`read_ifc_layer_memberships(native)` checks canonical round-trip/source identity,
regenerated layer metadata and actual native group membership. Default exports
without this option retain their previous behavior.

`sheet_to_dxf(drawing_set, spec, placements, layer_model=model,
layer_delivery=delivery)` verifies the same retained source and automatically
applies its resolution to the derived views. It writes native DXF `GROUP`s over
existing plotted handles, preserving each entity's primitive CAD layer. Shared
membership shares handles rather than duplicating linework. Empty/non-plotted
layers remain in the `OABM_LAYER_SELECTIONS` XRECORD and companion manifest;
empty native groups are omitted because DXF readback audits remove them.
The metadata preserves full original names and IDs alongside stable native names.
`read_dxf_layer_memberships(bytes, model)` verifies retained source/configuration,
group memberships, canonical source IDs and handle existence. **It does not
verify CAD coordinates, canonical geometry freshness, or coordinate-tamper
resistance.** CAD remains editable derived output. Its membership can remain
valid after coordinates change; geometric/quantity claims still come from the
canonical model.

`build_layer_review(delivery, review_config=ReviewConfig(...),
source_references=..., evidence=...)` builds the existing four-artifact
`officeadmin.bim-review-file` version 1.0.0 and returns its JSON together with the
existing saved-selection manifest sidecar. No canonical, review-bundle or review-file
schema is relabeled. `read_layer_review(review_json, selection_json)` checks
artifact hashes, strict canonical decoding/validation, exact GLB regeneration,
complete canonical rendered-node coverage, confidence/provenance/crop/source-map
joins and the same saved configuration/request/resolution. It retains the original
model version, including 1.6 assemblies. Configuration and request remain explicit
in the companion selection artifact, bound to the source/GLB; hashes identify
content and do not authenticate who supplied it.

The existing `build_confidence_review` and `build_review_bundle` APIs require
explicit `building_assemblies=True` for populated assemblies. Their inventories
then include canonical assembly parents and physical components, with source
provenance and actual quantity warnings. Existing defaults still refuse populated
assemblies. Physical curved routes retain their whole-model quantity limitation.

All new review JSON inputs (outer wrapper, canonical model, confidence review,
source mapping and selection sidecar) receive existing bounded UTF-8/depth/node
admission before parsing: 32 MiB per JSON input, depth 64 and 250,000 tokens/nodes,
with the review-file wrapper additionally limited to the existing 20 MiB transport
size. Duplicate keys remain refused, and resource/recursion failures return explicit
`ValueError` diagnostics instead of raw recursion failures.

The enclosed-room example now retains native membership readbacks and
`model.oabm-review.txt` plus `selection.json` for every preset/level delivery.
The payload keeps the existing transport contract. Its matching selection
sidecar is consumed by the verified local OfficeAdmin integration below.

## Verified local OfficeAdmin integration

The separate `banddude/officeadmin-books` branch `feat/bim-saved-layer-viewer` at
`dd5653a22cd2d1210513950001858973dcc0177c` admits the existing 1.6/1.7 assembly
review records and the matching source/GLB-bound selection sidecar. It checks
host/assembly level agreement, unique active host claims, nonempty owned children,
rendered component ownership and meshless semantic parents. ID lookup precedes
exact-name lookup even for legal cross-ID/name collisions. New file choices and
reset invalidate stale asynchronous sidecar successes and failures.

Actual Chrome/WebGL acceptance used the existing OfficeAdmin review components
and synthetic 168-object enclosed-room/electrical delivery, two persisted presets,
per-level selection, hide, isolate, ghost and reset controls. Reset and preset
reload restore identical viewport pixels; source model, bundle and GLB bytes stay
unchanged. Unknown-level/unassigned coverage stays disclosed, and shared supports
retain independent membership. The authorized MCP reader uses the same browser
resolver for read-only inspection. This TypeScript implementation follows the
engine 1.0.0 resolver contract and is pinned against five actual engine-generated
resolution manifests; it does not execute Python in the browser or classify facts
from names or geometry.

The reproducible OfficeAdmin harness is `scripts/bim-review/inspect-assembly.mjs`,
with `?fixture=assembly` and `?fixture=assembly-ghost`. Local external evidence is
retained in `oa-layer-viewer-evidence/REPORT.json`, `SHA256SUMS`, and screenshots
`01-reset.png` through `08-loaded-ghost-made-visible.png`. These synthetic receipts
are outside this engine repository. The initially ghosted delivery demonstrably
becomes visible when all-visible is selected, and review/reset restore its exact
ghost pixels. The final OA repair passed 84 focused tests, targeted typecheck/lint,
actual browser acceptance and independent source review at the head above.

This is a locally verified implementation, **not production deployed**. Local
viewer edits do not overwrite persisted engine presets. Exported ghost materials
may already have lost original colours: the viewer retains those grey colours
when restoring visible opacity and does not fabricate original materials. Glass
remains translucent. The existing unsupported canonical curved-route IFC boundary
and physical-route whole-model quantity limitation remain unchanged. Generic
viewers, including the Blender path above, are not certified by this acceptance.
