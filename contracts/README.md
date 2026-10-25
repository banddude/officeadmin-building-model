# Contracts

Canonical serialized contracts live here. Do not create competing workstream-local schemas.

## Current contract

- `oabm-model-v1.schema.json`: JSON Schema Draft 2020-12 for canonical model version `1.0.0`
- `oabm-model-v1.1.schema.json`: explicit version `1.1.0`, adding physical electrical boxes and selection outcomes
- `oabm-model-v1.2.schema.json`: explicit version `1.2.0`, adding whole-route raceway/cable product selections and unresolved outcomes
- `oabm-model-v1.3.schema.json`: explicit version `1.3.0`, adding shared support assemblies, posed components/interfaces, and selection outcomes; see [migration and limits](../docs/shared-supports.md)
- `oabm-model-v1.4.schema.json`: explicit version `1.4.0`, adding caller-confirmed structural members; see [migration and limits](../docs/structural-members.md)
- `oabm-model-v1.5.schema.json`: explicit version `1.5.0`, adding exact-product route-fitting selections, explicit interface-to-port bindings, and outcomes; see [bounded route-fitting contract](../docs/route-fitting-selection-contract.md)
- `oabm-model-v1.6.schema.json`: explicit version `1.6.0`, adding chosen presets plus separately identified physical parts; [bounded contract and consumers](../docs/building-assemblies-contract-v1.6-draft.md), part of [issue #356](https://github.com/banddude/officeadmin-building-model/issues/356)
- Python types and reference-integrity validation: `src/oabm/model/`
- Semantics and invariants: `docs/model-contract-v1.md`
- Contract fixtures: `fixtures/model/v1/`

Standalone input contract `raceway-policy-v1.schema.json` describes finite caller
baseline/company/job product permissions. Its strict reader/compiler lives in
`src/oabm/electrical/policy.py`; [semantics and integration](../docs/layered-raceway-policy.md)
preserve all canonical model versions and make no code-verification claim.

Derived report `panel-schedule-report-v1.schema.json` preserves nested canonical
source snapshots and supplied panel/circuit facts. See [report semantics and
explicit drawing adapter](../docs/panel-schedule-facts.md); it does not introduce
a building model version or a demand/engineering calculation.

Contract v1 covers coordinate system and units, stable object IDs, levels/stories, walls/slabs/ceilings/spaces, openings, electrical devices and equipment, ports/connectivity, obstacles and route constraints, 3D route centerlines and fittings, circuit/conductor hooks, provenance/confidence, serialization, and schema versioning.

## Change rule

Any contract change requires tests and migration notes. Version 1.0 remains the default and retains its serialized semantics. See [physical box contract and explicit migration](../docs/electrical-boxes.md) for version 1.1 and [raceway selections](../docs/raceway-selections.md) for version 1.2. A consumer must not silently reinterpret a document whose `schema_version` it does not support.

`scope-policy-v1.schema.json` and `scope-ledger-v1.schema.json` describe opt-in
caller scope decisions and source-bound object-count reports. They reference
existing canonical and panel-report snapshot definitions; canonical versions
and default exports remain unchanged. See [Scope ledger](../docs/scope-ledger.md).

`bounded-proposal-v1.schema.json` defines standalone request, result, configured
alias routing and measured benchmark records for the opt-in placement-ranking
boundary. It reuses canonical primitives and existing source snapshots; no
canonical migration or default behavior changes. See
[Bounded proposals](../docs/bounded-proposals.md).

Building-assembly contract `1.6.0` is explicitly selected; the default remains
`1.0.0`, with unchanged serialized semantics for versions 1.0 through 1.5.
Its records preserve immutable preset identities, typed resolved assumptions,
source evidence, physical component identities, and input/output digests.
Separate bounded producers support a straight timber wall with framed
rectangular voids and drywall, simple explicitly chosen door/window inserts,
and a rectangular suspended ceiling below an existing Slab deck. GLB and component quantities consume
the same physical records and suppress duplicate aggregate-host bodies or
physical quantity claims. Populated assemblies are refused by unsupported
routing, IFC, drawing, revision-diff, confidence-review and review-bundle
entrypoints. Empty upgraded models remain usable, including IFC round trip.

Full-room corners/joins, metal-stud presets, generated-deck production,
assembly-aware routing, regeneration and dependent route invalidation remain
unimplemented. Unmodeled items include boundary trim, tee joint hardware and
deck fasteners. The basic shapes do not certify loads, installation or
regulatory compliance. The schema covers structural shapes and evidence scopes;
cross-record identities, source agreement, geometry/host fit and digest
freshness remain Python semantic checks. See the [bounded contract](../docs/building-assemblies-contract-v1.6-draft.md)
and [wall producer](../docs/building-assembly-wall-producer-v1.md).
