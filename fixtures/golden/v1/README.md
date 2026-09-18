# Golden regression suite v1

This directory is the independent QA lane for issue #10. Every positive fixture is a complete canonical `BuildingModel` v1 document made only from synthetic data. `manifest.json` declares the known-answer cases, deterministic SHA-256 fingerprints, and small structural expectations consumed by `oabm.qa`.

Positive cases:

- `rectangular-room.json` — exact 5 m x 4 m x 3 m room geometry.
- `door-obstruction.json` — hosted door opening plus a hard routing keep-out.
- `panel-to-evse.json` — equipment/device ports, symmetric connectivity, route, circuit, conductors, and non-default confidence.
- `elevation-change.json` — 3D rise/run/drop route and ordered fittings.
- `multiple-valid-routes.json` — two canonical alternatives around one obstacle; the QA lane does not pick a winner.
- `impossible-route.json` — constrained input with no fabricated route result.
- `two-level-building.json` — explicit stories/elevations and a cross-level route.
- `synthetic-garage.json` — the public-safe canonical fixture later convergence lanes can consume for Gate B.

`invalid/` contains intentionally broken documents for strict boundary tests. They must never be treated as model examples or importer output.

## Reuse from another lane

A producer can validate any canonical handoff with:

```python
from oabm.qa import validate_lane_model

fingerprint = validate_lane_model(model)
```

For the checked-in suite, call `validate_golden_suite()` or load individual manifest cases with `load_golden_cases()`. These helpers do not implement IFC, routing, ingestion, quantities, or drawings; they only validate the canonical contract and declared QA expectations.
