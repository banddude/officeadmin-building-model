# Contracts

Canonical serialized contracts live here. Do not create competing workstream-local schemas.

## Current contract

- `oabm-model-v1.schema.json`: JSON Schema Draft 2020-12 for canonical model version `1.0.0`
- Python types and reference-integrity validation: `src/oabm/model/`
- Semantics and invariants: `docs/model-contract-v1.md`
- Contract fixtures: `fixtures/model/v1/`

Contract v1 covers coordinate system and units, stable object IDs, levels/stories, walls/slabs/ceilings/spaces, openings, electrical devices and equipment, ports/connectivity, obstacles and route constraints, 3D route centerlines and fittings, circuit/conductor hooks, provenance/confidence, serialization, and schema versioning.

## Change rule

Any contract change requires tests and migration notes. The initial `1.0.0` version has no predecessor, so no migration is required yet. A consumer must not silently reinterpret a document whose `schema_version` it does not support.
