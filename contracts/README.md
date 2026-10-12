# Contracts

Canonical serialized contracts live here. Do not create competing workstream-local schemas.

## Current contract

- `oabm-model-v1.schema.json`: JSON Schema Draft 2020-12 for canonical model version `1.0.0`
- `oabm-model-v1.1.schema.json`: explicit version `1.1.0`, adding physical electrical boxes and selection outcomes
- `oabm-model-v1.2.schema.json`: explicit version `1.2.0`, adding whole-route raceway/cable product selections and unresolved outcomes
- `oabm-model-v1.3.schema.json`: explicit version `1.3.0`, adding shared support assemblies, posed components/interfaces, and selection outcomes; see [migration and limits](../docs/shared-supports.md)
- Python types and reference-integrity validation: `src/oabm/model/`
- Semantics and invariants: `docs/model-contract-v1.md`
- Contract fixtures: `fixtures/model/v1/`

Contract v1 covers coordinate system and units, stable object IDs, levels/stories, walls/slabs/ceilings/spaces, openings, electrical devices and equipment, ports/connectivity, obstacles and route constraints, 3D route centerlines and fittings, circuit/conductor hooks, provenance/confidence, serialization, and schema versioning.

## Change rule

Any contract change requires tests and migration notes. Version 1.0 remains the default and retains its serialized semantics. See [physical box contract and explicit migration](../docs/electrical-boxes.md) for version 1.1 and [raceway selections](../docs/raceway-selections.md) for version 1.2. A consumer must not silently reinterpret a document whose `schema_version` it does not support.
