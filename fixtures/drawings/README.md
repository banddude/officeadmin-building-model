# Drawing fixtures

All fixtures in this directory are synthetic.

- `v1/drawing-model.json` is a canonical `BuildingModel` fixture with two levels, rooms, walls, slabs, a ceiling, opening, electrical equipment/devices, a route and fittings, a circuit/conductor, an obstacle, and a route constraint.
- `v1/expected-drawing-set.sha256` is the deterministic SHA-256 fingerprint of the derived output for the fixed plan, elevation, section, and schedule specifications used by `tests/test_drawings.py`.

The expected drawing set is a test artifact only. It is not a canonical model and must never be treated as a model input.
