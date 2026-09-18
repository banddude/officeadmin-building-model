# Golden fixtures and cross-workstream QA

Issue #10 owns the small, public-safe known-answer corpus under `fixtures/golden/v1/` and the reusable helpers in `oabm.qa`. The canonical model contract in `oabm.model` remains unchanged and is still the only semantic interchange format.

## What the harness checks

The suite exercises stable document-global IDs; strict canonical JSON round trips; deterministic serialization fingerprints; JSON Schema validity; global reference integrity; explicit symmetric port connectivity; 3D geometry and levels; route endpoint alignment; route/fitting ownership and order; circuits/conductors; provenance/confidence; and negative cases that must fail at a workstream boundary.

Every positive checked-in fixture carries `source_kind="synthetic"` provenance with a `fixture:` source ID. No customer plans, private project facts, credentials, or production data are permitted in this suite.

## Cross-workstream use

`validate_lane_model(model)` is intentionally narrow: it invokes the canonical validator, performs a strict serialize/readback cycle, verifies entity IDs survive unchanged, and returns a deterministic SHA-256 fingerprint. It does not add a competing model or reinterpret contract fields.

The manifest adds QA-only expectations for known fixtures: model ID, collection counts, required IDs, level elevations, route presence, ordered route IDs, and a golden fingerprint. These expectations describe test inputs/outputs only and are not part of the canonical schema.

The impossible-route fixture is constrained input with no route object. The multiple-valid-routes fixture contains two reference alternatives without selecting one. This keeps the QA lane independent of the routing algorithm while still giving that lane reproducible acceptance inputs.

## Running

Use the same commands as CI:

```text
python -m pip install -e '.[dev]'
python -m compileall -q src tests
pytest -q
```
